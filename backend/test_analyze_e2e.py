# -*- coding: utf-8 -*-
"""端到端验证：真实模版+问卷+数据 → run_analysis → 检查带红字模版和章节任务。
直接用 user_id=3 的 parse 模型配置调用真实模型。运行后保留测试项目便于前端查看。
"""
import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from docx import Document
from docx.oxml.ns import qn

from app import storage
from app.db import SessionLocal
from app.models import ChapterTask, Project, ProjectFile
from app.routers.projects import run_analysis

BASE = Path("/Users/mac/Downloads/※44-调研问卷与访谈提纲、观测要点")
USER_ID = 3

FILES = [
    ("template", "模版_原始备份.docx"),
    ("reference", "※小学生问卷和访谈提纲.docx"),
    ("reference", "※中学生问卷和访谈提纲.docx"),
    ("reference", "※大学生问卷和访谈提纲.docx"),
    ("reference", "※教师问卷和访谈提纲（包括教育管理人员和思政课教师）.docx"),
    ("reference", "※教育主管部分问卷和访谈提纲.docx"),
    ("data", "survey-submissions.xlsx"),
]


def main():
    db = SessionLocal()
    project = Project(user_id=USER_ID, name="端到端验证-模版红字生成")
    db.add(project)
    db.commit()
    db.refresh(project)
    pid = project.id
    for ftype, fname in FILES:
        data = (BASE / fname).read_bytes()
        path = f"{USER_ID}/{pid}/upload/{ftype}_{fname}"
        storage.upload_bytes(path, data)
        db.add(ProjectFile(project_id=pid, file_type=ftype, file_name=fname,
                           minio_path=path, size=len(data)))
    db.commit()
    db.close()
    print(f"project {pid} created, files uploaded")

    asyncio.run(run_analysis(pid, USER_ID))

    db = SessionLocal()
    project = db.get(Project, pid)
    print("status:", project.status, "error:", project.error_msg[:300])
    if project.status != "analyzed":
        db.close()
        sys.exit(1)

    tasks = db.query(ChapterTask).filter(ChapterTask.project_id == pid).order_by(ChapterTask.sort).all()
    print(f"chapter tasks: {len(tasks)}")
    heads = [t for t in tasks if t.task_type == "heading"]
    works = [t for t in tasks if t.task_type != "heading"]
    print(f"headings={len(heads)} work tasks={len(works)} "
          f"(text={sum(1 for t in works if t.task_type=='text')}, "
          f"table={sum(1 for t in works if t.task_type=='table')}, "
          f"image={sum(1 for t in works if t.task_type=='image')})")
    assert works, "没有拆解出写作任务"

    new_bytes = storage.download_bytes(project.parsed_template_path)
    doc = Document(io.BytesIO(new_bytes))
    out = Path("/tmp/parsed_template_e2e.docx")
    out.write_bytes(new_bytes)
    paras = doc.paragraphs
    reds = [p for p in paras if any(
        r.font.color and r.font.color.rgb is not None and str(r.font.color.rgb) == "FF0000"
        for r in p.runs)]
    print(f"red paragraphs in parsed template: {len(reds)}")
    for p in reds[:12]:
        print("  RED:", p.text[:60])
    img_count = sum(1 for p in paras if p._p.findall(".//" + qn("w:drawing")))
    print(f"remaining images: {img_count}, tables: {len(doc.tables)}")
    print(f"parsed template saved to {out}")
    db.close()


if __name__ == "__main__":
    main()
