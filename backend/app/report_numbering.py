"""导出前的确定性编号与格式规范化。

模型只负责内容与语义；图号、表号、题号格式由这里统一生成和改写，
不再依赖模型自觉遵守，也不会因为模型写法不一致而阻塞导出。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .report_text import _chinese_index

_PART_RE = re.compile(r"第\s*([零一二三四五六七八九十百千]+)\s*部分")
_SECTION_RE = re.compile(r"^\s*([零一二三四五六七八九十百千]+)\s*、")
_FIG_TOKEN = re.compile(r"图\s*\d+(?:\s*[\.\-]\s*\d+)*")
_TABLE_TOKEN = re.compile(r"表\s*\d+")
_QUESTION_NUMBER = re.compile(r"第\s*([0-9]{1,4})\s*题")

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def chinese_to_int(value: str) -> int | None:
    """把“七”“十二”“一百零二”转成整数；无法解析返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    total = 0
    current = 0
    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            total += (current or 1) * unit
            current = 0
        else:
            return None
    return total + current


def normalize_question_numbers(value: str) -> str:
    """把正文/表格/图内文字中的“第11题”统一为“第十一题”。"""
    if not isinstance(value, str):
        return value

    def _replace(match: re.Match) -> str:
        number = int(match.group(1))
        return f"第{_chinese_index(number)}题"

    return _QUESTION_NUMBER.sub(_replace, value)


def _normalize_strings(value: Any, skip_keys: set[str] | None = None) -> Any:
    if isinstance(value, str):
        return normalize_question_numbers(value)
    if isinstance(value, list):
        return [_normalize_strings(item, skip_keys) for item in value]
    if isinstance(value, dict):
        return {
            key: (item if skip_keys and key in skip_keys else _normalize_strings(item, skip_keys))
            for key, item in value.items()
        }
    return value


def normalize_content_questions(content: str, content_type: str) -> str:
    """对已保存的 text/table/image JSON 做题号规范化；解析失败原样返回。"""
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return normalize_question_numbers(content) if isinstance(content, str) else content
    skip = {"values", "counts", "percentages", "denominator", "multi_select", "layout"} \
        if content_type == "image" else None
    return json.dumps(_normalize_strings(data, skip), ensure_ascii=False)


def heading_numbers(by_id: dict[int, Any], task) -> tuple[int | None, int | None]:
    """沿标题祖先取“部分号、节号”；节号取最近的“一、二、…”标题。"""
    part = section = None
    parent_id = getattr(task, "parent_id", None)
    visited: set[int] = set()
    while parent_id and parent_id not in visited:
        visited.add(parent_id)
        parent = by_id.get(parent_id)
        if parent is None:
            break
        title = str(getattr(parent, "title", "") or "")
        if part is None:
            match = _PART_RE.search(title)
            if match:
                part = chinese_to_int(match.group(1))
        if section is None:
            match = _SECTION_RE.match(title)
            if match:
                section = chinese_to_int(match.group(1))
        parent_id = getattr(parent, "parent_id", None)
    return part, section


def figure_number_map(tasks: list) -> dict[int, str]:
    """按“部分-节-顺序”给图片任务分配图号。"""
    by_id = {task.id: task for task in tasks}
    groups: dict[tuple[int | None, int | None], list[int]] = {}
    ordered = sorted(tasks, key=lambda item: (getattr(item, "sort", 0), item.id))
    for task in ordered:
        if getattr(task, "task_type", "") != "image":
            continue
        key = heading_numbers(by_id, task)
        groups.setdefault(key, []).append(task.id)
    numbers: dict[int, str] = {}
    for (part, section), task_ids in groups.items():
        for index, task_id in enumerate(task_ids, start=1):
            if part and section:
                numbers[task_id] = f"图{part}.{section}-{index}"
            elif part:
                numbers[task_id] = f"图{part}-{index}"
            else:
                numbers[task_id] = f"图{index}"
    return numbers


def table_number_map(tasks: list) -> dict[int, int]:
    ordered = sorted(
        (task for task in tasks if getattr(task, "task_type", "") == "table"),
        key=lambda item: (getattr(item, "sort", 0), item.id))
    return {task.id: index for index, task in enumerate(ordered, start=1)}


def strip_figure_numbers(value: str) -> str:
    """删除文字中的图号，并清理残留的分隔符。"""
    if not isinstance(value, str):
        return value
    cleaned = _FIG_TOKEN.sub("", value)
    return re.sub(r"^[\s:：、,，\-]+", "", cleaned).strip()


def collect_figure_numbers(content: str) -> set[str]:
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return set()
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.update(match.group(0) for match in _FIG_TOKEN.finditer(value))
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    walk(data)
    return found


