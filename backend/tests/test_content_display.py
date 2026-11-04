"""Page previews share export editing; saved and historical data stay exact."""
import json

import pytest
from sqlalchemy import select

from app.models import ChapterTask, ModuleContent
from app.report_editorial import make_project_formatter
from app.routers import projects


CAVEAT = "依据现有资料作结构对应，尚未取得正式施测编码字典。"
METHOD = (CAVEAT + "各题以非空作答记录为分母。多选每记录去重，各项比例不能相加。"
          "统计以作答记录为单位，未作独立受访者识别及有效性筛选，不作共同量表排名、总体代表性或因果推断。"
          "“其他”不推断具体内容。改进意向不等同于实施情况或实际成效。")
RESULT = CAVEAT + "本题作答237条，选择116条（48.9%）；参与资格未确认。"


def setup_current(project, db, content_type="text"):
    item, task, user = project
    heading = ChapterTask(project_id=item.id, title="说明", task_type="heading", sort=-3)
    body = ChapterTask(project_id=item.id, title="调查结果", task_type="heading", sort=-1)
    db.add_all([heading, body])
    db.flush()
    method_task = ChapterTask(project_id=item.id, parent_id=heading.id, title="统计方法",
                              task_type="text", status="done", sort=-2)
    db.add(method_task)
    db.flush()
    method = ModuleContent(chapter_task_id=method_task.id, version=1, is_current=True,
                           status="ok", content_type="text", content=METHOD, evidence_json="[]")
    task.parent_id, task.status = body.id, "done"
    content = RESULT if content_type == "text" else json.dumps({
        "title": "调查结果", "columns": ["选项", "记录数", "比例"],
        "rows": [["其他", 116, "48.9%"]], "note": CAVEAT + "参与资格未确认。",
        "cell_metrics": {"0,1": {"metric": "count", "evidence_id": "ev_count"}},
    }, ensure_ascii=False)
    current = ModuleContent(chapter_task_id=task.id, version=2, is_current=True, status="ok",
                            content_type=content_type, content=content,
                            evidence_json='[{"text":"saved evidence"}]')
    history = ModuleContent(chapter_task_id=task.id, version=1, is_current=False, status="ok",
                            content_type=content_type, content=content, evidence_json=current.evidence_json)
    db.add_all([method, current, history])
    db.commit()
    return item, task, user, method_task, method, current, history


@pytest.mark.parametrize("content_type", ["text", "table"])
def test_current_preview_matches_export_formatter_preserves_source_and_history(project, db, content_type):
    item, task, user, method_task, _method, current, history = setup_current(project, db, content_type)
    saved = current.content, current.evidence_json
    tasks = db.scalars(select(ChapterTask).where(ChapterTask.project_id == item.id)).all()
    currents = {c.chapter_task_id: c for c in db.scalars(select(ModuleContent).where(
        ModuleContent.is_current == True)).all()}  # noqa: E712
    formatter, selected_method = make_project_formatter(item, tasks, currents, [])
    assert formatter.consolidate_methods and selected_method.id == method_task.id
    preview = projects.list_contents(item.id, task.id, None, user, db)["content"]
    assert preview["content"] == saved[0] and preview["evidence"] == json.loads(saved[1])
    assert "正式施测编码字典" not in preview["display_content"]
    assert "参与资格未确认" in preview["display_content"]
    if content_type == "text":
        assert preview["display_content"] == formatter.text(current.content)
        assert "237" in preview["display_content"] and "116" in preview["display_content"]
    else:
        displayed = json.loads(preview["display_content"])
        source = json.loads(current.content)
        assert displayed == formatter.table(source, module_title=task.title)
        assert displayed["rows"] == source["rows"] and displayed["cell_metrics"] == source["cell_metrics"]
    old = projects.list_contents(item.id, task.id, history.id, user, db)["content"]
    assert old["content"] == history.content and "display_content" not in old
    db.expire_all()
    assert (current.content, current.evidence_json) == saved


@pytest.mark.parametrize("invalid", ["missing", "failed", "stale_material", "stale_instruction", "not_current", "incomplete", "missing_preamble"])
def test_invalid_or_incomplete_current_method_cannot_hide_result_disclosure(project, db, invalid):
    item, task, user, method_task, method, _current, _history = setup_current(project, db)
    if invalid == "missing":
        db.delete(method)
    elif invalid == "failed":
        method.status = "failed"
    elif invalid == "stale_material":
        method.material_revision = item.material_revision + 1
    elif invalid == "stale_instruction":
        method_task.instruction_revision += 1
    elif invalid == "not_current":
        method.is_current = False
    elif invalid == "incomplete":
        method.content = METHOD.replace("多选每记录去重", "")
    else:
        db.add(ChapterTask(project_id=item.id, title="题目对应依据", task_type="text", sort=-2))
    db.commit()
    preview = projects.list_contents(item.id, task.id, None, user, db)["content"]
    assert preview["display_content"] == RESULT
    assert preview["content"] == RESULT


def test_stale_current_and_failed_history_are_returned_without_display_rewriting(project, db):
    item, task, user, _method_task, _method, current, history = setup_current(project, db)
    task.instruction_revision += 1
    history.status, history.error_msg = "failed", "原失败原因"
    db.commit()
    preview = projects.list_contents(item.id, task.id, None, user, db)["content"]
    assert preview["is_stale"] and preview["content"] == current.content
    assert "display_content" not in preview
    failed = projects.list_contents(item.id, task.id, history.id, user, db)["content"]
    assert failed["error_msg"] == "原失败原因" and "display_content" not in failed


def test_invalid_legacy_table_stays_inspectable(project, db):
    item, task, user, _method_task, _method, current, _history = setup_current(project, db, "table")
    current.content = "broken table JSON"
    db.commit()
    preview = projects.list_contents(item.id, task.id, None, user, db)["content"]
    assert preview["content"] == "broken table JSON" and "display_content" not in preview
