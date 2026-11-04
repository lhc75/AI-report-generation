"""Report editing must reduce repetition without hiding limits or changing data."""
import copy
import io
import re

import pytest
from docx import Document

from app import assemble
from app.models import ChapterTask, ModuleContent
from app.report_editorial import PUBLIC_METHODS, ReportEditorialFormatter, has_public_methodology


CAVEAT = "依据现有资料作结构对应，尚未取得正式施测编码字典。"
METHOD = (CAVEAT + "各题以非空作答记录为分母。多选每记录去重，各项比例不能相加。"
          "统计以作答记录为单位，未作独立受访者识别及有效性筛选，不作共同量表排名、总体代表性或因果推断。"
          "“其他”不推断具体内容。改进意向不等同于实施情况或实际成效。")

NATURAL_MAPPING = ("报告按各类问卷的题干和选项对现有数据进行对应，正式施测编码说明尚待补充。"
                   "对应关系无法确定的题目暂不作实质解释。各对象问卷分别分析，同号题目不视为相同指标。")
NATURAL_METHOD = (
    "现有资料未提供独立受访者识别和问卷有效性筛选规则，本报告以提交记录及各题作答记录为统计单位，"
    "所列记录数不等同于有效独立受访者人数。各题比例以该题非空且可识别的作答记录为基数，"
    "未作答或无法识别的回答不计入该题分母。\n"
    "多选题按每条记录内的选项去重，勾选率为选择某项的记录数占该题作答记录数的比例；"
    "同一记录可选择多项，不能将各项比例相加解释为整体记录占比。"
    "“其他”仅报告该选项的选择情况，不延伸解释未提供的开放回答。\n"
    "调查结果用于描述本次作答，不代表全省总体状况，也不用于推断因果关系。"
    "不同对象按各自题义分别报告，改进选择反映需求和意向。"
)


@pytest.fixture
def formatter():
    return ReportEditorialFormatter(["survey.csv"], consolidate_methods=True)


def test_methodology_detection_requires_all_disclosures():
    assert has_public_methodology(METHOD)
    for omission in ("多选每记录去重", "各项比例不能相加", "独立受访者识别", "有效性筛选", "因果推断",
                     "统计以作答记录为单位", "“其他”不推断具体内容", "改进意向不等同于实施情况或实际成效"):
        assert not has_public_methodology(METHOD.replace(omission, "")), omission


@pytest.mark.parametrize("original,replacement", [
    ("尚未取得正式施测编码字典", "已取得正式施测编码字典"),
    ("未作独立受访者识别及有效性筛选", "已完成独立受访者识别及有效性筛选"),
])
def test_confirmed_methodology_is_not_rewritten_as_unavailable(original, replacement):
    assert not has_public_methodology(METHOD.replace(original, replacement))


def test_unrelated_negation_cannot_override_confirmed_identification():
    text = (CAVEAT + "数据无重复，已完成独立受访者识别和有效性筛选。"
            "多选按每条记录去重，以非空作答记录为分母，比例不能相加。"
            "不作共同量表排名、总体代表性或因果推断。")
    assert not has_public_methodology(text)
    assert not has_public_methodology(METHOD + "已完成独立受访者识别及有效性筛选。")


def test_isolated_output_keeps_full_disclosure():
    formatter = ReportEditorialFormatter([])
    text = CAVEAT + "教师问卷第13题作答386条，条件资格未确认。"
    assert formatter.text(text) == text


@pytest.mark.parametrize("text", [
    '中学生问卷第2题以237条非空作答记录为分母，“完全没有”190条（80.2%），“偶尔存在”32条（13.5%）。',
    '教师问卷第7题“制度完善”134条（34.7%），“有相关举措”126条（32.6%）。',
    '小学生第12题，87条（27.2%）；“减少重复学习内容”81条（25.3%）。',
    '教师原第8题以386条非空作答记录为分母，“专项经费”197条，占51.0%。',
    '非空作答记录不等于有效独立人数，教师问卷第8题作答386条，“专项经费”197条，占51.0%。',
])
def test_result_punctuation_and_numeric_order_survive_method_removal(formatter, text):
    edited = formatter.text(CAVEAT + text)
    assert re.findall(r"\d+(?:\.\d+)?", edited) == re.findall(r"\d+(?:\.\d+)?", text)
    assert edited.count('，“') == text.count('，“')
    assert edited.count('；“') == text.count('；“')
    assert "字典" not in edited


