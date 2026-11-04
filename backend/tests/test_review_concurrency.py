"""导出前逐模块复核的受限并发与进度上报。"""
import asyncio
import json
import time

from app import review
from app.config import settings
from app.models import ChapterTask, ModelConfig, ModuleContent


def _review_config(db, user):
    db.add(ModelConfig(user_id=user.id, name="审核", agent_type="", base_url="http://offline",
                       api_key_enc="enc", model="offline", is_default=True))
    db.commit()


def _seed_task(db, item, index):
    task = ChapterTask(project_id=item.id, title=f"模块{index}", instruction=f"【填写：模块{index}】",
                       task_type="text", status="done", sort=index)
    db.add(task)
    db.flush()
    db.add(ModuleContent(chapter_task_id=task.id, content_type="text",
                         content="正文" * 60, version=1, is_current=True, status="ok",
                         material_revision=item.material_revision,
                         instruction_revision=task.instruction_revision))
    db.commit()
    return task


async def _final_ok(*_args, **_kwargs):
    return {"status": "ok", "issues": [], "repairs": []}


def test_module_reviews_run_with_bounded_concurrency_and_report_progress(project, db, monkeypatch):
    item, base_task, user = project
    _review_config(db, user)
    base_task.status = "done"
    db.add(ModuleContent(chapter_task_id=base_task.id, content_type="text",
                         content="正文" * 60, version=1, is_current=True, status="ok",
                         material_revision=item.material_revision,
                         instruction_revision=base_task.instruction_revision))
    db.commit()
    tasks = [base_task] + [_seed_task(db, item, index) for index in range(6)]
    monkeypatch.setattr(settings, "REVIEW_CONCURRENCY", 3)
    monkeypatch.setattr(review.generate, "document_location", lambda *_args: "测试位置")
    progress = []

    def record_progress(project_id, token, *, phase, label, done=0, total=0):
        progress.append((phase, done, total))

    monkeypatch.setattr(review.operations, "report_progress", record_progress)
    state = {"active": 0, "peak": 0}

    async def fake_module_review(_db, _cfg, _project, task, _row, *_args, **_kwargs):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(0.2)
        finally:
            state["active"] -= 1
        return {"status": "ok", "issues": [], "repairs": []}

    monkeypatch.setattr(review, "_module_review", fake_module_review)
    monkeypatch.setattr(review, "_final_review", _final_ok)

    started = time.monotonic()
    result = asyncio.run(review.review_and_repair(db, item))
    elapsed = time.monotonic() - started

    assert result["ok"], result
    assert 2 <= state["peak"] <= 3, f"并发峰值 {state['peak']} 超出限制"
    assert elapsed < 7 * 0.2 * 0.9, f"并发未生效：{elapsed:.2f}s"
    review_calls = [entry for entry in progress if entry[0] == "review"]
    assert review_calls[0] == ("review", 0, 7)
    assert review_calls[-1] == ("review", 7, 7)
    assert any(entry[0] == "final" for entry in progress)

    db.expire_all()
    for task in tasks:
        row = db.query(ModuleContent).filter_by(chapter_task_id=task.id, is_current=True).one()
        record = json.loads(row.review_json)
        assert record["protocol"] == review.REVIEW_PROTOCOL_VERSION
        assert record["status"] == "ok"


