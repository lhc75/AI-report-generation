"""导出排版回归：封面说明、按实际分节设置表格和自然图表分页。"""
import json

import pytest
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml.ns import qn
from docx.shared import Inches, Pt

from app import assemble


@pytest.mark.parametrize("cover,boundary", [
    (False, None), (True, None), (True, "page"), (True, "section"),
])
def test_front_explanation_stays_with_body_without_duplicate_page_break(tmp_path, cover, boundary):
    doc = Document()
    if cover:
        doc.add_paragraph("调查分析报告")
        doc.add_paragraph("某大学研究院")
        doc.add_paragraph("2025年12月")
    if boundary == "page":
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    elif boundary == "section":
        doc.add_section(WD_SECTION.NEW_PAGE)
    doc.add_paragraph("")
    heading = doc.add_paragraph("说 明")
    slot = doc.add_paragraph("【撰写文本：说明报告范围】")
    doc.add_heading("调查结果", 1)
    # 正文同名词语不能被当作前置标题重新分页。
    doc.add_paragraph("说明")
    template = tmp_path / "template.docx"
    doc.save(template)
    (tmp_path / "text.txt").write_text("本报告分析提交记录，并保留统计范围和限制。", encoding="utf-8")
    manifest = {"nodes": [
        {"id": 1, "type": "heading", "title": heading.text, "level": 1},
        {"id": 2, "type": "text", "title": "说明正文", "parent_id": 1, "level": 2,
         "instruction": slot.text, "slot_key": "legacy-slot", "file": "text.txt"},
        {"id": 3, "type": "heading", "title": "调查结果", "level": 1},
    ]}
    output = tmp_path / "out/report.docx"
    result = assemble.assemble_template_docx(template, manifest, output)
    report = Document(output)
    explanation = next(p for p in report.paragraphs if p.text == "说 明")
    assert explanation.paragraph_format.keep_with_next
    assert explanation.paragraph_format.keep_together
    assert bool(explanation.paragraph_format.page_break_before) is (cover and boundary is None)
    body_same_word = next(p for p in report.paragraphs if p.text == "说明")
    assert body_same_word.paragraph_format.page_break_before is None
    assert result["before"]["geometry"] == result["after"]["geometry"]
    assert assemble.make_report_validator(manifest)("ok", tmp_path)[0]


def test_table_uses_anchor_geometry_content_widths_and_safe_row_pagination(tmp_path):
    doc = Document()
    doc.sections[0].page_width = Inches(10)
    doc.add_heading("前节", 1)
    section = doc.add_section(WD_SECTION.NEW_PAGE)
    section.page_width, section.page_height = Inches(6), Inches(7)
    section.left_margin = section.right_margin = Inches(0.5)
    section.top_margin = section.bottom_margin = Inches(0.5)
    doc.add_heading("调查结果", 1)
    slot = doc.add_paragraph("【生成表格：问卷概况】")
    section = doc.add_section(WD_SECTION.NEW_PAGE)
    section.page_width = Inches(12)
    doc.add_paragraph("末节原正文")
    template = tmp_path / "template.docx"
    doc.save(template)
    regular = "保留题号映射、主题范围和解释限制，中文描述需要合理的列宽。" * 4
    oversized = "超长记录仍应允许跨页继续，不能截断或丢失。" * 250
    payload = {"title": "表1 问卷概况", "columns": ["对象", "题数", "对应题号", "说明"],
               "rows": [["教师", 22, regular, "统计单位为提交记录。"],
                        ["管理人员", 16, oversized, "长记录续页。"]],
               "note": "不改变任何内容与数字。"}
    (tmp_path / "table.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    manifest = {"nodes": [
        {"id": 1, "type": "heading", "title": "前节", "level": 1},
        {"id": 2, "type": "heading", "title": "调查结果", "level": 1},
        {"id": 3, "type": "table", "title": "问卷概况", "parent_id": 2, "level": 2,
         "instruction": slot.text, "slot_key": "legacy-slot", "file": "table.json"},
    ]}
    output = tmp_path / "out/report.docx"
    result = assemble.assemble_template_docx(template, manifest, output)
    report = Document(output)
    table = report.tables[0]
    widths = [column.width for column in table.columns]
    assert abs(sum(widths) - Inches(5)) < Pt(1)
    assert widths[2] > widths[1]
    assert table.autofit is False
    assert table.rows[0]._tr.trPr.find(qn("w:tblHeader")) is not None
    assert table.rows[0]._tr.trPr.find(qn("w:cantSplit")) is not None
    assert table.rows[1]._tr.trPr.find(qn("w:cantSplit")) is not None
    assert table.rows[2]._tr.trPr is None or table.rows[2]._tr.trPr.find(qn("w:cantSplit")) is None
    assert table.cell(1, 2).text == regular and table.cell(2, 2).text == oversized
    assert table.cell(1, 2).paragraphs[0].alignment == WD_ALIGN_PARAGRAPH.LEFT
    assert table.cell(1, 1).paragraphs[0].alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert table.cell(1, 2).paragraphs[0].runs[0].font.size == Pt(10.5)
    assert table.cell(1, 2).paragraphs[0]._p.pPr.find(qn("w:snapToGrid")).get(qn("w:val")) == "false"
    assert all(not p.paragraph_format.keep_with_next for row in table.rows[1:] for cell in row.cells for p in cell.paragraphs)
    title = next(p for p in report.paragraphs if p.text == payload["title"])
    assert title.paragraph_format.keep_with_next
    assert result["before"]["geometry"] == result["after"]["geometry"]
    assert assemble.make_report_validator(manifest)("ok", tmp_path)[0]


