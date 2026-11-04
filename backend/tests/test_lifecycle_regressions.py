"""生命周期回归：隔离数据库、本地模拟存储，不请求真实模型。"""
import asyncio
import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from docx import Document
from fastapi import BackgroundTasks, HTTPException, UploadFile
from pydantic import ValidationError
from sqlalchemy import select, event
from sqlalchemy.orm import Session

from app import generate, operations, storage
from app.config import Settings
from app.db import SessionLocal
from app.models import ChapterTask, ModelConfig, ModuleContent, Project, ProjectFile
from app.routers import projects
from app.routers.auth import AuthIn


def fresh(project_id):
    with SessionLocal() as session:
        item = session.get(Project, project_id)
        session.expunge(item)
        return item


def save(db, task, body="已核实的正文", status="ok"):
    return generate._save_content(db, task, "text", body, "", "", "write", "offline", 0,
                                  status=status, error_msg="模拟模型失败" if status == "failed" else "")


def test_failed_rewrite_keeps_current_report_and_history(project, db, monkeypatch):
    item, task, user = project
    old = save(db, task)
    task.status = "done"
    item.report_path = "old-valid.docx"
    db.commit()
    old_id = old.id
    # 故障包含模型配置缺失：也必须记录历史。
    result = asyncio.run(projects.rewrite_one(item.id, task.id, projects.RewriteIn(prompt="重写"),
                                              BackgroundTasks(), user, db))
    asyncio.run(projects.run_generate_one(item.id, task.id, "重写", result["run_id"]))
    db.expire_all()
    contents = db.scalars(select(ModuleContent).order_by(ModuleContent.version)).all()
    assert len(contents) == 2
    assert contents[0].id == old_id and contents[0].is_current
    assert contents[1].status == "failed" and not contents[1].is_current
    assert db.get(Project, item.id).report_path == "old-valid.docx"
    chapters = projects.list_chapters(item.id, user, db)
    assert chapters[0]["history_count"] == 2
    assert chapters[0]["latest_error"]
    assert fresh(item.id).last_run_id == result["run_id"]
    assert json.loads(fresh(item.id).last_run_result)["failed"] == 1


def test_rollback_restores_done_and_invalidates_report(project, db):
    item, task, user = project
    old = save(db, task)
    old_id = old.id
    db.commit()
    failed = save(db, task, status="failed")
    task.status = "failed"
    item.report_path = "old-report.docx"
    db.commit()
    projects.rollback_content(item.id, task.id, projects.RollbackIn(content_id=old_id), user, db)
    db.expire_all()
    assert db.get(ChapterTask, task.id).status == "done"
    assert db.get(ModuleContent, old_id).is_current
    assert not db.get(ModuleContent, failed.id).is_current
    assert fresh(item.id).report_path == ""
    response = asyncio.run(projects.assemble_project(item.id, BackgroundTasks(), user, db))
    assert response["run_id"]


def test_material_change_stales_current_and_rejects_rollback(project, db):
    item, task, user = project
    old = save(db, task)
    old_id = old.id
    db.commit()
    with operations.project_transaction(item.id) as (tx, locked):
        locked.report_path, locked.evidence_path = "report", "evidence"
        operations.invalidate_inputs(tx, locked)
    db.expire_all()
    assert db.get(ModuleContent, old_id).content
    assert projects.list_chapters(item.id, user, db)[0]["is_stale"]
    with pytest.raises(HTTPException) as error:
        projects.rollback_content(item.id, task.id, projects.RollbackIn(content_id=old_id), user, db)
    assert error.value.status_code == 400
    assert fresh(item.id).report_path == fresh(item.id).evidence_path == ""


def test_success_only_invalidates_report_not_evidence(project, db):
    item, task, _user = project
    item.evidence_path = "evidence"
    item.report_path = "report"
    save(db, task)
    db.commit()
    assert fresh(item.id).evidence_path == "evidence"
    assert fresh(item.id).report_path == ""


