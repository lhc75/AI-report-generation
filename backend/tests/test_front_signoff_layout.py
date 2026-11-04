"""说明末段与可确认落款相连，局部排版不改变材料内容或分节。"""
from datetime import date
import io

import pytest
from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from app import assemble
from app.models import ModuleContent, ProjectFile


def explanation_document():
    doc = Document()
    doc.add_paragraph("思想政治教育一体化建设路径及相关因素调查分析报告")
    doc.add_paragraph("贵州师范大学基础教育研究院")
    doc.add_paragraph("2025年12月")
    doc.add_section(WD_SECTION.NEW_PAGE)
    doc.add_paragraph("说 明")
    first = doc.add_paragraph("前段保留提交记录、原材料范围和统计限制。")
    last = doc.add_paragraph("最后说明段保留条件限制，不把提交记录认作有效独立人数。")
    author = doc.add_paragraph("贵州省基础教育研究院")
    report_date = doc.add_paragraph("2025年12月")
    for paragraph in (first, last, author, report_date):
        paragraph.paragraph_format.line_spacing = Pt(28)
        paragraph.paragraph_format.space_after = Pt(6)
        paragraph.paragraph_format.keep_with_next = True
        grid = OxmlElement("w:snapToGrid")
        grid.set(qn("w:val"), "true")
        paragraph._p.get_or_add_pPr().append(grid)
        paragraph.runs[0].font.size = Pt(12)
    bookmark = OxmlElement("w:bookmarkStart")
    bookmark.set(qn("w:id"), "4")
    bookmark.set(qn("w:name"), "explanation_signoff")
    author._p.insert(0, bookmark)
    doc.add_paragraph("目 录")
    doc.add_heading("调查结果", 1)
    doc.add_paragraph("正文不调整固定行距。")
    return doc, first, last, author, report_date


def test_explanation_tail_and_signoff_stay_adjacent_without_connecting_all_body():
    doc, first, last, author, report_date = explanation_document()
    original_texts = [p.text for p in doc.paragraphs]
    before = assemble._doc_signature(doc)
    body_xml = doc.paragraphs[-1]._p.xml
    assemble._format_front_signoff_layout(doc)
    assert [p.text for p in doc.paragraphs] == original_texts
    after = assemble._doc_signature(doc)
    assert all(before[key] == after[key] for key in (
        "sections", "section_properties", "geometry", "bookmark_names"))
    assert not first.paragraph_format.keep_with_next
    assert not first.paragraph_format.keep_together
    assert last.paragraph_format.keep_with_next
    assert not last.paragraph_format.keep_together
    assert author.paragraph_format.keep_with_next and author.paragraph_format.keep_together
    assert not report_date.paragraph_format.keep_with_next
    assert report_date.paragraph_format.keep_together
    assert first.paragraph_format.line_spacing == last.paragraph_format.line_spacing == 1
    assert author.paragraph_format.line_spacing == report_date.paragraph_format.line_spacing == 1
    assert first.paragraph_format.space_after == last.paragraph_format.space_after == 0
    assert author.paragraph_format.space_before == Pt(6)
    assert doc.paragraphs[-1]._p.xml == body_xml
    for paragraph in (first, last, author, report_date):
        assert paragraph._p.pPr.find(qn("w:snapToGrid")).get(qn("w:val")) == "false"
        assert paragraph.runs[0].font.size == Pt(12)


