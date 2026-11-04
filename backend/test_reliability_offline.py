# -*- coding: utf-8 -*-
"""通用可靠性验收：结构检查 + 生成内 AI 复核 + 导出前 AI 审核与修正。"""
import asyncio
import json
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from offline_support import initialize_offline_environment

env = initialize_offline_environment()

from PIL import Image

from app import generate, review, sandbox
from app.db import SessionLocal
from app.models import ChapterTask, ModelConfig, ModuleContent, Project, User


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def new_project(db, user, name):
    project = Project(user_id=user.id, name=name, status="analyzed")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def review_config(db, user):
    db.add(ModelConfig(user_id=user.id, name="离线审核", agent_type="", base_url="http://offline",
                       api_key_enc="unused", model="offline", is_default=True))
    db.commit()


def add_text_content(db, project, content, title="正文模块"):
    task = ChapterTask(project_id=project.id, title=title, task_type="text", status="done",
                       instruction="写一段说明", sort=1)
    db.add(task)
    db.commit()
    row = ModuleContent(chapter_task_id=task.id, content_type="text", content=content,
                        version=1, is_current=True, status="ok",
                        material_revision=project.material_revision,
                        instruction_revision=task.instruction_revision)
    db.add(row)
    db.commit()
    db.refresh(row)
    return task, row


def add_table_content(db, project, title="表格模块"):
    task = ChapterTask(project_id=project.id, title=title, task_type="table", status="done",
                       instruction="生成一张表", sort=2)
    db.add(task)
    db.commit()
    table = {"title": "结果表", "columns": ["选项", "人数"], "rows": [["支持", 2], ["反对", 1]]}
    row = ModuleContent(chapter_task_id=task.id, content_type="table",
                        content=json.dumps(table, ensure_ascii=False),
                        version=1, is_current=True, status="ok",
                        material_revision=project.material_revision,
                        instruction_revision=task.instruction_revision)
    db.add(row)
    db.commit()
    db.refresh(row)
    return task, row, table


def ok_record(content, project, task):
    return json.dumps({
        "status": "ok", "issues": [], "protocol": review.REVIEW_PROTOCOL_VERSION,
        "content_sha256": sha256(content.encode("utf-8")).hexdigest(),
        "material_revision": project.material_revision,
        "instruction_revision": task.instruction_revision, "reviewed_at": 1,
    }, ensure_ascii=False)


async def final_ok(*_args, **_kwargs):
    return {"status": "ok", "issues": [], "repairs": []}


# ---------------------------------------------------------------- 结构检查

def finish_structure_test():
    workdir = sandbox.fresh_workdir(0, 901)
    ok, note, normalized = generate.validate_finish(
        "text", json.dumps({"content": "正" * 80}, ensure_ascii=False), workdir)
    check(ok and json.loads(normalized)["content"] == "正" * 80, "文本 JSON 成果通过结构检查")
    ok, note, _ = generate.validate_finish("text", "太短", workdir)
    check(not ok and "太短" in note, "过短正文被结构检查拒绝")
    ok, note, _ = generate.validate_finish("table", '{"columns": ["a"], "rows": [[1]]}', workdir)
    check(ok, "扁平表格 JSON 通过结构检查")
    ok, note, _ = generate.validate_finish("table", '{"columns": ["a"], "rows": [["x", "y"]]}', workdir)
    check(not ok and "不一致" in note, "行列数量不一致的表格被拒绝")
    ok, note, _ = generate.validate_finish("image", "说明", workdir)
    check(not ok and "chart.png" in note, "缺少图表文件的图片成果被拒绝")
    image = Image.new("RGB", (600, 400), "white")
    for x in range(0, 600, 2):
        for y in range(0, 400, 2):
            image.putpixel((x, y), ((x * 7) % 256, (y * 11) % 256, ((x + y) * 13) % 256))
    image.save(workdir / "out" / "chart.png")
    (workdir / "out" / "chart_data.json").write_text(
        json.dumps({"title": "态度", "labels": ["支持"], "values": [2]}), encoding="utf-8")
    ok, note, _ = generate.validate_finish("image", "说明", workdir)
    check(ok, "图表文件与数据齐备后图片成果通过结构检查")
    (workdir / "out" / "chart_data.json").write_text(
        json.dumps({"title": "态度", "labels": ["支持"], "values": [1, 2]}), encoding="utf-8")
    ok, note, _ = generate.validate_finish("image", "说明", workdir)
    check(not ok, "labels/values 长度不一致的图表数据被拒绝")


