"""所有 pytest 默认使用临时数据库和存储，不读取业务 .env。"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

_test_root = tempfile.TemporaryDirectory(prefix="ai-report-pytest-")
os.environ.update({
    "AI_REPORT_ENV_FILE": "", "DB_ENGINE": "sqlite",
    "SQLITE_PATH": str(Path(_test_root.name) / "tests.sqlite"),
    "STORAGE_BACKEND": "local", "STORAGE_LOCAL_DIR": str(Path(_test_root.name) / "objects"),
    "REVIEW_CONCURRENCY": "1",  # 测试默认串行复核，保证既有用例确定性
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Base, SessionLocal, engine
from app import models, storage


@pytest.fixture
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        yield session


@pytest.fixture
def project(db, monkeypatch):
    monkeypatch.setattr(storage, "presigned_url", lambda path: "https://offline.invalid/" + path)
    user = models.User(username="isolated-user", password_hash="offline")
    db.add(user)
    db.flush()
    item = models.Project(user_id=user.id, name="回归项目", status="analyzed")
    db.add(item)
    db.flush()
    task = models.ChapterTask(project_id=item.id, title="调查结果", instruction="【填写：调查结果】",
                              task_type="text", status="pending")
    db.add(task)
    db.commit()
    return item, task, user

@pytest.fixture(autouse=True)
def dispose_engine_after_test():
    yield
    engine.dispose()
