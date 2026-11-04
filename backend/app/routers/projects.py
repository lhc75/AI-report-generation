"""项目、文件上传（MinIO）、模版 AI 分析、章节任务、内容生成与模块编辑。"""
import asyncio
import json
import logging
import io
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import assemble, generate, storage, template_parse
from .. import operations
from ..config import settings
from ..db import SessionLocal, get_db
from ..deps import get_current_user
from ..llm import chat_completion
from ..report_editorial import make_project_formatter
from ..models import (
    ChapterTask,
    GenerationRecord,
    ModelConfig,
    ModuleContent,
    Project,
    ProjectFile,
    User,
)
from ..skills import render_system_prompt

router = APIRouter(prefix="/api/projects", tags=["project"])
logger = logging.getLogger(__name__)

# 生成并发上限：避免同时启动过多 LLM 请求和沙箱子进程把系统拖垮，超出的排队执行
_gen_sem: asyncio.Semaphore | None = None


def gen_semaphore() -> asyncio.Semaphore:
    """惰性创建（信号量需在事件循环运行后使用）。"""
    global _gen_sem
    if _gen_sem is None:
        _gen_sem = asyncio.Semaphore(settings.GENERATE_CONCURRENCY)
    return _gen_sem

ALLOWED_EXT = {
    "template": {".docx"},
    "reference": {".docx", ".pdf"},
    "data": {".xlsx", ".xls", ".csv"},
}


class ProjectIn(BaseModel):
    name: str


