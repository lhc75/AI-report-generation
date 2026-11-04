"""分析环节 JSON 自修复：解析失败把错误回传给模型让它重出（离线，不访问网络）。"""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from docx import Document
from sqlalchemy import select

from app import storage
from app.models import ChapterTask, ModelConfig, Project, ProjectFile
from app.routers import projects

# 与真实现场同类：字符串内部夹带未转义的英文双引号
BROKEN_TASKS = (
    '[\n'
    '  {\n'
    '    "heading": "第一部分 调查概况",\n'
    '    "instructions": [\n'
    '      {\n'
    '        "type": "note",\n'
    '        "instruction": "原说明中的地区"甲、乙"已删除，替换为实际覆盖的选项。"\n'
    '      }\n'
    '    ]\n'
    '  }\n'
    ']'
)

GOOD_TASKS = json.dumps(
    [{
        "heading": "第一部分 调查概况",
        "instructions": [{"type": "text", "instruction": "【填写：概述调查对象与回收情况】"}],
    }],
    ensure_ascii=False,
)


def _fake_chat(script):
    """按序返回脚本内容（末项重复），并记录每次调用的 messages。"""
    calls = []

    async def _call(cfg, messages, timeout=600.0, retries=None):
        calls.append([dict(message) for message in messages])
        index = min(len(calls) - 1, len(script) - 1)
        return script[index]

    return _call, calls


@pytest.fixture
def debug_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "_analysis_debug_dir", lambda: tmp_path)
    return tmp_path


def _complete(monkeypatch, script):
    call, calls = _fake_chat(script)
    monkeypatch.setattr(projects, "chat_completion", call)
    return asyncio.run(projects._complete_analysis_json(
        SimpleNamespace(), [{"role": "user", "content": "prompt"}],
        label="模版写作指令输出", project_id=7,
    )), calls


def test_valid_first_reply_needs_no_repair(monkeypatch, debug_dir):
    result, calls = _complete(monkeypatch, [GOOD_TASKS])
    assert result == json.loads(GOOD_TASKS)
    assert len(calls) == 1
    assert (debug_dir / "parse_output_p7.txt").read_text(encoding="utf-8") == GOOD_TASKS
    assert not list(debug_dir.glob("*_repair*.txt"))


def test_repair_feeds_raw_output_and_error_back(monkeypatch, debug_dir):
    result, calls = _complete(monkeypatch, [BROKEN_TASKS, GOOD_TASKS])
    assert result == json.loads(GOOD_TASKS)
    assert len(calls) == 2
    repair_messages = calls[1]
    assert repair_messages[-2]["role"] == "assistant"
    assert repair_messages[-2]["content"] == BROKEN_TASKS
    fix_request = repair_messages[-1]
    assert fix_request["role"] == "user"
    assert "Expecting ',' delimiter" in fix_request["content"]
    assert '\\"' in fix_request["content"]
    assert (debug_dir / "parse_output_p7.txt").read_text(encoding="utf-8") == BROKEN_TASKS
    assert (debug_dir / "parse_output_p7_repair1.txt").read_text(encoding="utf-8") == GOOD_TASKS


def test_exhausted_repairs_raise_with_clear_message(monkeypatch, debug_dir):
    monkeypatch.setattr(projects.settings, "ANALYSIS_JSON_REPAIR_ATTEMPTS", 2)
    call, calls = _fake_chat([BROKEN_TASKS])
    monkeypatch.setattr(projects, "chat_completion", call)
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(projects._complete_analysis_json(
            SimpleNamespace(), [{"role": "user", "content": "prompt"}],
            label="模版写作指令输出", project_id=7,
        ))
    message = str(excinfo.value)
    assert "模版写作指令输出不是合法 JSON" in message
    assert "已自动请求模型修复 2 次仍未成功" in message
    assert "Expecting ',' delimiter" in message
    assert len(calls) == 3
    assert (debug_dir / "parse_output_p7_repair1.txt").exists()
    assert (debug_dir / "parse_output_p7_repair2.txt").exists()


def test_json_shape_mismatch_triggers_repair(monkeypatch, debug_dir):
    result, calls = _complete(monkeypatch, ['{"heading": "第一部分 调查概况"}', GOOD_TASKS])
    assert result == json.loads(GOOD_TASKS)
    assert len(calls) == 2
    assert "输出必须是 JSON 数组" in calls[1][-1]["content"]


def test_repairs_can_be_disabled(monkeypatch, debug_dir):
    monkeypatch.setattr(projects.settings, "ANALYSIS_JSON_REPAIR_ATTEMPTS", 0)
    call, calls = _fake_chat([BROKEN_TASKS])
    monkeypatch.setattr(projects, "chat_completion", call)
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(projects._complete_analysis_json(
            SimpleNamespace(), [{"role": "user", "content": "prompt"}],
            label="模版写作指令输出", project_id=7,
        ))
    assert "已自动请求模型修复 0 次" in str(excinfo.value)
    assert len(calls) == 1


def _template_bytes() -> bytes:
    document = Document()
    document.add_heading("第一部分 调查概况", level=1)
    document.add_paragraph("（模版正文占位）")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _seed_template_and_model(db, item, user):
    data = _template_bytes()
    path = f"{user.id}/{item.id}/upload/template.docx"
    storage.upload_bytes(path, data)
    db.add(ProjectFile(project_id=item.id, file_type="template", file_name="模版.docx",
                       minio_path=path, size=len(data)))
    db.add(ModelConfig(user_id=user.id, name="解析", agent_type="parse",
                       base_url="http://offline.invalid", api_key_enc="enc",
                       model="offline", is_default=True))
    db.commit()


def test_run_analysis_recovers_when_second_pass_repaired(project, db, monkeypatch, debug_dir):
    item, _task, user = project
    _seed_template_and_model(db, item, user)
    call, calls = _fake_chat(["[]", BROKEN_TASKS, GOOD_TASKS])
    monkeypatch.setattr(projects, "chat_completion", call)

    asyncio.run(projects.run_analysis(item.id, user.id))

    db.expire_all()
    refreshed = db.get(Project, item.id)
    assert refreshed.status == "analyzed", refreshed.error_msg
    work_tasks = db.scalars(
        select(ChapterTask).where(ChapterTask.project_id == item.id,
                                  ChapterTask.task_type != "heading")
    ).all()
    assert any("概述调查对象" in (row.instruction or "") for row in work_tasks)
    assert len(calls) == 3
    assert calls[2][-2]["role"] == "assistant"
    assert "Expecting ',' delimiter" in calls[2][-1]["content"]
    assert (debug_dir / f"parse_output_p{item.id}_repair1.txt").read_text(
        encoding="utf-8") == GOOD_TASKS


def test_run_analysis_reports_repair_exhaustion(project, db, monkeypatch, debug_dir):
    item, _task, user = project
    _seed_template_and_model(db, item, user)
    monkeypatch.setattr(projects.settings, "ANALYSIS_JSON_REPAIR_ATTEMPTS", 2)
    call, calls = _fake_chat(["[]", BROKEN_TASKS])
    monkeypatch.setattr(projects, "chat_completion", call)

    asyncio.run(projects.run_analysis(item.id, user.id))

    db.expire_all()
    refreshed = db.get(Project, item.id)
    assert refreshed.status == "failed"
    assert "已自动请求模型修复 2 次仍未成功" in (refreshed.error_msg or "")
    assert "Expecting ',' delimiter" in (refreshed.error_msg or "")
    assert len(calls) == 4  # 大纲 1 次 + 写作指令 1 + 2 次修复
    assert (debug_dir / f"parse_output_p{item.id}_repair2.txt").exists()
