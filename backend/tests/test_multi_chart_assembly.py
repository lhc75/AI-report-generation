"""多图导出不缩成一张长图，并完整保留锚点顺序、分节和每张图。"""
import hashlib
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image
from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml.ns import qn
from docx.shared import Inches

from app import assemble, generate, storage
from app.models import ModuleContent


def eight_image_report(tmp_path):
    doc = Document()
    doc.sections[0].page_width = Inches(10)
    doc.add_heading("原模板前章", 1)
    doc.add_paragraph("第一节原正文")
    narrow = doc.add_section(WD_SECTION.NEW_PAGE)
    narrow.page_width = Inches(4)
    narrow.page_height = Inches(6)
    narrow.left_margin = narrow.right_margin = Inches(0.5)
    narrow.top_margin = narrow.bottom_margin = Inches(0.5)
    doc.add_heading("调查结果", 1)
    slot = doc.add_paragraph("【生成图片：八组问卷统计】")
    following = doc.add_section(WD_SECTION.NEW_PAGE)
    following.page_width = Inches(8.5)
    following.page_height = Inches(11)
    separator = doc.paragraphs[-1]._p
    sect_pr = separator.pPr.find(qn("w:sectPr"))
    separator.pPr.remove(sect_pr)
    slot._p.get_or_add_pPr().append(sect_pr)
    separator.getparent().remove(separator)
    doc.add_paragraph("末节原正文")
    template = tmp_path / "template.docx"
    doc.save(template)
    original_geometry = assemble._doc_signature(doc)["geometry"]
    files = []
    for index in range(7):
        relative = f"child-{index}.png"
        Image.new("RGB", (1200, 1800), (30 * index, 100, 200)).save(tmp_path / relative, dpi=(150, 150))
        files.append(relative)
    files.append(files[-1])  # 重复图像也必须按出现次数验收。
    Image.new("RGB", (80, 2000), "red").save(tmp_path / "preview.png")
    nodes = [
        {"id": 1, "type": "heading", "title": "原模板前章", "level": 1},
        {"id": 2, "type": "heading", "title": "调查结果", "level": 1},
        {"id": 3, "parent_id": 2, "type": "image", "title": "八组问卷统计", "level": 2,
         "instruction": slot.text, "slot_key": "legacy-slot", "file": "preview.png", "image_files": files},
    ]
    for task_id, anchor_id, text, sort in [(4, 3, "追加正文", 1), (5, 4, "追加正文的链式内容", 2),
                                           (6, 3, "同锚点第二段", 3)]:
        relative = f"text-{task_id}.txt"
        (tmp_path / relative).write_text(text, encoding="utf-8")
        nodes.append({"id": task_id, "parent_id": 2, "type": "text", "title": text, "level": 2,
                      "anchor_task_id": anchor_id, "sort": sort, "file": relative})
    manifest = {"nodes": nodes}
    output = tmp_path / "out" / "report.docx"
    result = assemble.assemble_template_docx(template, manifest, output)
    return Document(output), manifest, result, original_geometry


def test_eight_images_fit_their_section_and_insertions_follow_all_charts(tmp_path):
    report, manifest, result, original_geometry = eight_image_report(tmp_path)
    assert len(report.inline_shapes) == 8
    assert all(shape.width <= Inches(3) and shape.height <= Inches(4.6) for shape in report.inline_shapes)
    assert result["after"]["geometry"] == original_geometry
    assert len(report.sections) == len(list(report.element.iter(qn("w:sectPr")))) == 3
    image_paragraphs = [p for p in report.paragraphs if list(p._p.iter(qn("w:drawing")))]
    assert len(image_paragraphs) == 8
    assert not any(item.get(qn("w:type")) == "page" for item in report.element.iter(qn("w:br")))
    assert all(p.paragraph_format.keep_together for p in image_paragraphs)
    assert all(not p.paragraph_format.keep_with_next for p in image_paragraphs)
    assert all(p.paragraph_format.page_break_before is False for p in image_paragraphs[1:])
    paragraphs = report.paragraphs
    last_image = next(index for index, p in reversed(list(enumerate(paragraphs)))
                      if list(p._p.iter(qn("w:drawing"))))
    text_indexes = [next(index for index, p in enumerate(paragraphs) if p.text == value)
                    for value in ("追加正文", "追加正文的链式内容", "同锚点第二段", "末节原正文")]
    assert last_image < text_indexes[0] < text_indexes[1] < text_indexes[2] < text_indexes[3]
    assert all(p._p.pPr.find(qn("w:sectPr")) is None for p in image_paragraphs)
    assert paragraphs[text_indexes[2]]._p.pPr.find(qn("w:sectPr")) is not None
    assert assemble.make_report_validator(manifest)("ok", tmp_path)[0]


