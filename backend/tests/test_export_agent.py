# -*- coding: utf-8 -*-
"""导出审核智能体：重复图号收敛、轮次无上限、无进展终止与跨轮反馈。"""
import asyncio
import json

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


def test_issue_signature_ignores_warnings_and_order():
    first = [{"severity": "error", "task_id": 2, "message": "B"},
             {"severity": "error", "task_id": 1, "message": "A"},
             {"severity": "warning", "task_id": 1, "message": "提示"}]
    second = [{"severity": "error", "task_id": 1, "message": "A"},
              {"severity": "error", "task_id": 2, "message": "B"}]
    assert review._issue_signature(first) == review._issue_signature(second)
    assert "提示" not in review._issue_signature(first)


def test_export_review_keeps_trying_then_stops_on_no_progress(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    _seed_content(db, item, task, "正文" * 60)
    calls = {"review": 0, "regen": 0}
    feedback_seen = []

    async def module_review(*_args, **_kwargs):
        calls["review"] += 1
        return {"status": "issues", "issues": [
            {"severity": "error", "message": "小学生第十三题数值互换"}], "repairs": []}

    async def regenerate(project_id, pending, feedback, results, run_token, done, total):
        calls["regen"] += len(pending)
        feedback_seen.append(feedback[pending[0].id])
        return done + len(pending), {item.id for item in pending}

    monkeypatch.setattr(review, "_module_review", module_review)
    monkeypatch.setattr(review, "_regenerate_tasks_parallel", regenerate)

    result = asyncio.run(review.review_and_repair(db, item, max_rounds=0))
    assert not result["ok"]
    # 不再受固定 2 轮限制：会先持续尝试，达到无进展阈值才停止
    assert calls["review"] > 3
    assert any("无进展" in issue["message"] for issue in result["issues"])
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


def test_task_regeneration_is_capped(project, db, monkeypatch):
    item, task, user = project
    _review_config(db, user)
    _seed_content(db, item, task, "正文" * 60)
    calls = {"regen": 0}

    async def module_review(*_args, **_kwargs):
        return {"status": "issues", "issues": [
            {"severity": "error", "message": "小学生第十三题数值互换"}], "repairs": []}

    async def regenerate(project_id, pending, feedback, results, run_token, done, total):
        calls["regen"] += len(pending)
        return done + len(pending), {item.id for item in pending}

    monkeypatch.setattr(review, "_module_review", module_review)
    monkeypatch.setattr(review, "_regenerate_tasks_parallel", regenerate)
    monkeypatch.setattr(review.settings, "REVIEW_MAX_TASK_REGENS", 2)

    result = asyncio.run(review.review_and_repair(db, item, max_rounds=0))
    assert not result["ok"]
    assert calls["regen"] <= 2
    assert any("已自动重生成 2 次" in issue["message"] for issue in result["issues"])
