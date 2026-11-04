"""项目级短事务与运行身份；I/O 和模型请求不持有数据库行锁。"""
import json
import logging
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, or_

from . import storage
from .db import SessionLocal
from .models import ChapterTask, ModuleContent, Project, ProjectFile, StorageCleanup

logger = logging.getLogger(__name__)
_locks: dict[int, threading.RLock] = {}
_locks_guard = threading.Lock()
BUSY = {"analyzing", "generating", "assembling"}


@contextmanager
def project_transaction(project_id: int, user_id: int | None = None):
    # 本机单 worker 的线程互斥也让 SQLite 离线验收具有相同操作顺序。
    with _locks_guard:
        lock = _locks.setdefault(project_id, threading.RLock())
    with lock, SessionLocal() as db, db.begin():
        project = db.scalar(select(Project).where(Project.id == project_id)
                            .with_for_update().execution_options(populate_existing=True))
        if project is None or (user_id is not None and project.user_id != user_id):
            raise HTTPException(404, "项目不存在")
        yield db, project


def require_idle(db, project: Project) -> None:
    active = db.scalar(select(ChapterTask.id).where(
        ChapterTask.project_id == project.id, ChapterTask.status.in_(["queued", "generating"])
    ).with_for_update().limit(1))
    if project.operation_token or project.status in BUSY or active:
        raise HTTPException(409, "项目正在处理，请等待完成后再操作")


def begin_run(db, project: Project, status: str | None = None) -> str:
    require_idle(db, project)
    token = str(uuid4())
    project.operation_token = token
    project.progress_json = "{}"
    if status:
        project.status = status
    project.error_msg = ""
    return token


def ensure_run(db, project_id: int, token: str | None, lock: bool = True) -> Project:
    statement = select(Project).where(Project.id == project_id)
    if lock:
        statement = statement.with_for_update()
    project = db.scalar(statement.execution_options(populate_existing=True))
    if project is None or (token is not None and project.operation_token != token):
        raise RuntimeError("操作已失效，拒绝提交过期运行结果")
    return project


def finish_run(project_id: int, token: str, status: str, result: dict) -> None:
    with project_transaction(project_id) as (_db, project):
        if project.operation_token != token:
            return
        project.operation_token = ""
        project.progress_json = "{}"
        project.status = status
        project.last_run_id = token
        project.last_run_result = json.dumps(result, ensure_ascii=False)
        project.error_msg = result.get("message", "") if result.get("failed") else ""


# 阶段权重：导出总进度中各阶段占用的百分比区间（仅用于进度条展示）。
_PROGRESS_PHASES = {
    "review": (0, 60),
    "repair": (60, 75),
    "final": (75, 80),
    "render": (80, 95),
    "qa": (95, 100),
}


def report_progress(project_id: int, token: str | None, *, phase: str, label: str,
                    done: int = 0, total: int = 0) -> None:
    """运行进度上报（仅展示）：令牌校验 + 静默失败，不影响导出主流程。"""
    if token is None:
        return
    base, span = _PROGRESS_PHASES.get(phase, (0, 100))
    ratio = min(1.0, max(0.0, done / total)) if total else 0.0
    payload = {
        "phase": phase,
        "label": label,
        "done": int(done),
        "total": int(total),
        "percent": int(base + span * ratio),
        "updated_at": int(time.time()),
    }
    try:
        with SessionLocal() as db:
            project = db.get(Project, project_id)
            if project is None or project.operation_token != token:
                return
            project.progress_json = json.dumps(payload, ensure_ascii=False)
            db.commit()
    except Exception:
        logger.warning("进度上报失败（不影响导出）：%s", phase, exc_info=True)


def queue_cleanup(db, path: str) -> None:
    if not path:
        return
    if any(isinstance(item, StorageCleanup) and item.path == path for item in db.new):
        return
    if not db.scalar(select(StorageCleanup.id).where(StorageCleanup.path == path)):
        db.add(StorageCleanup(path=path))


def invalidate_report(db, project: Project) -> None:
    queue_cleanup(db, project.report_path)
    project.report_path = ""


def invalidate_inputs(db, project: Project) -> None:
    invalidate_report(db, project)
    queue_cleanup(db, project.evidence_path)
    queue_cleanup(db, project.parsed_template_path)
    project.evidence_path = ""
    project.parsed_template_path = ""
    project.material_revision += 1
    project.status = "created"
    project.error_msg = ""
    db.query(ChapterTask).filter(ChapterTask.project_id == project.id,
                                ChapterTask.task_type != "heading").update(
        {"status": "pending"}, synchronize_session=False)


def content_is_valid(content, task, project) -> bool:
    return bool(content and content.status == "ok"
                and content.material_revision == project.material_revision
                and content.instruction_revision == task.instruction_revision)


def drain_cleanup() -> None:
    with SessionLocal() as db:
        rows = db.scalars(select(StorageCleanup).order_by(StorageCleanup.id).limit(100)).all()
        for row in rows:
            try:
                storage.remove(row.path)
                db.delete(row)
            except Exception:
                logger.warning("对象清理失败，下次重试：%s", row.path, exc_info=True)
        db.commit()


def discard_unpublished(paths: list[str]) -> None:
    """提交异常后只清理已确认无引用的对象；连接不明时保留，避免误删。"""
    try:
        with SessionLocal() as db:
            for path in filter(None, paths):
                used = (db.scalar(select(ProjectFile.id).where(ProjectFile.minio_path == path))
                        or db.scalar(select(ModuleContent.id).where(ModuleContent.image_path == path))
                        or db.scalar(select(Project.id).where(or_(
                            Project.report_path == path, Project.evidence_path == path,
                            Project.parsed_template_path == path))))
                if not used:
                    queue_cleanup(db, path)
            db.commit()
        drain_cleanup()
    except Exception:
        logger.warning("无法确认对象提交状态，保留文件等待清理", exc_info=True)
