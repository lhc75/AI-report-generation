"""导出图下只渲染编号图题：图题完整、可续页并参与验收；注记不再渲染。"""
import copy
import json

import pytest
from docx import Document
from docx.oxml.ns import qn
from PIL import Image

from app import assemble, generate


def sample_chart():
    return {"title": "衔接问题", "labels": ["重复讲授较多", "内容衔接顺畅"],
            "values": [99.9, 20.3], "metric": "percent", "counts": [1000000, 203203],
            "percentages": [99.9, 20.3], "denominator": 1001001, "multi_select": True,
            "note": "该题允许多选；每份答卷内重复选项去重，比例之和可超过100%。",
            "notes": ["尚未取得正式施测编码说明，题目对应依据现有资料。"],
            "source": "数据来源：调查问卷作答记录。", "caption": "图1 衔接问题勾选结果"}


def test_caption_repetition_is_grammar_aware_without_hiding_other_questions_or_limits():
    from app.report_text import chart_caption_repeats_title
    chart = {"title": "大学生对全学段育人梯度的感知", "caption": "大学生：全学段育人梯度感知",
             "source": "来源：大学生问卷。", "note": "本题作答84条。"}
    original = copy.deepcopy(chart)
    assert generate.verified_chart_notes(chart) == [chart["note"], chart["source"]]
    assert chart == original
    assert not chart_caption_repeats_title("教师问卷第12题：校本课程", "教师问卷第13题：校本课程")
    assert not chart_caption_repeats_title("教师对学生的评价", "学生对教师的评价")
    assert not chart_caption_repeats_title("大学生：活动感受", "不能据此估算参与率。")


@pytest.mark.parametrize("document", [False, True])
def test_axis_contains_only_metric_and_preview_has_separate_full_notes(tmp_path, monkeypatch, document):
    import matplotlib.axes
    axes = []
    original = matplotlib.axes.Axes.set_title

    def capture(self, *args, **kwargs):
        axes.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "set_title", capture)
    chart = sample_chart()
    original_chart = copy.deepcopy(chart)
    generate.render_verified_chart(chart, "横向条形图", tmp_path / "chart.png", document=document)
    ax = axes[0]
    assert ax.get_xlabel() == "勾选率（%）"
    assert not ax.get_ylabel()
    assert "本题作答记录：1001001条（多选）" in [text.get_text() for text in ax.texts]
    all_text = "\n".join(text.get_text().replace("\n", "")
                         for child in ax.figure.axes for text in child.texts)
    for note in generate.verified_chart_notes(chart):
        assert (note in all_text) is (not document)
    assert chart == original_chart


@pytest.mark.parametrize("instruction", ["横向条形图", "柱状图", "折线图"])
def test_value_annotations_have_measured_room_inside_axes(tmp_path, monkeypatch, instruction):
    import matplotlib.axes
    axes = []
    original = matplotlib.axes.Axes.set_title

    def capture(self, *args, **kwargs):
        axes.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "set_title", capture)
    generate.render_verified_chart(sample_chart(), instruction, tmp_path / "plot.png", document=True)
    ax = axes[0]
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    FigureCanvasAgg(ax.figure)
    import matplotlib
    with matplotlib.rc_context({"font.sans-serif": ["Hiragino Sans GB", "Arial Unicode MS"]}):
        ax.figure.canvas.draw()
    renderer = ax.figure.canvas.get_renderer()
    frame = ax.get_window_extent(renderer)
    values = [text for text in ax.texts if "条 / " in text.get_text()]
    assert len(values) == 2
    for annotation in values:
        box = annotation.get_window_extent(renderer)
        assert box.x1 <= frame.x1 - 10 if "横" in instruction else box.y1 <= frame.y1 - 10


