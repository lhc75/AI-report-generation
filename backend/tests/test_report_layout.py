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