@pytest.mark.parametrize("text", [
    "教师第13题以学校无一体化校本课程为条件，资格尚未确认，仅展示本题作答分布。",
    "教师第20题原件未注明多选，实际存在多项作答，按实际填报统计。",
    "大学生该题未设置“无明显问题”选项。",
    "大学生第14题反映建设期许，并非满意度；不能计算总体满意度。",
    "中学生该题以参与后为前提，不能解释为参与率。",
    "大学生84条提交，遵义34条；贵阳贵安为合项，不能单独判断贵安。",
    "观察层次不同，不将学校反馈与辖区统计视为配对数据。",
    '原选项为“依据现有资料作结构对应，尚未取得正式施测编码字典”。',
    '原选项为“多选勾选率不能相加为整体人数占比”。',
    'label = "English option"; sample has 3 records.',
])
def test_unique_limit_and_literal_options_are_preserved(formatter, text):
    assert formatter.text(text) == text


def test_unrecognized_caveat_is_not_deleted_by_keywords(formatter):
    text = "根据现有资料，只有参与活动的学生能够作答；分母尚需按资格复核。"
    assert formatter.text(text) == text


def test_reviewed_region_note_explains_school_classification_without_audit_prose(formatter):
    note = "实际地区记录完整，未发现缺失或非法类别；贵州师范大学及其附属学校作为独立地区类别。"
    payload = {"rows": [["贵州师范大学及其附属学校", 593]],
               "columns": ["地区类别", "记录数"],
               "notes": [note, "地区题为原问卷未编号基本信息题。",
                         "按全部提交记录计数，未经有效性筛选，不按提交编号去重，不等同于有效独立人数。"],
               "cell_evidence": {"r0c1": ["ev_school"]}, "cell_metrics": {"r0c1": "count"}}
    original = copy.deepcopy(payload)
    shown = formatter.table(payload)
    assert shown["notes"] == ["“贵州师范大学及其附属学校”沿用问卷原分类，含义不同于行政地区。"]
    for key in ("rows", "columns", "cell_evidence", "cell_metrics"):
        assert shown[key] == original[key]
    assert payload == original
    assert ReportEditorialFormatter([]).table(payload) == original


def test_reviewed_complete_teacher_note_keeps_question_base_in_all_surfaces(formatter):
    note = "教师全部提交记录为386条，本题未见缺失或不可解析作答；比例以教师全部提交记录为分母。"
    expected = "教师本题作答386条。"
    assert formatter.text(note) == expected
    assert formatter.table({"notes": [note]})["notes"] == [expected]
    assert formatter.chart({"note": note})["note"] == expected
    # Missingness, another denominator or another group cannot satisfy the
    # narrowly reviewed complete-case sentence and must remain visible.
    for altered in (note.replace("未见缺失", "存在缺失"),
                    note.replace("比例以教师", "比例以大学生"),
                    note.replace("不可解析作答", "3条不可解析作答")):
        assert formatter.chart({"note": altered})["note"] == altered


def test_condition_notes_are_natural_and_retained_beside_their_result(formatter):
    participation = "题干以参与后为前提；未核定参与资格，仅展示本题作答分布，不代表参与率。"
    condition = ("原题以“学校无大中小学思政教育一体化校本课程”为条件，条件资格及跳题规则未能确认，"
                 "不据教师第12题筛选，仅报告本题实际作答记录，不能解释为符合条件学校的活动分布。")
    payload = {"charts": [{"note": participation, "values": [39], "counts": [39], "denominator": 84},
                          {"note": condition, "values": [248], "counts": [248], "denominator": 386}]}
    shown = formatter.chart(payload)
    assert shown["charts"][0]["note"] == "本题询问参与后的感受；现有回答尚未确认是否实际参与，不能据此估算参与率。"
    assert shown["charts"][1]["note"] == "本题面向未开设一体化校本课程的学校；现有回答尚未按这一条件筛选，结果仅呈现本题反馈。"
    for old, new in zip(payload["charts"], shown["charts"]):
        for key in ("values", "counts", "denominator"):
            assert old[key] == new[key]
    # Only the question locator in the exact reviewed sentence is removable.
    result = formatter.text(condition + "第12题作答386条，选择248条（64.2%）。")
    assert result.endswith("第12题作答386条，选择248条（64.2%）。")
    assert "现有回答尚未按这一条件筛选" in result
    assert formatter.text("该题有12条异常记录，尚未核定参与资格。") == "该题有12条异常记录，尚未核定参与资格。"


