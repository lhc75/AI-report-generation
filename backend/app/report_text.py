"""Format source names for readers without changing stored statistical evidence."""
from __future__ import annotations

from copy import deepcopy
import re


_SURVEY_ROLE = r"(?:小学生|中学生|大学生|教师|教育主管部门|教育主管)"
_QUESTION_LOCATOR = rf"(?P<role>{_SURVEY_ROLE})(?:问卷)?(?:原(?:问卷)?|原件)?第[0-9一二三四五六七八九十百、，—–至及]+题"


def formal_chart_title(value: str) -> str:
    """Show the population and topic; keep questionnaire locators in metadata.

    Only a complete prefix or parenthesized suffix is a locator. Numbers in a
    topic or literal option label are never removed by this function.
    """
    if not isinstance(value, str):
        return value
    prefix = re.fullmatch(rf"\s*{_QUESTION_LOCATOR}\s*[：:｜|]\s*(?P<topic>.+?)\s*", value)
    if prefix:
        return f"{prefix['role']}：{prefix['topic']}"
    suffix = re.fullmatch(rf"\s*(?P<topic>.+?)\s*[（(]\s*{_QUESTION_LOCATOR}\s*[）)]\s*", value)
    if suffix:
        topic, role = suffix['topic'], suffix['role']
        return topic if role in topic else f"{role}：{topic}"
    return value


def chart_caption_repeats_title(title: str, caption: str) -> bool:
    """Recognize a repeated topic, without guessing synonyms or dropping facts.

    Chinese chart titles commonly use either ``大学生：主题感知`` or
    ``大学生对主题的感知``. Only that grammatical variation is equivalent;
    another population, question, measure, or an explanatory caption is kept.
    """
    if not isinstance(title, str) or not isinstance(caption, str):
        return False
    # A distinct question locator is meaningful even if the topic is identical.
    title_locators = [m.group() for m in re.finditer(_QUESTION_LOCATOR, title)]
    caption_locators = [m.group() for m in re.finditer(_QUESTION_LOCATOR, caption)]
    # A locator present on only one surface is useful traceability information,
    # even when the remaining topic words repeat exactly.
    if bool(title_locators) != bool(caption_locators):
        return False
    if title_locators and caption_locators and title_locators != caption_locators:
        return False

    def key(value: str) -> str:
        value = formal_chart_title(value.strip().rstrip("。"))
        match = re.fullmatch(rf"(?P<role>{_SURVEY_ROLE})(?:对|[：:])(?P<topic>.+)", value)
        if not match:
            return value
        topic = re.sub(r"的(?=感知$|评价$|认识$|理解$|体验$|满意度$)", "", match["topic"])
        return f"{match['role']}：{topic}"

    return bool(title.strip() and caption.strip()) and key(title) == key(caption)


_FIGURE_NUMBER_RE = re.compile(r"图\s*\d+[\.\-]\d+")


def caption_droppable_as_repeat(title: str, caption: str) -> bool:
    """图题是否可以按“与标题重复”省略：带图号的图题必须保留（图下需要编号）。"""
    return (_FIGURE_NUMBER_RE.search(str(caption or "")) is None
            and chart_caption_repeats_title(title, caption))


_QUOTE_PAIRS = (
    ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"), ("「", "」"),
    ("『", "』"), ("《", "》"), ("〈", "〉"), ("＂", "＂"), ("＇", "＇"),
)
QUOTED_LITERALS = re.compile("|".join(
    re.escape(left) + "[^" + re.escape(right) + "]*" + re.escape(right)
    for left, right in _QUOTE_PAIRS
))
_OPEN_QUOTES = "\"'“‘「『《〈＂＇"
_CLOSE_QUOTES = "\"'”’」』》〉＂＇"
_IDENTIFIER = r"A-Za-z0-9_.+~\-"
_CSV = rf"(?<![{_IDENTIFIER}])(?i:CSV)(?![A-Za-z0-9_])"
_QUOTED_CSV = "(?:" + "|".join(
    re.escape(left) + r"\s*(?i:CSV)\s*" + re.escape(right)
    for left, right in _QUOTE_PAIRS
) + ")"
_PLAIN_WORKSHEET_CSV = r"(?i:CSV)(?![A-Za-z0-9_]|字段|对应|数据|统计)"
_WORKSHEET_CSV = rf"(?:{_QUOTED_CSV}|{_PLAIN_WORKSHEET_CSV})"
_SHEET_SUFFIX = rf"(?:" + "|".join((
    rf"\s*(?:的\s*)?工作表\s*[：:]?\s*{_WORKSHEET_CSV}",
    rf"\s*(?:的\s*)?{_QUOTED_CSV}\s*工作表",
    rf"\s*[（(]\s*(?:工作表\s*[：:]?\s*)?{_WORKSHEET_CSV}\s*[）)]",
    rf"\s*(?:的\s*)?(?:[，,:：]\s*)?{_QUOTED_CSV}",
    rf"\s+{_PLAIN_WORKSHEET_CSV}(?:\s*工作表)?",
)) + ")?"

# Keep paths intact even when their basename is a known uploaded source. Quoted
# paths also cover spaces in directory names; unquoted paths stop at punctuation.
_PATH_STOP = r"\s<>\(\)\[\]{}\"'“”‘’「」『』《》〈〉＂＇。！？；，、"
_URL_PATH = re.compile(rf"[A-Za-z][A-Za-z0-9+.-]*://[^{_PATH_STOP}]+")
_WINDOWS_PATH = re.compile(rf"(?:[A-Za-z]:[\\/]|\\\\)[^{_PATH_STOP}]+")
_SLASH_PATH = re.compile(rf"[^{_PATH_STOP}:：]*[/\\][^{_PATH_STOP}:：]+")
_QUOTED_PATHS = tuple(re.compile(re.escape(left) + "(.*?)" + re.escape(right))
                      for left, right in _QUOTE_PAIRS)


