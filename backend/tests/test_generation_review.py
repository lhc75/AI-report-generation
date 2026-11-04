"""生成内 AI 复核：error 打回后修正通过、仅 warning 通过、审核失败任务失败。"""
import asyncio
import json
from types import SimpleNamespace

from app import generate


def _cfg():
    return SimpleNamespace(id=None, system_prompt="", model="offline")


def _prepare(project, db, monkeypatch, tmp_path):
    item, own, _user = project
    own.instruction = "写一段说明"
    db.commit()
    monkeypatch.setattr(generate, "pick_model", lambda *_args: _cfg())
    monkeypatch.setattr(generate, "build_materials", lambda *_args: ("参考资料", "数据摘要"))
    monkeypatch.setattr(generate, "_prepare_workdir", lambda *_args, **_kwargs: (tmp_path, []))
    return item, own


def test_generation_review_error_sends_back_and_task_recovers(project, db, monkeypatch, tmp_path):
    item, own = _prepare(project, db, monkeypatch, tmp_path)
    state = {"reviews": 0, "finishes": 0}

    async def fake_chat(_cfg, messages, **_kwargs):
        system = str(messages[0].get("content", ""))
        if "复核一个刚生成的报告模块" in system:
            state["reviews"] += 1
            if state["reviews"] == 1:
                return json.dumps({"status": "issues", "issues": [
                    {"severity": "error", "message": "数字与数据不符"}]}, ensure_ascii=False)
            return json.dumps({"status": "ok", "issues": []}, ensure_ascii=False)
        state["finishes"] += 1
        body = ("修改前正文" if state["finishes"] == 1 else "修改后正文") + "字" * 60
        return json.dumps({"action": "finish",
                           "action_input": json.dumps({"content": body}, ensure_ascii=False)},
                          ensure_ascii=False)

    monkeypatch.setattr(generate, "chat_completion", fake_chat)
    result = asyncio.run(generate.generate_task(db, item, own))
    assert result.status == "ok"
    assert state == {"reviews": 2, "finishes": 2}
    assert "修改后正文" in result.content
    record = json.loads(result.review_json)
    assert record["status"] == "ok" and record["content_sha256"]


def test_generation_review_warning_only_is_saved(project, db, monkeypatch, tmp_path):
    item, own = _prepare(project, db, monkeypatch, tmp_path)

    async def fake_chat(_cfg, messages, **_kwargs):
        system = str(messages[0].get("content", ""))
        if "复核一个刚生成的报告模块" in system:
            return json.dumps({"status": "issues", "issues": [
                {"severity": "warning", "message": "建议精简方法说明"}]}, ensure_ascii=False)
        return json.dumps({"action": "finish",
                           "action_input": json.dumps({"content": "正文" + "字" * 80}, ensure_ascii=False)},
                          ensure_ascii=False)

    monkeypatch.setattr(generate, "chat_completion", fake_chat)
    result = asyncio.run(generate.generate_task(db, item, own))
    assert result.status == "ok"
    record = json.loads(result.review_json)
    assert record["status"] == "warning"
    assert record["issues"][0]["message"] == "建议精简方法说明"


def test_generation_review_unavailable_fails_task(project, db, monkeypatch, tmp_path):
    item, own = _prepare(project, db, monkeypatch, tmp_path)

    async def fake_chat(_cfg, messages, **_kwargs):
        system = str(messages[0].get("content", ""))
        if "复核一个刚生成的报告模块" in system:
            raise RuntimeError("审核服务不可用")
        return json.dumps({"action": "finish",
                           "action_input": json.dumps({"content": "正文" + "字" * 80}, ensure_ascii=False)},
                          ensure_ascii=False)

    monkeypatch.setattr(generate, "chat_completion", fake_chat)
    result = asyncio.run(generate.generate_task(db, item, own))
    assert result.status == "failed"
    assert "审核模型调用失败" in result.error_msg