# ---------------------------------------------------------------- 生成内复核

def generation_review_test(db, user, monkeypatch_note=""):
    project = new_project(db, user, "生成内复核")
    task = ChapterTask(project_id=project.id, title="正文", task_type="text", status="pending",
                       instruction="写正文", sort=1)
    db.add(task)
    db.commit()
    db.refresh(task)
    state = {"reviews": 0, "finishes": 0}

    def cfg(_db, *_args):
        return SimpleNamespace(id=None, system_prompt="", model="offline")

    original_pick, original_materials = generate.pick_model, generate.build_materials
    original_prepare, original_chat = generate._prepare_workdir, generate.chat_completion
    workdir = sandbox.fresh_workdir(project.id, 902)

    async def fake_chat(_cfg, messages, **_kwargs):
        system = str(messages[0].get("content", ""))
        if "复核一个刚生成的报告模块" in system:
            state["reviews"] += 1
            if state["reviews"] == 1:
                return json.dumps({"status": "issues", "issues": [
                    {"severity": "error", "message": "数字与数据不符"}]}, ensure_ascii=False)
            return json.dumps({"status": "ok", "issues": []}, ensure_ascii=False)
        state["finishes"] += 1
        body = ("修改前正文" if state["finishes"] == 1 else "修改后正文") + "字" * 60
        return json.dumps({"action": "finish",
                           "action_input": json.dumps({"content": body}, ensure_ascii=False)},
                          ensure_ascii=False)

    generate.pick_model = cfg
    generate.build_materials = lambda *_args: ("参考资料", "数据摘要")
    generate._prepare_workdir = lambda *_args, **_kwargs: (workdir, [])
    generate.chat_completion = fake_chat
    try:
        row = asyncio.run(generate.generate_task(db, project, task))
    finally:
        generate.pick_model, generate.build_materials = original_pick, original_materials
        generate._prepare_workdir, generate.chat_completion = original_prepare, original_chat
    check(row.status == "ok" and "修改后正文" in row.content,
          "生成内复核 error 打回后修正提交，任务成功")
    check(state == {"reviews": 2, "finishes": 2}, "生成内复核按每次提交各复核一次")
    check(json.loads(row.review_json)["status"] == "ok", "生成内复核结论被持久化")

    # 审核模型不可用时任务失败并给出原因。
    task2 = ChapterTask(project_id=project.id, title="正文2", task_type="text", status="pending",
                        instruction="写正文", sort=2)
    db.add(task2)
    db.commit()
    db.refresh(task2)

    async def failing_chat(_cfg, messages, **_kwargs):
        system = str(messages[0].get("content", ""))
        if "复核一个刚生成的报告模块" in system:
            raise RuntimeError("审核服务不可用")
        return json.dumps({"action": "finish",
                           "action_input": json.dumps({"content": "正文" + "字" * 80}, ensure_ascii=False)},
                          ensure_ascii=False)

    generate.pick_model = cfg
    generate.build_materials = lambda *_args: ("参考资料", "数据摘要")
    generate._prepare_workdir = lambda *_args, **_kwargs: (workdir, [])
    generate.chat_completion = failing_chat
    try:
        failed = asyncio.run(generate.generate_task(db, project, task2))
    finally:
        generate.pick_model, generate.build_materials = original_pick, original_materials
        generate._prepare_workdir, generate.chat_completion = original_prepare, original_chat
    check(failed.status == "failed" and "审核" in failed.error_msg,
          "审核模型不可用时任务失败并给出原因")


