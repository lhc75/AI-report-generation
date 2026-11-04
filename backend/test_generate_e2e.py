# -*- coding: utf-8 -*-
"""端到端验证第二步：克隆项目1的已分析任务到 user3 的测试项目，
用真实模型各生成一个 文字/表格/图片 任务。运行后保留项目便于前端查看。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select

from app import storage
from app.db import SessionLocal
from app.generate import generate_task
from app.models import ChapterTask, Project, ProjectFile

SRC_PID = 1       # 已分析完成的来源项目
USER_ID = 3       # 有可用模型配置的用户


def clone_project(db) -> Project:
    src = db.get(Project, SRC_PID)
    assert src is not None, "来源项目不存在"
    project = Project(user_id=USER_ID, name="端到端验证-章节生成", status="analyzed",
                      parsed_template_path=src.parsed_template_path)
    db.add(project)
    db.commit()
    db.refresh(project)
    for f in db.scalars(select(ProjectFile).where(ProjectFile.project_id == SRC_PID)).all():
        data = storage.download_bytes(f.minio_path)
        path = f"{USER_ID}/{project.id}/upload/{f.file_type}_{f.file_name}"
        storage.upload_bytes(path, data)
        db.add(ProjectFile(project_id=project.id, file_type=f.file_type,
                           file_name=f.file_name, minio_path=path, size=f.size))
    id_map = {}
    rows = db.scalars(
        select(ChapterTask).where(ChapterTask.project_id == SRC_PID).order_by(ChapterTask.sort)
    ).all()
    for t in rows:
        nt = ChapterTask(
            project_id=project.id,
            parent_id=id_map.get(t.parent_id),
            level=t.level, title=t.title, sort=t.sort,
            task_type=t.task_type, instruction=t.instruction, status="pending",
        )
        db.add(nt)
        db.flush()
        id_map[t.id] = nt.id
    db.commit()
    return project


async def main():
    db = SessionLocal()
    project = clone_project(db)
    print(f"project {project.id} cloned from {SRC_PID}")
    tasks = db.scalars(
        select(ChapterTask)
        .where(ChapterTask.project_id == project.id, ChapterTask.task_type != "heading")
        .order_by(ChapterTask.sort)
    ).all()
    picked = []
    for kind in ("text", "table", "image"):
        t = next((x for x in tasks if x.task_type == kind), None)
        if t:
            picked.append(t)
    assert len(picked) == 3, "三种类型的任务都要选到"
    print("picked:", [(t.id, t.task_type, t.title[:40]) for t in picked])
    results = []
    for t in picked:
        print(f"--- generating task {t.id} ({t.task_type}) ...", flush=True)
        row = await generate_task(db, project, t)
        results.append((t, row))
        print(f"    status={row.status} version={row.version} err={row.error_msg[:120]}", flush=True)

    for t, row in results:
        print(f"\n===== task {t.id} [{t.task_type}] {t.title[:50]}")
        if row.status != "ok":
            print("FAILED:", row.error_msg[:300])
            continue
        if row.content_type == "text":
            print(row.content[:400])
        elif row.content_type == "table":
            obj = json.loads(row.content)
            print("columns:", obj.get("columns"))
            for r in obj.get("rows", [])[:5]:
                print("  ", r)
        elif row.content_type == "image":
            data = storage.download_bytes(row.image_path)
            out = Path(f"/tmp/e2e_chart_task{t.id}.png")
            out.write_bytes(data)
            print("image saved:", out, len(data), "bytes")
    db.close()
    fails = [t.id for t, r in results if r.status != "ok"]
    print("\nDONE, failed tasks:", fails or "无")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
