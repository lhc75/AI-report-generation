"""Reader-facing source names must not alter categories or statistical evidence."""
from copy import deepcopy
import re

import pytest

from app.report_text import SourceTextFormatter


SOURCE = "survey-submissions-5.csv"


@pytest.fixture
def formatter():
    return SourceTextFormatter([SOURCE])


@pytest.mark.parametrize("source", [
    SOURCE + '工作表“CSV”',
    '“' + SOURCE + '”的工作表＂csv＂',
    '《' + SOURCE + '》工作表：CSV',
    "'" + SOURCE + "' 的 'CSV' 工作表",
    SOURCE + '（工作表“CSV”）',
    SOURCE + '(CSV)',
    SOURCE + ' “CSV”',
    SOURCE + ' CSV',
])
def test_known_file_and_worksheet_become_one_natural_source(formatter, source):
    original = f"样本统计使用{source}，共有2054条提交记录，支持320条，占15.6%。"
    assert formatter.text(original) == "样本统计使用调查问卷数据，共有2054条提交记录，支持320条，占15.6%。"


def test_source_filename_digits_are_removed_without_changing_statistics(formatter):
    original = f"来源：{SOURCE}工作表“CSV”。中学237条，支持48条，占20.3%；分母237。"
    display = formatter.text(original)
    assert SOURCE not in display and "CSV" not in display
    assert re.findall(r"\d+(?:\.\d+)?%?", display) == ["237", "48", "20.3%", "237"]


@pytest.mark.parametrize("description,expected", [
    ("CSV字段对应数据", "数据字段对应数据"),
    ("CSV数据包含237条", "问卷数据包含237条"),
])
def test_filename_does_not_consume_csv_from_following_description(formatter, description, expected):
    assert formatter.text(SOURCE + " " + description) == "调查问卷数据 " + expected


@pytest.mark.parametrize("raw,expected", [
    ("CSV字段、csv对应、Csv数据", "数据字段、数据字段对应、问卷数据"),
    ("原始 CSV 数据文件", "原始问卷作答数据"),
    ("CSV数据文件", "问卷作答数据"),
    ("工作表＂CSV＂、CSV统计结果", "工作表＂问卷数据＂、问卷数据统计结果"),
    ("CSVStore、ACSV、csv_store、csv2、unknown.csv、prefix-CSV", "CSVStore、ACSV、csv_store、csv2、unknown.csv、prefix-CSV"),
])
def test_narrative_normalizes_descriptions_without_matching_identifiers(formatter, raw, expected):
    assert formatter.text(raw) == expected


def test_reference_to_previous_csv_description_stays_natural_and_local(formatter):
    original = ("分析资料包括上述问卷文本及原始CSV数据文件中的提交记录。"
                "该文件共有2054条提交记录，未经有效性筛选。"
                "另有问卷.docx。该文件包括12题。")
    assert formatter.text(original) == (
        "分析资料包括上述问卷文本及原始问卷作答数据中的提交记录。"
        "所提供数据共有2054条提交记录，未经有效性筛选。"
        "另有问卷.docx。该文件包括12题。"
    )


def test_multiple_sources_are_distinct_and_literal_without_substring_collisions():
    formatter = SourceTextFormatter(["survey.csv", "long-survey.csv", "Survey.csv", "survey.csv", "notes.docx"])
    assert formatter.text("survey.csv、long-survey.csv、Survey.csv") == (
        "调查问卷数据（一）、调查问卷数据（二）、调查问卷数据（三）"
    )
    assert formatter.text("longer-survey.csv、SURVEY.csv、survey.csv.backup") == (
        "longer-survey.csv、SURVEY.csv、survey.csv.backup"
    )


def test_chinese_aliases_do_not_create_numeric_statistics():
    names = [f"source-{index}.csv" for index in range(1, 13)]
    formatter = SourceTextFormatter(names)
    assert formatter.text(names[-1]) == "调查问卷数据（十二）"
    assert not re.search(r"\d", formatter.text("、".join(names)))


@pytest.mark.parametrize("path", [
    "/Users/mac/Documents/survey-submissions-5.csv",
    "data/survey-submissions-5.csv",
    "./survey-submissions-5.csv",
    "../CSV/survey-submissions-5.csv",
    r"C:\input\CSV\survey-submissions-5.csv",
    r"\\server\share\survey-submissions-5.csv",
    "https://example.test/CSV/survey-submissions-5.csv?sheet=CSV&count=237",
    "file:///Users/mac/survey-submissions-5.csv",
    '"/Users/mac/My Data/survey-submissions-5.csv"',
    '“/Users/mac/问卷 数据/survey-submissions-5.csv”',
    r'"C:\My Data\survey-submissions-5.csv"',
])
def test_real_paths_are_preserved_even_with_known_basename(formatter, path):
    original = f"路径：{path}；来源：{SOURCE}。CSV字段。"
    assert formatter.text(original) == f"路径：{path}；来源：调查问卷数据。数据字段。"


