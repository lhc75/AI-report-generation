"""第二步：按章节任务生成内容（写作/表格/图片三个 Agent）。

所有任务统一走 ReAct 智能体循环：模型每轮输出一个 JSON 动作
（run_python 在沙箱执行代码 / finish 提交成果），系统执行后把观察结果
反馈给模型，直到成果通过结构检查与 AI 复核或超过最大轮次。
后端只做结构检查；内容正确性（数字、口径、完整性）全部由审核 Agent 判断。
"""
import json
import logging
import os
import re
import stat
import time
import asyncio
import hashlib
import html
import inspect
import threading
from uuid import uuid4
from decimal import Decimal, InvalidOperation
from pathlib import Path
from unicodedata import east_asian_width

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import context_compact, sandbox, storage, template_parse, operations
from .config import settings
from .llm import chat_completion, ChatContextOverflow
from .models import ChapterTask, GenerationRecord, ModelConfig, ModuleContent, Project, ProjectFile
from .report_text import chart_caption_repeats_title, formal_chart_title
from .skills import render_system_prompt

logger = logging.getLogger(__name__)

AGENT_BY_TYPE = {"text": "write", "table": "analyze", "image": "chart"}
# 单个模板模块可覆盖多题；最终 Word 按题拆图，预览仍需受限以控制内存。
MAX_CHART_PANELS = 15
# 图表可读性阈值（通用经验值）：类别过多或标签过宽时改用横向条形，AI 可用 layout 显式覆盖。
DENSE_CHART_CATEGORY_LIMIT = 10          # 类别数超过该值直接横向
DENSE_CHART_LABEL_MIN_CATEGORIES = 6     # 类别数超过该值且存在过宽标签时横向
DENSE_CHART_LABEL_WIDTH = 16             # 触发横向的最长标签宽度（配合上一项）
CHART_LAYOUT_VALUES = ("horizontal", "vertical")
HORIZONTAL_LABEL_WRAP_WIDTH = 26         # 横向图类别标签换行宽度（宽度单位≈半角字符）
# 材料摘要算法版本：改变摘要内容或结构时递增，用于材料缓存失效。
MATERIALS_VERSION = 2


# 编辑性元语句（术语统一说明等）不该出现在报告正文，出现即按分句删除
_META_CLAUSE_RE = re.compile(r"旧称|原名|统一规范为|本报告统一使用|本说明")
# 含元语句的括号注释（如“样本构成表（旧称已统一规范为现名）”）优先整对删除
_META_PAREN_RE = re.compile(
    r"[（(][^（）()]*(?:旧称|原名|统一规范为|本报告统一使用|本说明)[^（）()]*[）)]"
)

def sanitize_prose(text: str) -> str:
    """生成内容兜底清洗：删除含编辑性元语句的分句。

    提示词虽禁止这类表述，但模型可能照抄写作指令中的编辑要求；
    删得过多（剩余不足 50 字）时保留原文，避免误伤正文。
    """
    if not text or not _META_CLAUSE_RE.search(text):
        return text
    out = _META_PAREN_RE.sub("", text)
    if not _META_CLAUSE_RE.search(out):
        return out.strip() or text
    clauses: list[str] = []
    buf = ""
    for seg in re.split(r"([。！？；\n])", out):
        buf += seg
        if seg in "。！？；\n":
            clauses.append(buf)
            buf = ""
    if buf:
        clauses.append(buf)
    cleaned = "".join(c for c in clauses if not _META_CLAUSE_RE.search(c)).strip()
    if len(cleaned) < 50 and len(text.strip()) >= 50:
        logger.warning("sanitize_prose: 清洗后内容过短，保留原文")
        return text
    return cleaned or text


def sanitize_table_json(payload: str) -> str:
    """表格 JSON 的单元格/表名做元语句清洗（兼容 {"table": ...} 包装）；解析失败时原样返回。"""
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return payload
    if not isinstance(obj, dict):
        return payload
    table = obj.get("table") if isinstance(obj.get("table"), dict) else obj
    if isinstance(table.get("title"), str):
        table["title"] = sanitize_prose(table["title"])
    table["columns"] = [
        sanitize_prose(c) if isinstance(c, str) else c for c in table.get("columns", [])
    ]
    table["rows"] = [
        [sanitize_prose(c) if isinstance(c, str) else c for c in row]
        for row in table.get("rows", [])
    ]
    if table is obj:
        return json.dumps(obj, ensure_ascii=False)
    obj["table"] = table
    return json.dumps(obj, ensure_ascii=False)


def pick_model(db: Session, user_id: int, agent_type: str) -> ModelConfig | None:
    """优先用对应 agent 类型的默认配置，其次全局默认，再次任意配置。"""
    cfg = db.scalar(
        select(ModelConfig).where(
            ModelConfig.user_id == user_id,
            ModelConfig.agent_type == agent_type,
            ModelConfig.is_default == True,  # noqa: E712
        )
    )
    if cfg is None:
        cfg = db.scalar(
            select(ModelConfig).where(
                ModelConfig.user_id == user_id,
                ModelConfig.agent_type == "",
                ModelConfig.is_default == True,  # noqa: E712
            )
        )
    if cfg is None:
        cfg = db.scalar(select(ModelConfig).where(ModelConfig.user_id == user_id))
    return cfg


def document_location(db: Session, task: ChapterTask) -> str:
    """任务在报告中的章节位置，如：第二部分 调查结果 / 二、主体认知 / 1、认知理解。

    只取标题型祖先：内容任务的 title 是写作指令，不能当作标题使用。
    """
    parts, pid = [], task.parent_id
    while pid:
        parent = db.get(ChapterTask, pid)
        if parent is None:
            break
        parts.append(parent.title)
        pid = parent.parent_id
    return " / ".join(reversed(parts)) or "（文档开头）"


def _primary_task_instruction(instruction: str) -> str:
    """只保留任务的首段要求，避免重复附加的公共成文/证据说明。"""
    text = str(instruction or "").strip()
    first = text.find("【")
    following = text.find("【", first + 1) if first >= 0 else -1
    return (text[:following] if following >= 0 else text).strip()


def related_task_source_context(db: Session, task: ChapterTask) -> str:
    """为文字里的“本节/上述”提供同节任务来源；不读取任何历史成品。"""
    if task.task_type != "text" or not task.parent_id:
        return ""
    nodes = db.scalars(select(ChapterTask).where(
        ChapterTask.project_id == task.project_id
    ).order_by(ChapterTask.sort, ChapterTask.id)).all()
    node_map = {node.id: node for node in nodes}
    if task.id not in node_map:
        return ""
    ancestors = []
    visited = {task.id}
    parent_id = task.parent_id
    while parent_id is not None:
        parent = node_map.get(parent_id)
        if parent is None or parent_id in visited or parent.task_type != "heading":
            return ""
        visited.add(parent_id)
        ancestors.append(parent)
        parent_id = parent.parent_id
    children: dict[int, list[ChapterTask]] = {}
    for node in nodes:
        if node.parent_id is not None:
            children.setdefault(node.parent_id, []).append(node)

    for parent in ancestors[:2]:
        descendants = []
        pending = list(children.get(parent.id, []))
        visited = {parent.id}
        while pending:
            node = pending.pop()
            if node.id in visited:
                return ""
            visited.add(node.id)
            descendants.append(node)
            pending.extend(children.get(node.id, []))
        related = [node for node in descendants if node.id != task.id
                   and node.task_type in {"table", "image", "text"}
                   and _primary_task_instruction(node.instruction)]
        if not related:
            continue
        # 表图明示题域优先；同类仍按模板顺序，正文说明只用于补充指代。
        related.sort(key=lambda node: (node.task_type == "text", node.sort, node.id))
        lines = [
            "\n\n【本节其他任务的来源参考】",
            "以下仅用于解释“本节”“上述题目”等指代，范围以本任务显式指令为准；"
            "不得以邻任务要求覆盖本任务、增加统计题目或合并不同题义。"
            "这是任务来源信息，不代表相邻成果已经生成或通过审核。",
        ]
        for node in related:
            kind = {"table": "表格", "image": "统计图", "text": "正文"}[node.task_type]
            lines.append(f"- {kind}《{node.title}》：{_primary_task_instruction(node.instruction)}")
        return "\n".join(lines)
    return ""