# ---------------------------------------------------------------- 导出前审核

def export_audit_test(db, user):
    project = new_project(db, user, "导出审核")
    review_config(db, user)
    task, row = add_text_content(db, project, "支持者有2人，占66.7%。" + "补充说明" * 20)

    # 1) 未变更模块复用生成时的复核结论：只调用整稿复核。
    row.review_json = ok_record(row.content, project, task)
    db.commit()
    final_calls = []

    async def module_should_not_run(*_args, **_kwargs):
        raise AssertionError("未变更模块不应重复复核")

    async def final_tracking(*_args, **kwargs):
        final_calls.append(kwargs.get("warnings", []))
        return {"status": "ok", "issues": [], "repairs": []}

    original_module, original_final = review._module_review, review._final_review
    review._module_review, review._final_review = module_should_not_run, final_tracking
    try:
        result = asyncio.run(review.review_and_repair(db, project))
    finally:
        review._module_review, review._final_review = original_module, original_final
    check(result["ok"] and len(final_calls) == 1, "未变更模块复用复核结论，只执行整稿复核")

    # 2) 内容变化后重新复核：error + 定向修正 → 自动修正后通过。
    row.content = "支持者有2人，占66.7%。" + "补充说明" * 20 + "（重写）"
    db.commit()
    seen = []

    async def repairing_module(_db, _cfg, _project, task_, row_, *_args, **_kwargs):
        seen.append(row_.content)
        if "支持者有2人" in row_.content:
            return {"status": "issues",
                    "issues": [{"severity": "error", "message": "数字与数据不符"}],
                    "repairs": [{"task_id": task_.id, "type": "text",
                                 "old": "支持者有2人", "new": "支持者有3人", "cell": ""}]}
        return {"status": "ok", "issues": [], "repairs": []}

    review._module_review, review._final_review = repairing_module, final_ok
    try:
        fixed = asyncio.run(review.review_and_repair(db, project))
    finally:
        review._module_review, review._final_review = original_module, original_final
    db.expire_all()
    current = review._current_contents(db, project.id)[task.id]
    check(fixed["ok"] and len(seen) == 2 and "支持者有3人" in current.content,
          "导出审核 error 触发定向修正并复评通过")
    check(current.version == 2, "自动修正创建新版本")

    # 3) 表格单元格修正。
    table_task, table_row, table = add_table_content(db, project)

    async def table_module(_db, _cfg, _project, task_, row_, *_args, **_kwargs):
        if task_.task_type != "table":
            return {"status": "ok", "issues": [], "repairs": []}
        if json.loads(row_.content)["rows"][0][1] == 3:
            return {"status": "ok", "issues": [], "repairs": []}
        return {"status": "issues",
                "issues": [{"severity": "error", "message": "单元格数字有误"}],
                "repairs": [{"task_id": task_.id, "type": "table_cell",
                             "old": "2", "new": "3", "cell": "r0c1"}]}

    review._module_review, review._final_review = table_module, final_ok
    try:
        table_result = asyncio.run(review.review_and_repair(db, project))
    finally:
        review._module_review, review._final_review = original_module, original_final
    db.expire_all()
    table_current = review._current_contents(db, project.id)[table_task.id]
    check(table_result["ok"] and json.loads(table_current.content)["rows"][0][1] == 3,
          "表格单元格定向修正生效并通过复核")

    # 4) error 且无法修正 → 自动重新生成失败时仍拦截，展示 AI 理由。
    blocked_project = new_project(db, user, "导出拦截")
    blocked_task, blocked_row = add_text_content(db, blocked_project, "正文" * 60)
    original_generate_task = generate.generate_task

    async def failed_regenerate(*_args, **_kwargs):
        return SimpleNamespace(status="failed", error_msg="模拟重新生成失败")

    async def unrepaired_module(*_args, **_kwargs):
        return {"status": "issues",
                "issues": [{"severity": "error", "message": "数字无法从数据复算"}],
                "repairs": []}

    review._module_review = unrepaired_module
    generate.generate_task = failed_regenerate
    try:
        blocked = asyncio.run(review.review_and_repair(db, blocked_project))
    finally:
        review._module_review, review._final_review = original_module, original_final
        generate.generate_task = original_generate_task
    check(not blocked["ok"] and blocked["issues"][0]["message"] == "数字无法从数据复算",
          "存在 error 且重新生成仍失败时拦截导出并展示理由")

    # 5) 修正定位不唯一时不得应用，仍拦截。
    ambiguous_project = new_project(db, user, "修正歧义")
    ambiguous_task, ambiguous_row = add_text_content(
        db, ambiguous_project, "原句出现两次：原句。原句。" + "补充" * 30)

    async def ambiguous_module(_args_db, _cfg, _project, task_, row_, *_args, **_kwargs):
        return {"status": "issues",
                "issues": [{"severity": "error", "message": "表述重复"}],
                "repairs": [{"task_id": task_.id, "type": "text",
                             "old": "原句", "new": "改句", "cell": ""}]}

    review._module_review = ambiguous_module
    generate.generate_task = failed_regenerate
    try:
        ambiguous = asyncio.run(review.review_and_repair(db, ambiguous_project))
    finally:
        review._module_review, review._final_review = original_module, original_final
        generate.generate_task = original_generate_task
    db.expire_all()
    ambiguous_current = review._current_contents(db, ambiguous_project.id)[ambiguous_task.id]
    check(not ambiguous["ok"] and "原句出现两次" in ambiguous_current.content,
          "修正定位不唯一时不应用并继续拦截")

    # 6) 仅 warning → 分级放行。
    warning_project = new_project(db, user, "警告放行")
    warning_task, _ = add_text_content(db, warning_project, "正文" * 60)

    async def warning_module(*_args, **_kwargs):
        return {"status": "issues",
                "issues": [{"severity": "warning", "message": "建议精简表述"}],
                "repairs": []}

    review._module_review, review._final_review = warning_module, final_ok
    try:
        warned = asyncio.run(review.review_and_repair(db, warning_project))
    finally:
        review._module_review, review._final_review = original_module, original_final
    check(warned["ok"] and warned["warnings"][0]["message"] == "建议精简表述",
          "仅 warning 时放行并记录提示")

    # 7) 整稿复核 error（能定位到模块）→ 重生成仍失败 → 拦截。
    async def blocked_final(*_args, **_kwargs):
        return {"status": "issues",
                "issues": [{"severity": "error", "message": "两处结论相互矛盾",
                            "task_ids": [warning_task.id]}],
                "repairs": []}

    review._module_review, review._final_review = warning_module, blocked_final
    generate.generate_task = failed_regenerate
    try:
        final_blocked = asyncio.run(review.review_and_repair(db, warning_project))
    finally:
        review._module_review, review._final_review = original_module, original_final
        generate.generate_task = original_generate_task
    check(not final_blocked["ok"] and final_blocked["issues"][0]["message"] == "两处结论相互矛盾",
          "整稿复核 error 拦截导出")

    # 7b) 整稿复核 error 定位不到模块 → 只提示放行。
    async def unattributed_final(*_args, **_kwargs):
        return {"status": "issues",
                "issues": [{"severity": "error", "message": "全文术语需要统一"}],
                "repairs": []}

    review._module_review, review._final_review = warning_module, unattributed_final
    try:
        noted = asyncio.run(review.review_and_repair(db, warning_project))
    finally:
        review._module_review, review._final_review = original_module, original_final
    check(noted["ok"] and any("无法定位到具体模块" in item["message"]
                              for item in noted["warnings"]),
          "整稿复核 error 定位不到模块时只提示不拦截")

    # 7c) 模块复核未完成（模型异常）→ 记提示放行，不触发重新生成。
    unavailable_project = new_project(db, user, "复核未完成")
    add_text_content(db, unavailable_project, "正文" * 60)

    async def unavailable_module(*_args, **_kwargs):
        raise RuntimeError("审核服务不可用")

    regen_calls = []

    async def tracking_regenerate(*_args, **_kwargs):
        regen_calls.append(True)
        return SimpleNamespace(status="failed", error_msg="不应被调用")

    review._module_review, review._final_review = unavailable_module, final_ok
    generate.generate_task = tracking_regenerate
    try:
        unavailable = asyncio.run(review.review_and_repair(db, unavailable_project))
    finally:
        review._module_review, review._final_review = original_module, original_final
        generate.generate_task = original_generate_task
    check(unavailable["ok"] and not regen_calls
          and "复核未完成（已放行）" in unavailable["warnings"][0]["message"],
          "模块复核未完成时记提示放行且不触发生成")

    # 8) 审核模型缺失 → 明确拦截提示。
    bare_project = new_project(db, user, "无审核模型")
    add_text_content(db, bare_project, "正文" * 60)
    check_user = db.get(User, user.id)
    db.query(ModelConfig).filter(ModelConfig.user_id == user.id).delete()
    db.commit()
    try:
        no_model = asyncio.run(review.review_and_repair(db, bare_project))
    finally:
        review_config(db, check_user)
    check(not no_model["ok"] and "未配置审核模型" in no_model["issues"][0]["message"],
          "未配置审核模型时拦截并提示")