def _chinese_index(value: int) -> str:
    digits = "零一二三四五六七八九"
    if value < 10:
        return digits[value]
    if value >= 10000:
        head, tail = divmod(value, 10000)
        return _chinese_index(head) + "万" + (
            ("零" if tail < 1000 else "") + _chinese_index(tail) if tail else ""
        )
    result = ""
    need_zero = False
    for divisor, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        digit, value = divmod(value, divisor)
        if digit:
            if need_zero:
                result += "零"
            result += ("" if divisor == 10 and digit == 1 and not result else digits[digit]) + unit
            need_zero = False
        elif result and value:
            need_zero = True
    return result


def _path_spans(value: str) -> list[tuple[int, int]]:
    spans = []
    for pattern in _QUOTED_PATHS:
        for match in pattern.finditer(value):
            if "/" in match.group(1) or "\\" in match.group(1):
                spans.append(match.span())
    for pattern in (_URL_PATH, _WINDOWS_PATH, _SLASH_PATH):
        spans.extend(match.span() for match in pattern.finditer(value))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


class SourceTextFormatter:
    """Create display copies with natural source labels and unchanged bindings.

    CSV categories in table cells/headers and chart labels remain literal. Only
    known source names and explicit data-description phrases change there.
    """

    def __init__(self, data_file_names: list[str]):
        names = list(dict.fromkeys(name for name in data_file_names
                                   if isinstance(name, str) and name.lower().endswith(".csv")))
        self._aliases = {
            name: "调查问卷数据" if len(names) == 1 else f"调查问卷数据（{_chinese_index(index)}）"
            for index, name in enumerate(names, 1)
        }
        self._source_pattern = None
        if names:
            # Literal, case-sensitive filenames avoid conflating distinct sources.
            files = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
            self._source_pattern = re.compile(
                rf"(?<![{_IDENTIFIER}/\\])[{re.escape(_OPEN_QUOTES)}]?"
                rf"(?P<filename>{files})(?![{_IDENTIFIER}/\\])"
                rf"[{re.escape(_CLOSE_QUOTES)}]?{_SHEET_SUFFIX}"
            )

    def _segment(self, value: str, *, narrative: bool) -> str:
        if self._source_pattern is not None:
            value = self._source_pattern.sub(lambda match: self._aliases[match.group("filename")], value)
        # This local reference follows only the converted CSV-file description;
        # an unrelated reference such as "问卷.docx。该文件…" remains unchanged.
        value = re.sub(
            rf"(原始\s*{_CSV}\s*数据文件)([^。！？\r\n]*[。！？]\s*)该文件",
            lambda match: "原始问卷作答数据" + match.group(2) + "所提供数据",
            value,
        )
        for pattern, replacement in (
            (rf"原始\s*{_CSV}\s*数据文件", "原始问卷作答数据"),
            (rf"{_CSV}\s*数据文件", "问卷作答数据"),
            (rf"{_CSV}\s*字段", "数据字段"),
            (rf"{_CSV}\s*对应", "数据字段对应"),
            (rf"{_CSV}\s*数据", "问卷数据"),
        ):
            value = re.sub(pattern, replacement, value)
        if narrative:
            value = re.sub(rf"{_CSV}(?![.{re.escape('+-~')}-])", "问卷数据", value)
        return value

    def _text(self, value: str, *, narrative: bool) -> str:
        if not isinstance(value, str):
            return value
        parts = []
        cursor = 0
        for start, end in _path_spans(value):
            parts.append(self._segment(value[cursor:start], narrative=narrative))
            parts.append(value[start:end])
            cursor = end
        parts.append(self._segment(value[cursor:], narrative=narrative))
        return "".join(parts)

    def text(self, value: str) -> str:
        return self._text(value, narrative=True)

    def _display_value(self, value, *, narrative: bool):
        if isinstance(value, str):
            return self._text(value, narrative=narrative)
        if isinstance(value, list):
            return [self._display_value(item, narrative=narrative) for item in value]
        if isinstance(value, tuple):
            return tuple(self._display_value(item, narrative=narrative) for item in value)
        return value

    def _table(self, payload: dict) -> None:
        for field in ("columns", "rows"):
            if field in payload:
                payload[field] = self._display_value(payload[field], narrative=False)
        for field in ("title", "note", "notes", "source", "caption"):
            if field in payload:
                payload[field] = self._display_value(payload[field], narrative=True)
        if isinstance(payload.get("table"), dict):
            self._table(payload["table"])

    def table(self, payload: dict) -> dict:
        display = deepcopy(payload)
        self._table(display)
        return display

    def _chart(self, metadata: dict) -> None:
        if "labels" in metadata:
            metadata["labels"] = self._display_value(metadata["labels"], narrative=False)
        for field in ("title", "x_label", "y_label", "note", "notes", "source", "caption"):
            if field in metadata:
                metadata[field] = self._display_value(metadata[field], narrative=True)
        if isinstance(metadata.get("charts"), list):
            for child in metadata["charts"]:
                if isinstance(child, dict):
                    self._chart(child)

    def chart(self, metadata: dict) -> dict:
        display = deepcopy(metadata)
        self._chart(display)
        return display