# 材料摘要缓存：同一项目文件不变时不重复下载/解析，显著降低批量生成时的负载
_materials_cache: dict[int, tuple[tuple, str, str]] = {}


def build_materials(db: Session, project: Project) -> tuple[str, str]:
    """参考资料摘要与数据摘要（按项目文件指纹缓存，文件变化自动失效）。"""
    files = db.scalars(
        select(ProjectFile).where(ProjectFile.project_id == project.id)
    ).all()
    fingerprint = (MATERIALS_VERSION, project.material_revision, tuple(sorted(
        (f.id, f.file_name, f.minio_path, f.statistics_config) for f in files)))
    cached = _materials_cache.get(project.id)
    if cached and cached[0] == fingerprint:
        return cached[1], cached[2]
    refs, datas = [], []
    for f in files:
        raw = storage.download_bytes(f.minio_path)
        if f.file_type == "reference":
            refs.append(template_parse.summarize_reference(f.file_name, raw, max_chars=20000))
        elif f.file_type == "data":
            datas.append(template_parse.summarize_data_structure(f.file_name, raw))
    result = ("\n\n".join(refs) or "（无）", "\n".join(datas) or "（无）")
    if len(_materials_cache) > 20:
        _materials_cache.clear()
    _materials_cache[project.id] = (fingerprint, *result)
    return result


def invalidate_materials(project_id: int) -> None:
    _materials_cache.pop(project_id, None)


REACT_PROTOCOL = (
    "你是一个 ReAct 智能体，通过「思考 → 行动 → 观察」的循环一步步完成任务。\n"
    "每一轮你只输出一个 JSON 对象，除此之外不要输出任何文字：\n"
    '{"thought": "你对当前局面的思考和下一步打算", "action": "run_python 或 finish", '
    '"action_input": "动作内容"}\n'
    "可用动作：\n"
    "1. run_python：在沙箱中执行 Python 代码（可用 pandas、openpyxl、matplotlib；"
    "数据文件在 ./data/ 目录）。action_input 填完整代码，执行结果会作为【观察】返回给你。\n"
    "【格式硬性要求】输出必须是单行合法 JSON：字符串内禁止原始换行（写成 \\n），内部双引号必须转义。代码中的换行同样用 \\n 表示。\n"
    "禁止使用函数调用、工具调用或 DSML/XML 标签格式（如 <｜｜DSML｜｜…>）；"
    "每轮只能输出上述 JSON 对象，run_python 代码一律放进 action_input 字符串。\n"
    "2. finish：任务完成，action_input 填最终成果（格式见任务说明）。"
    "文字/表格也可先把完整成果JSON保存到 ./out/result.json，"
    '再以 {"result_file":"out/result.json"} 提交；系统直接读取文件并执行同样的证据校验。\n'
    "观察日志最多展示3000字，只打印筛选后的少量证据或结果摘要，不能依赖日志复制大表。\n"
    "策略建议：先用 run_python 探索数据、算出需要的统计数字，确认无误后再 finish。\n"
)

_WRITING_GUIDE = (
    "【写作与统计口径】\n"
    "1. 正文、表格与图表只放给读者的内容：编码、文件名、统计过程自述等一律不写入成文；\n"
    "2. 题目语义与选项文字以【参考资料摘要】和 ./data 数据文件为准；数据文件的选项编码键形如 qN，"
    "与问卷文档题号可能并不一致，禁止把指令里的题号直接当作数据键使用；统计前必须先用选项数量与"
    "选项含义确认目标题目对应哪个键（数据摘要中若给出键位规律说明，先按该说明核对；也可先打印"
    "该字段的取值计数核对选项语义），确认不了对应关系时用自然中文说明关系不明；\n"
    "3. 所有统计数字必须先用 run_python 从 ./data 数据计算或核验后再写入，禁止凭记忆编造；\n"
    "4. 数据中的行数与频次是提交记录/作答记录；没有独立受访者识别，不写成“有效独立人数”“有效回收率”；\n"
    "5. 多选题按每条记录内选项去重统计勾选率，并在正文或表注标明“多选勾选率”"
    "（图表用 multi_select 字段标识）；单选题按选项占比统计；\n"
    "6. 方法说明集中、简短，同一段不重复堆砌口径；条件题或口径存疑时用一句话说明边界；\n"
    "7. 语言客观、书面化；来源用“调查问卷数据”“问卷作答记录”等自然称呼，不写文件名或工作表名。\n"
    "8. 成文（含表注、图注）不得写入修改过程、版本说明、‘已修正/原误用’等过程性文字，"
    "只保留统计口径与数据来源。\n"
    "9. 引用问卷题目一律用中文“第X题”（如“第一题”“第十二题”），不得写成 Q1、Q2、qN "
    "等字母编码（正文、表格、图题与图内文字一致）；题号对照表的说明写“题号按各问卷题目"
    "顺序表述”，不得写“沿用原问卷编号”；\n"
    "10. 不得写入资料完备性或对码过程的声明（如“未取得正式施测编码表”“据现有问卷结构"
    "对应”等）；题目与数据的对应按数据摘要的键位规律和问卷资料核对即可，成文只保留读者"
    "理解所必需的一句口径。\n"
)

TEXT_GOAL = (
    "【任务】撰写调查报告的一个章节正文。\n"
    "【章节位置】\n{path}\n\n【写作指令】\n{instruction}\n"
    "{extra}"
    "【成果要求】finish 的 action_input 必须是 JSON：{\"content\":\"正文文字\"}；"
    "也可以先把 JSON 保存到 ./out/result.json，再用 {\"result_file\":\"out/result.json\"} 提交。\n"
    "1. 不要输出【填写】等指令前缀，不要输出标题；\n"
    "2. 内容必须基于【参考资料摘要】、数据文件与材料，不编造数据或题目；\n"
    "3. 结果段按指令报告必要的频次、比例与作答记录基数；避免逐题堆砌与相邻段落重复相同数字；\n"
    + _WRITING_GUIDE +
    "【数据文件】./data/ 目录：{data_files}\n"
    "【参考资料摘要】\n{references}\n\n【数据摘要】\n{data_summary}\n"
)

TABLE_GOAL = (
    "【任务】统计生成报告中的一张数据表格。\n"
    "【章节位置】\n{path}\n\n【任务指令】\n{instruction}\n"
    "{extra}"
    "【成果要求】finish 的 action_input 是表格 JSON 字符串：\n"
    '{"table": {"title": "表名", "columns": ["列1", "列2"], "rows": [["值", "值"]]}, "note": "表注（可选）"}\n'
    "1. 表格数据必须用 run_python 读取 ./data/ 下的真实数据计算，禁止编造；\n"
    "2. 建议先用 run_python 把完整成果保存到 ./out/result.json，"
    '再以 {"result_file":"out/result.json"} 提交；只打印行数与首尾行摘要，避免日志截断；\n'
    "3. 占比类数值保留 1 到 2 位小数；题目概览中确实无对应项的格用“—”并在表注解释，"
    "统计数值格不能用占位符代替；\n"
    "4. 表注用 note（字符串）或 notes（字符串数组）；限制与口径说明写表注，不填充统计单元格；\n"
    + _WRITING_GUIDE +
    "【数据文件】./data/ 目录：{data_files}\n"
    "【参考资料摘要】\n{references}\n\n【数据摘要】\n{data_summary}\n"
)

