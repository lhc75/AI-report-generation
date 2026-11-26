"""Batch planning and deterministic Word output for survey statistics."""

import io
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Literal

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from .survey_data import LABELS, LEVELS, Level
from .survey_schools import QuestionnaireStage, infer_stage, school_stage

WORD_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TITLE_TOKEN = "{{报告标题}}"
BODY_TOKEN = "{{报告正文}}"


class LevelSettings(BaseModel):
    questionnaires: list[int] = Field(default_factory=list)
    templates: list[int] = Field(default_factory=list)
    split: bool = False


class BatchSettings(BaseModel):
    dataset_id: int
    school_roster_id: int | None = None
    questionnaire_stages: dict[int, QuestionnaireStage] = Field(default_factory=dict)
    indicator_id: int | None = None
    mode: Literal["province", "custom", "all", "levels"] = "province"
    enabled_levels: list[Level] = Field(default_factory=list, max_length=4)
    org_ids: list[int] = Field(default_factory=list, max_length=100000)
    excluded: list[int] = Field(default_factory=list, max_length=100000)
    levels: dict[Level, LevelSettings] = Field(default_factory=dict)
    overrides: dict[int, int] = Field(default_factory=dict)
    ai_orgs: list[int] = Field(default_factory=list, max_length=100000)
    automatic: bool = False
    indicator_assets: list[int] = Field(default_factory=list, max_length=30)


