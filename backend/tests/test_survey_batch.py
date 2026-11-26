import asyncio
import csv
import io
import json
import os
import time
from threading import Event
from uuid import uuid4
from zipfile import ZipFile

import pytest
import xlwt
from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml.ns import qn
from docx.shared import RGBColor
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import select, update

from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import ModelConfig, User
from app.security import create_token
from app.survey_models import SurveyAsset, SurveyBatch, SurveyBatchPreparation, SurveyDataset, SurveyProject, SurveyReport, SurveyReportDraft
from app import survey_ai, survey_authoring, survey_worker
from app.survey_reports import chart_image
from app.survey_templates import extract_report_layout


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


def test_complete_word_report_parsing_versions_and_download(client, db, monkeypatch):
    http, other = client
    base, dataset_id, _ = prepare(http, SMALL)
    owner = db.scalar(select(User).where(User.username == "batch-owner"))
    db.add(ModelConfig(user_id=owner.id, name="测试解析模型", agent_type="parse", base_url="http://localhost",
                       api_key_enc="", model="test", is_default=True))
    db.commit()
    reference = Document()
    reference.add_paragraph("六年级学生满意度题及选项说明")
    ref = io.BytesIO()
    reference.save(ref)
    successful(http.post(base + "/assets", data={"kind": "questionnaire"},
                         files={"file": ("学生.docx", ref.getvalue())}))
    book = Workbook()
    book.active.append(["指标", "填报口径"])
    book.active.append(["满意度", "按有效作答计算比例"])
    indicators = io.BytesIO()
    book.save(indicators)
    successful(http.post(base + "/assets", data={"kind": "indicator"},
                         files={"file": ("年度指标.xlsx", indicators.getvalue())}))
    successful(http.post(base + "/assets", data={"kind": "school"},
                         files={"file": ("学校.xlsx", roster_xlsx([("11", "同名学校", "小学")]))}))
    calls = []

    async def model(config, messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        assert config.agent_type == "parse"
        if "章节位置" not in payload:
            return json.dumps({"summary": "核对题目后按有效作答统计"})
        assert set(payload["资料摘要"]) == {"学生.docx", "年度指标.xlsx"}
        assert payload["学校学段汇总"]["primary"] == 1
        assert payload["答卷文件"] == ["答卷.xlsx"]
        assert any(q["题干"] == "满意度" for q in payload["答卷题库"])
        return json.dumps({"sections": [{
            "id": section["id"], "role": "scope" if section["章节"][-1] == "答卷范围" else "findings",
            "questionnaire_ids": None,
            "modules": [{
                "id": slot["id"], "kind": "table" if "按有效答卷汇总" in slot["原文"] else slot["原类型"],
                "instruction": "按有效答卷汇总" if "按有效答卷汇总" in slot["原文"] else "按年度资料核对本章指标和分母",
            } for slot in section["modules"]],
        } for section in payload["章节位置"]]})

    monkeypatch.setattr(survey_ai, "chat_completion", model)
    example = Document()
    example.sections[0].header.paragraphs[0].text = "监测页眉"
    example.add_paragraph("原报告封面", "Title")
    example.add_paragraph("原属地", "Subtitle")
    example.add_heading("答卷范围", 1)
    example.add_paragraph("示例答卷有 999999 份")
    example.add_table(1, 1).cell(0, 0).text = "示例统计表"
    example.add_section(WD_SECTION.NEW_PAGE)
    example.add_heading("监测发现", 1)
    example.add_heading("统计观察", 2)
    example.add_paragraph("旧报告正文")
    example.add_picture(chart_image([("旧选项", 999)], 1000))
    example.add_paragraph().add_run("【生成表格】按有效答卷汇总").font.color.rgb = RGBColor.from_string("FF0000")
    buffer = io.BytesIO()
    example.save(buffer)
    raw = buffer.getvalue()
    form = {"kind": "template", "level": "province", "parse_report": "true"}
    row = successful(http.post(base + "/assets", data=form, files={"file": ("完整报告.docx", raw)}))
    template_id = row["id"]
    assert row["parse_state"] == "queued"

    def parsed(asset_id, state="done"):
        project = await_state(http, base, lambda data: any(asset["id"] == asset_id and
                              asset.get("parse_state") in {"done", "failed"} for asset in data["assets"]))
        asset = next(asset for asset in project["assets"] if asset["id"] == asset_id)
        assert asset["parse_state"] == state, asset
        return asset

    result = parsed(template_id)
    assert result["ready"]
    assert len(calls) == 3
    assert [section["path"] for section in result["sections"]] == [["答卷范围"], ["监测发现"], ["监测发现", "统计观察"]]
    assert [m["kind"] for m in result["sections"][-1]["modules"]] == ["text", "image", "table"]
    assert result["sections"][-1]["modules"][-1]["instruction"] == "【生成表格】按有效答卷汇总"
    file_url = f"{base}/templates/{template_id}/file"
    assert http.get(file_url).content == raw
    assert http.get(file_url, headers=other).status_code == 404
    red = http.get(file_url, params={"parsed": True})
    assert red.is_success
    red_doc = Document(io.BytesIO(red.content))
    assert len(red_doc.sections) == 2
    assert not red_doc.tables and not red_doc.inline_shapes
    assert all("999999" not in p.text for p in red_doc.paragraphs)
    assert any(p.text == "【生成表格】按有效答卷汇总" for p in red_doc.paragraphs)
    assert any(p.text == "【撰写正文】按年度资料核对本章指标和分母" for p in red_doc.paragraphs)

    invalid = successful(http.post(base + "/assets", data=form,
                         files={"file": ("无章节.docx", docx_template("没有章节"))}))
    failure = parsed(invalid["id"], "failed")
    assert "未识别到章节" in failure["parse_error"] and not failure["ready"]
    retry = successful(http.post(f"{base}/templates/{invalid['id']}/parse"))
    assert retry["id"] == invalid["id"]
    parsed(invalid["id"], "failed")

    payload = {"dataset_id": dataset_id, "levels": {"province": {"templates": [template_id]}}}
    preview = successful(http.post(base + "/preview", json=payload))
    assert preview["organizations"][0]["template_id"] == template_id
    batch = successful(http.post(base + "/generate", json={**payload, "request_id": str(uuid4())}))
    batch_url = f"{base}/batches/{batch['id']}"
    version = successful(http.post(f"{base}/templates/{template_id}/parse"))
    assert version["id"] != template_id and version["previous_version"] == template_id
    parsed(version["id"])
    finished = await_state(http, batch_url, lambda data: data["counts"]["done"] or data["counts"]["failed"])
    assert finished["counts"]["done"] == 1, finished
    report = finished["reports"][0]
    db.expire_all()
    assert db.get(SurveyReport, report["id"]).template_id == template_id
    output_url = f"{batch_url}/reports/{report['id']}/download"
    output = http.get(output_url)
    assert output.is_success
    doc = Document(io.BytesIO(output.content))
    assert len(doc.sections) == 2
    assert doc.sections[1].header.paragraphs[0].text == "监测页眉"
    assert [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")] == ["答卷范围", "监测发现", "统计观察"]
    assert len(doc.tables) == 2 and len(doc.inline_shapes) == 1
    assert "问卷类型" in doc.tables[0].cell(0, 0).text
    assert all("999999" not in p.text and p.text not in {"原属地", "旧报告正文", "原报告封面"} for p in doc.paragraphs)
    assert http.delete(f"{base}/assets/{template_id}").is_success
    assert http.get(output_url).content == output.content

    long_report = Document()
    for index in range(158):
        long_report.add_heading(f"监测指标 {index + 1}", 1)
        long_report.add_paragraph("监测数据示例")
    long_buffer = io.BytesIO()
    long_report.save(long_buffer)
    before = len(calls)
    long_asset = successful(http.post(base + "/assets", data=form,
                            files={"file": ("长报告.docx", long_buffer.getvalue())}))
    assert len(parsed(long_asset["id"])["sections"]) == 158
    assert len(calls) - before == 18

    successful(http.post(base + "/assets", data={"kind": "data"},
                         files={"file": ("待导入.xlsx", workbook(SMALL[:1]))}))
    pending = http.post(base + "/assets", data=form, files={"file": ("待检查.docx", raw)})
    assert pending.status_code == 409 and "未导入" in pending.json()["detail"]


def test_inferred_interest_table_recalculates_only_selected_student_questionnaires(tmp_path):
    source = Document()
    source.add_heading("学生学习态度", 1)
    table = source.add_table(2, 3)
    for cell, text in zip(table.rows[0].cells, ["学段与学科", "完全或基本符合", "不确定"]):
        cell.text = text
    table.cell(1, 0).text = "六年级数学"
    table.cell(1, 1).text = "999 / 999"
    source.add_picture(chart_image([("六年级数学", 999)], 999))
    template = tmp_path / "sample.docx"
    source.save(template)
    sections = extract_report_layout(template.read_bytes())
    sections[0]["role"] = "metrics"
    assert sections[0]["measure"] == "course_interest"
    surveys = [{"id": 2, "name": "学生问卷-九年级数学", "sample": 5, "indicators": [],
                "questions": [{"id": 1, "title": "你喜欢上数学课。", "kind": "single",
                               "n": 4, "options": {"完全符合": 1, "基本符合": 2, "不确定": 1}}]}]
    result = tmp_path / "new.docx"
    survey_authoring.render_structured_word(
        result, title="测试报告", org="初中", template=(template, {}), sections=sections, surveys=surveys,
        draft=[{"key": "metrics", "text": "课程喜爱题分学科列示。"}], facts={}, annual=[], missing=[])
    reproduced = Document(result)
    assert [cell.text for cell in reproduced.tables[0].rows[1].cells] == [
        "九年级数学", "3 / 4（75.0%）", "1 / 4（25.0%）",
    ]
    assert len(reproduced.inline_shapes) == 1
    assert not any("六年级" in paragraph.text for paragraph in reproduced.paragraphs)


def roster_xlsx(rows):
    book = Workbook()
    book.active.append(["id", "area", "city", "school", "type", "loginId", "loginPassword"])
    for code, name, stage in rows:
        book.active.append([code, "同名区", "甲市", name, stage, "测试账号", "测试口令"])
    result = io.BytesIO()
    book.save(result)
    return result.getvalue()


def test_school_roster_filters_school_reports_and_snapshots_stages(client):
    http, other = client
    primary = "学生问卷-六年级科学"
    middle = "学生问卷-九年级物理"
    teachers = "教师问卷"
    rows = [
        ("甲小学", "001", "甲市", "同名区", primary, "满意", "图书", 10),
        ("甲小学", "001", "甲市", "同名区", middle, "满意", "图书", 11),
        ("乙中学", "1", "甲市", "同名区", middle, "满意", "图书", 20),
        ("乙中学", "1", "甲市", "同名区", teachers, "满意", "图书", 21),
        ("丙学校", "003", "甲市", "同名区", primary, "满意", "图书", 30),
    ]
    base, dataset_id, before = prepare(http, rows)
    params = {"dataset_id": dataset_id}
    roster_rows = [("001", "甲小学", "小学"), ("1", "乙中学", "初中")]

    def roster_upload(items):
        return http.post(base + "/assets", data={"kind": "school"},
                         files={"file": ("学校账号.xlsx", roster_xlsx(items))})

    template_response = http.get(base + "/school-template")
    assert template_response.is_success
    template = load_workbook(io.BytesIO(template_response.content))
    assert next(template.active.values) == ("id", "area", "city", "school", "type")
    assert template.active.column_dimensions["A"].number_format == "@"
    assert http.get(base + "/school-template", headers=other).status_code == 404
    assert all(row.get("school_stage") == "unknown" for row in before["organizations"] if row["level"] == "school")

    roster = successful(roster_upload(roster_rows))
    assert roster["counts"] == {"primary": 1, "middle": 1}
    assert roster_upload([*roster_rows, ("001", "甲小学", "初中")]).status_code == 422
    assert roster_upload([("", "甲小学", "小学")]).status_code == 422
    with SessionLocal() as db:
        asset = db.get(SurveyAsset, roster["id"])
        assert not asset.path
        assert "测试口令" not in str(asset.details) and "测试账号" not in str(asset.details)
        assert set(asset.details["schools"]) == {"001", "1"}
    catalog = successful(http.get(base + "/catalog", params=params))
    assert catalog["school_roster_id"] == roster["id"]
    schools = {row["school_code"]: row for row in catalog["organizations"] if row["level"] == "school"}
    assert {key: row["school_stage"] for key, row in schools.items()} == {"001": "primary", "1": "middle", "003": "unknown"}
    qids = {row["name"]: row["id"] for row in catalog["questionnaires"]}
    assert {row["name"]: row["school_stage"] for row in catalog["questionnaires"]} == {
        primary: "primary", middle: "middle", teachers: "any"}
    selection = {**params, "mode": "custom", "school_roster_id": roster["id"]}
    assert http.post(base + "/preview", json={**selection, "org_ids": [schools["001"]["id"]],
                     "questionnaire_stages": {qids[middle]: "any"}}).status_code == 422
    only_primary = successful(http.post(base + "/preview", json={**selection, "org_ids": [schools["001"]["id"]]}))
    row = only_primary["organizations"][0]
    assert row["incompatible_questionnaires"] == {str(qids[middle]): 1}
    assert row["skipped_questionnaires"] == [qids[teachers]]
    override = successful(http.post(base + "/preview", json={**selection, "org_ids": [schools["1"]["id"]],
                          "questionnaire_stages": {qids[teachers]: "primary"}}))
    assert override["organizations"][0]["incompatible_questionnaires"][str(qids[teachers])] == 1
    province = successful(http.post(base + "/preview", json={**selection, "mode": "province"}))
    assert province["levels"]["province"]["responses"] == 5

    request = {**selection, "org_ids": [row["id"] for row in schools.values()],
               "automatic": True, "request_id": str(uuid4())}
    preview = successful(http.post(base + "/preview", json=request))
    assert preview["reports"] == 3 and preview["levels"]["school"]["responses"] == 4
    batch_id = successful(http.post(base + "/generate", json=request))["id"]
    replacement = successful(roster_upload([("001", "甲小学", "初中"), roster_rows[1]]))
    assert replacement["id"] != roster["id"]
    assert http.post(base + "/preview", json=request).status_code == 409
    url = f"{base}/batches/{batch_id}"
    batch = await_state(http, url, lambda result: result["counts"]["done"] == 3 or result["counts"]["failed"])
    assert batch["counts"]["done"] == preview["reports"]
    reports = {row["school_stage"]: row for row in batch["reports"]}
    assert reports["primary"]["questionnaires"] == [primary]
    assert set(reports["middle"]["questionnaires"]) == {middle, teachers}
    assert reports["unknown"]["questionnaires"] == [primary]
    report = http.get(f"{url}/reports/{reports['primary']['id']}/download")
    assert report.is_success
    assert any("甲小学（小学）" in p.text for p in Document(io.BytesIO(report.content)).paragraphs)
    bundle = http.get(url + "/zip", params={"org_id": schools["001"]["id"]})
    assert bundle.is_success
    with ZipFile(io.BytesIO(bundle.content)) as archive:
        assert len(archive.namelist()) == 1 and "甲小学（小学）" in archive.namelist()[0]
    assert http.delete(f"{base}/assets/{replacement['id']}").is_success
    assert successful(http.get(base + "/catalog", params=params))["school_roster_id"] is None


def test_delete_survey_uploads_imports_and_project_preserves_history(client, tmp_path):
    http, other = client
    base, dataset_id, _ = prepare(http, SMALL)
    project_id = int(base.rsplit("/", 1)[-1])
    detail = successful(http.get(base))
    source = detail["assets"][0]
    sources = detail["latest_import"]["sources"]

    def upload(kind, name, data, **fields):
        return successful(http.post(base + "/assets", data={"kind": kind, **fields}, files={"file": (name, data)}))

    template = upload("template", "封面.docx", docx_template("{{报告标题}}", "{{报告正文}}"), level="province")
    template = successful(http.put(f"{base}/templates/{template['id']}", json={"title_index": 1, "body_index": 2}))
    original = upload("questionnaire", "学生.docx", docx_template("学生问卷原稿"))
    indicator = upload("indicator", "指标.docx", docx_template("测试指标"))
    extra = upload("data", "其他答卷.xlsx", workbook(SMALL[:1]))
    request = {"dataset_id": dataset_id, "levels": {"province": {"templates": [template["id"]]}},
               "indicator_assets": [indicator["id"]], "request_id": str(uuid4())}
    batch_id = successful(http.post(base + "/generate", json=request))["id"]
    batch_url = f"{base}/batches/{batch_id}"
    result = await_state(http, batch_url, lambda data: data["counts"]["done"] or data["counts"]["failed"])
    assert result["counts"]["done"] == 1
    download_url = f"{batch_url}/reports/{result['reports'][0]['id']}/download"
    with SessionLocal() as db:
        paths = {row.id: tmp_path / str(project_id) / row.path for row in db.scalars(select(SurveyAsset))}
    assert paths[extra["id"]].is_file()
    assert http.delete(f"{base}/assets/{extra['id']}", headers=other).status_code == 404
    assert http.delete(f"{base}/imports/{dataset_id}", headers=other).status_code == 404
    assert http.delete(base, headers=other).status_code == 404
    assert not successful(http.delete(f"{base}/assets/{extra['id']}"))["cleared_current"]
    assert not paths[extra["id"]].exists()

    assert successful(http.delete(f"{base}/imports/{dataset_id}"))["cleared_current"]
    assert successful(http.get(base))["dataset_id"] is None
    assert successful(http.get(base + "/imports")) == []
    assert http.get(download_url).is_success
    assert http.post(base + "/preview", json=request).status_code == 409
    assert paths[source["id"]].is_file()
    reimport = successful(http.post(base + "/imports", json={"sources": sources}))
    await_state(http, base, lambda data: data["dataset_id"] == reimport["id"])
    for asset in (template, original, indicator):
        assert http.delete(f"{base}/assets/{asset['id']}").is_success
    assert successful(http.delete(f"{base}/assets/{source['id']}"))["cleared_current"]
    assert successful(http.get(base))["assets"] == []
    assert successful(http.get(base))["dataset_id"] is None
    assert http.post(base + "/imports", json={"sources": sources}).status_code == 404
    assert paths[source["id"]].is_file() and paths[template["id"]].is_file()
    assert http.get(download_url).is_success
    assert http.delete(base).is_success
    assert http.get(base).status_code == 404 and http.get(download_url).status_code == 404
    assert not (tmp_path / str(project_id)).exists()
    with SessionLocal() as db:
        assert db.get(SurveyProject, project_id) is None
        assert list(db.scalars(select(SurveyDataset).where(SurveyDataset.project_id == project_id))) == []


def test_deletion_conflicts_with_running_import_and_report(client, monkeypatch):
    http, _ = client
    base, dataset_id, _ = prepare(http, SMALL)
    sources = successful(http.get(base))["latest_import"]["sources"]
    started, release = Event(), Event()
    build_index = survey_worker.build_index

    def paused_build(*args):
        started.set()
        assert release.wait(timeout=10)
        return build_index(*args)

    monkeypatch.setattr(survey_worker, "build_index", paused_build)
    version = successful(http.post(base + "/imports", json={"sources": sources}))
    try:
        assert started.wait(timeout=5)
        for url in (base, f"{base}/assets/{sources[0]['asset_id']}", f"{base}/imports/{version['id']}"):
            response = http.delete(url)
            assert response.status_code == 409 and "答卷导入版本" in response.json()["detail"]
        assert successful(http.get(base))["dataset_id"] == dataset_id
    finally:
        release.set()
    await_state(http, base, lambda row: row["dataset_id"] == version["id"])

    entered, resume = Event(), Event()
    process_report = survey_worker.process_report

    async def paused_report(report_id):
        entered.set()
        assert await asyncio.to_thread(resume.wait, 10)
        await process_report(report_id)

    monkeypatch.setattr(survey_worker, "process_report", paused_report)
    batch_id = successful(http.post(base + "/batches", json={"dataset_id": version["id"], "request_id": str(uuid4())}))["id"]
    try:
        await_state(http, f"{base}/batches/{batch_id}", lambda data: data["counts"]["running"])
        assert entered.wait(timeout=5)
        response = http.delete(base)
        assert response.status_code == 409 and "出报批次" in response.json()["detail"]
    finally:
        resume.set()
    await_state(http, f"{base}/batches/{batch_id}", lambda data: data["counts"]["done"])
    assert http.delete(base).is_success


def test_stop_preparation_cancels_model_wait_and_allows_batch_deletion(client, monkeypatch):
    http, other = client
    base, dataset_id, _ = prepare(http, SMALL)
    entered, cancelled = Event(), Event()

    async def blocked_outline(*args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(survey_worker, "make_outline", blocked_outline)
    request = {"dataset_id": dataset_id, "request_id": str(uuid4()), "mode": "all"}
    batch_id = successful(http.post(base + "/generate", json=request))["id"]
    url = f"{base}/batches/{batch_id}"
    assert entered.wait(5)
    assert http.delete(url).status_code == 409
    assert http.post(url + "/stop", headers=other).status_code == 404
    assert http.delete(url, headers=other).status_code == 404
    assert http.post(url + "/stop").is_success
    assert cancelled.wait(5)
    batch = await_state(http, url, lambda row: row["generation"]["state"] == "stopped")
    assert batch["counts"]["stopped"] == batch["total"]
    assert http.post(url + "/retry").status_code == 409
    assert http.post(url + "/stop").is_success
    assert http.delete(url).is_success
    assert http.get(url).status_code == 404
    assert successful(http.get(base + "/batches")) == []
    assert successful(http.get(base))["dataset_id"] == dataset_id


def test_stop_during_word_render_waits_for_file_cleanup(client, monkeypatch, tmp_path):
    http, _ = client
    base, dataset_id, _ = prepare(http, SMALL)
    request = {"dataset_id": dataset_id, "request_id": str(uuid4())}
    completed_id = successful(http.post(base + "/batches", json=request))["id"]
    completed_url = f"{base}/batches/{completed_id}"
    completed = await_state(http, completed_url, lambda row: row["counts"]["done"] == 1)
    completed_download = f"{completed_url}/reports/{completed['reports'][0]['id']}/download"

    entered, release = Event(), Event()
    original = survey_worker.build_word

    def blocked_word(*args, **kwargs):
        original(*args, **kwargs)
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr(survey_worker, "build_word", blocked_word)
    request["request_id"] = str(uuid4())
    batch_id = successful(http.post(base + "/batches", json=request))["id"]
    url = f"{base}/batches/{batch_id}"
    try:
        assert entered.wait(5)
        assert http.post(url + "/stop").is_success
        assert http.delete(url).status_code == 409
        assert successful(http.get(url))["counts"]["stopping"] == 1
    finally:
        release.set()
    stopped = await_state(http, url, lambda row: row["counts"]["stopped"] == 1)
    report_id = stopped["reports"][0]["id"]
    assert http.get(f"{url}/reports/{report_id}/download").status_code == 404
    assert http.delete(url).is_success
    assert not list(tmp_path.glob(f"*/report-{report_id}.*"))
    assert http.get(completed_download).is_success

    status_url = f"{base}/report-status?dataset_id={dataset_id}"
    assert successful(http.get(status_url))["current_org_ids"]
    assert http.delete(completed_url).is_success
    assert successful(http.get(status_url))["current_org_ids"] == []
    assert http.get(completed_download).status_code == 404
    assert not list(tmp_path.glob("*/report-*.docx"))


def test_report_status_matches_across_imports_and_allows_regeneration(client, monkeypatch):
    http, other = client
    base, first_version, first_catalog = prepare(http, [*SMALL, (*SMALL[0][:4], *SMALL[2][4:])])
    status_url = base + "/report-status"
    old_params = {"dataset_id": first_version}
    assert successful(http.get(status_url, params=old_params)) == {
        "dataset_id": first_version, "current_org_ids": [], "historical_org_ids": [],
    }
    assert http.get(status_url, params=old_params, headers=other).status_code == 404
    old_orgs = first_catalog["organizations"]
    generated = {row["id"] for row in old_orgs if row["level"] == "province" or
                 row["path"].endswith((" / 甲市", " / 甲市 / 同名区")) or row["school_code"] == "11"}
    school = next(row for row in old_orgs if row["school_code"] == "11")
    failed_school = next(row for row in old_orgs if row["school_code"] == "21")
    teacher = next(row["id"] for row in first_catalog["questionnaires"] if row["name"] == "教师")
    process_report = survey_worker.process_report

    async def fail_unfinished_reports(report_id):
        with SessionLocal() as db:
            job = db.get(SurveyReport, report_id)
            if job.org_id == failed_school["id"] or job.org_id == school["id"] and job.questionnaires == [teacher]:
                raise ValueError("测试出报失败")
        await process_report(report_id)

    monkeypatch.setattr(survey_worker, "process_report", fail_unfinished_reports)
    first_batch = successful(http.post(base + "/batches", json={
        **old_params, "mode": "custom", "org_ids": sorted(generated | {failed_school["id"]}),
        "levels": {"school": {"split": True}}, "request_id": str(uuid4()),
    }))["id"]
    first_url = f"{base}/batches/{first_batch}"
    result = await_state(http, first_url, lambda row: row["counts"]["done"] + row["counts"]["failed"] == row["total"])
    assert result["counts"]["done"] == 4 and result["counts"]["failed"] == 2
    first_report = next(row for row in result["reports"] if row["org_id"] == school["id"] and row["state"] == "done")
    download = f"{first_url}/reports/{first_report['id']}/download"
    original_file = http.get(download)
    assert original_file.is_success
    assert successful(http.get(status_url, params=old_params))["current_org_ids"] == sorted(generated)

    renamed = ("新校名", *SMALL[0][1:])
    source = successful(http.post(base + "/assets", data={"kind": "data"},
                                  files={"file": ("新版答卷.xlsx", workbook([SMALL[-1], renamed, SMALL[2]]))}))
    version = successful(http.post(base + "/imports", json={
        "sources": [{"asset_id": source["id"], "sheets": source["sheets"]}],
    }))["id"]
    await_state(http, base, lambda row: row["dataset_id"] == version)
    assert http.get(status_url, params=old_params).status_code == 409
    assert successful(http.delete(f"{base}/imports/{first_version}"))["ok"]
    params = {"dataset_id": version}
    current_catalog = successful(http.get(base + "/catalog", params=params))
    schools = {row["school_code"]: row for row in current_catalog["organizations"] if row["level"] == "school"}
    assert schools["11"]["name"] == "新校名" and schools["11"]["id"] != school["id"]
    historical = {row["id"] for row in current_catalog["organizations"] if row["level"] == "province" or
                  row["path"].endswith((" / 甲市", " / 甲市 / 同名区")) or row["school_code"] == "11"}
    assert successful(http.get(status_url, params=params)) == {
        "dataset_id": version, "current_org_ids": [], "historical_org_ids": sorted(historical),
    }

    template = successful(http.post(base + "/assets", data={"kind": "template", "level": "school"},
                                    files={"file": ("本次模板.docx", docx_template("新的标题"))}))
    template_id = successful(http.put(f"{base}/templates/{template['id']}",
                                      json={"title_index": 1, "body_index": 2}))["id"]
    qid = next(row["id"] for row in current_catalog["questionnaires"] if row["name"] == "学生")
    questions = successful(http.get(base + "/questions", params=params))
    question = next(row for row in questions if row["title"] == "数值" and row["questionnaire"] == qid)
    indicator_id = successful(http.post(base + "/indicators", json={
        **params, "rules": [{"name": "本次指标", "question_id": question["id"], "method": "mean"}],
    }))["id"]
    settings = {**params, "mode": "custom", "org_ids": [schools["11"]["id"], schools["21"]["id"]],
                "levels": {"school": {"questionnaires": [qid], "templates": [template_id]}},
                "indicator_id": indicator_id}
    assert successful(http.post(base + "/preview", json={**settings, "automatic": True}))["reports"] == 2
    monkeypatch.setattr(survey_worker, "process_report", process_report)
    request = {**settings, "request_id": str(uuid4())}
    batch_id = successful(http.post(base + "/generate", json=request))["id"]
    assert batch_id != first_batch
    assert successful(http.post(base + "/generate", json=request))["id"] == batch_id
    batch_url = f"{base}/batches/{batch_id}"
    batch = await_state(http, batch_url, lambda row: row["counts"]["done"] == 2 or row["counts"]["failed"])
    assert batch["counts"]["done"] == 2
    assert all(row["questionnaires"] == ["学生"] for row in batch["reports"])
    with SessionLocal() as db:
        assert db.get(SurveyBatch, batch_id).indicator_id == indicator_id
        assert all(db.get(SurveyReport, row["id"]).template_id == template_id for row in batch["reports"])
    assert all(http.get(f"{batch_url}/reports/{row['id']}/download").is_success for row in batch["reports"])
    status = successful(http.get(status_url, params=params))
    assert status["current_org_ids"] == sorted(settings["org_ids"])
    assert status["historical_org_ids"] == sorted(historical)
    assert http.get(download).content == original_file.content


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
    assert status["counts"] == {"queued": 0, "running": 0, "done": 1013, "failed": 0, "stopping": 0, "stopped": 0}
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


def annual_material(filename="2026年度指标.docx"):
    doc = Document()
    doc.add_paragraph("学生勾选图书者中，满意人数除以满意度有效作答人数。")
    doc.add_paragraph("学生答卷综合分。满意计五分，不满意计一分。每份答卷的数值题结果加满意度分值的两倍，两题都有效才计算，再对综合分取平均。")
    doc.add_paragraph("教师培训学时均值，按答卷实际有效学时统计。")
    output = io.BytesIO()
    doc.save(output)
    return filename, output.getvalue()


def configure_survey_model(db):
    user_id = db.scalar(select(User.id).order_by(User.id))
    db.add(ModelConfig(user_id=user_id, name="模拟批量 AI", agent_type="", model="offline",
                       base_url="https://example.invalid", api_key_enc="", is_default=True))
    db.commit()


def head_titles(doc):
    return [p.text for p in doc.paragraphs if p.style.name == "Heading 1"]


def original_docx(*lines):
    doc = Document()
    for line in lines:
        doc.add_paragraph(line)
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def original_pdf():
    pdf = PdfWriter()
    page = pdf.add_blank_page(width=300, height=100)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })}),
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 65 Td (Student confidence question) Tj ET")
    page[NameObject("/Contents")] = pdf._add_object(stream)
    buffer = io.BytesIO()
    pdf.write(buffer)
    return buffer.getvalue()


