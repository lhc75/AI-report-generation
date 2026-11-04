"""数据统计接口与旧版 dashboard 统计口径回归。"""
import io
from uuid import uuid4

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import dashboard, storage
from app.db import get_db
from app.deps import get_current_user
from app.models import ProjectFile, User
from app.routers import dashboard as dashboard_router

CSV_TEXT = """username,city,area,school,schoolId,grade,submit_count,update_time,question1,question2
u1,济南市,历下区,第一中学,1001,七年级,2,2026-10-01 08:00:00,A,x
u2,济南市,历下区,第一中学,1001,八年级,3,2026-10-02 09:00:00,B,y
u3,青岛市,市南区,第二中学,2001,七年级,4,2026-10-03 10:00:00,A,z
u4,青岛市,市南区,第二中学,2001,七年级,1,2026-10-01 11:00:00,C,w
"""


def sample_raw(encoding="utf-8-sig"):
    return CSV_TEXT.encode(encoding)


def sample_frame():
    return dashboard._read_dataframe("answers.csv", sample_raw(), meta_only=True)


def make_data_file(project, db, raw=None, name="answers.csv"):
    item, _task, user = project
    path = f"{user.id}/{item.id}/{uuid4().hex}/{name}"
    raw = sample_raw() if raw is None else raw
    storage.upload_bytes(path, raw)
    file = ProjectFile(project_id=item.id, file_type="data", file_name=name,
                       minio_path=path, size=len(raw))
    db.add(file)
    db.commit()
    return file


def _app(db, user=None):
    app = FastAPI()
    app.include_router(dashboard_router.router)
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: db
    return app


def test_overview_matches_original_dashboard_counts():
    result = dashboard.build_overview(sample_frame(), "第一", "")
    assert result["summary"] == {
        "total_submissions": 10,
        "student_count": 4,
        "school_count": 2,
        "last_update": "2026-10-03T10:00:00",
    }
    assert result["charts"]["city"] == [
        {"name": "济南市", "value": 5},
        {"name": "青岛市", "value": 5},
    ]
    assert result["charts"]["grade"] == [
        {"name": "七年级", "value": 7},
        {"name": "八年级", "value": 3},
    ]
    query = result["school_query"]
    assert query["found"] is True
    assert query["school"] == "第一中学"
    assert query["total_submissions"] == 5
    assert query["student_count"] == 2
    assert query["grades"] == [{"name": "七年级", "value": 2}, {"name": "八年级", "value": 3}]


def test_school_table_and_school_id_query():
    frame = sample_frame()
    table = dashboard.build_school_table(frame, [])
    assert table["total"] == 3
    assert table["rows"] == [
        {"city": "济南市", "area": "历下区", "school": "第一中学", "schoolId": "1001",
         "grade": "七年级", "submit_count": 2},
        {"city": "济南市", "area": "历下区", "school": "第一中学", "schoolId": "1001",
         "grade": "八年级", "submit_count": 3},
        {"city": "青岛市", "area": "市南区", "school": "第二中学", "schoolId": "2001",
         "grade": "七年级", "submit_count": 5},
    ]
    exact = dashboard.build_overview(frame, "", "2001")["school_query"]
    assert exact["total_submissions"] == 5
    assert exact["student_count"] == 2
    assert exact["grades"] == [{"name": "七年级", "value": 5}]
    missing = dashboard.build_overview(frame, "不存在", "")["school_query"]
    assert missing == {"found": False}


def test_text_range_date_filters_and_record_pagination():
    frame = sample_frame()
    text = dashboard_router.DashboardFilter(column="city", kind="text", value="青岛")
    filtered = dashboard.build_records(frame, [text], 1, 2)
    assert filtered["total"] == 2
    assert [row["username"] for row in filtered["rows"]] == ["u3", "u4"]

    numeric = dashboard_router.DashboardFilter(column="submit_count", kind="range", minimum=2, maximum=4)
    filtered = dashboard.build_records(frame, [numeric], 1, 10)
    assert filtered["total"] == 3
    assert {row["username"] for row in filtered["rows"]} == {"u1", "u2", "u3"}

    date = dashboard_router.DashboardFilter(column="update_time", kind="date",
                                            start="2026-10-02", end="2026-10-03")
    filtered = dashboard.build_records(frame, [date], 1, 10)
    assert filtered["total"] == 2
    assert {row["username"] for row in filtered["rows"]} == {"u2", "u3"}

    page = dashboard.build_records(frame, [], 2, 2)
    assert page["page"] == 2
    assert page["total"] == 4
    assert [row["username"] for row in page["rows"]] == ["u3", "u4"]


def test_csv_encodings_xlsx_first_sheet_and_schema_errors():
    gb = dashboard._read_dataframe("gb.csv", sample_raw("gb18030"), meta_only=True)
    assert len(gb) == 4
    assert gb.iloc[0]["city"] == "济南市"

    excel = pd.DataFrame([
        ["username", "city", "area", "school", "schoolId", "grade", "submit_count", "update_time", "question1"],
        ["u1", "济南市", "历下区", "第一中学", "1001", "七年级", 2, "2026-10-01", "A"],
    ])
    ignored = pd.DataFrame([["ignored"], ["ignored"]])
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        excel.to_excel(writer, index=False, header=False, sheet_name="作答")
        ignored.to_excel(writer, index=False, header=False, sheet_name="忽略")
    frame = dashboard._read_dataframe("answers.xlsx", buffer.getvalue(), meta_only=True)
    assert len(frame) == 1
    assert list(frame.columns) == dashboard.META_COLS

    with pytest.raises(dashboard.DashboardDataError, match="缺少必要列.*update_time"):
        dashboard._read_dataframe("bad.csv", "username,city\nu1,济南市\n".encode("utf-8"), meta_only=True)
    with pytest.raises(dashboard.DashboardDataError, match="没有可统计的记录"):
        dashboard._read_dataframe("empty.csv", sample_raw().splitlines()[0] + b"\n",
                                  meta_only=True)
    with pytest.raises(dashboard.DashboardDataError, match="解析失败"):
        dashboard._load_frame_or_error(
            "broken.csv",
            "username,city,area,school,schoolId,grade,submit_count,update_time\nu1,\"unclosed\n".encode("utf-8"),
            meta_only=True,
        )
    with pytest.raises(dashboard.DashboardDataError, match="仅支持"):
        dashboard._read_dataframe("bad.txt", b"text", meta_only=True)