def test_duplicate_batch_and_mutations_rejected(project, db):
    item, task, user = project
    project_id, task_id = item.id, task.id
    caller = SimpleNamespace(id=user.id)
    def start():
        try:
            with SessionLocal() as request_db:
                return asyncio.run(projects.generate_all(project_id, BackgroundTasks(), caller, request_db))
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda _unused: start(), range(2)))
    assert sum(isinstance(answer, dict) for answer in answers) == 1
    assert 409 in answers
    with pytest.raises(HTTPException) as error:
        projects.delete_task(project_id, task_id, user, db)
    assert error.value.status_code == 409


def test_stale_token_cannot_save_or_finish(project, db):
    item, task, user = project
    result = asyncio.run(projects.generate_all(item.id, BackgroundTasks(), user, db))
    with operations.project_transaction(item.id) as (_tx, locked):
        locked.operation_token = "newer-token"
    operations.finish_run(item.id, result["run_id"], "analyzed", {"succeeded": 1})
    assert fresh(item.id).operation_token == "newer-token"
    with pytest.raises(RuntimeError, match="失效"):
        operations.ensure_run(db, item.id, result["run_id"])
    asyncio.run(projects.run_generate_one(item.id, task.id, run_token=result["run_id"]))
    db.expire_all()
    assert db.scalar(select(ModuleContent.id)) is None
    assert db.get(ChapterTask, task.id).status == "queued"


def test_empty_batch_has_no_background_job(project, db):
    item, task, user = project
    task.status = "done"
    db.commit()
    background = BackgroundTasks()
    result = asyncio.run(projects.generate_all(item.id, background, user, db))
    assert result["total"] == 0 and not result["run_id"]
    assert not background.tasks and not fresh(item.id).operation_token


def docx_bytes():
    stream = io.BytesIO()
    Document().save(stream)
    return stream.getvalue()


@pytest.mark.parametrize("failure", ["upload", "commit"])
def test_template_replacement_failure_preserves_old(project, db, monkeypatch, failure):
    item, _task, user = project
    old_path = f"{user.id}/{item.id}/old.docx"
    storage.upload_bytes(old_path, docx_bytes())
    old = ProjectFile(project_id=item.id, file_type="template", file_name="old.docx", minio_path=old_path)
    db.add(old)
    db.commit()
    if failure == "upload":
        monkeypatch.setattr(storage, "upload_bytes", lambda *args: (_ for _ in ()).throw(OSError("模拟上传失败")))
    else:
        def fail_commit(session):
            if any(isinstance(row, ProjectFile) and row.file_name == "new.docx"
                   for row in session.identity_map.values()):
                raise RuntimeError("模拟数据库提交失败")
        event.listen(Session, "before_commit", fail_commit)
    try:
        with pytest.raises((OSError, RuntimeError)):
            asyncio.run(projects.upload_file(item.id, UploadFile(filename="new.docx", file=io.BytesIO(docx_bytes())),
                                             "template", user, db))
    finally:
        if failure == "commit":
            event.remove(Session, "before_commit", fail_commit)
    db.expire_all()
    rows = db.scalars(select(ProjectFile).where(ProjectFile.project_id == item.id)).all()
    assert len(rows) == 1 and rows[0].minio_path == old_path
    assert storage.download_bytes(old_path)
    assert not fresh(item.id).operation_token


def test_template_invalid_format_rejected(project, db):
    item, _task, user = project
    for name, raw in [("template.pdf", b"%PDF"), ("fake.docx", b"not a docx")]:
        with pytest.raises(HTTPException) as error:
            asyncio.run(projects.upload_file(item.id, UploadFile(filename=name, file=io.BytesIO(raw)),
                                             "template", user, db))
        assert error.value.status_code == 400
    assert not fresh(item.id).operation_token


