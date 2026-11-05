# -*- coding: utf-8 -*-
"""确定性编号：图号、表号、题号与引用重映射。"""
import json
from types import SimpleNamespace

from app import report_numbering as rn


def task(tid, task_type, title, *, parent_id=None, sort=0):
    return SimpleNamespace(id=tid, task_type=task_type, title=title,
                           parent_id=parent_id, sort=sort)


def test_chinese_to_int():
    assert rn.chinese_to_int("七") == 7
    assert rn.chinese_to_int("十二") == 12
    assert rn.chinese_to_int("一百零二") == 102
    assert rn.chinese_to_int("") is None


def test_figure_numbers_follow_part_section_and_order():
    tasks = [
        task(1, "heading", "第二部分 调查结果", sort=0),
        task(2, "heading", "二、体制机制建设", parent_id=1, sort=1),
        task(3, "image", "图A", parent_id=2, sort=2),
        task(4, "image", "图B", parent_id=2, sort=3),
        task(5, "heading", "三、课程体系建设", parent_id=1, sort=4),
        task(6, "image", "图C", parent_id=5, sort=5),
    ]
    numbers = rn.figure_number_map(tasks)
    assert numbers == {3: "图2.2-1", 4: "图2.2-2", 6: "图2.3-1"}


def test_table_numbers_follow_document_order():
    tasks = [task(3, "table", "表B", sort=3), task(1, "table", "表A", sort=1)]
    assert rn.table_number_map(tasks) == {1: 1, 3: 2}


def test_question_numbers_are_normalized_everywhere():
    assert rn.normalize_question_numbers("教师第11题与第2题") == "教师第十一题与第二题"
    text = json.dumps({"content": "见第13题"}, ensure_ascii=False)
    assert "第十三题" in rn.normalize_content_questions(text, "text")
    chart = json.dumps({"title": "第12题", "labels": ["第3题"],
                        "values": [1]}, ensure_ascii=False)
    fixed = json.loads(rn.normalize_content_questions(chart, "image"))
    assert fixed["title"] == "第十二题"
    assert fixed["labels"] == ["第三题"]
    assert fixed["values"] == [1]


def test_chart_renumber_replaces_top_caption_and_strips_subchart_numbers():
    data = {
        "caption": "图9.9-1 图9.9-1 满意度分布",
        "charts": [
            {"title": "图9.9-2 小学生", "caption": "图9.9-3 小学生"},
        ],
    }
    fixed, old = rn.renumber_chart_data(data, "图2.7-1")
    assert fixed["caption"] == "图2.7-1 满意度分布"
    assert fixed["charts"][0]["title"] == "小学生"
    assert fixed["charts"][0]["caption"] == "小学生"
    assert {"图9.9-1", "图9.9-2", "图9.9-3"} <= old


def test_table_renumber_keeps_data():
    content = json.dumps({"table": {"title": "表9 题目概览", "columns": ["A"],
                                    "rows": [["1"]]}}, ensure_ascii=False)
    fixed, old = rn.renumber_table_content(content, 2)
    payload = json.loads(fixed)
    assert payload["table"]["title"] == "表2 题目概览"
    assert payload["table"]["rows"] == [["1"]]
    assert old == {"表9"}


def test_reference_map_only_keeps_unique_targets():
    mapping = rn.unique_reference_map({"图2.2-2": {"图2.2-2", "图2.2-3"},
                                       "图2.3-1": {"图2.3-1"}})
    assert mapping == {"图2.3-1": "图2.3-1"}


def test_text_row_normalizes_question_and_references():
    content = json.dumps({"content": "见第11题与图2.2-2"}, ensure_ascii=False)
    fixed, _ = rn.normalize_row_content(
        content, "text", figure_refs={"图2.2-2": "图2.2-3"})
    assert "第十一题" in fixed
    assert "图2.2-3" in fixed
