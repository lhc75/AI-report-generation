# -*- coding: utf-8 -*-
"""离线验证第三步：原模板原位装配、重复 slot、插入模块和结构门槛。"""
import base64
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from offline_support import initialize_offline_environment

# 在 app 导入前创建独立 SQLite、对象存储和 sandbox 目录。
env = initialize_offline_environment()

from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from app import assemble, sandbox, storage
from app.models import ChapterTask, ModuleContent, Project, ProjectFile, User
from app.routers.projects import delete_tasks_cascade


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def module_content(db, **values):
    task = db.get(ChapterTask, values["chapter_task_id"])
    project = db.get(Project, task.project_id)
    return ModuleContent(
        material_revision=project.material_revision,
        instruction_revision=task.instruction_revision,
        **values,
    )


def bookmark(paragraph, name="keep_bookmark"):
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), "42")
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), "42")
    paragraph._p.insert(0, start)
    paragraph._p.append(end)


def write_valid_png(path: Path) -> None:
    path.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    ))


def build_template() -> bytes:
    doc = Document()
    doc.add_paragraph("模板原有封面标题")
    cover = doc.add_paragraph("模板书签内容")
    bookmark(cover)
    toc = doc.add_paragraph("目 录")
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), ' TOC \\o "1-3" \\h \\z \\u ')
    toc._p.append(field)
    doc.add_heading("第一部分 概况", level=1)
    doc.add_paragraph("原有前置说明")
    doc.add_paragraph("【填写：正文模块】")
    doc.add_section(WD_SECTION.NEW_PAGE)
    doc.add_heading("第二部分 结果", level=1)
    doc.add_paragraph("【填写：正文模块】")
    doc.add_paragraph("【生成表格：结果表】")
    doc.add_paragraph("【生成图片：结果图】")
    doc.add_paragraph("【填写：缺失模块】")
    doc.add_paragraph("【说明：这是一条模板备注，不产生任务】")
    doc.add_table(rows=1, cols=2)
    doc.tables[0].rows[0].cells[0].text = "模板原有表格"
    doc.tables[0].rows[0].cells[1].text = "保留"
    doc.add_picture(str(env.chart))
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


def test_front_instruction_and_repeated_image_slots():
    workdir = sandbox.fresh_workdir(0, 776)
    (workdir / "contents").mkdir(exist_ok=True)
    doc = Document()
    doc.add_paragraph("说 明")
    doc.add_paragraph("【填写：整体说明】")
    doc.add_heading("第一部分", level=1)
    doc.add_heading("A 小节", level=2)
    doc.add_paragraph("【生成图片：重复图】")
    doc.add_heading("B 小节", level=2)
    doc.add_paragraph("【生成图片：重复图】")
    template = workdir / "template.docx"
    doc.save(template)
    signature = assemble._doc_signature(Document(str(template)))
    (workdir / "contents" / "t2.json").write_text("说明内容已替换。", encoding="utf-8")
    write_valid_png(workdir / "contents" / "t5.png")
    write_valid_png(workdir / "contents" / "t7.png")
    manifest = {
        "title": "slot 漂移回归",
        "template": "template.docx",
        "signature": signature,
        "nodes": [
            {"id": 1, "parent_id": None, "level": 1, "title": "说 明", "type": "heading"},
            {
                "id": 2, "parent_id": 1, "level": 2, "title": "整体说明",
                "type": "text", "instruction": "【填写：整体说明】",
                "slot_key": "slot-front", "file": "contents/t2.json",
            },
            {"id": 3, "parent_id": None, "level": 1, "title": "第一部分", "type": "heading"},
            {"id": 4, "parent_id": 3, "level": 2, "title": "A 小节", "type": "heading"},
            {
                "id": 5, "parent_id": 4, "level": 3, "title": "重复图 A",
                "type": "image", "instruction": "【生成图片：重复图】",
                "slot_key": "slot-img-a", "file": "contents/t5.png",
            },
            {"id": 6, "parent_id": 3, "level": 2, "title": "B 小节", "type": "heading"},
            {
                "id": 7, "parent_id": 6, "level": 3, "title": "重复图 B",
                "type": "image", "instruction": "【生成图片：重复图】",
                "slot_key": "slot-img-b", "file": "contents/t7.png",
            },
        ],
    }
    output = workdir / "out" / "report.docx"
    assemble.assemble_template_docx(template, manifest, output)
    ok, note, _ = assemble.make_report_validator(manifest)("slot probe", workdir)
    check(ok, f"前置普通段落标题不要求 Heading 样式：{note or 'ok'}")
    out_doc = Document(str(output))
    texts = [paragraph.text for paragraph in out_doc.paragraphs]
    check("说明内容已替换。" in texts, "前置非 Heading 说明指令通过顺序兜底定位")
    check(not any(text.startswith("【") for text in texts), "重复图片指令全部被消费")
    check(len(out_doc.inline_shapes) == 2, "重复图片指令按各自章节原位替换")