IMAGE_GOAL = (
    "【任务】绘制报告中的一张统计图。\n"
    "【章节位置】\n{path}\n\n【任务指令】\n{instruction}\n"
    "{extra}"
    "【成果要求】先用 run_python 绘图并保存图表数据，成功后再 finish：\n"
    "1. 用 pandas 或 openpyxl 读取 ./data/ 下的真实数据绘图，禁止编造数据；\n"
    "2. matplotlib 保存 ./out/chart.png（dpi=150，figsize 按内容合理设置）；\n"
    "3. 同时把 {\"title\":\"图表标题\",\"labels\":[\"标签\"],\"values\":[0]} 保存到 "
    "./out/chart_data.json；可选补充 counts、percentages（与 labels 一一对应）、"
    "denominator（分母）、multi_select（多选勾选率）、caption（顶层图题）、"
    "layout（\"horizontal\" 或 \"vertical\"，可选，声明图表朝向）；"
    "counts、percentages 必须是与 labels 等长的数字数组；denominator 写单一数字，"
    "或与 labels 等长的数字数组；禁止把这些字段写成按群体分组的对象；"
    "多图对比使用 {\"charts\":[完整单图对象, ...]}，每个子图同结构；\n"
    "4. 中文字体：plt.rcParams['font.sans-serif'] = ['Hiragino Sans GB', 'PingFang SC', "
    "'Arial Unicode MS', 'SimHei']，并设置 plt.rcParams['axes.unicode_minus'] = False；\n"
    "5. 图表必须有标题与坐标轴或图例，数值标签尽量标注；\n"
    "6. 顶层 caption 保留任务指令给出的完整图题（含图号，如“图2.2-3 …”），多子图共用"
    "同一顶层图题、各自标题写在子图 title；不需要编写 note/source 注记；\n"
    "6.1 任务指令里的题号（如“教育主管部门Q7”“教师Q12”）只是内部定位写法：图表标题、"
    "子图标题与 labels 等一切图内文字必须改写为中文“第X题”（如“教育主管部门第七题”），"
    "或只写群体名称；不得照抄 Qx 字母编码；\n"
    "6.2 多群体/多题目对照必须拆成 charts 分面板：每个群体或题目一个面板，面板内按数值降序，"
    "不要把多组选项堆进同一个面板；\n"
    "6.3 类别标签只写最短可辨认文字，群体与题号放在面板 title 或顶层图题里统一交代，"
    "不要逐条重复；类别较多（经验上超过 10 个）或名称较长时使用横向条形"
    "（layout=\"horizontal\"）；\n"
    "6.4 数值标签保持单行（如“206条 / 64.4%”），不要写多行堆叠或重复口径的文字；\n"
    "7. finish 的 action_input 填一句图片说明；\n"
    + _WRITING_GUIDE +
    "【数据文件】./data/ 目录：{data_files}\n"
    "【参考资料摘要】\n{references}\n\n【数据摘要】\n{data_summary}\n"
)

IMAGE_GOAL += (
    "\n分组或地区基数较小不构成停画理由：仍按指令覆盖要求的题目出图，"
    "在正文或表注中提示该组作答记录数及‘基数较小，结果仅供参考’；"
    "只有整题没有任何作答记录时才可在 finish 中说明无法绘制。\n"
)
GOAL_BY_TYPE = {"text": TEXT_GOAL, "table": TABLE_GOAL, "image": IMAGE_GOAL}


def _extra(user_prompt: str) -> str:
    return f"\n用户补充要求：{user_prompt}\n" if user_prompt else ""


_DSML_INVOKE_RE = re.compile(
    r"<\s*[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*invoke\s+name\s*=\s*[\"']?(run_python|finish)",
    re.IGNORECASE,
)
_DSML_PARAM_RE = re.compile(
    r"<\s*[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*parameter\s+name\s*=\s*[\"']?action_input[\"']?[^>]*>"
    r"(.*?)"
    r"(?:<\s*/\s*[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*parameter|$)",
    re.DOTALL,
)


def _parse_dsml_action(text: str) -> dict | None:
    """抢救 DSML 风格的工具调用标记：<｜｜DSML｜｜ invoke name="...">…"""
    invoke = _DSML_INVOKE_RE.search(text)
    if invoke is None:
        return None
    action = invoke.group(1).strip()
    result: dict = {"action": action}
    param = _DSML_PARAM_RE.search(text, invoke.end())
    if param is not None:
        payload = html.unescape(param.group(1)).strip()
        if payload:
            result["action_input"] = payload
    if action == "finish" and "action_input" not in result:
        return None
    logger.info("react action parsed via DSML fallback")
    return result


def parse_action(reply: str) -> dict | None:
    """从模型输出中解析 ReAct 动作 JSON。

    严格解析失败时逐级降级：允许字符串内原始控制字符（strict=False）、
    容忍截断/转义错误并按字段名抢救；个别模型输出 DSML 工具调用标记时
    一并还原为动作，避免在 action_input 里夹带换行、引号或标记时被判为协议错误。
    """
    text = reply.strip()
    text = re.sub(r"^```(?:json)?\s*|```\s*$", "", text, flags=re.MULTILINE).strip()
    if "dsml" in text.lower():
        dsml = _parse_dsml_action(text)
        if dsml is not None:
            return dsml
    start = text.find("{")
    if start == -1:
        return None
    end = text.rfind("}")
    candidate = text[start : end + 1] if end > start else text[start:]
    for strict in (True, False):
        try:
            obj = json.loads(candidate, strict=strict)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            return obj
    repaired = _repair_action_json(candidate)
    if repaired is not None:
        logger.info("react action parsed via repair fallback")
    return repaired


def _unescape_json_fragment(fragment: str) -> str:
    """把 JSON 字符串片段还原为原始文本（处理 \\n、\\"、\\\\ 等转义）。"""
    try:
        return json.loads(f'"{fragment}"')
    except json.JSONDecodeError:
        return (fragment.replace("\\n", "\n").replace("\\t", "\t")
                        .replace('\\"', '"').replace("\\\\", "\\"))


def _repair_action_json(candidate: str) -> dict | None:
    """从非法/截断的协议 JSON 中按字段名抢救 action 与 action_input。"""
    m = re.search(r'"action"\s*:\s*"([^"]*)"', candidate)
    if not m:
        return None
    action = m.group(1).strip()
    if not action:
        return None
    result: dict = {"action": action}
    t = re.search(r'"thought"\s*:\s*"((?:[^"\\]|\\.)*)"', candidate)
    if t and t.end() <= m.start():
        result["thought"] = _unescape_json_fragment(t.group(1))
    i = re.search(r'"action_input"\s*:\s*"', candidate)
    if i and i.start() >= m.end():
        fragment = candidate[i.end():]
        fragment = re.sub(r'"\s*}\s*$', "", fragment)  # 去掉收尾的 "}
        fragment = re.sub(r'"\s*$', "", fragment)       # 去掉截断残留的引号
        result["action_input"] = _unescape_json_fragment(fragment)
    return result


def _react_preview(value: object, limit: int = 240) -> str:
    text = str(value or "").strip().replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "……"


def _stall_hint(turns_since_finish: int) -> str:
    """连续多轮只执行代码、未尝试提交成果时的系统提醒；未到提醒点返回空串。"""
    threshold = settings.REACT_STALE_FINISH_NUDGE
    if threshold <= 0 or turns_since_finish < threshold:
        return ""
    if (turns_since_finish - threshold) % 4 != 0:
        return ""
    return (
        f"【系统提醒】你已连续 {turns_since_finish} 轮只执行代码、尚未提交成果。"
        '请立即用 finish 提交（可填 {"result_file":"out/result.json"}），'
        "或在 finish 中说明无法完成的具体原因。"
    )


def _remaining_hint(remaining: int) -> str:
    """轮次即将耗尽时的催促；remaining 为剩余轮数。"""
    if remaining > 2:
        return ""
    return (
        f"【系统提醒】本次任务仅剩 {max(remaining, 1)} 轮，请勿继续探索或修改文件，"
        "立即 finish 提交现有成果；如无法完成，请在 finish 中写明具体原因。"
    )


