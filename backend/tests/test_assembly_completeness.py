"""表注、当前目录及合法章节树的真实 DOCX 结构回归。"""
import json
from datetime import date

import pytest
from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from app import assemble


def fixture(tmp_path, payload, *, section=False):
    doc = Document()
    doc.add_heading("调查结果", 1)
    slot = doc.add_paragraph("【生成表格：地区分布】")
    if section:
        doc.add_section(WD_SECTION.NEW_PAGE)
        section_p = doc.paragraphs[-1]._p
        properties = section_p.pPr.find(qn("w:sectPr"))
        section_p.pPr.remove(properties)
        slot._p.get_or_add_pPr().append(properties)
        section_p.getparent().remove(section_p)
    doc.add_paragraph("模板原正文")
    template = tmp_path / "template.docx"
    doc.save(template)
    (tmp_path / "table.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "child.txt").write_text("追加正文", encoding="utf-8")
    nodes = [
        {"id": 1, "type": "heading", "title": "调查结果", "level": 1},
        {"id": 2, "parent_id": 1, "type": "table", "title": "地区分布", "level": 2,
         "instruction": slot.text, "slot_key": "legacy-slot", "file": "table.json"},
        {"id": 3, "parent_id": 1, "type": "text", "title": "补充", "level": 2,
         "anchor_task_id": 2, "file": "child.txt"},
    ]
    manifest = {"nodes": nodes}
    output = tmp_path / "out" / "report.docx"
    result = assemble.assemble_template_docx(template, manifest, output)
    assert assemble.make_report_validator(manifest)("ok", tmp_path)[0]
    return Document(output), manifest, result


@pytest.mark.parametrize("wrapped", [False, True])
def test_table_note_and_notes_survive_assembly_before_inserted_text(tmp_path, wrapped):
    payload = {"title": "地区提交分布", "columns": ["地区", "提交数"], "rows": [["遵义", 34]],
               "note": "地区与学校类别混合设置。", "notes": ["遵义记录较少，比较须谨慎。", "比例显示可能有尾差。"]}
    if wrapped:
        payload = {"table": payload, "notes": ["统计范围为提交记录。"]}
    doc, manifest, _ = fixture(tmp_path, payload, section=True)
    texts = [paragraph.text for paragraph in doc.paragraphs]
    expected = ["地区与学校类别混合设置。", "遵义记录较少，比较须谨慎。", "比例显示可能有尾差。"]
    if wrapped:
        expected.append("统计范围为提交记录。")
    indexes = [texts.index(value) for value in expected]
    assert indexes == sorted(indexes)
    assert indexes[-1] < texts.index("追加正文") < texts.index("模板原正文")
    assert len(doc.sections) == 2
    assert len(list(doc.element.iter(qn("w:sectPr")))) == 2
    assert doc.tables[0].cell(1, 1).text == "34"
    next(paragraph for paragraph in doc.paragraphs if paragraph.text == expected[0])._p.getparent().remove(
        next(paragraph for paragraph in doc.paragraphs if paragraph.text == expected[0])._p)
    doc.save(tmp_path / "out" / "report.docx")
    ok, reason, _ = assemble.make_report_validator(manifest)("ok", tmp_path)
    assert not ok and "正文丢失" in reason


@pytest.mark.parametrize("note", [{"note": {"text": "不能静默丢弃"}}, {"notes": "字符串不能当数组"}, {"notes": [3]}])
def test_unsupported_note_contract_fails_explicitly(tmp_path, note):
    with pytest.raises(assemble.TemplateAssemblyError, match="note"):
        fixture(tmp_path, {"columns": ["地区"], "rows": [["遵义"]], **note})


def add_cached_toc(doc):
    doc.add_paragraph("目 录")
    sdt = OxmlElement("w:sdt")
    content = OxmlElement("w:sdtContent")
    for index, title in enumerate(["已删除的章节99", "旧调查结果17"]):
        paragraph = OxmlElement("w:p")
        if index == 0:
            for tag, value in (("w:fldChar", "begin"), ("w:instrText", None), ("w:fldChar", "separate")):
                run, item = OxmlElement("w:r"), OxmlElement(tag)
                if value:
                    item.set(qn("w:fldCharType"), value)
                else:
                    item.text = ' TOC \\o "1-3" \\h \\z \\u '
                run.append(item)
                paragraph.append(run)
        run, text = OxmlElement("w:r"), OxmlElement("w:t")
        text.text = title
        run.append(text)
        paragraph.append(run)
        if index == 1:
            run, ending = OxmlElement("w:r"), OxmlElement("w:fldChar")
            ending.set(qn("w:fldCharType"), "end")
            run.append(ending)
            paragraph.append(run)
        content.append(paragraph)
    sdt.append(content)
    doc.element.body.insert(len(doc.element.body) - 1, sdt)


@pytest.mark.parametrize("toc_kind", ["cached_sdt", "static", "simple"])
def test_directory_contains_current_headings_without_stale_page_numbers(tmp_path, toc_kind):
    doc = Document()
    doc.add_paragraph("封面日期 2025年12月")
    if toc_kind == "cached_sdt":
        add_cached_toc(doc)
    else:
        directory = doc.add_paragraph("目 录")
        if toc_kind == "static":
            doc.add_paragraph("已删除的章节\t99")
            doc.add_paragraph("旧调查结果\t17")
        else:
            field = OxmlElement("w:fldSimple")
            field.set(qn("w:instr"), ' TOC \\o "1-3" ')
            directory._p.append(field)
    doc.add_heading("调查结果", 1)
    doc.add_heading("具体结果", 2)
    template = tmp_path / "template.docx"
    doc.save(template)
    nodes = [{"id": 1, "type": "heading", "title": "修改后的调查结果", "template_title": "调查结果", "level": 1},
             {"id": 2, "parent_id": 1, "type": "heading", "title": "具体结果", "level": 2}]
    manifest = {"nodes": nodes}
    output = tmp_path / "out" / "report.docx"
    result = assemble.assemble_template_docx(template, manifest, output)
    report = Document(output)
    all_text = "".join(element.text or "" for element in report.element.iter(qn("w:t")))
    assert "已删除的章节" not in all_text and "旧调查结果" not in all_text
    assert "99" not in all_text and "17" not in all_text
    assert "封面日期 2025年12月" in all_text
    assert assemble._toc_cache_lines(report) == ["修改后的调查结果", "具体结果"]
    assert '\\n "1-9"' in report.element.xml
    assert report.settings.element.find(qn("w:updateFields")).get(qn("w:val")) == "true"
    assert result["refreshed_tocs"] == 1
    assert assemble.make_report_validator(manifest)("ok", tmp_path)[0]
    toc_text = next(element for element in report.element.iter(qn("w:t")) if element.text == "具体结果")
    toc_text.text = "过期目录标题"
    report.save(output)
    ok, reason, _ = assemble.make_report_validator(manifest)("ok", tmp_path)
    assert not ok and "toc_entries" in reason


@pytest.mark.parametrize("nodes, reason", [
    ([{"id": 1, "type": "heading", "title": "环", "parent_id": 1}], "循环"),
    ([{"id": 1, "type": "heading", "title": "缺父", "parent_id": 9}], "不存在"),
    ([{"id": 1, "type": "heading", "title": "结论", "level": 2},
      {"id": 2, "type": "heading", "title": "建议", "level": 2, "parent_id": 1}], "同级"),
    ([{"id": 1, "type": "text", "title": "正文"},
      {"id": 2, "type": "heading", "title": "错误父节点", "parent_id": 1}], "不是标题"),
])
def test_invalid_section_tree_rejected_before_document_mutation(nodes, reason):
    with pytest.raises(assemble.TemplateAssemblyError, match=reason):
        assemble._manifest_section_paths(nodes)


def test_only_confirmed_front_and_final_signoff_dates_are_updated(tmp_path):
    doc = Document()
    doc.add_paragraph("调查报告")
    doc.add_paragraph("贵州师范大学基础教育研究院")
    cover_date = doc.add_paragraph()
    cover_date.add_run("2025年").bold = True
    cover_date.add_run("12月").italic = True
    doc.add_paragraph("说明")
    doc.add_paragraph("数据采集日期为2025年12月，不能据制作日期改变。")
    doc.add_paragraph("贵州省基础教育研究院")
    front_signoff = doc.add_paragraph("2025年12月")
    doc.add_heading("调查结果", 1)
    doc.add_paragraph("数据收集于2025年12月20日，历史对比年度为2024年。")
    doc.add_paragraph("某市教育局")
    body_date = doc.add_paragraph("2025年12月")
    doc.add_paragraph("这段正文继续分析历史材料，上一独立日期不属于报告落款。")
    doc.add_paragraph("调查组")
    final_date = doc.add_paragraph("2025年12月20日")
    bookmark = OxmlElement("w:bookmarkStart")
    bookmark.set(qn("w:id"), "8")
    bookmark.set(qn("w:name"), "report_date")
    cover_date._p.insert(0, bookmark)
    before = assemble._doc_signature(doc)
    updates = assemble._refresh_report_dates(doc, date(2026, 10, 10))
    assert [(item["before"], item["after"], item["location"]) for item in updates] == [
        ("2025年12月", "2026年10月", "front_signoff"),
        ("2025年12月", "2026年10月", "front_signoff"),
        ("2025年12月20日", "2026年10月10日", "final_signoff"),
    ]
    assert cover_date.text == front_signoff.text == "2026年10月"
    assert cover_date.runs[0].bold and cover_date.runs[1].italic
    assert body_date.text == "2025年12月"
    assert final_date.text == "2026年10月10日"
    assert "数据采集日期为2025年12月，不能据制作日期改变。" in [p.text for p in doc.paragraphs]
    assert "数据收集于2025年12月20日，历史对比年度为2024年。" in [p.text for p in doc.paragraphs]
    after = assemble._doc_signature(doc)
    assert all(before[key] == after[key] for key in ["sections", "section_properties", "geometry", "bookmark_names"])


def test_export_date_replacements_are_part_of_integrity_expectation(tmp_path):
    doc = Document()
    doc.add_paragraph("调查组")
    doc.add_paragraph("2025年12月")
    doc.add_heading("调查结果", 1)
    doc.add_paragraph("正文历史日期为2025年12月。")
    template = tmp_path / "template.docx"
    doc.save(template)
    manifest = {"nodes": [{"id": 1, "type": "heading", "title": "调查结果", "level": 1}]}
    output = tmp_path / "out" / "report.docx"
    result = assemble.assemble_template_docx(template, manifest, output, date(2026, 10, 10))
    assert result["generated_on"] == "2026-10-10"
    assert len(result["date_updates"]) == 1
    assert assemble.make_report_validator(manifest)("ok", tmp_path)[0]
    report = Document(output)
    report.paragraphs[1].text = "2025年12月"
    report.save(output)
    ok, reason, _ = assemble.make_report_validator(manifest)("ok", tmp_path)
    assert not ok and "正文丢失" in reason


def test_unlabelled_and_invalid_dates_are_preserved():
    doc = Document()
    doc.add_paragraph("数据采集时间")
    doc.add_paragraph("2025年12月")
    doc.add_paragraph("调查组")
    doc.add_paragraph("2025年2月30日")
    doc.add_heading("调查结果", 1)
    doc.add_paragraph("2025年12月")
    original = [paragraph.text for paragraph in doc.paragraphs]
    assert assemble._refresh_report_dates(doc, date(2026, 10, 10)) == []
    assert original == [paragraph.text for paragraph in doc.paragraphs]

