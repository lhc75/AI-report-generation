"""Import answer exports into an immutable, scope-indexed statistics file."""

import math
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from contextlib import closing
from itertools import chain, islice
from functools import lru_cache
from pathlib import Path
from typing import Literal

from openpyxl import load_workbook
from pydantic import BaseModel, Field

LEVELS = ("province", "city", "county", "school")
LABELS = dict(zip(LEVELS, ("省", "市州", "区县", "学校")))
Level = Literal["province", "city", "county", "school"]
ALIASES = {
    "city": ("市/州", "市州", "市", "地市", "城市"),
    "county": ("区县", "区/县", "县区", "区", "县"),
    "school": ("学校", "学校名称"),
    "school_id": ("学校ID", "学校代码", "学校编号"),
    "questionnaire": ("问卷", "问卷名称", "问卷类型"),
}
METADATA = {name for names in ALIASES.values() for name in names} | {
    "账号", "姓名", "提交时间", "学号", "身份证号", "联系方式", "省", "省份",
}
PRIVATE_HEADER = re.compile(r"姓名|学号|账号|电话|联系方式|身份证|邮箱|住址|填写.*学校|任教年级.*班级|（填写内容）")
OPTION_MARKER = re.compile(r"(?:^|[;；/|])\s*([A-ZＡ-Ｚ])[.．、:：)）]")
MAX_OPTIONS = 100
UNANSWERED = {"", "未作答", "未填写", "未回答", "（未作答）", "(未作答)"}


class QuestionRule(BaseModel):
    kind: Literal["auto", "single", "multiple", "number", "ignore"] = "auto"
    separator: str = Field(default="；", min_length=1, max_length=12)


class SheetMapping(BaseModel):
    sheet: str
    header: int = Field(default=1, ge=1, le=200)
    fields: dict[str, int] = Field(default_factory=dict)
    questionnaire: str = Field(default="", max_length=255)
    questions: dict[int, QuestionRule] = Field(default_factory=dict)


def clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def label(value) -> str:
    return unicodedata.normalize("NFKC", clean(value))


def detect_fields(row) -> dict[str, int]:
    names = [label(value).casefold() for value in row]
    result = {}
    for field, aliases in ALIASES.items():
        matches = {label(alias).casefold() for alias in aliases}
        indices = [i for i, value in enumerate(names) if value in matches]
        if len(indices) == 1:
            result[field] = indices[0]
    return result


def inspect_workbook(path: Path, filename: str) -> list[dict]:
    with path.open("rb") as stream, closing(load_workbook(stream, read_only=True, data_only=True)) as book:
        result = []
        for sheet in book.worksheets:
            preview = [[clean(value)[:250] for value in row]
                       for row in islice(sheet.values, 12)]
            candidates = [detect_fields(row) for row in preview]
            if not candidates:
                continue
            index = max(range(len(candidates)), key=lambda i: (len(candidates[i]), sum(bool(x) for x in preview[i])))
            result.append({
                "sheet": sheet.title, "header": index + 1,
                "fields": candidates[index], "questionnaire": Path(filename).stem,
                "headers": preview[index], "row_hint": max(0, (sheet.max_row or 0) - index - 1),
            })
    if not result:
        raise ValueError("工作簿没有可导入的工作表")
    return result


