"""窄页多列表必须保留完整人数与百分比 token，不能靠缩改数据排版。"""
import copy
import json

import pytest
from docx import Document
from docx.shared import Inches, Pt

from app import assemble


def paired_table():
    return {
        "title": "本土资源挖掘与利用情况",
        "columns": ["教师问卷原题号", "教师题目短主题", "教育主管部门问卷原题号",
                    "教育主管部门题目短主题", "选项原文", "教师频次（作答记录）",
                    "教师题内占比", "教育主管部门频次（作答记录）", "教育主管部门题内占比",
                    "教师该题作答记录数", "教育主管部门该题作答记录数"],
        "rows": [["第十九题", "学校本土资源挖掘利用", "第十三题", "辖区本土资源挖掘与融入思政教育",
                  "挖掘利用充分，常态化融入思政课堂与实践活动", 108, "28.0%", 488, "47.5%", 386, 1027],
                 ["第十九题", "学校本土资源挖掘利用", "第十三题", "辖区本土资源挖掘与融入思政教育",
                  "挖掘利用较好，能够结合教学开展相关资源运用", 148, "38.3%", 357, "34.8%", 386, 1027]],
    }


def test_actual_paired_numeric_tokens_have_room_without_expanding_page():
    payload = paired_table()
    before = copy.deepcopy(payload)
    available = int(Inches(5.768))
    widths = assemble._table_column_widths(payload["columns"], payload["rows"], available)
    assert sum(widths) == available
    # 旧按表头权重分配时，38.3%只有约30pt，百分号会另起一行。
    assert widths[6] > Pt(40) and widths[8] > Pt(40)
    assert widths[10] >= Pt(33.8)  # 1027必须完整显示，含默认左右边距。
    assert widths[4] > widths[5]  # 剩余空间继续优先长选项原文。
    assert payload == before


def test_numeric_column_is_sized_from_data_not_a_longer_header():
    payload = paired_table()
    widths = assemble._table_column_widths(payload["columns"], payload["rows"], Inches(5.768))
    payload["columns"][6] += "（仅以该题全部非空作答记录为分母计算）" * 5
    changed = assemble._table_column_widths(payload["columns"], payload["rows"], Inches(5.768))
    assert changed == widths


@pytest.mark.parametrize("values", [[1, "待核实"], ["1—2次", "每月一次"], [None, ""]])
def test_text_or_missing_columns_are_not_treated_as_numeric(values):
    assert assemble._numeric_column_min_width(values) is None


def test_long_signed_grouped_numbers_fullwidth_percent_and_all_numeric_table():
    columns = ["记录数", "变化", "比例"]
    rows = [["1,234,567", "-12.75", "100.0％"], [None, "+3.25", "0.0%"]]
    available = Inches(5.768)
    widths = assemble._table_column_widths(columns, rows, available)
    assert sum(widths) == available
    assert all(width > Pt(50) for width in widths)
    assert assemble._numeric_column_min_width(["100.0％"]) > assemble._numeric_column_min_width(["100.0%"])


def test_impossible_numeric_tokens_fail_explicitly_instead_of_overflowing():
    with pytest.raises(assemble.TemplateAssemblyError, match="完整显示数值"):
        assemble._table_column_widths(["记录数", "比例"], [["1234567890123456789", "100.0%"]], Inches(0.5))


def test_rendered_ooxml_keeps_body_tokens_and_page_width(tmp_path):
    payload = paired_table()
    doc = Document()
    section = doc.sections[0]
    section.left_margin = section.right_margin = Inches(1)
    section.page_width = Inches(7.768)
    anchor = doc.add_paragraph("【表格】")
    assemble._insert_table_after(doc, anchor._p, json.dumps(payload, ensure_ascii=False))
    output = tmp_path / "paired.docx"
    doc.save(output)
    report = Document(output)
    table = report.tables[0]
    assert [cell.text for cell in table.rows[1].cells] == [str(value) for value in payload["rows"][0]]
    assert table.cell(1, 6).text == "28.0%"
    assert table.cell(1, 10).text == "1027"
    assert sum(column.width for column in table.columns) <= section.page_width - section.left_margin - section.right_margin + Pt(0.1)