def report_with_captions(tmp_path):
    doc = Document()
    doc.add_paragraph("【生成图片：统计结果】")
    doc.add_paragraph("后续正文")
    template = tmp_path / "template.docx"
    doc.save(template)
    files = []
    for index in range(2):
        relative = f"chart-{index}.png"
        Image.new("RGB", (1000, 600), "white").save(tmp_path / relative)
        files.append(relative)
    node = {"id": 1, "type": "image", "title": "统计结果", "instruction": "【生成图片：统计结果】",
            "slot_key": "legacy-image-slot", "file": files[0], "image_files": files,
            "image_notes": [["图1 第一张统计结果"], ["图2 第二张统计结果"]],
            "image_group_notes": ["图3 组图总图题"]}
    manifest = {"nodes": [node]}
    output = tmp_path / "out" / "report.docx"
    assemble.assemble_template_docx(template, manifest, output)
    return Document(output), manifest, output


def test_each_chart_has_caption_lines_before_next_chart(tmp_path):
    report, manifest, _ = report_with_captions(tmp_path)
    paragraphs = report.paragraphs
    drawings = [index for index, p in enumerate(paragraphs) if list(p._p.iter(qn("w:drawing")))]
    notes = manifest["nodes"][0]["image_notes"]
    assert len(drawings) == 2
    assert [p.text for p in paragraphs[drawings[0] + 1:drawings[1]]] == notes[0]
    assert [p.text for p in paragraphs[drawings[1] + 1:-1]] == notes[1] + manifest["nodes"][0]["image_group_notes"]
    assert all(paragraphs[index].paragraph_format.keep_with_next for index in drawings)
    for position, drawing in enumerate(drawings):
        next_drawing = drawings[position + 1] if position + 1 < len(drawings) else len(paragraphs) - 1
        captions = paragraphs[drawing + 1:next_drawing]
        assert captions
        for index, p in enumerate(captions):
            assert p.paragraph_format.keep_together
            assert p.paragraph_format.keep_with_next is (index < len(captions) - 1)
            assert p.runs[0].font.size.pt == 10.5
            assert p.paragraph_format.first_line_indent == 0
    assert not paragraphs[-1].paragraph_format.keep_with_next
    assert assemble.make_report_validator(manifest)("ok", tmp_path)[0]


def test_oversized_caption_can_continue_and_do_not_lock_the_next_chart(tmp_path):
    doc = Document()
    anchor = doc.add_paragraph("图表占位")
    doc.add_paragraph("后续正文")
    image = tmp_path / "chart.png"
    Image.new("RGB", (1000, 600), "white").save(image)
    long_caption = "完整保留超长图题说明，允许在图后继续排版。" * 300
    notes = [[long_caption, "图尾补充。"], ["下一图图题。"]]
    assert not assemble._chart_caption_cluster_fits(doc.sections[0], image, notes[0])
    assemble._insert_image_group_after(doc, anchor._p, [image, image], notes=notes,
                                       group_notes=["末图共同图题。"])
    paragraphs = doc.paragraphs
    long_paragraph = next(p for p in paragraphs if p.text == long_caption)
    assert not long_paragraph.paragraph_format.keep_together
    assert not long_paragraph.paragraph_format.keep_with_next
    assert not next(p for p in paragraphs if p.text == notes[0][-1]).paragraph_format.keep_with_next
    last_note = next(p for p in paragraphs if p.text == notes[1][0])
    assert last_note.paragraph_format.keep_with_next
    assert not next(p for p in paragraphs if p.text == "末图共同图题。").paragraph_format.keep_with_next
    assert [p.text for p in paragraphs if p.text] == [
        long_caption, "图尾补充。", "下一图图题。", "末图共同图题。", "后续正文"]


def test_losing_caption_blocks_completeness_validation(tmp_path):
    report, manifest, output = report_with_captions(tmp_path)
    lost = next(p for p in report.paragraphs if p.text.startswith("图1 "))
    lost._p.getparent().remove(lost._p)
    report.save(output)
    ok, reason, _ = assemble.make_report_validator(manifest)("ok", tmp_path)
    assert not ok and "正文丢失" in reason and "图1" in reason