def _read_finish_file(task_type: str, payload: str, workdir) -> str:
    """直接读取有界沙箱成果，日志截断不能改变实际提交内容。"""
    try:
        wrapper = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return payload
    if not isinstance(wrapper, dict) or "result_file" not in wrapper:
        return payload
    if task_type not in {"text", "table"}:
        raise ValueError("result_file 仅用于文字或表格成果")
    if set(wrapper) != {"result_file"} or not isinstance(wrapper["result_file"], str):
        raise ValueError("文件提交必须只包含字符串 result_file")
    if workdir is None:
        raise ValueError("文件提交缺少沙箱工作目录")
    relative = Path(wrapper["result_file"])
    if (relative.is_absolute() or not relative.parts or relative.parts[0] != "out"
            or ".." in relative.parts or relative.suffix.lower() != ".json"):
        raise ValueError("成果文件必须是 out/ 内的相对 JSON 路径，不允许目录穿越")
    path = sandbox.output_path(workdir, relative)
    limit = min(settings.SCRIPT_SANDBOX_MAX_OUTPUT_BYTES, sandbox.MAX_OUTPUT_BYTES)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("成果文件必须是普通文件，不能使用链接")
        if not 0 < info.st_size <= limit:
            raise ValueError(f"成果文件为空或超过 {limit} 字节上限")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"成果文件超过 {limit} 字节上限")
    content = raw.decode("utf-8")
    result = json.loads(content)
    if not isinstance(result, dict) or "result_file" in result:
        raise ValueError("成果文件必须包含最终成果 JSON 对象，不能嵌套文件引用")
    return content


def _finish_table_structure(obj) -> tuple[bool, str, str]:
    """表格成果的结构检查：需要 columns/rows，行列等长，单元格为文字或数值。"""
    table = obj.get("table") if isinstance(obj, dict) and isinstance(obj.get("table"), dict) else obj
    if not isinstance(table, dict):
        return False, "表格 JSON 必须是对象", ""
    columns, rows = table.get("columns"), table.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return False, "表格 JSON 缺少 columns/rows 字段", ""
    if not columns or not rows:
        return False, "表格 columns 或 rows 为空，请先用 run_python 从真实数据统计", ""
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns):
            return False, f"表格第 {index + 1} 行单元格数量与列数不一致", ""
        for cell in row:
            if isinstance(cell, (dict, list)):
                return False, f"表格第 {index + 1} 行含对象单元格，请只放文字或数值", ""
    normalized = dict(table)
    for key in ("note", "caption", "source"):
        if isinstance(obj, dict) and key in obj and key not in table:
            normalized[key] = obj[key]
    return True, "", json.dumps(normalized, ensure_ascii=False)


def _numeric_cell_ok(value) -> bool:
    """结构检查用：可渲染的数字（不含布尔、对象与普通文字）。"""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and bool(re.fullmatch(r"[-+]?\d+(?:\.\d+)?%?", value.strip()))


def _numeric_sequence(value, expected: int) -> list[float] | None:
    """渲染容错：只接受与数据点等长的数字数组，形状不符时返回 None 按缺省处理。"""
    if not isinstance(value, list) or len(value) != expected:
        return None
    try:
        return [_chart_value(item) for item in value]
    except ValueError:
        return None


