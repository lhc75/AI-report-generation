# -*- coding: utf-8 -*-
"""上下文压缩：token 估算、压缩结构、摘要替换与机械降级。"""
import asyncio

from app import context_compact


def make_messages(turns: int):
    messages = [{"role": "system", "content": "系统提示"}, {"role": "user", "content": "任务目标"}]
    for index in range(1, turns + 1):
        messages.append({"role": "assistant",
                         "content": f'{{"action":"run_python","action_input":"print({index})"}}'})
        messages.append({"role": "user", "content": f"【观察】第{index}轮结果"})
    return messages


def test_estimate_text_tokens_counts_east_asian_as_one():
    assert context_compact.estimate_text_tokens("中文") == 2
    assert context_compact.estimate_text_tokens("abcd") == 2  # 4/3 向上取整
    assert context_compact.estimate_text_tokens("中a") == 2  # 1 + ceil(1/3)
    messages = [{"role": "user", "content": "中文"}]
    assert context_compact.estimate_tokens(messages) == 4 + 2


def test_compact_keeps_head_and_recent_turns():
    messages = make_messages(6)
    calls = []

    async def summarize(digest):
        calls.append(digest)
        return "压缩后的摘要"

    compacted = asyncio.run(context_compact.compact_messages(messages, 4, summarize))
    assert compacted[0] == messages[0]
    assert context_compact.SUMMARY_MARKER in compacted[1]["content"]
    assert "压缩后的摘要" in compacted[1]["content"]
    assert compacted[2:] == messages[2 + 2 * (6 - 4):]  # 保留最近 4 组（8 条）
    assert len(compacted) == 2 + 8
    assert [m["role"] for m in compacted] == ["system", "user"] + ["assistant", "user"] * 4
    # 摘要器只收到被压缩的中间轮次
    assert calls and "第1轮" in calls[0] and "第5轮" not in calls[0]


def test_repeat_compaction_replaces_previous_summary():
    messages = make_messages(6)
    calls = []

    async def summarize(digest):
        calls.append(digest)
        return "第一版摘要" if len(calls) == 1 else "第二版摘要"

    first = asyncio.run(context_compact.compact_messages(messages, 4, summarize))
    extended = first + make_messages(3)[2:]
    second = asyncio.run(context_compact.compact_messages(extended, 4, summarize))
    content = second[1]["content"]
    assert content.count(context_compact.SUMMARY_MARKER) == 1
    assert "第二版摘要" in content
    assert "第一版摘要" not in content
    # 第二次摘要输入里带上了既有摘要，信息链不会断
    assert "第一版摘要" in calls[1]


def test_fallback_digest_when_summarizer_fails():
    messages = make_messages(6)

    async def summarize(digest):
        raise RuntimeError("summary model down")

    compacted = asyncio.run(context_compact.compact_messages(messages, 2, summarize))
    content = compacted[1]["content"]
    assert context_compact.SUMMARY_MARKER in content
    assert "第1轮 行动：" in content  # 机械摘要直出
    assert len(content) < 3500  # 摘要总量有界


def test_no_middle_returns_unchanged():
    async def summarize(digest):
        raise AssertionError("无需压缩时不应调用摘要器")

    messages = make_messages(2)
    result = asyncio.run(context_compact.compact_messages(messages, 4, summarize))
    assert result == messages
    assert context_compact.estimate_tokens(result) == context_compact.estimate_tokens(messages)


def test_build_digest_budget_keeps_newest_turns():
    messages = make_messages(6)
    digest = context_compact.build_digest(messages[2:], max_chars=200)
    assert "第6轮" in digest
    assert "第1轮" not in digest
    assert len(digest) <= 200
