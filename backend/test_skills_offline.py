# -*- coding: utf-8 -*-
"""离线验证 Skills 的导入、隔离、挂载与运行时提示词组合。"""
import sys
import time
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import delete

sys.path.insert(0, str(Path(__file__).resolve().parent))

from offline_support import install_offline_environment

_offline_env = install_offline_environment()

from app.db import SessionLocal, init_db
from app.models import ModelConfig, ModelConfigSkill, Skill, User
from app.routers.model_configs import ConfigIn, validate
from app.routers.skills import ImportCommitIn, ImportSkillIn, delete_skill, import_skills
from app.skills import parse_skill_json, parse_skill_markdown, render_system_prompt


def check(condition, message):
    print(("PASS " if condition else "FAIL ") + message)
    if not condition:
        raise AssertionError(message)


def main():
    init_db()
    db = SessionLocal()
    suffix = str(int(time.time() * 1000000))
    owner = User(username=f"skill_owner_{suffix}", password_hash="offline")
    other = User(username=f"skill_other_{suffix}", password_hash="offline")
    db.add_all([owner, other])
    db.commit()
    db.refresh(owner)
    db.refresh(other)
    config = None
    try:
        markdown_items = parse_skill_markdown(
            b"---\nname: Markdown Skill\ndescription: Markdown import\n---\n# Markdown Skill\n\nUse source facts only."
        )
        check(markdown_items[0]["name"] == "Markdown Skill", "SKILL.md frontmatter 解析")
        json_items = parse_skill_json(
            b'{"skills":[{"name":"JSON Skill","description":"JSON import","content":"Check evidence."}]}'
        )
        check(json_items[0]["name"] == "JSON Skill", "JSON 数组封装解析")

        payload = ImportCommitIn(
            skills=[ImportSkillIn(name="Reusable Skill", description="first", content="First body.")],
        )
        result = import_skills(payload, owner, db)
        check(result == {"created": 1, "updated": 0, "skipped": 0}, "首次导入创建 Skill")

        duplicate = import_skills(payload, owner, db)
        check(duplicate["skipped"] == 1, "同名导入默认跳过")

        replacement = import_skills(
            ImportCommitIn(
                skills=[ImportSkillIn(name="Reusable Skill", description="updated", content="Updated body.")],
                conflict_strategy="replace",
            ),
            owner,
            db,
        )
        skill = db.query(Skill).filter(Skill.user_id == owner.id, Skill.name == "Reusable Skill").one()
        check(replacement["updated"] == 1 and skill.content == "Updated body.", "同名导入覆盖")

        config = ModelConfig(
            user_id=owner.id,
            name="Skill test config",
            agent_type="write",
            base_url="http://127.0.0.1:9/v1",
            api_key_enc="unused",
            model="test",
            system_prompt="Base instruction.",
            params_json="{}",
        )
        db.add(config)
        db.flush()
        db.add(ModelConfigSkill(model_config_id=config.id, skill_id=skill.id, position=0))
        db.commit()

        rendered = render_system_prompt(db, config)
        check(
            rendered.index("Base instruction.") < rendered.index("## 已挂载技能")
            and "Updated body." in rendered,
            "模型提示词按基础 Prompt 后接已挂载 Skills",
        )

        foreign_skill = Skill(
            user_id=owner.id,
            name="Owner only skill",
            description="",
            content="Owner content.",
            source_type="manual",
        )
        db.add(foreign_skill)
        db.commit()
        try:
            validate(
                ConfigIn(
                    name="No access",
                    base_url="http://127.0.0.1:9/v1",
                    model="test",
                    skill_ids=[foreign_skill.id],
                ),
                other,
                db,
            )
            raise AssertionError("other user should not attach owner Skill")
        except HTTPException as exc:
            check(exc.status_code == 400, "跨用户 Skill 挂载被拒绝")

        deleted = delete_skill(skill.id, owner, db)
        remaining = db.query(ModelConfigSkill).filter(ModelConfigSkill.skill_id == skill.id).count()
        check(deleted["affected_config_count"] == 1 and remaining == 0, "删除 Skill 自动解除挂载")
    finally:
        if config is not None and config.id is not None:
            db.execute(
                delete(ModelConfigSkill).where(ModelConfigSkill.model_config_id == config.id)
            )
            db.delete(config)
        db.flush()
        db.execute(delete(Skill).where(Skill.user_id.in_([owner.id, other.id])))
        db.execute(delete(User).where(User.id.in_([owner.id, other.id])))
        db.commit()
        db.close()
    print("\nALL SKILL CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        _offline_env.close()