def _finish_chart_structure(workdir, instruction: str = "") -> tuple[bool, str]:
    """图表成果的结构检查：chart.png 可解码；chart_data.json 的 labels/values 等长非空。"""
    try:
        chart_png = sandbox.output_path(workdir, "out/chart.png") if workdir is not None else None
    except ValueError as exc:
        return False, str(exc)
    if chart_png is None or not chart_png.exists() or chart_png.stat().st_size <= 5000:
        return False, "尚未生成 ./out/chart.png（或图片过小），请先用 run_python 绘图"
    try:
        from PIL import Image

        with Image.open(chart_png) as image:
            image.verify()
    except Exception:
        return False, "chart.png 无法解码，请重新绘图后再 finish"
    try:
        chart_data = json.loads(
            sandbox.output_path(workdir, "out/chart_data.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, f"读取 ./out/chart_data.json 失败：{exc}"
    if not isinstance(chart_data, dict):
        return False, "chart_data.json 必须是对象"
    charts = chart_data.get("charts")
    items = charts if isinstance(charts, list) and charts else [chart_data]
    if len(items) > MAX_CHART_PANELS:
        return False, f"图表数量超过上限 {MAX_CHART_PANELS}"
    pie_requested = "饼" in instruction or "pie" in instruction.lower()
    for index, chart in enumerate(items, start=1):
        if not isinstance(chart, dict):
            return False, f"第 {index} 张图元数据必须是对象"
        labels, values = chart.get("labels"), chart.get("values")
        if not isinstance(labels, list) or not isinstance(values, list) or not labels:
            return False, f"第 {index} 张图必须包含非空 labels/values 数组"
        if len(labels) != len(values):
            return False, f"第 {index} 张图的 labels/values 长度不一致"
        if any(isinstance(label, (dict, list)) or label is None for label in labels):
            return False, f"第 {index} 张图的 labels 必须是一一对应的文字数组"
        for field in ("values", "counts", "percentages"):
            sequence = chart.get(field)
            if sequence is None:
                continue
            if not isinstance(sequence, list) or len(sequence) != len(labels):
                return False, (f"第 {index} 张图的 {field} 必须是与 labels 等长的数字数组"
                               "（不能是对象）")
            if any(not _numeric_cell_ok(item) for item in sequence):
                return False, f"第 {index} 张图的 {field} 只能包含数字，不能是对象或文字"
        denominator = chart.get("denominator")
        if denominator is not None:
            if isinstance(denominator, list):
                if (len(denominator) != len(labels)
                        or any(not _numeric_cell_ok(item) for item in denominator)):
                    return False, f"第 {index} 张图的 denominator 必须是与 labels 等长的数字数组"
            elif not _numeric_cell_ok(denominator):
                return False, f"第 {index} 张图的 denominator 必须是数字或与 labels 等长的数字数组"
        multi_select = chart.get("multi_select")
        if multi_select is not None and not isinstance(multi_select, bool):
            return False, f"第 {index} 张图的 multi_select 必须是 true/false"
        layout = chart.get("layout")
        if layout is not None and str(layout).strip().lower() not in CHART_LAYOUT_VALUES:
            return False, (f"第 {index} 张图的 layout 只能是 horizontal 或 vertical"
                           "（或删除该字段）")
        if pie_requested:
            for field in ("pie_counts", "pie_percentages"):
                sequence = chart.get(field)
                if (not isinstance(sequence, list) or len(sequence) != len(labels)
                        or any(not _numeric_cell_ok(item) for item in sequence)):
                    return False, (f"第 {index} 张图按指令是饼图，"
                                   f"必须提供与 labels 等长的数字数组 {field}")
    return True, ""


def validate_finish(task_type: str, payload: str, workdir, instruction: str = "") -> tuple[bool, str, str]:
    """finish 成果的结构检查（后端唯一的成果检查）；内容正确性由 AI 复核判断。"""
    try:
        payload = _read_finish_file(task_type, payload, workdir)
    except (OSError, ValueError, TypeError) as exc:
        return False, f"成果文件无法提交：{exc}", ""
    if task_type == "text":
        payload = payload.strip()
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            text = obj.get("content")
            if not isinstance(text, str):
                return False, '正文 JSON 缺少字符串 content 字段，请按 {"content":"正文文字"} 提交', ""
        elif obj is None and payload[:1] not in "{[":
            text = payload
        else:
            # JSON 结构残缺或解析成非对象：按协议打回，避免把 {"content":…}
            # 外壳当成正文存入成果。
            return False, '正文成果需为 {"content":"正文文字"} JSON 或纯文字，请修正后重新提交', ""
        text = text.strip()
        if len(text) < 50:
            return False, "正文太短（少于 50 字），请把章节内容写完整", ""
        return True, "", json.dumps({"content": text}, ensure_ascii=False)
    if task_type == "table":
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            start, end = payload.find("{"), payload.rfind("}")
            if start == -1 or end <= start:
                return False, "action_input 不是合法的表格 JSON", ""
            try:
                obj = json.loads(payload[start:end + 1])
            except json.JSONDecodeError as exc:
                return False, f"action_input 不是合法的表格 JSON：{exc}", ""
        ok, note, normalized = _finish_table_structure(obj)
        return (True, "", normalized) if ok else (False, note, "")
    ok, note = _finish_chart_structure(workdir, instruction)
    if not ok:
        return False, note, ""
    return True, "", payload.strip()


async def react_run(db: Session, cfg: ModelConfig, goal_prompt: str, workdir, task_type: str,
                    finish_validator=None) -> str:
    """ReAct 主循环：思考 → 行动 → 观察，直到 finish 通过校验。返回规范化后的成果。

    finish_validator: 可选自定义校验（同步或异步）(payload, workdir) -> (ok, note, normalized)；
    不传则按 task_type 使用内置结构检查。生成任务用它接入 AI 复核。
    """
    # 部分推理服务只接受开头一条 system 消息，合并下发
    system_parts = [p for p in (render_system_prompt(db, cfg), REACT_PROTOCOL) if p]
    messages = [{"role": "system", "content": "\n\n".join(system_parts)}]
    messages.append({"role": "user", "content": goal_prompt})
    last_note = ""
    last_action = ""
    last_action_input = ""
    last_observation = ""
    last_run_python_input = ""
    same_run_python_turns = 0
    run_python_failures = 0
    stalled_turns = 0
    turns_since_finish = 0

    async def _summarize(digest: str) -> str:
        # 复用当前任务模型；经模块级 chat_completion 调用，离线测试可拦截。
        prompt = context_compact.COMPACT_SYSTEM_PROMPT.format(limit=1500)
        return await chat_completion(
            cfg,
            [
                {"role": "system", "content": prompt},
                {"role": "user",
                 "content": f"{context_compact.COMPACT_REQUEST_MARKER}\n{digest}"},
            ],
            timeout=settings.LLM_REQUEST_TIMEOUT,
        )

    async def _compact(keep_turns: int) -> None:
        nonlocal messages
        messages = await context_compact.compact_messages(messages, keep_turns, _summarize)

    async def _chat() -> str:
        # 每轮请求前估算上下文；接近窗口时先压缩历史再调用模型。
        limit = settings.REACT_CONTEXT_WINDOW_TOKENS * settings.REACT_CONTEXT_COMPRESS_RATIO
        if context_compact.estimate_tokens(messages) > limit and len(messages) > 2:
            await _compact(settings.REACT_CONTEXT_KEEP_TURNS)
        try:
            return await chat_completion(cfg, messages, timeout=settings.LLM_REQUEST_TIMEOUT)
        except ChatContextOverflow:
            # 服务端已判定超长：强制压缩（只保留最近 2 组）后重试本轮的请求一次。
            await _compact(2)
            try:
                return await chat_completion(cfg, messages, timeout=settings.LLM_REQUEST_TIMEOUT)
            except ChatContextOverflow:
                raise ValueError(
                    "任务上下文超出模型窗口（已自动压缩历史后仍超限）："
                    "请在 .env 调大 REACT_CONTEXT_WINDOW_TOKENS 或更换更大上下文的模型"
                ) from None

    for turn in range(1, settings.REACT_MAX_TURNS + 1):
        reply = await _chat()
        messages.append({"role": "assistant", "content": reply})
        reply_text = reply.strip()
        if not reply_text:
            last_action = "empty"
            same_run_python_turns = 0
            run_python_failures = 0
            stalled_turns += 1
            last_note = "模型返回空回复（content / reasoning_content / refusal 均为空）"
            if stalled_turns >= 3:
                raise ValueError(
                    f"ReAct 连续 {stalled_turns} 轮没有可用回复，"
                    f"模型配置可能不兼容当前协议（最后问题：{last_note[:200]}）"
                )
            messages.append({
                "role": "user",
                "content": "【观察】模型返回空回复，请只输出一个 JSON 对象："
                           '{"thought": "...", "action": "run_python 或 finish", "action_input": "..."}',
            })
            continue
        action = parse_action(reply)
        if action is None:
            last_action = "parse_error"
            same_run_python_turns = 0
            run_python_failures = 0
            # 文字任务的容错：模型没按协议输出 JSON，但直接给出了长正文时，按 finish 处理
            if task_type == "text" and len(reply_text) >= 200 and '"action"' not in reply:
                fallback = (finish_validator(reply, workdir) if finish_validator is not None
                            else validate_finish("text", reply, workdir))
                if inspect.isawaitable(fallback):
                    fallback = await fallback
                ok, note, normalized = fallback
                if ok:
                    logger.info("task react finished (plain text fallback) at turn %s", turn)
                    return normalized
            stalled_turns += 1
            last_note = (
                "没有解析到合法的动作 JSON"
                if reply_text
                else "模型返回空回复（content / reasoning_content / refusal 均为空）"
            )
            if reply_text:
                last_note = f"{last_note}（回复前200字：{reply_text[:200]}）"
            if stalled_turns >= 3:
                raise ValueError(
                    f"ReAct 连续 {stalled_turns} 轮没有可解析动作，"
                    f"模型配置可能不兼容当前协议（最后问题：{last_note[:200]}）"
                )
            messages.append({
                "role": "user",
                "content": "【观察】格式错误：没有解析到合法的动作 JSON。请只输出一个 JSON 对象："
                           '{"thought": "...", "action": "run_python 或 finish", "action_input": "..."}',
            })
            continue
        stalled_turns = 0
        act = str(action.get("action", "")).strip()
        inp = action.get("action_input", "")
        if not isinstance(inp, str):
            inp = json.dumps(inp, ensure_ascii=False)
        last_action = act or "unknown"
        last_action_input = _react_preview(inp)
        if act == "run_python":
            # 空白归一化后再比对：换行/缩进不同但内容相同的重复也算卡循环。
            normalized_input = re.sub(r"\s+", " ", inp).strip()
            if normalized_input and normalized_input == last_run_python_input:
                same_run_python_turns += 1
            else:
                last_run_python_input = normalized_input
                same_run_python_turns = 1
            if same_run_python_turns >= 3:
                raise ValueError(
                    "ReAct 连续重复 run_python，模型可能陷入探索循环"
                    f"（最后动作：run_python；代码摘要：{last_action_input}；"
                    f"最后观察：{_react_preview(last_observation)}）"
                )
            if workdir is None:
                obs = "本任务没有代码执行环境，请直接 finish。"
                run_python_failures = 0
            else:
                result = await asyncio.to_thread(sandbox.run_python, inp, workdir)
                if result["ok"]:
                    obs = result["stdout"][-3000:] or "（代码执行成功，无输出）"
                    run_python_failures = 0
                else:
                    obs = "执行失败：\n" + (result["stderr"] or result["stdout"])[-3000:]
                    run_python_failures += 1
            turns_since_finish += 1
            stall = _stall_hint(turns_since_finish)
            urgency = _remaining_hint(settings.REACT_MAX_TURNS - turn)
            if stall or urgency:
                obs = "\n".join(part for part in (obs, stall, urgency) if part)
            last_observation = obs
            last_note = (
                f"最后动作：run_python；代码摘要：{last_action_input}；"
                f"观察：{_react_preview(obs)}"
            )
            if run_python_failures >= settings.REACT_MAX_RUN_FAILURES:
                raise ValueError(
                    f"ReAct 连续 {run_python_failures} 次 run_python 执行失败，"
                    f"模型可能无法完成当前任务（{last_note[:300]}）"
                )
            messages.append({"role": "user", "content": f"【观察】{obs}"})
            continue
        if act == "finish":
            same_run_python_turns = 0
            run_python_failures = 0
            turns_since_finish = 0
            result = (finish_validator(inp, workdir) if finish_validator is not None
                      else validate_finish(task_type, inp, workdir))
            if inspect.isawaitable(result):
                result = await result
            ok, note, normalized = result
            if ok:
                logger.info("task react finished at turn %s", turn)
                return normalized
            logger.info("react finish rejected at turn %s: %s", turn, str(note)[:300])
            last_note = f"最后动作：finish；校验问题：{note}；成果摘要：{last_action_input}"
            obs = f"成果未通过校验：{note}。请修正后重新 finish。"
            urgency = _remaining_hint(settings.REACT_MAX_TURNS - turn)
            if urgency:
                obs = f"{obs}\n{urgency}"
            messages.append({"role": "user", "content": f"【观察】{obs}"})
            continue
        same_run_python_turns = 0
        run_python_failures = 0
        last_note = f"最后动作：{last_action}；动作输入摘要：{last_action_input}"
        messages.append({"role": "user", "content": f"【观察】未知动作“{act}”，只能用 run_python 或 finish。"})
    final_note = last_note or "模型在整个 ReAct 过程中没有返回可用内容"
    raise ValueError(f"ReAct 超过 {settings.REACT_MAX_TURNS} 轮仍未完成（最后问题：{final_note[:300]}）")


def _next_version(db: Session, task_id: int) -> int:
    versions = db.scalars(
        select(ModuleContent.version).where(ModuleContent.chapter_task_id == task_id)
    ).all()
    return (max(versions) if versions else 0) + 1


def _save_content(
    db: Session,
    task: ChapterTask,
    content_type: str,
    content: str,
    image_path: str,
    prompt: str,
    agent_type: str,
    model: str,
    elapsed: int,
    review_json: str = "{}",
    status: str = "ok",
    error_msg: str = "",
) -> ModuleContent:
    db.scalar(select(ChapterTask).where(ChapterTask.id == task.id).with_for_update())
    project = db.get(Project, task.project_id)
    if status == "ok":
        db.query(ModuleContent).filter(ModuleContent.chapter_task_id == task.id).update(
            {"is_current": False})
        operations.invalidate_report(db, project)
    row = ModuleContent(
        chapter_task_id=task.id,
        version=_next_version(db, task.id),
        content_type=content_type,
        content=content,
        review_json=review_json,
        material_revision=project.material_revision,
        instruction_revision=task.instruction_revision,
        image_path=image_path,
        prompt=prompt,
        status=status,
        error_msg=error_msg,
        is_current=status == "ok",
    )
    db.add(row)
    db.flush()
    db.add(GenerationRecord(
        module_content_id=row.id, agent_type=agent_type, model=model,
        prompt=prompt, elapsed_seconds=elapsed,
    ))
    return row


def _prepare_workdir(db: Session, project: Project, task: ChapterTask, run_token=None):
    """建立沙箱工作目录并拷入数据文件；返回 (workdir, 数据文件名列表)。"""
    files = db.scalars(
        select(ProjectFile).where(
            ProjectFile.project_id == project.id, ProjectFile.file_type == "data"
        )
    ).all()
    if not files and task.task_type in ("table", "image"):
        raise ValueError("该项目没有数据文件，表格/图片任务无法生成")
    workdir = sandbox.fresh_workdir(project.id, task.id, run_token)
    names = []
    for f in files:
        if Path(f.file_name).name != f.file_name or "\\" in f.file_name:
            raise ValueError("数据文件名包含非法路径，请重新上传")
        (workdir / "data" / f.file_name).write_bytes(storage.download_bytes(f.minio_path))
        names.append(f.file_name)
    references, _summary = build_materials(db, project)
    (workdir / "data" / "reference-materials.txt").write_text(references, encoding="utf-8")
    preceding = db.execute(select(ChapterTask, ModuleContent).join(ModuleContent,
        ModuleContent.chapter_task_id == ChapterTask.id).where(
        ChapterTask.project_id == project.id, ChapterTask.sort < task.sort,
        ModuleContent.is_current == True, ModuleContent.status == "ok").order_by(ChapterTask.sort)).all()
    context = [{"task_id": prior.id, "title": prior.title, "type": prior.task_type,
                "content": row.content[:4000]}
               for prior, row in preceding if operations.content_is_valid(row, prior, project)]
    (workdir / "data" / "preceding-content.json").write_text(
        json.dumps(context, ensure_ascii=False), encoding="utf-8")
    return workdir, names


def _chart_value(value) -> float:
    text = str(value).strip().replace(",", "").replace("，", "").replace("％", "%")
    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()
    text = text.translate(str.maketrans("０１２３４５６７８９．－", "0123456789.-"))
    try:
        number = float(Decimal(text))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"图表值不是数字：{value}") from exc
    return number


def _chart_label_width(text: str) -> int:
    """估算中英文混排标签的显示宽度，中文全角字符按两个窄字符计算。"""
    return sum(
        2 if east_asian_width(char) in {"W", "F", "A"} else 1
        for char in text
    )


def _wrap_chart_label(label: str, max_width: int = 14) -> str:
    """将过长分类标签换成多行，保留原始标签内容和字符顺序。"""
    text = re.sub(r"\s+", " ", str(label).strip())
    if not text:
        return " "
    # 括号类定位后缀（如“（小学生·第十二题）”）优先整体换到下一行，避免从中间断开。
    head, sep, tail = text.rpartition("（")
    segments = [text]
    if (sep and head
            and _chart_text_width(head) <= max_width
            and _chart_text_width(sep + tail) <= max_width):
        segments = [head, sep + tail]
    lines: list[str] = []
    for segment in segments:
        current: list[str] = []
        current_width = 0
        for char in segment:
            char_width = _chart_label_width(char)
            if (current and current_width + char_width > max_width
                    and char not in "，。；：！？、）)]}》」』%％"):
                lines.append("".join(current).strip())
                current = []
                current_width = 0
            current.append(char)
            current_width += char_width
        if current:
            lines.append("".join(current).strip())
    return "\n".join(lines) or " "


def _chart_text_width(text: str) -> int:
    return sum(_chart_label_width(char) for char in str(text))


def _set_category_ticks(ax, labels: list[str]) -> tuple[list[str], int]:
    """一次性设置横轴分类标签，避免自动分类刻度与手动标签重叠。"""
    display_labels = [_wrap_chart_label(label) for label in labels]
    has_wrapped_label = any("\n" in label for label in display_labels)
    rotation = 30 if has_wrapped_label else 0
    positions = list(range(len(display_labels)))
    ax.set_xticks(positions)
    ax.set_xticklabels(
        display_labels,
        rotation=rotation,
        ha="right" if rotation else "center",
        multialignment="center",
    )
    return display_labels, rotation


def _chart_layout(chart_data: dict, instruction: str) -> str:
    """横/竖布局：AI 声明的 layout 优先，指令关键词其次，最后按类别密度兜底。"""
    declared = chart_data.get("layout")
    if isinstance(declared, str) and declared.strip().lower() in CHART_LAYOUT_VALUES:
        return declared.strip().lower()
    text = instruction or ""
    if "横" in text or "barh" in text.lower():
        return "horizontal"
    if "条形" in text and "柱" not in text:
        return "horizontal"
    labels = [str(label) for label in chart_data.get("labels") or []]
    if len(labels) > DENSE_CHART_CATEGORY_LIMIT:
        return "horizontal"
    if (len(labels) > DENSE_CHART_LABEL_MIN_CATEGORIES
            and any(_chart_label_width(label) > DENSE_CHART_LABEL_WIDTH for label in labels)):
        return "horizontal"
    return "vertical"


def _chart_panel_height(chart_data: dict, instruction: str) -> float:
    """单个面板高度：横向图按换行后的总行数增高，竖向保持原有上限。"""
    labels = [str(label) for label in chart_data.get("labels") or []]
    if _chart_layout(chart_data, instruction) == "horizontal":
        lines = sum(len(_wrap_chart_label(label, HORIZONTAL_LABEL_WRAP_WIDTH).splitlines())
                    for label in labels)
        return max(4.5, min(24.0, 2.4 + 0.30 * max(lines, 4)))
    return max(4.5, min(9.0, 3.5 + len(labels) * 0.5))


_chart_render_lock = threading.Lock()


def render_verified_chart(chart_data: dict, instruction: str, output_path: Path, *, document: bool = False) -> None:
    # pyplot 持有进程级全局状态；批次并发生成时重绘必须互斥。
    with _chart_render_lock:
        import matplotlib
        sizes = {"font.size": 14, "axes.titlesize": 16, "axes.labelsize": 14,
                 "xtick.labelsize": 14, "ytick.labelsize": 14} if document else {}
        with matplotlib.rc_context(sizes):
            _render_verified_chart(chart_data, instruction, output_path, document=document)


def verified_chart_notes(chart_data: dict) -> list[str]:
    """保留完整图注；与坐标轴、简短作答基数分别排版。"""
    notes: list[str] = []
    for key in ("note", "source", "caption"):
        value = chart_data.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"图表 {key} 必须是字符串")
            if key == "caption" and chart_caption_repeats_title(chart_data.get("title"), value):
                continue
            notes.extend(line.strip() for line in value.splitlines() if line.strip())
    extra = chart_data.get("notes", [])
    if not isinstance(extra, list) or any(not isinstance(value, str) for value in extra):
        raise ValueError("图表 notes 必须是字符串数组")
    for value in extra:
        notes.extend(line.strip() for line in value.splitlines() if line.strip())
    return list(dict.fromkeys(notes))


