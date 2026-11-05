"""FastAPI 入口。"""
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import storage
from .config import ROOT_DIR, settings
from .db import init_db
from .routers import app_info, auth, dashboard, model_configs, projects, skills
from .storage import ensure_bucket

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="后台管理系统", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(app_info.router)
app.include_router(model_configs.router)
app.include_router(projects.router)
app.include_router(dashboard.router)
app.include_router(skills.router)


@app.on_event("startup")
def startup() -> None:
    init_db()
    ensure_bucket()
    _recover_stale_status()
    from .operations import drain_cleanup
    drain_cleanup()


def _recover_stale_status() -> None:
    """服务重启后，把中断的分析/生成状态复位，避免任务永远卡在中间态。"""
    import json
    from sqlalchemy import select
    from .db import SessionLocal
    from .models import ChapterTask, Project

    db = SessionLocal()
    try:
        interrupted = db.scalars(select(Project).where(Project.operation_token != "")).all()
        for project in interrupted:
            tasks = db.scalars(select(ChapterTask).where(ChapterTask.project_id == project.id,
                ChapterTask.status.in_(["generating", "queued"]))).all()
            project.last_run_id = project.operation_token
            project.last_run_result = json.dumps({"succeeded": 0, "failed": max(1, len(tasks)),
                "total": max(1, len(tasks)), "message": "服务重启导致操作中断，请重试"}, ensure_ascii=False)
            if project.status == "generating":
                from .generate import _save_content
                for task in tasks:
                    _save_content(db, task, task.task_type, "", "", "", "", "", 0,
                                  status="failed", error_msg="服务重启导致生成中断，请重试")
            project.error_msg = "服务重启导致操作中断，请重试"
            project.progress_json = "{}"
        db.query(ChapterTask).filter(ChapterTask.status.in_(["generating", "queued"])).update(
            {"status": "failed"}, synchronize_session=False
        )
        db.query(Project).filter(Project.status == "analyzing").update(
            {"status": "failed", "error_msg": "服务重启导致分析中断，请重新点击分析",
             "progress_json": "{}"},
            synchronize_session=False,
        )
        db.query(Project).filter(Project.status == "generating").update(
            {"status": "analyzed", "progress_json": "{}"}, synchronize_session=False
        )
        db.query(Project).filter(Project.status == "assembling").update(
            {"status": "analyzed", "error_msg": "服务重启导致组装中断，请重新导出",
             "progress_json": "{}"},
            synchronize_session=False,
        )
        db.query(Project).filter(Project.operation_token != "").update(
            {"operation_token": ""}, synchronize_session=False)
        db.flush()
        db.commit()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/storage/{file_path:path}", include_in_schema=False)
def serve_stored_file(file_path: str):
    """本地存储模式下供浏览器/桌面端下载生成的报告与素材。"""
    if settings.STORAGE_BACKEND != "local":
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        target = storage.local_file_path(file_path)
    except ValueError:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target, filename=target.name)


def _mount_frontend() -> None:
    """桌面客户端由后端直接托管前端构建产物（同源访问 /api）。"""
    dist = Path(os.environ.get("AI_REPORT_FRONTEND_DIST") or (ROOT_DIR / "frontend" / "dist"))
    if not (dist / "index.html").is_file():
        return
    root = dist.resolve()
    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    index_file = root / "index.html"
    # 入口 HTML 必须每次回源：客户端升级后旧缓存会继续引用已删除的旧 chunk，
    # 表现为“装了新版本但界面还是旧的”。
    no_store = {"Cache-Control": "no-store, must-revalidate"}

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        candidate = (root / full_path).resolve()
        if candidate.is_file() and candidate.is_relative_to(root):
            if candidate == index_file:
                return FileResponse(candidate, headers=no_store)
            return FileResponse(candidate)
        return FileResponse(index_file, headers=no_store)


_mount_frontend()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.BACKEND_PORT, reload=True)