@pytest.mark.parametrize("notes", [[], ["a", "b"], [["a"], [1]], "caption"])
def test_caption_contract_rejects_ambiguous_or_wrong_length_lists(notes):
    with pytest.raises(assemble.TemplateAssemblyError, match="图题清单"):
        assemble._image_caption_lines({"id": 1, "image_notes": notes}, 2)


def test_group_preview_keeps_child_and_common_notes_in_separate_panels(tmp_path, monkeypatch):
    import matplotlib.axes
    seen = []
    original = matplotlib.axes.Axes.text

    def capture(self, x, y, text, *args, **kwargs):
        seen.append(text.replace("\n", ""))
        return original(self, x, y, text, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "text", capture)
    child = sample_chart()
    payload = {"charts": [child, copy.deepcopy(child)], "note": "共同说明：不同问卷的统计不可直接比较。"}
    original_payload = json.dumps(payload, ensure_ascii=False)
    generate.render_verified_chart(payload, "横向条形图", tmp_path / "group.png")
    assert sum(payload["note"] in text for text in seen) == 1
    assert sum(child["note"] in text for text in seen) == 2
    assert json.dumps(payload, ensure_ascii=False) == original_payload


def test_word_manifest_renders_only_top_caption_and_ignores_notes(tmp_path):
    metadata = {"charts": [{"note": "共同说明：不可比较。"},
                           {"note": "第二子图说明。", "source": "来源：教师问卷。"}],
                "caption": "图5 各题结果对照", "note": "父级说明。"}
    original = copy.deepcopy(metadata)
    notes, common = assemble._chart_captions_for_manifest(metadata)
    assert notes == [[], []]
    assert common == ["图5 各题结果对照"]
    assert metadata == original

    doc = Document()
    doc.add_paragraph("【生成图片：各题结果】")
    doc.add_paragraph("图片后的正文仍应完整保留。")
    template = tmp_path / "template.docx"
    doc.save(template)
    files = []
    for position in range(2):
        relative = f"chart-{position}.png"
        Image.new("RGB", (1000, 600), "white").save(tmp_path / relative)
        files.append(relative)
    manifest = {"nodes": [{"id": 1, "type": "image", "title": "各题结果",
                           "instruction": "【生成图片：各题结果】", "slot_key": "legacy-slot",
                           "file": files[0], "image_files": files,
                           "image_notes": notes, "image_group_notes": common}]}
    output = tmp_path / "out/report.docx"
    assemble.assemble_template_docx(template, manifest, output)
    texts = [paragraph.text for paragraph in Document(output).paragraphs]
    assert texts.count("图5 各题结果对照") == 1
    for forbidden in ("共同说明：不可比较。", "第二子图说明。", "来源：教师问卷。", "父级说明。"):
        assert forbidden not in texts
    assert assemble.make_report_validator(manifest)("ok", tmp_path)[0]


def test_word_manifest_falls_back_to_child_captions_without_top_caption():
    metadata = {"charts": [{"caption": "图6 左组"}, {"caption": "图6 右组"}]}
    original = copy.deepcopy(metadata)
    notes, common = assemble._chart_captions_for_manifest(metadata)
    assert notes == [["图6 左组"], ["图6 右组"]] and common == []
    assert metadata == original
    repeated = {"title": "衔接问题", "caption": "衔接问题"}
    notes, common = assemble._chart_captions_for_manifest(repeated)
    assert notes == [[]] and common == []


def test_numbered_caption_is_kept_even_when_it_repeats_title():
    repeated = {"title": "图2.3-1 知识梯度与内容重复感知",
                "caption": "图2.3-1 知识梯度与内容重复感知",
                "charts": [{"labels": ["a"], "values": [1]}, {"labels": ["b"], "values": [2]}]}
    notes, common = assemble._chart_captions_for_manifest(repeated)
    assert common == ["图2.3-1 知识梯度与内容重复感知"]
    assert notes == [[], []]