def test_completeness_requires_every_child_including_repeated_images(tmp_path):
    report, manifest, _, _ = eight_image_report(tmp_path)
    expected = [hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()
                for relative in manifest["nodes"][2]["image_files"]]
    assert assemble._image_signatures(report) == expected
    final_image = [p for p in report.paragraphs if list(p._p.iter(qn("w:drawing")))][-1]
    final_image._p.getparent().remove(final_image._p)
    report.save(tmp_path / "out" / "report.docx")
    ok, reason, _ = assemble.make_report_validator(manifest)("ok", tmp_path)
    assert not ok and "图片丢失" in reason


@pytest.mark.parametrize("relative", ["../outside.png", "/tmp/outside.png", "linked.png"])
def test_child_image_paths_cannot_escape_workdir(tmp_path, relative):
    outside = tmp_path.parent / "outside.png"
    Image.new("RGB", (100, 100), "red").save(outside)
    if relative == "linked.png":
        (tmp_path / relative).symlink_to(outside)
    with pytest.raises(ValueError):
        assemble._image_files({"id": 3, "file": "preview.png", "image_files": [relative]}, tmp_path)


@pytest.mark.parametrize("count", [8, 10])
def test_only_reviewed_metadata_is_split_without_changing_combined_preview(tmp_path, monkeypatch, count):
    (tmp_path / "contents").mkdir()
    charts = [{"title": f"图{index}", "labels": ["A"], "values": [index], "metric": "count"}
              for index in range(count)]
    task = SimpleNamespace(id=3240, title="八组图", instruction="条形图")
    content = SimpleNamespace(content=json.dumps({"charts": charts}), image_path="original-preview")
    called = []

    def render(chart, instruction, target, *, document=False):
        assert document
        called.append((chart, instruction, target))
        Image.new("RGB", (100, 100), "blue").save(target)

    monkeypatch.setattr(generate, "render_verified_chart", render)
    with pytest.raises(assemble.TemplateAssemblyError, match="尚未通过"):
        assemble._verified_chart_files(tmp_path, task, content, False)
    assert called == []
    files = assemble._verified_chart_files(tmp_path, task, content, True)
    assert len(files) == len(called) == count
    assert [chart for chart, _, _ in called] == charts
    assert [target for _, _, target in called] == [tmp_path / relative for relative in files]
    assert content.image_path == "original-preview"


def test_chart_panel_bound_is_shared_by_preview_and_export(tmp_path):
    charts = [{"title": "态度", "labels": ["支持"], "values": [40], "metric": "percent"}
              for _ in range(generate.MAX_CHART_PANELS + 1)]
    with pytest.raises(ValueError, match=f"1至{generate.MAX_CHART_PANELS}"):
        generate.render_verified_chart({"charts": charts}, "条形图", tmp_path / "too-many.png")
    task = SimpleNamespace(id=3230, title="本土实践", instruction="条形图")
    content = SimpleNamespace(content=json.dumps({"charts": charts}))
    with pytest.raises(assemble.TemplateAssemblyError, match="子图元数据无效"):
        assemble._verified_chart_files(tmp_path, task, content, True)


