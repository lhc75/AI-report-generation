"""Apply survey deletions while retaining inputs needed by historical batches."""

import logging
import shutil
from pathlib import Path

from sqlalchemy import delete, select, update

from .config import settings
from .db import SessionLocal
from .survey_models import (SurveyAnalysisPhrase, SurveyAsset, SurveyBatch, SurveyBatchPreparation,
                            SurveyDataset, SurveyIndicatorVersion, SurveyProject, SurveyReport,
                            SurveyReportDraft)

logger = logging.getLogger(__name__)
ACTIVE = ("queued", "running")


def project_dir(project_id: int, *, create: bool = True) -> Path:
    base = (Path(settings.SURVEY_DATA_DIR) if settings.SURVEY_DATA_DIR else
            Path(settings.STORAGE_LOCAL_DIR).resolve().parent / "survey-private").resolve()
    if base.is_relative_to(Path(settings.STORAGE_LOCAL_DIR).resolve()):
        raise ValueError("批量报告目录须位于公开下载目录之外")
    root = base / str(project_id)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def lock_project(db, project_id, *, user_id=None, deleted=False):
    # A write acquires the project lock on both SQLite and MySQL.
    condition = (SurveyProject.id == project_id, SurveyProject.deleted.is_(deleted))
    if user_id is not None:
        condition += (SurveyProject.user_id == user_id,)
    updated = db.execute(update(SurveyProject).where(*condition).values(dataset_id=SurveyProject.dataset_id))
    return db.get(SurveyProject, project_id, populate_existing=True) if updated.rowcount else None


def active_import(db, project_id):
    return db.scalar(select(SurveyDataset).where(SurveyDataset.project_id == project_id,
                     SurveyDataset.state.in_(ACTIVE), SurveyDataset.archived.is_(False)).limit(1))


def active_batch(db, project_id):
    for model in (SurveyBatchPreparation, SurveyReport):
        relation = model.id if model is SurveyBatchPreparation else model.batch_id
        row = db.execute(select(SurveyBatch.id, model.state).join(model, relation == SurveyBatch.id)
                         .where(SurveyBatch.project_id == project_id, model.state.in_(ACTIVE)).limit(1)).first()
        if row:
            return row
    return None


def remove_project_records(db, project):
    batch_ids = select(SurveyBatch.id).where(SurveyBatch.project_id == project.id)
    report_ids = select(SurveyReport.id).where(SurveyReport.batch_id.in_(batch_ids))
    for model, condition in (
        (SurveyAnalysisPhrase, SurveyAnalysisPhrase.batch_id.in_(batch_ids)),
        (SurveyReportDraft, SurveyReportDraft.id.in_(report_ids)),
        (SurveyReport, SurveyReport.batch_id.in_(batch_ids)),
        (SurveyBatchPreparation, SurveyBatchPreparation.id.in_(batch_ids)),
    ):
        db.execute(delete(model).where(condition))
    for model in (SurveyBatch, SurveyIndicatorVersion, SurveyDataset, SurveyAsset):
        db.execute(delete(model).where(model.project_id == project.id))
    project.dataset_id = None
    project.deleted = True


def purge_project(project_id):
    with SessionLocal.begin() as db:
        project = lock_project(db, project_id, deleted=True)
        if project is None:
            return
        try:
            shutil.rmtree(project_dir(project_id, create=False))
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("Could not clean survey project %s", project_id)
            return
        db.delete(project)


def purge_archived_inputs(project_id):
    with SessionLocal.begin() as db:
        if lock_project(db, project_id) is None:
            return
        batches = list(db.scalars(select(SurveyBatch).where(SurveyBatch.project_id == project_id)))
        batch_datasets = {batch.dataset_id for batch in batches}
        batch_ids = [batch.id for batch in batches]
        if batch_ids:
            references = set(db.scalars(select(SurveyReport.template_id).where(
                SurveyReport.batch_id.in_(batch_ids), SurveyReport.template_id.is_not(None)).distinct()))
        else:
            references = set()
        for batch in batches:
            references.update(batch.config.get("indicator_assets", []))
            references.update(item["asset_id"] for item in batch.config.get("questionnaire_originals", []))
        versions = db.scalars(select(SurveyDataset).where(SurveyDataset.project_id == project_id))
        root = project_dir(project_id, create=False)
        for version in versions:
            if version.id in batch_datasets or version.state in ACTIVE:
                references.update(source["asset_id"] for source in version.sources)
            elif version.archived:
                for suffix in (".sqlite", ".working"):
                    unlink_input(root / f"dataset-{version.id}{suffix}")
        assets = list(db.scalars(select(SurveyAsset).where(SurveyAsset.project_id == project_id)))
        live_paths = {asset.path for asset in assets if not asset.archived or asset.id in references}
        for asset in assets:
            if asset.archived and asset.id not in references:
                if not asset.path or asset.path in live_paths or unlink_input(root / asset.path):
                    asset.path = ""
                    asset.details = {}


def unlink_input(path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.exception("Could not remove survey input %s", path)
        return False
    return True


def recover_deleted_projects():
    with SessionLocal() as db:
        project_ids = list(db.scalars(select(SurveyProject.id).where(SurveyProject.deleted.is_(True))))
    for project_id in project_ids:
        purge_project(project_id)