def test_full_records_include_question_columns():
    frame = dashboard._read_dataframe("answers.csv", sample_raw(), meta_only=False)
    payload = dashboard.build_records(frame, [], 1, 2)
    assert payload["columns"][-2:] == ["question1", "question2"]
    assert payload["rows"][0]["question1"] == "A"


def test_dashboard_api_requires_auth_and_enforces_project_scope(project, db):
    item, _task, user = project
    file = make_data_file(project, db)
    other = User(username="other-dashboard-user", password_hash="offline")
    db.add(other)
    db.commit()

    with TestClient(_app(db)) as client:
        assert client.get("/api/dashboard/sources").status_code == 401
        response = client.get("/api/dashboard/sources")
        assert response.status_code == 401

    with TestClient(_app(db, other)) as client:
        response = client.get("/api/dashboard/overview",
                              params={"project_id": item.id, "file_id": file.id})
    assert response.status_code == 404
    assert response.json()["detail"] == "项目不存在"


def test_dashboard_api_returns_sources_overview_and_paginated_records(project, db):
    item, _task, user = project
    file = make_data_file(project, db)
    with TestClient(_app(db, user)) as client:
        sources = client.get("/api/dashboard/sources")
        assert sources.status_code == 200
        projects = sources.json()["projects"]
        assert projects[0]["id"] == dashboard_router.BUILTIN_PROJECT_ID
        project_payload = next(entry for entry in projects if entry["id"] == item.id)
        assert project_payload["data_files"][0]["id"] == file.id

        overview = client.get("/api/dashboard/overview", params={
            "project_id": item.id, "file_id": file.id, "school_keyword": "第二",
        })
        assert overview.status_code == 200
        body = overview.json()
        assert body["file"]["file_name"] == "answers.csv"
        assert body["summary"]["total_submissions"] == 10
        assert body["school_query"]["schoolId"] == "2001"

        records = client.post("/api/dashboard/records", json={
            "project_id": item.id,
            "file_id": file.id,
            "mode": "full",
            "page": 2,
            "page_size": 3,
            "filters": [{"column": "city", "kind": "text", "value": ""}],
        })
        assert records.status_code == 200
        payload = records.json()
        assert payload["page"] == 2
        assert payload["page_size"] == 3
        assert payload["total"] == 4
        assert len(payload["rows"]) == 1
        assert payload["rows"][0]["username"] == "u4"

        school = client.post("/api/dashboard/school-table", json={
            "project_id": item.id,
            "file_id": file.id,
            "filters": [{"column": "submit_count", "kind": "range", "minimum": 3, "maximum": 5}],
        })
        assert school.status_code == 200
        assert school.json()["total"] == 2


def test_builtin_student_csv_sources_can_be_queried(project, db, tmp_path, monkeypatch):
    item, _task, user = project
    data_dir = tmp_path / "学生作答情况_12.14"
    data_dir.mkdir()
    (data_dir / "内置样例.csv").write_bytes(sample_raw())
    monkeypatch.setattr(dashboard, "BUILTIN_DATA_DIR", data_dir)

    with TestClient(_app(db, user)) as client:
        sources = client.get("/api/dashboard/sources")
        assert sources.status_code == 200
        builtin = sources.json()["projects"][0]
        assert builtin["id"] == dashboard_router.BUILTIN_PROJECT_ID
        assert builtin["name"] == dashboard_router.BUILTIN_PROJECT_NAME
        assert [file["file_name"] for file in builtin["data_files"]] == ["内置样例.csv"]
        file_id = builtin["data_files"][0]["id"]

        overview = client.get("/api/dashboard/overview", params={
            "project_id": dashboard_router.BUILTIN_PROJECT_ID,
            "file_id": file_id,
        })
        assert overview.status_code == 200
        assert overview.json()["summary"]["total_submissions"] == 10

        records = client.post("/api/dashboard/records", json={
            "project_id": dashboard_router.BUILTIN_PROJECT_ID,
            "file_id": file_id,
            "mode": "meta",
            "page": 1,
            "page_size": 2,
        })
        assert records.status_code == 200
        assert records.json()["total"] == 4
        assert len(records.json()["rows"]) == 2

        traversal = client.get("/api/dashboard/overview", params={
            "project_id": dashboard_router.BUILTIN_PROJECT_ID,
            "file_id": "builtin:..\\outside.csv",
        })
        assert traversal.status_code == 404


def test_dashboard_api_reports_invalid_schema_and_page_size(project, db):
    item, _task, user = project
    bad_raw = "username,city\nu1,济南市\n".encode("utf-8")
    file = make_data_file(project, db, raw=bad_raw, name="bad.csv")
    with TestClient(_app(db, user)) as client:
        response = client.get("/api/dashboard/overview",
                              params={"project_id": item.id, "file_id": file.id})
        assert response.status_code == 400
        assert "缺少必要列" in response.json()["detail"]

        file_id = make_data_file(project, db).id
        response = client.post("/api/dashboard/records", json={
            "project_id": item.id, "file_id": file_id, "page": 1, "page_size": 201,
        })
        assert response.status_code == 422






