"""Skill 查询、导入解析与运行时提示词组合。"""
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ModelConfig, ModelConfigSkill, Skill


def list_config_skills(db: Session, config_id: int) -> list[Skill]:
    return db.scalars(
        select(Skill)
        .join(ModelConfigSkill, ModelConfigSkill.skill_id == Skill.id)
        .where(ModelConfigSkill.model_config_id == config_id)
        .order_by(ModelConfigSkill.position, Skill.id)
    ).all()


def skill_ref(skill: Skill) -> dict:
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
    }


def render_system_prompt(db: Session | None, cfg: ModelConfig) -> str:
    """把模型配置本身与已挂载 Skills 合并为单条系统指令。"""
    parts = [cfg.system_prompt.strip()] if cfg.system_prompt else []
    skills = list_config_skills(db, cfg.id) if db is not None and cfg.id is not None else []
    if skills:
        skill_blocks = [
            f"### {skill.name}\n{skill.content.strip()}"
            for skill in skills
            if skill.content.strip()
        ]
        if skill_blocks:
            parts.append("## 已挂载技能\n" + "\n\n".join(skill_blocks))
    return "\n\n".join(parts)


def _clean_text(value: Any, field: str, max_length: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是文本")
    text = value.strip()
    if required and not text:
        raise ValueError(f"{field} 不能为空")
    if len(text) > max_length:
        raise ValueError(f"{field} 不能超过 {max_length} 个字符")
    return text


def normalize_skill_item(raw: Any, source_type: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Skill 必须是对象")
    name = _clean_text(raw.get("name"), "name", 128)
    description = raw.get("description", "")
    if description is None:
        description = ""
    description = _clean_text(description, "description", 512, required=False)
    content = _clean_text(raw.get("content"), "content", 50000)
    return {
        "name": name,
        "description": description,
        "content": content,
        "source_type": source_type,
    }


def _frontmatter_and_body(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() != "---":
            continue
        metadata: dict[str, str] = {}
        for line in lines[1:index]:
            match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$", line)
            if not match:
                continue
            metadata[match.group(1)] = match.group(2).strip().strip("'\"")
        return metadata, "\n".join(lines[index + 1:]).strip()
    return {}, text


def parse_skill_markdown(raw: bytes) -> list[dict]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("SKILL.md 必须使用 UTF-8 编码") from exc
    if not text.strip():
        raise ValueError("SKILL.md 为空")
    metadata, body = _frontmatter_and_body(text)
    heading = re.search(r"(?m)^#\s+(.+?)\s*$", body)
    name = metadata.get("name") or (heading.group(1).strip() if heading else "")
    remaining = body
    if heading:
        remaining = body[heading.end():].strip()
    first_paragraph = re.split(r"\n\s*\n", remaining, maxsplit=1)[0].strip()
    description = metadata.get("description") or first_paragraph
    return [normalize_skill_item(
        {"name": name, "description": description, "content": body},
        "imported_skill_md",
    )]


def parse_skill_json(raw: bytes) -> list[dict]:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("JSON 文件格式无效") from exc
    if isinstance(data, dict):
        items = data.get("skills", [data])
    else:
        items = data
    if not isinstance(items, list) or not items:
        raise ValueError("JSON 必须是 Skill 对象、Skill 数组或包含 skills 数组的对象")
    return [normalize_skill_item(item, "imported_json") for item in items]


def parse_import_file(filename: str, raw: bytes) -> list[dict]:
    lower_name = filename.lower()
    if lower_name.endswith(".json"):
        return parse_skill_json(raw)
    if lower_name == "skill.md" or lower_name.endswith(".skill.md") or lower_name.endswith(".md"):
        return parse_skill_markdown(raw)
    raise ValueError("仅支持 SKILL.md 或 JSON 文件")
