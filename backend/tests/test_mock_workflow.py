"""上传→分析→三类生成→失败重写/回退→导出 AI 审核→真实 Word/PDF 渲染（离线模拟）。"""
import asyncio
import io
import json
import os
import shutil
from pathlib import Path

from docx import Document
from docx.shared import RGBColor
from fastapi import BackgroundTasks, UploadFile
from sqlalchemy import select

from app import assemble, generate, sandbox, storage
from app.models import ChapterTask, ModelConfig, ModuleContent, Project
from app.routers import projects


def test_mock_model_workflow_with_native_docx_render(project, db, monkeypatch, tmp_path):
    run_dirs = []
    fresh_workdir = sandbox.fresh_workdir
    def record_workdir(project_id, task_id, run_token=None):
        directory = fresh_workdir(project_id, task_id, run_token)
        if task_id == 0:
            run_dirs.append(directory)
        return directory
    monkeypatch.setattr(sandbox, "fresh_workdir", record_workdir)
    item, initial, user = project
    db.delete(initial)
    item.status = "created"
    db.add(ModelConfig(user_id=user.id, name="模拟模型", base_url="https://offline.invalid",
                       api_key_enc="not-used", model="mock-model", is_default=True))
    db.commit()
    project_id = item.id
    doc = Document()
    doc.add_paragraph("调查报告")
    for title, instruction in [
        ("调查分析", "【填写：说明支持与反对人数】"),
        ("统计表", "【生成表格：各选项人数表】"),
        ("统计图", "【生成图片（柱状图）：各选项人数】"),
    ]:
        doc.add_heading(title, level=1)
        doc.add_paragraph("原有说明文字，导出后必须保留。")
        doc.add_paragraph().add_run(instruction).font.color.rgb = RGBColor(255, 0, 0)
    doc_stream = io.BytesIO()
    doc.save(doc_stream)
    for filename, raw, kind in [("template.docx", doc_stream.getvalue(), "template"),
                                ("调查数据.csv", "态度\n支持\n支持\n反对\n".encode(), "data")]:
        asyncio.run(projects.upload_file(project_id, UploadFile(filename=filename, file=io.BytesIO(raw)), kind, user, db))
    background = BackgroundTasks()
    analysis = asyncio.run(projects.analyze(project_id, background, user, db))
    assert analysis["run_id"]
    asyncio.run(background())
    db.expire_all()
    item = db.get(Project, project_id)
    assert item.status == "analyzed"

    text = ("支持者有2人，占66.7%。调查结果显示，多数受访者持支持态度。"
            "后续工作可结合不同意见进一步了解受访者的具体需求，并据此安排沟通与反馈。")
    table = {"title": "表1 各选项人数表", "columns": ["选项", "人数"], "rows": [["支持", 2], ["反对", 1]]}
    chart = {"title": "态度人数", "caption": "图1 态度人数",
             "labels": ["支持", "反对"], "values": [2, 1],
             "metric": "count", "counts": [2, 1]}
    chart_code = (
        "import pandas as pd, json, random\n"
        "from PIL import Image, ImageDraw\n"
        "df = pd.read_csv('data/调查数据.csv'); counts = df['态度'].value_counts()\n"
        "assert counts['支持'] == 2 and counts['反对'] == 1\n"
        "img = Image.new('RGB', (900, 600), 'white'); draw = ImageDraw.Draw(img)\n"
        "draw.rectangle([80, 80, 380, 520], fill=(76, 120, 168))\n"
        "draw.rectangle([470, 260, 770, 520], fill=(228, 87, 86))\n"
        "random.seed(1)\n"
        "for _ in range(20000):\n"
        "    draw.point((random.randrange(900), random.randrange(600)),\n"
        "               fill=(random.randrange(256), random.randrange(256), random.randrange(256)))\n"
        "img.save('out/chart.png')\n"
        f"json.dump({chart!r}, open('out/chart_data.json', 'w'), ensure_ascii=False)\n"
        "print('真实数据绘图完成')"
    )
    calls = []
    review_calls = []

    async def fake_model(_cfg, messages, **_kwargs):
        system = str(messages[0].get("content", ""))
        goal = str(messages[1]["content"]) if len(messages) > 1 else ""
        if "复核一个刚生成的报告模块" in system:
            review_calls.append("light")
            return json.dumps({"status": "ok", "issues": []})
        if "整稿一致性复核" in system:
            review_calls.append("final")
            return json.dumps({"status": "ok", "issues": []})
        if "审核一个报告模块" in goal:
            review_calls.append("module")
            return json.dumps({
                "action": "finish",
                "action_input": json.dumps({"status": "ok", "issues": []}, ensure_ascii=False),
            }, ensure_ascii=False)
        calls.append(goal[:30])
        if "【任务】绘制" in goal:
            payload = ({"action": "run_python", "action_input": chart_code} if len(messages) == 2
                       else {"action": "finish", "action_input": "受访者态度人数统计图"})
        elif "【任务】统计生成" in goal:
            payload = {"action": "finish", "action_input": json.dumps(table, ensure_ascii=False)}
        else:
            payload = {"action": "finish",
                       "action_input": json.dumps({"content": text}, ensure_ascii=False)}
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(generate, "chat_completion", fake_model)
    async def workflow():
        background = BackgroundTasks()
        response = await projects.generate_all(project_id, background, user, db)
        await background()
        return response
    generated = asyncio.run(workflow())
    db.expire_all()
    item = db.get(Project, project_id)
    assert item.last_run_id == generated["run_id"]
    assert json.loads(item.last_run_result)["succeeded"] == 3
    assert review_calls.count("light") == 3
    tasks = db.scalars(select(ChapterTask).where(ChapterTask.project_id == project_id,
                                               ChapterTask.task_type != "heading")).all()
    text_task = next(t for t in tasks if t.task_type == "text")
    current = db.scalar(select(ModuleContent).where(ModuleContent.chapter_task_id == text_task.id,
                                                   ModuleContent.is_current == True))
    old_id = current.id
    assert current.content == text
    assert json.loads(current.review_json)["status"] == "ok"
    async def fail_model(*_args, **_kwargs):
        raise RuntimeError("模拟重写故障")
    monkeypatch.setattr(generate, "chat_completion", fail_model)
    background = BackgroundTasks()
    asyncio.run(projects.rewrite_one(project_id, text_task.id, projects.RewriteIn(prompt="重写"), background, user, db))
    asyncio.run(background())
    db.expire_all()
    assert db.get(ModuleContent, old_id).is_current
    projects.rollback_content(project_id, text_task.id, projects.RollbackIn(content_id=old_id), user, db)
    monkeypatch.setattr(generate, "chat_completion", fake_model)
    background = BackgroundTasks()
    exported = asyncio.run(projects.assemble_project(project_id, background, user, db))
    asyncio.run(background())
    db.expire_all()
    item = db.get(Project, project_id)
    assert item.last_run_id == exported["run_id"]
    assert item.report_path, item.error_msg
    # 未变更模块复用生成时的复核结论，导出不再重复逐模块复核。
    assert "module" not in review_calls
    assert review_calls.count("final") == 1
    report = tmp_path / "mock-report.docx"
    report.write_bytes(storage.download_bytes(item.report_path))
    final_doc = Document(report)
    assert text in "\n".join(p.text for p in final_doc.paragraphs)
    assert sum(p.text == "原有说明文字，导出后必须保留。" for p in final_doc.paragraphs) == 3
    assert len(final_doc.tables) == len(final_doc.inline_shapes) == 1
    # 实际导出已经过原生渲染门槛，取出其 PDF 和逐页 PNG 供人工检查。
    if os.environ.get("AI_REPORT_TEST_ARTIFACT_DIR"):
        destination = Path(os.environ["AI_REPORT_TEST_ARTIFACT_DIR"])
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(report, destination / "模拟流程报告.docx")
        if run_dirs:
            for file in (run_dirs[-1] / "qa").glob("*.pdf"):
                shutil.copy2(file, destination / "模拟流程报告.pdf")
            for file in (run_dirs[-1] / "qa").glob("*.png"):
                shutil.copy2(file, destination / file.name)
    assert calls
