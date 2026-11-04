# -*- coding: utf-8 -*-
"""合成完整模板/资料回归：全量结构、渲染及导出，AI 审核完全模拟。"""
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks

sys.path.insert(0, str(Path(__file__).resolve().parent))

from offline_support import install_offline_environment

env = install_offline_environment()

from docx import Document

from app import assemble, generate, review, sandbox, storage, template_parse
from app.db import SessionLocal
from app.models import ChapterTask, ModelConfig, ModuleContent, Project, ProjectFile, User
from app.routers.projects import assemble_project, delete_tasks_cascade, save_tasks


def prepare_complete_fixtures():
    """给共享结构模板标注所有章节，并生成已知规模的合成参考材料。"""
    raw = env.template.read_bytes()
    instructions = []
    for index, block in enumerate(template_parse.extract_structure(raw)):
        if block["kind"] not in {"heading", "front_title"}:
            continue
        items = [
            {"type": "text", "instruction": "【填写文字分析：依据项目参考材料编写本模块。】"},
            {"type": "image", "instruction": "【生成图片（柱状图）：展示数据文件的样本数。】"},
        ]
        if len(instructions) < 4:
            items.append({"type": "table", "instruction": "【生成表格：展示参考材料中的模块依据。】"})
        if index == 0:
            items.append({"type": "note", "instruction": "【说明：模板控制说明不生成内容。】"})
        instructions.append({"heading": block["text"], "instructions": items})
    template = env.root / "complete-template.docx"
    template.write_bytes(template_parse.write_instructions_to_docx(raw, instructions))
    reference = env.root / "complete-reference.docx"
    document = Document()
    document.add_heading("合成问卷与访谈材料", 1)
    for number in range(420):
        document.add_paragraph(f"合成问题条目{number + 1}：请结合课程教学、跨学段衔接和实践活动说明情况。")
    document.save(reference)
    return template, [template, env.xlsx, env.reference, env.reference_big, reference]


def module_content(project, task, **values):
    return ModuleContent(
        chapter_task_id=task.id,
        material_revision=project.material_revision,
        instruction_revision=task.instruction_revision,
        **values,
    )


