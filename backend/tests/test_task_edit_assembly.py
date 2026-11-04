"""编辑任务后仍填入原模板位置，且标题改名只改变指定标题。"""
import json

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import RGBColor

from app import assemble, template_parse


def red_instruction(doc, text):
    paragraph = doc.add_paragraph()
    paragraph.add_run(text).font.color.rgb = RGBColor(255, 0, 0)
    return paragraph


def template_manifest(tmp_path, doc, *, ai_tasks=None):
    template = tmp_path / "template.docx"
    doc.save(template)
    blocks = template_parse.extract_structure(template.read_bytes())
    tasks = (
        template_parse.tasks_from_ai_result(blocks, ai_tasks)
        if ai_tasks is not None else template_parse.tasks_from_red_marks(blocks)
    )
    contents = tmp_path / "contents"
    contents.mkdir()
    nodes = []
    for task in tasks:
        node = {
            "id": task["tmp_id"] + 1,
            "parent_id": None if task["parent_tmp"] is None else task["parent_tmp"] + 1,
            "title": task["title"], "type": task["task_type"],
            "level": task["level"], "instruction": task["instruction"],
            "template_title": task["title"], "template_instruction": task["instruction"],
            "slot_key": task["slot_key"], "anchor_task_id": None, "sort": task["sort"],
        }
        if node["type"] != "heading":
            write_content(tmp_path, node, f"原位置 {node['id']} 的新正文")
        nodes.append(node)
    return template, {"nodes": nodes, "signature": assemble._doc_signature(doc)}


def write_content(tmp_path, node, content):
    suffix = "json" if node["type"] == "table" else "txt"
    path = tmp_path / "contents" / f"{node['id']}.{suffix}"
    path.write_text(json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else content,
                    encoding="utf-8")
    node["file"] = str(path.relative_to(tmp_path))


def finish(tmp_path, template, manifest):
    output = tmp_path / "out" / "report.docx"
    assemble.assemble_template_docx(template, manifest, output)
    assert assemble.make_report_validator(manifest)("ok", tmp_path)[0]
    return Document(output)


def test_changed_instruction_keeps_original_red_template_slot(tmp_path):
    doc = Document()
    doc.add_heading("调查结果", 1)
    red_instruction(doc, "【填写：统计调查结果并说明原始口径】")
    doc.add_paragraph("不可改动的模板原正文")
    template, manifest = template_manifest(tmp_path, doc)
    task = manifest["nodes"][1]
    task["instruction"] = "【填写：用新的叙述角度解释这次调查结果】"
    task["title"] = task["instruction"]
    result = finish(tmp_path, template, manifest)
    assert [p.text for p in result.paragraphs] == ["调查结果", "原位置 2 的新正文", "不可改动的模板原正文"]


def test_changed_type_and_repeated_instructions_stay_in_individual_slots(tmp_path):
    doc = Document()
    doc.add_heading("调查结果", 1)
    red_instruction(doc, "【填写：相同指令的统计内容】")
    doc.add_paragraph("两个模块之间的原文")
    red_instruction(doc, "【填写：相同指令的统计内容】")
    template, manifest = template_manifest(tmp_path, doc)
    first, second = manifest["nodes"][1:]
    first["type"] = "table"
    first["instruction"] = "【生成表格：调查结果】"
    write_content(tmp_path, first, {"columns": ["选项", "人数"], "rows": [["甲", 20]]})
    second["instruction"] = "【填写：另一种说明】"
    result = finish(tmp_path, template, manifest)
    assert len(result.tables) == 1
    middle = next(p for p in result.paragraphs if p.text == "两个模块之间的原文")
    second_paragraph = next(p for p in result.paragraphs if p.text == "原位置 3 的新正文")
    body = result.element.body
    assert body.index(result.tables[0]._tbl) < body.index(middle._p) < body.index(second_paragraph._p)


def test_renamed_heading_retains_nested_and_duplicate_subsection_slots(tmp_path):
    doc = Document()
    for title in ("第一部分", "第二部分"):
        doc.add_heading(title, 1)
        doc.add_heading("一、相同名称", 2)
        red_instruction(doc, "【填写：相同指令的统计内容】")
    template, manifest = template_manifest(tmp_path, doc)
    headings = [node for node in manifest["nodes"] if node["type"] == "heading"]
    headings[0]["title"] = "重命名后的第一部分"
    headings[1]["title"] = "新的小节名称"
    result = finish(tmp_path, template, manifest)
    assert [p.text for p in result.paragraphs] == [
        "重命名后的第一部分", "新的小节名称", "原位置 3 的新正文",
        "第二部分", "一、相同名称", "原位置 6 的新正文",
    ]


