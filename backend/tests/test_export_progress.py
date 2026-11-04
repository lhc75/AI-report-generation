"""导出进度字段：上报、令牌保护与运行边界清理。"""
import json

from app import operations
from app.models import Project
from app.routers.projects import project_to_dict


def test_report_progress_writes_with_token_and_clears_on_run_boundaries(project, db):
    item, _task, _user = project
    token = operations.begin_run(db, item, "assembling")
    db.commit()

    operations.report_progress(item.id, token, phase="review", label="模块复核", done=1, total=2)
    db.expire_all()
    payload = json.loads(db.get(Project, item.id).progress_json)
    assert payload["phase"] == "review" and payload["label"] == "模块复核"
    assert payload["done"] == 1 and payload["total"] == 2
    assert payload["percent"] == 30  # review 阶段 0-60%：1/2 → 30%
    assert project_to_dict(db.get(Project, item.id))["progress"]["percent"] == 30

    # 令牌不符或为空时不写入。
    operations.report_progress(item.id, "bogus-token", phase="review",
                               label="模块复核", done=2, total=2)
    operations.report_progress(item.id, None, phase="review", label="模块复核", done=2, total=2)
    db.expire_all()
    assert json.loads(db.get(Project, item.id).progress_json)["done"] == 1

    # 固定阶段（无计数）按阶段基点取值。
    operations.report_progress(item.id, token, phase="qa", label="渲染验收")
    db.expire_all()
    assert json.loads(db.get(Project, item.id).progress_json)["percent"] == 95

    operations.finish_run(item.id, token, "analyzed", {"succeeded": 1, "failed": 0, "total": 1})
    db.expire_all()
    assert json.loads(db.get(Project, item.id).progress_json) == {}
    assert project_to_dict(db.get(Project, item.id))["progress"] == {}
