# -*- coding: utf-8 -*-
"""离线验证第二步：沙箱、表格/图片代码契约、版本管理、插入/删除。不调用模型。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from offline_support import install_offline_environment
env = install_offline_environment()

from fastapi import BackgroundTasks, HTTPException

from app import generate, sandbox, storage
from app.db import SessionLocal
from app.generate import (
    _next_version,
    _set_category_ticks,
    _wrap_chart_label,
    render_verified_chart,
    sanitize_prose,
    sanitize_table_json,
    validate_finish,
)
from app.models import ChapterTask, ModuleContent, Project, ProjectFile, User
from app.routers.projects import (
    InsertIn,
    RollbackIn,
    delete_task,
    insert_task,
    list_contents,
    rollback_content,
)

XLSX = env.xlsx


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def main():
    # 1) 沙箱基本能力
    wd = sandbox.fresh_workdir(0, 1)
    r = sandbox.run_python("print('hello')", wd)
    check(r["ok"] and "hello" in r["stdout"], "沙箱执行简单代码")
    r = sandbox.run_python("import time\ntime.sleep(30)", wd, timeout=2)
    check(not r["ok"] and r["timeout"], "沙箱超时被终止")
    r = sandbox.run_python("import os\nprint(sorted(os.environ.keys()))", wd)
    check("MYSQL_PASSWORD" not in r["stdout"] and "MODEL_CONFIG_ENCRYPTION_KEY" not in r["stdout"],
          "沙箱环境变量不泄露服务端配置")

    # 2) 表格代码契约：真实数据 → JSON
    (wd / "data" / XLSX.name).write_bytes(XLSX.read_bytes())
    code = (
        "import pandas as pd, json\n"
        f"df = pd.read_excel('./data/{XLSX.name}')\n"
        "vc = df['问卷编码'].value_counts()\n"
        "print(json.dumps({'title': '问卷提交量', 'columns': ['问卷编码', '提交量'], "
        "'rows': [[k, int(v)] for k, v in vc.items()]}, ensure_ascii=False))\n"
    )
    r = sandbox.run_python(code, wd)
    check(r["ok"], f"表格统计代码执行成功 {r['stderr'][:200]}")
    table = json.loads(r["stdout"].strip().splitlines()[-1])
    check(table["columns"] == ["问卷编码", "提交量"], "表格 JSON 解析成功")
    total = sum(int(row[1]) for row in table["rows"])
    check(total == 1066, f"统计结果与合成数据预设值一致（合计 {total}）")

    # 3) 图片代码契约：matplotlib 中文图表
    chart_code = (
        "import matplotlib\nmatplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.rcParams['font.sans-serif'] = ['Hiragino Sans GB', 'PingFang SC', 'Arial Unicode MS', 'SimHei']\n"
        "plt.rcParams['axes.unicode_minus'] = False\n"
        "import pandas as pd\n"
        f"df = pd.read_excel('./data/{XLSX.name}')\n"
        "vc = df['问卷编码'].value_counts()\n"
        "fig, ax = plt.subplots(figsize=(8, 4))\n"
        "ax.bar(['教育主管', '教师', '小学生', '中学生', '大学生'], vc.values)\n"
        "ax.set_title('各问卷类型提交量')\n"
        "fig.savefig('./out/chart.png', dpi=150)\n"
    )
    r = sandbox.run_python(chart_code, wd)
    chart = wd / "out" / "chart.png"
    check(r["ok"] and chart.exists() and chart.stat().st_size > 5000,
          f"图表代码生成 PNG（{chart.stat().st_size if chart.exists() else 0} 字节）{r['stderr'][:200]}")
    (env.root / "sandbox_chart_test.png").write_bytes(chart.read_bytes())
    verified_chart = wd / "out" / "verified_chart.png"
    render_verified_chart(
        {
            "title": "已验证提交量",
            "labels": ["教育主管", "教师", "小学生"],
            "values": [453, 336, 151],
        },
        "【生成图片（柱状图）：问卷提交量】",
        verified_chart,
    )
    check(verified_chart.exists() and verified_chart.stat().st_size > 5000,
          "最终图表由已验证 labels/values JSON 重绘")

    # 3b) 长中文分类标签：换行并只设置一组横轴刻度，避免视觉重叠
    long_labels = [
        "本科文化、红色故事宣讲活动",
        "思政专题讲座、志愿服务课程",
        "主题班会与实践教育活动",
        "校内外实践教学活动",
        "网络思政教育活动",
        "其他形式活动",
        "未参加过相关活动",
    ]
    wrapped = [_wrap_chart_label(label) for label in long_labels]
    check(any("\n" in label for label in wrapped), "长中文分类标签自动换行")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    display_labels, rotation = _set_category_ticks(ax, long_labels)
    check(
        display_labels == wrapped
        and len(ax.get_xticklabels()) == len(long_labels)
        and rotation > 0,
        "横轴标签只设置一次且数量与分类一致",
    )
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    extents = [tick.get_window_extent(renderer) for tick in ax.get_xticklabels()]
    check(all(extent.height > 0 and extent.width > 0 for extent in extents),
          "换行后的横轴标签具有有效绘制区域")
    plt.close(fig)

    long_chart = wd / "out" / "verified_long_labels.png"
    render_verified_chart(
        {
            "title": "教师问卷活动参与情况",
            "labels": long_labels,
            "values": [65.8, 46.7, 39.9, 35.4, 28.3, 23.8, 7.7],
        },
        "【生成图片（柱状图）：教师问卷活动参与情况】",
        long_chart,
    )
    check(long_chart.exists() and long_chart.stat().st_size > 5000,
          "长中文标签最终图表生成成功")

    # 4) 版本管理与模块编辑（临时 SQLite、自建用户）
    db = SessionLocal()
    user = db.get(User, env.user_id)
    project = Project(user_id=user.id, name="离线验证-第二步", status="analyzed")
    db.add(project)
    db.commit()
    db.refresh(project)
    pid = project.id
    t1 = ChapterTask(project_id=pid, parent_id=None, level=2, title="任务A", sort=1,
                     task_type="text", instruction="【填写：A】")
    t2 = ChapterTask(project_id=pid, parent_id=None, level=2, title="任务B", sort=2,
                     task_type="text", instruction="【填写：B】")
    t3 = ChapterTask(project_id=pid, parent_id=None, level=2, title="任务C", sort=3,
                     task_type="text", instruction="【填写：C】")
    db.add_all([t1, t2, t3])
    db.commit()
    for t in (t1, t2, t3):
        db.refresh(t)

    # 两个版本 + 回退
    c1 = ModuleContent(chapter_task_id=t1.id, version=1, content_type="text",
                       content="版本一内容", status="ok", is_current=False)
    c2 = ModuleContent(chapter_task_id=t1.id, version=2, content_type="text",
                       content="版本二内容", status="ok", is_current=True)
    db.add_all([c1, c2])
    db.commit()
    db.refresh(c1)
    rollback_content(pid, t1.id, RollbackIn(content_id=c1.id), user, db)
    db.refresh(c1)
    check(c1.is_current, "回退后 v1 成为当前版本")
    out = list_contents(pid, t1.id, None, user, db)
    check(len(out["versions"]) == 2 and out["content"]["content"] == "版本一内容",
          "版本列表与当前内容正确")

    # 插入模块：sort 顺延
    bg = BackgroundTasks()
    res = asyncio.run(insert_task(pid, InsertIn(after_task_id=t1.id, requirement="生成表格“测试表”：行列说明"), bg, user, db))
    new_id = res["task_id"]
    rows = db.query(ChapterTask).filter(ChapterTask.project_id == pid).order_by(ChapterTask.sort).all()
    titles = [(r.title, r.sort, r.task_type) for r in rows]
    check([t[0] for t in titles] == ["任务A", "生成表格“测试表”：行列说明", "任务B", "任务C"],
          f"插入位置正确 {titles}")
    new_task = db.get(ChapterTask, new_id)
    check(new_task.task_type == "table", "插入模块按需求描述识别为表格任务")

    # The insertion owns the project until its background run finishes.
    try:
        delete_task(pid, new_id, user, db)
    except HTTPException as exc:
        check(exc.status_code == 409, "生成期间删除模块被项目互斥拒绝")
    else:
        check(False, "生成期间不应删除模块")
    original_generate = generate.generate_task

    async def fake_generate(task_db, task_project, task, user_prompt="", run_token=None):
        task.status = "done"
        task_db.commit()

    generate.generate_task = fake_generate
    try:
        asyncio.run(bg())
    finally:
        generate.generate_task = original_generate
    db.expire_all()
    check(db.get(Project, pid).operation_token == "", "后台任务结束后释放项目操作锁")

    # 删除级联
    res = delete_task(pid, new_id, user, db)
    check(res["deleted"] == 1, "删除插入的模块")
    res = delete_task(pid, t1.id, user, db)
    check(res["deleted"] == 1, "删除任务A")
    check(db.query(ModuleContent).filter(ModuleContent.chapter_task_id == t1.id).count() == 0,
          "任务A的生成内容级联删除")

    # 清理临时项目
    for t in (t2, t3):
        delete_task(pid, t.id, user, db)
    db.query(Project).filter(Project.id == pid).delete()
    db.commit()
    db.close()

    # 生成内容兜底清洗：删除编辑性元语句分句
    dirty_text = (
        "需要说明的是，本报告覆盖五类样本，不呈现社会人员相关数据；"
        "原表述中的旧称“中小学思想政治一体化建设”在本报告中统一规范为“大中小学思想政治教育一体化建设”。"
        "大学生样本仅4份，相关结论应谨慎解读，不宜据此推断总体情况。"
    )
    cleaned = sanitize_prose(dirty_text)
    check("旧称" not in cleaned and "统一规范为" not in cleaned, "正文元语句分句已删除")
    check("不呈现社会人员相关数据" in cleaned and "谨慎解读" in cleaned, "正常分句保留")
    # 不含元语句的正文原样保留
    plain = "本报告围绕课程体系与学段衔接展开分析，样本覆盖五类群体，数据真实可靠。"
    check(sanitize_prose(plain) == plain, "干净正文不受影响")
    # 清洗后过短时保留原文（防误伤）
    short_dirty = "旧称“X”统一规范为“Y”。"
    check(sanitize_prose(short_dirty) == short_dirty, "清洗后过短保留原文")
    # 表格 JSON 清洗
    tbl = json.dumps({
        "title": "样本构成表（旧称已统一规范为现名）",
        "columns": ["群体", "占比"],
        "rows": [["教育主管部门", "42.5%"], ["教师", "31.5%"]],
    }, ensure_ascii=False)
    tbl_out = json.loads(sanitize_table_json(tbl))
    check("旧称" not in tbl_out["title"] and "统一规范为" not in tbl_out["title"], "表格标题清洗")
    check(tbl_out["rows"][0][0] == "教育主管部门", "表格数据单元格不受影响")
    check(sanitize_table_json("不是JSON") == "不是JSON", "非法 JSON 原样返回")

    # finish 结构检查：文本只做最小长度与形状检查，内容正确性由 AI 复核判断。
    clean = "本次调查共回收有效问卷1066份，覆盖五类群体，样本结构合理。" * 5
    ok, _, normalized = validate_finish("text", clean, None)
    check(ok and json.loads(normalized)["content"] == clean, "原生长正文通过结构检查")
    ok, _, normalized = validate_finish(
        "text", json.dumps({"content": clean}, ensure_ascii=False), None)
    check(ok and json.loads(normalized)["content"] == clean, "JSON 成果正文通过结构检查")
    ok, note, _ = validate_finish("text", "太短", None)
    check(not ok and "太短" in note, "过短正文仍被拒绝")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        env.close()
