"""Persistent survey projects, immutable inputs, and resumable report jobs."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class SurveyProject(Base):
    __tablename__ = "survey_project"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    year: Mapped[int] = mapped_column(Integer)
    province: Mapped[str] = mapped_column(String(100))
    dataset_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    deleted: Mapped[bool] = mapped_column(default=False)


class SurveyAsset(Base):
    __tablename__ = "survey_asset"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("survey_project.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(120))
    digest: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON)
    archived: Mapped[bool] = mapped_column(default=False)


class SurveyDataset(Base):
    __tablename__ = "survey_dataset"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("survey_project.id"), index=True)
    state: Mapped[str] = mapped_column(String(16), index=True, default="queued")
    sources: Mapped[list] = mapped_column(JSON)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(default=False)


class SurveyTemplateParse(Base):
    __tablename__ = "survey_template_parse"

    id: Mapped[int] = mapped_column(ForeignKey("survey_asset.id"), primary_key=True, autoincrement=False)
    state: Mapped[str] = mapped_column(String(16), index=True, default="queued")
    error: Mapped[str] = mapped_column(Text, default="")


class SurveyIndicatorVersion(Base):
    __tablename__ = "survey_indicator_version"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("survey_project.id"), index=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("survey_dataset.id"))
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("survey_asset.id"), default=None)
    rules: Mapped[list] = mapped_column(JSON)


class SurveyBatch(Base):
    __tablename__ = "survey_batch"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("survey_project.id"), index=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("survey_dataset.id"))
    indicator_id: Mapped[int | None] = mapped_column(ForeignKey("survey_indicator_version.id"), default=None)
    request_id: Mapped[str] = mapped_column(String(36))
    config: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (UniqueConstraint("project_id", "request_id"),)


class SurveyReport(Base):
    __tablename__ = "survey_report"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("survey_batch.id"), index=True)
    org_id: Mapped[int] = mapped_column(Integer)
    org_level: Mapped[str] = mapped_column(String(16))
    org_path: Mapped[str] = mapped_column(String(900))
    school_stage: Mapped[str | None] = mapped_column(String(16), default=None)
    template_id: Mapped[int | None] = mapped_column(ForeignKey("survey_asset.id"))
    questionnaires: Mapped[list] = mapped_column(JSON)
    include_ai: Mapped[bool] = mapped_column(default=False)
    state: Mapped[str] = mapped_column(String(16), index=True, default="queued")
    error: Mapped[str] = mapped_column(Text, default="")
    notice: Mapped[str] = mapped_column(String(255), default="")


class SurveyBatchPreparation(Base):
    __tablename__ = "survey_batch_preparation"

    id: Mapped[int] = mapped_column(ForeignKey("survey_batch.id"), primary_key=True, autoincrement=False)
    state: Mapped[str] = mapped_column(String(16), index=True, default="queued")
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")


class SurveyReportDraft(Base):
    __tablename__ = "survey_report_draft"

    id: Mapped[int] = mapped_column(ForeignKey("survey_report.id"), primary_key=True, autoincrement=False)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)


class SurveyAnalysisPhrase(Base):
    __tablename__ = "survey_analysis_phrase"

    batch_id: Mapped[int] = mapped_column(ForeignKey("survey_batch.id"), primary_key=True)
    level: Mapped[str] = mapped_column(String(16), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("survey_report.id"), index=True)
