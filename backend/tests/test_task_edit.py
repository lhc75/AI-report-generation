"""章节编辑回归：临时数据库与存储，不调用模型或业务服务。"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app import generate, operations, storage
from app.db import get_db
from app.deps import get_current_user
from app.models import ChapterTask, ModuleContent, Project, StorageCleanup
from app.routers import projects


def current_content(db, task, body="已核实的旧正文", status="ok"):
    return generate._save_content(
        db, task, "text", body, "", "", "write", "offline", 0,
        status=status, error_msg="模型失败记录" if status == "failed" else "",
    )


def edit(project, db, title="修改后的模块", instruction="【填写：修改后的调查结果】"):
    item, task, user = project
    return projects.edit_task(item.id, task.id,
                              projects.TaskEditIn(title=title, instruction=instruction), user, db)


def refresh(db, item, task):
    db.expire_all()
    return db.get(Project, item.id), db.get(ChapterTask, task.id)


def test_edit_keeps_history_marks_current_stale_and_preserves_template_location(project, db):
    item, task, user = project
    parent = ChapterTask(project_id=item.id, title="原章节", task_type="heading", sort=1)
    anchor = ChapterTask(project_id=item.id, title="锚点模块", instruction="【填写：锚点】", sort=2)
    db.add_all([parent, anchor])
    db.flush()
    original_title, original_instruction = task.title, task.instruction
    task.parent_id, task.anchor_task_id, task.sort, task.level = parent.id, anchor.id, 17, 3
    task.slot_key = "slot_unchanged"
    successful = current_content(db, task)
    failed = current_content(db, task, status="failed")
    task.status = "done"
    item.report_path, item.evidence_path = "edit/old-report.docx", "edit/evidence.json"
    storage.upload_bytes(item.report_path, b"old report")
    storage.upload_bytes(item.evidence_path, b"evidence")
    db.commit()
    success_id, failed_id, project_id, task_id = successful.id, failed.id, item.id, task.id

    result = edit(project, db, "  修改后的标题  ", "  【填写：修改后的统计要求】  ")
    item, task = refresh(db, item, task)
    assert result == {"ok": True, "task_id": task_id, "changed": True,
                      "task_type": "text", "status": "pending"}
    assert task.title == "修改后的标题" and task.instruction == "【填写：修改后的统计要求】"
    assert task.instruction_revision == 1 and task.status == "pending"
    assert task.template_title == original_title and task.template_instruction == original_instruction
    assert (task.parent_id, task.anchor_task_id, task.sort, task.level, task.slot_key) == (
        parent.id, anchor.id, 17, 3, "slot_unchanged")
    assert item.material_revision == 0 and item.evidence_path == "edit/evidence.json"
    assert storage.download_bytes(item.evidence_path) == b"evidence"
    assert item.report_path == "" and not item.operation_token and item.status == "analyzed"
    assert db.get(ModuleContent, success_id).is_current
    assert db.get(ModuleContent, success_id).content == "已核实的旧正文"
    assert db.get(ModuleContent, failed_id).status == "failed"
    assert db.scalar(select(ModuleContent).where(ModuleContent.chapter_task_id == task_id)
                     .order_by(ModuleContent.version.desc())).id == failed_id

    response = projects.list_contents(project_id, task_id, None, user, db)
    assert len(response["versions"]) == 2
    assert response["content"]["id"] == success_id and response["content"]["is_stale"]
    assert all(not version["can_rollback"] for version in response["versions"])
    with pytest.raises(HTTPException) as error:
        projects.rollback_content(project_id, task_id, projects.RollbackIn(content_id=success_id), user, db)
    assert error.value.status_code == 400
    with pytest.raises(HTTPException) as error:
        asyncio.run(projects.assemble_project(project_id, BackgroundTasks(), user, db))
    assert error.value.status_code == 400


def test_second_edit_and_restoring_original_text_do_not_overwrite_template_metadata(project, db):
    item, task, _user = project
    original_title, original_instruction = task.title, task.instruction
    edit(project, db)
    item, task = refresh(db, item, task)
    edit((item, task, project[2]), db, "再次修改", "【生成表格：各组人数】")
    item, task = refresh(db, item, task)
    edit((item, task, project[2]), db, original_title, original_instruction)
    item, task = refresh(db, item, task)
    assert task.instruction_revision == 3
    assert (task.template_title, task.template_instruction) == (original_title, original_instruction)
    assert task.task_type == "text" and task.status == "pending"


def test_noop_keeps_revision_status_report_and_metadata(project, db, monkeypatch):
    item, task, _user = project
    successful = current_content(db, task)
    task.status = "done"
    item.report_path, item.evidence_path = "valid-report", "valid-evidence"
    db.commit()
    monkeypatch.setattr(operations, "invalidate_report", lambda *_args: pytest.fail("无变化不能失效报告"))
    result = edit(project, db, f"  {task.title}  ", f"\n{task.instruction}\n")
    item, task = refresh(db, item, task)
    assert not result["changed"]
    assert task.instruction_revision == 0 and task.status == "done"
    assert task.template_title == task.template_instruction == ""
    assert item.report_path == "valid-report" and item.evidence_path == "valid-evidence"
    assert operations.content_is_valid(db.get(ModuleContent, successful.id), task, item)


def test_heading_rename_keeps_descendant_content_valid(project, db):
    item, task, user = project
    heading = ChapterTask(project_id=item.id, title="原章节", task_type="heading", instruction="", sort=1)
    db.add(heading)
    db.flush()
    task.parent_id = heading.id
    successful = current_content(db, task)
    task.status = "done"
    item.report_path, item.evidence_path = "old-report", "same-evidence"
    db.commit()
    result = projects.edit_task(item.id, heading.id, projects.TaskEditIn(title="新章节"), user, db)
    item, task = refresh(db, item, task)
    heading = db.get(ChapterTask, heading.id)
    assert result["changed"] and result["task_type"] == "heading"
    assert heading.title == "新章节" and heading.template_title == "原章节"
    assert heading.instruction_revision == 0 and heading.instruction == ""
    assert task.status == "done" and task.parent_id == heading.id
    assert operations.content_is_valid(db.get(ModuleContent, successful.id), task, item)
    assert item.report_path == "" and item.evidence_path == "same-evidence"
    # 标题编辑不应该让有效子模块重新生成。
    background = BackgroundTasks()
    result = asyncio.run(projects.generate_all(item.id, background, user, db))
    assert result["total"] == 0 and not background.tasks


def test_heading_rejects_generation_instruction(project, db):
    item, task, _user = project
    task.task_type, task.instruction = "heading", ""
    db.commit()
    with pytest.raises(HTTPException) as error:
        edit(project, db, "章节", "【填写：正文】")
    assert error.value.status_code == 400
    item, task = refresh(db, item, task)
    assert task.task_type == "heading" and task.instruction_revision == 0


@pytest.mark.parametrize(("instruction", "expected"), [
    ("【生成表格：各组人数】", "table"),
    ("【生成图片：调查结果饼图】", "image"),
    ("【填写：解释调查结果】", "text"),
])
def test_instruction_change_updates_type(project, db, instruction, expected):
    item, task, _user = project
    task.task_type, task.slot_key = "image", "original-slot"
    db.commit()
    edit(project, db, task.title, instruction)
    item, task = refresh(db, item, task)
    assert task.task_type == expected and task.instruction_revision == 1
    assert task.slot_key == "original-slot" and task.status == "pending"


@pytest.mark.parametrize("instruction", [None, "", "  \n  ", "【说明：来源】", "【本模板：规则】",
                                        "【数据来源：文件】", "【备注：要求】", "【注意：要求】"])
def test_nonheading_rejects_empty_or_note_instruction(project, db, instruction):
    item, task, _user = project
    original = (task.title, task.instruction)
    with pytest.raises(HTTPException) as error:
        edit(project, db, "新标题", instruction)
    assert error.value.status_code == 400
    item, task = refresh(db, item, task)
    assert (task.title, task.instruction) == original and task.instruction_revision == 0


@pytest.mark.parametrize("busy", ["token", "analyzing", "generating", "assembling", "queued", "task_generating"])
def test_busy_project_rejects_edit(project, db, busy):
    item, task, _user = project
    if busy == "token":
        item.operation_token = "active-run"
    elif busy in {"queued", "task_generating"}:
        task.status = "queued" if busy == "queued" else "generating"
    else:
        item.status = busy
    db.commit()
    with pytest.raises(HTTPException) as error:
        edit(project, db)
    assert error.value.status_code == 409
    item, task = refresh(db, item, task)
    assert task.instruction_revision == 0


def test_edit_requires_analyzed_project(project, db):
    item, _task, _user = project
    item.status = "created"
    db.commit()
    with pytest.raises(HTTPException) as error:
        edit(project, db)
    assert error.value.status_code == 400


@pytest.mark.parametrize("target", ["missing_project", "other_user", "missing_task", "other_project_task"])
def test_edit_rejects_unowned_or_missing_target(project, db, target):
    item, task, user = project
    other = Project(user_id=user.id, name="另一个项目", status="analyzed")
    db.add(other)
    db.flush()
    other_task = ChapterTask(project_id=other.id, title="另一个任务", instruction="【填写：结果】")
    db.add(other_task)
    db.commit()
    project_id, task_id, caller = item.id, task.id, user
    if target == "missing_project":
        project_id = other.id + 999
    elif target == "other_user":
        caller = SimpleNamespace(id=user.id + 999)
    elif target == "missing_task":
        task_id = other_task.id + 999
    else:
        task_id = other_task.id
    with pytest.raises(HTTPException) as error:
        projects.edit_task(project_id, task_id, projects.TaskEditIn(title="编辑", instruction="【填写：新要求】"),
                           caller, db)
    assert error.value.status_code == 404


def test_transaction_failure_preserves_content_report_and_location(project, db, monkeypatch):
    item, task, _user = project
    successful = current_content(db, task)
    task.status = "done"
    original = (task.title, task.instruction)
    item.report_path, item.evidence_path = "retained-report", "retained-evidence"
    db.commit()
    project_id, task_id, content_id = item.id, task.id, successful.id
    deleted = []
    monkeypatch.setattr(storage, "remove", lambda path: deleted.append(path))

    def fail_commit(session):
        if any(isinstance(row, ChapterTask) and row.id == task_id and row.title == "触发失败"
               for row in session.dirty):
            raise RuntimeError("模拟编辑事务失败")

    event.listen(Session, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="模拟编辑事务失败"):
            edit(project, db, "触发失败")
    finally:
        event.remove(Session, "before_commit", fail_commit)
    item, task = refresh(db, item, task)
    assert (task.title, task.instruction) == original and task.instruction_revision == 0
    assert task.template_title == task.template_instruction == ""
    assert task.status == "done" and item.report_path == "retained-report"
    assert item.evidence_path == "retained-evidence" and item.id == project_id
    assert db.get(ModuleContent, content_id).is_current
    assert db.scalar(select(StorageCleanup.id)) is None and deleted == []


@pytest.mark.parametrize("payload", [
    {"instruction": "【填写：正文】"},
    {"title": "  \n  ", "instruction": "【填写：正文】"},
    {"title": "字" * 513, "instruction": "【填写：正文】"},
    {"title": "标题", "instruction": "字" * 20001},
])
def test_patch_endpoint_validates_input(project, db, payload):
    item, task, user = project
    app = FastAPI()
    app.include_router(projects.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        response = client.patch(f"/api/projects/{item.id}/tasks/{task.id}", json=payload)
    assert response.status_code == 422
    item, task = refresh(db, item, task)
    assert task.instruction_revision == 0


def test_patch_endpoint_saves_without_starting_generation(project, db, monkeypatch):
    item, task, user = project
    app = FastAPI()
    app.include_router(projects.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(generate, "generate_task", lambda *_args, **_kwargs: pytest.fail("编辑不应生成"))
    with TestClient(app) as client:
        response = client.patch(f"/api/projects/{item.id}/tasks/{task.id}",
                                json={"title": "新的标题", "instruction": "【生成表格：人数】"})
    assert response.status_code == 200 and response.json()["task_type"] == "table"
    item, task = refresh(db, item, task)
    assert task.status == "pending" and item.operation_token == "" and item.last_run_id == ""