def test_layout_runs_after_generated_explanation_is_filled(tmp_path):
    doc, first, last, author, report_date = explanation_document()
    slot = "【撰写文本：说明正文】"
    first.text = slot
    last._p.getparent().remove(last._p)
    template = tmp_path / "template.docx"
    doc.save(template)
    generated = "前段保留提交记录、原材料范围和统计限制。\n最后说明段保留条件限制。"
    (tmp_path / "text.txt").write_text(generated, encoding="utf-8")
    manifest = {"nodes": [
        {"id": 1, "type": "heading", "title": "说 明", "level": 1},
        {"id": 2, "type": "text", "title": "说明正文", "parent_id": 1, "level": 2,
         "instruction": slot, "slot_key": "legacy-slot", "file": "text.txt"},
        {"id": 3, "type": "heading", "title": "调查结果", "level": 1},
    ]}
    output = tmp_path / "out/report.docx"
    result = assemble.assemble_template_docx(template, manifest, output, date(2026, 10, 11))
    report = Document(output)
    tail = next(p for p in report.paragraphs if p.text == generated.splitlines()[-1])
    author = next(p for p in report.paragraphs if p.text == "贵州省基础教育研究院")
    assert tail.paragraph_format.keep_with_next and author.paragraph_format.keep_with_next
    assert "贵州师范大学基础教育研究院" in [p.text for p in report.paragraphs]
    assert [p.text for p in report.paragraphs].count("2026年10月") == 2
    assert result["before"]["geometry"] == result["after"]["geometry"]
    assert assemble.make_report_validator(manifest)("ok", tmp_path)[0]


def test_confirmed_organization_repairs_only_legacy_terminal_signoff():
    doc, first, last, author, report_date = explanation_document()
    first.text = "贵州师范大学基础教育研究院组织开展了问卷调查，结果用于分析建设情况。"
    doc.add_paragraph("正文曾引用贵州省基础教育研究院的原文名称。")
    updates = assemble._repair_confirmed_signoff(doc)
    assert len(updates) == 1
    assert author.text == "贵州师范大学基础教育研究院"
    assert "贵州省基础教育研究院" in doc.paragraphs[-1].text


def test_confirmed_organization_requires_two_independent_matches():
    doc, first, last, author, report_date = explanation_document()
    updates = assemble._repair_confirmed_signoff(doc)
    assert updates == []
    assert author.text == "贵州省基础教育研究院"


def test_repeated_cover_name_without_organizer_predicate_does_not_repair():
    doc, first, last, author, report_date = explanation_document()
    first.text = "贵州师范大学基础教育研究院"
    assert assemble._repair_confirmed_signoff(doc) == []
    assert author.text == "贵州省基础教育研究院"


def _parsed_and_original_templates(db, project, monkeypatch, *, confirmed=True):
    item, task, _ = project
    slot = "【撰写文本：说明正文】"
    original, first, _, _, _ = explanation_document()
    first.text = ("贵州师范大学基础教育研究院组织开展了问卷调查，结果用于分析建设情况。"
                  if confirmed else "本次问卷调查用于分析建设情况。")
    original.add_paragraph("正文曾引用贵州省基础教育研究院的原文名称。")
    original.add_paragraph("贵州省基础教育研究院")
    original.add_paragraph("2025年12月")
    original_stream = io.BytesIO()
    original.save(original_stream)
    parsed = Document(io.BytesIO(original_stream.getvalue()))
    parsed.paragraphs[5].text = slot
    last = parsed.paragraphs[6]
    last._p.getparent().remove(last._p)
    parsed_stream = io.BytesIO()
    parsed.save(parsed_stream)
    stored = {"original-template.docx": original_stream.getvalue(),
              "parsed-template.docx": parsed_stream.getvalue()}
    monkeypatch.setattr(assemble.storage, "download_bytes", lambda path: stored[path])
    item.parsed_template_path = "parsed-template.docx"
    db.add(ProjectFile(project_id=item.id, file_type="template", file_name="original.docx",
                       minio_path="original-template.docx", size=len(original_stream.getvalue())))
    task.title, task.instruction, task.slot_key = "说明正文", slot, "legacy-slot"
    task.status = "done"
    db.add(ModuleContent(chapter_task_id=task.id, content_type="text", content="本次调查结果用于描述作答情况。",
                         status="ok", is_current=True))
    db.commit()
    return item, stored