def test_reviewed_sentences_do_not_rewrite_literal_source_quotations(formatter):
    sentences = [
        "实际地区记录完整，未发现缺失或非法类别；贵州师范大学及其附属学校作为独立地区类别",
        "题干以参与后为前提；未核定参与资格，仅展示本题作答分布，不代表参与率",
    ]
    for sentence in sentences:
        literal = f"原文为《{sentence}》。"
        assert formatter.text(literal) == literal
        # Unknown qualifiers must not be silently absorbed into a reviewed
        # complete sentence, and leading whitespace need not defeat a review.
        assert formatter.text("不能认为" + sentence + "。") == "不能认为" + sentence + "。"
        assert formatter.text(sentence + "是否成立仍待核实。") == sentence + "是否成立仍待核实。"
        assert formatter.text("  " + sentence + "。") != "  " + sentence + "。"


def test_spaced_real_chart_note_does_not_repeat_shared_methods(formatter):
    note = ('同一记录可勾选多项，勾选率不能相加解释为整体人数占比；'
            '“其他”仅报告勾选，不推断具体内容。 依据现有资料作结构对应，'
            '尚未取得正式施测编码字典。 各对象题义不同，不作共同量表排名。 '
            '来源：调查问卷数据。')
    edited = formatter.chart({"title": "改进需求", "note": note})
    assert edited["note"] == "来源：调查问卷数据。"


def test_empty_chart_source_stays_empty_after_group_source_deduplication(formatter):
    for source in ("", "  "):
        chart = {"source": source, "title": "活动反馈"}
        assert formatter.chart(chart)["source"] == ""
    note = "题目按现有资料结构对应，尚未取得正式施测编码说明。"
    assert formatter.chart({"note": note})["note"] == ""


def test_denominator_inside_existing_parentheses_stays_flat(formatter):
    original = '教师问卷第2题最多选择“有”（310条，以386条非空作答记录为分母，占80.3%）。'
    edited = formatter.text(original)
    assert edited == '教师问卷第2题最多选择“有”（310条，本题作答386条，占80.3%）。'


def test_parentheses_before_a_literal_option_keep_global_depth(formatter):
    original = '教师问卷第2题（选择“有”的310条，以386条非空作答记录为分母，占80.3%）。'
    assert formatter.text(original) == '教师问卷第2题（选择“有”的310条，本题作答386条，占80.3%）。'


def test_cleanup_after_deletion_cannot_touch_literal_option_punctuation(formatter):
    original = '原选项为“先登记；；后作答”。'
    assert formatter.text(CAVEAT + original) == original


@pytest.mark.parametrize("left,right", [("'", "'"), ("＂", "＂"), ("＇", "＇"), ("〈", "〉"), ("《", "》")])
def test_source_quote_pairs_all_protect_literal_options(formatter, left, right):
    original = f"原选项为{left}非空作答记录；；依据现有资料作结构对应，尚未取得正式施测编码字典{right}。"
    assert formatter.text(CAVEAT + original) == original


@pytest.mark.parametrize("ending", ["？", "！", "?", "!"])
def test_existing_terminal_punctuation_is_kept_after_method_removal(formatter, ending):
    original = f"是否还需核验{ending}"
    assert formatter.text(CAVEAT + original) == original


def test_initial_preposition_does_not_become_orphaned(formatter):
    original = '教师问卷第7题，以本题386条非空作答记录为分母，“制度完善”134条（34.7%）。'
    assert formatter.text(original) == '教师问卷第7题（本题作答386条），“制度完善”134条（34.7%）。'


def test_other_note_consolidation_requires_method_section(formatter):
    note = '条件资格未确认。\n“其他”不扩写为无原文的活动。'
    edited = formatter.chart({"title": "活动", "labels": ["其他"], "note": note})
    assert edited["note"] == '条件资格未确认。'
    assert edited["labels"] == ["其他"]
    isolated = ReportEditorialFormatter([]).chart({"note": note})
    assert isolated["note"] == note


def test_methodology_voice_describes_limits_instead_of_author_instructions(formatter):
    original = ('缺失或不可识别作答按实际材料逐项核验，不预设其存在，也不据此直接删除提交记录。'
                '不生成访谈引语、有效独立人数或信效度结论，也不据此声称总体代表性或因果效应。')
    edited = formatter.text(original)
    assert '缺失或不可识别的回答不计入该题基数，提交记录仍保留' in edited
    assert '现有资料不足以支持访谈引语、有效独立人数及信效度结论' in edited