def _chart_number_label(value) -> str:
    number = _chart_value(value)
    # g 格式会把大频次变成科学计数法并截短有效数字；报告注值使用完整十进制。
    return f"{number:.0f}" if number.is_integer() else format(Decimal(str(number)), "f")


def _fit_chart_annotations(fig, ax, annotations: list, horizontal: bool) -> None:
    """根据实际字形宽度预留注值空间，长频次/比例不会贴到边框。"""
    for _ in range(4):
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        frame = ax.get_window_extent(renderer)
        bounds = [annotation.get_window_extent(renderer) for annotation in annotations]
        if not bounds:
            return
        if horizontal:
            overflow = max(box.x1 for box in bounds) - frame.x1 + 12
            if overflow <= 0:
                return
            low, high = ax.get_xlim()
            ax.set_xlim(low, high + overflow / frame.width * (high - low) * 1.15)
        else:
            overflow = max(box.y1 for box in bounds) - frame.y1 + 12
            if overflow <= 0:
                return
            low, high = ax.get_ylim()
            ax.set_ylim(low, high + overflow / frame.height * (high - low) * 1.15)


def _spread_annotations(fig, ax, annotations: list, horizontal: bool) -> None:
    """注释防重叠（通用兜底）：竖排密集时旋转注释，仍相交则逐级缩小字号。"""
    if not annotations:
        return

    def has_overlap() -> bool:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        boxes = [item.get_window_extent(renderer) for item in annotations]
        return any(boxes[i].overlaps(boxes[j])
                   for i in range(len(boxes)) for j in range(i + 1, len(boxes)))

    if not has_overlap():
        return
    if not horizontal:
        # 竖排密集：注释沿柱竖排（宽度收窄），配合扩轴避免超出顶部。
        for item in annotations:
            item.set_rotation(90)
        if not has_overlap():
            return
    for item in annotations:
        item.set_fontsize(11)
    if not has_overlap():
        return
    for item in annotations:
        item.set_fontsize(9)
    has_overlap()


