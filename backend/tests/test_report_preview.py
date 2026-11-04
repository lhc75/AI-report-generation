"""报告在线预览接口的鉴权与字节一致性回归。"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.routers import projects

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _app(db, user=None):
    app = FastAPI()
    app.include_router(projects.router)
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: db
    return app


def test_preview_requires_auth(project, db):
    item, task, user = project
    with TestClient(_app(db)) as client:
        response = client.get(f"/api/projects/{item.id}/report/preview")
    assert response.status_code == 401


def test_preview_404_without_report(project, db):
    item, task, user = project
    with TestClient(_app(db, user)) as client:
        response = client.get(f"/api/projects/{item.id}/report/preview")
    assert response.status_code == 404
    assert "尚未导出报告" in response.json()["detail"]


def test_preview_returns_docx_bytes(project, db):
    item, task, user = project
    raw = b"fake-docx-bytes-for-preview"
    path = f"{user.id}/{item.id}/report/preview-regression.docx"
    storage.upload_bytes(path, raw, content_type=DOCX_MEDIA_TYPE)
    item.report_path = path
    db.commit()
    with TestClient(_app(db, user)) as client:
        response = client.get(f"/api/projects/{item.id}/report/preview")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(DOCX_MEDIA_TYPE)
    assert response.content == raw