def test_central_methodology_contains_scope_without_repeated_machine_prose(formatter):
    result = formatter.text(CAVEAT + "共2054条提交记录。", methodology=True)
    assert result.count("2054") == 1
    assert "结构对应" not in result and "正式施测编码字典" not in result
    assert has_public_methodology(result)


def test_standard_methodology_is_recognized_and_not_repeated(formatter):
    standard = "\n\n".join(PUBLIC_METHODS)
    assert has_public_methodology(standard)
    assert formatter.text(standard, methodology=True) == standard
    assert not has_public_methodology(standard + "已完成独立受访者识别及有效性筛选。")


def test_natural_methodology_uses_other_preamble_without_reinserting_every_rule():
    formatter = ReportEditorialFormatter([], consolidate_methods=True, methodology_context=NATURAL_MAPPING)
    assert has_public_methodology(NATURAL_MAPPING + "\n" + NATURAL_METHOD)
    assert formatter.text(NATURAL_METHOD, methodology=True) == NATURAL_METHOD
    assert not has_public_methodology(NATURAL_METHOD)  # mapping is in another module
    missing = ReportEditorialFormatter([], consolidate_methods=True).text(NATURAL_METHOD, methodology=True)
    assert "正式施测编码说明尚待确认。" in missing
    assert missing.count("不同对象按各自题义分别报告") == 1
    assert missing.count("多选题按每条记录内的选项去重") == 1
    assert has_public_methodology(missing)


@pytest.mark.parametrize("removed,restored", [
    ("正式施测编码说明尚待补充", "正式施测编码说明尚待确认。"),
    ("未作答或无法识别的回答不计入该题分母", "各题比例以该题非空且可识别的作答记录为基数"),
    ("多选题按每条记录内的选项去重", "多选题按每条记录内的选项去重"),
    ("不能将各项比例相加解释为整体记录占比", "不能将各项比例相加解释为整体记录占比"),
    ("不延伸解释未提供的开放回答", "“其他”仅报告选择情况，不解释未提供的开放回答。"),
    ("不代表全省总体状况", "结果仅描述本次作答，不代表全省总体状况。"),
    ("不用于推断因果关系", "结果不用于推断因果关系。"),
    ("改进选择反映需求和意向", "改进选择反映需求和意向，不等同于实施情况或实际成效。"),
])
def test_method_completion_only_restores_missing_disclosures(removed, restored):
    text = (NATURAL_MAPPING + "\n" + NATURAL_METHOD).replace(removed, "")
    formatter = ReportEditorialFormatter([], consolidate_methods=True)
    # Denominator semantics remain present in the first clause if only its
    # explanatory second clause is removed; they need no duplicate completion.
    if removed == "未作答或无法识别的回答不计入该题分母":
        assert has_public_methodology(text)
        assert formatter.text(text, methodology=True) == text
        return
    assert not has_public_methodology(text)
    edited = formatter.text(text, methodology=True)
    assert restored in edited
    assert has_public_methodology(edited)
    assert edited.count("本报告以提交记录及各题作答记录为统计单位") == 1


def test_natural_methodology_keywords_without_negative_limits_do_not_enable_consolidation():
    insufficient = ("问卷数据已对应，编码说明完整。统计记录、受访者人数、有效性筛选情况均已核实。"
                    "多选可相加，各对象代表全省总体并证明因果关系，措施已实施。")
    assert not has_public_methodology(insufficient)


@pytest.mark.parametrize("bad", [
    NATURAL_METHOD.replace("多选题按每条记录内的选项去重", "多选题未按每条记录内的选项去重"),
    NATURAL_METHOD.replace("各题比例以该题非空且可识别的作答记录为基数，未作答或无法识别的回答不计入该题分母",
                          "非空作答记录数量另附，不在此交代比例基数"),
    NATURAL_METHOD + "正式施测编码说明已经取得。",
])
def test_methodology_detector_rejects_negated_or_confirmed_variants(bad):
    assert not has_public_methodology(NATURAL_MAPPING + "\n" + bad)