def test_heading_rename_preserves_style_run_formatting_and_bookmarks(tmp_path):
    doc = Document()
    heading = doc.add_heading("", 1)
    heading.add_run("原标").bold = True
    heading.add_run("题").italic = True
    bookmark = OxmlElement("w:bookmarkStart")
    bookmark.set(qn("w:id"), "12")
    bookmark.set(qn("w:name"), "original_heading")
    heading._p.insert(0, bookmark)
    ending = OxmlElement("w:bookmarkEnd")
    ending.set(qn("w:id"), "12")
    heading._p.append(ending)
    red_instruction(doc, "【填写：完整的原始模板指令】")
    template, manifest = template_manifest(tmp_path, doc)
    manifest["nodes"][0]["title"] = "新的标题"
    result = finish(tmp_path, template, manifest)
    assert result.paragraphs[0].text == "新的标题"
    assert result.paragraphs[0].style.name == "Heading 1"
    assert result.paragraphs[0].runs[0].bold
    assert result.paragraphs[0].runs[1].italic
    assert assemble._doc_signature(result)["bookmark_names"] == ["original_heading"]


def test_plain_front_section_heading_and_child_instruction_can_be_edited(tmp_path):
    doc = Document()
    doc.add_paragraph("说明")
    front_instruction = "【填写：说明本次调查的范围与时间安排】"
    red_instruction(doc, front_instruction)
    doc.add_heading("正文", 1)
    body_instruction = "【填写：归纳主要调查结果与原因】"
    red_instruction(doc, body_instruction)
    template, manifest = template_manifest(tmp_path, doc, ai_tasks=[
        {"heading": "说明", "instructions": [{"type": "text", "instruction": front_instruction}]},
        {"heading": "正文", "instructions": [{"type": "text", "instruction": body_instruction}]},
    ])
    manifest["nodes"][0]["title"] = "调查说明"
    manifest["nodes"][1]["instruction"] = "【填写：最新的调查说明】"
    result = finish(tmp_path, template, manifest)
    assert result.paragraphs[0].text == "调查说明"
    assert result.paragraphs[0].style.name == "Normal"
    assert "原位置 2 的新正文" in [p.text for p in result.paragraphs]


def test_legacy_blank_slots_can_use_preserved_original_locator_fields(tmp_path):
    doc = Document()
    doc.add_heading("原始章节", 1)
    red_instruction(doc, "【填写：原始指令完整说明】")
    template, manifest = template_manifest(tmp_path, doc)
    for node in manifest["nodes"]:
        node["slot_key"] = "" if node["type"] == "heading" else "legacy-content"
    manifest["nodes"][0]["title"] = "修改后的章节"
    manifest["nodes"][1]["instruction"] = "【填写：更新后的不同指令】"
    result = finish(tmp_path, template, manifest)
    assert result.paragraphs[0].text == "修改后的章节"
    assert "原位置 2 的新正文" in [p.text for p in result.paragraphs]


def test_inserted_sibling_and_chain_after_edited_table_keep_order(tmp_path):
    doc = Document()
    doc.add_heading("原章节", 1)
    red_instruction(doc, "【填写：完整的原始模板指令】")
    doc.add_paragraph("模块后面的模板正文")
    template, manifest = template_manifest(tmp_path, doc)
    original = manifest["nodes"][1]
    original["type"] = "table"
    original["instruction"] = "【生成表格：编辑后的模块】"
    write_content(tmp_path, original, {"columns": ["结果"], "rows": [["原模块"]]})
    for identifier, anchor, sort in ((5, 2, 20), (4, 3, 2), (3, 2, 10)):
        child = {"id": identifier, "parent_id": 1, "title": "插入模块", "type": "text", "level": 2,
                 "instruction": "插入指令", "anchor_task_id": anchor, "sort": sort}
        write_content(tmp_path, child, f"插入内容 {identifier}")
        manifest["nodes"].append(child)
    result = finish(tmp_path, template, manifest)
    texts = [p.text for p in result.paragraphs]
    assert texts.index("插入内容 3") < texts.index("插入内容 4") < texts.index("插入内容 5") < texts.index("模块后面的模板正文")


def test_validator_requires_exact_edited_heading_and_preserved_body(tmp_path):
    doc = Document()
    doc.add_heading("原章节", 1)
    doc.add_paragraph("必须保留的原正文")
    red_instruction(doc, "【填写：完整的原始模板指令】")
    template, manifest = template_manifest(tmp_path, doc)
    manifest["nodes"][0]["title"] = "编辑后的章节"
    result = finish(tmp_path, template, manifest)
    result.paragraphs[0].text = "原章节"
    result.save(tmp_path / "out" / "report.docx")
    ok, reason, _ = assemble.make_report_validator(manifest)("ok", tmp_path)
    assert not ok and "编辑后的章节" in reason


def test_missing_original_heading_locator_cannot_silently_ignore_rename(tmp_path):
    doc = Document()
    doc.add_heading("原章节", 1)
    red_instruction(doc, "【填写：完整的原始模板指令】")
    template, manifest = template_manifest(tmp_path, doc)
    heading = manifest["nodes"][0]
    heading.update(title="新章节", template_title="不存在的章节", slot_key="unknown")
    with pytest.raises(assemble.TemplateAssemblyError, match="无法定位原模板"):
        assemble.assemble_template_docx(template, manifest, tmp_path / "out" / "report.docx")
