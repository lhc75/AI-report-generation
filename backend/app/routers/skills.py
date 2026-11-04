"""用户私有 Skills 的管理与导入接口。"""
from collections import Counter

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import ModelConfigSkill, Skill, User
from ..skills import normalize_skill_item, parse_import_file

router = APIRouter(prefix="/api/skills", tags=["skill"])


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    content: str = Field(min_length=1, max_length=50000)


class ImportSkillIn(SkillIn):
    source_type: str = Field(default="imported_json", max_length=32)


class ImportCommitIn(BaseModel):
    skills: list[ImportSkillIn] = Field(min_length=1)
    conflict_strategy: str = "skip"


def skill_to_dict(skill: Skill, usage_count: int = 0) -> dict:
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "content": skill.content,
        "source_type": skill.source_type,
        "usage_count": usage_count,
        "created_at": skill.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": skill.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_own_skill(skill_id: int, user: User, db: Session) -> Skill:
    skill = db.get(Skill, skill_id)
    if skill is None or skill.user_id != user.id:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return skill


@router.get("")
def list_skills(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    usage = (
        select(ModelConfigSkill.skill_id, func.count().label("usage_count"))
        .group_by(ModelConfigSkill.skill_id)
        .subquery()
    )
    rows = db.execute(
        select(Skill, func.coalesce(usage.c.usage_count, 0))
        .outerjoin(usage, usage.c.skill_id == Skill.id)
        .where(Skill.user_id == user.id)
        .order_by(Skill.name, Skill.id)
    ).all()
    return [skill_to_dict(skill, int(count)) for skill, count in rows]


@router.get("/{skill_id:int}")
def get_skill(skill_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    skill = get_own_skill(skill_id, user, db)
    count = db.scalar(
        select(func.count()).select_from(ModelConfigSkill)
        .where(ModelConfigSkill.skill_id == skill.id)
    )
    return skill_to_dict(skill, int(count or 0))


@router.post("")
def create_skill(data: SkillIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = normalize_skill_item(data.model_dump(), "manual")
    skill = Skill(user_id=user.id, **item)
    db.add(skill)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="同名 Skill 已存在") from exc
    db.refresh(skill)
    return skill_to_dict(skill)


@router.put("/{skill_id:int}")
def update_skill(
    skill_id: int, data: SkillIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    skill = get_own_skill(skill_id, user, db)
    item = normalize_skill_item(data.model_dump(), skill.source_type or "manual")
    skill.name = item["name"]
    skill.description = item["description"]
    skill.content = item["content"]
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="同名 Skill 已存在") from exc
    db.refresh(skill)
    count = db.scalar(
        select(func.count()).select_from(ModelConfigSkill)
        .where(ModelConfigSkill.skill_id == skill.id)
    )
    return skill_to_dict(skill, int(count or 0))


@router.delete("/{skill_id:int}")
def delete_skill(skill_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    skill = get_own_skill(skill_id, user, db)
    affected = db.scalar(
        select(func.count()).select_from(ModelConfigSkill)
        .where(ModelConfigSkill.skill_id == skill.id)
    ) or 0
    db.execute(delete(ModelConfigSkill).where(ModelConfigSkill.skill_id == skill.id))
    db.delete(skill)
    db.commit()
    return {"ok": True, "affected_config_count": int(affected)}


@router.post("/import/preview")
async def preview_import(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raw = await file.read()
    if len(raw) > 1024 * 1024:
        raise HTTPException(status_code=400, detail="导入文件不能超过 1 MB")
    try:
        items = parse_import_file(file.filename or "", raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    names = [item["name"] for item in items]
    duplicate_names = {name for name, count in Counter(names).items() if count > 1}
    existing = set(db.scalars(
        select(Skill.name).where(Skill.user_id == user.id, Skill.name.in_(names))
    ).all())
    preview = []
    for index, item in enumerate(items):
        status = "create"
        error = ""
        if item["name"] in duplicate_names:
            status = "error"
            error = "导入文件内存在同名 Skill"
        elif item["name"] in existing:
            status = "conflict"
        preview.append({"index": index, "status": status, "error": error, **item})
    return {"items": preview}


@router.post("/import")
def import_skills(
    data: ImportCommitIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    if data.conflict_strategy not in {"skip", "replace"}:
        raise HTTPException(status_code=400, detail="conflict_strategy 仅支持 skip 或 replace")
    normalized = [normalize_skill_item(item.model_dump(), item.source_type) for item in data.skills]
    duplicate_names = {name for name, count in Counter(item["name"] for item in normalized).items() if count > 1}
    if duplicate_names:
        raise HTTPException(status_code=400, detail="导入内容存在同名 Skill")
    existing = {
        skill.name: skill
        for skill in db.scalars(
            select(Skill).where(Skill.user_id == user.id, Skill.name.in_([i["name"] for i in normalized]))
        ).all()
    }
    created = updated = skipped = 0
    for item in normalized:
        skill = existing.get(item["name"])
        if skill is None:
            db.add(Skill(user_id=user.id, **item))
            created += 1
            continue
        if data.conflict_strategy == "skip":
            skipped += 1
            continue
        skill.description = item["description"]
        skill.content = item["content"]
        skill.source_type = item["source_type"]
        updated += 1
    db.commit()
    return {"created": created, "updated": updated, "skipped": skipped}
