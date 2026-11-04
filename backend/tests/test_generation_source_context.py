"""指代来源来自同项目同节任务，独立数据库、不读取旧成果或调用模型。"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app import generate
from app.models import ChapterTask, ModuleContent, Project


def add_task(db, item, title, *, kind="heading", parent=None, instruction="", sort=0):
    task = ChapterTask(project_id=item.id, title=title, task_type=kind,
                       parent_id=parent.id if parent else None, instruction=instruction, sort=sort)
    db.add(task)
    db.flush()
    return task


def test_nearest_section_uses_source_tasks_and_keeps_unrelated_sections_out(project, db):
    item, own, _user = project
    top = add_task(db, item, "调查结果")
    section = add_task(db, item, "价值认同", parent=top)
    other = add_task(db, item, "行为感知", parent=top)
    own.parent_id = section.id
    own.instruction = "【填写：只描述上述帮助体验，不讨论满意度】"
    plot = add_task(db, item, "三道帮助题", kind="image", parent=section, sort=2,
                    instruction="【生成图片：中学生第12题、大学生第12、10题】\n【成文要求】重复公共规则")
    add_task(db, item, "邻表", kind="table", parent=section, sort=1,
             instruction="【生成表格：本题真实来源】\n【证据要求】重复公共规则")
    add_task(db, item, "无关满意度", kind="image", parent=other,
             instruction="【生成图片：小学生第11题】")
    db.add(ModuleContent(chapter_task_id=plot.id, content_type="image", content="不应读取的旧成品",
                         version=1, is_current=True))
    db.commit()
    queries = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement.lower())

    event.listen(db.get_bind(), "before_cursor_execute", capture)
    try:
        context = generate.related_task_source_context(db, own)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)
    assert "中学生第12题、大学生第12、10题" in context
    assert "无关满意度" not in context and "小学生第11题" not in context
    assert "旧成品" not in context and "重复公共规则" not in context
    assert "不代表相邻成果已经生成或通过审核" in context
    assert "范围以本任务显式指令为准" in context
    assert context.index("邻表") < context.index("三道帮助题")
    assert not any("module_content" in query for query in queries)
    assert not any(query.lstrip().startswith(("update", "insert", "delete")) for query in queries)
    assert own.instruction == "【填写：只描述上述帮助体验，不讨论满意度】"


def test_summary_without_neighbors_can_look_up_exactly_one_level(project, db):
    item, own, _user = project
    report = add_task(db, item, "整份报告")
    section = add_task(db, item, "主体认知", parent=report)
    sibling = add_task(db, item, "价值认同", parent=section)
    summary = add_task(db, item, "小结", parent=section)
    own.parent_id = summary.id
    add_task(db, item, "帮助题", kind="image", parent=sibling,
             instruction="【生成图片：大学生第12题】")
    add_task(db, item, "其他部分", kind="image", parent=report,
             instruction="【生成图片：教师第22题】")
    db.commit()
    context = generate.related_task_source_context(db, own)
    assert "大学生第12题" in context and "教师第22题" not in context


def test_no_source_after_one_parent_step_does_not_expand_to_whole_report(project, db):
    item, own, _user = project
    report = add_task(db, item, "整份报告")
    section = add_task(db, item, "空节", parent=report)
    summary = add_task(db, item, "小结", parent=section)
    own.parent_id = summary.id
    add_task(db, item, "无关图", kind="image", parent=report,
             instruction="【生成图片：教师第22题】")
    db.commit()
    assert generate.related_task_source_context(db, own) == ""


def test_cross_project_parent_and_cycles_fail_without_scope_leak(project, db):
    item, own, user = project
    other = Project(user_id=user.id, name="另一个项目")
    db.add(other)
    db.flush()
    foreign_parent = add_task(db, other, "外项目标题")
    add_task(db, other, "外项目图", parent=foreign_parent, kind="image",
             instruction="【生成图片：不能访问的来源】")
    own.parent_id = foreign_parent.id
    db.commit()
    assert generate.related_task_source_context(db, own) == ""
    first = add_task(db, item, "循环标题甲")
    second = add_task(db, item, "循环标题乙", parent=first)
    first.parent_id, own.parent_id = second.id, second.id
    add_task(db, item, "循环内图", parent=first, kind="image", instruction="【生成图片：不合法树】")
    db.commit()
    assert generate.related_task_source_context(db, own) == ""


@pytest.mark.parametrize("kind", ["image", "table"])
def test_explicit_chart_or_table_scope_is_not_extended_by_neighbor_context(project, db, kind):
    item, own, _user = project
    parent = add_task(db, item, "实践")
    own.parent_id, own.task_type = parent.id, kind
    own.instruction = "【生成图片：依据教师问卷第13题，使用真实资料；不据教师第12题筛选】"
    add_task(db, item, "邻图", parent=parent, kind="image",
             instruction="【生成图片：依据小学生问卷第1题，使用真实资料】")
    db.commit()
    assert generate.related_task_source_context(db, own) == ""


@pytest.mark.parametrize("instruction,expected", [
    ("【填写】依据大学生第7题描述梯度感知。\n【篇幅与结构】300字\n【证据要求】公共规则",
     "【填写】依据大学生第7题描述梯度感知。"),
    ("【生成图片：依据中学生第12题、大学生第12、10题】\n【成文要求】公共规则",
     "【生成图片：依据中学生第12题、大学生第12、10题】"),
])
def test_primary_instruction_keeps_source_body_after_short_directive_tag(instruction, expected):
    assert generate._primary_task_instruction(instruction) == expected


def test_generation_goal_receives_source_context_but_own_instruction_stays_intact(
        project, db, monkeypatch, tmp_path):
    item, own, _user = project
    parent = add_task(db, item, "价值认同")
    own.parent_id = parent.id
    own.instruction = "【填写：只解释上述三道题的主观帮助，不合并不同问卷题目】"
    add_task(db, item, "三题图", parent=parent, kind="image",
             instruction="【生成图片：中学生问卷第12题、大学生问卷第12、10题】\n【公共要求】不得重复")
    db.commit()
    observed = {}
    monkeypatch.setattr(generate, "pick_model", lambda *_args: SimpleNamespace(model="offline"))
    monkeypatch.setattr(generate, "build_materials", lambda *_args: ("原问卷", "原始资料"))
    monkeypatch.setattr(generate, "_prepare_workdir", lambda *_args, **_kwargs: (tmp_path, []))

    async def react(_db, _cfg, goal, *_args, **_kwargs):
        observed["goal"] = goal
        return json.dumps({"content": "报告正文"}, ensure_ascii=False)

    async def review_ok(*_args, **_kwargs):
        return {"status": "ok", "issues": [], "repairs": []}

    from app import review as review_module
    monkeypatch.setattr(generate, "react_run", react)
    monkeypatch.setattr(review_module, "review_generated_content", review_ok)
    result = asyncio.run(generate.generate_task(db, item, own))
    assert result.status == "ok"
    assert own.instruction in observed["goal"]
    assert "中学生问卷第12题、大学生问卷第12、10题" in observed["goal"]
    assert "范围以本任务显式指令为准" in observed["goal"]
    assert "不得重复" not in observed["goal"]
    assert observed["goal"].index(own.instruction) < observed["goal"].index("【本节其他任务的来源参考】")
    assert own.instruction == "【填写：只解释上述三道题的主观帮助，不合并不同问卷题目】"
