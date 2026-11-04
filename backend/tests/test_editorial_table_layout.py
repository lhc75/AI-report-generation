"""对比表展示压缩：只压缩定位列的重复内容，绝不删除统计单元格。"""
import copy

import pytest

from app.report_editorial import ReportEditorialFormatter


def comparison_table(width=11):
    if width == 11:
        columns = ["教师题号", "教师调查内容", "主管部门题号", "主管部门调查内容", "选项",
                   "教师选择记录数", "教师占比", "主管部门选择记录数", "主管部门占比",
                   "教师本题作答记录数", "主管部门本题作答记录数"]
        rows = [["第十九题", "学校本土资源挖掘利用", "第十三题", "辖区本土资源挖掘与融入思政教育",
                 "挖掘利用充分，常态化融入思政课堂与实践活动", 108, "28.0%", 488, "47.5%", 386, 1027],
                ["第十九题", "学校本土资源挖掘利用", "第十三题", "辖区本土资源挖掘与融入思政教育",
                 "挖掘利用较好，能够结合教学开展相关资源运用", 148, "38.3%", 357, "34.8%", 386, 1027]]
    else:
        columns = ["教师题号", "主管部门题号", "调查内容", "选项", "教师选择记录数", "教师勾选率",
                   "主管部门选择记录数", "主管部门勾选率", "教师本题作答记录数", "主管部门本题作答记录数"]
        rows = [["第20题", "第14题", "系统梳理本土红色文化资源", "系统梳理整合本土红色文化资源",
                 226, "58.5%", 666, "64.8%", 386, 1027],
                ["第20题", "第14题", "融入课堂", "推动本土资源有机融入思政课堂",
                 223, "57.8%", 533, "51.9%", 386, 1027]]
    return {"columns": columns, "rows": rows, "notes": ["两类问卷分别报告。"]}


def test_eleven_columns_move_full_repeated_context_but_keep_statistics():
    source = comparison_table()
    original = copy.deepcopy(source)
    shown = ReportEditorialFormatter([]).table(source)
    assert source == original
    assert shown["columns"] == ["选项", "教师记录数", "教师占比", "主管部门记录数", "主管部门占比",
                                "教师作答数", "主管部门作答数"]
    assert shown["rows"] == [row[4:] for row in original["rows"]]
    assert shown["notes"] == [
        "题目：教师问卷第十九题（学校本土资源挖掘利用）；教育主管部门问卷第十三题（辖区本土资源挖掘与融入思政教育）。",
        "两类问卷分别报告。"]


def test_ten_columns_keep_varying_topics_and_move_only_constant_question_locators():
    source = comparison_table(10)
    original = copy.deepcopy(source)
    shown = ReportEditorialFormatter([]).table(source)
    assert source == original
    assert shown["columns"] == ["调查内容", "选项", "教师记录数", "教师勾选率", "主管部门记录数",
                                "主管部门勾选率", "教师作答数", "主管部门作答数"]
    assert shown["rows"] == [row[2:] for row in original["rows"]]
    assert shown["rows"][0][0] != shown["rows"][1][0]
    assert shown["notes"][0] == "题目：教师问卷第20题；教育主管部门问卷第14题。"


def test_actual_education_authority_header_aliases_match_the_reviewed_schema():
    source = comparison_table()
    source["columns"] = [column.replace("主管部门", "教育主管部门") for column in source["columns"]]
    shown = ReportEditorialFormatter([]).table(source)
    assert len(shown["columns"]) == 7
    assert shown["rows"] == [row[4:] for row in source["rows"]]


def test_nested_table_compacts_outer_and_inner_together():
    table = comparison_table()
    source = {"table": table}
    original = copy.deepcopy(source)
    shown = ReportEditorialFormatter([]).table(source)
    assert source == original
    assert len(shown["table"]["columns"]) == 7
    assert shown["table"]["rows"] == [row[4:] for row in original["table"]["rows"]]


@pytest.mark.parametrize("unsafe_change", [
    "unknown_schema", "varying_question", "varying_topic", "short_row", "empty_rows",
    "empty_locator", "numeric_locator", "unknown_note_shape",
])
def test_ambiguous_schema_or_locator_never_discards_cells(unsafe_change):
    source = comparison_table()
    if unsafe_change == "unknown_schema":
        source["columns"][6] = "教师满意度评分"
    elif unsafe_change == "varying_question":
        source["rows"][1][0] = "第二十题"
    elif unsafe_change == "varying_topic":
        source["rows"][1][1] = "不同调查内容"
    elif unsafe_change == "short_row":
        source["rows"][1].pop()
    elif unsafe_change == "empty_rows":
        source["rows"] = []
    elif unsafe_change == "empty_locator":
        for row in source["rows"]:
            row[1] = ""
    elif unsafe_change == "numeric_locator":
        for row in source["rows"]:
            row[0] = 19
    else:
        source["notes"] = {"unexpected": "保留说明"}
    original = copy.deepcopy(source)
    shown = ReportEditorialFormatter([]).table(source)
    assert source == original
    assert shown["columns"] == original["columns"]
    assert shown["rows"] == original["rows"]
    assert shown["notes"] == original["notes"]


def test_ten_columns_with_different_question_locators_remain_uncompressed():
    source = comparison_table(10)
    source["rows"][1][1] = "第15题"
    shown = ReportEditorialFormatter([]).table(source)
    assert shown["rows"] == source["rows"]
    assert shown["columns"] == source["columns"]


@pytest.mark.parametrize("placeholder", [
    "其他（请简要填写：）；其他（请简要填写：）",
    "其他（请简要填写：                ）；其他（请简要填写：                  ）",
    "其他（请简要填写：\t　）；其他（请简要填写：　）",
])
def test_only_unfilled_duplicated_other_option_is_simplified_on_display(placeholder):
    source = {"columns": ["调查内容", "选项", "选择记录数"],
              "rows": [["其他", placeholder, 18]]}
    original = copy.deepcopy(source)
    shown = ReportEditorialFormatter([]).table(source)
    assert source == original
    assert shown["rows"] == [["其他", "其他", 18]]


@pytest.mark.parametrize("option", [
    "其他（请简要填写：资源库）；其他（请简要填写：）",
    "其他（请简要填写：）；其他（请简要填写：校际合作）",
    "其他（请简要填写：空白）；其他（请简要填写：）",
    "其他（请简要填写：）",
    "其他（请简要填写：）；其他（请简要填写：）；其他（请简要填写：）",
    "其他（请简要填写：），其他（请简要填写：）",
])
def test_real_open_answers_or_different_literal_options_are_never_erased(option):
    source = {"columns": ["选项", "记录数"], "rows": [[option, 1]]}
    assert ReportEditorialFormatter([]).table(source)["rows"] == source["rows"]


def test_empty_other_placeholder_in_non_option_column_is_preserved():
    placeholder = "其他（请简要填写：）；其他（请简要填写：）"
    source = {"columns": ["开放回答", "记录数"], "rows": [[placeholder, 1]]}
    assert ReportEditorialFormatter([]).table(source) == source
