# -*- coding: utf-8 -*-
"""导出审核智能体：重复图号收敛、轮次无上限、无进展终止与跨轮反馈。"""
import asyncio
import json
from types import SimpleNamespace

from app import generate, review
from app.models import ChapterTask, ModelConfig, ModuleContent
from app.report_text import dedupe_leading_figure_number


def _review_config(db, user):
    db.add(ModelConfig(user_id=user.id, name="审核", agent_type="", base_url="http://offline",
                       api_key_enc="enc", model="offline", is_default=True))
    db.commit()


def _seed_content(db, item, task, content):
    row = ModuleContent(
        chapter_task_id=task.id, content_type=task.task_type, content=content,
        version=1, is_current=True, status="ok",
        material_revision=item.material_revision,
        instruction_revision=task.instruction_revision,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_dedupe_leading_figure_number():
    assert dedupe_leading_figure_number(
        "图2.7-1 图2.7-1 大中小学学生满意度分布") == "图2.7-1 大中小学学生满意度分布"
    assert dedupe_leading_figure_number(
        "图 2.7-6  图 2.7-6 贵阳贵安地区") == "图 2.7-6 贵阳贵安地区"
    # 正常图题与不同图号保持原样
    assert dedupe_leading_figure_number("图2.7-1 主题分布") == "图2.7-1 主题分布"
    assert dedupe_leading_figure_number("图2.7-1 图2.7-2 对比") == "图2.7-1 图2.7-2 对比"
    assert dedupe_leading_figure_number("正文图2.7-1 图2.7-1") == "正文图2.7-1 图2.7-1"


def test_sanitize_chart_data_handles_multi_panel():
    data = {
        "caption": "图2.7-1 图2.7-1 总体分布",
        "charts": [
            {"title": "小学生", "caption": "图2.7-2 图2.7-2 小学生"},
            {"title": "中学生", "caption": "图2.7-3 中学生"},
        ],
    }
    fixed = generate.sanitize_chart_data(data)
    assert fixed["caption"] == "图2.7-1 总体分布"
    assert fixed["charts"][0]["caption"] == "图2.7-2 小学生"
    assert fixed["charts"][1]["caption"] == "图2.7-3 中学生"
    assert data["caption"] == "图2.7-1 图2.7-1 总体分布"  # 输入不被就地修改


def test_normalize_chart_captions_saves_new_version(project, db):
    item, _task, user = project
    part = ChapterTask(project_id=item.id, title="第二部分 调查结果", instruction="",
                       task_type="heading", status="pending", sort=0)
    db.add(part)
    db.flush()
    section = ChapterTask(project_id=item.id, title="七、提质优化与综合反馈", instruction="",
                          task_type="heading", status="pending", parent_id=part.id, sort=1)
    db.add(section)
    db.flush()
    image_task = ChapterTask(project_id=item.id, title="图表", instruction="【生成图片】",
                             task_type="image", status="pending",
                             parent_id=section.id, sort=2)
    db.add(image_task)
    db.commit()
    db.refresh(image_task)
    _seed_content(db, item, image_task, json.dumps({
        "title": "满意度", "labels": ["A"], "values": [1],
        "caption": "图9.9-9 图9.9-9 满意度分布",
    }, ensure_ascii=False))

    review._normalize_chart_captions(db, item, [part, section, image_task], None)
    current = review._current_contents(db, item.id)[image_task.id]
    assert current.version == 2
    assert json.loads(current.content)["caption"] == "图2.7-1 满意度分布"
    assert db.get(ChapterTask, image_task.id).status == "done"


def test_targeted_repair_marks_task_done(project, db):
    item, task, _user = project
    task.status = "failed"
    _seed_content(db, item, task, "旧表述只出现一次。" + "其余正文保持不变。" * 20)
    db.commit()
    applied = review._apply_repairs(db, item, [{
        "task_id": task.id,
        "type": "text",
        "old": "旧表述",
        "new": "新表述",
    }], "offline")
    assert task.id in applied
    assert db.get(ChapterTask, task.id).status == "done"


def test_issue_signature_ignores_warnings_and_order():
    first = [{"severity": "error", "task_id": 2, "message": "B"},
             {"severity": "error", "task_id": 1, "message": "A"},
             {"severity": "warning", "task_id": 1, "message": "提示"}]
    second = [{"severity": "error", "task_id": 1, "message": "A"},
              {"severity": "error", "task_id": 2, "message": "B"}]
    assert review._issue_signature(first) == review._issue_signature(second)
    assert "提示" not in review._issue_signature(first)


def test_export_review_keeps_trying_until_retry_cap(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    _seed_content(db, item, task, "正文" * 60)
    calls = {"review": 0, "regen": 0}
    feedback_seen = []

    async def module_review(*_args, **_kwargs):
        calls["review"] += 1
        return {"status": "issues", "issues": [
            {"severity": "error", "message": "小学生第十三题数值互换"}], "repairs": []}

    async def regenerate(project_id, pending, feedback, results, run_token, done, total, **_kwargs):
        calls["regen"] += len(pending)
        feedback_seen.append(feedback[pending[0].id])
        return done + len(pending), {item.id for item in pending}

    monkeypatch.setattr(review, "_module_review", module_review)
    monkeypatch.setattr(review, "_regenerate_tasks_parallel", regenerate)

    result = asyncio.run(review.review_and_repair(db, item, max_rounds=0))
    assert not result["ok"]
    # 相同问题也必须完成首次失败后的 5 次自动重试。
    assert calls["regen"] == 5
    assert calls["review"] == 6
    assert any("已自动重生成 5 次" in issue["message"] for issue in result["issues"])
    # 第二轮起必须把前几轮未解决的问题带回给模型
    assert any("前几轮已提出但未解决" in text for text in feedback_seen[1:])


def test_sanitize_review_issues_downgrades_numbering_and_misattribution():
    from types import SimpleNamespace
    text_task = SimpleNamespace(id=1, task_type="text")
    image_task = SimpleNamespace(id=2, task_type="image")
    issues = [
        {"severity": "error", "message": "题号格式不统一，应统一为中文数字"},
        {"severity": "error", "message": "labels 与标题不一致", "task_id": 1},
        {"severity": "error", "message": "q13 数值互换", "task_id": 1},
        {"severity": "error", "message": "labels 与标题不一致", "task_id": 2},
    ]
    result = review._sanitize_review_issues(issues, {1: text_task, 2: image_task})
    assert [item["severity"] for item in result] == [
        "warning", "warning", "error", "error"]
    assert "已转为提示" in result[0]["message"]
    assert "模块类型不符" in result[1]["message"]


def test_export_regeneration_uses_cumulative_diagnosis(project, db, monkeypatch):
    item, task, _user = project
    _review_config(db, _user)
    current = _seed_content(db, item, task, "正文" * 60)
    diagnosis = json.dumps({
        "attempt": 2,
        "root_causes": ["复算口径不一致"],
        "fix_strategies": ["重新按题目分组复算"],
        "failed_approaches": ["沿用旧分母"],
        "must_verify": ["逐组核对分母"],
    }, ensure_ascii=False)
    captured = {}

    async def fake_diagnose(_db, _project, _task, **kwargs):
        captured["diagnosis_args"] = kwargs
        return diagnosis

    async def fake_generate(_db, _project, _task, user_prompt="", run_token=None,
                            failure_summary=""):
        captured["prompt"] = user_prompt
        captured["failure_summary"] = failure_summary
        return SimpleNamespace(status="ok", error_msg="")

    monkeypatch.setattr(generate, "diagnose_failure", fake_diagnose)
    monkeypatch.setattr(generate, "generate_task", fake_generate)
    done, succeeded = asyncio.run(review._regenerate_tasks_parallel(
        item.id, [task], {task.id: "【审核意见】小学生第十三题数值互换"},
        {}, None, 0, 1, attempts={task.id: 2}, retry_limit=5))

    assert done == 1 and succeeded == {task.id}
    assert captured["diagnosis_args"]["attempt"] == 2
    assert captured["diagnosis_args"]["retry_limit"] == 5
    assert "审核意见" in captured["prompt"]
    assert "自动失败诊断" not in captured["prompt"]
    assert captured["failure_summary"] == diagnosis
    assert "复算口径不一致" in generate.build_retry_prompt(
        captured["prompt"], captured["failure_summary"])
    db.expire_all()
    assert db.get(ModuleContent, current.id).failure_summary == diagnosis


def test_task_regeneration_is_capped(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    _seed_content(db, item, task, "正文" * 60)
    calls = {"regen": 0}

    async def module_review(*_args, **_kwargs):
        return {"status": "issues", "issues": [
            {"severity": "error", "message": "小学生第十三题数值互换"}], "repairs": []}

    async def regenerate(project_id, pending, feedback, results, run_token, done, total, **_kwargs):
        calls["regen"] += len(pending)
        return done + len(pending), {item.id for item in pending}

    monkeypatch.setattr(review, "_module_review", module_review)
    monkeypatch.setattr(review, "_regenerate_tasks_parallel", regenerate)
    monkeypatch.setattr(review.settings, "FAILURE_RETRY_LIMIT", 2)

    result = asyncio.run(review.review_and_repair(db, item, max_rounds=0))
    assert not result["ok"]
    assert calls["regen"] <= 2
    assert any("已自动重生成 2 次" in issue["message"] for issue in result["issues"])
