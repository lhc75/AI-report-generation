"""数据摘要中的选项编码键位规律说明（键号与题号偏移）。"""
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
