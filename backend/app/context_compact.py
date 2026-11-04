"""ReAct 对话历史的 token 估算与自动压缩（上下文超限保护）。

估算刻意保守：东亚字符按 1 token、其余字符按 1/3 token（向上取整），
每条消息再叠加固定开销；宁可提前压缩，也不要撞上模型上下文上限。

压缩策略：保留 system 与 goal（摘要并入 goal 的 user 消息，保持
system/user 交替结构），保留最近 keep_turns 组「助手回复 + 观察」，
中间轮次交给摘要器压成一段过程摘要；摘要器调用失败时降级为机械摘要，
压缩永不阻断主流程。摘要器由调用方注入（react_run 里指向模块级
chat_completion，离线测试可 monkeypatch 拦截）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SUMMARY_MARKER = "【前情摘要（系统自动压缩）】"
_SUMMARY_TIP = "（此段由系统自动压缩生成；关键数字以数据文件与 out/ 下的成果文件为准）"

# 摘要请求的固定标识：离线测试的假模型靠它识别压缩调用。
COMPACT_REQUEST_MARKER = "【上下文压缩】"
COMPACT_SYSTEM_PROMPT = (
    "你是 ReAct 过程压缩器（上下文压缩）。把下面提供的多轮「行动-观察」记录"
    "压缩成一段不超过 {limit} 字的中文摘要，必须保留：已经完成的计算与结论要点、"
    "引用的证据ID、结果文件（out/*.json）的当前状态、尚未解决的问题与下一步计划。"
    "不得编造记录中没有的数字，不要输出 JSON，只输出摘要正文。"
)


def _heavy(char: str) -> bool:
    """东亚字符（含全角标点）按 1 token 计。"""
    return ord(char) > 0x2E7F


def estimate_text_tokens(text: str) -> int:
    heavy = sum(1 for ch in text if _heavy(ch))
    light = len(text) - heavy
    return heavy + -(-light // 3)  # 其余字符按 1/3 token 向上取整


def estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        total += 4 + estimate_text_tokens(str(message.get("content") or ""))
    return total


def _preview(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + "……"


def build_digest(messages: list[dict], max_chars: int = 30000) -> str:
    """把「行动-观察」轮次变成逐轮机械摘要。

    每轮行动与观察各截 600 字；总量按最新优先取够预算，输出按时间正序；
    最新一轮即使超预算也会截到预算内保留，保证摘要不至于为空。
    """
    lines: list[str] = []
    turn = 0
    for message in messages:
        role = message.get("role")
        content = _preview(message.get("content"), 600)
        if role == "assistant":
            turn += 1
            lines.append(f"第{turn}轮 行动：{content}")
        elif lines:
            lines[-1] += f"；观察：{content}"
        else:
            lines.append(f"观察：{content}")
    picked: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) + 1 > max_chars:
            if picked:
                break
            line = _preview(line, max(0, max_chars - used))
        picked.append(line)
        used += len(line) + 1
    picked.reverse()
    return "\n".join(picked)


def _split_summary(content: str) -> tuple[str, str]:
    """把 goal 拆成（正文, 既有摘要块）；没有摘要块时第二项为空。"""
    index = content.find(SUMMARY_MARKER)
    if index == -1:
        return content, ""
    return content[:index].rstrip(), content[index:]


async def compact_messages(messages: list[dict], keep_turns: int, summarize,
                           max_summary_chars: int = 1500) -> list[dict]:
    """压缩中间轮次；无需压缩（中间为空）时原样返回。

    summarize: async (transcript: str) -> str。抛出异常或返回空时自动降级为
    机械摘要，保证压缩本身永不打断任务。
    """
    keep = max(0, int(keep_turns))
    tail_start = len(messages) - keep * 2
    if tail_start <= 2:
        return messages
    head, middle, tail = messages[:2], messages[2:tail_start], messages[tail_start:]
    goal, previous = _split_summary(str(head[1].get("content") or ""))
    digest = build_digest(middle)
    if previous:
        digest = f"{previous}\n{digest}".strip()
    recap = ""
    try:
        recap = str(await summarize(digest) or "").strip()
    except Exception:
        logger.warning("context compact summarizer failed, using mechanical digest",
                       exc_info=True)
    if not recap:
        recap = _preview(digest, 3000)
    recap = _preview(recap, max_summary_chars)
    merged = {
        "role": "user",
        "content": f"{goal}\n\n{SUMMARY_MARKER}\n{_SUMMARY_TIP}\n{recap}",
    }
    compacted = [head[0], merged, *tail]
    logger.info(
        "react context compacted: ~%s -> ~%s tokens (kept last %s messages)",
        estimate_tokens(messages), estimate_tokens(compacted), len(tail),
    )
    return compacted