def test_chart_caption_grammar_variant_is_removed_but_specific_information_is_kept(formatter):
    title = "大学生对全学段育人梯度的感知"
    payload = {"title": title, "caption": "大学生：全学段育人梯度感知",
               "values": [46.4, 39.3], "value_evidence": ["ev_1", "ev_2"], "denominator": 84}
    original = copy.deepcopy(payload)
    shown = formatter.chart(payload)
    assert shown["caption"] == ""
    assert shown["title"] == title
    assert payload == original
    for key in ("values", "value_evidence", "denominator"):
        assert shown[key] == original[key]
    for caption in ("中学生：全学段育人梯度感知", "大学生：全学段育人梯度满意度",
                    "图7 大学生：全学段育人梯度感知", "39条回答认为非常清晰，占46.4%。"):
        assert formatter.chart({"title": title, "caption": caption})["caption"] == caption
    assert formatter.chart({"title": "大学生：活动感知",
                            "caption": "大学生问卷第7题：活动感知"})["caption"] == "大学生问卷第7题：活动感知"
    # 带图号的图题即使与标题完全相同也必须保留（图下需要编号）。
    numbered = {"title": "图2.3-1 知识梯度与内容重复感知", "caption": "图2.3-1 知识梯度与内容重复感知"}
    assert formatter.chart(numbered)["caption"] == "图2.3-1 知识梯度与内容重复感知"


def test_table_edits_notes_and_headers_without_touching_cells_or_binding(formatter):
    table = {"columns": ["短主题", "原件题号", "频次", "比例"],
             "rows": [["落实", "第13题", 248, "64.2%"], ["其他", "第13题", 0, "0.0%"]],
             "note": CAVEAT + "条件资格尚未确认，不代表符合条件学校的活动比例。",
             "cell_evidence": {"r0c2": ["ev_1"]}, "cell_metrics": {"r0c2": "count"},
             "extra": {"note": CAVEAT}}
    original = copy.deepcopy(table)
    edited = formatter.table(table)
    assert table == original
    assert edited["rows"] == original["rows"]
    assert edited["columns"] == ["调查内容", "问卷题号", "频次", "比例"]
    assert edited["note"] == "条件资格尚未确认，不代表符合条件学校的活动比例。"
    for key in ("cell_evidence", "cell_metrics", "extra"):
        assert edited[key] == original[key]


def test_numbered_module_supplies_missing_table_caption_number(formatter):
    payload = {"title": "资源利用", "columns": ["教师频次（作答记录）", "教师该题作答记录数"],
               "rows": [[148, 386]], "cell_metrics": {"r0c0": "count", "r0c1": "denominator"}}
    edited = formatter.table(payload, module_title="表6 本土资源利用")
    assert edited["title"] == "表6 资源利用"
    assert edited["columns"] == ["教师选择记录数", "教师本题作答记录数"]
    assert edited["rows"] == payload["rows"] and edited["cell_metrics"] == payload["cell_metrics"]
    assert formatter.table({"title": "表8 评价"}, module_title="表8 评价体系")["title"] == "表8 评价"


def test_child_chart_caveats_stay_with_child_and_numbers_keep_bindings(formatter):
    chart = {"note": CAVEAT, "charts": [
        {"title": "中学生原第13题：本校思政教育短板", "labels": ["参加3—4次", "其他"],
         "values": [48.9, 0], "metric": "percent", "value_evidence": ["ev_a", "ev_b"],
         "counts": [116, 0], "denominator": 237, "source": "中学生问卷第13题", "note": CAVEAT},
        {"title": "教师问卷原第13题：特色活动", "labels": ["其他"], "values": [248],
         "value_evidence": ["ev_c"], "metric": "count", "denominator": 386,
         "note": "条件资格未确认，仅报告本题作答分布。" + CAVEAT},
    ]}
    original = copy.deepcopy(chart)
    edited = formatter.chart(chart)
    assert chart == original and edited["note"] == ""
    assert edited["charts"][0]["title"] == "中学生：本校思政教育短板"
    assert edited["charts"][0]["source"] == "来源：中学生问卷。"
    assert edited["charts"][1]["note"] == "条件资格未确认，仅报告本题作答分布。"
    for old, new in zip(original["charts"], edited["charts"]):
        for key in ("labels", "values", "metric", "value_evidence", "counts", "denominator"):
            assert new.get(key) == old.get(key)