def infer_rule(values: list, header: str) -> QuestionRule:
    if not values:
        return QuestionRule(kind="ignore")
    if all(as_number(value) is not None for value in values):
        return QuestionRule(kind="number")
    for separator in ("；", ";", "|", "/"):
        if any(len(OPTION_MARKER.findall(val)) > 1 and separator in val for val in values):
            return QuestionRule(kind="multiple", separator=separator)
    if "多选" in header:
        for separator in ("；", ";", "|", "/", "、"):
            if any(separator in val for val in values):
                return QuestionRule(kind="multiple", separator=separator)
        return QuestionRule(kind="multiple")
    combinations = {value for value in values if "；" in value}
    tokens = Counter(token for value in combinations for token in answer_options(value, "；"))
    if (len(combinations) >= 2 and 1 < len(tokens) <= 25
            and any(count > 1 for count in tokens.values())
            and all(len(token) <= 100 and not any(mark in token for mark in ("：", '"'))
                    for token in tokens)
            and any(as_number(token) is None for token in tokens)):
        return QuestionRule(kind="multiple")
    if max(map(len, values)) <= 120 and len(set(values)) <= min(MAX_OPTIONS, max(12, len(values) // 2)):
        return QuestionRule(kind="single")
    return QuestionRule(kind="ignore")


@lru_cache(maxsize=16384)
def answer_options(value: str, separator: str = "") -> tuple[str, ...]:
    values = value.split(separator) if separator else (value,)
    options = set()
    for element in values:
        option = element.strip()
        if option in UNANSWERED or len(option) > 120:
            continue
        option = re.sub(r"^((?:[A-Z][.、]\s*)?其他)\s*[:：].*$", r"\1", option)
        options.add(option)
    return tuple(sorted(options))


def as_number(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and abs(number) <= 1e100 else None


SCHEMA = """
CREATE TABLE org(id INTEGER PRIMARY KEY, level TEXT NOT NULL, name TEXT NOT NULL,
                 parent INTEGER, school_code TEXT, path TEXT NOT NULL);
CREATE TABLE questionnaire(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE question(id INTEGER PRIMARY KEY, questionnaire INTEGER NOT NULL,
                      title TEXT NOT NULL, kind TEXT NOT NULL);
CREATE TABLE sample(org INTEGER NOT NULL, questionnaire INTEGER NOT NULL, total INTEGER NOT NULL,
                    PRIMARY KEY(org, questionnaire)) WITHOUT ROWID;
CREATE TABLE answer(org INTEGER NOT NULL, question INTEGER NOT NULL, n INTEGER NOT NULL,
                    numeric_n INTEGER NOT NULL, value_sum REAL NOT NULL,
                    PRIMARY KEY(org, question)) WITHOUT ROWID;
CREATE TABLE choice(org INTEGER NOT NULL, question INTEGER NOT NULL, option TEXT NOT NULL,
                    n INTEGER NOT NULL, PRIMARY KEY(org, question, option)) WITHOUT ROWID;
CREATE TABLE notice(source TEXT NOT NULL, sheet TEXT NOT NULL, issue TEXT NOT NULL, total INTEGER NOT NULL);
"""


def mapped_sheet(sheet, mapping: SheetMapping, source: str):
    rows = sheet.iter_rows(min_row=mapping.header, values_only=True)
    headers = next(rows, ())
    if not headers:
        raise ValueError(f"{source} / {mapping.sheet} 找不到表头")
    if set(mapping.fields) - set(ALIASES) or any(i < 0 or i >= len(headers) for i in mapping.fields.values()):
        raise ValueError(f"{source} / {mapping.sheet} 字段映射超出表头范围")
    if len(set(mapping.fields.values())) != len(mapping.fields):
        raise ValueError("同一列只能映射一个组织或问卷字段")
    if set(mapping.questions) - set(range(len(headers))):
        raise ValueError("题型映射超出表头范围")
    samples = list(islice(rows, 256))
    plan = []
    for index, value in enumerate(headers):
        title = clean(value)
        if (not title or index in mapping.fields.values() or label(title) in METADATA
                or PRIVATE_HEADER.search(title)):
            continue
        rule = mapping.questions.get(index, QuestionRule())
        if rule.kind == "auto":
            values = [clean(row[index]) for row in samples if len(row) > index]
            rule = infer_rule([item for item in values if item not in UNANSWERED], title)
        if rule.kind != "ignore":
            plan.append((index, title[:400], rule))
    return plan, chain(samples, rows)


def response_scope(row, mapping, field_indices):
    city, county, school, code, name = [label(row[i]) if i is not None and i < len(row) else ""
                                       for i in field_indices]
    if "questionnaire" not in mapping.fields:
        name = mapping.questionnaire.strip()
    return city, county, school, code, name


class Importer:
    def __init__(self, db: sqlite3.Connection, province: str):
        self.db = db
        self.organizations = {("province", None, province): 1}
        self.paths = {1: province}
        self.school_paths = {}
        self.questionnaires = {}
        db.execute("INSERT INTO org VALUES(1, 'province', ?, NULL, NULL, ?)", (province, province))

    def org(self, level, parent, name, code=None) -> int:
        key = (level, parent, code or name)
        if key not in self.organizations:
            path = f"{self.paths[parent]} / {name}"
            org_id = self.db.execute(
                "INSERT INTO org(level, name, parent, school_code, path) VALUES (?, ?, ?, ?, ?)",
                (level, name, parent, code, path),
            ).lastrowid
            self.organizations[key] = org_id
            self.paths[org_id] = path
        return self.organizations[key]

    def leaf(self, city, county, school, code, notices) -> int:
        if not city:
            notices["缺少市州，答卷计入省级"] += 1
            return 1
        city_id = self.org("city", 1, city)
        if not county:
            notices["缺少区县，答卷计入市州及省级"] += 1
            return city_id
        county_id = self.org("county", city_id, county)
        if not school or not code:
            notices["缺少学校名称或学校 ID，答卷计入区县及上级"] += 1
            return county_id
        previous = self.school_paths.setdefault(code, (county_id, school))
        if previous != (county_id, school):
            raise ValueError(f"学校 ID {code} 对应多个组织路径，请修正来源后重新导入")
        return self.org("school", county_id, school, code)

    def questionnaire(self, name, plan) -> tuple[int, list[int]]:
        signature = tuple((title, rule.kind, rule.separator if rule.kind == "multiple" else "") for _, title, rule in plan)
        if name in self.questionnaires:
            previous, survey_id, question_ids = self.questionnaires[name]
            if previous != signature:
                raise ValueError(f"问卷「{name}」的题目或题型不一致，请为不同问卷设置不同名称")
            return survey_id, question_ids
        survey_id = self.db.execute("INSERT INTO questionnaire(name) VALUES (?)", (name,)).lastrowid
        question_ids = [
            self.db.execute("INSERT INTO question(questionnaire,title,kind) VALUES (?,?,?)",
                            (survey_id, title, rule.kind)).lastrowid
            for _, title, rule in plan
        ]
        self.questionnaires[name] = (signature, survey_id, question_ids)
        return survey_id, question_ids

    def import_sheet(self, sheet, mapping: SheetMapping, source: str):
        plan, rows = mapped_sheet(sheet, mapping, source)
        sample_count = Counter()
        answers = {}
        options = defaultdict(Counter)
        vocabulary = defaultdict(set)
        suppressed = set()
        notices = Counter()
        bound_surveys = {}
        bound_locations = {}
        field_indices = [mapping.fields.get(key) for key in ("city", "county", "school", "school_id", "questionnaire")]
        for row in rows:
            if not any(value is not None and value != "" for value in row):
                continue
            city, county, school, code, name = response_scope(row, mapping, field_indices)
            if not name:
                notices["缺少问卷类型，未计入统计"] += 1
                continue
            if len(name) > 255:
                raise ValueError("问卷名称超过 255 字符")
            location = (city, county, school, code)
            if all(location):
                org_id = bound_locations.get(location)
                if org_id is None:
                    org_id = bound_locations[location] = self.leaf(*location, notices)
            else:
                org_id = self.leaf(*location, notices)
            if name not in bound_surveys:
                bound_surveys[name] = self.questionnaire(name, plan)
            survey_id, question_ids = bound_surveys[name]
            sample_count[org_id, survey_id] += 1
            for (index, _title, rule), question_id in zip(plan, question_ids):
                value = clean(row[index]) if index < len(row) else ""
                if value in UNANSWERED:
                    continue
                key = (org_id, question_id)
                counts = answers.get(key)
                if counts is None:
                    counts = answers[key] = [0, 0, 0.0]
                if rule.kind == "number":
                    number = as_number(value)
                    if number is None:
                        continue
                    counts[0] += 1
                    counts[1] += 1
                    counts[2] += number
                else:
                    if question_id in suppressed:
                        continue
                    choices = answer_options(value, rule.separator if rule.kind == "multiple" else "")
                    if not choices:
                        continue
                    counts[0] += 1
                    vocabulary[question_id].update(choices)
                    if len(vocabulary[question_id]) > MAX_OPTIONS:
                        suppressed.add(question_id)
                        notices["不同文本取值过多，已省略其选项分布"] += 1
                        continue
                    options[key].update(choices)
        self.db.executemany(
            "INSERT INTO sample VALUES (?,?,?) ON CONFLICT DO UPDATE SET total=total+excluded.total",
            ((org, survey, count) for (org, survey), count in sample_count.items()),
        )
        self.db.executemany(
            "INSERT INTO answer VALUES (?,?,?,?,?) ON CONFLICT DO UPDATE SET n=n+excluded.n,"
            "numeric_n=numeric_n+excluded.numeric_n, value_sum=value_sum+excluded.value_sum",
            ((org, question, *counts) for (org, question), counts in answers.items() if counts[0]),
        )
        self.db.executemany(
            "INSERT INTO choice VALUES (?,?,?,?) ON CONFLICT DO UPDATE SET n=n+excluded.n",
            ((org, question, option, count) for (org, question), values in options.items()
             if question not in suppressed for option, count in values.items()),
        )
        self.db.executemany("INSERT INTO notice VALUES (?,?,?,?)",
                            ((source, sheet.title, key, count) for key, count in notices.items()))
        for question_id in suppressed:
            self.db.execute("DELETE FROM choice WHERE question=?", (question_id,))
            self.db.execute("UPDATE question SET kind='ignore' WHERE id=?", (question_id,))

    def roll_up(self):
        for level in reversed(LEVELS[1:]):
            for table, keys, values in (
                ("sample", ("questionnaire",), ("total",)),
                ("answer", ("question",), ("n", "numeric_n", "value_sum")),
                ("choice", ("question", "option"), ("n",)),
            ):
                dimensions = ", ".join(f"t.{column}" for column in keys)
                totals = ", ".join(f"SUM(t.{column})" for column in values)
                updates = ", ".join(f"{column}={table}.{column}+excluded.{column}" for column in values)
                self.db.execute(
                    f"INSERT INTO {table} SELECT o.parent, {dimensions}, {totals} "
                    f"FROM {table} t JOIN org o ON o.id=t.org WHERE o.level=? "
                    f"GROUP BY o.parent, {dimensions} ON CONFLICT DO UPDATE SET {updates}", (level,),
                )
        self.db.execute("DELETE FROM choice WHERE question IN (SELECT id FROM question WHERE kind='ignore')")


def build_index(destination: Path, province: str, sources: list[tuple[Path, str, list[SheetMapping]]], on_progress):
    temporary = destination.with_suffix(".working")
    temporary.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(temporary)) as db:
            db.executescript(SCHEMA)
            importer = Importer(db, province)
            for index, (path, name, mappings) in enumerate(sources, start=1):
                on_progress(index - 1, len(sources), name)
                with path.open("rb") as stream, closing(load_workbook(stream, read_only=True, data_only=True)) as book:
                    for mapping in mappings:
                        importer.import_sheet(book[mapping.sheet], mapping, name)
                db.commit()
            if not importer.questionnaires:
                raise ValueError("未找到可统计的答卷，请核对表头和问卷映射")
            on_progress(len(sources), len(sources), "正在汇总各级组织")
            importer.roll_up()
            db.commit()
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def open_index(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db