def test_ten_source_questions_render_all_preview_panels(tmp_path, monkeypatch):
    import matplotlib.axes
    drawn = []
    original = matplotlib.axes.Axes.set_title

    def capture(self, title, *args, **kwargs):
        drawn.append(title)
        return original(self, title, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "set_title", capture)
    charts = [{"title": f"第{n}题", "labels": ["支持"], "values": [40], "metric": "percent",
               "counts": [20], "percentages": [40], "denominator": 50} for n in range(1, 11)]
    generate.render_verified_chart({"charts": charts}, "条形图", tmp_path / "ten-panels.png")
    assert drawn == [item["title"] for item in charts]
    assert (tmp_path / "ten-panels.png").stat().st_size > 5000


@pytest.mark.parametrize("files", [[], "chart.png", [None]])
def test_invalid_image_file_contract_fails_explicitly(tmp_path, files):
    with pytest.raises(assemble.TemplateAssemblyError, match="清单无效"):
        assemble._image_files({"id": 3, "file": "preview.png", "image_files": files}, tmp_path)


def test_invalid_chart_metadata_blocks_export(tmp_path):
    task = SimpleNamespace(id=9, title="问题模块")
    content = SimpleNamespace(content="不是 JSON")
    with pytest.raises(assemble.TemplateAssemblyError, match="问题模块.*无法读取"):
        assemble._validated_chart_metadata(task, content)
    content = SimpleNamespace(content=json.dumps(["列表不是对象"]))
    with pytest.raises(assemble.TemplateAssemblyError, match="问题模块.*无效"):
        assemble._validated_chart_metadata(task, content)


def test_manifest_keeps_image_metadata_and_db_unchanged(tmp_path, project, db, monkeypatch):
    item, task, _ = project
    task.task_type = "image"
    task.instruction = "【生成图片：态度统计图】"
    task.slot_key = "legacy-image-slot"
    task.status = "done"
    chart = {"title": "态度统计图", "labels": ["A", "B"], "values": [66.7, 33.3],
             "metric": "percent", "multi_select": True, "denominator": 3}
    chart["note"] = "完整的图下注释。"
    chart["notes"] = ["第二段说明，必须保留。"]
    chart["caption"] = "图9 教师态度分布"
    original = json.dumps(chart, ensure_ascii=False)
    content = ModuleContent(chapter_task_id=task.id, version=1, content_type="image", status="ok",
                            is_current=True, content=original, image_path="original-preview")
    db.add(content)
    db.commit()
    template = Document()
    template.add_heading("调查结果", 1)
    template.add_paragraph(task.instruction)
    stream = io.BytesIO()
    template.save(stream)
    monkeypatch.setattr(assemble, "_template_bytes", lambda *_: stream.getvalue())
    preview = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(preview, format="PNG")
    monkeypatch.setattr(storage, "download_bytes", lambda *_: preview.getvalue())
    observed = []

    def render(value, instruction, path, *, document=False):
        observed.append((value["multi_select"], document))
        Image.new("RGB", (20, 20), "blue").save(path)

    monkeypatch.setattr(generate, "render_verified_chart", render)
    progress = []
    manifest = assemble.prepare_manifest(db, item, tmp_path, verified_chart_metadata=True,
                                         image_progress=lambda done, total: progress.append((done, total)))
    node = next(node for node in manifest["nodes"] if node["id"] == task.id)
    assert observed == [(True, True)]
    assert len(node["image_files"]) == 1
    # 图下只保留编号图题；note/notes 字段不再渲染。
    assert node["image_notes"] == [[chart["caption"]]]
    assert node["image_group_notes"] == []
    assert progress == [(1, 1)]
    assert (tmp_path / node["file"]).read_bytes() == preview.getvalue()
    db.expire_all()
    assert db.get(ModuleContent, content.id).content == original
    assert db.get(ModuleContent, content.id).image_path == "original-preview"