def renumber_chart_data(data: dict, number: str) -> tuple[dict, set[str]]:
    """顶层 caption 使用系统图号；子图标题与图内文字去掉图号。"""
    if not isinstance(data, dict):
        return data, set()
    result = json.loads(json.dumps(data, ensure_ascii=False))
    old_numbers: set[str] = set()

    def clean(value: str) -> str:
        old_numbers.update(match.group(0) for match in _FIG_TOKEN.finditer(value))
        return strip_figure_numbers(value)

    caption = result.get("caption")
    base = clean(caption) if isinstance(caption, str) else ""
    if not base:
        title = result.get("title")
        base = clean(title) if isinstance(title, str) else ""
    if base:
        result["caption"] = f"{number} {base}".strip()
    else:
        result.pop("caption", None)

    charts = result.get("charts")
    if isinstance(charts, list):
        for chart in charts:
            if not isinstance(chart, dict):
                continue
            for key in ("caption", "title", "note", "source"):
                if isinstance(chart.get(key), str):
                    chart[key] = clean(chart[key])
            if isinstance(chart.get("notes"), list):
                chart["notes"] = [clean(item) if isinstance(item, str) else item
                                  for item in chart["notes"]]
    return result, old_numbers


def collect_table_number(content: str) -> set[str]:
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return set()
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.update(match.group(0) for match in _TABLE_TOKEN.finditer(value))
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    walk(data)
    return found


def renumber_table_content(content: str, number: int) -> tuple[str, set[str]]:
    """把表格标题改为系统表号；保留其余字段与数据。"""
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content, set()
    if not isinstance(data, dict):
        return content, set()
    old_numbers: set[str] = set()

    def clean(value: str) -> str:
        old_numbers.update(match.group(0) for match in _TABLE_TOKEN.finditer(value))
        return re.sub(r"^[\s:：、,，\-]+", "", _TABLE_TOKEN.sub("", value)).strip()

    table = data.get("table")
    target = table if isinstance(table, dict) else data
    title = target.get("title")
    if isinstance(title, str) and title.strip():
        base = clean(title)
        if base:
            target["title"] = f"表{number} {base}".strip()
        else:
            target["title"] = f"表{number}"
    return json.dumps(data, ensure_ascii=False), old_numbers


def unique_reference_map(pairs: dict[str, set[str]]) -> dict[str, str]:
    """只保留一一对应的旧号→新号映射，避免歧义替换。"""
    return {old: next(iter(news)) for old, news in pairs.items() if len(news) == 1}


def remap_references(value: str, mapping: dict[str, str]) -> str:
    if not isinstance(value, str) or not mapping:
        return value
    result = value
    for old, new in sorted(mapping.items(), key=lambda item: -len(item[0])):
        result = result.replace(old, new)
    return result


def normalize_row_content(content: str, content_type: str, *,
                          figure_number: str | None = None,
                          table_number: int | None = None,
                          figure_refs: dict[str, str] | None = None,
                          table_refs: dict[str, str] | None = None) -> tuple[str, set[str]]:
    """对单个模块执行确定性编号、题号规范化与引用重映射。"""
    if content_type == "image":
        try:
            data = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return content, set()
        old_numbers: set[str] = set()
        if figure_number:
            data, old_numbers = renumber_chart_data(data, figure_number)
        data = _normalize_strings(
            data, {"values", "counts", "percentages", "denominator", "multi_select", "layout"})
        text = json.dumps(data, ensure_ascii=False)
        for old, new in (figure_refs or {}).items():
            text = text.replace(old, new)
        return text, old_numbers
    if content_type == "table":
        new_content, old_numbers = (renumber_table_content(content, table_number)
                                    if table_number else (content, set()))
        try:
            data = json.loads(new_content)
        except (TypeError, json.JSONDecodeError):
            return normalize_question_numbers(new_content), old_numbers
        data = _normalize_strings(data)
        text = json.dumps(data, ensure_ascii=False)
        for old, new in (table_refs or {}).items():
            text = text.replace(old, new)
        return text, old_numbers
    # text
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        text = normalize_question_numbers(content)
        for old, new in (figure_refs or {}).items():
            text = text.replace(old, new)
        for old, new in (table_refs or {}).items():
            text = text.replace(old, new)
        return text, set()
    data = _normalize_strings(data)
    if isinstance(data, dict) and isinstance(data.get("content"), str):
        body = data["content"]
        for old, new in (figure_refs or {}).items():
            body = body.replace(old, new)
        for old, new in (table_refs or {}).items():
            body = body.replace(old, new)
        data["content"] = body
    return json.dumps(data, ensure_ascii=False), set()