def test_repair_regenerations_run_parallel_and_report_progress(project, db, monkeypatch):
    item, base_task, user = project
    _review_config(db, user)
    base_task.status = "done"
    db.add(ModuleContent(chapter_task_id=base_task.id, content_type="text",
                         content="重写成功" + "字" * 40, version=1, is_current=True, status="ok",
                         material_revision=item.material_revision,
                         instruction_revision=base_task.instruction_revision))
    db.commit()
    _seed_task(db, item, index=0)
    for index in range(1, 6):
        _seed_task(db, item, index=index)
    monkeypatch.setattr(settings, "REVIEW_CONCURRENCY", 4)
    monkeypatch.setattr(settings, "GENERATE_CONCURRENCY", 3)
    monkeypatch.setattr(review.generate, "document_location", lambda *_args: "测试位置")
    progress = []

    def record_progress(project_id, token, *, phase, label, done=0, total=0):
        progress.append((phase, label, done, total))

    monkeypatch.setattr(review.operations, "report_progress", record_progress)
    state = {"active": 0, "peak": 0}

    async def fake_module_review(_db, _cfg, _project, task, row, *_args, **_kwargs):
        if (row.content or "").startswith("重写成功"):
            return {"status": "ok", "issues": [], "repairs": []}
        return {"status": "issues", "repairs": [],
                "issues": [{"severity": "error", "message": "数字与数据不符"}]}

    async def fake_regenerate(_db, _project, task, user_prompt="", run_token=None):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(0.2)
        finally:
            state["active"] -= 1
        assert "审核意见" in user_prompt
        _db.query(ModuleContent).filter(
            ModuleContent.chapter_task_id == task.id).update({"is_current": False})
        row = ModuleContent(chapter_task_id=task.id, content_type="text",
                            content="重写成功" + "字" * 40, version=2, is_current=True,
                            status="ok", material_revision=_project.material_revision,
                            instruction_revision=task.instruction_revision)
        _db.add(row)
        _db.commit()
        return row

    monkeypatch.setattr(review, "_module_review", fake_module_review)
    monkeypatch.setattr(review.generate, "generate_task", fake_regenerate)
    monkeypatch.setattr(review, "_final_review", _final_ok)

    started = time.monotonic()
    result = asyncio.run(review.review_and_repair(db, item))
    elapsed = time.monotonic() - started

    assert result["ok"], result
    assert 2 <= state["peak"] <= 3, f"重生成并发峰值 {state['peak']} 超出限制"
    assert elapsed < 6 * 0.2 * 0.9, f"重生成未并行：{elapsed:.2f}s"
    repair_calls = [entry for entry in progress if entry[1] == "定向修正/重新生成"]
    assert repair_calls[0] == ("repair", "定向修正/重新生成", 0, 6)
    assert repair_calls[-1] == ("repair", "定向修正/重新生成", 6, 6)
    counts = [entry[2] for entry in repair_calls]
    assert counts == sorted(counts) and counts[-1] == 6


def test_recheck_runs_with_progress_and_refreshes_results(project, db, monkeypatch):
    item, text_task, user = project
    image_task = ChapterTask(project_id=item.id, title="图表", instruction="【生成图片】",
                             task_type="image", status="pending")
    db.add(image_task)
    db.commit()
    db.refresh(image_task)
    _review_config(db, user)
    from app.models import ModuleContent as Row
    db.add(Row(chapter_task_id=text_task.id, content_type="text", content="旧正文" * 30,
               version=1, is_current=True, status="ok",
               material_revision=item.material_revision,
               instruction_revision=text_task.instruction_revision))
    db.add(Row(chapter_task_id=image_task.id, content_type="image",
               content=json.dumps({"title": "图", "labels": ["A"], "values": [1]}, ensure_ascii=False),
               version=1, is_current=True, status="ok",
               material_revision=item.material_revision,
               instruction_revision=image_task.instruction_revision))
    db.commit()
    progress = []
    monkeypatch.setattr(review.operations, "report_progress",
                        lambda project_id, token, *, phase, label, done=0, total=0:
                        progress.append((phase, label, done, total)))
    state = {"final": 0}

    async def module_review(_db, _cfg, _project, task_, row_, *_args, **_kwargs):
        if (row_.content or "").startswith("修正后"):
            return {"status": "ok", "issues": [], "repairs": []}
        return {"status": "issues", "repairs": [],
                "issues": [{"severity": "error", "message": "数字与数据不符"}]}

    async def final_conflict(*_args, **_kwargs):
        state["final"] += 1
        if state["final"] == 1:
            return {"status": "issues", "issues": [
                {"severity": "error", "task_ids": [text_task.id, image_task.id],
                 "message": "两模块数字冲突"}], "repairs": []}
        return {"status": "ok", "issues": [], "repairs": []}

    async def regenerate(_db, _project, task_, user_prompt="", run_token=None):
        _db.query(Row).filter(Row.chapter_task_id == task_.id).update({"is_current": False})
        content = ("修正后的正文" + "字" * 60 if task_.task_type == "text"
                   else "修正后的图表数据 " + json.dumps(
                       {"title": "图", "labels": ["A"], "values": [2]}, ensure_ascii=False))
        row = Row(chapter_task_id=task_.id, content_type=task_.task_type, content=content,
                  version=2, is_current=True, status="ok",
                  material_revision=_project.material_revision,
                  instruction_revision=task_.instruction_revision)
        _db.add(row)
        _db.commit()
        return row

    monkeypatch.setattr(review, "_module_review", module_review)
    monkeypatch.setattr(review, "_final_review", final_conflict)
    monkeypatch.setattr(review.generate, "generate_task", regenerate)
    result = asyncio.run(review.review_and_repair(db, item))

    assert result["ok"], result
    recheck_calls = [entry for entry in progress if entry[1] == "复评"]
    assert recheck_calls, "复评阶段应上报进度"
    assert recheck_calls[0][2] == 0 and recheck_calls[-1][2] == 2
    assert recheck_calls[-1] == ("review", "复评", 2, 2)
