"""数据摘要中的选项编码键位规律说明（键号与题号偏移）。"""
import io

from openpyxl import Workbook

from app import template_parse


def _summary(csv_text: str) -> str:
    return template_parse.summarize_data_structure("问卷.csv", csv_text.encode("utf-8"))


def test_key_rule_states_offset_when_keys_start_at_q2():
    csv_text = (
        "提交ID,选项编码\n"
        "1,region：GYGA；q2：A；q3：B；q4：C\n"
        "2,region：TR；q2：B；q3：C；q4：A\n"
    )
    summary = _summary(csv_text)
    assert "键号整体比题号大 1" in summary
    assert "q2…q4" in summary


def test_key_rule_keeps_neutral_wording_without_offset_pattern():
    csv_text = (
        "提交ID,选项编码\n"
        "1,q1：A；q2：B\n"
    )
    summary = _summary(csv_text)
    assert "键号比问卷文档题号大 1" not in summary
    assert "核对" in summary


def test_plain_csv_without_packed_codes_has_no_key_note():
    csv_text = (
        "姓名,成绩\n"
        "甲,90\n"
        "乙,80\n"
    )
    summary = _summary(csv_text)
    assert "键：选项" not in summary


def _xlsx_bytes(rows: list[list[str]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "问卷"
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_data_summary_excludes_question_code_mapping_row_from_submission_count():
    raw = _xlsx_bytes([
        ["姓别", "年级", "地区", "话题_了解法律"],
        ["id", "username", "region", "question13_A"],
        ["男", "五年级", "贵阳", "A"],
        ["女", "六年级", "遵义", "B"],
        ["男", "五年级", "铜仁", "C"],
    ])
    summary = template_parse.summarize_data_structure("问卷.xlsx", raw)
    assert "3条提交记录" in summary
    assert "4条提交记录" not in summary
    assert "原始样例（非统计结果）：姓别=男；年级=五年级；地区=贵阳" in summary


def test_data_summary_keeps_first_actual_row_when_it_is_not_a_code_mapping():
    raw = _xlsx_bytes([
        ["姓名", "成绩", "备注"],
        ["甲", "90", "正常"],
        ["乙", "80", "正常"],
    ])
    summary = template_parse.summarize_data_structure("成绩.xlsx", raw)
    assert "2条提交记录" in summary
    assert "原始样例（非统计结果）：姓名=甲；成绩=90" in summary