class IndicatorRule(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    method: Literal["count", "rate", "multiple_rate", "mean"]
    question_id: int
    options: list[str] = Field(default_factory=list, max_length=100)
    denominator: Literal["answered", "sample"] = "answered"
    source: str = Field(default="", max_length=500)


def scope_catalog(db, schools: dict | None = None) -> dict:
    organizations = [dict(row) for row in db.execute("SELECT * FROM org ORDER BY path,id")]
    school_samples = {}
    for row in db.execute("SELECT s.org,s.questionnaire FROM sample s JOIN org o ON o.id=s.org WHERE o.level='school'"):
        school_samples.setdefault(row["org"], []).append(row["questionnaire"])
    for org in organizations:
        if org["level"] == "school":
            org["school_stage"] = school_stage(org, schools or {})
            org["questionnaire_ids"] = school_samples.get(org["id"], [])
    return {
        "organizations": organizations,
        "questionnaires": [{**dict(row), "school_stage": infer_stage(row["name"])}
                           for row in db.execute("SELECT * FROM questionnaire ORDER BY name,id")],
        "notices": [dict(row) for row in db.execute("SELECT * FROM notice")],
        "total": db.execute("SELECT COALESCE(SUM(total),0) FROM sample WHERE org=1").fetchone()[0],
    }


def preview_reports(db, settings: BatchSettings, templates: dict[int, dict],
                    schools: dict | None = None) -> tuple[dict, list[dict]]:
    orgs = {row["id"]: dict(row) for row in db.execute("SELECT * FROM org ORDER BY path,id")}
    questionnaires = {row["id"]: row["name"] for row in db.execute("SELECT * FROM questionnaire ORDER BY name,id")}
    if settings.questionnaire_stages.keys() - questionnaires.keys():
        raise ValueError("适用学段配置包含已失效的问卷")
    stages = {qid: infer_stage(name) for qid, name in questionnaires.items()}
    for qid, stage in settings.questionnaire_stages.items():
        if stages[qid] not in ("any", stage):
            raise ValueError(f"「{questionnaires[qid]}」的学段由年级确定")
        stages[qid] = stage
    selected = set(settings.org_ids)
    if settings.mode == "province":
        selected = {1}
    elif settings.mode == "all":
        selected = set(orgs)
    elif settings.mode == "levels":
        selected = {org_id for org_id, org in orgs.items() if org["level"] in settings.enabled_levels}
    selected.difference_update(settings.excluded)
    if selected - orgs.keys():
        raise ValueError("所选组织已失效，请刷新导入结果")
    if not selected:
        raise ValueError("至少选择一个出报组织")
    known_ai = set(settings.ai_orgs) & selected
    counts = {(row["org"], row["questionnaire"]): row["total"]
              for row in db.execute("SELECT * FROM sample") if row["org"] in selected}
    counters = Counter()
    groups = {level: {"organizations": 0, "responses": 0, "reports": 0, "skipped": 0} for level in LEVELS}
    rows = []
    jobs = []
    for org_id, org in orgs.items():
        if org_id not in selected:
            continue
        level = org["level"]
        config = settings.levels.get(level, LevelSettings())
        chosen = list(dict.fromkeys(config.questionnaires)) or list(questionnaires)
        if set(chosen) - questionnaires.keys():
            raise ValueError(f"{LABELS[level]}级选择了已失效的问卷")
        allowed = list(dict.fromkeys(config.templates))
        if any(templates.get(template, {}).get("level") != level for template in allowed):
            raise ValueError(f"{LABELS[level]}级模板须从本级已配置模板中选择")
        override = settings.overrides.get(org_id)
        if override is not None and override not in allowed:
            raise ValueError(f"「{org['path']}」手动指定的模板不在候选列表")
        assignment = None
        if allowed:
            assignment = override if override is not None else allowed[counters[level] % len(allowed)]
            counters[level] += 1
        stage = school_stage(org, schools or {})
        sample = {qid: counts.get((org_id, qid), 0) for qid in chosen}
        incompatible = {qid: count for qid, count in sample.items()
                        if stage in ("primary", "middle") and stages[qid] not in ("any", stage)}
        available = [qid for qid in chosen if sample[qid] and qid not in incompatible]
        split = config.split and not (settings.automatic and level == "province")
        combined = available if level == "school" else chosen
        variants = ([[qid] for qid in available] if split else [combined]) if available else []
        subtotal = groups[level]
        subtotal["organizations"] += 1
        subtotal["responses"] += sum(count for qid, count in sample.items() if qid not in incompatible)
        subtotal["reports"] += len(variants)
        subtotal["skipped"] += not bool(variants)
        rows.append({**org, "school_stage": stage, "template_id": assignment, "samples": sample,
                     "reports": len(variants), "incompatible_questionnaires": incompatible,
                     "skipped_questionnaires": [qid for qid in chosen if not sample[qid] and qid not in incompatible]})
        jobs.extend({"org_id": org_id, "org_level": level, "org_path": org["path"], "template_id": assignment,
                     "school_stage": stage,
                     "questionnaires": variant, "include_ai": settings.automatic or org_id in known_ai}
                    for variant in variants)
    preview = {"levels": groups, "reports": len(jobs), "organizations": rows,
               "questionnaires": questionnaires, "ai_reports": sum(job["include_ai"] for job in jobs)}
    return preview, jobs


def template_preview(path: Path) -> dict:
    doc = Document(path)
    paragraphs = [{"index": i, "text": entry.text[:240], "style": entry.style.name}
                  for i, entry in enumerate(doc.paragraphs) if entry.text.strip()]
    return {
        "paragraphs": paragraphs,
        "title_index": next((row["index"] for row in paragraphs if row["text"] == TITLE_TOKEN), None),
        "body_index": next((row["index"] for row in paragraphs if row["text"] == BODY_TOKEN), None),
    }


def question_catalog(db, questionnaire_id=None) -> list[dict]:
    return [dict(row) for row in db.execute(
        "SELECT q.*, s.name AS questionnaire_name FROM question q JOIN questionnaire s ON s.id=q.questionnaire "
        "WHERE q.kind!='ignore' AND (? IS NULL OR q.questionnaire=?) ORDER BY q.questionnaire,q.id",
        (questionnaire_id, questionnaire_id),
    )]


def question_options(db, question_id: int) -> list[str]:
    return [row[0] for row in db.execute("SELECT option FROM choice WHERE org=1 AND question=? ORDER BY n DESC,option",
                                        (question_id,))]


def check_rules(db, rules: list[IndicatorRule]) -> None:
    questions = {row["id"]: row for row in question_catalog(db)}
    for rule in rules:
        question = questions.get(rule.question_id)
        if question is None:
            raise ValueError(f"「{rule.name}」需要选择当前答卷中的题目")
        kind = question["kind"]
        if rule.method == "mean" and kind != "number":
            raise ValueError(f"「{rule.name}」均值只适用于数值题")
        if rule.method in {"rate", "multiple_rate"} and not rule.options:
            raise ValueError(f"「{rule.name}」请选择计算比例的选项")
        if rule.method == "rate" and kind != "single":
            raise ValueError(f"「{rule.name}」比例适用于单选题，多选题请选择勾选率")
        if rule.method == "multiple_rate" and (kind != "multiple" or len(rule.options) != 1):
            raise ValueError(f"「{rule.name}」多选勾选率需要指定一个选项")
        if kind == "multiple" and len(rule.options) > 1:
            raise ValueError(f"「{rule.name}」多选计数需要指定一个选项")
        if len(set(rule.options)) != len(rule.options) or set(rule.options) - set(question_options(db, rule.question_id)):
            raise ValueError(f"「{rule.name}」包含重复或不存在的选项")


def report_data(db, org_id: int, questionnaire_ids: list[int], rules: list[dict]) -> list[dict]:
    result = []
    for questionnaire_id in questionnaire_ids:
        survey = db.execute("SELECT name FROM questionnaire WHERE id=?", (questionnaire_id,)).fetchone()
        sample = db.execute("SELECT total FROM sample WHERE org=? AND questionnaire=?",
                            (org_id, questionnaire_id)).fetchone()
        questions = {}
        for row in db.execute(
            "SELECT q.id,q.title,q.kind,a.n,a.numeric_n,a.value_sum FROM question q "
            "LEFT JOIN answer a ON a.org=? AND a.question=q.id "
            "WHERE q.questionnaire=? AND q.kind!='ignore' ORDER BY q.id", (org_id, questionnaire_id),
        ):
            questions[row["id"]] = {**dict(row), "options": {}}
        if sample:
            for choice in db.execute(
                "SELECT c.question,c.option,c.n FROM choice c JOIN question q ON q.id=c.question "
                "WHERE c.org=? AND q.questionnaire=? ORDER BY c.n DESC,c.option", (org_id, questionnaire_id),
            ):
                questions[choice["question"]]["options"][choice["option"]] = choice["n"]
        indicators = []
        for rule in rules:
            question = questions.get(rule["question_id"])
            if question:
                indicators.append(calculate_indicator(rule, question, sample[0] if sample else 0))
        result.append({"id": questionnaire_id, "name": survey[0], "sample": sample[0] if sample else 0,
                       "questions": list(questions.values()), "indicators": indicators})
    return result


def calculate_indicator(rule: dict, question: dict, sample: int) -> dict:
    n = question["n"] or 0
    total = sum(question["options"].get(option, 0) for option in rule["options"]) if rule["options"] else n
    denominator = sample if rule["denominator"] == "sample" else n
    method = rule["method"]
    if method == "mean":
        numeric_n = question["numeric_n"] or 0
        display = f"{question['value_sum'] / numeric_n:.2f}（有效数值 {numeric_n:,} 条）" if numeric_n else "无有效数值"
    elif method == "count":
        display = f"{total:,} 条（有效作答 {n:,} 条）" if sample else "无答卷"
    else:
        display = f"{total / denominator:.1%}（{total:,} / {denominator:,}）" if denominator else "分母为零"
    return {"name": rule["name"], "value": display, "question": question["title"], "source": rule.get("source", "")}


def safe_filename(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text).strip(". ")
    return text[:85].strip(". ") or "问卷报告"


def default_styles(doc):
    if sys.platform == "darwin":
        east_asia = "Songti SC"
    elif sys.platform == "win32":
        east_asia = "SimSun"
    else:
        east_asia = "Noto Sans CJK SC"
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3"):
        style = doc.styles[name]
        style.font.color.rgb = RGBColor.from_string("18232C")
        style.font.size = Pt({"Normal": 11, "Title": 24, "Heading 1": 16, "Heading 2": 13, "Heading 3": 11}[name])
        properties = style.element.get_or_add_rPr()
        fonts = properties.get_or_add_rFonts()
        fonts.attrib.pop(qn("w:eastAsiaTheme"), None)
        fonts.set(qn("w:eastAsia"), east_asia)
        language = properties.find(qn("w:lang"))
        if language is None:
            language = OxmlElement("w:lang")
            properties.append(language)
        language.set(qn("w:eastAsia"), "zh-CN")
    title = doc.styles["Title"].element.get_or_add_pPr()
    border = title.find(qn("w:pBdr"))
    if border is not None:
        title.remove(border)


def new_document(level, title, template):
    if template is None:
        doc = Document()
        default_styles(doc)
        section = doc.sections[0]
        section.top_margin = section.bottom_margin = Cm(2.25)
        section.left_margin = section.right_margin = Cm(2.5)
        heading = doc.add_paragraph(title, style="Title")
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER if level in ("province", "city") else WD_ALIGN_PARAGRAPH.LEFT
        heading.runs[0].font.color.rgb = RGBColor.from_string("17212B")
        heading.runs[0].font.size = Pt({"province": 25, "city": 23, "county": 21, "school": 20}[level])
        return doc, None
    path, positions = template
    doc = Document(path)
    paragraphs = doc.paragraphs
    title_paragraph = paragraphs[positions["title_index"]]
    body_paragraph = paragraphs[positions["body_index"]]
    if title_paragraph.runs:
        title_paragraph.runs[0].text = title
        for run in title_paragraph.runs[1:]:
            run.text = ""
    else:
        title_paragraph.add_run(title)
    return doc, body_paragraph


def chart_image(options, denominator):
    width, row_height = 585, 32
    image = Image.new("RGB", (width, row_height * len(options)), "white")
    draw = ImageDraw.Draw(image)
    for index, (_, count) in enumerate(options, 1):
        y = (index - 1) * row_height
        proportion = count / denominator
        draw.text((4, y + 9), str(index), fill="#24303d")
        draw.rectangle((25, y + 6, 455, y + 25), fill="#E9EEF2")
        if count:
            draw.rectangle((25, y + 6, 25 + round(430 * proportion), y + 25), fill="#365D77")
        draw.text((465, y + 9), f"{proportion:.1%}", fill="#24303d")
    stream = io.BytesIO()
    image.save(stream, "PNG")
    stream.seek(0)
    return stream


def build_word(output: Path, *, title: str, org: str, level: str, year: int, surveys: list[dict],
               template=None, ai_text: str = "") -> None:
    doc, anchor = new_document(level, title, template)
    old_nodes = set(doc._element.body)
    doc.add_paragraph(f"{year} 年度  |  {LABELS[level]}级  |  {org}")
    doc.add_heading("答卷概况", level=1)
    for survey in surveys:
        doc.add_paragraph(f"{survey['name']}  ·  {survey['sample']:,} 条答卷" if survey["sample"]
                          else f"{survey['name']}  ·  暂无答卷")
    if ai_text:
        doc.add_heading("综合解读", level=1)
        for line in ai_text.splitlines():
            if line.strip():
                doc.add_paragraph(line.strip())
    for survey in surveys:
        doc.add_heading(survey["name"], level=1)
        if not survey["sample"]:
            doc.add_paragraph("本范围暂无这类问卷的答卷。")
            continue
        if survey["indicators"]:
            doc.add_heading("年度指标", level=2)
            for indicator in survey["indicators"]:
                source = f"。指标来源 {indicator['source']}" if indicator["source"] else ""
                doc.add_paragraph(f"{indicator['name']}  {indicator['value']}。对应题目 {indicator['question']}{source}")
        eligible = [q for q in survey["questions"] if q["n"] and len(q["options"]) >= 2]
        if eligible:
            chosen = max(eligible, key=lambda q: (q["n"], len(q["options"]), -q["id"]))
            main = list(chosen["options"].items())[:5]
            leader, count = main[0]
            leaders = sum(total == count for total in chosen["options"].values())
            doc.add_heading("主要发现", level=2)
            if leaders == 1:
                verb = "勾选" if chosen["kind"] == "multiple" else "选择"
                doc.add_paragraph(f"「{chosen['title']}」有 {chosen['n']:,} 条有效作答。"
                                  f"「{leader}」的{verb}数最高，为 {count:,} 条（{count / chosen['n']:.1%}）。")
            else:
                doc.add_paragraph(f"「{chosen['title']}」有 {chosen['n']:,} 条有效作答。"
                                  f"最高频次为 {count:,} 条，共 {leaders} 个选项并列。")
            doc.add_heading("选项分布", level=2)
            doc.add_paragraph(f"{chosen['title']}（有效作答 {chosen['n']:,} 条）")
            doc.add_picture(chart_image(main, chosen["n"]), width=Cm(12.5))
            for index, (option, count) in enumerate(main, 1):
                doc.add_paragraph(f"{index}. {option}  {count:,} 条（{count / chosen['n']:.1%}）")
        doc.add_heading("逐题统计", level=2)
        for question in survey["questions"]:
            doc.add_heading(question["title"], level=3)
            n = question["n"]
            if not n:
                doc.add_paragraph("无有效作答")
                continue
            if question["kind"] == "number":
                doc.add_paragraph(f"有效数值 {n:,} 条，均值 {question['value_sum'] / question['numeric_n']:.2f}。")
                continue
            unit = "勾选率，可合计超过 100%" if question["kind"] == "multiple" else "选项占比"
            doc.add_paragraph(f"有效作答 {n:,} 条。{unit}以本题有效作答为分母。")
            for option, count in question["options"].items():
                doc.add_paragraph(f"{option}  {count:,} 条（{count / n:.1%}）")
    if anchor is not None:
        for node in list(doc._element.body):
            if node not in old_nodes:
                anchor._element.addprevious(node)
        anchor._element.getparent().remove(anchor._element)
    doc.save(output)
