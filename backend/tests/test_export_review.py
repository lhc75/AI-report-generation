"""导出前 AI 审核：复用未变更结论、自动修正、分级放行与协议校验。"""
import asyncio
import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from app import review
from app.models import ChapterTask, ModelConfig, ModuleContent


def _review_config(db, user):
    db.add(ModelConfig(user_id=user.id, name="审核", agent_type="", base_url="http://offline",
                       api_key_enc="enc", model="offline", is_default=True))
    db.commit()


def _seed_content(db, item, task, content, review_record=None):
    row = ModuleContent(
        chapter_task_id=task.id,
        content_type=task.task_type,
        content=content,
        version=1,
        is_current=True,
        status="ok",
        material_revision=item.material_revision,
        instruction_revision=task.instruction_revision,
        review_json=json.dumps(review_record) if review_record else "{}",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _ok_record(row, item, task):
    return {
        "status": "ok",
        "issues": [],
        "protocol": review.REVIEW_PROTOCOL_VERSION,
        "content_sha256": sha256(row.content.encode()).hexdigest(),
        "material_revision": item.material_revision,
        "instruction_revision": task.instruction_revision,
        "reviewed_at": 1,
    }


async def _final_ok(*_args, **_kwargs):
    return {"status": "ok", "issues": [], "repairs": []}


def test_unchanged_content_reuses_review_and_only_runs_final_review(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    row = _seed_content(db, item, task, "正文" * 60)
    row.review_json = json.dumps(_ok_record(row, item, task))
    db.commit()

    async def fail_module(*_args, **_kwargs):
        raise AssertionError("未变更模块不应重复复核")

    monkeypatch.setattr(review, "_module_review", fail_module)
    monkeypatch.setattr(review, "_final_review", _final_ok)
    result = asyncio.run(review.review_and_repair(db, item))
    assert result["ok"], result
    assert result["warnings"] == []


def test_error_triggers_repair_then_passes(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    _seed_content(db, item, task, "支持者有2人，占66.7%。" + "补充说明" * 20)
    seen = []

    async def module_review(_db, _cfg, _project, task_, row_, *_args, **_kwargs):
        seen.append(row_.content)
        if "支持者有2人" in row_.content:
            return {
                "status": "issues",
                "issues": [{"severity": "error", "message": "数字与数据不符"}],
                "repairs": [{"task_id": task_.id, "type": "text",
                             "old": "支持者有2人", "new": "支持者有3人", "cell": ""}],
            }
        return {"status": "ok", "issues": [], "repairs": []}

    monkeypatch.setattr(review, "_module_review", module_review)
    monkeypatch.setattr(review, "_final_review", _final_ok)
    result = asyncio.run(review.review_and_repair(db, item))
    assert result["ok"], result
    assert len(seen) == 2 and "支持者有3人" in seen[1]
    current = review._current_contents(db, item.id)[task.id]
    assert "支持者有3人" in current.content
    assert current.version == 2


def test_error_without_repair_regenerates_and_passes(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    _seed_content(db, item, task, "旧正文" * 40)
    state = {"reviewed": 0}

    async def module_review(_db, _cfg, _project, task_, row_, *_args, **_kwargs):
        state["reviewed"] += 1
        if row_.content.startswith("旧正文"):
            return {"status": "issues",
                    "issues": [{"severity": "error", "message": "内容缺失"}],
                    "repairs": []}
        return {"status": "ok", "issues": [], "repairs": []}

    async def regenerate(_db, _project, task_, user_prompt="", run_token=None):
        assert "审核意见" in user_prompt
        assert "一律以复算为准" in user_prompt
        _db.query(ModuleContent).filter(
            ModuleContent.chapter_task_id == task_.id).update({"is_current": False})
        row = ModuleContent(chapter_task_id=task_.id, content_type="text",
                            content="新正文" + "字" * 60, version=2, is_current=True, status="ok",
                            material_revision=_project.material_revision,
                            instruction_revision=task_.instruction_revision)
        _db.add(row)
        _db.commit()
        return row

    monkeypatch.setattr(review, "_module_review", module_review)
    monkeypatch.setattr(review, "_final_review", _final_ok)
    monkeypatch.setattr(review.generate, "generate_task", regenerate)
    result = asyncio.run(review.review_and_repair(db, item))
    assert result["ok"], result
    assert state["reviewed"] == 2
    current = review._current_contents(db, item.id)[task.id]
    assert current.content.startswith("新正文")


def test_error_without_repair_blocks_export_when_regeneration_fails(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    _seed_content(db, item, task, "正文" * 60)
    regenerated = []

    async def module_review(*_args, **_kwargs):
        return {"status": "issues",
                "issues": [{"severity": "error", "message": "数字无法从数据复算"}],
                "repairs": []}

    async def fail_regenerate(_db, _project, task_, user_prompt="", run_token=None):
        regenerated.append(task_.id)
        return SimpleNamespace(status="failed", error_msg="模拟重新生成失败")

    monkeypatch.setattr(review, "_module_review", module_review)
    monkeypatch.setattr(review.generate, "generate_task", fail_regenerate)
    result = asyncio.run(review.review_and_repair(db, item))
    assert not result["ok"]
    assert result["issues"][0]["message"] == "数字无法从数据复算"
    # 拦截信息以章节位置为标题，不再展示写作指令样式的内容。
    assert result["issues"][0]["title"] == "（文档开头）"
    assert regenerated == [task.id]


def test_final_review_error_regenerates_target_and_passes(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    _seed_content(db, item, task, "正文" * 60)
    state = {"final": 0}

    async def module_ok(*_args, **_kwargs):
        return {"status": "ok", "issues": [], "repairs": []}

    async def final_with_target(*_args, **_kwargs):
        state["final"] += 1
        if state["final"] == 1:
            return {"status": "issues", "issues": [
                {"severity": "error", "task_ids": [task.id], "message": "数字与数据不符"}],
                "repairs": []}
        return {"status": "ok", "issues": [], "repairs": []}

    async def regenerate(_db, _project, task_, user_prompt="", run_token=None):
        assert "审核意见" in user_prompt
        _db.query(ModuleContent).filter(
            ModuleContent.chapter_task_id == task_.id).update({"is_current": False})
        row = ModuleContent(chapter_task_id=task_.id, content_type="text",
                            content="修正后的正文" + "字" * 60, version=2, is_current=True, status="ok",
                            material_revision=_project.material_revision,
                            instruction_revision=task_.instruction_revision)
        _db.add(row)
        _db.commit()
        return row

    monkeypatch.setattr(review, "_module_review", module_ok)
    monkeypatch.setattr(review, "_final_review", final_with_target)
    monkeypatch.setattr(review.generate, "generate_task", regenerate)
    result = asyncio.run(review.review_and_repair(db, item))
    assert result["ok"], result
    assert state["final"] == 2


def test_issue_task_ids_only_keeps_known_modules():
    assert review._issue_task_ids([3479, 3488, 3480], {3479, 3480}) == [3479, 3480]
    assert review._issue_task_ids("3480", None) == [3480]
    assert review._issue_task_ids(["3480", 3480], {3480}) == [3480]
    assert review._issue_task_ids(["无编号", None], {3480}) == []


def test_final_review_conflict_regenerates_all_referenced_modules(project, db, monkeypatch):
    item, text_task, user = project
    _review_config(db, user)
    image_task = ChapterTask(project_id=item.id, title="图表", instruction="【生成图片】",
                             task_type="image", status="pending")
    db.add(image_task)
    db.commit()
    db.refresh(image_task)
    _seed_content(db, item, text_task, "正文" * 60)
    _seed_content(db, item, image_task, json.dumps(
        {"title": "图", "labels": ["A"], "values": [1]}, ensure_ascii=False))
    state = {"final": 0, "regenerated": [], "rechecked": []}

    async def module_ok(*_args, **_kwargs):
        state["rechecked"].append(_args[3].id if len(_args) > 3 else 0)
        return {"status": "ok", "issues": [], "repairs": []}

    async def final_conflict(*_args, **_kwargs):
        state["final"] += 1
        if state["final"] == 1:
            return {"status": "issues", "issues": [{
                "severity": "error",
                "task_ids": [text_task.id, image_task.id],
                "message": (f"数据冲突：task_id {text_task.id} 正文称67.1%，"
                            f"但 task_id {image_task.id} 图表为61.54%。"),
            }], "repairs": []}
        return {"status": "ok", "issues": [], "repairs": []}

    async def regenerate(_db, _project, task_, user_prompt="", run_token=None):
        state["regenerated"].append(task_.id)
        assert "审核意见" in user_prompt
        _db.query(ModuleContent).filter(
            ModuleContent.chapter_task_id == task_.id).update({"is_current": False})
        content = ("修正后的正文" + "字" * 60 if task_.task_type == "text"
                   else json.dumps({"title": "图", "labels": ["A"], "values": [2]},
                                   ensure_ascii=False))
        row = ModuleContent(chapter_task_id=task_.id, content_type=task_.task_type,
                            content=content, version=2, is_current=True, status="ok",
                            material_revision=_project.material_revision,
                            instruction_revision=task_.instruction_revision)
        _db.add(row)
        _db.commit()
        return row

    monkeypatch.setattr(review, "_module_review", module_ok)
    monkeypatch.setattr(review, "_final_review", final_conflict)
    monkeypatch.setattr(review.generate, "generate_task", regenerate)
    result = asyncio.run(review.review_and_repair(db, item))
    assert result["ok"], result
    assert state["final"] == 2
    assert sorted(state["regenerated"]) == sorted([text_task.id, image_task.id])
    # 整稿复核触发的重生成内容必须再做模块级复评。
    assert sorted(set(state["rechecked"])) == sorted([text_task.id, image_task.id])


def test_final_review_repair_applied_directly_without_regeneration(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    _seed_content(db, item, task, "统计显示，小学生积极评价率83.4%，其余群体均已核对。" + "补充说明" * 20)
    state = {"final": 0}

    async def module_ok(*_args, **_kwargs):
        return {"status": "ok", "issues": [], "repairs": []}

    async def final_with_repair(*_args, **_kwargs):
        state["final"] += 1
        if state["final"] == 1:
            return {"status": "issues",
                    "issues": [{"severity": "error", "task_ids": [task.id],
                                "message": "小学口径应为 97.8%，与图表不一致"}],
                    "repairs": [{"task_id": task.id, "type": "text",
                                 "old": "83.4%", "new": "97.8%", "cell": ""}]}
        return {"status": "ok", "issues": [], "repairs": []}

    async def fail_regenerate(*_args, **_kwargs):
        raise AssertionError("可精确定位的修复不应触发整模块重生成")

    monkeypatch.setattr(review, "_module_review", module_ok)
    monkeypatch.setattr(review, "_final_review", final_with_repair)
    monkeypatch.setattr(review.generate, "generate_task", fail_regenerate)
    result = asyncio.run(review.review_and_repair(db, item))
    assert result["ok"], result
    assert state["final"] == 2
    current = review._current_contents(db, item.id)[task.id]
    assert "97.8%" in current.content and "83.4%" not in current.content
    assert current.version == 2


def test_final_review_repair_needs_unique_fragment_then_regenerates(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    _seed_content(db, item, task, "正文" * 60)
    state = {"final": 0, "prompts": []}

    async def module_ok(*_args, **_kwargs):
        return {"status": "ok", "issues": [], "repairs": []}

    async def final_with_repair(*_args, **_kwargs):
        state["final"] += 1
        if state["final"] == 1:
            return {"status": "issues",
                    "issues": [{"severity": "error", "task_ids": [task.id],
                                "message": "数字与数据不符"}],
                    "repairs": [{"task_id": task.id, "type": "text",
                                 "old": "正文", "new": "正文二", "cell": ""}]}
        return {"status": "ok", "issues": [], "repairs": []}

    async def regenerate(_db, _project, task_, user_prompt="", run_token=None):
        state["prompts"].append(user_prompt)
        _db.query(ModuleContent).filter(
            ModuleContent.chapter_task_id == task_.id).update({"is_current": False})
        row = ModuleContent(chapter_task_id=task_.id, content_type="text",
                            content="重新撰写后的正文" + "字" * 60, version=2, is_current=True,
                            status="ok", material_revision=_project.material_revision,
                            instruction_revision=task_.instruction_revision)
        _db.add(row)
        _db.commit()
        return row

    monkeypatch.setattr(review, "_module_review", module_ok)
    monkeypatch.setattr(review, "_final_review", final_with_repair)
    monkeypatch.setattr(review.generate, "generate_task", regenerate)
    result = asyncio.run(review.review_and_repair(db, item))
    assert result["ok"], result
    assert state["prompts"], "片段不唯一时应回落到重生成"
    assert "一律以复算为准" in state["prompts"][0]
    current = review._current_contents(db, item.id)[task.id]
    assert current.content.startswith("重新撰写后的正文")


def test_review_task_id_accepts_numeric_string():
    obj = review._normalize_review_payload(json.dumps(
        {"status": "issues", "issues": [
            {"severity": "error", "task_id": "3480", "message": "x"}]},
        ensure_ascii=False))
    assert obj["issues"][0]["task_ids"] == [3480]


def test_final_review_protocol_requires_known_module_ids():
    valid = review._normalize_review_payload(json.dumps(
        {"status": "issues", "issues": [
            {"severity": "error", "task_ids": [3469, 3470], "message": "数字冲突"}]},
        ensure_ascii=False), module_ids={3469, 3470})
    assert valid["issues"][0]["task_ids"] == [3469, 3470]
    for bad in (
        {"status": "issues", "issues": [{"severity": "error", "message": "无编号"}]},
        {"status": "issues", "issues": [{"severity": "error", "task_ids": [9999], "message": "越界"}]},
    ):
        with pytest.raises(review.ReviewError):
            review._normalize_review_payload(json.dumps(bad, ensure_ascii=False),
                                             module_ids={3469})
    warned = review._normalize_review_payload(json.dumps(
        {"status": "issues", "issues": [{"severity": "warning", "message": "整体建议"}]},
        ensure_ascii=False), module_ids={3469})
    assert warned["issues"] == [{"severity": "warning", "message": "整体建议"}]


async def _module_ok(*_args, **_kwargs):
    return {"status": "ok", "issues": [], "repairs": []}


def test_module_review_input_uses_location_not_instruction_title(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    _seed_content(db, item, task, "正文" * 60)
    heading = ChapterTask(project_id=item.id, title="第二部分 调查结果", instruction="",
                          task_type="heading", status="pending", sort=0)
    db.add(heading)
    db.commit()
    task.parent_id = heading.id
    db.commit()
    captured = {}

    async def fake_react_run(_db, _cfg, _goal, workdir, _task_type, finish_validator=None):
        captured.update(json.loads((workdir / "review_input.json").read_text(encoding="utf-8")))
        return json.dumps({"status": "ok", "issues": [], "repairs": []}, ensure_ascii=False)

    monkeypatch.setattr(review.generate, "react_run", fake_react_run)
    monkeypatch.setattr(review, "_final_review", _final_ok)
    result = asyncio.run(review.review_and_repair(db, item))
    assert result["ok"], result
    assert "标题" not in captured
    assert captured["位置"] == "第二部分 调查结果"
    assert captured["写作指令"] == task.instruction


def test_module_review_unavailable_is_allowed_with_warning(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    _seed_content(db, item, task, "正文" * 60)

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("审核服务不可用")

    async def must_not_regenerate(*_args, **_kwargs):
        raise AssertionError("复核未完成不应触发生成")

    monkeypatch.setattr(review, "_module_review", unavailable)
    monkeypatch.setattr(review, "_final_review", _final_ok)
    monkeypatch.setattr(review.generate, "generate_task", must_not_regenerate)
    result = asyncio.run(review.review_and_repair(db, item))
    assert result["ok"], result
    assert "导出前复核未完成（已放行）" in result["warnings"][0]["message"]


def test_final_review_failure_is_degraded_to_warning(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    row = _seed_content(db, item, task, "正文" * 60)
    row.review_json = json.dumps(_ok_record(row, item, task))
    db.commit()

    async def broken_final(*_args, **_kwargs):
        raise review.ReviewError("审核输出不是合法 JSON")

    monkeypatch.setattr(review, "_final_review", broken_final)
    result = asyncio.run(review.review_and_repair(db, item))
    assert result["ok"], result
    assert any("整稿复核未完成（不影响导出）" in item_["message"]
               for item_ in result["warnings"])


def test_final_error_without_module_ids_is_reported_as_note(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    _seed_content(db, item, task, "正文" * 60)

    async def unattributed_final(*_args, **_kwargs):
        return {"status": "issues", "issues": [
            {"severity": "error", "message": "全文术语需要统一"}], "repairs": []}

    async def must_not_regenerate(*_args, **_kwargs):
        raise AssertionError("定位不到模块的问题不应触发生成")

    monkeypatch.setattr(review, "_module_review", _module_ok)
    monkeypatch.setattr(review, "_final_review", unattributed_final)
    monkeypatch.setattr(review.generate, "generate_task", must_not_regenerate)
    result = asyncio.run(review.review_and_repair(db, item))
    assert result["ok"], result
    assert any("无法定位到具体模块" in item_["message"] for item_ in result["warnings"])


def test_text_repair_cleans_chart_note_and_keeps_json_valid(project, db, monkeypatch):
    item, text_task, user = project
    _review_config(db, user)
    _seed_content(db, item, text_task, "正文" * 60)
    image_task = ChapterTask(project_id=item.id, title="图表", instruction="【生成图片】",
                             task_type="image", status="pending")
    db.add(image_task)
    db.commit()
    db.refresh(image_task)
    dirty_note = "原误用数据键q8，已修正为q9"
    _seed_content(db, item, image_task, json.dumps(
        {"title": "图", "labels": ["A"], "values": [1], "note": dirty_note},
        ensure_ascii=False))

    async def module_review(_db, _cfg, _project, task_, row_, *_args, **_kwargs):
        if dirty_note in row_.content:
            return {"status": "issues",
                    "issues": [{"severity": "error", "message": "图注含过程性文字"}],
                    "repairs": [{"task_id": task_.id, "type": "text",
                                 "old": dirty_note, "new": "单选题，选项占比", "cell": ""}]}
        return {"status": "ok", "issues": [], "repairs": []}

    monkeypatch.setattr(review, "_module_review", module_review)
    monkeypatch.setattr(review, "_final_review", _final_ok)
    result = asyncio.run(review.review_and_repair(db, item))
    assert result["ok"], result
    current = review._current_contents(db, item.id)[image_task.id]
    payload = json.loads(current.content)
    assert payload["note"] == "单选题，选项占比"
    assert current.version >= 3  # 规范化生成新版本，修复再生成一版


def test_warning_only_passes_with_warnings(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    _seed_content(db, item, task, "正文" * 60)

    async def module_review(*_args, **_kwargs):
        return {"status": "issues",
                "issues": [{"severity": "warning", "message": "建议精简表述"}],
                "repairs": []}

    monkeypatch.setattr(review, "_module_review", module_review)
    monkeypatch.setattr(review, "_final_review", _final_ok)
    result = asyncio.run(review.review_and_repair(db, item))
    assert result["ok"], result
    assert result["warnings"][0]["message"] == "建议精简表述"


def test_missing_content_blocks_export(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)

    async def fail_regenerate(_db, _project, task_, user_prompt="", run_token=None):
        return SimpleNamespace(status="failed", error_msg="没有可用的生成环境")

    monkeypatch.setattr(review.generate, "generate_task", fail_regenerate)
    result = asyncio.run(review.review_and_repair(db, item))
    assert not result["ok"]
    assert "尚未生成" in result["issues"][0]["message"] or "内容为空" in result["issues"][0]["message"]


def test_empty_content_regenerates_even_with_stale_ok_cache(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    row = _seed_content(db, item, task, "")
    # 空内容被旧逻辑误存为 ok 缓存；新逻辑必须绕过缓存并要求重新生成。
    row.review_json = json.dumps(_ok_record(row, item, task))
    db.commit()
    state = {"reviewed": 0, "regenerated": 0}

    async def module_review(_db, _cfg, _project, task_, row_, *_args, **_kwargs):
        state["reviewed"] += 1
        assert (row_.content or "").strip(), "空内容不应进入模型复核"
        return {"status": "ok", "issues": [], "repairs": []}

    async def regenerate(_db, _project, task_, user_prompt="", run_token=None):
        state["regenerated"] += 1
        assert "内容为空" in user_prompt
        _db.query(ModuleContent).filter(
            ModuleContent.chapter_task_id == task_.id).update({"is_current": False})
        new_row = ModuleContent(chapter_task_id=task_.id, content_type="text",
                                content="重新生成的正文" + "字" * 60, version=2, is_current=True,
                                status="ok", material_revision=_project.material_revision,
                                instruction_revision=task_.instruction_revision)
        _db.add(new_row)
        _db.commit()
        return new_row

    monkeypatch.setattr(review, "_module_review", module_review)
    monkeypatch.setattr(review, "_final_review", _final_ok)
    monkeypatch.setattr(review.generate, "generate_task", regenerate)
    result = asyncio.run(review.review_and_repair(db, item))
    assert result["ok"], result
    assert state == {"reviewed": 1, "regenerated": 1}


def test_review_protocol_validation_and_formatting():
    obj = review._normalize_review_payload(json.dumps(
        {"status": "issues", "issues": [{"severity": "warning", "message": "x"}]},
        ensure_ascii=False))
    assert obj["issues"] == [{"severity": "warning", "message": "x"}]
    with pytest.raises(review.ReviewError):
        review._normalize_review_payload("不是 JSON")
    with pytest.raises(review.ReviewError):
        review._normalize_review_payload(json.dumps({"status": "ok", "issues": [{"message": "x"}]}))
    # 形状不完整的修正项只被丢弃，不影响问题清单的解析。
    tolerant = review._normalize_review_payload(json.dumps(
        {"status": "issues", "issues": [{"severity": "warning", "message": "x"}],
         "repairs": [{"task_id": 1, "type": "text", "old": "", "new": "y"},
                     {"task_id": 1, "type": "table_cell", "old": "1", "new": "2"}]}))
    assert tolerant["repairs"] == []
    assert "严重" in review.format_issues([{"severity": "error", "title": "T", "message": "M"}])