def test_originals_and_all_answer_formats(client, db, monkeypatch):
    http, other = client
    configure_survey_model(db)
    project = successful(http.post("/api/surveys", json={"name": "上传验收", "province": "测试省", "year": 2026}))
    base = f"/api/surveys/{project['id']}"

    def upload(kind, name, raw):
        return successful(http.post(base + "/assets", data={"kind": kind}, files={"file": (name, raw)}))

    teacher = upload("questionnaire", "教师问卷原稿.docx",
                     original_docx("培训参与情况", "满意度题目的原稿", "满意", "教师量表说明"))
    student = upload("questionnaire", "学生.pdf", original_pdf())
    pending = upload("questionnaire", "补充问卷.docx", original_docx("额外题目"))
    assert student["has_text"]
    assert http.post(base + "/assets", data={"kind": "data"}, files={"file": ("损坏.xlsx", b"broken")}).status_code == 422
    xlsx = upload("data", "原始.xlsx", workbook([
        ("乙学校", "12", "甲市", "同名区", "教师", "满意", "图书", 40),
    ]))
    headers = ["账号", "姓名", "学校", "学校ID", "市/州", "区县", "问卷", "满意度", "资源（多选）", "数值"]
    csv_text = io.StringIO()
    csv.writer(csv_text).writerows([headers, ["2", "测试姓名", "乙学校", "12", "甲市", "同名区", "教师", "不满意", "图书", 50]])
    csv_asset = upload("data", "另一批.csv", csv_text.getvalue().encode("gb18030"))
    xls_book = xlwt.Workbook()
    xls_sheet = xls_book.add_sheet("答卷")
    for row_index, row in enumerate([headers, ["3", "测试姓名", "乙学校", "12", "甲市", "同名区", "学生", "满意", "图书", 30]]):
        for column, value in enumerate(row):
            xls_sheet.write(row_index, column, value)
    xls_data = io.BytesIO()
    xls_book.save(xls_data)
    xls_asset = upload("data", "学生答卷.xls", xls_data.getvalue())
    source_specs = {"sources": [{"asset_id": asset["id"], "sheets": asset["sheets"]} for asset in [xlsx, csv_asset, xls_asset]]}
    import_version = successful(http.post(base + "/imports", json=source_specs))
    state = await_state(http, base, lambda row: row["latest_import"]["state"] in ("done", "failed"))
    assert state["latest_import"]["state"] == "done", state
    dataset_id = import_version["id"]
    catalog = successful(http.get(base + "/catalog", params={"dataset_id": dataset_id}))
    assert catalog["total"] == 3 and len(state["imported_sources"]) == 3
    qids = {row["name"]: row["id"] for row in catalog["questionnaires"]}
    ids = {row["id"]: row.get("linked_questionnaire_id") for row in state["assets"] if row["kind"] == "questionnaire"}
    assert ids == {teacher["id"]: qids["教师"], student["id"]: qids["学生"], pending["id"]: None}
    headers_response = successful(http.get(f"{base}/assets/{xlsx['id']}/headers",
                                            params={"sheet": xlsx["sheets"][0]["sheet"], "header": 2}))
    assert headers_response["headers"] == headers
    link_url = f"{base}/questionnaire-originals/{teacher['id']}"
    assert http.put(link_url, json={"dataset_id": dataset_id, "questionnaire_id": qids["教师"]}, headers=other).status_code == 404
    assert successful(http.put(link_url, json={"dataset_id": dataset_id, "questionnaire_id": qids["教师"]}))["questionnaire_name"] == "教师"

    source_calls, report_calls, corrections = [], [], []
    teacher_note = "满意度题目的原稿"
    student_note = "Student confidence question"
    material = upload("indicator", "年度.docx", original_docx("统计教师答卷图书选择者中的满意率"))

    async def model(_config, messages, **_kwargs):
        payload = json.loads(messages[1]["content"])
        if "原稿位置原文" in payload:
            questions = payload["题库"]
            kind = questions[0]["questionnaire"]
            source_calls.append(kind)
            title = teacher_note if kind == "教师" else student_note
            location = next(key for key, text in payload["原稿位置原文"].items() if title in text)
            item = {"question_id": next(row["id"] for row in questions if row["title"] == "满意度"),
                    "location": location, "title": title}
            if kind == "教师":
                item.update(options=[{"text": "满意", "location": "段落 3"}],
                            scale="教师量表说明", scale_location="段落 4")
            return json.dumps({"items": [item]}, ensure_ascii=False)
        if "指标资料" in payload:
            assert {item["title"] for item in payload["问卷原稿对照"]} <= {teacher_note, student_note}
            ids = {q["title"]: q["id"] for q in payload["题库"] if q["questionnaire"] == "教师"}
            rule = {"questionnaire_id": qids["教师"], "method": "rate", "denominator": "answered",
                    "population": [{"question_id": ids["资源（多选）"], "op": "selected", "value": "图书"}],
                    "matches": [{"question_id": ids["满意度"], "op": "selected", "value": "满意"}]}
            return json.dumps({"items": [{"name": "教师满意率", "location": "段落 1", "definition": "选择图书的教师中，满意度有效作答为分母",
                                          "rule": rule, "questionnaire_id": qids["教师"]}]}, ensure_ascii=False)
        if "章节要求" in payload:
            assert "年度指标" not in payload
            return json.dumps({"sections": [{"key": key, "title": title, "instruction": "按核实结果撰写"}
                                            for key, title, _ in payload["章节要求"]]}, ensure_ascii=False)
        notes = {item["title"] for item in payload.get("问卷原稿对照", [])}
        report_calls.append(notes)
        if "正文" in payload:
            return '{"approved":true,"issues":[]}'
        specific = ("建议结合师资情况安排校本教研。" if teacher_note in notes
                    else "建议结合学生反馈安排学习支持。")
        if notes == {student_note} and payload["任务"] == "红字解析":
            if len(messages) == 2:
                specific = "本节同时说明教师满意率和学生反馈。"
            else:
                corrections.append(messages[-1]["content"])
        return json.dumps({"sections": [
            {"key": key, "text": specific, "evidence": [choices[0]], "actor": payload["责任主体"][0]}
            for key, choices in payload["允许引用"].items()]}, ensure_ascii=False)

    monkeypatch.setattr(survey_ai, "chat_completion", model)
    school = next(row["id"] for row in catalog["organizations"] if row["level"] == "school")
    payload = {"dataset_id": dataset_id, "mode": "custom", "org_ids": [1, school],
               "indicator_assets": [material["id"]], "levels": {"province": {"questionnaires": [qids["教师"]]},
                                                                 "school": {"split": True}}}
    assert successful(http.post(base + "/preview", json=payload))["reports"] == 3
    assert successful(http.post(base + "/preview", json={**payload, "mode": "province"}))["reports"] == 1
    created = successful(http.post(base + "/generate", json={**payload, "request_id": str(uuid4())}))
    assert successful(http.put(link_url, json={"dataset_id": dataset_id, "questionnaire_id": qids["学生"]}))["questionnaire_name"] == "学生"
    url = f"{base}/batches/{created['id']}"
    batch = await_state(http, url, lambda row: row["counts"]["done"] == 3 or row["counts"]["failed"]
                        or row["generation"]["state"] == "failed")
    assert batch["counts"]["done"] == 3, batch
    assert batch["generation"]["original_issues"] == [{"source": "补充问卷.docx", "reason": "原稿尚未关联问卷类型"}]
    assert sorted(source_calls) == ["学生", "教师"]
    assert any("未选择问卷" in correction for correction in corrections)
    assert set(map(frozenset, report_calls)) == {frozenset([teacher_note]), frozenset([student_note])}
    for report in batch["reports"]:
        report_url = f"{url}/reports/{report['id']}"
        doc = Document(io.BytesIO(http.get(report_url + "/download").content))
        red = Document(io.BytesIO(http.get(report_url + "/analysis").content))
        assert len([r for p in red.paragraphs for r in p.runs if str(r.font.color.rgb) == "FF0000"]) == 4
        text = " ".join(p.text for p in doc.paragraphs)
        if report["questionnaires"] == ["教师"]:
            assert "校本教研" in text and "学习支持" not in text
            assert any("50.0%（1 / 2）" in cell.text for table in doc.tables for row in table.rows for cell in row.cells)
        else:
            assert "学习支持" in text and "校本教研" not in text
            assert "教师满意率" not in " ".join(p.text for p in red.paragraphs)
    with SessionLocal.begin() as local:
        saved = local.get(SurveyBatch, created["id"]).config["questionnaire_originals"]
        assert {item["asset_id"]: item["questionnaire_id"] for item in saved} == {teacher["id"]: qids["教师"], student["id"]: qids["学生"]}
    reimport = successful(http.post(base + "/imports", json=source_specs))
    state = await_state(http, base, lambda row: row["dataset_id"] == reimport["id"])
    assert next(row for row in state["assets"] if row["id"] == teacher["id"])["linked_questionnaire_id"] == qids["学生"]
    assert http.get(f"{url}/reports/{batch['reports'][0]['id']}/download").is_success


