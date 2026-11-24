# -*- coding: utf-8 -*-
"""统一失败诊断与首次失败后最多 5 次自动重试。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import assemble, generate
from app.models import ModelConfig
from app.routers import projects


def _diagnosis(attempt: int, tag: str = "校验失败") -> str:
    return json.dumps({
        "attempt": attempt,
        "root_causes": [f"{tag}-根因-{attempt}"],
        "fix_strategies": [f"{tag}-修正-{attempt}"],
        "failed_approaches": [f"{tag}-旧做法-{attempt}"],
        "must_verify": [f"{tag}-核验-{attempt}"],
    }, ensure_ascii=False)


def _review_config(db, user):
    db.add(ModelConfig(user_id=user.id, name="审核", agent_type="review",
                       base_url="http://offline", api_key_enc="enc", model="offline",
                       is_default=True))
    db.commit()


def test_diagnosis_merges_previous_failure_and_new_rules(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    previous = _diagnosis(1, "旧问题")
    seen = {}

    async def fake_chat(_cfg, messages, **_kwargs):
        seen["payload"] = json.loads(messages[-1]["content"])
        return json.dumps({
            "root_causes": ["本轮缺少 chart.png"],
            "fix_strategies": ["先运行绘图代码并检查文件存在"],
            "failed_approaches": ["直接 finish"],
            "must_verify": ["out/chart.png 大小超过 5000 字节"],
        }, ensure_ascii=False)

    monkeypatch.setattr(generate, "chat_completion", fake_chat)
    summary = asyncio.run(generate.diagnose_failure(
        db, item, task,
        current_error="ReactRunError: 尚未生成 chart.png",
        failure_digest="- 第3轮 finish 被拒：尚未生成 chart.png",
        previous_summary=previous, attempt=2, retry_limit=5))

    obj = json.loads(summary)
    assert obj["attempt"] == 2
    assert "旧问题-根因-1" in obj["root_causes"]
    assert "本轮缺少 chart.png" in obj["root_causes"]
    assert "先运行绘图代码并检查文件存在" in obj["fix_strategies"]
    assert seen["payload"]["previous_diagnosis"]["attempt"] == 1
    rendered = generate.failure_summary_text(summary)
    assert "【根因】" in rendered and "【重试前必须核验】" in rendered


def test_diagnosis_falls_back_without_blocking_retry(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)

    async def broken_chat(*_args, **_kwargs):
        raise RuntimeError("diagnosis unavailable")

    monkeypatch.setattr(generate, "chat_completion", broken_chat)
    summary = asyncio.run(generate.diagnose_failure(
        db, item, task,
        current_error="KeyError: q7",
        failure_digest="- 第2轮 run_python 执行失败：KeyError: q7",
        attempt=1, retry_limit=5))
    obj = json.loads(summary)
    assert any("KeyError: q7" in item for item in obj["root_causes"])
    assert any("KeyError: q7" in item for item in obj["failed_approaches"])
    assert "生成任务最终失败" not in summary


def test_run_generate_one_retries_five_times_then_stops(project, db, monkeypatch):
    item, task, _user = project
    calls = []
    prompts = []
    diagnoses = []

    async def never_succeeds(project_id, task_id, user_prompt, run_token, failure_summary=""):
        calls.append((project_id, task_id, failure_summary))
        prompts.append(user_prompt)
        return False, SimpleNamespace(status="failed"), "内容校验失败", "- 第1轮 finish 被拒"

    async def fake_diagnose(_db, _project, _task, *, current_error, failure_digest,
                            review_context="", previous_summary="", attempt=1, retry_limit=5):
        diagnoses.append({
            "attempt": attempt,
            "previous_summary": previous_summary,
            "current_error": current_error,
            "failure_digest": failure_digest,
            "retry_limit": retry_limit,
        })
        return _diagnosis(attempt)

    monkeypatch.setattr(projects, "_run_generate_attempt", never_succeeds)
    monkeypatch.setattr(generate, "diagnose_failure", fake_diagnose)
    monkeypatch.setattr(projects.settings, "FAILURE_RETRY_LIMIT", 5)

    asyncio.run(projects.run_generate_one(
        item.id, task.id, "保留这个用户要求", finish=False))

    assert len(calls) == 6
    assert len(diagnoses) == 5
    assert [item["attempt"] for item in diagnoses] == [1, 2, 3, 4, 5]
    assert prompts == ["保留这个用户要求"] * 6
    assert calls[-1][2] == _diagnosis(5)
    effective = generate.build_retry_prompt(prompts[-1], calls[-1][2])
    assert effective.startswith("保留这个用户要求")
    assert "自动失败诊断" in effective
    assert "校验失败-根因-5" in effective


def test_run_generate_one_stops_after_success(project, db, monkeypatch):
    item, task, _user = project
    calls = {"attempt": 0, "diagnose": 0}

    async def succeed_on_third(project_id, task_id, user_prompt, run_token, failure_summary=""):
        calls["attempt"] += 1
        if calls["attempt"] < 3:
            return False, SimpleNamespace(status="failed"), "内容校验失败", "- 失败观察"
        return True, SimpleNamespace(status="ok"), "", ""

    async def fake_diagnose(*_args, **kwargs):
        calls["diagnose"] += 1
        return _diagnosis(kwargs["attempt"])

    monkeypatch.setattr(projects, "_run_generate_attempt", succeed_on_third)
    monkeypatch.setattr(generate, "diagnose_failure", fake_diagnose)

    result = asyncio.run(projects.run_generate_one(item.id, task.id, finish=False))
    assert result.status == "ok"
    assert calls == {"attempt": 3, "diagnose": 2}


def test_run_generate_one_does_not_retry_auth_or_configuration_errors(project, db, monkeypatch):
    item, task, _user = project
    calls = {"attempt": 0, "diagnose": 0}

    async def auth_failure(*_args, **_kwargs):
        calls["attempt"] += 1
        return False, SimpleNamespace(status="failed"), "HTTPStatusError: 401 Unauthorized", ""

    async def fake_diagnose(*_args, **_kwargs):
        calls["diagnose"] += 1
        return _diagnosis(1)

    monkeypatch.setattr(projects, "_run_generate_attempt", auth_failure)
    monkeypatch.setattr(generate, "diagnose_failure", fake_diagnose)

    asyncio.run(projects.run_generate_one(item.id, task.id, finish=False))
    assert calls == {"attempt": 1, "diagnose": 0}


def test_failed_content_exposes_saved_failure_summary(project, db):
    item, task, _user = project
    summary = _diagnosis(3, "图表未生成")
    row = generate._save_content(
        db, task, "image", "", "", "", "chart", "offline", 0,
        status="failed", error_msg="ReactRunError: missing chart.png",
        failure_digest="- 多轮 finish 被拒", failure_summary=summary)
    db.commit()
    payload = projects.content_to_dict(row, with_body=False)
    assert payload["failure_summary"] == generate.failure_summary_text(summary)
    assert "图表未生成-根因-3" in payload["failure_summary"]
    assert payload["status"] == "failed"


def test_assembly_failure_does_not_trigger_content_regeneration(project, db, monkeypatch):
    item, _task, _user = project
    calls = {"diagnose": 0}

    async def review_ok(*_args, **_kwargs):
        return {"ok": True, "issues": []}

    async def fake_diagnose(*_args, **_kwargs):
        calls["diagnose"] += 1
        return _diagnosis(1)

    def fail_manifest(*_args, **_kwargs):
        raise RuntimeError("模板装配失败")

    monkeypatch.setattr(assemble.review, "review_and_repair", review_ok)
    monkeypatch.setattr(assemble, "prepare_manifest", fail_manifest)
    monkeypatch.setattr(generate, "diagnose_failure", fake_diagnose)

    with pytest.raises(RuntimeError, match="模板装配失败"):
        asyncio.run(assemble.assemble_report(db, item))
    assert calls["diagnose"] == 0


def test_retryable_failure_classification():
    assert generate.is_retryable_failure("ReactRunError: 尚未生成 chart.png")
    assert not generate.is_retryable_failure("HTTP 401 Client Error")
    assert not generate.is_retryable_failure("HTTPStatusError: 403 Forbidden")
    assert not generate.is_retryable_failure("该模块没有数据文件")
    assert not generate.is_retryable_failure("未配置审核模型")