def test_actual_export_centralizes_methods_once_and_keeps_saved_versions(tmp_path, project, db, monkeypatch):
    item, result_task, _ = project
    heading = ChapterTask(project_id=item.id, title="说 明", task_type="heading", sort=-3)
    db.add(heading)
    db.flush()
    method_task = ChapterTask(project_id=item.id, parent_id=heading.id, title="统计方法",
                              task_type="text", instruction="【填写：统计方法】", status="done",
                              slot_key="method-slot", sort=-2)
    body_heading = ChapterTask(project_id=item.id, title="调查结果", task_type="heading", sort=-1)
    db.add_all([method_task, body_heading])
    db.flush()
    result_task.slot_key = "result-slot"
    result_task.status = "done"
    result_task.parent_id = body_heading.id
    text = CAVEAT + '该题作答237条，“实践偏少”116条（48.9%）；参与资格未确认。'
    method = ModuleContent(chapter_task_id=method_task.id, version=1, is_current=True, status="ok",
                           content_type="text", content=METHOD, evidence_json="[]")
    result = ModuleContent(chapter_task_id=result_task.id, version=1, is_current=True, status="ok",
                           content_type="text", content=text, evidence_json='[{"text":"original"}]')
    db.add_all([method, result])
    db.commit()
    saved = (method.content, result.content, method.evidence_json, result.evidence_json)
    template = Document()
    template.add_heading("说 明", 1)
    template.add_paragraph(method_task.instruction)
    template.add_heading("调查结果", 1)
    template.add_paragraph(result_task.instruction)
    raw = io.BytesIO()
    template.save(raw)
    monkeypatch.setattr(assemble, "_template_bytes", lambda *_: raw.getvalue())
    manifest = assemble.prepare_manifest(db, item, tmp_path)
    output = tmp_path / "out" / "report.docx"
    assemble.assemble_template_docx(tmp_path / "template.docx", manifest, output)
    visible = "\n".join(p.text for p in Document(output).paragraphs)
    assert visible.count("正式施测编码说明尚待确认") == 1
    assert "正式施测编码字典" not in visible
    assert '该题作答237条，“实践偏少”116条（48.9%）；参与资格未确认。' in visible
    db.expire_all()
    assert (method.content, result.content, method.evidence_json, result.evidence_json) == saved
    assert manifest["editorial_policy"]["shared_methods_centralized"]


def test_actual_export_keeps_natural_method_and_source_preamble_once(tmp_path, project, db, monkeypatch):
    item, result_task, _ = project
    heading = ChapterTask(project_id=item.id, title="说明", task_type="heading", sort=-5)
    db.add(heading)
    db.flush()
    mapping_task = ChapterTask(project_id=item.id, parent_id=heading.id, title="题目对应依据", task_type="text",
                               instruction="【填写：题目对应】", slot_key="mapping-slot", status="done", sort=-4)
    method_task = ChapterTask(project_id=item.id, parent_id=heading.id, title="公共统计口径", task_type="text",
                              instruction="【填写：公共统计】", slot_key="method-slot", status="done", sort=-3)
    body = ChapterTask(project_id=item.id, title="调查结果", task_type="heading", sort=-1)
    db.add_all([mapping_task, method_task, body])
    db.flush()
    result_task.parent_id = body.id
    result_task.status = "done"
    result_task.slot_key = "result-slot"
    for task, content in ((mapping_task, NATURAL_MAPPING), (method_task, NATURAL_METHOD),
                          (result_task, CAVEAT + "本题作答84条，不能据此估算参与率。")):
        db.add(ModuleContent(chapter_task_id=task.id, version=1, is_current=True, status="ok",
                             content_type="text", content=content, evidence_json="[]"))
    db.commit()
    template = Document()
    template.add_heading("说明", 1)
    template.add_paragraph(mapping_task.instruction)
    template.add_paragraph(method_task.instruction)
    template.add_heading("调查结果", 1)
    template.add_paragraph(result_task.instruction)
    raw = io.BytesIO()
    template.save(raw)
    monkeypatch.setattr(assemble, "_template_bytes", lambda *_: raw.getvalue())
    manifest = assemble.prepare_manifest(db, item, tmp_path)
    output = tmp_path / "out" / "report.docx"
    assemble.assemble_template_docx(tmp_path / "template.docx", manifest, output)
    visible = "\n".join(p.text for p in Document(output).paragraphs)
    assert NATURAL_MAPPING in visible
    assert NATURAL_METHOD in visible
    assert visible.count("正式施测编码说明尚待补充") == 1
    assert visible.count("多选题按每条记录内的选项去重") == 1
    assert visible.count("本报告以提交记录及各题作答记录为统计单位") == 1
    assert "正式施测编码说明尚待确认" not in visible
    assert "正式施测编码字典" not in visible
    assert "本题作答84条，不能据此估算参与率。" in visible
    assert manifest["editorial_policy"]["shared_methods_centralized"]
