"""数据库模型：用户、模型配置、项目、项目文件、章节写作任务、模块内容、生成记录。"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ModelConfig(Base):
    """AI 模型配置；agent_type 为空字符串表示全局默认配置。"""

    __tablename__ = "model_config"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    agent_type: Mapped[str] = mapped_column(String(32), default="")  # ''=全局 parse/analyze/write/chart/assemble/review
    base_url: Mapped[str] = mapped_column(String(512))
    api_key_enc: Mapped[str] = mapped_column(Text)  # Fernet 加密存储
    model: Mapped[str] = mapped_column(String(128))
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组
    params_json: Mapped[str] = mapped_column(Text, default="{}")  # {参数名: {"enabled": bool, "value": float}}
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Skill(Base):
    """用户私有的可复用模型技能说明。"""

    __tablename__ = "skill"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_skill_user_name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(512), default="")
    content: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(32), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class ModelConfigSkill(Base):
    """模型配置与 Skill 的多对多挂载关系。"""

    __tablename__ = "model_config_skill"

    model_config_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("model_config.id"), primary_key=True
    )
    skill_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("skill.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Project(Base):
    __tablename__ = "project"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="created")  # created/analyzing/analyzed/generating/assembling/failed
    error_msg: Mapped[str] = mapped_column(Text, default="")
    parsed_template_path: Mapped[str] = mapped_column(String(512), default="")
    evidence_path: Mapped[str] = mapped_column(String(512), default="")
    report_path: Mapped[str] = mapped_column(String(512), default="")  # 导出的 Word 报告
    operation_token: Mapped[str] = mapped_column(String(36), default="")
    material_revision: Mapped[int] = mapped_column(Integer, default=0)
    last_run_id: Mapped[str] = mapped_column(String(36), default="")
    last_run_result: Mapped[str] = mapped_column(Text, default="{}")
    progress_json: Mapped[str] = mapped_column(Text, default="{}")  # 运行中进度（仅展示用）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    files: Mapped[list["ProjectFile"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ProjectFile(Base):
    __tablename__ = "project_file"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("project.id"), index=True)
    file_type: Mapped[str] = mapped_column(String(16))  # template/reference/data
    file_name: Mapped[str] = mapped_column(String(255))
    minio_path: Mapped[str] = mapped_column(String(512))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    statistics_config: Mapped[str] = mapped_column(Text, default='{"fields":[]}')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    project: Mapped[Project] = relationship(back_populates="files")


class ChapterTask(Base):
    """模版解析产物：章节节点（heading）与写作任务（text/table/image）。"""

    __tablename__ = "chapter_task"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("project.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    level: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(512))
    sort: Mapped[int] = mapped_column(Integer, default=0)
    task_type: Mapped[str] = mapped_column(String(16), default="text")  # heading/text/table/image
    instruction: Mapped[str] = mapped_column(Text, default="")
    template_title: Mapped[str] = mapped_column(String(512), default="")
    template_instruction: Mapped[str] = mapped_column(Text, default="")
    instruction_revision: Mapped[int] = mapped_column(Integer, default=0)
    slot_key: Mapped[str] = mapped_column(String(512), default="")
    anchor_task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/queued/generating/done/failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ModuleContent(Base):
    """章节任务的生成内容；每次生成/重写产生一个新版本，is_current 标记当前版本。"""

    __tablename__ = "module_content"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chapter_task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chapter_task.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_type: Mapped[str] = mapped_column(String(16), default="text")  # text/table/image
    # 大表格 JSON 与逐格证据可能超过 64KB，MySQL 使用 MEDIUMTEXT；db.py 负责存量表升级。
    content: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT(), "mysql"), default="")  # 文字内容或表格 JSON
    # 旧版证据元数据列：已停用，保留以兼容历史数据。
    evidence_json: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT(), "mysql"), default="[]")
    # AI 复核记录：{status, issues, content_version, material_revision, instruction_revision, reviewed_at}
    review_json: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT(), "mysql"), default="{}")
    material_revision: Mapped[int] = mapped_column(Integer, default=0)
    instruction_revision: Mapped[int] = mapped_column(Integer, default=0)
    image_path: Mapped[str] = mapped_column(String(512), default="")  # 图片 MinIO 路径
    prompt: Mapped[str] = mapped_column(Text, default="")  # 本次生成使用的用户补充提示词
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok/failed
    error_msg: Mapped[str] = mapped_column(Text, default="")
    # 失败过程摘要（最近几轮的具体错误与最后动作），下一次生成时注入提示词；
    # 与 error_msg 分开存放，避免界面提示被长文本淹没。
    failure_digest: Mapped[str] = mapped_column(Text, default="")
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class GenerationRecord(Base):
    """生成记录：每次模型调用的追溯信息。"""

    __tablename__ = "generation_record"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    module_content_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("module_content.id"), index=True)
    agent_type: Mapped[str] = mapped_column(String(32), default="")  # write/table/chart
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    elapsed_seconds: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class StorageCleanup(Base):
    """对象引用删除与待清理记录同事务提交，失败后可重试。"""
    __tablename__ = "storage_cleanup"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(String(512), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