def _render_verified_chart(chart_data: dict, instruction: str, output_path: Path, *, document: bool = False) -> None:
    """按已校验的 chart_data 重绘最终图片，避免未验证的像素内容直接进入报告。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = [
        "Hiragino Sans GB", "PingFang SC", "Arial Unicode MS", "SimHei",
    ]
    plt.rcParams["axes.unicode_minus"] = False
    charts = chart_data.get("charts", [chart_data])
    if not isinstance(charts, list) or not charts or len(charts) > MAX_CHART_PANELS:
        raise ValueError(f"子图数量必须为1至{MAX_CHART_PANELS}")
    # Word 将完整说明作为可换行的正文图注；独立预览保留说明区，不能丢失统计限制。
    panels: list[tuple[dict | None, list[str], float]] = []
    for chart in charts:
        panels.append((chart, [], _chart_panel_height(chart, instruction)))
        if not document:
            notes = verified_chart_notes(chart)
            if notes:
                wrapped = [_wrap_chart_label(note, 110) for note in notes]
                lines = sum(len(note.splitlines()) for note in wrapped)
                panels.append((None, wrapped, max(0.35, lines * 0.2)))
    if not document and "charts" in chart_data:
        common_notes = [_wrap_chart_label(note, 110) for note in verified_chart_notes(chart_data)]
        if common_notes:
            panels.append((None, common_notes, max(0.35, sum(len(n.splitlines()) for n in common_notes) * 0.2)))
    heights = [panel[2] for panel in panels]
    fig, axes = plt.subplots(len(panels), 1, figsize=(10, sum(heights)), squeeze=False,
                             gridspec_kw={"height_ratios": heights})
    plotted = []
    for (chart, notes, _), ax in zip(panels, axes[:, 0]):
        if chart is None:
            ax.set_axis_off()
            ax.text(0, 1, "\n".join(notes), transform=ax.transAxes, ha="left", va="top", fontsize=10)
        else:
            annotations, horizontal = _draw_verified_chart(fig, ax, chart, instruction, document=document)
            plotted.append((ax, annotations, horizontal))
    fig.tight_layout(h_pad=1.8)
    for ax, annotations, horizontal in plotted:
        _spread_annotations(fig, ax, annotations, horizontal)
        _fit_chart_annotations(fig, ax, annotations, horizontal)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)


def _draw_verified_chart(fig, ax, chart_data: dict, instruction: str, *, document: bool = False) -> tuple[list, bool]:
    labels = [str(label) for label in chart_data["labels"]]
    values = [_chart_value(value) for value in chart_data["values"]]
    if not labels or len(labels) != len(values):
        raise ValueError("图表标签或数值为空或长度不一致")
    raw_counts = chart_data.get("counts")
    counts = _numeric_sequence(raw_counts, len(labels)) if raw_counts is not None else None
    if counts is None and raw_counts is None and chart_data.get("metric") == "count":
        counts = values
    raw_percentages = chart_data.get("percentages")
    percentages = (_numeric_sequence(raw_percentages, len(labels))
                   if raw_percentages is not None else None)
    if percentages is None and raw_percentages is None and chart_data.get("metric") == "percent":
        percentages = values
    counts = counts or []
    percentages = percentages or []
    annotations = []
    for i, value in enumerate(values):
        parts = []
        if counts:
            parts.append(f"{_chart_number_label(counts[i])}条")
        if percentages:
            parts.append(f"{_chart_number_label(percentages[i])}%")
        annotations.append(" / ".join(parts) or _chart_number_label(value))
    title = str(chart_data.get("title", "") or "").strip() or "统计图"
    chart_hint = (instruction or "").lower()
    is_pie = "饼" in instruction or "pie" in chart_hint
    is_line = "折线" in instruction or "line" in chart_hint
    horizontal = (not is_pie and not is_line
                  and _chart_layout(chart_data, instruction) == "horizontal")
    display_labels: list[str] = []
    rotation = 0
    annotation_artists = []
    if is_pie:
        if "pie_counts" not in chart_data:
            raise ValueError("饼图缺少完整类别与分母校验，请重新生成")
        percentages = iter(chart_data["pie_percentages"])
        ax.pie(chart_data["pie_counts"], labels=labels,
               autopct=lambda _pct: f"{next(percentages):g}%", startangle=90)
    elif is_line:
        x_positions = list(range(len(labels)))
        ax.plot(x_positions, values, marker="o")
        for x_position, value, annotation in zip(x_positions, values, annotations):
            annotation_artists.append(ax.annotate(
                annotation,
                (x_position, value),
                textcoords="offset points",
                xytext=(0, 6),
                ha="center",
            ))
        display_labels, rotation = _set_category_ticks(ax, labels)
    elif horizontal:
        bars = ax.barh([_wrap_chart_label(label, HORIZONTAL_LABEL_WRAP_WIDTH)
                        for label in labels], values)
        annotation_artists = ax.bar_label(bars, labels=annotations, padding=3)
        ax.invert_yaxis()
    else:
        x_positions = list(range(len(labels)))
        bars = ax.bar(x_positions, values)
        annotation_artists = ax.bar_label(bars, labels=annotations, padding=3)
        display_labels, rotation = _set_category_ticks(ax, labels)
    ax.set_title(_wrap_chart_label(formal_chart_title(title), 80), pad=30 if "denominator" in chart_data else 12)
    if chart_data.get("metric") == "percent":
        (ax.set_xlabel if horizontal else ax.set_ylabel)("勾选率（%）" if chart_data.get("multi_select") else "占比（%）")
    elif chart_data.get("metric"):
        (ax.set_xlabel if horizontal else ax.set_ylabel)({"count": "作答记录数 / 频次（条）", "sample_size": "提交记录数（条）", "denominator": "该题作答记录数（条）"}.get(chart_data["metric"], "数值"))
    if not is_pie:
        ax.margins(x=0.25 if horizontal else 0.05, y=0.15)
    if "denominator" in chart_data:
        denominator = chart_data["denominator"]
        basis = ""
        if isinstance(denominator, list):
            # 多群体对比图：逐点分母去重后并列展示；形状不符时省略注记。
            numbers = _numeric_sequence(denominator, len(labels))
            if numbers is not None:
                basis_labels = []
                for item in numbers:
                    label = _chart_number_label(item)
                    if label not in basis_labels:
                        basis_labels.append(label)
                basis = "各组作答记录：" + " 条 / ".join(basis_labels) + " 条"
        elif isinstance(denominator, (int, float)) and not isinstance(denominator, bool):
            basis = f"本题作答记录：{_chart_number_label(denominator)}条"
        if basis:
            if chart_data.get("multi_select"):
                basis += "（多选）"
            ax.text(1, 1.015, basis, transform=ax.transAxes, ha="right", va="bottom",
                    fontsize=12 if document else 10, color="#444444")
    return annotation_artists, horizontal


async def generate_task(db: Session, project: Project, task: ChapterTask,
                        user_prompt: str = "", run_token: str | None = None) -> ModuleContent:
    """准备、生成和持久化均记录失败；只有有效运行能够提交新版本。"""
    if task.task_type == "heading":
        raise ValueError("标题节点不需要生成内容")
    project_id, task_id = project.id, task.id
    agent_type = AGENT_BY_TYPE.get(task.task_type, "write")
    started = time.time()
    cfg = None
    model_name = ""
    image_path = ""
    review_state: dict = {}
    try:
        project = operations.ensure_run(db, project_id, run_token)
        task = db.get(ChapterTask, task_id)
        if task is None:
            raise RuntimeError("生成任务已不存在")
        task.status = "generating"
        db.commit()
        cfg = pick_model(db, project.user_id, agent_type)
        if cfg is None:
            raise ValueError("未找到模型配置，请先在“AI 配置”页面添加模型")
        review_cfg = pick_model(db, project.user_id, "review")
        if review_cfg is None:
            raise ValueError("未配置审核模型，请在“AI 配置”页面添加审核 Agent")
        model_name = cfg.model
        path = document_location(db, task)
        source_context = related_task_source_context(db, task)
        references, data_summary = build_materials(db, project)
        workdir, data_names = _prepare_workdir(db, project, task, run_token)
        goal = (GOAL_BY_TYPE[task.task_type]
                .replace("{extra}", _extra(user_prompt)).replace("{path}", path)
                .replace("{instruction}", task.instruction + source_context)
                .replace("{data_files}", "、".join(data_names) or "（无）")
                .replace("{references}", references).replace("{data_summary}", data_summary))

        from . import review as review_engine  # 延迟导入，避免与 review 循环依赖

        async def check_finish(value, directory):
            ok, note, normalized = validate_finish(
                task.task_type, value, directory, task.instruction)
            if not ok:
                return ok, note, normalized
            # 清洗先行，保证"被复核的内容"就是"最终保存的内容"。
            if task.task_type == "text":
                review_content = sanitize_prose(json.loads(normalized)["content"])
                normalized = json.dumps({"content": review_content}, ensure_ascii=False)
            elif task.task_type == "table":
                normalized = sanitize_table_json(normalized)
                review_content = normalized
            else:
                chart_data = json.loads(
                    sandbox.output_path(directory, "out/chart_data.json").read_text(encoding="utf-8"))
                normalized = json.dumps(chart_data, ensure_ascii=False)
                review_content = normalized
            result = await review_engine.review_generated_content(
                db,
                review_cfg,
                location=path,
                instruction=task.instruction,
                task_type=task.task_type,
                content_text=review_content,
                references=references,
                data_summary=data_summary,
            )
            review_state["result"] = result
            review_state["content"] = review_content
            errors = [item for item in result["issues"] if item["severity"] == "error"]
            if errors:
                lines = "\n".join(f"- {item['message']}" for item in errors[:6])
                return False, f"审核未通过：\n{lines}", ""
            return True, "", normalized

        db.commit()
        payload = await react_run(db, cfg, goal, workdir, task.task_type,
                                  finish_validator=check_finish)
        review_result = review_state.get("result") or {"status": "ok", "issues": []}
        review_content = review_state.get("content", payload)
        if task.task_type == "image":
            chart_data = json.loads(sandbox.output_path(workdir, "out/chart_data.json").read_text(encoding="utf-8"))
            verified_chart = sandbox.output_path(workdir, "out/verified_chart.png")
            await asyncio.to_thread(render_verified_chart, chart_data, task.instruction, verified_chart)
            image_path = f"{project.user_id}/{project_id}/images/task{task_id}_{uuid4().hex}.png"
            await asyncio.to_thread(storage.upload_bytes, image_path, verified_chart.read_bytes(), "image/png")
            payload = review_content
        elif task.task_type == "text":
            payload = review_content
        task_type = task.task_type
        db.rollback()
        project = operations.ensure_run(db, project_id, run_token)
        task = db.get(ChapterTask, task_id)
        row = _save_content(db, task, task_type, payload, image_path, user_prompt,
                            agent_type, model_name, int(time.time() - started),
                            review_json=_review_record(review_result, project, task, payload))
        task.status = "done"
        db.commit()
        return row
    except Exception as exc:
        logger.exception("generate task %s failed", task_id)
        db.rollback()
        if image_path:
            operations.discard_unpublished([image_path])
        operations.ensure_run(db, project_id, run_token)
        task = db.get(ChapterTask, task_id)
        if task is None:
            raise RuntimeError("任务已失效") from exc
        row = _save_content(db, task, task.task_type, "", "", user_prompt,
                            agent_type, model_name, int(time.time() - started), status="failed",
                            error_msg=f"{type(exc).__name__}: {str(exc) or type(exc).__name__}"[:500])
        task.status = "failed"
        db.commit()
        return row


def _review_record(review_result: dict, project: Project, task: ChapterTask, content: str) -> str:
    """生成内复核的持久化记录；导出审核据此跳过未变更模块。"""
    from .review import REVIEW_PROTOCOL_VERSION  # 延迟导入避免循环依赖
    record = {
        "status": "warning" if review_result.get("issues") else "ok",
        "issues": review_result.get("issues", []),
        "protocol": REVIEW_PROTOCOL_VERSION,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "material_revision": project.material_revision,
        "instruction_revision": task.instruction_revision,
        "reviewed_at": int(time.time()),
    }
    return json.dumps(record, ensure_ascii=False)