def review_record(content: str, project: Project, task: ChapterTask) -> str:
    return json.dumps({
        "status": "ok",
        "issues": [],
        "protocol": review.REVIEW_PROTOCOL_VERSION,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "material_revision": project.material_revision,
        "instruction_revision": task.instruction_revision,
        "reviewed_at": 1,
    }, ensure_ascii=False)


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def main():
    template, fixture_paths = prepare_complete_fixtures()
    db = SessionLocal()
    user = db.get(User, env.user_id)
    project = Project(user_id=user.id, name="合成完整模板/资料回归", status="analyzed")
    db.add(project)
    db.commit()
    db.refresh(project)
    uploaded: list[str] = []
    try:
        for path in fixture_paths:
            if path == template:
                file_type = "template"
            elif path.suffix.lower() in {".xlsx", ".csv"}:
                file_type = "data"
            else:
                file_type = "reference"
            object_path = f"{user.id}/{project.id}/upload/{path.name}"
            storage.upload_bytes(object_path, path.read_bytes())
            uploaded.append(object_path)
            db.add(ProjectFile(
                project_id=project.id, file_type=file_type, file_name=path.name,
                minio_path=object_path, size=path.stat().st_size,
            ))
        db.commit()

        template_bytes = template.read_bytes()
        blocks = template_parse.extract_structure(template_bytes)
        tasks = template_parse.tasks_from_red_marks(blocks)
        content_tasks = [task for task in tasks if task["task_type"] != "heading"]
        check(len(content_tasks) >= 40, "合成完整模板拆解出通用内容任务")
        check({task["task_type"] for task in content_tasks} == {"text", "table", "image"},
              "合成完整模板覆盖文字、表格和图片任务")
        check(not any(template_parse.is_note_instruction(task["instruction"]) for task in content_tasks),
              "模板控制说明没有进入内容任务")
        save_tasks(db, project.id, tasks)
        db.commit()
        rows = db.query(ChapterTask).filter(
            ChapterTask.project_id == project.id
        ).order_by(ChapterTask.sort).all()
        data_rows = sum(len(sheet_rows) - 1 for _, sheet_rows in
                        template_parse.read_rows(env.xlsx.name, env.xlsx.read_bytes()))
        chart_data = {
            "title": "结构化结果图",
            "labels": ["样本数"],
            "values": [data_rows],
            "metric": "count",
            "counts": [data_rows],
        }
        chart_workdir = sandbox.fresh_workdir(project.id, 87)
        image_source = chart_workdir / "out" / "verified-chart.png"
        generate.render_verified_chart(chart_data, "【生成图片（柱状图）：样本数】", image_source)
        check(image_source.stat().st_size > 5000, "合成图表可由图表数据重新渲染")

        for task in rows:
            if task.task_type == "heading":
                continue
            if task.task_type == "text":
                content = (
                    "本模块内容依据项目数据文件与参考资料编排。"
                    "正文中的具体事实均可用数据文件核验，方法口径集中说明。"
                )
                db.add(module_content(project, task,
                    version=1, content_type="text", content=content,
                    review_json=review_record(content, project, task),
                    status="ok", is_current=True,
                ))
            elif task.task_type == "table":
                content = json.dumps({
                    "title": "结构化结果表", "columns": ["项目", "依据"],
                    "rows": [["本模块", "数据文件"],
                             ["样本数", data_rows]],
                }, ensure_ascii=False)
                db.add(module_content(project, task,
                    version=1, content_type="table", content=content,
                    review_json=review_record(content, project, task),
                    status="ok", is_current=True,
                ))
            elif task.task_type == "image":
                image_path = f"{user.id}/{project.id}/images/task{task.id}.png"
                storage.upload_bytes(image_path, image_source.read_bytes(), content_type="image/png")
                uploaded.append(image_path)
                content = json.dumps(chart_data, ensure_ascii=False)
                db.add(module_content(project, task,
                    version=1, content_type="image", content=content,
                    image_path=image_path,
                    review_json=review_record(content, project, task),
                    status="ok", is_current=True,
                ))
            task.status = "done"
        db.commit()

        workdir = sandbox.fresh_workdir(project.id, 88)
        manifest = assemble.prepare_manifest(db, project, workdir)
        output = workdir / "out" / "report.docx"
        result = assemble.assemble_template_docx(
            workdir / manifest["template"], manifest, output
        )
        check(result["before"]["sections"] == result["after"]["sections"],
              "合成完整模板装配后 section 数量保持")
        check(result["before"]["geometry"] == result["after"]["geometry"],
              "合成完整模板装配后页面几何保持")
        ok, note, _ = assemble.make_report_validator(manifest)("synthetic complete fixture", workdir)
        check(ok, f"合成完整模板结构门槛通过：{note}")
        render = assemble._render_gate(output, workdir)
        check(render["ok"], f"合成完整模板 LibreOffice 渲染门槛通过：pages={render.get('pages')}，{render.get('message', '')}")
        check(render["pages"] > 0 and len(render["page_images"]) == render["pages"],
              "合成完整模板页面 PNG 数量与 PDF 页数一致")
        doc = Document(str(output))
        check(len(doc.sections) == result["before"]["sections"], "合成完整报告 section 可重新打开")
        check(not assemble._instruction_paragraphs(doc), "合成完整报告没有残留红色编写指令")

        review_reply = json.dumps({"status": "ok", "issues": []}, ensure_ascii=False)
        original_chat = generate.chat_completion
        original_pick_model = generate.pick_model

        async def fake_chat(_cfg, messages, timeout=600.0):
            system = str(messages[0].get("content", ""))
            check("整稿一致性复核" in system, "未变更模块复用生成时的复核结论，导出只做整稿复核")
            return review_reply

        generate.chat_completion = fake_chat
        generate.pick_model = lambda *_args, **_kwargs: ModelConfig(
            user_id=user.id, name="offline-review", agent_type="review",
            base_url="http://offline", api_key_enc="unused", system_prompt="", model="offline-review"
        )
        try:
            background = BackgroundTasks()
            accepted = asyncio.run(assemble_project(project.id, background, user, db))
            check(bool(accepted["run_id"]), "完整导出入口分配运行令牌")
            asyncio.run(background())
            db.refresh(project)
            exported_path = project.report_path
        finally:
            generate.chat_completion = original_chat
            generate.pick_model = original_pick_model
        uploaded.append(exported_path)
        db.refresh(project)
        check(project.report_path == exported_path and storage.download_bytes(exported_path),
              "完整导出路径依次通过审核、模板/DOCX/渲染门槛后才上传报告")

        # 篡改一个模块内容：复核结论失效，AI 复核给出 error 时应拦截导出。
        invalid_content = next(
            row for row in db.query(ModuleContent).filter(
                ModuleContent.chapter_task_id.in_([
                    task.id for task in rows if task.task_type == "text"
                ]),
                ModuleContent.is_current == True,  # noqa: E712
            ).all()
        )
        invalid_content.content = "被篡改的正文内容，与数据文件无关。" * 4
        db.commit()

        async def blocked_chat(_cfg, messages, timeout=600.0):
            goal = str(messages[1].get("content", "")) if len(messages) > 1 else ""
            if "审核一个报告模块" in goal:
                return json.dumps({
                    "action": "finish",
                    "action_input": json.dumps({"status": "issues", "issues": [
                        {"severity": "error", "message": "内容与数据文件不符"}]}, ensure_ascii=False),
                }, ensure_ascii=False)
            return review_reply

        generate.chat_completion = blocked_chat
        generate.pick_model = lambda *_args, **_kwargs: ModelConfig(
            user_id=user.id, name="offline-review", agent_type="review",
            base_url="http://offline", api_key_enc="unused", system_prompt="", model="offline-review"
        )
        try:
            background = BackgroundTasks()
            asyncio.run(assemble_project(project.id, background, user, db))
            with patch("app.routers.projects.logger.exception") as expected_error:
                asyncio.run(background())
            check(expected_error.call_count == 1, "审核失败的后台导出记录错误")
        finally:
            generate.chat_completion = original_chat
            generate.pick_model = original_pick_model
        db.refresh(project)
        check("审核" in project.error_msg and not project.operation_token,
              "审核失败时完整导出路径中止并释放运行令牌")
        check(not project.report_path, "审核失败后不保留 report_path")
        try:
            storage.download_bytes(exported_path)
        except Exception:
            check(True, "审核失败后会删除先前导出的过期报告")
        else:
            check(False, "审核失败后不应保留先前导出的文件")

        print("SYNTHETIC_REPORT (temporary)", output)
        print("SYNTHETIC_QA (temporary)", render["pdf"])
    finally:
        task_ids = [task.id for task in db.query(ChapterTask).filter(
            ChapterTask.project_id == project.id
        ).all()]
        delete_tasks_cascade(db, task_ids)
        db.query(ProjectFile).filter(ProjectFile.project_id == project.id).delete()
        db.query(Project).filter(Project.id == project.id).delete()
        db.commit()
        for object_path in uploaded:
            try:
                storage.remove(object_path)
            except Exception:
                pass
        db.close()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        env.close()