def review_protocol_test():
    good = review._normalize_review_payload(json.dumps(
        {"status": "issues", "issues": [{"severity": "warning", "message": "x"}]},
        ensure_ascii=False))
    check(good["issues"] == [{"severity": "warning", "message": "x"}], "合法审核协议被接受")
    check(review._normalize_review_payload(json.dumps({"status": "ok"}))["issues"] == [],
          "无问题的 ok 协议被接受")
    for bad in ("不是 JSON", json.dumps({"status": "unknown"}),
                json.dumps({"status": "issues", "issues": [{"message": "缺少 severity"}]})):
        try:
            review._normalize_review_payload(bad)
        except review.ReviewError:
            continue
        check(False, f"非法审核协议应被拒绝：{bad[:40]}")
    tolerant = review._normalize_review_payload(json.dumps(
        {"status": "issues", "issues": [{"severity": "warning", "message": "x"}],
         "repairs": [{"task_id": 1, "type": "text", "old": "", "new": "y"}]}))
    check(tolerant["repairs"] == [], "形状不完整的修正项被丢弃而不影响问题清单")
    check("严重" in review.format_issues([{"severity": "error", "title": "T", "message": "M"}]),
          "审核问题可整理为可读文本")
    strict = review._normalize_review_payload(json.dumps(
        {"status": "issues", "issues": [{"severity": "error", "task_ids": [7],
                                         "message": "跨模块冲突"}]}, ensure_ascii=False),
        module_ids={7})
    check(strict["issues"][0]["task_ids"] == [7], "整稿复核 error 的模块编号被保留")
    try:
        review._normalize_review_payload(json.dumps(
            {"status": "issues", "issues": [{"severity": "error", "message": "无编号"}]},
            ensure_ascii=False), module_ids={7})
    except review.ReviewError:
        check(True, "整稿复核 error 缺少模块编号被拒绝")
    else:
        check(False, "整稿复核 error 缺少模块编号被拒绝")


def main():
    db = SessionLocal()
    user = db.get(User, env.user_id)
    try:
        finish_structure_test()
        review_protocol_test()
        generation_review_test(db, user)
        export_audit_test(db, user)
    finally:
        db.close()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        env.close()
