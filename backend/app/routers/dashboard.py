"""数据统计接口：读取内置问卷数据或当前用户项目数据文件。"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import dashboard
from ..db import get_db
from ..deps import get_current_user
from ..models import Project, ProjectFile, User

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

BUILTIN_PROJECT_ID = "builtin-student"
BUILTIN_PROJECT_NAME = "内置学生作答数据（12.14）"
BUILTIN_FILE_PREFIX = "builtin:"


class DashboardFilter(BaseModel):
    column: str = Field(min_length=1, max_length=255)
    kind: Literal["text", "range", "date"] = "text"
    value: str | None = Field(default=None, max_length=500)
    minimum: float | None = None
    maximum: float | None = None
    start: date | None = None
    end: date | None = None


class SchoolTableIn(BaseModel):
    project_id: int | str
    file_id: int | str
    filters: list[DashboardFilter] = Field(default_factory=list, max_length=20)


class RecordsIn(BaseModel):
    project_id: int | str
    file_id: int | str
    mode: Literal["meta", "full"] = "meta"
    page: int = Field(default=1, ge=1, le=100000)
    page_size: int = Field(default=50, ge=1, le=200)
    filters: list[DashboardFilter] = Field(default_factory=list, max_length=20)


@dataclass
class ResolvedDataFile:
    id: str
    file_name: str
    source_ref: str
    size: int
    created_at: str


def _source_text(value: int | str) -> str:
    return str(value).strip()


def _resolve_builtin_file(file_id: str) -> ResolvedDataFile:
    if not file_id.startswith(BUILTIN_FILE_PREFIX):
        raise HTTPException(status_code=404, detail="内置数据文件不存在")
    name = file_id[len(BUILTIN_FILE_PREFIX):]
    if not name or Path(name).name != name or "\\" in name or "/" in name:
        raise HTTPException(status_code=404, detail="内置数据文件不存在")
    path = (dashboard.BUILTIN_DATA_DIR / name).resolve()
    if not path.is_file() or not path.is_relative_to(dashboard.BUILTIN_DATA_DIR.resolve()):
        raise HTTPException(status_code=404, detail="内置数据文件不存在")
    return ResolvedDataFile(
        id=file_id,
        file_name=name,
        source_ref=f"{BUILTIN_FILE_PREFIX}{name}",
        size=path.stat().st_size,
        created_at=datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    )


def _data_file(project_id: int | str, file_id: int | str, user: User, db: Session) -> ResolvedDataFile:
    project_key = _source_text(project_id)
    file_key = _source_text(file_id)
    if project_key == BUILTIN_PROJECT_ID:
        return _resolve_builtin_file(file_key)

    if not project_key.isdigit() or not file_key.isdigit():
        raise HTTPException(status_code=404, detail="项目不存在")
    project = db.get(Project, int(project_key))
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="项目不存在")
    file = db.get(ProjectFile, int(file_key))
    if file is None or file.project_id != project.id or file.file_type != "data":
        raise HTTPException(status_code=404, detail="数据文件不存在")
    return ResolvedDataFile(
        id=str(file.id),
        file_name=file.file_name,
        source_ref=file.minio_path,
        size=file.size,
        created_at=file.created_at.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _handle_data_error(exc: dashboard.DashboardDataError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/sources")
def list_sources(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    projects = db.scalars(
        select(Project).where(Project.user_id == user.id).order_by(Project.id.desc())
    ).all()
    project_ids = [project.id for project in projects]
    files = []
    if project_ids:
        files = db.scalars(
            select(ProjectFile)
            .where(ProjectFile.project_id.in_(project_ids), ProjectFile.file_type == "data")
            .order_by(ProjectFile.id)
        ).all()
    grouped: dict[int, list[ProjectFile]] = defaultdict(list)
    for file in files:
        grouped[file.project_id].append(file)

    payload_projects = [
        {
            "id": project.id,
            "name": project.name,
            "data_files": [
                {
                    "id": file.id,
                    "file_name": file.file_name,
                    "size": file.size,
                    "created_at": file.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                }
                for file in grouped.get(project.id, [])
            ],
        }
        for project in projects
    ]
    builtin_files = dashboard.list_builtin_files()
    if builtin_files:
        payload_projects.insert(0, {
            "id": BUILTIN_PROJECT_ID,
            "name": BUILTIN_PROJECT_NAME,
            "data_files": builtin_files,
        })
    return {"projects": payload_projects}


@router.get("/overview")
async def get_overview(
    project_id: int | str = Query(...),
    file_id: int | str = Query(...),
    school_keyword: str = Query(default="", max_length=200),
    school_id: str = Query(default="", max_length=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    file = _data_file(project_id, file_id, user, db)
    try:
        frame = await asyncio.to_thread(
            dashboard.load_meta_frame, file.id, file.file_name, file.source_ref
        )
        payload = dashboard.build_overview(frame, school_keyword, school_id)
    except dashboard.DashboardDataError as exc:
        raise _handle_data_error(exc) from exc
    return {
        "file": {
            "id": file.id,
            "file_name": file.file_name,
            "size": file.size,
            "created_at": file.created_at,
        },
        **payload,
    }


@router.post("/school-table")
async def get_school_table(data: SchoolTableIn,
                           user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    file = _data_file(data.project_id, data.file_id, user, db)
    try:
        frame = await asyncio.to_thread(
            dashboard.load_meta_frame, file.id, file.file_name, file.source_ref
        )
        payload = dashboard.build_school_table(frame, data.filters)
    except dashboard.DashboardDataError as exc:
        raise _handle_data_error(exc) from exc
    return payload


@router.post("/records")
async def get_records(data: RecordsIn,
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    file = _data_file(data.project_id, data.file_id, user, db)
    try:
        if data.mode == "full":
            frame = await asyncio.to_thread(
                dashboard.load_full_frame, file.id, file.file_name, file.source_ref
            )
        else:
            frame = await asyncio.to_thread(
                dashboard.load_meta_frame, file.id, file.file_name, file.source_ref
            )
        payload = dashboard.build_records(frame, data.filters, data.page, data.page_size)
    except dashboard.DashboardDataError as exc:
        raise _handle_data_error(exc) from exc
    return payload
