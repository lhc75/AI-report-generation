"""终端短续文排版必须保持文本、字号、分节及显式模板约束。"""
from copy import deepcopy

import pytest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from app import assemble


def fixture():
    doc = Document()
    doc.styles['Normal'].paragraph_format.line_spacing = 1
    grid = doc.sections[-1]._sectPr.find(qn('w:docGrid'))
    grid.set(qn('w:type'), 'lines')
    paragraphs = [doc.add_paragraph(text) for text in (
        '培训安排结合教师参与情况。', '评价指标与学段特点相对应。', '后续持续记录实施情况与反馈。')]
    for paragraph in paragraphs:
        paragraph.runs[0].font.size = Pt(12)
    return doc, paragraphs, {7: (paragraphs[0]._p, paragraphs[-1]._p)}


def test_terminal_flow_preserves_all_text_fonts_and_section():
    doc, paragraphs, spans = fixture()
    text = [p.text for p in paragraphs]
    section = doc.sections[-1]._sectPr.xml
    assert assemble._format_terminal_text_flow(doc, spans)['task_id'] == 7
    assert [p.text for p in paragraphs] == text
    assert all(p.runs[0].font.size == Pt(12) for p in paragraphs)
    assert doc.sections[-1]._sectPr.xml == section
    assert all(p._p.pPr.find(qn('w:snapToGrid')).get(qn('w:val')) == 'false' for p in paragraphs)
    before = doc.element.xml
    assert assemble._format_terminal_text_flow(doc, spans) == {}
    assert doc.element.xml == before


@pytest.mark.parametrize('constraint', ['snapToGrid', 'pageBreakBefore', 'keepNext', 'keepLines', 'spacing'])
@pytest.mark.parametrize('location', ['paragraph', 'style', 'base_style'])
def test_explicit_layout_constraints_are_never_overridden(constraint, location):
    doc, paragraphs, spans = fixture()
    style = doc.styles.add_style('TerminalBody', WD_STYLE_TYPE.PARAGRAPH)
    style.base_style = doc.styles['Normal']
    style.paragraph_format.line_spacing = 1
    for p in paragraphs:
        p.style = style
    if location == 'paragraph':
        ppr = paragraphs[-1]._p.get_or_add_pPr()
    else:
        target = style if location == 'style' else style.base_style
        ppr = target.element.get_or_add_pPr()
    old = ppr.find(qn('w:' + constraint))
    if old is not None:
        ppr.remove(old)
    element = OxmlElement('w:' + constraint)
    if constraint == 'spacing':
        element.set(qn('w:line'), '240')
        element.set(qn('w:lineRule'), 'exact')
        if location == 'base_style':
            style.paragraph_format.line_spacing = None
    else:
        element.set(qn('w:val'), 'true')
    ppr.append(element)
    before = doc.element.xml
    assert assemble._format_terminal_text_flow(doc, spans) == {}
    assert doc.element.xml == before


@pytest.mark.parametrize('tail', ['blank', 'table', 'break', 'section', 'signoff', 'date'])
def test_non_prose_or_nonterminal_blocks_keep_template_layout(tail):
    doc, paragraphs, spans = fixture()
    if tail == 'blank':
        doc.add_paragraph()
    elif tail == 'table':
        doc.add_table(rows=1, cols=1)
    elif tail == 'break':
        paragraphs[-1].add_run().add_break()
    elif tail == 'section':
        paragraphs[-1]._p.get_or_add_pPr().append(deepcopy(doc.sections[-1]._sectPr))
    elif tail == 'signoff':
        paragraphs[-1].text = '贵州省基础教育研究院'
    elif tail == 'date':
        paragraphs[-1].text = '2026年10月'
    before = doc.element.xml
    assert assemble._format_terminal_text_flow(doc, spans) == {}
    assert doc.element.xml == before


def test_inserted_leaf_is_the_only_terminal_span_adjusted():
    doc, paragraphs, spans = fixture()
    inserted = [doc.add_paragraph(text) for text in ('新段落甲。', '新段落乙。', '新段落丙。')]
    spans[8] = (inserted[0]._p, inserted[-1]._p)
    assert assemble._format_terminal_text_flow(doc, spans)['task_id'] == 8
    assert all(p._p.pPr is None for p in paragraphs)


def test_unconfirmed_spacing_does_not_enable_grid_override():
    doc, _, spans = fixture()
    doc.styles['Normal'].paragraph_format.line_spacing = None
    before = doc.element.xml
    assert assemble._format_terminal_text_flow(doc, spans) == {}
    assert doc.element.xml == before


@pytest.mark.parametrize('tag', ['w:cr', 'w:fldChar', 'w:instrText', 'w:footnoteReference',
                               'w:endnoteReference', 'w:tab', 'm:oMath'])
def test_complex_inline_content_preserves_template_flow(tag):
    doc, paragraphs, spans = fixture()
    paragraphs[-1].runs[0]._r.append(OxmlElement(tag))
    before = doc.element.xml
    assert assemble._format_terminal_text_flow(doc, spans) == {}
    assert doc.element.xml == before