def test_one_click_annual_rules_fixed_outlines_and_resumable_documents(client, db, monkeypatch):
    http, other = client
    configure_survey_model(db)
    base, dataset_id, catalog = prepare(http, SMALL)
    qids = {row["name"]: row["id"] for row in catalog["questionnaires"]}
    materials = successful(http.post(base + "/assets", data={"kind": "indicator"}, files={"file": annual_material()}))
    province_template = successful(http.post(base + "/assets", data={"kind": "template", "level": "province"},
        files={"file": ("省级模板.docx", docx_template("{{报告标题}}", "{{报告正文}}"))}))
    successful(http.put(f"{base}/templates/{province_template['id']}", json={"title_index": 1, "body_index": 2}))
    corrections = []
    stage_calls = {"outline": 0, "approved": 0, "red": 0, "rewritten": 0}
    reviewed_province = False
    actor_by_level = survey_authoring.ACTORS

    async def fake_model(_config, messages, **_kwargs):
        nonlocal reviewed_province
        payload = json.loads(messages[1]["content"])
        if "指标资料" in payload:
            ids = {row["title"]: row["id"] for row in payload["题库"] if row["questionnaire"] == "学生"}
            question_id = ids["满意度"]
            rate_options = [{"question_id": question_id, "op": "selected",
                             "value": "满意" if len(messages) > 2 else "不在题库"}]
            corrections.append(messages[-1]["content"])
            count_rule = {"questionnaire_id": qids["学生"], "method": "rate", "matches": rate_options,
                          "population": [{"question_id": ids["资源（多选）"], "op": "selected", "value": "图书"}],
                          "denominator": "answered", "unit": "百分比"}
            score_rule = {"questionnaire_id": qids["学生"], "method": "score",
                          "terms": [{"question_id": ids["数值"]}, {"question_id": question_id,
                                    "weight": 2, "scores": {"满意": 5, "不满意": 1}}], "unit": "分"}
            return json.dumps({"items": [
                {"name": "图书使用者满意率", "location": "段落 1", "definition": "勾选图书，按满意度有效作答作分母",
                 "questionnaire_id": qids["学生"], "rule": count_rule},
                {"name": "学生综合分", "location": "段落 2", "definition": "数值加满意度分值的两倍，两题都有效",
                 "questionnaire_id": qids["学生"], "rule": score_rule},
                {"name": "教师培训学时", "location": "段落 3", "definition": "教师实际有效培训学时的均值",
                 "questionnaire_id": qids["教师"], "rule": None, "reason": "答卷中缺少培训学时题"},
            ]}, ensure_ascii=False)
        level = payload["层级"]
        if "章节要求" in payload:
            stage_calls["outline"] += 1
            return json.dumps({"sections": [{"key": key, "title": title, "instruction": "本节按核实的资料撰写并说明样本范围"}
                                            for key, title, _ in payload["章节要求"]]}, ensure_ascii=False)
        if "正文" in payload:
            stage_calls["approved"] += 1
            if level == "province" and not reviewed_province:
                reviewed_province = True
                definitions = [entry["methodology"] for entry in payload["证据"].values() if "methodology" in entry]
                assert definitions[0]["numerator"] == 1 and definitions[0]["denominator"] == 2
                assert "两倍" in definitions[1]["definition"]
                return '{"approved":false,"issues":["按已核验的口径说明综合分"]}'
            return '{"approved":true,"issues":[]}'
        if payload.get("审核意见"):
            assert payload["审核意见"] == "按已核验的口径说明综合分"
            stage_calls["rewritten"] += 1
        if payload["任务"] == "红字解析":
            stage_calls["red"] += 1
        return json.dumps({"sections": [
            {"key": key, "text": "依据本批答卷范围和问卷统计，先复核问题并安排适当跟进。", "evidence": [choices[0]],
             "actor": actor_by_level[level][0]} for key, choices in payload["允许引用"].items()
        ]}, ensure_ascii=False)

    monkeypatch.setattr(survey_ai, "chat_completion", fake_model)
    request = {"dataset_id": dataset_id, "mode": "all", "indicator_assets": [materials["id"]],
               "levels": {"province": {"split": True, "templates": [province_template["id"]]}}, "automatic": True}
    preview = successful(http.post(base + "/preview", json=request))
    assert preview["reports"] == 8
    assert preview["levels"]["province"]["reports"] == 1
    levels = successful(http.post(base + "/preview", json={**request, "mode": "levels", "enabled_levels": ["city"]}))
    assert levels["reports"] == 2
    only_province = successful(http.post(base + "/preview", json={**request, "mode": "province"}))
    assert only_province["reports"] == 1
    selected = [row["id"] for row in catalog["organizations"] if row["level"] in ("province", "school")]
    mixed = successful(http.post(base + "/preview", json={**request, "mode": "custom", "org_ids": selected,
        "levels": {"school": {"questionnaires": [qids["教师"]], "split": True}}}))
    assert mixed["reports"] == 2 and mixed["levels"]["school"]["skipped"] == 2

    request["request_id"] = str(uuid4())
    assert http.post(base + "/generate", json=request, headers=other).status_code == 404
    created = successful(http.post(base + "/generate", json=request))
    assert successful(http.post(base + "/generate", json=request)) == created
    url = base + "/batches/" + str(created["id"])
    batch = await_state(http, url, lambda row: row["generation"]["state"] == "failed"
                        or row["counts"]["failed"] > 0 or row["counts"]["done"] == 8)
    assert batch["counts"] == {"queued": 0, "running": 0, "done": 8, "failed": 0, "stopping": 0, "stopped": 0}, batch
    assert len(corrections) == 2 and "筛选选项" in corrections[1]
    assert batch["generation"]["missing"][0]["reason"] == "答卷中缺少培训学时题"
    assert stage_calls["outline"] == 4 and stage_calls["approved"] == 9 and stage_calls["red"] > 8
    assert stage_calls["rewritten"] == 1
    outlines = {}
    red_texts = {}
    province_id = None
    for report in batch["reports"]:
        report_id, level = report["id"], report["org_level"]
        word_url = f"{url}/reports/{report_id}"
        published = http.get(word_url + "/download")
        red = http.get(word_url + "/analysis")
        assert published.is_success and red.is_success, (published.status_code, red.status_code)
        doc, analysis = Document(io.BytesIO(published.content)), Document(io.BytesIO(red.content))
        assert head_titles(doc) == head_titles(analysis)
        assert outlines.setdefault(level, head_titles(doc)) == head_titles(doc)
        red_runs = [r.text for p in analysis.paragraphs for r in p.runs if str(r.font.color.rgb) == "FF0000"]
        assert len(red_runs) == 4
        assert all(report["org_path"] in text and "【核实依据】" in text for text in red_runs)
        assert all(str(r.font.color.rgb) != "FF0000" for p in doc.paragraphs for r in p.runs)
        red_texts.setdefault(level, set()).add(tuple(red_runs))
        if level == "province":
            province_id = report_id
            assert doc.paragraphs[0].text == "原来的封面"
            assert doc.paragraphs[-1].text == "模板页尾"
            cells = [cell.text for table in doc.tables for row in table.rows for cell in row.cells]
            assert any("50.0%（1 / 2）" in cell for cell in cells), cells
            assert any("41.00分（有效 2 条）" in cell for cell in cells), cells
            assert any("答卷中缺少培训学时题" in cell for cell in cells)
            assert http.get(word_url + "/analysis", headers=other).status_code == 404
        elif level == "school":
            assert "学校管理团队" in " ".join(p.text for p in doc.paragraphs)
    assert len(set(title for outline in outlines.values() for title in outline)) == 16
    assert len(red_texts["school"]) == 3
    zipped_red = http.get(url + "/zip", params={"analysis": True, "level": "school"})
    assert zipped_red.is_success
    with ZipFile(io.BytesIO(zipped_red.content)) as bundle:
        assert len(bundle.namelist()) == 3 and bundle.testzip() is None

    calls_before = stage_calls.copy()
    with SessionLocal.begin() as local:
        preparation = local.get(SurveyBatchPreparation, created["id"])
        assert preparation.checkpoint["calculated"]
        assert local.get(SurveyReportDraft, province_id).checkpoint["approved"]
        local.get(SurveyReport, province_id).state = "running"
    survey_worker.report_path(1, province_id).unlink()
    survey_worker.recover_interrupted()
    await_state(http, url, lambda row: row["counts"]["done"] == 8)
    assert stage_calls == calls_before
    assert http.get(f"{url}/reports/{province_id}/download").is_success


