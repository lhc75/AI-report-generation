# -*- coding: utf-8 -*-
"""ReAct 防卡死闸门与上下文超限兜底（离线假模型，不访问网络）。"""
import asyncio
import json

import pytest

from app import generate, llm
from app.models import ModelConfig


def make_cfg():
    return ModelConfig(user_id=1, name="offline", agent_type="", base_url="http://offline",
                       api_key_enc="enc", model="offline", system_prompt="", is_default=True)


def action(name, payload=""):
    return json.dumps({"thought": "t", "action": name, "action_input": payload},
                      ensure_ascii=False)


def ok_validator(payload, workdir):
    return True, "", payload


def run_react(fake_chat, monkeypatch, goal="测试目标", **settings_overrides):
    monkeypatch.setattr(generate, "chat_completion", fake_chat)
    for name, value in settings_overrides.items():
        monkeypatch.setattr(generate.settings, name, value)
    return asyncio.run(generate.react_run(
        None, make_cfg(), goal, None, "text", finish_validator=ok_validator))


def test_whitespace_variant_repeats_trip_breaker(monkeypatch):
    codes = ["import json\nprint(1)", "import  json\n\nprint(1)  ", "import json\nprint(1)\n"]
    state = {"n": 0}

    async def fake_chat(cfg, messages, **kwargs):
        code = codes[min(state["n"], len(codes) - 1)]
        state["n"] += 1
        return action("run_python", code)

    with pytest.raises(ValueError, match="连续重复 run_python"):
        run_react(fake_chat, monkeypatch)


def test_stale_finish_nudge_then_finish(monkeypatch):
    seen = []
    state = {"n": 0}

    async def fake_chat(cfg, messages, **kwargs):
        seen.append(str(messages[-1].get("content", "")))
        state["n"] += 1
        if state["n"] <= 6:
            return action("run_python", f"print({state['n']})")
        return action("finish", "完成")

    result = run_react(fake_chat, monkeypatch, REACT_STALE_FINISH_NUDGE=6)
    assert result == "完成"
    assert state["n"] == 7  # 提醒后立即收敛，未烧完轮次
    assert any("【系统提醒】" in text for text in seen)


def test_context_overflow_compacts_and_retries(monkeypatch):
    calls = []
    state = {"overflowed": False}

    async def fake_chat(cfg, messages, **kwargs):
        last = str(messages[-1].get("content", ""))
        if "【上下文压缩】" in last:
            return "压缩后的前情摘要"
        calls.append(list(messages))
        if not state["overflowed"] and len(calls) >= 4:
            state["overflowed"] = True
            raise llm.ChatContextOverflow("模拟上下文超限")
        if len(calls) >= 5:
            return action("finish", "完成")
        return action("run_python", f"print({len(calls)})")

    # 窗口调大：确保走"服务端报超长 → 强制压缩重试"的兜底路径
    result = run_react(fake_chat, monkeypatch, REACT_CONTEXT_WINDOW_TOKENS=10 ** 9)
    assert result == "完成"
    assert state["overflowed"] is True
    assert any("前情摘要" in str(item[1]["content"]) for item in calls[4:])


def test_proactive_compaction_when_estimate_over_ratio(monkeypatch):
    seen = []
    state = {"n": 0}

    async def fake_chat(cfg, messages, **kwargs):
        last = str(messages[-1].get("content", ""))
        if "【上下文压缩】" in last:
            return "主动压缩摘要"
        seen.append(list(messages))
        state["n"] += 1
        if state["n"] >= 5:
            return action("finish", "完成")
        return action("run_python", f"print({state['n']})")

    long_goal = "目标" + "很长的任务说明。" * 500
    result = run_react(fake_chat, monkeypatch, goal=long_goal,
                       REACT_CONTEXT_WINDOW_TOKENS=4000, REACT_CONTEXT_KEEP_TURNS=2)
    assert result == "完成"
    assert any("前情摘要" in str(item[1]["content"]) for item in seen)


def test_turn_urgency_hint(monkeypatch):
    seen = []
    state = {"n": 0}

    async def fake_chat(cfg, messages, **kwargs):
        seen.append(str(messages[-1].get("content", "")))
        state["n"] += 1
        return action("run_python", f"print({state['n']})")

    with pytest.raises(ValueError, match="超过 4 轮"):
        run_react(fake_chat, monkeypatch, REACT_MAX_TURNS=4,
                  REACT_STALE_FINISH_NUDGE=99)
    assert any("仅剩" in text for text in seen)


def test_parse_action_recovers_dsml_variants():
    full_width = (
        '<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="run_python">\n'
        '<｜｜DSML｜｜ parameter name="action_input" string="true">'
        'import json\nprint(json.load(open("x.json")))\n'
        '</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>'
    )
    parsed = generate.parse_action(full_width)
    assert parsed is not None and parsed["action"] == "run_python"
    assert "import json" in parsed["action_input"]

    half_width = (
        '<||DSML|| invoke name="finish">'
        '<||DSML|| parameter name="action_input">完成</||DSML|| parameter>'
    )
    assert generate.parse_action(half_width) == {"action": "finish", "action_input": "完成"}


def test_no_turn_cap_runs_beyond_ten(monkeypatch):
    """默认不设轮次上限：超过旧版 10 轮仍继续，直到模型自己 finish。"""
    state = {"n": 0}

    async def fake_chat(cfg, messages, **kwargs):
        state["n"] += 1
        if state["n"] <= 15:
            return action("run_python", f"print({state['n']})")
        return action("finish", "完成")

    result = run_react(fake_chat, monkeypatch, REACT_MAX_TURNS=0,
                       REACT_STALE_FINISH_NUDGE=99)
    assert result == "完成"
    assert state["n"] == 16


def test_time_budget_stops_unbounded_run(monkeypatch):
    async def fake_chat(cfg, messages, **kwargs):
        await asyncio.sleep(0.01)
        return action("run_python", "print(1)")

    with pytest.raises(generate.ReactRunError, match="已运行"):
        run_react(fake_chat, monkeypatch, REACT_MAX_TURNS=0, REACT_MAX_SECONDS=0.005,
                  REACT_STALE_FINISH_NUDGE=99)


def test_failure_digest_is_carried_by_run_error(monkeypatch):
    state = {"n": 0}

    async def fake_chat(cfg, messages, **kwargs):
        state["n"] += 1
        return action("finish", f"坏成果{state['n']}")

    def reject(payload, workdir):
        return False, f"校验失败：{payload}", ""

    monkeypatch.setattr(generate, "chat_completion", fake_chat)
    monkeypatch.setattr(generate.settings, "REACT_MAX_TURNS", 3)
    with pytest.raises(generate.ReactRunError) as info:
        asyncio.run(generate.react_run(None, make_cfg(), "目标", None, "text",
                                       finish_validator=reject))
    assert "finish 被拒" in info.value.failure_digest
    assert "校验失败" in info.value.failure_digest


def test_repeated_failure_observation_trips_breaker(monkeypatch, tmp_path):
    state = {"n": 0}

    async def fake_chat(cfg, messages, **kwargs):
        state["n"] += 1
        return action("run_python", f"print({state['n']})")

    def failing_run(code, workdir):
        return {"ok": False, "stdout": "", "stderr": "KeyError: 'q7'"}

    monkeypatch.setattr(generate, "chat_completion", fake_chat)
    monkeypatch.setattr(generate.sandbox, "run_python", failing_run)
    with pytest.raises(generate.ReactRunError, match="相同失败观察"):
        asyncio.run(generate.react_run(None, make_cfg(), "目标", tmp_path, "text",
                                       finish_validator=ok_validator))