def test_markdown_table_in_text_is_rendered_as_native_word_table(tmp_path):
    doc = Document()
    slot = doc.add_paragraph("【填写：说明报告范围】")
    doc.add_paragraph("后续正文")
    template = tmp_path / "template.docx"
    doc.save(template)
    text = (
        "本报告说明调查范围，组成见表2。\n\n"
        "表2　各类问卷提交记录构成（单位：条）\n\n"
        "| 统计项目 | 小学生 | 中学生 |\n"
        "|---|---:|---:|\n"
        "| 提交记录数 | 3,646 | 5,435 |\n\n"
        "注：提交记录不等同于独立受访人数。"
    )
    (tmp_path / "text.txt").write_text(text, encoding="utf-8")
    manifest = {"nodes": [
        {"id": 1, "type": "text", "title": "说明", "level": 1,
         "instruction": slot.text, "slot_key": "legacy-slot", "file": "text.txt"},
    ]}
    output = tmp_path / "out/report.docx"
    assemble.assemble_template_docx(template, manifest, output)
    report = Document(output)
    assert len(report.tables) == 1
    assert [cell.text for cell in report.tables[0].rows[0].cells] == ["统计项目", "小学生", "中学生"]
    assert [cell.text for cell in report.tables[0].rows[1].cells] == ["提交记录数", "3,646", "5,435"]
    assert all("|" not in paragraph.text for paragraph in report.paragraphs)
    assert assemble.make_report_validator(manifest)("ok", tmp_path)[0]


def test_display_table_numbers_follow_actual_order(tmp_path):
    (tmp_path / "front.txt").write_text(
        "六类问卷构成见表2。\n\n表2　问卷构成\n\n"
        "| 对象 | 记录数 |\n|---|---:|\n| 小学生 | 3646 |\n",
        encoding="utf-8",
    )
    (tmp_path / "question.json").write_text(
        json.dumps({"title": "表1 问卷内容概览", "columns": ["对象"], "rows": [["小学生"]],
                    "note": "本表见表1。"},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "body.txt").write_text("问卷索引见表1。", encoding="utf-8")
    nodes = [
        {"id": 1, "type": "text", "file": "front.txt"},
        {"id": 2, "type": "table", "file": "question.json"},
        {"id": 3, "type": "text", "file": "body.txt"},
    ]
    assemble._renumber_display_tables(tmp_path, nodes)
    front = (tmp_path / "front.txt").read_text(encoding="utf-8")
    body = (tmp_path / "body.txt").read_text(encoding="utf-8")
    question = json.loads((tmp_path / "question.json").read_text(encoding="utf-8"))
    assert "构成见表1。" in front
    assert "表1　问卷构成" in front
    assert question["title"].startswith("表2 ")
    assert question["note"] == "本表见表2。"
    assert body == "问卷索引见表2。"


def test_embedded_markdown_table_duplicate_is_removed_when_table_task_exists(tmp_path):
    task_table = {
        "title": "表2 地区分布",
        "columns": ["地区", "大学生", "小学生", "合计"],
        "rows": [["贵阳", "6770", "936", "7706"], ["安顺", "597", "818", "1415"]],
    }
    (tmp_path / "table.json").write_text(json.dumps(task_table, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "text.txt").write_text(
        "地区情况见表2。\n\n表2 地区分布\n\n"
        "| 地区 | 小学生 | 大学生 | 合计 |\n|---|---:|---:|---:|\n"
        "| 安顺 | 818 | 597 | 1415 |\n"
        "| 贵阳 | 936 | 6770 | 7706 |\n\n"
        "正文继续说明地区差异。",
        encoding="utf-8",
    )
    nodes = [
        {"id": 1, "type": "table", "file": "table.json"},
        {"id": 2, "type": "text", "file": "text.txt"},
    ]
    assemble._dedupe_embedded_tables(tmp_path, nodes)
    text = (tmp_path / "text.txt").read_text(encoding="utf-8")
    assert "| 地区 |" not in text
    assert "表2 地区分布" not in text
    assert text.startswith("地区情况见表2。")
    assert "正文继续说明地区差异。" in text
