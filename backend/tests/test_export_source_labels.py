"""导出副本使用中文来源名称，存储内容与图表数据保持不变。"""
import io
import json
from types import SimpleNamespace

from docx import Document
from PIL import Image

from app import assemble, generate
from app.models import ChapterTask, ModuleContent, ProjectFile
from app.report_text import SourceTextFormatter


def test_export_text_and_table_removes_csv_names_without_changing_history(
        tmp_path, project, db, monkeypatch):
    item, task, _ = project
    task.slot_key = "legacy-text-slot"
    task.status = "done"
    text = '样本统计使用survey-submissions-5.csv工作表“CSV”，共有2054条提交记录，支持320条，占15.6%。'
    body = ModuleContent(chapter_task_id=task.id, version=1, is_current=True,
                         status="ok", content_type="text", content=text)
    table_task = ChapterTask(project_id=item.id, title="提交统计", task_type="table", status="done",
                             instruction="【生成表格：提交统计】", anchor_task_id=task.id, sort=1)
    db.add_all([body, table_task, ProjectFile(project_id=item.id, file_type="data",
                file_name="survey-submissions-5.csv", minio_path="actual/raw.csv")])
    db.flush()
    payload = {"columns": ["对象", "数量", "字段说明"],
               "rows": [["教师", 386, "CSV对应：原件第20题→q21"]],
               "note": "数据来源：survey-submissions-5.csv。按该题作答记录为分母。"}
    table_body = ModuleContent(chapter_task_id=table_task.id, version=1, is_current=True,
                               status="ok", content_type="table",
                               content=json.dumps(payload, ensure_ascii=False))
    db.add(table_body)
    db.commit()
    original = (body.content, table_body.content)
    template = Document()
    template.add_paragraph(task.instruction)
    raw = io.BytesIO()
    template.save(raw)
    monkeypatch.setattr(assemble, "_template_bytes", lambda *_: raw.getvalue())

    manifest = assemble.prepare_manifest(db, item, tmp_path)
    output = tmp_path / "out" / "report.docx"
    assemble.assemble_template_docx(tmp_path / "template.docx", manifest, output)
    report = Document(output)
    visible = "\n".join([p.text for p in report.paragraphs] +
                         [c.text for t in report.tables for r in t.rows for c in r.cells])
    assert "csv" not in visible.lower()
    assert "调查问卷数据，共有2054条提交记录，支持320条，占15.6%。" in visible
    assert "数据字段对应：原件第20题→q21" in visible
    assert "386" in visible and "数据来源：调查问卷数据。" in visible
    db.expire_all()
    assert (body.content, table_body.content) == original
    assert db.query(ProjectFile).one().minio_path == "actual/raw.csv"


def test_chart_redraw_receives_display_names_and_preserves_values(tmp_path, monkeypatch):
    chart = {"title": "CSV统计结果", "labels": ["支持", "反对"], "values": [4, 2],
             "metric": "count", "denominator": 6, "note": "数据来源：survey-submissions-5.csv。"}
    task = SimpleNamespace(id=7, title="统计图", instruction="【生成图片（条形图）：统计结果】")
    original = json.dumps(chart, ensure_ascii=False)
    content = SimpleNamespace(content=original)
    received = []

    def render(value, instruction, path, *, document=False):
        received.append(value)
        assert document
        Image.new("RGB", (100, 100), "blue").save(path)

    monkeypatch.setattr(generate, "render_verified_chart", render)
    (tmp_path / "contents").mkdir()
    files = assemble._verified_chart_files(tmp_path, task, content, True,
                     source_formatter=SourceTextFormatter(["survey-submissions-5.csv"]))
    assert len(files) == 1 and (tmp_path / files[0]).is_file()
    display = received[0]
    assert display["title"] == "问卷数据统计结果"
    assert display["note"] == "数据来源：调查问卷数据。"
    for key in ("values", "metric", "denominator", "labels"):
        assert display[key] == chart[key]
    assert content.content == original
