"""模型配置 CRUD + 测试连接。api_key 加密存储，列表接口不回显明文。"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..llm import SAMPLING_PARAMS, test_connection
from ..models import ModelConfig, ModelConfigSkill, Skill, User
from ..security import encrypt_key
from ..skills import list_config_skills, skill_ref

router = APIRouter(prefix="/api/model-configs", tags=["model-config"])

AGENT_TYPES = ["", "parse", "analyze", "write", "chart", "assemble", "review"]


class ParamItem(BaseModel):
    enabled: bool = False
    value: float | None = None


class ConfigIn(BaseModel):
    name: str
    agent_type: str = ""
    base_url: str
    api_key: str | None = None  # 更新时为空表示不修改
    model: str
    system_prompt: str = ""
    skill_ids: list[int] = Field(default_factory=list)
    params: dict[str, ParamItem] = Field(default_factory=dict)
    is_default: bool = False


def to_dict(db: Session, cfg: ModelConfig) -> dict:
    skills = [skill_ref(skill) for skill in list_config_skills(db, cfg.id)]
    return {
        "id": cfg.id,
        "name": cfg.name,
        "agent_type": cfg.agent_type,
        "base_url": cfg.base_url,
        "api_key": "",  # 不回显
        "model": cfg.model,
        "system_prompt": cfg.system_prompt,
        "skills": skills,
        "skill_ids": [skill["id"] for skill in skills],
        "params": json.loads(cfg.params_json or "{}"),
        "is_default": cfg.is_default,
    }


def validate(data: ConfigIn, user: User, db: Session) -> None:
    if data.agent_type not in AGENT_TYPES:
        raise HTTPException(status_code=400, detail=f"agent_type 必须是 {AGENT_TYPES} 之一")
    for name in data.params:
        if name not in SAMPLING_PARAMS:
            raise HTTPException(status_code=400, detail=f"不支持的参数：{name}")
    if len(set(data.skill_ids)) != len(data.skill_ids):
        raise HTTPException(status_code=400, detail="Skill 不能重复挂载")
    if not data.skill_ids:
        return
    found = set(db.scalars(
        select(Skill.id).where(Skill.user_id == user.id, Skill.id.in_(data.skill_ids))
    ).all())
    if found != set(data.skill_ids):
        raise HTTPException(status_code=400, detail="存在无权访问或不存在的 Skill")


def dump_params(data: ConfigIn) -> str:
    return json.dumps({k: v.model_dump() for k, v in data.params.items()}, ensure_ascii=False)


def clear_default(db: Session, user_id: int, agent_type: str) -> None:
    db.execute(
        update(ModelConfig)
        .where(ModelConfig.user_id == user_id, ModelConfig.agent_type == agent_type)
        .values(is_default=False)
    )


def update_config_skills(db: Session, cfg: ModelConfig, skill_ids: list[int]) -> None:
    db.execute(
        delete(ModelConfigSkill).where(ModelConfigSkill.model_config_id == cfg.id)
    )
    if skill_ids:
        skills = {
            skill.id: skill
            for skill in db.scalars(select(Skill).where(Skill.id.in_(skill_ids))).all()
        }
        db.add_all([
            ModelConfigSkill(model_config_id=cfg.id, skill_id=skill_id, position=index)
            for index, skill_id in enumerate(skill_ids)
        ])
        # 旧字段保留为名称快照，兼容老版本读取；运行时不再依赖它。
        cfg.skills = json.dumps(
            [skills[skill_id].name for skill_id in skill_ids], ensure_ascii=False
        )
    else:
        cfg.skills = "[]"


@router.get("")
def list_configs(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(ModelConfig).where(ModelConfig.user_id == user.id).order_by(ModelConfig.id)
    ).all()
    return [to_dict(db, r) for r in rows]


@router.post("")
def create_config(data: ConfigIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    validate(data, user, db)
    if not data.api_key:
        raise HTTPException(status_code=400, detail="api_key 不能为空")
    if data.is_default:
        clear_default(db, user.id, data.agent_type)
    cfg = ModelConfig(
        user_id=user.id,
        name=data.name,
        agent_type=data.agent_type,
        base_url=data.base_url,
        api_key_enc=encrypt_key(data.api_key),
        model=data.model,
        system_prompt=data.system_prompt,
        params_json=dump_params(data),
        is_default=data.is_default,
    )
    db.add(cfg)
    db.flush()
    update_config_skills(db, cfg, data.skill_ids)
    db.commit()
    db.refresh(cfg)
    return to_dict(db, cfg)


@router.put("/{cfg_id}")
def update_config(cfg_id: int, data: ConfigIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    validate(data, user, db)
    cfg = db.get(ModelConfig, cfg_id)
    if cfg is None or cfg.user_id != user.id:
        raise HTTPException(status_code=404, detail="配置不存在")
    if data.is_default:
        clear_default(db, user.id, data.agent_type)
    cfg.name = data.name
    cfg.agent_type = data.agent_type
    cfg.base_url = data.base_url
    if data.api_key:
        cfg.api_key_enc = encrypt_key(data.api_key)
    cfg.model = data.model
    cfg.system_prompt = data.system_prompt
    cfg.params_json = dump_params(data)
    cfg.is_default = data.is_default
    update_config_skills(db, cfg, data.skill_ids)
    db.commit()
    return to_dict(db, cfg)


@router.delete("/{cfg_id}")
def delete_config(cfg_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cfg = db.get(ModelConfig, cfg_id)
    if cfg is None or cfg.user_id != user.id:
        raise HTTPException(status_code=404, detail="配置不存在")
    db.execute(
        delete(ModelConfigSkill).where(ModelConfigSkill.model_config_id == cfg.id)
    )
    db.delete(cfg)
    db.commit()
    return {"ok": True}


@router.post("/{cfg_id}/test")
async def test_config(cfg_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cfg = db.get(ModelConfig, cfg_id)
    if cfg is None or cfg.user_id != user.id:
        raise HTTPException(status_code=404, detail="配置不存在")
    try:
        reply, warning = await test_connection(cfg)
        return {"ok": True, "reply": reply[:100], "warning": warning}
    except Exception as exc:
        return {"ok": False, "error": (str(exc) or type(exc).__name__)[:300]}