def test_prepare_manifest_uses_original_organizer_when_parsed_template_has_lost_it(
        db, project, monkeypatch, tmp_path):
    item, stored = _parsed_and_original_templates(db, project, monkeypatch)
    original_bytes = stored["original-template.docx"]
    parsed_bytes = stored["parsed-template.docx"]
    manifest = assemble.prepare_manifest(db, item, tmp_path)
    assert manifest["signoff_confirmation"]["file"] == "signoff-source.docx"
    assert (tmp_path / "template.docx").read_bytes() == parsed_bytes
    assert (tmp_path / "signoff-source.docx").read_bytes() == original_bytes
    result = assemble.assemble_template_docx(tmp_path / "template.docx", manifest,
                                             tmp_path / "out/report.docx", date(2026, 10, 11))
    paragraphs = [paragraph.text for paragraph in Document(tmp_path / "out/report.docx").paragraphs]
    assert paragraphs.count("贵州师范大学基础教育研究院") == 2
    assert paragraphs.count("贵州省基础教育研究院") == 1  # 正文独立机构段保持不变
    assert "正文曾引用贵州省基础教育研究院的原文名称。" in paragraphs
    assert result["author_updates"][0]["confirmation_source"] == "original_template"
    assert result["before"]["bookmark_names"] == result["after"]["bookmark_names"]
    assert assemble.make_report_validator(manifest)("ok", tmp_path)[0]
    assert stored["original-template.docx"] == original_bytes
    assert stored["parsed-template.docx"] == parsed_bytes


@pytest.mark.parametrize("ambiguity", ["unconfirmed", "source_changed", "target_changed", "path_changed"])
def test_original_signoff_confirmation_rejects_unconfirmed_or_changed_files(
        db, project, monkeypatch, tmp_path, ambiguity):
    item, _ = _parsed_and_original_templates(db, project, monkeypatch, confirmed=ambiguity != "unconfirmed")
    manifest = assemble.prepare_manifest(db, item, tmp_path)
    template = tmp_path / "template.docx"
    if ambiguity == "unconfirmed":
        assert manifest["signoff_confirmation"] is None
    elif ambiguity == "source_changed":
        doc = Document(tmp_path / "signoff-source.docx")
        doc.add_paragraph("原件确认副本已变化。")
        doc.save(tmp_path / "signoff-source.docx")
    elif ambiguity == "target_changed":
        doc = Document(template)
        doc.paragraphs[-3].text = "正文曾引用贵州省基础教育研究院；装配模板已变化。"
        doc.save(template)
    else:
        manifest["signoff_confirmation"]["file"] = "../signoff-source.docx"
    result = assemble.assemble_template_docx(template, manifest, tmp_path / "out/report.docx")
    paragraphs = [paragraph.text for paragraph in Document(tmp_path / "out/report.docx").paragraphs]
    assert paragraphs.count("贵州省基础教育研究院") == 2
    assert result["author_updates"] == []


@pytest.mark.parametrize("ambiguity", ["author", "date", "invalid_date", "body_tail", "table", "drawing", "pict"])
def test_unconfirmed_or_nontext_explanation_is_not_reformatted(ambiguity):
    doc, first, last, author, report_date = explanation_document()
    if ambiguity == "author":
        author.text = "上述结论来自原材料。"
    elif ambiguity == "date":
        report_date.text = "材料收集于2025年12月"
    elif ambiguity == "invalid_date":
        report_date.text = "2025年2月30日"
    elif ambiguity == "body_tail":
        report_date._p.addnext(doc.add_paragraph("最后一段为正文，不是落款。")._p)
    elif ambiguity == "table":
        table = doc.add_table(rows=1, cols=1)
        last._p.addnext(table._tbl)
    else:
        last._p.append(OxmlElement(f"w:{ambiguity}"))
    original = doc.element.xml
    assemble._format_front_signoff_layout(doc)
    assert doc.element.xml == original


def test_body_explanation_and_institution_are_untouched():
    doc = Document()
    doc.add_heading("调查结果", 1)
    doc.add_paragraph("说明")
    doc.add_paragraph("正文中的机构与材料日期。")
    doc.add_paragraph("贵州省基础教育研究院")
    doc.add_paragraph("2025年12月")
    original = doc.element.xml
    assemble._format_front_signoff_layout(doc)
    assert doc.element.xml == original
