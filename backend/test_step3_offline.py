# -*- coding: utf-8 -*-
"""离线验证第三步：增量合并分析、材料缓存、ReAct 循环（mock 模型）、全部生成并发。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from offline_support import install_offline_environment
env = install_offline_environment()

from app import generate, llm, sandbox, template_parse
from app.config import settings
from app.db import SessionLocal
from app.models import ChapterTask, ModuleContent, Project, ProjectFile, User
from app.routers import projects as proj_router


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def make_task(tmp_id, parent_tmp, level, title, sort, task_type, instruction):
    return {
        "tmp_id": tmp_id, "parent_tmp": parent_tmp, "level": level, "title": title,
        "sort": sort, "task_type": task_type, "instruction": instruction,
    }


def test_merge_save_tasks(db, user):
    project = Project(user_id=user.id, name="离线验证-增量合并", status="analyzed")
    db.add(project)
    db.commit()
    db.refresh(project)
    pid = project.id

    first = [
        make_task(1, None, 1, "第一部分", 1, "heading", ""),
        make_task(2, 1, 2, "模块甲", 2, "text", "【填写：甲】"),
        make_task(3, 1, 2, "模块乙", 3, "text", "【填写：乙】"),
        make_task(4, 1, 2, "模块丙", 4, "table", "【生成表格“丙”：x】"),
    ]
    template_parse.assign_slot_keys(first)
    proj_router.save_tasks(db, pid, first)
    db.commit()
    rows = db.query(ChapterTask).filter(ChapterTask.project_id == pid).order_by(ChapterTask.sort).all()
    check([r.title for r in rows] == ["第一部分", "模块甲", "模块乙", "模块丙"], "首次拆解入库")
    jia = next(r for r in rows if r.title == "模块甲")
    yi = next(r for r in rows if r.title == "模块乙")
    bing = next(r for r in rows if r.title == "模块丙")
    # 模块甲已有生成内容且状态 done
    jia.status = "done"
    db.add(ModuleContent(chapter_task_id=jia.id, version=1, content_type="text",
                         content="已生成内容", status="ok", is_current=True))
    db.commit()

    # 再次分析：甲不变、乙指令变化、丙被删除、新增模块丁
    second = [
        make_task(10, None, 1, "第一部分", 1, "heading", ""),
        make_task(20, 10, 2, "模块甲", 2, "text", "【填写：甲】"),
        make_task(30, 10, 2, "模块乙", 3, "text", "【填写：乙（新版指令）】"),
        make_task(40, 10, 2, "模块丁", 4, "text", "【填写：丁】"),
    ]
    template_parse.assign_slot_keys(second)
    proj_router.save_tasks(db, pid, second)
    db.commit()
    rows = db.query(ChapterTask).filter(ChapterTask.project_id == pid).order_by(ChapterTask.sort).all()
    check([r.title for r in rows] == ["第一部分", "模块甲", "模块乙", "模块丁"],
          f"再次分析后结构正确 {[r.title for r in rows]}")
    jia2 = next(r for r in rows if r.title == "模块甲")
    yi2 = next(r for r in rows if r.title == "模块乙")
    check(jia2.id == jia.id, "指令未变的模块保留原行（不推倒重来）")
    check(jia2.status == "done", "已完成模块状态保留")
    check(db.query(ModuleContent).filter(ModuleContent.chapter_task_id == jia.id).count() == 1,
          "已生成内容在再次分析后保留")
    check(yi2.id == yi.id and yi2.instruction == "【填写：乙（新版指令）】", "指令变化更新到原行")
    check(yi2.status == "pending", "指令变化的已完成模块标回待生成")
    check(yi2.slot_key == yi.slot_key, "指令变化但模板位置不变时 slot_key 保持稳定")
    check(db.get(ChapterTask, bing.id) is None, "消失的模块被删除")
    check(db.query(ModuleContent).filter(ModuleContent.chapter_task_id == bing.id).count() == 0,
          "消失模块的内容级联删除")
    ding = next(r for r in rows if r.title == "模块丁")
    check(ding.parent_id == jia2.parent_id and ding.status == "pending", "新增模块挂载正确")

    proj_router.delete_tasks_cascade(db, [r.id for r in rows])
    db.query(Project).filter(Project.id == pid).delete()
    db.commit()


def test_materials_cache(db, user):
    project = Project(user_id=user.id, name="离线验证-材料缓存", status="analyzed")
    db.add(project)
    db.commit()
    db.refresh(project)
    pid = project.id
    reference = env.reference.read_bytes()
    data = "分组,人数\n教师,10\n学生,5\n".encode("utf-8")
    generate.storage.upload_bytes("fake/a.docx", reference)
    generate.storage.upload_bytes("fake/b.csv", data)
    db.add(ProjectFile(project_id=pid, file_type="reference", file_name="a.docx",
                       minio_path="fake/a.docx", size=len(reference)))
    db.add(ProjectFile(project_id=pid, file_type="data", file_name="b.csv",
                       minio_path="fake/b.csv", size=len(data)))
    db.commit()

    calls = {"n": 0}
    orig = generate.storage.download_bytes

    def counting(path):
        calls["n"] += 1
        return orig(path)

    generate.storage.download_bytes = counting
    try:
        generate.invalidate_materials(pid)
        generate.build_materials(db, project)
        first_calls = calls["n"]
        check(first_calls == 2, f"首次构建摘要下载了全部文件（{first_calls}）")
        generate.build_materials(db, project)
        generate.build_materials(db, project)
        check(calls["n"] == first_calls, "文件未变时命中缓存，不重复下载")
        generate.invalidate_materials(pid)
        generate.build_materials(db, project)
        check(calls["n"] == first_calls + 2, "缓存失效后重新下载")
    finally:
        generate.storage.download_bytes = orig
        generate.invalidate_materials(pid)

    db.query(ProjectFile).filter(ProjectFile.project_id == pid).delete()
    db.query(Project).filter(Project.id == pid).delete()
    db.commit()


def test_react_loop():
    wd = sandbox.fresh_workdir(0, 999)
    (wd / "data" / "d.csv").write_text("group,cnt\n教师,10\n小学生,5\n", encoding="utf-8")

    replies = iter([
        "我先看看数据",  # 非法输出 → 观察格式错误
        json.dumps({"thought": "先统计", "action": "run_python",
                    "action_input": "import pandas as pd, json\n"
                                    "df = pd.read_csv('./data/d.csv')\n"
                                    "print(json.dumps({'title': 't', 'columns': ['群体', '数'], "
                                    "'rows': df.values.tolist()}, ensure_ascii=False))"}),
        json.dumps({"thought": "统计完成", "action": "finish",
                    "action_input": json.dumps(
                        {"title": "t", "columns": ["群体", "数"],
                         "rows": [["教师", 10], ["小学生", 5]]}, ensure_ascii=False)}),
    ])
    seen_obs = []
    orig_chat = generate.chat_completion

    async def fake_chat(cfg, messages, timeout=600.0):
        for m in messages:
            if m["role"] == "user" and m["content"].startswith("【观察】"):
                seen_obs.append(m["content"])
        return next(replies)

    generate.chat_completion = fake_chat
    try:
        cfg = generate.ModelConfig(user_id=0, name="fake", base_url="http://x", model="m")
        out = asyncio.run(generate.react_run(None, cfg, "目标提示词", wd, "table"))
    finally:
        generate.chat_completion = orig_chat
    obj = json.loads(out)
    check(obj["columns"] == ["群体", "数"] and len(obj["rows"]) == 2, "ReAct 循环产出规范表格 JSON")
    check(any("格式错误" in o for o in seen_obs), "非法输出收到格式错误观察")
    check(any("教师" in o for o in seen_obs), "run_python 执行结果作为观察反馈")


def test_chat_completion_reasoning_fallback():
    reasoning = json.dumps({
        "thought": "只在 reasoning 字段里返回最终动作",
        "action": "finish",
        "action_input": "ok",
    }, ensure_ascii=False)
    half = len(reasoning) // 2
    sse = (
        b"data: " + json.dumps(
            {"choices": [{"delta": {"reasoning_content": reasoning[:half]}}]},
            ensure_ascii=False,
        ).encode() + b"\n\n"
        + b"data: " + json.dumps(
            {"choices": [{"delta": {"reasoning_content": reasoning[half:]}}]},
            ensure_ascii=False,
        ).encode() + b"\n\n"
        + b"data: [DONE]\n\n"
    )
    # 故意在某个多字节字符中间切断，验证按字节拼帧不会因解码错位丢数据
    cut = next(i + 1 for i, byte in enumerate(sse) if 0xC0 <= byte <= 0xF4)
    fragments = [sse[:cut], sse[cut:]]

    class FakeStreamResponse:
        status_code = 200

        async def aiter_bytes(self):
            for fragment in fragments:
                yield fragment

    class FakeStream:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        def __init__(self, timeout=None, verify=True, trust_env=True):
            self.timeout = timeout
            self.verify = verify
            self.trust_env = trust_env

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, json=None, headers=None):
            return FakeStream()

    orig_client = llm.httpx.AsyncClient
    orig_decrypt = llm.decrypt_key
    llm.httpx.AsyncClient = FakeClient
    llm.decrypt_key = lambda _value: "token"
    try:
        cfg = generate.ModelConfig(
            user_id=0,
            name="fake",
            base_url="http://x",
            model="m",
            api_key_enc="enc",
        )
        reply = asyncio.run(llm.chat_completion(cfg, [{"role": "user", "content": "ping"}], timeout=1))
    finally:
        llm.httpx.AsyncClient = orig_client
        llm.decrypt_key = orig_decrypt
    obj = json.loads(reply)
    check(obj["action"] == "finish" and obj["action_input"] == "ok",
          "reasoning_content 中的有效回复不会被吞掉")


def test_react_stall_guards():
    wd = sandbox.fresh_workdir(0, 997)
    cfg = generate.ModelConfig(
        user_id=0,
        name="fake",
        base_url="http://x",
        model="m",
        api_key_enc="enc",
    )
    original_chat = generate.chat_completion

    async def fake_empty_chat(_cfg, _messages, timeout=600.0):
        return ""

    generate.chat_completion = fake_empty_chat
    try:
        try:
            asyncio.run(generate.react_run(None, cfg, "目标提示词", wd, "review"))
        except ValueError as exc:
            check("空回复" in str(exc) and "连续 3 轮" in str(exc),
                  "连续空回复会尽早失败并提示原因")
        else:
            check(False, "连续空回复应触发失败")
    finally:
        generate.chat_completion = original_chat

    replies = iter(["我先看看数据", "还是先看数据", "再等等"])

    async def fake_text_chat(_cfg, _messages, timeout=600.0):
        return next(replies)

    generate.chat_completion = fake_text_chat
    try:
        try:
            asyncio.run(generate.react_run(None, cfg, "目标提示词", wd, "review"))
        except ValueError as exc:
            check("没有可解析动作" in str(exc) and "连续 3 轮" in str(exc),
                  "连续无 JSON 动作会尽早失败并提示原因")
        else:
            check(False, "连续无 JSON 动作应触发失败")
    finally:
        generate.chat_completion = original_chat

    repeated = json.dumps({
        "thought": "反复探索",
        "action": "run_python",
        "action_input": "print('same')",
    }, ensure_ascii=False)

    async def fake_repeat_chat(_cfg, _messages, timeout=600.0):
        return repeated

    generate.chat_completion = fake_repeat_chat
    try:
        try:
            asyncio.run(generate.react_run(None, cfg, "目标提示词", wd, "review"))
        except ValueError as exc:
            check("重复 run_python" in str(exc) and "代码摘要" in str(exc),
                  "重复 run_python 会提前失败并带代码摘要")
        else:
            check(False, "重复 run_python 应触发失败")
    finally:
        generate.chat_completion = original_chat

    failure_counter = {"n": 0}

    async def fake_failure_chat(_cfg, _messages, timeout=600.0):
        failure_counter["n"] += 1
        return json.dumps({
            "thought": "继续执行错误代码",
            "action": "run_python",
            "action_input": f"raise RuntimeError('bad-{failure_counter['n']}')",
        }, ensure_ascii=False)

    generate.chat_completion = fake_failure_chat
    try:
        try:
            asyncio.run(generate.react_run(None, cfg, "目标提示词", wd, "review"))
        except ValueError as exc:
            # 失败阈值已配置化（REACT_MAX_RUN_FAILURES），断言跟随当前设置
            threshold = generate.settings.REACT_MAX_RUN_FAILURES
            check(f"连续 {threshold} 次 run_python 执行失败" in str(exc)
                  and "最后动作：run_python" in str(exc),
                  "连续 run_python 失败会提前失败并带最后动作")
        else:
            check(False, "连续 run_python 失败应触发失败")
    finally:
        generate.chat_completion = original_chat

    success_counter = {"n": 0}

    async def fake_never_finish_chat(_cfg, _messages, timeout=600.0):
        success_counter["n"] += 1
        return json.dumps({
            "thought": "只探索不提交",
            "action": "run_python",
            "action_input": f"print({success_counter['n']})",
        }, ensure_ascii=False)

    generate.chat_completion = fake_never_finish_chat
    try:
        try:
            asyncio.run(generate.react_run(None, cfg, "目标提示词", wd, "review"))
        except ValueError as exc:
            check("最后动作：run_python" in str(exc) and "观察" in str(exc),
                  "超轮次未 finish 时错误信息包含最后动作和观察")
        else:
            check(False, "只 run_python 不 finish 应触发超轮次失败")
    finally:
        generate.chat_completion = original_chat


def test_react_finish_validation():
    wd = sandbox.fresh_workdir(0, 998)
    ok, note, _ = generate.validate_finish("text", "太短", wd)
    check(not ok and "太短" in note, "文字成果过短被拒绝")
    ok, _, norm = generate.validate_finish("table", '{"columns": ["a"], "rows": [[1]]}', wd)
    check(ok and json.loads(norm)["rows"] == [[1]], "表格成果校验通过并规范化")
    ok, note, _ = generate.validate_finish("image", "说明", wd)
    check(not ok and "chart.png" in note, "未生成图片时 finish 被拒绝")
    (wd / "out").mkdir(exist_ok=True)
    (wd / "out" / "chart.png").write_bytes(env.chart.read_bytes())
    (wd / "out" / "chart_data.json").write_text(
        json.dumps({"title": "示意", "labels": ["A", "B"], "values": [1, 2]}, ensure_ascii=False),
        encoding="utf-8",
    )
    ok, _, _ = generate.validate_finish("image", "说明", wd)
    check(ok, "图片与图表数据齐备后 finish 通过")


def test_generate_all_concurrency(db, user):
    project = Project(user_id=user.id, name="离线验证-全部生成并发", status="analyzed")
    db.add(project)
    db.commit()
    db.refresh(project)
    pid = project.id
    tasks = [
        ChapterTask(project_id=pid, level=1, title="并发模块一", sort=1,
                    task_type="text", instruction="【填写：一】", status="failed"),
        ChapterTask(project_id=pid, level=1, title="并发模块二", sort=2,
                    task_type="text", instruction="【填写：二】", status="pending"),
        ChapterTask(project_id=pid, level=1, title="并发模块三", sort=3,
                    task_type="text", instruction="【填写：三】", status="failed"),
        ChapterTask(project_id=pid, level=1, title="已完成模块", sort=4,
                    task_type="text", instruction="【填写：四】", status="done"),
    ]
    db.add_all(tasks)
    db.commit()
    runnable_ids = {t.id for t in tasks[:3]}
    done_id = tasks[3].id

    running = 0
    max_running = 0
    entry_statuses = {}
    orig_generate = generate.generate_task

    async def fake_generate(task_db, task_project, task, user_prompt="", run_token=None):
        nonlocal running, max_running
        entry_statuses[task.id] = task.status
        task.status = "generating"
        task_db.commit()
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.05)
        running -= 1
        task.status = "done"
        task_db.commit()

    generate.generate_task = fake_generate
    try:
        asyncio.run(proj_router.run_generate_all(pid, user.id))
    finally:
        generate.generate_task = orig_generate

    verify_db = SessionLocal()
    try:
        expected = min(settings.GENERATE_CONCURRENCY, len(runnable_ids))
        check(max_running == expected, f"全部生成按并发上限执行（峰值 {max_running}/{expected}）")
        check(set(entry_statuses) == runnable_ids, "全部生成只调度待生成/失败任务")
        check(all(entry_statuses[tid] == "queued" for tid in runnable_ids),
              "待生成/失败任务开始前排为 queued")
        check(all(verify_db.get(ChapterTask, tid).status == "done" for tid in runnable_ids),
              "批量生成任务全部完成")
        check(verify_db.get(ChapterTask, done_id).status == "done", "已完成模块未被重复生成")
        check(verify_db.get(Project, pid).status == "analyzed", "批量生成结束后项目恢复已分析")
    finally:
        verify_db.close()

    db.rollback()
    proj_router.delete_tasks_cascade(db, [t.id for t in tasks])
    db.query(Project).filter(Project.id == pid).delete()
    db.commit()


def test_semaphore():
    sem = proj_router.gen_semaphore()
    check(sem._value == settings.GENERATE_CONCURRENCY,
          f"生成并发上限 = {settings.GENERATE_CONCURRENCY}")


def main():
    db = SessionLocal()
    user = db.get(User, env.user_id)
    try:
        test_merge_save_tasks(db, user)
        test_materials_cache(db, user)
        test_generate_all_concurrency(db, user)
    finally:
        db.close()
    test_react_loop()
    test_chat_completion_reasoning_fallback()
    test_react_stall_guards()
    test_react_finish_validation()
    test_semaphore()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        env.close()
