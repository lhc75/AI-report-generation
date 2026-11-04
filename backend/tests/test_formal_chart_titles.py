"""来源题号留在存储标题，图题显示测量主题且不修改原选项。"""
import copy

import pytest

from app import generate
from app.report_text import formal_chart_title


@pytest.mark.parametrize("stored,display", [
    ("中学生原第13题：本校思政教育短板", "中学生：本校思政教育短板"),
    ("教师问卷原第13题：参加教研的感受", "教师：参加教研的感受"),
    ("本土资源应用期许（大学生问卷第7题）", "大学生：本土资源应用期许"),
    ("小学生第1题｜课堂形式", "小学生：课堂形式"),
    ("教育主管部门第8题：跨学段协作", "教育主管部门：跨学段协作"),
])
def test_known_question_locator_becomes_population_and_topic(stored, display):
    assert formal_chart_title(stored) == display
    assert formal_chart_title(display) == display


@pytest.mark.parametrize("title", [
    "中学生：本校思政教育短板", "第13题相关主题", "参加3—4次教研的感受", "本土资源应用期许",
])
def test_titles_without_population_and_question_locator_keep_their_meaning(title):
    assert formal_chart_title(title) == title


def saved_chart():
    return {
        "title": "满意度评价（小学生问卷第11题）", "labels": ["非常满意", "参加3—4次"],
        "values": [40, 60], "metric": "percent",
        "counts": [20, 30], "percentages": [40, 60], "denominator": 50,
        "note": "据现有问卷结构对应，未取得正式施测编码表。",
    }


@pytest.mark.parametrize("document", [False, True])
def test_verified_chart_renders_formal_title_without_mutating_saved_metadata(
        tmp_path, monkeypatch, document):
    import matplotlib.axes
    axes = []
    original_set_title = matplotlib.axes.Axes.set_title

    def capture(self, *args, **kwargs):
        axes.append(self)
        return original_set_title(self, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "set_title", capture)
    chart = saved_chart()
    stored = copy.deepcopy(chart)
    generate.render_verified_chart(chart, "横向条形图", tmp_path / "formal.png", document=document)
    assert axes[0].get_title() == "小学生：满意度评价"
    assert [label.get_text() for label in axes[0].get_yticklabels()] == stored["labels"]
    assert chart == stored
