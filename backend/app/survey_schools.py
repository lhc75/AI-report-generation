"""Read school rosters and resolve questionnaire eligibility for school reports."""

import io
import re
from typing import Literal

from openpyxl import Workbook, load_workbook

from .survey_data import label

SchoolStage = Literal["primary", "middle"]
QuestionnaireStage = Literal["primary", "middle", "any"]
STAGE_NAMES = {"primary": "小学", "middle": "初中", "unknown": "学段未匹配"}
ROSTER_COLUMNS = {
    "id": ("id", "学校id", "学校编号", "学校代码"),
    "area": ("area", "区县", "区/县"),
    "city": ("city", "市州", "市/州"),
    "school": ("school", "学校", "学校名称"),
    "type": ("type", "学段", "学校类型"),
}
GRADE_STAGE = re.compile(r"(?<![0-9])([1-9一二三四五六七八九])年级")


def read_roster(stream) -> dict:
    book = load_workbook(stream, read_only=True, data_only=True)
    try:
        rows = book.active.values
        headers = [label(value).casefold() for value in next(rows, ())]
        columns = {}
        for name, aliases in ROSTER_COLUMNS.items():
            matches = [i for i, header in enumerate(headers) if header in aliases]
            if len(matches) != 1:
                raise ValueError(f"学校信息表第一行须包含且只包含一个 {name}（{aliases[1]}）列")
            columns[name] = matches[0]
        schools = {}
        counts = {"primary": 0, "middle": 0}
        for number, row in enumerate(rows, 2):
            values = {name: label(row[index]) if index < len(row) else "" for name, index in columns.items()}
            if not any(values.values()):
                continue
            if not all(values.values()):
                raise ValueError(f"学校信息表第 {number} 行缺少学校 ID、辖区、学校名称或学段")
            school_id = values.pop("id")
            stage = {"小学": "primary", "初中": "middle", "初级中学": "middle"}.get(values.pop("type"))
            if stage is None:
                raise ValueError(f"学校信息表第 {number} 行的学段须为小学或初中")
            record = {**values, "stage": stage}
            if school_id in schools:
                if schools[school_id] != record:
                    raise ValueError(f"学校信息表第 {number} 行的学校 ID {school_id} 与前面的学校信息冲突")
                continue
            schools[school_id] = record
            counts[stage] += 1
        if not schools:
            raise ValueError("学校信息表没有可导入的学校")
        return {"schools": schools, "counts": counts}
    finally:
        book.close()


def roster_template() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "学校信息"
    sheet.append(list(ROSTER_COLUMNS))
    sheet.column_dimensions["A"].number_format = "@"
    sheet.column_dimensions["D"].width = 30
    sheet.freeze_panes = "A2"
    result = io.BytesIO()
    book.save(result)
    return result.getvalue()


def infer_stage(name: str) -> QuestionnaireStage:
    if "学生" not in name:
        return "any"
    stages = {"primary" if grade in "123456一二三四五六" else "middle"
              for grade in GRADE_STAGE.findall(label(name))}
    return stages.pop() if len(stages) == 1 else "any"


def school_stage(org: dict, schools: dict) -> str | None:
    if org["level"] == "school":
        return schools.get(label(org["school_code"]), {}).get("stage", "unknown")
    return None


def school_path(path: str, stage: str | None) -> str:
    return f"{path}（{STAGE_NAMES[stage]}）" if stage else path