@pytest.mark.skipif(os.getenv("RUN_SURVEY_LOAD") != "1", reason="数千份一键出报压测需显式开启")
def test_one_click_two_thousand_word_reports_with_bounded_model_concurrency(client, db, monkeypatch):
    http, _ = client
    configure_survey_model(db)
    rows = [(f"学校{i:04}", str(i), "测试市", "测试区", "学生", "满意", "网络", 22) for i in range(2000)]
    base, dataset_id, _ = prepare(http, rows)
    active, peak, calls = 0, 0, 0

    async def fake_model(_config, messages, **_kwargs):
        nonlocal active, peak, calls
        active += 1
        calls += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.002)
            payload = json.loads(messages[1]["content"])
            if "章节要求" in payload:
                return json.dumps({"sections": [{"key": key, "title": title, "instruction": "按实际作答范围说明本节结果"}
                    for key, title, _ in payload["章节要求"]]}, ensure_ascii=False)
            if "正文" in payload:
                return '{"approved":true,"issues":[]}'
            return json.dumps({"sections": [
                {"key": key, "text": "按本批答卷复核当前样本，并登记本次观察范围。",
                 "evidence": [choices[0]], "actor": survey_authoring.ACTORS[payload["层级"]][0]}
                for key, choices in payload["允许引用"].items()]}, ensure_ascii=False)
        finally:
            active -= 1

    monkeypatch.setattr(survey_ai, "chat_completion", fake_model)
    monkeypatch.setattr(settings, "SURVEY_REPORT_WORKERS", 6)
    created = successful(http.post(base + "/generate", json={"dataset_id": dataset_id, "mode": "all",
                                                           "request_id": str(uuid4())}))
    url = base + "/batches/" + str(created["id"])
    status = await_state(http, url, lambda row: row["counts"]["done"] + row["counts"]["failed"] == 2003,
                         timeout=360)
    assert status["counts"] == {"queued": 0, "running": 0, "done": 2003, "failed": 0, "stopping": 0, "stopped": 0}
    assert 1 < peak <= 3 and calls >= 6009
    archived = http.get(url + "/zip", params={"level": "school", "part": 20})
    assert archived.is_success
    with ZipFile(io.BytesIO(archived.content)) as bundle:
        assert len(bundle.namelist()) == 100 and bundle.testzip() is None
