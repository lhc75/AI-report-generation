"""图表可读性：横/竖布局自适应、AI layout 声明优先、注释防重叠与结构校验。"""
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image

from app import generate


def _record_bar_calls(monkeypatch):
    calls = {"bar": 0, "barh": 0}
    original_bar = matplotlib.axes.Axes.bar
    original_barh = matplotlib.axes.Axes.barh

    def record_bar(self, *args, **kwargs):
        calls["bar"] += 1
        return original_bar(self, *args, **kwargs)

    def record_barh(self, *args, **kwargs):
        calls["barh"] += 1
        return original_barh(self, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "bar", record_bar)
    monkeypatch.setattr(matplotlib.axes.Axes, "barh", record_barh)
    return calls


def test_chart_layout_priority_and_density():
    small = {"labels": ["甲", "乙"], "values": [1, 2]}
    assert generate._chart_layout({**small, "layout": "vertical"}, "条形图") == "vertical"
    assert generate._chart_layout(small, "横向条形图") == "horizontal"
    assert generate._chart_layout(small, "条形图") == "horizontal"
    assert generate._chart_layout(small, "柱状图") == "vertical"
    dense = {"labels": [f"类别{i}" for i in range(12)], "values": list(range(12))}
    assert generate._chart_layout(dense, "柱状图") == "horizontal"
    wide = {"labels": ["很长的类别名称超过阈值" * 2, "乙", "丙", "丁", "戊", "己", "庚"],
            "values": [1] * 7}
    assert generate._chart_layout(wide, "柱状图") == "horizontal"
    narrow = {"labels": ["甲", "乙", "丙", "丁", "戊", "己", "庚"], "values": [1] * 7}
    assert generate._chart_layout(narrow, "柱状图") == "vertical"


def test_chart_panel_height_scales_for_horizontal():
    dense = {"labels": [f"很长的类别名称与补充说明第{i}项" for i in range(22)],
             "values": list(range(22))}
    assert generate._chart_panel_height(dense, "柱状图") >= 15
    assert generate._chart_panel_height({"labels": ["a", "b"], "values": [1, 2]}, "柱状图") == 4.5


def test_wrap_label_prefers_parenthesis_break():
    assert generate._wrap_chart_label("课堂更生动有趣（小学生·第十二题）", 26) == \
        "课堂更生动有趣\n（小学生·第十二题）"


def test_dense_chart_renders_horizontal(monkeypatch, tmp_path):
    calls = _record_bar_calls(monkeypatch)
    chart = {"title": "分布", "labels": [f"类别{i}" for i in range(12)],
             "values": list(range(12, 0, -1)), "counts": list(range(12, 0, -1)),
             "percentages": [10.0] * 12, "metric": "count"}
    generate.render_verified_chart(chart, "条形图", tmp_path / "dense.png", document=True)
    # 新版 matplotlib 的 barh 内部会转发到 bar，用 barh 是否被调用来判定横向。
    assert calls["barh"] == 1
    assert (tmp_path / "dense.png").stat().st_size > 5000


def test_declared_layout_overrides_density(monkeypatch, tmp_path):
    calls = _record_bar_calls(monkeypatch)
    chart = {"title": "分布", "labels": [f"类别{i}" for i in range(12)],
             "values": list(range(12)), "layout": "vertical"}
    generate.render_verified_chart(chart, "柱状图", tmp_path / "vertical.png", document=True)
    assert calls["barh"] == 0 and calls["bar"] >= 1


def test_spread_annotations_resolves_vertical_overlap(monkeypatch):
    monkeypatch.setattr(generate, "_chart_layout", lambda *_args, **_kwargs: "vertical")
    chart = {"title": "密集", "labels": [f"类别{i}" for i in range(10)],
             "values": [100] * 10, "counts": [100] * 10, "percentages": [12.3] * 10}
    fig, ax = plt.subplots(figsize=(10, 6))
    try:
        annotations, horizontal = generate._draw_verified_chart(
            fig, ax, chart, "柱状图", document=True)
        assert not horizontal

        def overlaps() -> bool:
            fig.canvas.draw()
            renderer = fig.canvas.get_renderer()
            boxes = [item.get_window_extent(renderer) for item in annotations]
            return any(boxes[i].overlaps(boxes[j])
                       for i in range(len(boxes)) for j in range(i + 1, len(boxes)))

        assert overlaps(), "构造样本应发生标注重叠，验证防重叠逻辑"
        generate._spread_annotations(fig, ax, annotations, horizontal=False)
        assert not overlaps() or any(item.get_fontsize() < 14 for item in annotations) \
            or all(item.get_rotation() == 90 for item in annotations)
    finally:
        plt.close(fig)


def _chart_workdir(tmp_path):
    (tmp_path / "out").mkdir(exist_ok=True)
    image = Image.new("RGB", (600, 400), "white")
    for x in range(0, 600, 2):
        for y in range(0, 400, 2):
            image.putpixel((x, y), ((x * 7) % 256, (y * 11) % 256, ((x + y) * 13) % 256))
    image.save(tmp_path / "out" / "chart.png")
    return tmp_path


def test_finish_chart_layout_field_validation(tmp_path):
    workdir = _chart_workdir(tmp_path)
    payload = {"title": "t", "labels": ["a", "b"], "values": [1, 2], "layout": "Horizontal"}
    (workdir / "out" / "chart_data.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    ok, note, _ = generate.validate_finish("image", "说明", workdir)
    assert ok, note
    payload["layout"] = "diagonal"
    (workdir / "out" / "chart_data.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    ok, note, _ = generate.validate_finish("image", "说明", workdir)
    assert not ok and "layout" in note