def main():
    test_front_instruction_and_repeated_image_slots()
    db = env.session_factory()
    user = db.get(User, env.user_id)
    project = Project(user_id=user.id, name="离线验证-原位装配", status="analyzed",
                      material_revision=2)
    db.add(project)
    db.commit()
    db.refresh(project)
    pid = project.id
    template_path = f"{user.id}/{pid}/upload/template.docx"
    template_bytes = build_template()
    storage.upload_bytes(template_path, template_bytes)
    db.add(ProjectFile(
        project_id=pid, file_type="template", file_name="template.docx",
        minio_path=template_path, size=len(template_bytes),
    ))
    db.commit()

    tasks = [
        ChapterTask(project_id=pid, level=1, title="第一部分 概况", sort=1,
                    task_type="heading", instruction="", slot_key="heading-1", status="done"),
        ChapterTask(project_id=pid, level=2, title="正文一", sort=2,
                    task_type="text", instruction="【填写：正文模块】",
                    slot_key="slot-text-1", status="done"),
        ChapterTask(project_id=pid, level=1, title="第二部分 结果", sort=3,
                    task_type="heading", instruction="", slot_key="heading-2", status="done"),
        ChapterTask(project_id=pid, level=2, title="正文二", sort=4,
                    task_type="text", instruction="【填写：正文模块】",
                    slot_key="slot-text-2", status="done"),
        ChapterTask(project_id=pid, level=2, title="结果表", sort=5,
                    task_type="table", instruction="【生成表格：结果表】",
                    slot_key="slot-table", status="done"),
        ChapterTask(project_id=pid, level=2, title="结果图", sort=6,
                    task_type="image", instruction="【生成图片：结果图】",
                    slot_key="slot-image", status="done"),
        ChapterTask(project_id=pid, level=2, title="缺失模块", sort=7,
                    task_type="text", instruction="【填写：缺失模块】",
                    slot_key="slot-missing", status="done"),
    ]
    for task in tasks:
        task.instruction_revision = 1
    db.add_all(tasks)
    db.commit()
    for task in tasks:
        db.refresh(task)
    evidence_id = "ev_test"
    db.add_all([
        module_content(db,
            chapter_task_id=tasks[1].id, version=1, content_type="text",
            content="正文一已经按照模板位置替换。",
            evidence_json=json.dumps([{
                "text": "正文一已经按照模板位置替换。",
                "kind": "fact",
                "evidence_ids": [evidence_id],
            }], ensure_ascii=False),
            status="ok", is_current=True,
        ),
        module_content(db,
            chapter_task_id=tasks[3].id, version=1, content_type="text",
            content="正文二保留在第二部分。",
            evidence_json=json.dumps([{
                "text": "正文二保留在第二部分。",
                "kind": "fact",
                "evidence_ids": [evidence_id],
            }], ensure_ascii=False),
            status="ok", is_current=True,
        ),
        module_content(db,
            chapter_task_id=tasks[4].id, version=1, content_type="table",
            content=json.dumps({
                "title": "结果表",
                "columns": ["类别", "说明"],
                "rows": [["A", "通过"]],
            }, ensure_ascii=False),
            evidence_json=json.dumps({
                "evidence_ids": [evidence_id],
                "cell_evidence": {},
            }, ensure_ascii=False),
            status="ok", is_current=True,
        ),
    ])
    image_path = f"{user.id}/{pid}/images/result.png"
    storage.upload_bytes(image_path, env.chart.read_bytes(), content_type="image/png")
    db.add(module_content(db,
        chapter_task_id=tasks[5].id, version=1, content_type="image",
        image_path=image_path,
        evidence_json=json.dumps({
            "title": "结果图", "labels": ["A"], "values": [1], "metric": "count",
            "value_evidence": [evidence_id],
            "evidence_ids": [evidence_id],
        }, ensure_ascii=False),
        status="ok", is_current=True,
    ))
    db.commit()

    inserted = ChapterTask(
        project_id=pid, level=2, title="插入模块", sort=6,
        task_type="text", instruction="【填写：插入模块】",
        slot_key="slot-inserted", anchor_task_id=tasks[4].id, status="done",
    )
    db.add(inserted)
    db.commit()
    db.refresh(inserted)
    db.add(module_content(db,
        chapter_task_id=inserted.id, version=1, content_type="text",
        content="这是明确锚定在结果表之后的插入模块。",
        evidence_json=json.dumps([{
            "text": "这是明确锚定在结果表之后的插入模块。",
            "kind": "fact",
            "evidence_ids": [evidence_id],
        }], ensure_ascii=False),
        status="ok", is_current=True,
    ))
    db.commit()

    try:
        workdir = sandbox.fresh_workdir(pid, 77)
        manifest = assemble.prepare_manifest(db, project, workdir)
        check(len(manifest["nodes"]) == 8, "manifest 包含标题、重复正文、表格、图片和插入模块")
        check(manifest["nodes"][1]["slot_key"] == "slot-text-1", "manifest 保留 slot_key")
        check(any(node.get("anchor_task_id") == tasks[4].id for node in manifest["nodes"]),
              "manifest 保留插入模块 anchor_task_id")
        missing_node = next(node for node in manifest["nodes"] if node["title"] == "缺失模块")
        check(missing_node.get("missing") is True, "缺失内容模块被显式标记")

        output = workdir / "out" / "report.docx"
        result = assemble.assemble_template_docx(
            workdir / manifest["template"], manifest, output
        )
        check(result["before"]["sections"] == result["after"]["sections"],
              "装配不改变 section 数量")
        check(result["before"]["geometry"] == result["after"]["geometry"],
              "装配不改变页面几何")
        check(result["before"]["sections"] == 2 and result["before"]["toc"],
              "多 section 模板和目录域被识别")

        doc = Document(str(output))
        texts = [paragraph.text for paragraph in doc.paragraphs]
        check("模板原有封面标题" in texts and "模板书签内容" in texts,
              "模板封面和书签文本保留")
        check(texts.count("正文一已经按照模板位置替换。") == 1
              and "正文二保留在第二部分。" in texts,
              "重复指令按出现顺序原位替换")
        check("这是明确锚定在结果表之后的插入模块。" in texts,
              "插入模块按 anchor_task_id 放到表格之后")
        check("（本模块内容待补充）" in texts, "缺失模块进入占位位置")
        check(not any(text.startswith("【") for text in texts),
              "报告正文不残留模板编写指令")
        check(len(doc.tables) >= 2, "模板原有表格和生成表格同时保留")
        check(len(doc.inline_shapes) >= 2, "模板原有图片和生成图片同时保留")
        check("bookmarkStart" in doc.element.xml, "模板书签结构保留")
        check("TOC" in doc.element.xml, "模板目录域结构保留")

        # 无锚点插入必须阻止，防止随机插入到最近节点。
        inserted_node = next(node for node in manifest["nodes"] if node["title"] == "插入模块")
        inserted_node["anchor_task_id"] = None
        try:
            assemble.assemble_template_docx(
                workdir / manifest["template"], manifest, workdir / "out" / "blocked.docx"
            )
        except assemble.TemplateAssemblyError as exc:
            check(bool(str(exc)), f"没有唯一锚点的插入模块被阻止（{exc}）")
        else:
            check(False, "没有唯一锚点的插入模块不应被放行")
    finally:
        delete_tasks_cascade(db, [task.id for task in tasks] + [inserted.id])
        db.query(ProjectFile).filter(ProjectFile.project_id == pid).delete()
        db.query(Project).filter(Project.id == pid).delete()
        db.commit()
        storage.remove(template_path)
        storage.remove(image_path)
        db.close()

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        env.close()