def test_committed_template_is_kept_when_commit_response_fails(project, db):
    item, _task, user = project
    project_id = item.id
    def fail_after_commit(session):
        if any(isinstance(row, ProjectFile) and row.file_name == "new.docx"
               for row in session.identity_map.values()):
            raise RuntimeError("提交成功后连接响应丢失")
    event.listen(Session, "after_commit", fail_after_commit)
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(projects.upload_file(project_id, UploadFile(filename="new.docx", file=io.BytesIO(docx_bytes())),
                                             "template", user, db))
    finally:
        event.remove(Session, "after_commit", fail_after_commit)
    db.expire_all()
    saved = db.scalar(select(ProjectFile).where(ProjectFile.project_id == project_id))
    assert saved.file_name == "new.docx" and storage.download_bytes(saved.minio_path)
    assert not fresh(project_id).operation_token


def test_failed_file_delete_preserves_storage_and_references(project, db):
    item, _task, user = project
    path = f"{user.id}/{item.id}/preserved.csv"
    storage.upload_bytes(path, b"field\nvalue\n")
    file = ProjectFile(project_id=item.id, file_type="data", file_name="preserved.csv", minio_path=path)
    db.add(file)
    item.report_path = "preserved-report"
    db.commit()
    file_id, project_id = file.id, item.id
    def fail_commit(session):
        if any(isinstance(row, ProjectFile) and row.id == file_id for row in session.deleted):
            raise RuntimeError("模拟删除事务提交失败")
    event.listen(Session, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError):
            projects.delete_file(project_id, file_id, user, db)
    finally:
        event.remove(Session, "before_commit", fail_commit)
    db.expire_all()
    assert db.get(ProjectFile, file_id) and storage.download_bytes(path)
    assert fresh(project_id).report_path == "preserved-report"


def test_statistics_config_invalidates_inputs(project, db):
    item, _task, user = project
    path = f"{user.id}/{item.id}/data.csv"
    storage.upload_bytes(path, "选择,区域\nA/B,东部\nA,西部\n".encode())
    file = ProjectFile(project_id=item.id, file_type="data", file_name="data.csv", minio_path=path)
    db.add(file)
    item.report_path, item.evidence_path = "report", "evidence"
    db.commit()
    result = projects.get_statistics_config(item.id, file.id, user, db)
    sheet = result["sheets"][0]["name"]
    assert result["sheets"][0]["columns"][0]["index"] == 0
    projects.put_statistics_config(item.id, file.id, projects.StatisticsConfigIn(fields=[
        {"sheet": sheet, "column_index": 0, "question_type": "multiple", "separator": "/"}
    ]), user, db)
    changed = fresh(item.id)
    assert changed.material_revision == 1
    assert changed.report_path == changed.evidence_path == ""
    assert changed.status == "created"


def test_password_bytes_and_url_encoding():
    AuthIn(username="test", password="a" * 72)
    AuthIn(username="test", password="中" * 24)
    for password in ("a" * 73, "中" * 25):
        with pytest.raises(ValidationError, match="72"):
            AuthIn(username="test", password=password)
    settings = Settings(MYSQL_PASSWORD="@:/#密码", MYSQL_USER="u@ser")
    assert settings.mysql_url.password == "@:/#密码"
    assert settings.mysql_url.username == "u@ser"


def test_restart_records_failed_attempt_keeps_current_and_report(project, db):
    from app.main import _recover_stale_status
    item, task, _user = project
    old = save(db, task)
    old_id = old.id
    item.report_path = "valid-report"
    item.status, item.operation_token = "generating", "interrupted-token"
    task.status = "generating"
    db.commit()
    _recover_stale_status()
    db.expire_all()
    item, task = db.get(Project, item.id), db.get(ChapterTask, task.id)
    assert not item.operation_token and item.status == "analyzed"
    assert item.last_run_id == "interrupted-token"
    assert json.loads(item.last_run_result)["failed"] == 1
    assert task.status == "failed" and item.report_path == "valid-report"
    contents = db.scalars(select(ModuleContent).order_by(ModuleContent.version)).all()
    assert contents[0].id == old_id and contents[0].is_current
    assert contents[1].status == "failed" and "服务重启" in contents[1].error_msg
