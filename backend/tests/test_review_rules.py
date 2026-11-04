"""审核与写作提示词包含通用文风规则（题号中文、禁止资料声明、图下只留图题）。"""
from app import generate, review


def test_writing_guide_requires_chinese_question_numbering():
    guide = generate._WRITING_GUIDE
    assert "第X题" in guide
    assert "Q1" in guide and "qN" in guide
    assert "未取得正式施测编码表" in guide


def test_review_notes_flag_q_codes_and_internal_disclaimers():
    notes = review._REVIEW_COMMON_NOTES
    assert "第X题" in notes
    assert "Q1" in notes
    assert "未取得正式施测编码表" in notes
    assert "沿用原问卷编号" in notes


def test_image_goal_drops_note_and_keeps_caption_contract():
    goal = generate.IMAGE_GOAL
    assert "note（短注）" not in goal
    assert "顶层 caption" in goal


def test_final_review_protocol_offers_direct_repairs():
    guide = review.FINAL_REVIEW_OUTPUT_GUIDE
    assert "必须给出 repairs" in guide
    assert "唯一文本片段" in guide
    assert "全稿统一编号方案" in guide


def test_module_review_requires_consistent_metric_scope():
    assert "逐项复算且口径一致" in review.MODULE_REVIEW_GOAL


def test_image_goal_includes_readability_rules():
    goal = generate.IMAGE_GOAL
    for marker in ("分面板", 'layout="horizontal"', "单行", "最短可辨认文字"):
        assert marker in goal, marker


def test_review_notes_flag_chart_readability():
    assert "图表排版可读性" in review._REVIEW_COMMON_NOTES


def test_seed_skill_texts_include_layout_principles():
    import importlib.util
    from pathlib import Path

    seed_path = Path(__file__).resolve().parents[1] / "seed_model_config_skills.py"
    spec = importlib.util.spec_from_file_location("seed_skills_module", seed_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("matplotlib绘图", "柱状图生成", "条形图生成"):
        _desc, content = module.SKILLS[name]
        assert "横向" in content or "分面板" in content, name