def project_to_dict(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "status": p.status,
        "error_msg": p.error_msg,
        "parsed_template_path": p.parsed_template_path,
        "evidence_path": p.evidence_path,
        "report_path": p.report_path,
        "operation_token": p.operation_token,
        "material_revision": p.material_revision,
        "last_run_id": p.last_run_id,
        "last_run_result": json.loads(p.last_run_result or "{}"),
        "progress": json.loads(p.progress_json or "{}"),
        "created_at": p.created_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def file_to_dict(f: ProjectFile) -> dict:
    return {
        "id": f.id,
        "file_type": f.file_type,
        "file_name": f.file_name,
        "size": f.size,
        "created_at": f.created_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def clear_project_export(project: Project, db: Session) -> None:
    """输入资料变化或重新导出前，清除旧报告引用，避免失败时暴露过期报告。"""
    operations.invalidate_report(db, project)
    operations.queue_cleanup(db, project.evidence_path)
    project.evidence_path = ""


def get_own_project(project_id: int, user: User, db: Session) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.post("")
def create_project(data: ProjectIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project = Project(user_id=user.id, name=data.name)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project_to_dict(project)


@router.get("")
def list_projects(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Project).where(Project.user_id == user.id).order_by(Project.id.desc())
    ).all()
    return [project_to_dict(p) for p in rows]


@router.get("/{project_id}")
def get_project(project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return project_to_dict(get_own_project(project_id, user, db))


@router.delete("/{project_id}")
def delete_project(project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    with operations.project_transaction(project_id, user.id) as (tx, project):
        operations.require_idle(tx, project)
        for file in project.files:
            operations.queue_cleanup(tx, file.minio_path)
        for path in (project.parsed_template_path, project.evidence_path, project.report_path):
            operations.queue_cleanup(tx, path)
        task_ids = tx.scalars(select(ChapterTask.id).where(ChapterTask.project_id == project_id)).all()
        delete_tasks_cascade(tx, list(task_ids))
        tx.delete(project)
    operations.drain_cleanup()
    return {"ok": True}


@router.post("/{project_id}/files")
async def upload_file(project_id: int, file: UploadFile, file_type: str = Query(...),
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if file_type not in ALLOWED_EXT:
        raise HTTPException(400, "file_type 必须是 template/reference/data")
    name = file.filename or "unnamed"
    if Path(name).name != name or "\\" in name or len(name) > 255:
        raise HTTPException(400, "文件名不能包含路径，且不能超过 255 字符")
    if Path(name).suffix.lower() not in ALLOWED_EXT[file_type]:
        raise HTTPException(400, f"{file_type} 类型仅支持：{'、'.join(sorted(ALLOWED_EXT[file_type]))}")
    with operations.project_transaction(project_id, user.id) as (tx, project):
        token = operations.begin_run(tx, project)
        previous_status = project.status
    path = f"{user.id}/{project_id}/upload/{uuid4().hex}/{file_type}_{name}"
    try:
        data = await file.read()
        if file_type == "template":
            from docx import Document
            try:
                Document(io.BytesIO(data))
            except Exception as exc:
                raise HTTPException(400, "模板不是有效的 DOCX 文件，请从 Word 另存为 .docx") from exc
        await asyncio.to_thread(storage.upload_bytes, path, data)
        with operations.project_transaction(project_id, user.id) as (tx, project):
            if project.operation_token != token:
                raise HTTPException(409, "上传操作已失效")
            if file_type == "template":
                for old in tx.scalars(select(ProjectFile).where(
                    ProjectFile.project_id == project_id, ProjectFile.file_type == "template")).all():
                    operations.queue_cleanup(tx, old.minio_path)
                    tx.delete(old)
            pf = ProjectFile(project_id=project_id, file_type=file_type, file_name=name,
                             minio_path=path, size=len(data))
            tx.add(pf)
            operations.invalidate_inputs(tx, project)
            project.operation_token = ""
            tx.flush()
            result = file_to_dict(pf)
    except Exception:
        await asyncio.to_thread(operations.discard_unpublished, [path])
        operations.finish_run(project_id, token, previous_status, {"failed": 1, "message": "文件上传未完成，原文件已保留"})
        raise
    generate.invalidate_materials(project_id)
    operations.drain_cleanup()
    return result


@router.get("/{project_id}/files")
def list_files(project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_own_project(project_id, user, db)
    rows = db.scalars(
        select(ProjectFile).where(ProjectFile.project_id == project_id).order_by(ProjectFile.id)
    ).all()
    return [file_to_dict(f) for f in rows]


@router.delete("/{project_id}/files/{file_id}")
def delete_file(project_id: int, file_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    with operations.project_transaction(project_id, user.id) as (tx, project):
        operations.require_idle(tx, project)
        pf = tx.get(ProjectFile, file_id)
        if pf is None or pf.project_id != project_id:
            raise HTTPException(404, "文件不存在")
        operations.queue_cleanup(tx, pf.minio_path)
        tx.delete(pf)
        operations.invalidate_inputs(tx, project)
    generate.invalidate_materials(project_id)
    operations.drain_cleanup()
    return {"ok": True}


class StatisticsConfigIn(BaseModel):
    fields: list[dict] = Field(default_factory=list)


def _statistics_file(project_id, file_id, user, db):
    get_own_project(project_id, user, db)
    file = db.get(ProjectFile, file_id)
    if file is None or file.project_id != project_id or file.file_type != "data":
        raise HTTPException(404, "数据文件不存在")
    return file


@router.get("/{project_id}/files/{file_id}/statistics-config")
def get_statistics_config(project_id: int, file_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    file = _statistics_file(project_id, file_id, user, db)
    sheets = [{"name": sheet, "columns": [{"index": index, "name": column or f"列{index + 1}"}
               for index, column in enumerate(rows[0])]} for sheet, rows in
               template_parse.read_rows(file.file_name, storage.download_bytes(file.minio_path)) if rows]
    return {"sheets": sheets, "config": template_parse.normalize_config(file.statistics_config)}


@router.put("/{project_id}/files/{file_id}/statistics-config")
def put_statistics_config(project_id: int, file_id: int, data: StatisticsConfigIn,
                          user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        config = template_parse.normalize_config(data.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with operations.project_transaction(project_id, user.id) as (tx, project):
        token = operations.begin_run(tx, project)
        previous_status = project.status
        file = _statistics_file(project_id, file_id, user, tx)
        name, path = file.file_name, file.minio_path
    try:
        sheets = {sheet: rows for sheet, rows in template_parse.read_rows(name, storage.download_bytes(path))}
        for item in config['fields']:
            rows = sheets.get(item['sheet'])
            if not rows or item['column_index'] >= len(rows[0]):
                raise HTTPException(400, "统计设置引用了不存在的工作表或列")
        with operations.project_transaction(project_id, user.id) as (tx, project):
            if project.operation_token != token:
                raise HTTPException(409, "统计设置操作已失效")
            file = _statistics_file(project_id, file_id, user, tx)
            if template_parse.normalize_config(file.statistics_config) != config:
                file.statistics_config = json.dumps(config, ensure_ascii=False)
                operations.invalidate_inputs(tx, project)
            project.operation_token = ""
    except Exception:
        operations.finish_run(project_id, token, previous_status, {"failed": 1, "message": "统计设置未保存"})
        raise
    generate.invalidate_materials(project_id)
    operations.drain_cleanup()
    return {"ok": True, "config": config}


@router.get("/{project_id}/files/{file_id}/download")
def download_file(project_id: int, file_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_own_project(project_id, user, db)
    pf = db.get(ProjectFile, file_id)
    if pf is None or pf.project_id != project_id:
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"url": storage.presigned_url(pf.minio_path)}


# ---------- 模版 AI 分析 ----------

def pick_parse_model(db: Session, user_id: int) -> ModelConfig | None:
    """解析优先用 parse 类型的默认配置，其次全局默认配置。"""
    cfg = db.scalar(
        select(ModelConfig).where(
            ModelConfig.user_id == user_id, ModelConfig.agent_type == "parse", ModelConfig.is_default == True  # noqa: E712
        )
    )
    if cfg is None:
        cfg = db.scalar(
            select(ModelConfig).where(
                ModelConfig.user_id == user_id, ModelConfig.agent_type == "", ModelConfig.is_default == True  # noqa: E712
            )
        )
    if cfg is None:
        cfg = db.scalar(select(ModelConfig).where(ModelConfig.user_id == user_id))
    return cfg


def delete_tasks_cascade(db: Session, task_ids: list[int]) -> None:
    """级联删除任务：生成内容、生成记录、MinIO 图片。调用方负责 commit。"""
    if not task_ids:
        return
    contents = db.scalars(
        select(ModuleContent).where(ModuleContent.chapter_task_id.in_(task_ids))
    ).all()
    for c in contents:
        if c.image_path:
            operations.queue_cleanup(db, c.image_path)
        db.query(GenerationRecord).filter(
            GenerationRecord.module_content_id == c.id
        ).delete()
        db.delete(c)
    db.flush()  # 先落库内容删除，再删任务，避免外键约束冲突
    for tid in task_ids:
        row = db.get(ChapterTask, tid)
        if row is not None:
            db.delete(row)
    db.flush()


def save_tasks(db: Session, project_id: int, tasks: list[dict]) -> None:
    """把带 tmp_id/parent_tmp 的任务列表增量合并入库。

    与已有任务按「章节路径 + 同名序号」匹配：匹配上的保留原行（含已生成内容，
    再次分析不会推倒重来），只更新指令/类型/排序；新出现的插入；消失的旧任务
    级联删除。这样重新点击“分析”后，全部生成可以从待生成的模块继续。
    """
    existing = db.scalars(
        select(ChapterTask).where(ChapterTask.project_id == project_id)
        .order_by(ChapterTask.sort)
    ).all()
    by_id = {t.id: t for t in existing}

    def existing_path(t: ChapterTask) -> str:
        parts = [t.template_title or t.title]
        pid = t.parent_id
        while pid and pid in by_id:
            p = by_id[pid]
            parts.append(p.template_title or p.title)
            pid = p.parent_id
        return "/".join(reversed(parts))

    pool: dict[str, list[ChapterTask]] = {}
    slot_pool: dict[str, list[ChapterTask]] = {}
    for t in existing:
        pool.setdefault(existing_path(t), []).append(t)
        if t.slot_key:
            slot_pool.setdefault(t.slot_key, []).append(t)

    tmp_map = {t["tmp_id"]: t for t in tasks}

    def new_path(t: dict) -> str:
        parts = [t["title"]]
        pid = t["parent_tmp"]
        while pid is not None and pid in tmp_map:
            p = tmp_map[pid]
            parts.append(p["title"])
            pid = p["parent_tmp"]
        return "/".join(reversed(parts))

    id_map: dict[int, int] = {}
    matched: set[int] = set()
    ordered = sorted(tasks, key=lambda x: x["sort"])
    # 第一遍：匹配或新建行（parent 第二遍统一回填，避免顺序依赖）
    row_map: dict[int, ChapterTask] = {}
    for t in ordered:
        path = new_path(t)
        slot_key = str(t.get("slot_key", "") or "")
        slot_candidates = slot_pool.get(slot_key, []) if slot_key else []
        row = next((candidate for candidate in slot_candidates if candidate.id not in matched), None)
        if row is None:
            candidates = pool.get(path) or []
            row = next((candidate for candidate in candidates if candidate.id not in matched), None)
        if row is None:
            row = ChapterTask(project_id=project_id, status="pending")
            db.add(row)
        else:
            matched.add(row.id)
        row.level = t["level"]
        row.title = t["title"]
        row.sort = t["sort"]
        # 已完成的模块指令发生变化时，保留内容但标回待生成，提示需要重新生成
        if row.task_type != t["task_type"] or row.instruction != t["instruction"]:
            if row.id is not None:
                row.instruction_revision = (row.instruction_revision or 0) + 1
                row.status = "pending"
        row.task_type = t["task_type"]
        row.instruction = t["instruction"]
        row.template_title = t["title"]
        row.template_instruction = t["instruction"]
        row.slot_key = str(t.get("slot_key", "") or row.slot_key or "")
        if row.task_type == "heading":
            row.status = "done"  # 标题节点无需生成，避免统计显示为待生成
        db.flush()
        row_map[t["tmp_id"]] = row
    # 第二遍：回填 parent_id
    for t in ordered:
        row = row_map[t["tmp_id"]]
        parent = row_map.get(t["parent_tmp"]) if t["parent_tmp"] is not None else None
        row.parent_id = parent.id if parent is not None else None
        id_map[t["tmp_id"]] = row.id
    db.flush()
    # 消失的旧任务级联删除（已匹配的行不动）
    doomed = [t.id for t in existing if t.id not in matched]
    delete_tasks_cascade(db, doomed)


# 分析环节 JSON 自修复：解析失败时把模型原始输出与精确解析错误一并回传，
# 请模型自己修正后重出；不针对具体坏法写猜测式修补规则。
_JSON_REPAIR_PROMPT = (
    "上一条回复不是合法 JSON：{error}\n"
    "请修正语法错误后重新输出完整 JSON：\n"
    "1) 内容与含义保持不变，只修语法问题；\n"
    "2) 只输出 JSON 本身，不要任何解释、前后缀或 markdown 代码块；\n"
    "3) 字符串内部的英文双引号必须转义（写成 \\\"），或改用中文引号“”；\n"
    "4) 字符串内部不要出现原始换行，需要换行时写成 \\n。"
)


def _analysis_debug_dir() -> Path:
    """分析模型原始输出的落盘目录（单独抽出便于测试替换）。"""
    directory = Path(__file__).resolve().parents[2] / "data"
    directory.mkdir(exist_ok=True)
    return directory


def _save_analysis_output(project_id: int, tag: str, attempt: int, raw: str) -> None:
    """保存分析模型每轮输出：首轮沿用旧文件名，修复轮加 _repairN 后缀。"""
    suffix = "" if attempt == 0 else f"_repair{attempt}"
    (_analysis_debug_dir() / f"parse_output_p{project_id}{tag}{suffix}.txt").write_text(
        raw, encoding="utf-8"
    )


def _parse_ai_json_array(raw: str) -> list[dict]:
    """解析模型输出为数组；形状不符时抛协议错误，交由 AI 自修复。"""
    payload = template_parse.parse_ai_json(raw)
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("输出必须是 JSON 数组，且每个元素是对象")
    return payload


async def _complete_analysis_json(cfg: ModelConfig, messages: list, *,
                                  label: str, project_id: int, tag: str = "") -> list[dict]:
    """调用模型并要求输出可解析的 JSON 数组；解析失败时让模型自己纠错重出。

    最多额外修复 settings.ANALYSIS_JSON_REPAIR_ATTEMPTS 次（0 关闭）；
    仍失败抛 ValueError，由调用方按原有方式报错或兜底。
    """
    max_repairs = max(0, int(settings.ANALYSIS_JSON_REPAIR_ATTEMPTS))
    attempt = 0
    chat_messages = list(messages)
    last_error: Exception | None = None
    while True:
        raw = await chat_completion(cfg, chat_messages, timeout=settings.LLM_REQUEST_TIMEOUT)
        _save_analysis_output(project_id, tag, attempt, raw)
        try:
            return _parse_ai_json_array(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt >= max_repairs:
                break
            attempt += 1
            logger.warning("%s不是合法 JSON（%s），请求模型自修复（%d/%d）",
                           label, exc, attempt, max_repairs)
            chat_messages = chat_messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": _JSON_REPAIR_PROMPT.format(error=str(exc))},
            ]
    raise ValueError(
        f"{label}不是合法 JSON，已自动请求模型修复 {attempt} 次仍未成功：{last_error}"
        f"（模型原始输出已保存到 backend/data/parse_output_p{project_id}{tag}*.txt）"
    ) from last_error


async def run_analysis(project_id: int, user_id: int, run_token: str | None = None) -> None:
    """后台执行：解析模版 → 拆解章节任务 → 入库；AI 解析时回写带红字指令的模版。"""
    db = SessionLocal()
    uploaded_paths = []
    token = run_token
    try:
        project = operations.ensure_run(db, project_id, token, lock=False)
        files = db.scalars(select(ProjectFile).where(ProjectFile.project_id == project_id)).all()
        template = next((f for f in files if f.file_type == "template"), None)
        if template is None:
            raise ValueError("请先上传模版文件")
        if not template.file_name.lower().endswith(".docx"):
            raise ValueError("模版解析目前仅支持 .docx 格式（PDF 模版解析将在后续版本支持）")

        template_bytes = storage.download_bytes(template.minio_path)
        blocks = template_parse.extract_structure(template_bytes)
        headings = [b for b in blocks if b["kind"] == "heading"]
        if not headings:
            raise ValueError("模版中未识别到标题（请使用 Word 的标题样式）")

        has_red = any(b.get("red") for b in blocks if b["kind"] == "para")
        if has_red:
            # 模版已带红色指令：直接拆解入库
            tasks = template_parse.tasks_from_red_marks(blocks)
            parsed_path = ""
        else:
            # 无红字：两趟模型解析——第一趟规划大纲结构（对齐问卷设计维度），
            # 第二趟对重构后的大纲逐章生成写作指令，最后合并写回模版
            cfg = pick_parse_model(db, user_id)
            if cfg is None:
                raise ValueError("未找到模型配置，请先在“AI 配置”页面添加模型")
            refs, datas = [], []
            for f in files:
                raw = storage.download_bytes(f.minio_path)
                if f.file_type == "reference":
                    refs.append(template_parse.summarize_reference(f.file_name, raw))
                elif f.file_type == "data":
                    datas.append(template_parse.summarize_data_structure(f.file_name, raw))
            data_text = ("\n".join(datas) or "（无）") + (
                "\n统计单位为提交记录，不能自动称有效独立人数；禁止将不同角色同号题合并。"
                "题干与中文选项以参考资料为准；复合或额外字段只说明边界，不设计无法从数据计算的任务。")
            refs_text = "\n\n".join(refs) or "（无）"

            def _sys_msgs() -> list:
                system_prompt = render_system_prompt(db, cfg)
                return (
                    [{"role": "system", "content": system_prompt}]
                    if system_prompt else []
                )

            # 第一趟：结构规划（失败或输出非法时按“全部保留”兜底，不影响第二趟）
            renames: dict = {}
            drops: dict = {}
            new_secs: list = []
            try:
                outline_prompt = (
                    template_parse.OUTLINE_PROMPT
                    .replace("{outline}", template_parse.outline_summary(blocks))
                    .replace("{references}", refs_text)
                    .replace("{data_summary}", data_text)
                )
                # 本地大模型生成较慢，给足超时；输出非法 JSON 时先让模型自修复
                plan = await _complete_analysis_json(
                    cfg, _sys_msgs() + [{"role": "user", "content": outline_prompt}],
                    label="模版结构规划输出", project_id=project_id, tag="_outline",
                )
                renames, drops, new_secs = template_parse.outline_plan_to_edits(plan, blocks)
            except Exception:
                logger.exception("outline planning failed, fallback to keep-all")
                renames, drops, new_secs = {}, {}, []
            edited_blocks = template_parse.apply_structure_to_blocks(
                blocks, renames, drops, new_secs
            )

            # 第二趟：对重构后的大纲逐章生成写作指令
            structure_text = template_parse.structure_summary(edited_blocks)
            prompt = (
                template_parse.PARSE_PROMPT
                .replace("{structure}", structure_text)
                .replace("{references}", refs_text)
                .replace("{data_summary}", data_text)
            )
            # 输出非法 JSON 时把解析错误回传给模型自修复；每轮输出落盘便于排查
            ai_tasks = await _complete_analysis_json(
                cfg, _sys_msgs() + [{"role": "user", "content": prompt}],
                label="模版写作指令输出", project_id=project_id,
            )
            # 清洗指令：改写“旧称…统一为…”元语句、英文编码转中文名称，
            # 防止写作 AI 把编辑性表述和编码照抄进报告正文
            ai_tasks = template_parse.sanitize_ai_tasks(
                ai_tasks, template_parse.extract_code_map(data_text)
            )
            # 合并：第二趟按新标题生成的指令映射回模版原标题，补上结构编辑
            merged = template_parse.merge_structure_and_instructions(
                ai_tasks, renames, drops, new_secs
            )
            new_docx = template_parse.write_instructions_to_docx(template_bytes, merged)
            parsed_path = f"{user_id}/{project_id}/parsed/{token or uuid4().hex}.docx"
            storage.upload_bytes(parsed_path, new_docx)
            uploaded_paths.append(parsed_path)
            tasks = template_parse.tasks_from_ai_result(blocks, merged)

        if not any(t["task_type"] != "heading" for t in tasks):
            raise ValueError(
                "未拆解出任何写作任务（模版中没有红色指令，AI 解析也未产出指令；"
                f"模型原始输出已保存到 backend/data/parse_output_p{project_id}.txt）"
            )

        db.rollback()
        project = operations.ensure_run(db, project_id, token)
        operations.invalidate_report(db, project)
        if project.parsed_template_path != parsed_path:
            operations.queue_cleanup(db, project.parsed_template_path)
        save_tasks(db, project_id, tasks)
        project.status = "analyzed"
        project.parsed_template_path = parsed_path
        project.evidence_path = ""
        project.error_msg = ""
        db.commit()
        if token:
            operations.finish_run(project_id, token, "analyzed", {"succeeded": 1, "failed": 0, "total": 1})
    except Exception as exc:
        logger.exception("analysis failed")
        db.rollback()
        operations.discard_unpublished(uploaded_paths)
        msg = f"{type(exc).__name__}: {str(exc) or type(exc).__name__}"[:500]
        if token:
            operations.finish_run(project_id, token, "failed", {"failed": 1, "message": msg})
        else:
            project = db.get(Project, project_id)
            if project:
                project.status, project.error_msg = "failed", msg
                db.commit()
    finally:
        db.close()
        operations.drain_cleanup()


@router.post("/{project_id}/analyze")
async def analyze(project_id: int, background: BackgroundTasks,
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    with operations.project_transaction(project_id, user.id) as (tx, project):
        token = operations.begin_run(tx, project, "analyzing")
        if not tx.scalar(select(ProjectFile.id).where(ProjectFile.project_id == project_id,
                                                     ProjectFile.file_type == "template")):
            raise HTTPException(400, "请先上传 DOCX 模板")
        operations.invalidate_report(tx, project)
    background.add_task(run_analysis, project_id, user.id, token)
    return {"ok": True, "status": "analyzing", "run_id": token}


@router.get("/{project_id}/chapters")
def list_chapters(project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project = get_own_project(project_id, user, db)
    rows = db.scalars(select(ChapterTask).where(ChapterTask.project_id == project_id)
                      .order_by(ChapterTask.sort, ChapterTask.id)).all()
    history = db.scalars(select(ModuleContent).where(
        ModuleContent.chapter_task_id.in_([row.id for row in rows] or [0]))
        .order_by(ModuleContent.version.desc())).all()
    result = []
    for row in rows:
        contents = [content for content in history if content.chapter_task_id == row.id]
        current = next((content for content in contents if content.is_current), None)
        latest = contents[0] if contents else None
        result.append({
            "id": row.id, "parent_id": row.parent_id, "level": row.level,
            "title": row.title, "sort": row.sort, "task_type": row.task_type,
            "instruction": row.instruction, "status": row.status,
            "has_content": current is not None, "history_count": len(contents),
            "current_version": current.version if current else 0,
            "latest_error": latest.error_msg if latest and latest.status == "failed" else "",
            "is_stale": current is not None and not operations.content_is_valid(current, row, project),
        })
    return result


@router.get("/{project_id}/parsed-template")
def download_parsed(project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project = get_own_project(project_id, user, db)
    if not project.parsed_template_path:
        raise HTTPException(status_code=404, detail="没有解析生成的模版（模版自带红字指令时不会生成）")
    return {"url": storage.presigned_url(project.parsed_template_path)}


# ---------- 第三步：组装导出 ----------

async def run_assemble(project_id: int, run_token: str | None = None) -> None:
    async with gen_semaphore():
        db = SessionLocal()
        try:
            project = operations.ensure_run(db, project_id, run_token, lock=False)
            await assemble.assemble_report(db, project, run_token=run_token)
            if run_token:
                operations.finish_run(project_id, run_token, "analyzed", {"succeeded": 1, "failed": 0, "total": 1})
        except Exception as exc:
            logger.exception("run_assemble failed")
            db.rollback()
            if run_token:
                operations.finish_run(project_id, run_token, "analyzed", {
                    "failed": 1, "total": 1, "message": f"组装导出失败：{type(exc).__name__}: {str(exc) or repr(exc)}"[:1000]})
        finally:
            db.close()
            operations.drain_cleanup()


@router.post("/{project_id}/assemble")
async def assemble_project(project_id: int, background: BackgroundTasks,
                           user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    with operations.project_transaction(project_id, user.id) as (tx, project):
        operations.require_idle(tx, project)
        if project.status != "analyzed":
            raise HTTPException(400, "请先完成模版分析")
        tasks = tx.scalars(select(ChapterTask).where(ChapterTask.project_id == project_id,
                                                   ChapterTask.task_type != "heading")).all()
        current = {c.chapter_task_id: c for c in tx.scalars(select(ModuleContent).where(
            ModuleContent.chapter_task_id.in_([t.id for t in tasks] or [0]), ModuleContent.is_current == True)).all()}
        incomplete = [t for t in tasks if not operations.content_is_valid(current.get(t.id), t, project)]
        if not tasks or incomplete:
            details = "；".join(f"{t.id} {t.title[:45]}" for t in incomplete[:12])
            raise HTTPException(400, "存在未生成或过期的模块，请先生成内容" + (f"：{details}" if details else ""))
        token = operations.begin_run(tx, project, "assembling")
        operations.invalidate_report(tx, project)
    background.add_task(run_assemble, project_id, token)
    return {"ok": True, "status": "assembling", "run_id": token}


@router.get("/{project_id}/report")
def download_report(project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project = get_own_project(project_id, user, db)
    if not project.report_path:
        raise HTTPException(status_code=404, detail="尚未导出报告，请先点击“导出报告”")
    return {"url": storage.presigned_url(project.report_path)}


@router.get("/{project_id}/report/preview")
def preview_report(project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project = get_own_project(project_id, user, db)
    if not project.report_path:
        raise HTTPException(status_code=404, detail="尚未导出报告，请先点击“导出报告”")
    return Response(
        content=storage.download_bytes(project.report_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ---------- 第二步：章节内容生成与模块编辑 ----------


class RewriteIn(BaseModel):
    prompt: str = ""


class InsertIn(BaseModel):
    after_task_id: int  # 在该模块之后插入
    requirement: str


class RollbackIn(BaseModel):
    content_id: int


class TaskEditIn(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    instruction: str | None = Field(default=None, max_length=20000)

    @field_validator("title", "instruction", mode="before")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value


def get_own_task(project_id: int, task_id: int, user: User, db: Session) -> ChapterTask:
    get_own_project(project_id, user, db)
    task = db.get(ChapterTask, task_id)
    if task is None or task.project_id != project_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.patch("/{project_id}/tasks/{task_id}")
def edit_task(project_id: int, task_id: int, data: TaskEditIn,
              user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """编辑生成任务；保留历史和原模板定位，不触发模型请求。"""
    with operations.project_transaction(project_id, user.id) as (tx, project):
        operations.require_idle(tx, project)
        if project.status != "analyzed":
            raise HTTPException(400, "请先完成模版分析")
        task = tx.get(ChapterTask, task_id)
        if task is None or task.project_id != project_id:
            raise HTTPException(404, "任务不存在")
        if task.task_type == "heading":
            if data.instruction:
                raise HTTPException(400, "标题节点只需编辑标题")
            instruction, task_type = task.instruction, "heading"
        else:
            if not data.instruction:
                raise HTTPException(400, "请填写生成指令")
            if template_parse.is_note_instruction(data.instruction):
                raise HTTPException(400, "请填写内容生成指令，不能改为说明或备注")
            instruction = data.instruction
            task_type = template_parse.classify_instruction(instruction)
        changed = (task.title != data.title or task.instruction != instruction or task.task_type != task_type)
        if changed:
            # 定位信息记录模板原值；以后编辑不能覆盖，否则导出会失去模板锚点。
            task.template_title = task.template_title or task.title
            task.template_instruction = task.template_instruction or task.instruction
            task.title, task.instruction = data.title, instruction
            task.task_type = task_type
            if task_type != "heading":
                task.instruction_revision += 1
                task.status = "pending"
            operations.invalidate_report(tx, project)
        result = {"ok": True, "task_id": task.id, "changed": changed,
                  "task_type": task.task_type, "status": task.status}
    operations.drain_cleanup()
    return result


async def run_generate_one(project_id: int, task_id: int, user_prompt: str = "",
                           run_token: str | None = None, finish: bool = True) -> None:
    async with gen_semaphore():
        db = SessionLocal()
        try:
            project = operations.ensure_run(db, project_id, run_token, lock=False)
            task = db.get(ChapterTask, task_id)
            if task is None:
                raise ValueError("生成任务已不存在")
            await generate.generate_task(db, project, task, user_prompt, run_token=run_token)
        except Exception as exc:
            logger.exception("run_generate_one failed")
            db.rollback()
            try:
                operations.ensure_run(db, project_id, run_token)
                task = db.get(ChapterTask, task_id)
                if task:
                    task.status = "failed"
                    db.commit()
            except RuntimeError:
                db.rollback()
        finally:
            db.close()
            if finish and run_token:
                _finish_generation(project_id, run_token, [task_id])
            operations.drain_cleanup()


def _finish_generation(project_id: int, run_token: str, task_ids: list[int]) -> None:
    with SessionLocal() as db:
        tasks = db.scalars(select(ChapterTask).where(ChapterTask.id.in_(task_ids or [0]))).all()
        succeeded = sum(task.status == "done" for task in tasks)
        failed = len(task_ids) - succeeded
    operations.finish_run(project_id, run_token, "analyzed", {
        "succeeded": succeeded, "failed": failed, "total": len(task_ids),
        "message": f"生成完成：成功 {succeeded} 个，失败 {failed} 个，请查看失败详情并重试" if failed else "",
    })


async def run_generate_all(project_id: int, user_id: int, run_token: str | None = None,
                           task_ids: list[int] | None = None) -> None:
    # 兼容离线直接调用；HTTP 入口已在短事务中确定任务清单。
    if run_token is None:
        with operations.project_transaction(project_id, user_id) as (tx, project):
            run_token = operations.begin_run(tx, project, "generating")
            tasks = tx.scalars(select(ChapterTask).where(ChapterTask.project_id == project_id,
                ChapterTask.task_type != "heading", ChapterTask.status.in_(["pending", "failed"]))).all()
            task_ids = [task.id for task in tasks]
            for task in tasks:
                task.status = "queued"
    try:
        await asyncio.gather(*(run_generate_one(project_id, task_id, run_token=run_token, finish=False)
                               for task_id in task_ids or []))
    finally:
        _finish_generation(project_id, run_token, task_ids or [])


@router.post("/{project_id}/generate")
async def generate_all(project_id: int, background: BackgroundTasks,
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    with operations.project_transaction(project_id, user.id) as (tx, project):
        operations.require_idle(tx, project)
        if project.status != "analyzed":
            raise HTTPException(400, "请先完成模版分析")
        tasks = tx.scalars(select(ChapterTask).where(ChapterTask.project_id == project_id,
            ChapterTask.task_type != "heading", ChapterTask.status.in_(["pending", "failed"]))
            .order_by(ChapterTask.sort, ChapterTask.id)).all()
        if not tasks:
            return {"ok": True, "status": "analyzed", "run_id": "", "total": 0}
        token = operations.begin_run(tx, project, "generating")
        task_ids = [task.id for task in tasks]
        for task in tasks:
            task.status = "queued"
    background.add_task(run_generate_all, project_id, user.id, token, task_ids)
    return {"ok": True, "status": "generating", "run_id": token, "total": len(task_ids)}


def _start_task(project_id, task_id, user, background, prompt=""):
    with operations.project_transaction(project_id, user.id) as (tx, project):
        operations.require_idle(tx, project)
        if project.status != "analyzed":
            raise HTTPException(400, "请先完成模版分析")
        task = tx.get(ChapterTask, task_id)
        if task is None or task.project_id != project_id:
            raise HTTPException(404, "任务不存在")
        if task.task_type == "heading":
            raise HTTPException(400, "标题节点不需要生成内容")
        token = operations.begin_run(tx, project, "generating")
        task.status = "queued"
    background.add_task(run_generate_one, project_id, task_id, prompt, token)
    return {"ok": True, "run_id": token, "status": "generating"}


@router.post("/{project_id}/tasks/{task_id}/generate")
async def generate_one(project_id: int, task_id: int, background: BackgroundTasks,
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _start_task(project_id, task_id, user, background)


@router.post("/{project_id}/tasks/{task_id}/rewrite")
async def rewrite_one(project_id: int, task_id: int, data: RewriteIn, background: BackgroundTasks,
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _start_task(project_id, task_id, user, background, data.prompt)


def content_to_dict(c: ModuleContent, with_body: bool) -> dict:
    item = {
        "id": c.id,
        "version": c.version,
        "content_type": c.content_type,
        "prompt": c.prompt,
        "status": c.status,
        "error_msg": c.error_msg,
        "is_current": c.is_current,
        "created_at": c.created_at.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if with_body:
        item["content"] = c.content
        item["evidence"] = json.loads(c.evidence_json or "[]")
        item["image_url"] = storage.presigned_url(c.image_path) if c.image_path else ""
    return item


@router.get("/{project_id}/tasks/{task_id}/contents")
def list_contents(
    project_id: int,
    task_id: int,
    content_id: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """版本列表 + 指定（默认当前）版本的内容。"""
    task = get_own_task(project_id, task_id, user, db)
    project = get_own_project(project_id, user, db)
    rows = db.scalars(
        select(ModuleContent)
        .where(ModuleContent.chapter_task_id == task_id)
        .order_by(ModuleContent.version.desc())
    ).all()
    current = next((c for c in rows if c.is_current), None)
    detail = None
    if content_id is not None:
        detail = next((c for c in rows if c.id == content_id), None)
    elif current is not None:
        detail = current
    elif rows:
        detail = rows[0]

    display_content = None
    if (detail is not None and detail.is_current
            and operations.content_is_valid(detail, task, project)
            and detail.content_type in {"text", "table"}):
        tasks = db.scalars(select(ChapterTask).where(ChapterTask.project_id == project_id)
                           .order_by(ChapterTask.sort, ChapterTask.id)).all()
        current_contents = {c.chapter_task_id: c for c in db.scalars(select(ModuleContent).where(
            ModuleContent.chapter_task_id.in_([t.id for t in tasks]),
            ModuleContent.is_current == True,  # noqa: E712
        )).all()}
        names = db.scalars(select(ProjectFile.file_name).where(
            ProjectFile.project_id == project_id, ProjectFile.file_type == "data"
        ).order_by(ProjectFile.id)).all()
        formatter, method_task = make_project_formatter(project, tasks, current_contents, names)
        try:
            if detail.content_type == "text":
                display_content = formatter.text(detail.content,
                    methodology=bool(method_task and task.id == method_task.id))
            else:
                display_content = json.dumps(formatter.table(json.loads(detail.content),
                                            module_title=task.title), ensure_ascii=False)
        except (ValueError, TypeError, AttributeError):
            # Invalid old payloads stay inspectable; display editing cannot alter
            # or obscure the stored content and its evidence.
            logger.warning("模块 %s 的预览编辑未通过，保留原内容", task.id, exc_info=True)

    def version(c, body):
        result = content_to_dict(c, with_body=body)
        result["can_rollback"] = operations.content_is_valid(c, task, project)
        result["is_stale"] = (c.material_revision != project.material_revision or
                              c.instruction_revision != task.instruction_revision)
        if body and display_content is not None:
            result["display_content"] = display_content
        return result
    return {"versions": [version(c, False) for c in rows],
            "content": version(detail, True) if detail else None}


@router.post("/{project_id}/tasks/{task_id}/rollback")
def rollback_content(project_id: int, task_id: int, data: RollbackIn,
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    with operations.project_transaction(project_id, user.id) as (tx, project):
        operations.require_idle(tx, project)
        task = get_own_task(project_id, task_id, user, tx)
        target = tx.get(ModuleContent, data.content_id)
        if target is None or target.chapter_task_id != task_id:
            raise HTTPException(404, "版本不存在")
        if not operations.content_is_valid(target, task, project):
            raise HTTPException(400, "失败或材料/指令已变化的版本不能回退，请重新生成")
        tx.query(ModuleContent).filter(ModuleContent.chapter_task_id == task_id).update({"is_current": False})
        target.is_current = True
        task.status = "done"
        operations.invalidate_report(tx, project)
    operations.drain_cleanup()
    return {"ok": True}


@router.post("/{project_id}/tasks/insert")
async def insert_task(project_id: int, data: InsertIn, background: BackgroundTasks,
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    requirement = data.requirement.strip()
    if not requirement:
        raise HTTPException(400, "请填写新模块的需求描述")
    with operations.project_transaction(project_id, user.id) as (tx, project):
        operations.require_idle(tx, project)
        if project.status != "analyzed":
            raise HTTPException(400, "请先完成模版分析")
        after = tx.get(ChapterTask, data.after_task_id)
        if after is None or after.project_id != project_id or after.task_type == "heading":
            raise HTTPException(404, "插入位置不存在或不是内容模块")
        for sibling in tx.scalars(select(ChapterTask).where(ChapterTask.project_id == project_id,
                                                           ChapterTask.sort > after.sort)).all():
            sibling.sort += 1
        instruction = requirement if requirement.startswith("【") else f"【填写：{requirement}】"
        token = operations.begin_run(tx, project, "generating")
        task = ChapterTask(project_id=project_id, parent_id=after.parent_id, level=after.level,
            title=requirement[:60], sort=after.sort + 1, task_type=template_parse.classify_instruction(instruction),
            instruction=instruction, anchor_task_id=after.id, status="queued")
        tx.add(task)
        operations.invalidate_report(tx, project)
        tx.flush()
        task_id = task.id
    background.add_task(run_generate_one, project_id, task_id, "", token)
    operations.drain_cleanup()
    return {"ok": True, "task_id": task_id, "run_id": token, "status": "generating"}


@router.delete("/{project_id}/tasks/{task_id}")
def delete_task(project_id: int, task_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    with operations.project_transaction(project_id, user.id) as (tx, project):
        operations.require_idle(tx, project)
        task = get_own_task(project_id, task_id, user, tx)
        tasks = tx.scalars(select(ChapterTask).where(ChapterTask.project_id == project_id)).all()
        doomed = {task.id}
        while True:
            additional = {row.id for row in tasks if row.parent_id in doomed or row.anchor_task_id in doomed}
            if additional <= doomed:
                break
            doomed.update(additional)
        delete_tasks_cascade(tx, list(doomed))
        operations.invalidate_report(tx, project)
    operations.drain_cleanup()
    return {"ok": True, "deleted": len(doomed)}
