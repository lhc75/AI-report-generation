import io
import os
import time
from uuid import uuid4
from zipfile import ZipFile

import pytest
from docx import Document
from docx.oxml.ns import qn
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select, update

from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import ModelConfig, User
from app.security import create_token
from app.survey_models import SurveyReport
from app import survey_worker


def workbook(rows):
    book = Workbook()
    sheet = book.active
    sheet.append(["", "", "", "", "", "", "", "教育调查"])
    sheet.append(["账号", "姓名", "学校", "学校ID", "市/州", "区县", "问卷", "满意度", "资源（多选）", "数值"])
    for i, row in enumerate(rows, 1):
        sheet.append([str(i), "测试姓名", *row])
    result = io.BytesIO()
    book.save(result)
    return result.getvalue()


def docx_template(marker, body="原来的正文"):
    doc = Document()
    doc.add_paragraph("原来的封面")
    doc.add_paragraph(marker)
    doc.add_paragraph(body)
    doc.add_paragraph("模板页尾")
    result = io.BytesIO()
    doc.save(result)
    return result.getvalue()


def await_state(client, url, predicate, timeout=25):
    stop = time.monotonic() + timeout
    while time.monotonic() < stop:
        response = client.get(url)
        assert response.is_success, response.text
        data = response.json()
        if predicate(data):
            return data
        time.sleep(0.12)
    pytest.fail(f"后台任务未完成 {data}")


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    for username in ("batch-owner", "batch-other"):
        db.add(User(username=username, password_hash="unused"))
    db.commit()
    users = db.scalars(select(User).order_by(User.id)).all()
    monkeypatch.setattr(settings, "SURVEY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "offline-test-only-for-survey-batches")
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {create_token(users[0].id)}"
        yield client, {"Authorization": f"Bearer {create_token(users[1].id)}"}


def successful(response):
    assert response.is_success, response.text
    return response.json()


def prepare(client, rows):
    project = successful(client.post("/api/surveys", json={"name": "年度问卷", "province": "测试省", "year": 2026}))
    base = f"/api/surveys/{project['id']}"
    source = successful(client.post(base + "/assets", data={"kind": "data"}, files={"file": ("答卷.xlsx", workbook(rows))}))
    mapping = source["sheets"][0]
    assert mapping["header"] == 2
    assert mapping["fields"] == {"city": 4, "county": 5, "school": 2, "school_id": 3, "questionnaire": 6}
    request = {"sources": [{"asset_id": source["id"], "sheets": [mapping]}]}
    imported = successful(client.post(base + "/imports", json=request))
    status = await_state(client, base, lambda data: data["latest_import"]["state"] in ("done", "failed"))
    assert status["latest_import"]["state"] == "done", status
    return base, imported["id"], successful(client.get(base + "/catalog", params={"dataset_id": imported["id"]}))


SMALL = [
    ("同名学校", "11", "甲市", "同名区", "学生", "满意", "图书；网络", 10),
    ("同名学校", "11", "甲市", "同名区", "学生", "未作答", "图书；图书", 20),
    ("乙学校", "12", "甲市", "同名区", "教师", "满意", "图书", 40),
    ("同名学校", "21", "乙市", "同名区", "学生", "不满意", "图书；讲座", 60),
]


def test_scope_indicators_templates_versions_and_downloads(client):
    http, other = client
    base, dataset_id, catalog = prepare(http, SMALL)
    assert catalog["total"] == 4
    organizations = catalog["organizations"]
    assert len(organizations) == 8
    assert len({row["id"] for row in organizations if row["level"] == "county"}) == 2
    schools = sorted((row for row in organizations if row["level"] == "school"), key=lambda row: row["school_code"])
    assert schools[0]["path"] != schools[-1]["path"]
    assert http.get(base, headers=other).status_code == 404
    assert http.get("/api/projects").is_success

    qids = {row["name"]: row["id"] for row in catalog["questionnaires"]}
    fields = successful(http.get(base + "/questions", params={"dataset_id": dataset_id}))
    student = {row["title"]: row["id"] for row in fields if row["questionnaire"] == qids["学生"]}
    metric_rules = [
        {"name": "满意比例", "question_id": student["满意度"], "method": "rate", "options": ["满意"], "denominator": "answered"},
        {"name": "图书勾选率", "question_id": student["资源（多选）"], "method": "multiple_rate", "options": ["图书"], "denominator": "sample"},
        {"name": "数值均值", "question_id": student["数值"], "method": "mean"},
    ]
    indicator = successful(http.post(base + "/indicators", json={"dataset_id": dataset_id, "rules": metric_rules}))
    second_rule_set = successful(http.post(base + "/indicators", json={"dataset_id": dataset_id, "rules": metric_rules[:1]}))
    assert indicator["id"] != second_rule_set["id"]
    context = {"dataset_id": dataset_id, "mode": "province", "indicator_id": indicator["id"]}
    plan = successful(http.post(base + "/preview", json=context))
    assert plan["reports"] == 1
    assert plan["levels"]["province"]["responses"] == 4
    assert all(plan["levels"][level]["reports"] == 0 for level in ("city", "county", "school"))
    province_split = {**context, "levels": {"province": {"split": True}}}
    assert successful(http.post(base + "/preview", json=province_split))["reports"] == 2
    all_plan = successful(http.post(base + "/preview", json={**context, "mode": "all", "excluded": [schools[-1]["id"]]}))
    assert all_plan["reports"] == 7
    submit = {**context, "request_id": str(uuid4())}
    batch_id = successful(http.post(base + "/batches", json=submit))["id"]
    assert successful(http.post(base + "/batches", json=submit))["id"] == batch_id
    batch_url = f"{base}/batches/{batch_id}"
    batch = await_state(http, batch_url, lambda row: row["counts"]["done"] == 1)
    assert batch["total"] == 1
    report_id = batch["reports"][0]["id"]
    response = http.get(f"{batch_url}/reports/{report_id}/download")
    assert response.is_success
    document = Document(io.BytesIO(response.content))
    for style_name in ("Title", "Normal", "Heading 1", "Heading 2", "Heading 3"):
        assert document.styles[style_name].element.rPr.rFonts.get(qn("w:eastAsia"))
    texts = "\n".join(p.text for p in document.paragraphs)
    for expected in ("满意比例  50.0%", "图书勾选率  100.0%", "数值均值  30.00", "教师", "学生"):
        assert expected in texts
    assert "测试姓名" not in texts
    assert "未作答" not in texts
    assert http.get(f"{batch_url}/reports/{report_id}/download", headers=other).status_code == 404

    template_ids = []
    for index in range(2):
        uploaded = successful(http.post(base + "/assets", data={"kind": "template", "level": "school"},
                                        files={"file": (f"版本{index}.docx", docx_template(f"标题{index}", f"正文{index}"))}))
        assert len(uploaded["paragraphs"]) == 4
        version = successful(http.put(f"{base}/templates/{uploaded['id']}", json={"title_index": 1, "body_index": 2}))
        template_ids.append(version["id"])
    ids = [row["id"] for row in schools]
    mixed = {**context, "mode": "custom", "org_ids": [1, schools[0]["parent"], *ids, ids[0]],
             "levels": {"school": {"questionnaires": [qids["学生"], qids["教师"]], "templates": template_ids, "split": True}},
             "overrides": {str(ids[0]): template_ids[1]}}
    preview = successful(http.post(base + "/preview", json=mixed))
    assert preview["reports"] == 5
    assert next(row["template_id"] for row in preview["organizations"] if row["id"] == ids[0]) == template_ids[1]
    default = successful(http.post(base + "/preview", json={**mixed, "overrides": {}}))
    assert {r["id"]: r["template_id"] for r in preview["organizations"] if r["id"] != ids[0]} == {
        r["id"]: r["template_id"] for r in default["organizations"] if r["id"] != ids[0]
    }
    assert next(row["samples"] for row in preview["organizations"] if row["id"] == ids[0])[str(qids["教师"])] == 0
    batch_id = successful(http.post(base + "/batches", json={**mixed, "request_id": str(uuid4())}))["id"]
    batch_url = f"{base}/batches/{batch_id}"
    batch = await_state(http, batch_url, lambda row: row["counts"]["done"] == 5)
    custom = next(row for row in batch["reports"] if row["org_id"] == ids[0])
    response = http.get(f"{batch_url}/reports/{custom['id']}/download")
    paragraphs = [p.text for p in Document(io.BytesIO(response.content)).paragraphs]
    assert paragraphs[0] == "原来的封面"
    assert paragraphs[-1] == "模板页尾"
    assert "正文1" not in paragraphs
    bundle = http.get(batch_url + "/zip", params={"level": "school", "org_id": schools[0]["parent"]})
    assert bundle.is_success
    with ZipFile(io.BytesIO(bundle.content)) as result:
        assert len(result.namelist()) == 2
        assert result.testzip() is None


def test_failed_report_can_retry_and_startup_recovers_claim(client, monkeypatch):
    http, _ = client
    base, dataset_id, _ = prepare(http, SMALL)
    original = survey_worker.build_word

    def fail_once(*args, **kwargs):
        raise ValueError("测试中断")

    monkeypatch.setattr(survey_worker, "build_word", fail_once)
    created = successful(http.post(base + "/batches", json={"dataset_id": dataset_id, "request_id": str(uuid4())}))
    url = base + "/batches/" + str(created["id"])
    batch = await_state(http, url, lambda row: row["counts"]["failed"] == 1)
    report_id = batch["reports"][0]["id"]
    assert batch["reports"][0]["error"] == "测试中断"
    monkeypatch.setattr(survey_worker, "build_word", original)
    assert http.post(f"{url}/reports/{report_id}/retry").is_success
    await_state(http, url, lambda row: row["counts"]["done"] == 1)
    with SessionLocal.begin() as db:
        db.execute(update(SurveyReport).where(SurveyReport.id == report_id).values(state="running"))
    survey_worker.recover_interrupted()
    await_state(http, url, lambda row: row["counts"]["done"] == 1)


@pytest.mark.parametrize("corrected", [True, False])
def test_questionnaire_ai_corrects_output_or_completes_without_it(client, db, monkeypatch, corrected):
    http, _ = client
    user_id = db.scalar(select(User.id).order_by(User.id))
    db.add(ModelConfig(user_id=user_id, name="模拟写作 AI", agent_type="write", model="offline",
                       base_url="https://example.invalid", api_key_enc="", is_default=True))
    db.commit()
    base, dataset_id, _ = prepare(http, SMALL)
    responses = []

    async def fake_model(_config, messages, **_kwargs):
        responses.append(messages)
        if corrected and len(responses) > 1:
            assert "请修正" in messages[-1]["content"]
            return "学生和教师问卷应分别观察。"
        return "98% 的答卷支持该观点。"

    monkeypatch.setattr(survey_worker, "chat_completion", fake_model)
    created = successful(http.post(base + "/batches", json={
        "dataset_id": dataset_id, "request_id": str(uuid4()), "ai_orgs": [1],
    }))
    url = base + "/batches/" + str(created["id"])
    report = await_state(http, url, lambda row: row["counts"]["done"] == 1)["reports"][0]
    response = http.get(f"{url}/reports/{report['id']}/download")
    assert response.is_success
    paragraphs = "\n".join(p.text for p in Document(io.BytesIO(response.content)).paragraphs)
    assert "答卷概况" in paragraphs and "98%" not in paragraphs
    assert ("学生和教师问卷应分别观察" in paragraphs) == corrected
    assert bool(report["notice"]) != corrected
    assert len(responses) == (2 if corrected else 3)


@pytest.mark.skipif(os.getenv("RUN_SURVEY_LOAD") != "1", reason="千份 Word 压测需显式开启")
def test_generate_thousand_documents_and_fixed_zip_parts(client):
    http, _ = client
    rows = [(f"学校{i:04}", str(i), "测试市", "测试区", "学生", "满意", "网络", 22) for i in range(1010)]
    base, dataset_id, _ = prepare(http, rows)
    created = successful(http.post(base + "/batches", json={"dataset_id": dataset_id, "mode": "all",
                                                          "request_id": str(uuid4())}))
    url = base + "/batches/" + str(created["id"])
    status = await_state(http, url, lambda row: not (row["counts"]["queued"] + row["counts"]["running"]), timeout=150)
    assert status["counts"] == {"queued": 0, "running": 0, "done": 1013, "failed": 0}
    names = set()
    for part in range(1, 12):
        response = http.get(url + "/zip", params={"level": "school", "part": part})
        assert response.is_success
        with ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.testzip() is None
            assert len(archive.namelist()) == (100 if part < 11 else 10)
            assert names.isdisjoint(archive.namelist())
            names.update(archive.namelist())
    assert len(names) == 1010