def test_table_display_copy_preserves_all_numbers_bindings_and_unknown_fields(formatter):
    payload = {
        "columns": ["CSV", "CSV字段", "人数", "比例"],
        "rows": [["CSV", "CSV对应：原件第20题→q21", 48, "20.3%"],
                 ["CSVStore", "ACSV", 0, "0.0%"]],
        "title": "CSV统计结果", "note": f"来源：{SOURCE}工作表“CSV”，分母237。",
        "notes": ["CSV数据按原始作答记录计算。"], "caption": "CSV数据表",
        "source": SOURCE,
        "cell_evidence": {"r0c2": ["ev-48"], "r0c3": ["ev-48"]},
        "cell_metrics": {"r0c2": "count", "r0c3": "percent"},
        "evidence_ids": [SOURCE, "CSV"],
        "claims": [{"text": "CSV字段", "statistics": [{"token": "48", "metric": "count"}]}],
        "method": {"note": "CSV数据"},
        "unknown": {"title": "CSV数据", "path": "data/" + SOURCE},
    }
    original = deepcopy(payload)
    display = formatter.table(payload)
    assert payload == original and display is not payload
    assert display["columns"] == ["CSV", "数据字段", "人数", "比例"]
    assert display["rows"] == [["CSV", "数据字段对应：原件第20题→q21", 48, "20.3%"],
                              ["CSVStore", "ACSV", 0, "0.0%"]]
    assert display["note"] == "来源：调查问卷数据，分母237。"
    assert display["notes"] == ["问卷数据按原始作答记录计算。"]
    assert display["title"] == "问卷数据统计结果"
    assert display["source"] == "调查问卷数据"
    assert display["caption"] == "问卷数据表"
    for key in ("cell_evidence", "cell_metrics", "evidence_ids", "claims", "method", "unknown"):
        assert display[key] == original[key]
        assert display[key] is not payload[key]


def test_wrapped_table_converts_only_declared_display_fields(formatter):
    payload = {"table": {"columns": ["来源"], "rows": [[SOURCE]], "note": "CSV字段"},
               "note": "CSV数据", "evidence": {"note": "CSV数据", "file_name": SOURCE}}
    display = formatter.table(payload)
    assert display["table"]["rows"] == [["调查问卷数据"]]
    assert display["table"]["note"] == "数据字段"
    assert display["note"] == "问卷数据"
    assert display["evidence"] == payload["evidence"]
    assert payload["table"]["rows"] == [[SOURCE]]


def test_group_chart_copy_retains_values_and_metadata_and_raw_categories(formatter):
    child = {
        "title": "CSV统计结果", "labels": ["CSV", "CSVStore", "ACSV", SOURCE, "CSV对应：q1"],
        "values": [48, 0, 1, 2, 3], "metric": "count", "denominator": 237,
        "value_evidence": ["ev48", "ev0", "ev1", "ev2", "ev3"],
        "evidence_ids": [SOURCE, "CSV"], "source": SOURCE, "note": "CSV数据，作答237条。",
        "x_label": "CSV字段", "y_label": "数量", "caption": "CSV图", "notes": ["CSV对应：q1。"],
        "method": {"title": "CSV数据"}, "unknown": {"labels": ["CSV字段"]},
        "claims": [{"text": "CSV数据", "statistics": [{"token": "48", "evidence_id": "ev48"}]}],
    }
    payload = {"charts": [child], "note": f"数据来源：{SOURCE}。", "extra": deepcopy(child)}
    original = deepcopy(payload)
    display = formatter.chart(payload)
    assert payload == original and display is not payload
    shown = display["charts"][0]
    assert shown["labels"] == ["CSV", "CSVStore", "ACSV", "调查问卷数据", "数据字段对应：q1"]
    assert shown["title"] == "问卷数据统计结果"
    assert shown["x_label"] == "数据字段"
    assert shown["note"] == "问卷数据，作答237条。"
    assert shown["notes"] == ["数据字段对应：q1。"]
    assert shown["caption"] == "问卷数据图"
    assert display["note"] == "数据来源：调查问卷数据。"
    for key in ("values", "metric", "denominator", "value_evidence", "evidence_ids", "method", "unknown", "claims"):
        assert shown[key] == child[key]
    assert display["extra"] == original["extra"]
    assert display["extra"] is not payload["extra"]


def test_display_field_dicts_are_not_recursively_rewritten(formatter):
    field_value = {"CSV": "CSV字段", "title": SOURCE, "statistics": [48]}
    assert formatter.table({"rows": [[field_value]]}) == {"rows": [[field_value]]}
    assert formatter.chart({"notes": [field_value]}) == {"notes": [field_value]}
