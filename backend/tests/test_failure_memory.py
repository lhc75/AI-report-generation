# -*- coding: utf-8 -*-
"""跨尝试失败记忆：只采用同任务、同指令版本的 ReAct 失败摘要。"""
from app import generate
from app.models import ModuleContent


def _failed_row(db, task, digest="", revision=0, version=1, status="failed"):
    row = ModuleContent(chapter_task_id=task.id, version=version, status=status,
                        instruction_revision=revision, failure_digest=digest)
    db.add(row)
    db.commit()
    return row


def test_previous_failure_memory_injected(project, db):
    _project, task, _user = project
    _failed_row(db, task, "- 第3轮 run_python 执行失败：KeyError: 'q7'", revision=0)
    memory = generate._previous_failure_memory(db, task)
    assert "上一次生成失败经验" in memory
    assert "KeyError" in memory
    assert "不要原样重复" in memory
    extra = generate._extra("请更简洁", memory)
    assert memory in extra and "用户补充要求：请更简洁" in extra


def test_failure_memory_ignored_after_instruction_change(project, db):
    _project, task, _user = project
    task.instruction_revision = 2
    db.commit()
    _failed_row(db, task, "- 第2轮 finish 被拒：正文太短", revision=1)
    assert generate._previous_failure_memory(db, task) == ""


def test_failure_memory_empty_without_digest(project, db):
    _project, task, _user = project
    _failed_row(db, task, "", revision=0)
    assert generate._previous_failure_memory(db, task) == ""


def test_successful_row_is_not_used_as_memory(project, db):
    _project, task, _user = project
    row = _failed_row(db, task, "- 旧失败", revision=0)
    row.status = "ok"
    db.commit()
    assert generate._previous_failure_memory(db, task) == ""
