"""内容审核：生成内 AI 复核 + 导出前逐模块 AI 审核与自动修正。

后端不做任何内容语义校验。审核 Agent 读取模块位置、写作指令、成果内容与数据文件
（可在沙箱中运行代码复算抽查），按统一协议输出问题清单与可选的定向修正：
{"status":"ok|issues","issues":[{"severity":"error|warning","message":...}],
 "repairs":[{"task_id":1,"type":"text|table_cell","old":...,"new":...,"cell":...}]}

导出时：仍有 error 级问题则拦截并展示理由；仅 warning 允许放行并记录。
复核本身未完成（模型不可用或输出不合协议）只记提示，不当作内容错误。
生成时：轻量复核（单次调用，不运行代码），error 会作为审核意见打回生成循环。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import generate, operations, report_numbering, sandbox, storage
from .config import settings
from .db import SessionLocal
from .models import ChapterTask, ModuleContent, Project, ProjectFile
from .skills import render_system_prompt

logger = logging.getLogger(__name__)

SEVERITIES = {"error", "warning"}

# 审核协议版本：判定口径或输出规则变化时递增，旧版本缓存自动失效并重新复核。
REVIEW_PROTOCOL_VERSION = 8


class ReviewError(Exception):
    """审核模型输出不符合协议。"""


class ReviewUnavailableError(Exception):
    """审核模型不可用（调用失败或输出无法解析）。"""


REVIEW_OUTPUT_GUIDE = (
    "输出唯一 JSON（不要输出其他文字）：\n"
    '{"status":"ok|issues","issues":[{"severity":"error|warning","message":"问题与理由"}],'
    '"repairs":[{"task_id":1,"type":"text|table_cell","old":"原文片段或旧单元格值",'
    '"new":"修正后的文字或值","cell":"r0c1（仅表格）"}]}\n'
    "severity 只有两级：error=数字或事实不可靠、内容明显不完整或与指令不符（会拦截导出）；"
    "warning=表述或格式建议（仅提示）。没有问题时 status=ok 且 issues 为空数组。\n"
)

FINAL_REVIEW_OUTPUT_GUIDE = (
    "输出唯一 JSON（不要输出其他文字）：\n"
    '{"status":"ok|issues","issues":[{"severity":"error|warning","message":"问题与理由",'
    '"task_ids":[3469,3470]}],"repairs":[{"task_id":1,"type":"text|table_cell",'
    '"old":"原文中唯一的旧片段或旧单元格值","new":"修正后的文字或值","cell":"r0c1（仅表格）"}]}\n'
    "severity 只有两级：error=确认存在的跨模块冲突（会拦截导出并要求相关模块重算）；"
    "warning=提醒（仅提示）。error 的 task_ids 必填，且只能使用 review_input 模块列表里的编号；"
    "没有问题时 status=ok 且 issues 为空数组。\n"
    "能用一处唯一文本片段直接改好的冲突（如某模块里的单个数字、称谓或措辞需要替换），"
    "必须给出 repairs（old 必须在目标模块中唯一出现；表格用 table_cell 定位单元格），"
    "不要为这种可精确定位的问题要求整模块重算；仅当无法用文本片段修好时才留空 repairs。\n"
    "图号、表号、题号数字格式与 caption 编号由系统在导出前统一生成和规范化，"
    "不要把它们作为 error 或 warning 报出，也不要给出这类 repairs；审核只关注内容与数据语义。\n"
)

_REVIEW_COMMON_NOTES = (
    "【通用判定口径】\n"
    "1. 数据文件的“选项编码”列是打包键值（如 q2：A；q5：B/C），键 qN 与问卷文档题号可能存在偏移"
    "（例如数据中的 q5 可能对应问卷文档第4题）；复算前必须先用题目语义（选项数量、选项含义）"
    "确认对应字段（数据摘要中若给出键位规律说明，先按该说明核对），确认不了对应关系时"
    "不得据此判定数字错误；\n"
    "2. 写作指令要求“注明数据来源”时，中文自然称谓（如“调查问卷数据”“问卷作答记录”）即视为满足，"
    "不得要求正文出现文件名、工作表名或文件路径；\n"
    "3. 对于可以通过补充或修改一两句解决的缺失（如缺少一句来源说明、表述不清），"
    "必须给出可精确定位的 repairs（old 在目标内容中唯一出现），而不是只在 issues 里报问题。\n"
    "4. 成果中出现修改过程、版本说明、‘已修正/原误用’等过程性文字属于未完成痕迹，"
    "应作为 error 报出并给出可定位的 repairs。\n"
    "5. 成文里出现传输格式残留（如 {\"content\":…} JSON 外壳、content: 字段名）"
    "属于未清理的半成品，应作为 error 报出；无法用一句话修好时不要给 repairs，"
    "直接要求重新生成该模块。\n"
    "6. 成文（正文、表格、图题与图内文字、表注）引用问卷题目必须用中文题号（如“第X题”）；"
    "数字题号的写法由系统导出前统一转换（“第11题”会自动变为“第十一题”），"
    "不要因为数字/中文写法差异报 error；但 Q1、Q2、qN 等字母编码属于内部写法，"
    "应作为 error 报出并优先给出可直接应用的 repairs（表格单元格用 table_cell，"
    "正文或图内文字用 text；old 必须是唯一可定位的片段），确无法唯一匹配时才要求重新生成；"
    "题号对照表的说明不得再写“沿用原问卷编号”。\n"
    "7. 成文不得出现资料完备性或对码过程声明（如“未取得正式施测编码表”"
    "“据现有问卷结构对应”等），属于内部信息，应作为 error 报出并修复"
    "（删除该句或改写成口径必需的一句话）。\n"
    "8. 图表排版可读性：单面板类别过多（经验上超过 10 个）、类别标签过长或把群体/题号"
    "逐条重复塞进标签、多群体混排未拆面板、数值标注明显会堆叠——属于需要返工的可读性问题，"
    "应作为 error 报出；修正要求必须直接可执行（拆为 charts 分面板、设 layout=\"horizontal\"、"
    "精简 labels、把群体与题号移到面板标题）。\n"
)

MODULE_REVIEW_GOAL = (
    "【任务】审核一个报告模块的生成成果是否可靠。\n"
    "工作目录有 review_input.json（模块位置、写作指令、成果内容、数据摘要）；"
    "./data 是原始数据文件，你可以用 run_python 抽查或重新计算其中的数字。\n"
    "审核要点（通用，不针对特定行业主题）：\n"
    "1. 内容是否完成了写作指令的要求，是否存在占位、空内容或半成品；\n"
    "2. 数字能否由 ./data 数据复算得到，占比/勾选率/分母的口径是否正确；\n"
    "3. 表格与图表是否内部自洽（行列、标签与数值一致），标题与内容是否匹配，"
    "图表排版是否可读（类别密度、标签长度、分组呈现、标注密度）；\n"
    "4. 是否存在明显的编造、与数据矛盾或自相矛盾的表述；\n"
    "5. 方法说明是否简明、不重复堆砌。\n"
    "6. 图表成果的顶层 caption 用完整的图题描述本图内容；图号由系统导出时自动生成，"
    "缺失图号不算问题，也不要要求模型补图号；子图标题只写对象与题目，不要写图号。\n"
    "7. 同一句或同一组并列的多个数字必须逐项复算且口径一致（例如同一句按“A+B”合并，"
    "则每个群体都按“A+B”）；出现组内口径混用（个别项只取 A、其余取 A+B）时按 error 报出，"
    "并给出应改成的一致口径数字。\n"
    "对图表、表格类模块，必须用 run_python 从 ./data 复算其中至少一项数值（占比、频次）"
    "与图表/表格数据核对，核对一致才可通过；复算时先按选项语义确认数据字段与题目的对应关系。\n"
    "需要修正时给出 repairs：text 修正的 old 必须是目标内容中唯一出现的文本片段"
    "（正文句子、表格/图表 JSON 内的图注文字均可）；"
    "table_cell 修正用 cell 定位行列；修正不得引入无法从数据复算的数字。\n"
    "审核完成用 finish 提交上述 JSON；也可以先把 JSON 写入 ./out/review.json 再以 "
    '{"result_file":"out/review.json"} 提交。\n'
) + _REVIEW_COMMON_NOTES + REVIEW_OUTPUT_GUIDE

LIGHT_REVIEW_GOAL = (
    "【任务】复核一个刚生成的报告模块（正文/表格/图表数据）。\n"
    "输入包含写作指令、成果内容与数据文件结构摘要（本步骤无法运行代码）。\n"
    "检查：是否完成指令、是否明显编造或内容不完整、数字与口径是否可疑、结构是否自洽；"
    "正文是否残留 {\"content\":…} 之类的 JSON 外壳或字段名。\n"
    "没有把握的细节不要挑剔；只在确有把握的问题上标 error。\n"
) + _REVIEW_COMMON_NOTES + REVIEW_OUTPUT_GUIDE

FINAL_REVIEW_GOAL = (
    "【任务】整稿一致性复核：查看各模块在报告中的位置与内容摘要，检查跨模块的明显问题"
    "（同一指标数字或口径冲突、内容自相矛盾、术语前后不一、大段重复自述）。\n"
    "只报告能确定的问题，不提出需要重写整模块的建议。\n"
    "review_input 的“模块”列表给出每个模块的 task_id、位置与内容摘要；“位置”是模板章节结构，"
    "不是成果文字，不要把它当作内容问题上报。\n"
    "error 级问题必须能对应到“模块”列表中的模块：task_ids 填涉及的全部模块编号，"
    "message 说明冲突细节；无法指认到具体模块的观察一律按 warning 报告。\n"
) + _REVIEW_COMMON_NOTES + FINAL_REVIEW_OUTPUT_GUIDE


def _trim(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "……（截断）"


_TABLE_DISPLAY_FIELDS = ("title", "columns", "rows", "note", "notes", "caption", "source")


def _reviewable_content(row: ModuleContent) -> str:
    """审核展示用的内容：兼容旧版图片元数据；表格/图表只保留展示字段。"""
    content = (row.content or "").strip() or (getattr(row, "evidence_json", "") or "").strip()
    if row.content_type not in {"table", "image"}:
        return content
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content
    if row.content_type == "table" and isinstance(payload, dict):
        table = payload.get("table") if isinstance(payload.get("table"), dict) else payload
        return json.dumps({key: table[key] for key in _TABLE_DISPLAY_FIELDS if key in table},
                          ensure_ascii=False)
    if row.content_type == "image" and isinstance(payload, dict):
        def strip(chart: dict) -> dict:
            return {key: value for key, value in chart.items()
                    if key != "evidence_ids" and not str(key).endswith("_evidence")}

        stripped = strip(payload)
        if isinstance(payload.get("charts"), list):
            stripped["charts"] = [strip(item) for item in payload["charts"]
                                  if isinstance(item, dict)]
        return json.dumps(stripped, ensure_ascii=False)
    return content


def _normalize_review_payload(raw: str, *,
                              module_ids: set[int] | None = None) -> dict[str, Any]:
    """解析并校验审核模型的输出协议；不符合协议时抛 ReviewError。

    module_ids 提供时（整稿复核），error 级问题必须给出这些模块编号，
    确保每个拦截项都能定位到具体模块并进入修复闭环。
    """
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|```\s*$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ReviewError("审核输出中没有 JSON")
    candidate = text[start:end + 1]
    obj = None
    for strict in (True, False):
        try:
            obj = json.loads(candidate, strict=strict)
            break
        except json.JSONDecodeError:
            continue
    if obj is None:
        raise ReviewError("审核输出不是合法 JSON")
    return _validate_review_object(obj, module_ids)


def _issue_task_ids(value: Any, known: set[int] | None = None) -> list[int]:
    """解析问题涉及的模块编号；known 提供时只保留本次审核范围内的编号。"""
    raw = value if isinstance(value, (list, tuple)) else [value]
    ids: list[int] = []
    for item in raw:
        match = re.search(r"\d+", str(item))
        if match is None:
            continue
        task_id = int(match.group())
        if (known is not None and task_id not in known) or task_id in ids:
            continue
        ids.append(task_id)
    return ids


def _validate_review_object(obj: Any, module_ids: set[int] | None = None) -> dict[str, Any]:
    if not isinstance(obj, dict) or obj.get("status") not in {"ok", "issues"}:
        raise ReviewError("审核 JSON 缺少 status（ok|issues）")
    raw_issues = obj.get("issues", [])
    if not isinstance(raw_issues, list):
        raise ReviewError("审核 issues 必须是数组")
    issues = []
    for item in raw_issues:
        if not isinstance(item, dict):
            raise ReviewError("审核 issues 的每一项必须是对象")
        severity = item.get("severity")
        message = str(item.get("message", "")).strip()
        if severity not in SEVERITIES or not message:
            raise ReviewError("审核 issues 每项需要 severity（error|warning）与 message")
        entry = {"severity": severity, "message": message}
        ids = _issue_task_ids(item.get("task_ids", item.get("task_id")), module_ids)
        if severity == "error" and module_ids is not None and not ids:
            raise ReviewError(
                "error 级问题必须给出 review_input 模块列表中的 task_ids，"
                "无法定位到模块的观察请按 warning 报告"
            )
        if ids:
            entry["task_ids"] = ids
        issues.append(entry)
    raw_repairs = obj.get("repairs", [])
    if not isinstance(raw_repairs, list):
        raise ReviewError("审核 repairs 必须是数组")
    repairs = []
    for item in raw_repairs:
        # 修正项只做容错：形状不完整的条目直接丢弃（不应用），问题清单仍按严格协议校验。
        if not isinstance(item, dict):
            logger.warning("审核 repairs 存在非对象条目，已忽略")
            continue
        repair_type = item.get("type")
        old, new = str(item.get("old", "")), str(item.get("new", ""))
        if repair_type not in {"text", "table_cell"} or not old or not new:
            logger.warning("审核 repairs 条目缺少 type/old/new，已忽略：%s", str(item)[:120])
            continue
        if repair_type == "table_cell" and not re.fullmatch(r"r\d+c\d+", str(item.get("cell", ""))):
            logger.warning("审核 table_cell 修正缺少 cell，已忽略：%s", str(item)[:120])
            continue
        try:
            task_id = int(item.get("task_id"))
        except (TypeError, ValueError):
            logger.warning("审核 repairs 条目缺少 task_id，已忽略：%s", str(item)[:120])
            continue
        repairs.append({
            "task_id": task_id,
            "type": repair_type,
            "old": old,
            "new": new,
            "cell": str(item.get("cell", "")),
        })
    return {"status": obj["status"], "issues": issues, "repairs": repairs}


def format_issues(issues: list[dict[str, Any]], limit: int = 8) -> str:
    """把审核问题整理成一行行可读文本（供导出错误信息展示）。"""
    lines = []
    for item in issues[:limit]:
        title = item.get("title") or f"任务{item.get('task_id', '')}"
        severity = item.get("severity", "error")
        label = "严重" if severity == "error" else "提示"
        lines.append(f"（{label}）{title}：{item.get('message', '')}")
    if len(issues) > limit:
        lines.append(f"……等共 {len(issues)} 条问题")
    return "；".join(lines) if lines else "无"


# ---------------------------------------------------------------- 生成内复核

async def review_generated_content(
    db: Session,
    cfg,
    *,
    location: str,
    instruction: str,
    task_type: str,
    content_text: str,
    references: str,
    data_summary: str,
) -> dict[str, Any]:
    """生成内轻量复核：单次模型调用；异常或协议错误重试一次后抛 ReviewUnavailableError。"""
    payload = {
        "位置": location,
        "类型": {"text": "正文", "table": "表格", "image": "图表数据"}.get(task_type, task_type),
        "写作指令": _trim(instruction, 4000),
        "成果内容": _trim(content_text, 8000),
        "数据摘要": _trim(data_summary, 4000),
        "参考资料摘要": _trim(references, 3000),
    }
    system_prompt = "\n\n".join(
        part for part in (render_system_prompt(db, cfg), LIGHT_REVIEW_GOAL) if part
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "review_input:\n" + json.dumps(payload, ensure_ascii=False)},
    ]
    last_error = ""
    for _attempt in range(3):
        try:
            raw = await generate.chat_completion(
                cfg, messages, timeout=settings.LLM_REQUEST_TIMEOUT)
        except Exception as exc:
            raise ReviewUnavailableError(f"审核模型调用失败：{exc}") from exc
        try:
            return _normalize_review_payload(raw)
        except ReviewError as exc:
            last_error = str(exc)
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": f"输出不符合要求：{last_error}。请只输出规定 JSON。",
            })
    raise ReviewUnavailableError(f"审核输出连续 3 次不符合协议：{last_error}")


# ---------------------------------------------------------------- 导出前审核

def _current_contents(db: Session, project_id: int) -> dict[int, ModuleContent]:
    tasks = db.scalars(
        select(ChapterTask).where(ChapterTask.project_id == project_id)
    ).all()
    rows = db.scalars(
        select(ModuleContent).where(
            ModuleContent.chapter_task_id.in_([t.id for t in tasks] or [0]),
            ModuleContent.is_current == True,  # noqa: E712
            ModuleContent.status == "ok",
        )
    ).all()
    return {row.chapter_task_id: row for row in rows}


def _cached_review(row: ModuleContent, project: Project, task: ChapterTask) -> dict[str, Any] | None:
    """未变更模块复用已存复核结论（只有 ok/warning 会被持久化）。"""
    try:
        record = json.loads(row.review_json or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict) or record.get("status") not in {"ok", "warning"}:
        return None
    if record.get("protocol") != REVIEW_PROTOCOL_VERSION:
        return None
    if record.get("material_revision") != project.material_revision:
        return None
    if record.get("instruction_revision") != task.instruction_revision:
        return None
    if record.get("content_sha256") != sha256(row.content.encode("utf-8")).hexdigest():
        return None
    issues = record.get("issues", [])
    if not isinstance(issues, list):
        return None
    return {"status": "issues" if issues else "ok", "issues": issues, "repairs": []}


def _store_review(row: ModuleContent, project: Project, task: ChapterTask,
                  result: dict[str, Any]) -> None:
    record = {
        "status": "warning" if result["issues"] else "ok",
        "issues": result["issues"],
        "protocol": REVIEW_PROTOCOL_VERSION,
        "content_sha256": sha256(row.content.encode("utf-8")).hexdigest(),
        "material_revision": project.material_revision,
        "instruction_revision": task.instruction_revision,
        "reviewed_at": int(time.time()),
    }
    row.review_json = json.dumps(record, ensure_ascii=False)


def _review_finish_validator(payload: str, workdir):
    try:
        payload = generate._read_finish_file("text", payload, workdir)
    except (OSError, ValueError, TypeError) as exc:
        return False, f"审核成果文件无法提交：{exc}", ""
    try:
        obj = _normalize_review_payload(payload)
    except ReviewError as exc:
        return False, str(exc), ""
    return True, "", json.dumps(obj, ensure_ascii=False)


def _load_data_files(db: Session, project: Project) -> list[tuple[str, bytes]]:
    """数据文件只读取一次，供并行复核的各 worker 复用。"""
    files = db.scalars(
        select(ProjectFile).where(
            ProjectFile.project_id == project.id, ProjectFile.file_type == "data")
    ).all()
    loaded: list[tuple[str, bytes]] = []
    for item in files:
        if Path(item.file_name).name != item.file_name or "\\" in item.file_name:
            continue
        loaded.append((item.file_name, storage.download_bytes(item.minio_path)))
    return loaded


def _write_data_files(workdir: Path, data_files: list[tuple[str, bytes]]) -> None:
    data_dir = workdir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in data_files:
        (data_dir / name).write_bytes(payload)


def _copy_data_files(db: Session, project: Project, workdir: Path) -> None:
    _write_data_files(workdir, _load_data_files(db, project))


_CHART_SCHEMA_TERMS = re.compile(r"\blabels\b|\bcaption\b|图内标题|子图标题", re.IGNORECASE)
_NUMBERING_TERMS = re.compile(r"图号|表号|题号|图内标题|子图标题|\blabels\b|\bcaption\b",
                              re.IGNORECASE)
_DATA_TERMS = re.compile(r"数字|数值|数据|百分比|占比|频次|计数|分母|样本|复算|互换|"
                         r"不一致|不符|错误|缺失|遗漏", re.IGNORECASE)
_VALUE_TERMS = re.compile(r"数值|数据|百分比|占比|频次|计数|分母|样本|复算|互换|"
                          r"不一致|不符|错误|缺失|遗漏", re.IGNORECASE)


def _is_numbering_only(message: str) -> bool:
    text = str(message or "")
    return bool(_NUMBERING_TERMS.search(text)) and not _VALUE_TERMS.search(text)


def _sanitize_review_issues(issues: list[dict[str, Any]],
                            tasks_by_id: dict[int, ChapterTask],
                            default_task: ChapterTask | None = None) -> list[dict[str, Any]]:
    """把系统负责的编号/格式问题和明显错位的指认降级为提示。

    编号由导出前的确定性层统一处理；审核模型的职责只有内容与数据语义。
    """
    for item in issues:
        if item.get("severity") != "error":
            continue
        message = str(item.get("message") or "")
        if _is_numbering_only(message):
            item["message"] = message + "（编号/题号格式由系统统一处理，已转为提示）"
            item["severity"] = "warning"
            logger.info("审核编号类问题转为提示：%s", message[:120])
            continue
        ids = item.get("task_ids")
        if not ids and item.get("task_id") is not None:
            ids = [item["task_id"]]
        targets = [tasks_by_id.get(task_id) for task_id in (ids or [])]
        targets = [task for task in targets if task is not None]
        if not targets and default_task is not None:
            targets = [default_task]
        if (_CHART_SCHEMA_TERMS.search(message) and targets
                and all(task.task_type != "image" for task in targets)):
            item["message"] = message + "（审核指认与模块类型不符，已转为提示）"
            item["severity"] = "warning"
            logger.warning("审核指认与模块类型不符，已降级：%s", message[:120])
    return issues


async def _module_review(db: Session, cfg, project: Project, task: ChapterTask,
                         row: ModuleContent, location: str, references: str,
                         data_summary: str, run_token: str | None, *,
                         data_files: list[tuple[str, bytes]] | None = None) -> dict[str, Any]:
    """带沙箱的模块复核：可运行代码抽查数据；输出 issues 与定向 repairs。"""
    review_input = {
        "task_id": task.id,
        "位置": location,
        "类型": {"text": "正文", "table": "表格", "image": "图表数据"}.get(task.task_type),
        "写作指令": _trim(task.instruction, 4000),
        "成果内容": _trim(_reviewable_content(row), 8000),
        "数据摘要": _trim(data_summary, 4000),
        "参考资料摘要": _trim(references, 3000),
    }
    last_error: Exception | None = None
    attempts = max(1, int(settings.REVIEW_MODULE_ATTEMPTS or 3))
    for _attempt in range(attempts):
        try:
            workdir = sandbox.fresh_workdir(project.id, -(1000 + task.id), run_token)
            if data_files is None:
                _copy_data_files(db, project, workdir)
            else:
                _write_data_files(workdir, data_files)
            (workdir / "review_input.json").write_text(
                json.dumps(review_input, ensure_ascii=False, indent=2), encoding="utf-8")
            raw = await generate.react_run(
                db, cfg, MODULE_REVIEW_GOAL, workdir, "review",
                finish_validator=_review_finish_validator,
            )
            result = json.loads(raw)
            result["issues"] = _sanitize_review_issues(
                list(result.get("issues") or []), {task.id: task}, default_task=task)
            return result
        except Exception as exc:
            last_error = exc
            logger.warning("模块 %s 复核第 %s/%s 次失败：%s",
                           task.id, _attempt + 1, attempts, exc)
    raise last_error


async def _final_review(db: Session, cfg, project: Project, tasks: list[ChapterTask],
                        current: dict[int, ModuleContent],
                        locations: dict[int, str]) -> dict[str, Any]:
    """整稿复核：只给 AI 看报告位置与内容摘要，跨模块问题必须指认模块编号。"""
    modules = [
        {
            "task_id": task.id,
            "位置": locations[task.id],
            "类型": {"text": "正文", "table": "表格", "image": "图表数据"}.get(task.task_type),
            "内容": _trim(_reviewable_content(current[task.id]),
                          360 if task.task_type in {"image", "table"} else 200),
        }
        for task in tasks if task.id in current
    ]
    payload = {
        "项目": project.name,
        "说明": "每个模块的“内容”会写入“位置”对应的章节；审核对象是各模块之间的相互一致性。",
        "模块": modules,
    }
    module_ids = {item["task_id"] for item in modules}
    system_prompt = "\n\n".join(
        part for part in (render_system_prompt(db, cfg), FINAL_REVIEW_GOAL) if part
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "review_input:\n" + json.dumps(payload, ensure_ascii=False)},
    ]
    last_error = ""
    for _attempt in range(3):
        raw = await generate.chat_completion(cfg, messages, timeout=settings.LLM_REQUEST_TIMEOUT)
        try:
            result = _normalize_review_payload(raw, module_ids=module_ids)
            result["issues"] = _sanitize_review_issues(
                list(result.get("issues") or []),
                {task.id: task for task in tasks})
            return result
        except ReviewError as exc:
            last_error = str(exc)
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": (f"输出不符合要求：{last_error}。请只输出一个完整 JSON 对象"
                            "（不要任何解释或 markdown），issues 不超过 10 条，"
                            "每条 message 不超过 200 字。"),
            })
    raise ReviewError(last_error)


def _coerce_cell(value: str):
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return value


def _error_entry(task: ChapterTask, location: str, message: str) -> dict[str, Any]:
    """导出拦截项：标题用章节位置，避免把写作指令当作标题展示。"""
    return {"severity": "error", "task_id": task.id, "task_ids": [task.id],
            "title": location, "message": message}


def _warning_entries(tasks: list[ChapterTask], locations: dict[int, str],
                     results: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"severity": "warning", "task_id": task.id, "title": locations[task.id],
         "message": item["message"]}
        for task in tasks for item in results.get(task.id, {}).get("issues", [])
        if item["severity"] == "warning"
    ]


async def _review_modules_parallel(
        project_id: int, cfg, pending: list[ChapterTask], locations: dict[int, str],
        references: str, data_summary: str, run_token: str | None,
        results: dict[int, dict[str, Any]],
        data_files: list[tuple[str, bytes]], done: int, total: int,
        label: str = "模块复核") -> int:
    """受限并行的逐模块复核；每个 worker 使用独立会话，完成后落库并上报进度。"""
    semaphore = asyncio.Semaphore(max(1, int(getattr(settings, "REVIEW_CONCURRENCY", 5))))

    async def worker(task: ChapterTask) -> tuple[int, dict[str, Any]]:
        async with semaphore:
            try:
                with SessionLocal() as wdb:
                    wproject = wdb.get(Project, project_id)
                    wtask = wdb.get(ChapterTask, task.id)
                    wrow = wdb.scalars(select(ModuleContent).where(
                        ModuleContent.chapter_task_id == task.id,
                        ModuleContent.is_current == True,  # noqa: E712
                        ModuleContent.status == "ok",
                    )).first()
                    if (wproject is None or wtask is None or wrow is None
                            or not (wrow.content or "").strip()):
                        return task.id, {"status": "issues", "repairs": [], "issues": [
                            {"severity": "error",
                             "message": "模块内容为空或尚未生成，请先生成该模块"}]}
                    try:
                        result = await _module_review(
                            wdb, cfg, wproject, wtask, wrow, locations.get(task.id, ""),
                            references, data_summary, run_token, data_files=data_files)
                    except Exception as exc:
                        # 复核没跑完不等于内容有问题：记提示放行，不重生成模块。
                        logger.warning("模块 %s 复核未完成：%s", task.id, exc)
                        return task.id, {"status": "issues", "repairs": [], "issues": [
                            {"severity": "warning",
                             "message": f"导出前复核未完成（已放行）：{exc}"}]}
                    if not any(item["severity"] == "error" for item in result["issues"]):
                        _store_review(wrow, wproject, wtask, result)
                    wdb.commit()
                    return task.id, result
            except Exception as exc:  # worker 兜底：单模块异常不打断整批
                logger.warning("模块 %s 复核任务异常：%s", task.id, exc)
                return task.id, {"status": "issues", "repairs": [], "issues": [
                    {"severity": "warning",
                     "message": f"导出前复核未完成（已放行）：{exc}"}]}

    futures = [asyncio.ensure_future(worker(task)) for task in pending]
    for future in asyncio.as_completed(futures):
        task_id, result = await future
        results[task_id] = result
        done += 1
        operations.report_progress(project_id, run_token, phase="review",
                                   label=label, done=done, total=total)
    return done


async def _regenerate_tasks_parallel(
        project_id: int, pending: list[ChapterTask], feedback: dict[int, str],
        results: dict[int, dict[str, Any]], run_token: str | None,
        done: int, total: int) -> tuple[int, set[int]]:
    """受限并行的模块重生成（复用 GENERATE_CONCURRENCY）；每完成一项即刷新进度。"""
    semaphore = asyncio.Semaphore(max(1, int(getattr(settings, "GENERATE_CONCURRENCY", 8))))

    async def worker(task: ChapterTask) -> tuple[int, str]:
        async with semaphore:
            try:
                with SessionLocal() as wdb:
                    wproject = wdb.get(Project, project_id)
                    wtask = wdb.get(ChapterTask, task.id)
                    if wproject is None or wtask is None:
                        return task.id, "任务已失效"
                    row = await generate.generate_task(
                        wdb, wproject, wtask, user_prompt=feedback.get(task.id, ""),
                        run_token=run_token)
                    if row is not None and row.status == "ok":
                        return task.id, ""
                    return task.id, getattr(row, "error_msg", "") or "未知错误"
            except Exception as exc:
                logger.warning("模块 %s 重新生成异常：%s", task.id, exc)
                return task.id, str(exc)

    futures = [asyncio.ensure_future(worker(task)) for task in pending]
    succeeded: set[int] = set()
    for future in asyncio.as_completed(futures):
        task_id, note = await future
        done += 1
        if note:
            results[task_id] = {"status": "issues", "repairs": [], "issues": [
                {"severity": "error", "message": f"重新生成未完成：{note}"}]}
        else:
            succeeded.add(task_id)
        operations.report_progress(project_id, run_token, phase="repair",
                                   label="定向修正/重新生成", done=done, total=total)
    return done, succeeded


def _apply_repairs(db: Session, project: Project, repairs: list[dict[str, Any]],
                   model: str, run_token: str | None = None) -> set[int]:
    """应用定向修正（旧文本/单元格唯一匹配才生效），生成新版本等待复评。"""
    applied: set[int] = set()
    current = _current_contents(db, project.id)
    for repair in repairs:
        task_id = repair["task_id"]
        row = current.get(task_id)
        task = db.get(ChapterTask, task_id)
        if row is None or task is None or not operations.content_is_valid(row, task, project):
            continue
        old, new = repair["old"], repair["new"]
        if repair["type"] == "text":
            if row.content.count(old) != 1:
                continue
            content = row.content.replace(old, new)
            # 结构化内容中的文本片段（表注/图注）修正后必须仍是合法 JSON。
            if row.content_type in {"table", "image"}:
                try:
                    json.loads(content)
                except json.JSONDecodeError:
                    continue
        elif repair["type"] == "table_cell" and row.content_type == "table":
            match = re.fullmatch(r"r(\d+)c(\d+)", repair["cell"])
            try:
                table = json.loads(row.content)
            except json.JSONDecodeError:
                continue
            rows = table.get("rows") if isinstance(table, dict) else None
            row_index, col_index = int(match.group(1)), int(match.group(2))
            if (not isinstance(rows, list) or row_index >= len(rows)
                    or not isinstance(rows[row_index], list) or col_index >= len(rows[row_index])):
                continue
            if str(rows[row_index][col_index]) != old:
                continue
            rows[row_index][col_index] = _coerce_cell(new)
            content = json.dumps(table, ensure_ascii=False)
        else:
            continue
        operations.ensure_run(db, project.id, run_token)
        generate._save_content(
            db, task, row.content_type, content, row.image_path,
            "导出审核自动修正", "review", model, 0,
        )
        applied.add(task_id)
    if applied:
        db.commit()
    return applied


def _issue_signature(issues: list[dict[str, Any]]) -> str:
    """把 error 集归一成“任务 + 问题类别”的签名，避免措辞变化绕过无进展检测。"""
    parts: set[str] = set()
    for item in issues:
        if item.get("severity") != "error":
            continue
        ids = item.get("task_ids")
        if not ids and item.get("task_id") is not None:
            ids = [item["task_id"]]
        key = ",".join(str(value) for value in sorted(ids or []))
        parts.add(f"{key}:{_issue_category(str(item.get('message') or ''))}")
    return "\n".join(sorted(parts))


def _issue_category(message: str) -> str:
    text = str(message or "")
    if _NUMBERING_TERMS.search(text):
        return "numbering"
    if re.search(r"缺失|缺少|遗漏|为空|未生成|未提供", text):
        return "missing"
    if re.search(r"冲突|矛盾|重复|不一致|不符", text):
        return "conflict"
    if _DATA_TERMS.search(text):
        return "data"
    return "other"


def _review_stop_issue(reason: str, detail: str) -> dict[str, Any]:
    return {
        "severity": "error",
        "title": "自动修复已停止",
        "message": f"{reason}：{detail}。请根据上面的问题手动修改后重新导出。",
    }


def _normalize_chart_captions(db: Session, project: Project,
                              tasks: list[ChapterTask],
                              run_token: str | None) -> None:
    """导出前确定性编号与规范化：图号、表号、题号与引用。

    模型只写内容；编号和格式由系统接管，避免重复图号、阿拉伯题号等
    结构性问题反复进入 AI 审核与重生成。
    """
    current = _current_contents(db, project.id)
    by_id = {task.id: task for task in tasks}
    figure_numbers = report_numbering.figure_number_map(tasks)
    table_numbers = report_numbering.table_number_map(tasks)

    figure_pairs: dict[str, set[str]] = {}
    table_pairs: dict[str, set[str]] = {}
    for task_id, row in current.items():
        task = by_id.get(task_id)
        if task is None:
            continue
        if task.task_type == "image" and task_id in figure_numbers:
            number = figure_numbers[task_id]
            for old in report_numbering.collect_figure_numbers(row.content):
                figure_pairs.setdefault(old, set()).add(number)
        elif task.task_type == "table" and task_id in table_numbers:
            number = f"表{table_numbers[task_id]}"
            for old in report_numbering.collect_table_number(row.content):
                table_pairs.setdefault(old, set()).add(number)
    figure_refs = report_numbering.unique_reference_map(figure_pairs)
    table_refs = report_numbering.unique_reference_map(table_pairs)
    table_refs = {old: new for old, new in table_refs.items() if old != new}

    changed = False
    for task_id, row in current.items():
        task = by_id.get(task_id)
        if task is None:
            continue
        normalized, _old = report_numbering.normalize_row_content(
            row.content, task.task_type,
            figure_number=figure_numbers.get(task_id),
            table_number=table_numbers.get(task_id),
            figure_refs=figure_refs,
            table_refs=table_refs,
        )
        if normalized == row.content:
            continue
        logger.info("导出前规范化：任务 %s（%s）内容已更新", task_id, task.task_type)
        operations.ensure_run(db, project.id, run_token)
        generate._save_content(
            db, task, task.task_type, normalized, row.image_path,
            "导出前确定性规范化（图号/表号/题号）", "review", "", 0)
        changed = True
    if changed:
        db.commit()


async def review_and_repair(db: Session, project: Project, max_rounds: int | None = None,
                            run_token: str | None = None) -> dict[str, Any]:
    """导出前审核：逐模块 AI 复核 → 自动修正 → 复评 → 整稿复核 → 分级放行。

    只有 AI 明确判定的 error 拦截导出；复核未完成（模型或协议问题）记提示放行。
    """
    cfg = generate.pick_model(db, project.user_id, "review")
    if cfg is None:
        return {"ok": False, "issues": [
            {"severity": "error", "title": "审核配置",
             "message": "未配置审核模型，请在“AI 配置”页面添加"}]}
    references, data_summary = generate.build_materials(db, project)
    tasks = db.scalars(
        select(ChapterTask)
        .where(ChapterTask.project_id == project.id, ChapterTask.task_type != "heading")
        .order_by(ChapterTask.sort)
    ).all()
    locations = {task.id: generate.document_location(db, task) for task in tasks}
    _normalize_chart_captions(db, project, tasks, run_token)
    results: dict[int, dict[str, Any]] = {}
    data_files = _load_data_files(db, project)
    total = len(tasks)
    max_rounds = (int(settings.REVIEW_MAX_ROUNDS or 0)
                  if max_rounds is None else max(0, int(max_rounds)))
    budget = max(0.0, float(settings.REVIEW_MAX_SECONDS or 0))
    deadline = time.monotonic() + budget if budget > 0 else None
    history: dict[int, list[str]] = {}
    task_regen_counts: dict[int, int] = {}
    regen_cap = max(1, int(settings.REVIEW_MAX_TASK_REGENS or 4))
    last_signature = ""
    stalled_rounds = 0
    round_no = 0
    while True:
        current = _current_contents(db, project.id)
        done_count = 0
        pending: list[ChapterTask] = []
        for task in tasks:
            if task.id in results:
                done_count += 1
                continue
            row = current.get(task.id)
            if row is None or not (row.content or "").strip():
                results[task.id] = {"status": "issues", "repairs": [], "issues": [
                    {"severity": "error", "message": "模块内容为空或尚未生成，请先生成该模块"}]}
                done_count += 1
                continue
            result = _cached_review(row, project, task)
            if result is not None:
                results[task.id] = result
                done_count += 1
                continue
            pending.append(task)
        operations.report_progress(project.id, run_token, phase="review",
                                   label="模块复核", done=done_count, total=total)
        if pending:
            done_count = await _review_modules_parallel(
                project.id, cfg, pending, locations, references, data_summary,
                run_token, results, data_files, done_count, total)
            # 并行 worker 用独立会话提交了新版本，父会话重新读取避免陈旧对象。
            db.expire_all()
        errors = [
            _error_entry(task, locations[task.id], item["message"])
            for task in tasks for item in results.get(task.id, {}).get("issues", [])
            if item["severity"] == "error"
        ]
        if not errors:
            break
        for task in tasks:
            if not any(item["severity"] == "error"
                       for item in results.get(task.id, {}).get("issues", [])):
                history.pop(task.id, None)
        if max_rounds and round_no >= max_rounds:
            db.commit()
            return {"ok": False, "issues": errors + [
                _review_stop_issue("轮次上限", f"已完成 {round_no} 轮自动修复")]}
        if deadline is not None and time.monotonic() > deadline:
            db.commit()
            return {"ok": False, "issues": errors + [
                _review_stop_issue("时间预算", f"审核已超过 {int(budget)} 秒")]}
        signature = _issue_signature(errors)
        if signature == last_signature:
            stalled_rounds += 1
        else:
            last_signature, stalled_rounds = signature, 0
        if stalled_rounds >= max(1, int(settings.REVIEW_NO_PROGRESS_LIMIT)):
            db.commit()
            return {"ok": False, "issues": errors + [
                _review_stop_issue("无进展", f"连续 {stalled_rounds + 1} 轮问题没有变化")]}
        target_ids = {item["task_id"] for item in errors}
        fix_total = max(1, len(target_ids))
        operations.report_progress(project.id, run_token, phase="repair",
                                   label="定向修正/重新生成", done=0, total=fix_total)
        repairs = [
            repair for task in tasks
            for repair in results.get(task.id, {}).get("repairs", [])
        ]
        applied = _apply_repairs(db, project, repairs, cfg.model, run_token) if repairs else set()
        fix_done = len(applied)
        if fix_done:
            operations.report_progress(project.id, run_token, phase="repair",
                                       label="定向修正/重新生成",
                                       done=fix_done, total=fix_total)
        # 无可用定向修正的模块：带着审核意见自动重新生成（等价于人工点“重写”）。
        regenerated: set[int] = set()
        unresolved_tasks = [task for task in tasks if task.id in target_ids - applied]
        capped_tasks = [task for task in unresolved_tasks
                        if task_regen_counts.get(task.id, 0) >= regen_cap]
        unresolved_tasks = [task for task in unresolved_tasks
                            if task_regen_counts.get(task.id, 0) < regen_cap]
        for task in capped_tasks:
            message = f"该模块已自动重生成 {regen_cap} 次仍未通过，已停止自动重试，请人工处理"
            results[task.id] = {"status": "issues", "repairs": [], "issues": [{
                "severity": "error", "message": message}]}
            errors.append(_error_entry(task, locations[task.id], message))
            logger.warning("模块 %s 达到重生成上限 %s，停止自动重试", task.id, regen_cap)
        feedback_map: dict[int, str] = {}
        for task in unresolved_tasks:
            messages = [item["message"] for item in results[task.id]["issues"]
                        if item["severity"] == "error"]
            feedback_map[task.id] = (
                "【审核意见】上一版存在问题：" + "；".join(messages[:6])
                + "。请针对性补齐或修正后重新提交。写作指令中的示例数字仅供参考；"
                "凡与 ./data 复算结果或其他模块已核实数字不一致，一律以复算为准，"
                "并统一同组数字口径（如同一句按“A+B”合并，则每个群体都按“A+B”），"
                "与本问题无关的图号、称谓与已核实数字保持原样，不要改动；"
                "重新提交前逐项自检。"
            )
            prior = list(dict.fromkeys(history.get(task.id, [])))
            if prior:
                feedback_map[task.id] += (
                    "\n【前几轮已提出但未解决】" + "；".join(prior[-6:])
                    + "。上一轮修改没有解决这些问题，不要重复同样的改法；"
                    "请先对照 ./data 复算，核对字段、题号与选项语义的对应关系，"
                    "再换一种处理方式。")
            history.setdefault(task.id, []).extend(messages[:6])
            logger.info("模块 %s 按审核意见重新生成：%s", task.id, feedback_map[task.id][:160])
        if unresolved_tasks:
            for task in unresolved_tasks:
                task_regen_counts[task.id] = task_regen_counts.get(task.id, 0) + 1
            fix_done, regenerated = await _regenerate_tasks_parallel(
                project.id, unresolved_tasks, feedback_map, results, run_token,
                fix_done, fix_total)
        if not applied and not regenerated:
            db.commit()
            return {"ok": False, "issues": errors + [
                _review_stop_issue("无进展", "本轮没有可执行的修复或重生成")]}
        for task_id in applied | regenerated:
            results.pop(task_id, None)
        round_no += 1
        db.commit()
    db.commit()

    operations.report_progress(project.id, run_token, phase="final",
                               label="整稿一致性复核")
    current = _current_contents(db, project.id)
    final_errors: list[dict[str, Any]] = []
    # 上一轮重生成内容在复评中发现的问题；非空时跳过整稿复核，直接进入本轮修复。
    pending: list[dict[str, Any]] = []
    # 定位不到模块的整稿问题无法重生成修复，按提示记录而不是拦截。
    final_notes: list[dict[str, Any]] = []
    # 整稿复核给出的定向修复（可直接落盘的单点勘误），每轮随复核结果刷新。
    final_repairs: list[dict[str, Any]] = []

    final_round = 0
    final_history: dict[int, list[str]] = {}
    last_final_signature = ""
    final_stalled = 0
    final_stop: dict[str, Any] | None = None
    while True:
        if pending:
            final_errors, pending = pending, []
            final_repairs = []
        else:
            try:
                final = await _final_review(db, cfg, project, tasks, current, locations)
            except Exception as exc:
                # 整稿复核属于附加一致性检查；模型/协议抖动不应阻断导出，降级为提示并记录。
                logger.warning("final review failed", exc_info=True)
                warnings = _warning_entries(tasks, locations, results) + final_notes + [
                    {"severity": "warning", "title": "整稿复核",
                     "message": f"整稿复核未完成（不影响导出）：{exc}"}]
                return {"ok": True, "issues": [], "warnings": warnings}
            final_repairs = final.get("repairs", [])
            final_errors = []
            for item in final["issues"]:
                if item["severity"] != "error":
                    continue
                ids = [task_id for task_id in item.get("task_ids", []) if task_id in locations]
                if ids:
                    final_errors.append({
                        "severity": "error", "task_id": ids[0], "task_ids": ids,
                        "title": "整稿复核", "message": item["message"]})
                else:
                    final_notes.append({
                        "severity": "warning", "title": "整稿复核",
                        "message": item["message"] + "（无法定位到具体模块，按提示放行）"})
        if not final_errors:
            return {"ok": True, "issues": [],
                    "warnings": _warning_entries(tasks, locations, results) + final_notes}
        by_task: dict[int, list[str]] = {}
        for item in final_errors:
            for task_id in item["task_ids"]:
                by_task.setdefault(task_id, []).append(item["message"])
        target_ids = set(by_task) & {task.id for task in tasks}
        if max_rounds and final_round >= max_rounds:
            final_stop = _review_stop_issue("轮次上限", f"整稿复核已完成 {final_round} 轮修复")
            break
        if deadline is not None and time.monotonic() > deadline:
            final_stop = _review_stop_issue("时间预算", f"审核已超过 {int(budget)} 秒")
            break
        signature = _issue_signature(final_errors)
        if signature == last_final_signature:
            final_stalled += 1
        else:
            last_final_signature, final_stalled = signature, 0
        if final_stalled >= max(1, int(settings.REVIEW_NO_PROGRESS_LIMIT)):
            final_stop = _review_stop_issue("无进展", f"连续 {final_stalled + 1} 轮问题没有变化")
            break
        repair_ids = {item["task_id"] for item in final_repairs}
        fix_total = max(1, len(target_ids | repair_ids))
        operations.report_progress(project.id, run_token, phase="repair",
                                   label="定向修正/重新生成", done=0, total=fix_total)
        # 先落盘 AI 给出的单点勘误（唯一匹配才生效），再对无法直接修复的模块重生成。
        applied_final = (_apply_repairs(db, project, final_repairs, cfg.model, run_token)
                         if final_repairs else set())
        fix_done = len(applied_final)
        if fix_done:
            operations.report_progress(project.id, run_token, phase="repair",
                                       label="定向修正/重新生成",
                                       done=fix_done, total=fix_total)
        regen_tasks = [task for task in tasks if task.id in target_ids - applied_final]
        capped_final = [task for task in regen_tasks
                        if task_regen_counts.get(task.id, 0) >= regen_cap]
        regen_tasks = [task for task in regen_tasks
                       if task_regen_counts.get(task.id, 0) < regen_cap]
        for task in capped_final:
            final_errors.append({
                "severity": "error", "task_id": task.id, "task_ids": [task.id],
                "title": "自动修复已停止",
                "message": f"该模块已自动重生成 {regen_cap} 次仍未通过，请人工处理"})
            logger.warning("整稿复核：模块 %s 达到重生成上限 %s", task.id, regen_cap)
        feedback_map: dict[int, str] = {}
        for task in regen_tasks:
            messages = by_task.get(task.id, [])
            feedback_map[task.id] = (
                "【审核意见】整稿复核发现本模块与其他模块存在冲突："
                + "；".join(message[:400] for message in messages[:4])
                + "。请用 ./data 数据复算后修正本模块；统计前先按选项数量与含义确认数据字段"
                "与题目的对应关系，再据此更新正文或图表数据，保证与其他模块引用同一指标的数值一致。"
                "写作指令中的示例数字仅供参考；凡与复算结果或其他模块已核实数字不一致，"
                "一律以复算为准，并统一同组数字口径（如同一句按“A+B”合并，则每个群体都按“A+B”），"
                "与本问题无关的图号、称谓与已核实数字保持原样，不要改动；"
                "重新提交前逐项自检。"
            )
            prior = list(dict.fromkeys(final_history.get(task.id, [])))
            if prior:
                feedback_map[task.id] += (
                    "\n【前几轮已提出但未解决】" + "；".join(prior[-6:])
                    + "。上一轮修改没有解决这些问题，不要重复同样的改法；"
                    "请先复算并核对数据键，再换一种处理方式。")
            final_history.setdefault(task.id, []).extend(messages[:4])
            logger.info("整稿复核指认模块 %s，按意见重新生成", task.id)
        regenerated_final: set[int] = set()
        if regen_tasks:
            for task in regen_tasks:
                task_regen_counts[task.id] = task_regen_counts.get(task.id, 0) + 1
            fix_done, regenerated_final = await _regenerate_tasks_parallel(
                project.id, regen_tasks, feedback_map, results, run_token,
                fix_done, fix_total)
        if not applied_final and not regenerated_final:
            final_stop = _review_stop_issue("无进展", "本轮没有可执行的修复或重生成")
            break
        db.commit()
        # 复评：被修复或重生成的内容重新过一遍模块级沙箱复核，新问题进入下一轮修复。
        pending = await _recheck_regenerated(
            project.id, cfg, project, tasks, locations,
            applied_final | regenerated_final, references, data_summary,
            run_token, results, data_files)
        final_round += 1
    issues: list[dict[str, Any]] = ([final_stop] if final_stop else []) + final_errors
    issues += _warning_entries(tasks, locations, results) + final_notes
    return {"ok": False, "issues": issues}


async def _recheck_regenerated(
        project_id: int, cfg, project: Project, tasks: list[ChapterTask],
        locations: dict[int, str], regenerated: set[int], references: str,
        data_summary: str, run_token: str | None,
        results: dict[int, dict[str, Any]],
        data_files: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
    """整稿复核触发修复/重生成后的模块级复评（并行）；返回仍需修复的 error 清单。"""
    pending = [task for task in tasks if task.id in regenerated]
    for task in pending:
        results.pop(task.id, None)
    if pending:
        operations.report_progress(project_id, run_token, phase="review",
                                   label="复评", done=0, total=len(pending))
        await _review_modules_parallel(
            project_id, cfg, pending, locations, references, data_summary,
            run_token, results, data_files, 0, len(pending), label="复评")
    errors: list[dict[str, Any]] = []
    for task in pending:
        errors.extend(_error_entry(task, locations[task.id], item["message"])
                      for item in results.get(task.id, {}).get("issues", [])
                      if item["severity"] == "error")
    return errors
