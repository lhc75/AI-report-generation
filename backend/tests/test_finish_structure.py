"""finish 结构检查：文本/表格/图的形状与文件提交（不含任何内容语义校验）。"""
import json
import os

import pytest
from PIL import Image

from app import generate


def _text(count=80):
    return "正" * count


def test_text_finish_accepts_json_content_and_raw_text(tmp_path):
    ok, note, normalized = generate.validate_finish(
        "text", json.dumps({"content": _text()}, ensure_ascii=False), tmp_path)
    assert ok, note and json.loads(normalized)["content"] == _text()
    ok, note, normalized = generate.validate_finish("text", _text(), tmp_path)
    assert ok and json.loads(normalized)["content"] == _text()
    ok, note, _ = generate.validate_finish("text", "太短", tmp_path)
    assert not ok and "太短" in note


def test_text_finish_rejects_mangled_json_envelope(tmp_path):
    """转录失败时 {"content": …} 外壳不能被当成正文存入成果。"""
    mangled = '{"content":"' + "正" * 60 + '，"引号未转义""}'
    ok, note, _ = generate.validate_finish("text", mangled, tmp_path)
    assert not ok and "content" in note
    for broken in (json.dumps({"text": _text()}, ensure_ascii=False), json.dumps(_text())):
        ok, note, _ = generate.validate_finish("text", broken, tmp_path)
        assert not ok, broken


def test_table_finish_structure_checks(tmp_path):
    flat = {"title": "表", "columns": ["选项", "人数"], "rows": [["支持", 2], ["反对", 1]],
            "note": "注：单位条。"}
    ok, note, normalized = generate.validate_finish(
        "table", json.dumps(flat, ensure_ascii=False), tmp_path)
    assert ok, note
    assert json.loads(normalized)["rows"] == flat["rows"]
    assert json.loads(normalized)["note"] == flat["note"]
    wrapped = {"table": {"columns": ["a"], "rows": [["x"]]}, "note": "顶层注"}
    ok, _, normalized = generate.validate_finish("table", json.dumps(wrapped), tmp_path)
    assert ok and json.loads(normalized)["note"] == "顶层注"
    for broken in (
        {"columns": ["a"]},
        {"columns": ["a"], "rows": []},
        {"columns": ["a", "b"], "rows": [["x"]]},
        {"columns": ["a"], "rows": [[{"bad": 1}]]},
    ):
        ok, note, _ = generate.validate_finish("table", json.dumps(broken, ensure_ascii=False), tmp_path)
        assert not ok, broken


def _make_chart_workdir(tmp_path, labels=("支持", "反对"), values=(2, 1)):
    (tmp_path / "out").mkdir(exist_ok=True)
    image = Image.new("RGB", (600, 400), "white")
    for x in range(0, 600, 2):
        for y in range(0, 400, 2):
            image.putpixel((x, y), ((x * 7) % 256, (y * 11) % 256, ((x + y) * 13) % 256))
    image.save(tmp_path / "out" / "chart.png")
    (tmp_path / "out" / "chart_data.json").write_text(
        json.dumps({"title": "态度", "labels": list(labels), "values": list(values)}, ensure_ascii=False),
        encoding="utf-8")
    return tmp_path


def test_image_finish_requires_decodable_png_and_chart_shape(tmp_path):
    ok, note, _ = generate.validate_finish("image", "说明", tmp_path)
    assert not ok and "chart.png" in note
    workdir = _make_chart_workdir(tmp_path)
    assert workdir.joinpath("out/chart.png").stat().st_size > 5000
    ok, note, _ = generate.validate_finish("image", "说明", workdir)
    assert ok, note
    (workdir / "out" / "chart_data.json").write_text(
        json.dumps({"title": "t", "labels": ["a", "b"], "values": [1]}), encoding="utf-8")
    ok, note, _ = generate.validate_finish("image", "说明", workdir)
    assert not ok and "长度不一致" in note
    (workdir / "out" / "chart_data.json").write_text(
        json.dumps({"title": "t"}), encoding="utf-8")
    ok, note, _ = generate.validate_finish("image", "说明", workdir)
    assert not ok and "labels/values" in note
    (workdir / "out" / "chart_data.json").write_text(
        json.dumps({"title": "t", "labels": ["a", "b"], "values": [1, 2]}), encoding="utf-8")
    ok, note, _ = generate.validate_finish("image", "说明", workdir)
    assert ok, note
    ok, note, _ = generate.validate_finish("image", "说明", workdir, "【生成图片（饼图）：分布】")
    assert not ok and "pie_counts" in note


def test_image_finish_rejects_object_shaped_fields(tmp_path):
    workdir = _make_chart_workdir(tmp_path)
    (workdir / "out" / "chart_data.json").write_text(json.dumps({
        "title": "t", "labels": ["a", "b"], "values": [1, 2],
        "counts": {"分组一": [1], "分组二": [2]},
    }, ensure_ascii=False), encoding="utf-8")
    ok, note, _ = generate.validate_finish("image", "说明", workdir)
    assert not ok and "counts" in note
    (workdir / "out" / "chart_data.json").write_text(json.dumps({
        "title": "t", "labels": ["a", "b"], "values": [1, 2],
        "denominator": {"a": 10, "b": 20},
    }, ensure_ascii=False), encoding="utf-8")
    ok, note, _ = generate.validate_finish("image", "说明", workdir)
    assert not ok and "denominator" in note
    (workdir / "out" / "chart_data.json").write_text(json.dumps({
        "title": "t", "labels": ["a"], "values": [{"a": 1}],
    }, ensure_ascii=False), encoding="utf-8")
    ok, note, _ = generate.validate_finish("image", "说明", workdir)
    assert not ok and "values" in note


@pytest.mark.parametrize("path", ["../result.json", "out/../result.json", "/tmp/result.json",
                                  "data/result.json", "out/result.txt"])
def test_finish_file_rejects_paths_outside_output(tmp_path, path):
    ok, note, _ = generate.validate_finish("table", json.dumps({"result_file": path}), tmp_path)
    assert not ok and "成果文件" in note


def test_finish_file_rejects_links_directories_large_and_invalid_json(tmp_path, monkeypatch):
    (tmp_path / "out").mkdir()
    outside = tmp_path / "source.json"
    outside.write_text('{"columns":["类别"],"rows":[["支持"]]}', encoding="utf-8")
    result = tmp_path / "out/result.json"
    submit = lambda: generate.validate_finish("table", '{"result_file":"out/result.json"}', tmp_path)
    result.symlink_to(outside)
    assert not submit()[0]
    result.unlink()
    os.link(outside, result)
    assert not submit()[0]
    result.unlink()
    result.mkdir()
    assert not submit()[0]
    result.rmdir()
    result.write_text("{}" * 100, encoding="utf-8")
    monkeypatch.setattr(generate.settings, "SCRIPT_SANDBOX_MAX_OUTPUT_BYTES", 100)
    assert not submit()[0]
    for invalid in (b"", b"{", b"\xff", b"[]", b'{"result_file":"out/other.json"}'):
        result.write_bytes(invalid)
        assert not submit()[0]
