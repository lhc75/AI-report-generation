"""Self-contained offline regression fixtures; call before importing app modules.

Every script gets a temporary SQLite database, local object-store substitute and
synthetic documents. An unmocked model client fails before making a network call.
"""
from __future__ import annotations

import atexit
import os
import sys
import tempfile
from pathlib import Path

_environment = None


class OfflineEnvironment:
    def __init__(self):
        if "app.db" in sys.modules:
            raise RuntimeError("Install the offline environment before importing app.db")
        self._temporary = tempfile.TemporaryDirectory(prefix="ai-report-offline-")
        self.root = Path(self._temporary.name).resolve()
        self._closed = False
        from cryptography.fernet import Fernet
        configured = {
            "AI_REPORT_ENV_FILE": "", "DB_ENGINE": "sqlite",
            "SQLITE_PATH": str(self.root / "regression.sqlite"),
            "STORAGE_BACKEND": "local", "STORAGE_LOCAL_DIR": str(self.root / "objects"),
            "MODEL_CONFIG_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "JWT_SECRET_KEY": "offline-regression-only", "LLM_CA_BUNDLE": "",
            "GENERATE_CONCURRENCY": "2", "REACT_MAX_TURNS": "10",
        }
        self._previous_environment = {name: os.environ.get(name) for name in configured}
        os.environ.update(configured)
        from app import db, llm, sandbox, storage
        from app.models import User
        db.init_db()
        storage.ensure_bucket()
        self.session_factory = db.SessionLocal
        self._engine = db.engine
        with self.session_factory() as session:
            user = User(username="offline-regression", password_hash="not-for-login")
            session.add(user)
            session.commit()
            self.user_id = user.id
        self._original_client = llm.httpx.AsyncClient

        class UnmockedModelClient:
            def __init__(self, *args, **kwargs):
                raise AssertionError("Offline regression attempted an unmocked HTTP/model request")

        llm.httpx.AsyncClient = UnmockedModelClient
        self._llm = llm
        self._original_presign = storage.presigned_url
        storage.presigned_url = lambda path: f"/offline-objects/{path}"
        self._storage = storage
        self._original_workdir = sandbox.fresh_workdir

        def fresh_workdir(project_id, task_id, run_token=None):
            base = self.root / "sandbox" / f"p{project_id}" / f"t{task_id}"
            base.mkdir(parents=True, exist_ok=True)
            workdir = Path(tempfile.mkdtemp(prefix="run-", dir=base)).resolve()
            workdir.chmod(0o700)
            (workdir / "data").mkdir()
            (workdir / "out").mkdir()
            return workdir

        sandbox.fresh_workdir = fresh_workdir
        self._sandbox = sandbox
        self._build_fixtures()
        atexit.register(self.close)

    def _build_fixtures(self):
        from docx import Document
        from docx.shared import Inches
        from openpyxl import Workbook
        from PIL import Image, ImageDraw
        fixtures = self.root / "fixtures"
        fixtures.mkdir()
        self.chart = fixtures / "chart.png"
        image = Image.new("RGB", (640, 360), "white")
        draw = ImageDraw.Draw(image)
        for i, height in enumerate([210, 165, 130, 90, 50]):
            draw.rectangle((60 + i * 105, 320 - height, 125 + i * 105, 320), fill=(30 + i * 25, 105, 190))
        image.save(self.chart, compress_level=0)

        self.xlsx = fixtures / "survey-submissions.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "问卷回收"
        sheet.append(["问卷编码", "问卷名称", "地区", "学段", "回答"])
        groups = [
            ("education_supervisor", "教育主管部门问卷", 453),
            ("teacher", "教师问卷", 336),
            ("primary_student", "小学生问卷", 151),
            ("middle_student", "中学生问卷", 122),
            ("college_student", "大学生问卷", 4),
        ]
        for code, label, count in groups:
            name = f"贵州省大中小学思想政治教育一体化建设问卷调查（{label}）"
            for index in range(count):
                answers = []
                for question in range(1, 11):
                    if code == "education_supervisor" and question == 2:
                        value = "A" if index < 364 else "B"
                    elif question in (3, 7):
                        value = "A/B" if index % 3 else "B/C"
                    else:
                        value = ["A", "B", "C"][index % 3]
                    answers.append(f"q{question}：{value}")
                sheet.append([code, name, ["东部", "中部", "西部"][index % 3], label, "；".join(answers)])
        workbook.save(self.xlsx)

        self.template = fixtures / "template.docx"
        document = Document()
        document.add_paragraph("说 明")
        document.add_paragraph("原调查共3647份有效问卷，旧报告正文用于检验说明章节替换。")
        document.add_paragraph("贵州省基础教育研究院")
        document.add_paragraph("目 录")
        headings = [
            (1, "第一部分 概述"), (2, "一、目的"), (2, "二、调查内容"),
            (1, "第二部分 调查结果"), (2, "一、主体认知"),
            (3, "1、认知理解"), (3, "2、价值认同"), (3, "3、行为感知"), (3, "4、小结"),
            (2, "三、要素协同"), (3, "1、课程协同"), (3, "2、教学协同"), (3, "3、组织协同"), (3, "4、小结"),
            (2, "四、评估与衔接"), (3, "1、评估"), (3, "2、衔接"), (3, "3、反馈"), (3, "4、小结"),
            (2, "五、综合反馈"), (3, "1、满意程度"), (3, "2、建议"),
            (1, "第三部分 结论"), (2, "一、主要结论"), (2, "二、改进建议"), (2, "三、保障"), (2, "四、展望"),
        ]
        for index, (level, title) in enumerate(headings):
            document.add_heading(title, level)
            document.add_paragraph("前学段对现学段的影响量表描述，未覆盖章节应完整保留。" if title == "3、行为感知"
                                   else f"{title}的既有正文，验证模板改写保留与替换。")
            for _ in range(4 if index < 9 else 3):
                document.add_picture(str(self.chart), width=Inches(1))
            if index < 4:
                table = document.add_table(rows=2, cols=2)
                table.cell(0, 0).text = "旧表标题"
                table.cell(1, 1).text = "旧表内容"
        document.save(self.template)

        self.reference = fixtures / "primary-questionnaire.docx"
        reference = Document()
        reference.add_heading("小学生问卷", 1)
        reference.add_paragraph("你觉得学校的思政课堂和活动对你有什么帮助？")
        reference.add_paragraph("请填写你认为最需要改进的地方。")
        reference.save(self.reference)
        self.reference_big = fixtures / "teacher-questionnaire.docx"
        reference = Document()
        for part in range(1, 7):
            reference.add_heading(f"第{part}部分", 1)
            for question in range(12):
                reference.add_paragraph(f"第{part}部分问题{question}：请结合课程教学、跨学段衔接和实践活动说明实际情况。")
        reference.add_paragraph("第六部分最后一题：目前亟须补齐的短板有哪些？")
        reference.save(self.reference_big)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._llm.httpx.AsyncClient = self._original_client
        self._storage.presigned_url = self._original_presign
        self._sandbox.fresh_workdir = self._original_workdir
        self._engine.dispose()
        self._temporary.cleanup()
        for name, previous in self._previous_environment.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous


def install_offline_environment():
    global _environment
    if _environment is None:
        _environment = OfflineEnvironment()
    return _environment


initialize_offline_environment = install_offline_environment
