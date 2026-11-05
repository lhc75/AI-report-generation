"""第三步：证据审核后，在原 Word 模板中原位填充并导出。"""
from __future__ import annotations

import io
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
import unicodedata
import xml.etree.ElementTree as ET
from copy import deepcopy
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.image.image import Image
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from docx.section import Section
from docx.text.paragraph import Paragraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import operations, review, sandbox, storage, template_parse
from .models import ChapterTask, ModuleContent, Project, ProjectFile
from .report_text import SourceTextFormatter
from .report_editorial import ReportEditorialFormatter, make_project_formatter

logger = logging.getLogger(__name__)

_INSTRUCTION_RE = re.compile(r"^【")
_REPORT_DATE_RE = re.compile(r"\s*(20\d{2})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?\s*")
_SIGNOFF_AUTHOR_RE = re.compile(
    r"(?:研究院|研究所|大学|学院|学校|教育局|教育厅|委员会|人民政府|办公室|"
    r"课题组|项目组|调查组|编写组|工作组|研究团队|公司|协会|学会)$"
)


class TemplateAssemblyError(ValueError):
    pass


def _template_bytes(db: Session, project: Project) -> bytes:
    if project.parsed_template_path:
        try:
            return storage.download_bytes(project.parsed_template_path)
        except Exception:
            logger.warning("parsed template unavailable, falling back to original", exc_info=True)
    return _original_template_bytes(db, project)


def _original_template_bytes(db: Session, project: Project) -> bytes:
    """读取上传原件；解析后的模板可能已经移除了可用于核实落款的正文。"""
    tpl = db.scalar(
        select(ProjectFile).where(
            ProjectFile.project_id == project.id,
            ProjectFile.file_type == "template",
        )
    )
    if tpl is None:
        raise TemplateAssemblyError("项目没有上传模板")
    if not tpl.file_name.lower().endswith(".docx"):
        raise TemplateAssemblyError("当前模板装配仅支持 DOCX")
    return storage.download_bytes(tpl.minio_path)


def _doc_signature(doc: Document) -> dict[str, Any]:
    xml = doc.element.xml
    bookmark_names = sorted(re.findall(r'<w:bookmarkStart[^>]*w:name="([^"]+)"', xml))
    heading_titles = [
        paragraph.text.strip()
        for paragraph in doc.paragraphs
        if paragraph.style is not None
        and paragraph.style.name.startswith("Heading")
        and paragraph.text.strip()
    ]
    return {
        "sections": len(doc.sections),
        "section_properties": len(re.findall(r"<w:sectPr\b", xml)),
        "geometry": [
            [
                section.page_width,
                section.page_height,
                section.top_margin,
                section.bottom_margin,
                section.left_margin,
                section.right_margin,
            ]
            for section in doc.sections
        ],
        "bookmarks": len(re.findall(r"<w:bookmarkStart\b", xml)),
        "bookmark_names": bookmark_names,
        "heading_titles": heading_titles,
        "toc": bool(re.search(r"TOC\s+\\", xml)),
        "toc_entries": _toc_cache_lines(doc),
        "tables": len(doc.tables),
        "images": len(doc.inline_shapes),
        "static_paragraphs": [
            paragraph.text for paragraph in doc.paragraphs
            if paragraph.text.strip() and not _INSTRUCTION_RE.match(paragraph.text.strip())
        ],
        "table_contents": [_table_signature(table) for table in doc.tables],
        "image_contents": _image_signatures(doc),
    }


def _toc_cache_lines(doc: Document) -> list[str]:
    stack: list[dict] = []
    paragraphs: dict[Any, list[str]] = {}
    for element in doc.element.body.iter():
        if element.tag == qn("w:fldChar"):
            kind = element.get(qn("w:fldCharType"))
            if kind == "begin":
                stack.append({"toc": False, "separated": False})
            elif kind == "separate" and stack:
                stack[-1]["separated"] = True
            elif kind == "end" and stack:
                stack.pop()
        elif element.tag == qn("w:instrText") and stack and re.match(r"\s*TOC\b", element.text or "", re.I):
            stack[-1]["toc"] = True
        elif element.tag == qn("w:t"):
            inside_toc = any(field["toc"] and field["separated"] for field in stack)
            parent = element.getparent()
            while parent is not None and not inside_toc:
                inside_toc = parent.tag == qn("w:fldSimple") and bool(re.match(r"\s*TOC\b", parent.get(qn("w:instr"), ""), re.I))
                parent = parent.getparent()
            if inside_toc:
                paragraph = _field_paragraph(element)
                paragraphs.setdefault(paragraph, []).append(element.text or "")
    return ["".join(parts).strip() for parts in paragraphs.values() if "".join(parts).strip()]


def _node_content_file(workdir: Path, task: ChapterTask, content: ModuleContent | None, *,
                       source_formatter: SourceTextFormatter | None = None,
                       methodology: bool = False) -> str | None:
    if content is None:
        return None
    if task.task_type == "text":
        rel = f"contents/t{task.id}.txt"
        text = (source_formatter.text(content.content, methodology=methodology)
                if isinstance(source_formatter, ReportEditorialFormatter)
                else source_formatter.text(content.content) if source_formatter else content.content)
        (workdir / rel).write_text(text, encoding="utf-8")
        return rel
    if task.task_type == "table":
        rel = f"contents/t{task.id}.json"
        payload = content.content
        if source_formatter:
            table = json.loads(payload)
            display = (source_formatter.table(table, module_title=task.title)
                       if isinstance(source_formatter, ReportEditorialFormatter)
                       else source_formatter.table(table))
            payload = json.dumps(display, ensure_ascii=False)
        (workdir / rel).write_text(payload, encoding="utf-8")
        return rel
    if task.task_type == "image" and content.image_path:
        rel = f"contents/t{task.id}.png"
        (workdir / rel).write_bytes(storage.download_bytes(content.image_path))
        return rel
    return None


def _validated_chart_metadata(task: ChapterTask, content: ModuleContent) -> dict:
    """读取并做结构检查图表元数据；内容正确性由导出前 AI 审核保证。"""
    try:
        metadata = json.loads(_chart_metadata_source(content) or "{}")
    except (TypeError, ValueError) as exc:
        raise TemplateAssemblyError(f"模块 {task.title} 的图表元数据无法读取") from exc
    if not isinstance(metadata, dict):
        raise TemplateAssemblyError(f"模块 {task.title} 的图表元数据无效")
    return metadata


def _chart_metadata_source(content: ModuleContent) -> str:
    """图表元数据取自 content；旧版模块曾存于 evidence_json，做一次兼容回退。"""
    return (content.content or "").strip() or (getattr(content, "evidence_json", "") or "").strip()


def _verified_chart_files(workdir: Path, task: ChapterTask, content: ModuleContent,
                          verified_chart_metadata: bool, *, metadata: dict | None = None,
                          source_formatter: SourceTextFormatter | None = None) -> list[str]:
    metadata = metadata if metadata is not None else json.loads(_chart_metadata_source(content) or "{}")
    if not isinstance(metadata, dict):
        return []
    from .generate import MAX_CHART_PANELS, render_verified_chart

    charts = metadata.get("charts", [metadata])
    if not isinstance(charts, list) or not charts or len(charts) > MAX_CHART_PANELS:
        raise TemplateAssemblyError(f"模块 {task.title} 的子图元数据无效")
    if not verified_chart_metadata:
        if len(charts) == 1:
            return []
        raise TemplateAssemblyError(f"模块 {task.title} 的多图元数据尚未通过导出审核")
    files = []
    for index, chart in enumerate(charts, start=1):
        if not isinstance(chart, dict) or "charts" in chart:
            raise TemplateAssemblyError(f"模块 {task.title} 的第 {index} 张子图元数据无效")
        relative = f"contents/t{task.id}-chart-{index}.png"
        target = sandbox.output_path(workdir, relative)
        display_chart = source_formatter.chart(chart) if source_formatter else chart
        render_verified_chart(display_chart, task.instruction, target, document=True)
        files.append(relative)
    return files


def _chart_captions_for_manifest(metadata: dict) -> tuple[list[list[str]], list[str]]:
    """图下只保留编号图题：顶层 caption 作为整组图题，缺省时退回各子图 caption。"""
    from .report_text import caption_droppable_as_repeat, dedupe_leading_figure_number

    def caption_lines(item: dict) -> list[str]:
        value = item.get("caption")
        if not isinstance(value, str):
            return []
        value = dedupe_leading_figure_number(value)
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        # 带图号的图题必须保留（与图内标题相同也不视为冗余），否则图会失去编号；
        # 仅省略既无图号又与标题重复的图题。
        if lines and caption_droppable_as_repeat(str(item.get("title") or ""), value):
            return []
        return lines

    charts = metadata.get("charts", [metadata])
    common = caption_lines(metadata) if "charts" in metadata else []
    notes = [[] for _ in charts] if common else [caption_lines(chart) for chart in charts]
    return notes, common


def prepare_manifest(db: Session, project: Project, workdir: Path, *,
                     verified_chart_metadata: bool = False,
                     image_progress=None) -> dict[str, Any]:
    """导出模板、任务、当前模块和证据元数据，供确定性装配与审核使用。"""
    workdir = Path(workdir)
    (workdir / "contents").mkdir(parents=True, exist_ok=True)
    template_path = workdir / "template.docx"
    template_bytes = _template_bytes(db, project)
    template_path.write_bytes(template_bytes)
    base_doc = Document(io.BytesIO(template_bytes))
    signoff_confirmation = _prepare_signoff_confirmation(
        db, project, workdir, template_bytes, base_doc,
    )

    tasks = db.scalars(
        select(ChapterTask)
        .where(ChapterTask.project_id == project.id)
        .order_by(ChapterTask.sort, ChapterTask.id)
    ).all()
    current = {
        c.chapter_task_id: c
        for c in db.scalars(
            select(ModuleContent).where(
                ModuleContent.chapter_task_id.in_([t.id for t in tasks] or [0]),
                ModuleContent.is_current == True,  # noqa: E712
                ModuleContent.status == "ok",
            )
        ).all()
    }
    nodes = []
    data_file_names = db.scalars(
        select(ProjectFile.file_name).where(
            ProjectFile.project_id == project.id,
            ProjectFile.file_type == "data",
        ).order_by(ProjectFile.id)
    ).all()
    # 审核仍使用原内容及真实来源；导出副本以中文来源名称呈现，避免实现文件名进入报告。
    source_formatter, method_task = make_project_formatter(project, tasks, current, data_file_names)
    consolidate_methods = source_formatter.consolidate_methods
    editorial_changes = []
    image_total = sum(1 for task in tasks
                      if task.task_type == "image" and current.get(task.id) is not None)
    image_done = 0
    for task in tasks:
        content = current.get(task.id)
        node = {
            "id": task.id,
            "parent_id": task.parent_id,
            "level": task.level,
            "title": task.title,
            "type": task.task_type,
            "instruction": task.instruction,
            "template_title": getattr(task, "template_title", "") or task.title,
            "template_instruction": getattr(task, "template_instruction", "") or task.instruction,
            "slot_key": task.slot_key,
            "anchor_task_id": task.anchor_task_id,
            "sort": task.sort,
        }
        if task.task_type != "heading":
            if content is not None and (
                (getattr(content, "material_revision", 0) or 0)
                != (getattr(project, "material_revision", 0) or 0)
                or (getattr(content, "instruction_revision", 0) or 0)
                != (getattr(task, "instruction_revision", 0) or 0)
            ):
                raise TemplateAssemblyError(f"模块 {task.title} 的材料或指令已变化，请重新生成")
            node["missing"] = content is None or (
                task.task_type == "image" and not content.image_path
            )
            if content is not None:
                node["file"] = _node_content_file(workdir, task, content,
                                                   source_formatter=source_formatter,
                                                   methodology=bool(consolidate_methods and task.id == method_task.id))
                if task.task_type in {"text", "table"}:
                    editorial_changes.append({
                        "task_id": task.id,
                        "stored_content_sha256": hashlib.sha256(content.content.encode()).hexdigest(),
                        "display_content_sha256": hashlib.sha256((workdir / node["file"]).read_bytes()).hexdigest(),
                    })
                if task.task_type == "image" and not node["missing"]:
                    metadata = None
                    if verified_chart_metadata:
                        metadata = _validated_chart_metadata(task, content)
                    image_files = _verified_chart_files(workdir, task, content, verified_chart_metadata,
                                                        metadata=metadata, source_formatter=source_formatter)
                    if image_files:
                        node["image_files"] = image_files
                        display_metadata = source_formatter.chart(metadata or json.loads(content.content))
                        node["image_notes"], node["image_group_notes"] = _chart_captions_for_manifest(display_metadata)
                        image_done += 1
                        if image_progress is not None:
                            image_progress(image_done, image_total)
        nodes.append(node)

    template_nodes = [
        node for node in nodes
        if node["type"] != "heading" and not node.get("anchor_task_id")
    ]
    blank_slots = [node["id"] for node in template_nodes if not node.get("slot_key")]
    if blank_slots:
        raise TemplateAssemblyError(
            f"模板任务缺少稳定 slot_key：{blank_slots[:5]}，请重新分析模板"
        )
    slots = [node["slot_key"] for node in template_nodes]
    duplicates = sorted({slot for slot in slots if slots.count(slot) > 1})
    if duplicates:
        raise TemplateAssemblyError(
            f"模板任务 slot_key 不唯一：{duplicates[:3]}"
        )

    manifest = {
        "title": project.name,
        "template": "template.docx",
        "signature": _doc_signature(base_doc),
        "nodes": nodes,
        "signoff_confirmation": signoff_confirmation,
        "editorial_policy": {"version": 1, "shared_methods_centralized": consolidate_methods,
                             "methodology_task_id": method_task.id if consolidate_methods else None,
                             "display_copies": editorial_changes},
    }
    manifest["signature"] = _expected_signature(base_doc, nodes, _resolve_slots(base_doc, manifest))
    (workdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def _table_signature(table) -> list[list[str]]:
    return [[cell.text for cell in row.cells] for row in table.rows]


def _image_signatures(doc: Document) -> list[str]:
    result = []
    for shape in doc.inline_shapes:
        for blip in shape._inline.iter(qn("a:blip")):
            relationship = blip.get(qn("r:embed"))
            if relationship:
                result.append(hashlib.sha256(doc.part.related_parts[relationship].blob).hexdigest())
    return result


def manifest_outline(manifest: dict) -> str:
    lines = []
    for node in manifest["nodes"]:
        indent = "  " * (min(node["level"], 4) - 1)
        mark = ""
        if node["type"] != "heading":
            mark = f"（{node['type']}{'，缺内容' if node.get('missing') else ''}）"
        lines.append(f"{indent}{node['title']}{mark}")
    return "\n".join(lines)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _instruction_paragraphs(doc: Document) -> list[Paragraph]:
    return [
        paragraph
        for paragraph in doc.paragraphs
        if _INSTRUCTION_RE.match(paragraph.text.strip())
    ]


def _heading_candidates(doc: Document, title: str) -> list[Paragraph]:
    return [
        paragraph
        for paragraph in doc.paragraphs
        if paragraph.text.strip() == title
        and paragraph.style is not None
        and paragraph.style.name.startswith("Heading")
    ]


def _paragraph_section_paths(doc: Document) -> dict[Any, str]:
    paths: dict[Any, str] = {}
    stack: list[str] = []
    for paragraph in doc.paragraphs:
        style_name = paragraph.style.name if paragraph.style is not None else ""
        if style_name.startswith("Heading"):
            match = re.search(r"(\d+)$", style_name)
            level = int(match.group(1)) if match else 1
            stack = stack[: level - 1]
            stack.append(paragraph.text.strip())
            paths[paragraph._p] = " / ".join(stack)
        else:
            paths[paragraph._p] = " / ".join(stack)
    return paths


def _node_section_path(node: dict, node_map: dict[int, dict]) -> str:
    parts: list[str] = []
    parent_id = node.get("parent_id")
    seen = {node["id"]}
    while parent_id is not None:
        if parent_id in seen:
            raise TemplateAssemblyError(f"模块 {node['id']} 的章节父节点存在循环")
        seen.add(parent_id)
        if parent_id not in node_map:
            raise TemplateAssemblyError(f"模块 {node['id']} 的章节父节点 {parent_id} 不存在")
        parent = node_map[parent_id]
        if parent["type"] != "heading":
            raise TemplateAssemblyError(f"模块 {node['id']} 的章节父节点不是标题")
        parts.append(_template_title(parent))
        parent_id = parent.get("parent_id")
    return " / ".join(reversed(parts))


def _manifest_section_paths(nodes: list[dict]) -> dict[int, str]:
    """为旧数据补全标题路径：优先 task tree，缺 parent_id 时按 level/sort 恢复。"""
    node_map = {item["id"]: item for item in nodes}
    if len(node_map) != len(nodes):
        raise TemplateAssemblyError("模块 ID 重复，无法确定章节树")
    paths: dict[int, str] = {}
    stack: list[str] = []
    for node in nodes:
        tree_path = _node_section_path(node, node_map)
        if node["type"] == "heading":
            level = max(1, int(node.get("level", 1) or 1))
            parent = node_map.get(node.get("parent_id"))
            if parent is not None and level <= int(parent.get("level", 1) or 1):
                raise TemplateAssemblyError(f"标题 {node['title']} 不能挂在同级或更低级标题之下")
            if tree_path:
                paths[node["id"]] = tree_path
                stack = tree_path.split(" / ")
            else:
                stack = stack[: level - 1]
                paths[node["id"]] = " / ".join(stack)
            stack.append(_template_title(node))
        else:
            paths[node["id"]] = tree_path or " / ".join(stack)
    return paths


def _template_title(node: dict) -> str:
    return str(node.get("template_title") or node.get("title", "")).strip()


def _template_instruction(node: dict) -> str:
    return str(node.get("template_instruction") or node.get("instruction", ""))


def _stable_template_slots(doc: Document) -> dict[str, list[Paragraph]]:
    """从原模板重建 slot 身份，编辑后的指令和类型不参与定位。

    前置小节在红字模板中可没有 heading 任务，在 AI 写回模板中有 heading
    任务，因此为前置指令同时保留两种原始路径；已保存的 slot_key 决定采用哪种。
    """
    raw = io.BytesIO()
    doc.save(raw)
    front_titles = {
        block["text"] for block in template_parse.extract_structure(raw.getvalue())
        if block["kind"] == "front_title"
    }
    paragraph_paths = _paragraph_section_paths(doc)
    candidates: dict[str, list[Paragraph]] = {}
    occurrences: dict[tuple[str, str], int] = {}
    current_front = ""
    seen_heading = False

    def add(paragraph: Paragraph, path: str, kind: str) -> None:
        identity = (path, kind)
        occurrence = occurrences.get(identity, 0)
        occurrences[identity] = occurrence + 1
        key = template_parse._slot_key_for_task({"task_type": kind}, path, occurrence)
        candidates.setdefault(key, []).append(paragraph)

    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        style = paragraph.style.name if paragraph.style is not None else ""
        path = paragraph_paths.get(paragraph._p, "").replace(" / ", "/")
        if style.startswith("Heading"):
            seen_heading = True
            add(paragraph, path, "heading")
        elif not seen_heading and text in front_titles:
            current_front = text
            add(paragraph, text, "heading")
        elif _INSTRUCTION_RE.match(text) and not template_parse.is_note_instruction(text):
            kind = template_parse.classify_instruction(text)
            add(paragraph, path, kind)
            if not seen_heading and current_front:
                add(paragraph, current_front, kind)
    return candidates


def _expected_signature(doc: Document, nodes: list[dict], slots: dict[int, Any]) -> dict[str, Any]:
    """只替换已绑定标题的验收预期，原正文和未编辑标题仍逐段保留。"""
    signature = _doc_signature(doc)
    replacements = {
        slots[node["id"]]: str(node["title"]).strip()
        for node in nodes
        if node["type"] == "heading" and node["id"] in slots
    }
    signature["heading_titles"] = [
        replacements.get(paragraph._p, paragraph.text.strip())
        for paragraph in doc.paragraphs
        if paragraph.style is not None
        and paragraph.style.name.startswith("Heading")
        and paragraph.text.strip()
    ]
    signature["static_paragraphs"] = [
        replacements.get(paragraph._p, paragraph.text)
        for paragraph in doc.paragraphs
        if paragraph.text.strip() and not _INSTRUCTION_RE.match(paragraph.text.strip())
    ]
    return signature


def _resolve_slots(doc: Document, manifest: dict) -> dict[int, Any]:
    """按原始 slot 身份定位；旧数据按原章节路径、原指令和顺序补定位。

    跨章节的全局指令回退只允许在候选唯一时使用；否则模板位置已经不确定，
    不能依赖“拿第一个”这种偶然顺序继续导出。
    """
    paragraph_paths = _paragraph_section_paths(doc)
    stable_slots = _stable_template_slots(doc)
    instruction_paragraphs = _instruction_paragraphs(doc)
    ordered_template_paragraphs = [
        paragraph for paragraph in instruction_paragraphs
        if not template_parse.is_note_instruction(paragraph.text)
    ]
    ordered_template_nodes = [
        node for node in manifest["nodes"]
        if node["type"] != "heading" and not node.get("anchor_task_id")
    ]
    ordered_template_slots = {
        node["id"]: ordered_template_paragraphs[index]
        for index, node in enumerate(ordered_template_nodes)
        if index < len(ordered_template_paragraphs)
    }
    instruction_map: dict[str, list[Paragraph]] = {}
    for paragraph in instruction_paragraphs:
        instruction_map.setdefault(_norm(paragraph.text), []).append(paragraph)
    instruction_path_map: dict[tuple[str, str], list[Paragraph]] = {}
    for paragraph in instruction_paragraphs:
        instruction_path_map.setdefault(
            (paragraph_paths.get(paragraph._p, ""), _norm(paragraph.text)),
            [],
        ).append(paragraph)
    node_paths = _manifest_section_paths(manifest["nodes"])
    heading_map: dict[tuple[str, str], list[Paragraph]] = {}
    for node in manifest["nodes"]:
        if node.get("anchor_task_id") is not None:
            # 新增模块没有模板 slot，不能复用锚点模块的指令段落。
            continue
        if node["type"] == "heading":
            path = node_paths[node["id"]]
            title = _template_title(node)
            full_path = " / ".join(part for part in (path, title) if part)
            heading_map.setdefault((path, title), []).extend(
                paragraph
                for paragraph in _heading_candidates(doc, title)
                if paragraph_paths.get(paragraph._p, "") == full_path
            )

    used: set[Any] = set()
    slots: dict[int, Any] = {}
    for node in manifest["nodes"]:
        if node.get("anchor_task_id") is not None:
            continue
        candidates = [
            paragraph for paragraph in stable_slots.get(node.get("slot_key", ""), [])
            if paragraph._p not in used
        ]
        if candidates:
            # slot_key 采用原模板类型及标题路径；改名、改指令、改类型后仍绑定原段落。
            pass
        elif node["type"] == "heading":
            path = node_paths[node["id"]]
            title = _template_title(node)
            candidates = heading_map.get((path, title), [])
            if not candidates:
                # AI 写回的“说明”等前置小节可能使用普通段落样式。
                candidates = [
                    paragraph for paragraph in doc.paragraphs
                    if paragraph.text.strip() == title
                    and not paragraph_paths.get(paragraph._p, "")
                    and not path
                    and not _INSTRUCTION_RE.match(paragraph.text.strip())
                    and paragraph._p not in used
                ]
                if len(candidates) > 1:
                    raise TemplateAssemblyError(f"前置小节标题无法唯一定位：{title}")
        else:
            instruction = _norm(_template_instruction(node))
            path = node_paths[node["id"]]
            candidates = instruction_path_map.get((path, instruction), [])
            if not candidates:
                ordered = ordered_template_slots.get(node["id"])
                if ordered is not None and ordered._p not in used:
                    if _norm(ordered.text) != instruction:
                        raise TemplateAssemblyError(
                            "模板指令顺序兜底不匹配："
                            f"任务 {node['id']} 期望 {_template_instruction(node)[:80]}，"
                            f"模板为 {ordered.text.strip()[:80]}"
                        )
                    candidates = [ordered]
                else:
                    fallback = [
                        paragraph for paragraph in instruction_map.get(instruction, [])
                        if paragraph._p not in used
                    ]
                    if len(fallback) > 1:
                        raise TemplateAssemblyError(
                            "模板指令无法按完整章节路径唯一定位："
                            f"{_template_instruction(node)[:120]}"
                        )
                    candidates = fallback
        chosen = next((p for p in candidates if p._p not in used), None)
        if chosen is not None:
            used.add(chosen._p)
            slots[node["id"]] = chosen._p
    return slots


def _remove_runs(paragraph: Paragraph) -> None:
    for run in list(paragraph.runs):
        run._element.getparent().remove(run._element)


def _replace_heading_title(anchor_p, title: str) -> None:
    """保留标题段落、文字样式、书签及分节，仅更新显示文字。"""
    text_nodes = list(anchor_p.iter(qn("w:t")))
    if not text_nodes:
        Paragraph(anchor_p, anchor_p.getparent()).add_run(title)
        return
    text_nodes[0].text = title
    for text_node in text_nodes[1:]:
        text_node.text = ""
    for tag in ("w:tab", "w:br", "w:cr"):
        for element in list(anchor_p.iter(qn(tag))):
            element.getparent().remove(element)


def _insert_paragraph_after(element, text: str, ppr=None, parent=None) -> Any:
    new_p = OxmlElement("w:p")
    if ppr is not None:
        copied_ppr = deepcopy(ppr)
        for section_properties in list(copied_ppr.iter(qn("w:sectPr"))):
            section_properties.getparent().remove(section_properties)
        new_p.append(copied_ppr)
    element.addnext(new_p)
    paragraph = Paragraph(new_p, parent)
    paragraph.add_run(text)
    return new_p


def _replace_with_text(anchor_p, text: str) -> Any:
    parent = anchor_p.getparent()
    anchor_para = Paragraph(anchor_p, parent)
    original_ppr = anchor_p.pPr
    lines = [line.strip() for line in re.split(r"\n+", text or "") if line.strip()]
    if not lines:
        lines = ["（本模块内容待补充）"]
    _remove_runs(anchor_para)
    anchor_para.add_run(lines[0])
    previous = anchor_p
    for line in lines[1:]:
        previous = _insert_paragraph_after(previous, line, original_ppr, parent)
    _transfer_section_properties(anchor_p, previous)
    return previous


def _transfer_section_properties(source, target) -> None:
    if source is target or getattr(source, "pPr", None) is None:
        return
    properties = source.pPr.find(qn("w:sectPr"))
    if properties is not None:
        source.pPr.remove(properties)
        target.get_or_add_pPr().append(properties)


def _text_units(value: Any) -> float:
    """按中西文字宽估算换行负荷，仅影响排版，不改变单元格内容。"""
    return sum(1 if unicodedata.east_asian_width(char) in ("W", "F") else 0.5
               for char in str(value if value is not None else ""))


def _numeric_column_min_width(values: list, font_size: float = 10.5) -> int | None:
    """纯数值列按完整 token 留宽，不能把百分号拆到下一行。"""
    tokens = [str(value).strip() for value in values if value is not None and str(value).strip()]
    number = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?[%％]?"
    if not tokens or any(re.fullmatch(number, token) is None for token in tokens):
        return None
    # 数字使用偏保守的西文字宽，百分号比数字更宽；Table Grid 的左右
    # 默认边距各 5.4pt，另留 2pt 余量以兼容模板的西文字体替换。
    def token_width(token: str) -> float:
        return sum(1 if unicodedata.east_asian_width(char) in ("W", "F") else
                   0.9 if char == "%" else 0.3 if char in ".," else 0.5
                   for char in token) * font_size
    # 短人数列仍至少容纳两个中文表头字，避免为数值留宽后把长表头
    # 压成一字一行，反而让同一张表增加不必要的续页。
    return math.ceil(Pt(max(max(map(token_width, tokens)), 2 * font_size) + 10.8 + 2))


def _table_column_widths(columns: list, rows: list, available_width: int) -> list[int]:
    if not columns:
        return []
    # 数值列使用正文中最长完整数值的宽度，不因中文长表头而占走文字列
    # 的空间；剩余页面宽度再按文字负荷分配，保持总宽度与分节一致。
    loads = [max(4, _text_units(title),
                 *(_text_units(row[index]) for row in rows if index < len(row)))
             for index, title in enumerate(columns)]
    numeric = [_numeric_column_min_width([row[index] for row in rows if index < len(row)])
               for index in range(len(columns))]
    text_columns = [index for index, width in enumerate(numeric) if width is None]
    numeric_total = sum(width for width in numeric if width is not None)
    if numeric_total + len(text_columns) > available_width:
        raise TemplateAssemblyError("表格页面宽度不足以完整显示数值，请减少列数或增大页面可用宽度")
    minimum = min(Inches(0.55), available_width / len(columns) * 0.6)
    if text_columns:
        minimum = min(minimum, (available_width - numeric_total) / len(text_columns))
    widths = [int(minimum) if width is None else width for width in numeric]
    extra = available_width - sum(widths)
    flexible = text_columns or list(range(len(columns)))
    total_load = sum(loads[index] for index in flexible)
    for index in flexible:
        widths[index] += int(extra * loads[index] / total_load)
    widths[flexible[-1]] += available_width - sum(widths)
    return widths


def _table_row_height(values: list, widths: list[int], font_size: float) -> float:
    lines = []
    for index, width in enumerate(widths):
        value = values[index] if index < len(values) else ""
        capacity = max(1, (width / 12700 - 9) / font_size)
        lines.append(sum(max(1, math.ceil(_text_units(line) / capacity))
                         for line in str(value if value is not None else "").split("\n")))
    return max(lines, default=1) * font_size * 1.15 + 6


def _set_cell_text(cell, value: Any, *, header: bool = False, font_size: float = 10.5,
                   allow_split: bool = False) -> None:
    cell.text = "" if value is None else str(value)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    for paragraph in cell.paragraphs:
        paragraph.alignment = (WD_ALIGN_PARAGRAPH.CENTER if header or _text_units(value) <= 8
                               else WD_ALIGN_PARAGRAPH.LEFT)
        formatting = paragraph.paragraph_format
        formatting.first_line_indent = 0
        formatting.left_indent = formatting.right_indent = 0
        formatting.space_before = formatting.space_after = Pt(2)
        formatting.line_spacing = 1.15
        formatting.keep_with_next = header
        formatting.keep_together = False
        formatting.widow_control = not allow_split
        # 中文模板的正文网格可能以 24–28pt 强制对齐；表格使用自己的行距，
        # 否则设置小号字体后仍会把一个记录撑到整页。
        grid = OxmlElement("w:snapToGrid")
        grid.set(qn("w:val"), "false")
        paragraph._p.get_or_add_pPr().append(grid)
        for run in paragraph.runs:
            run.font.size = Pt(font_size)
            run.bold = header


def _content_notes(payload: dict) -> list[str]:
    """表注/图注使用明确的字符串合同，不丢弃无法理解的备注。"""
    notes: list[str] = []
    note = payload.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise TemplateAssemblyError("note 必须是字符串")
        notes.extend(line.strip() for line in re.split(r"\n+", note) if line.strip())
    extra = payload.get("notes", [])
    if not isinstance(extra, list) or any(not isinstance(item, str) for item in extra):
        raise TemplateAssemblyError("notes 必须是字符串数组")
    for item in extra:
        notes.extend(line.strip() for line in re.split(r"\n+", item) if line.strip())
    return list(dict.fromkeys(notes))


def _insert_table_after(doc: Document, anchor_p, payload: str) -> Any:
    obj = json.loads(payload)
    table_data = obj.get("table", obj)
    columns = table_data.get("columns", [])
    rows = table_data.get("rows", [])
    if not columns or not rows:
        raise TemplateAssemblyError("表格内容缺少 columns/rows")
    parent = anchor_p.getparent()
    anchor_para = Paragraph(anchor_p, parent)
    _remove_runs(anchor_para)
    title = str(table_data.get("title", "") or "").strip()
    if title:
        anchor_para.add_run(title)
    anchor_para.paragraph_format.keep_with_next = True
    anchor_para.paragraph_format.keep_together = True
    anchor_para.paragraph_format.space_after = Pt(6)
    section = _anchor_section(doc, anchor_p)
    available_width = section.page_width - section.left_margin - section.right_margin
    if available_width <= 0:
        raise TemplateAssemblyError("表格所在分节没有可用的页面空间")
    widths = _table_column_widths(columns, rows, available_width)
    font_size = 10.5
    table = doc.add_table(rows=1 + len(rows), cols=len(columns))
    table.style = "Table Grid"
    table.autofit = False
    for column, width in zip(table.columns, widths):
        column.width = width
    table_width = table._tbl.tblPr.find(qn("w:tblW"))
    table_width.set(qn("w:type"), "dxa")
    table_width.set(qn("w:w"), str(round(available_width / 635)))
    header_properties = table.rows[0]._tr.get_or_add_trPr()
    header_properties.append(OxmlElement("w:tblHeader"))
    header_properties.append(OxmlElement("w:cantSplit"))
    for col, value in enumerate(columns):
        _set_cell_text(table.rows[0].cells[col], value, header=True, font_size=font_size)
    page_height = (section.page_height - section.top_margin - section.bottom_margin) / 12700
    maximum_row_height = page_height - _table_row_height(columns, widths, font_size) - 12
    for row_index, row in enumerate(rows, start=1):
        # 普通记录作为整体移至下一页。超过一页的长记录必须允许续页，
        # 否则 Word/LibreOffice 可能将内容裁切或留下无法排版的空页。
        allow_split = _table_row_height(row, widths, font_size) > maximum_row_height
        if not allow_split:
            table.rows[row_index]._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
        for col, value in enumerate(row[: len(columns)]):
            _set_cell_text(table.rows[row_index].cells[col], value, font_size=font_size,
                           allow_split=allow_split)
    for row in table.rows:
        for cell, width in zip(row.cells, widths):
            cell.width = width
    table._tbl.getparent().remove(table._tbl)
    anchor_p.addnext(table._tbl)
    # 留一个明确的尾部段落作为后续插入任务的锚点，避免插入内容落到表格标题和表格之间。
    tail = OxmlElement("w:p")
    table._tbl.addnext(tail)
    notes = _content_notes(table_data)
    if table_data is not obj:
        notes = list(dict.fromkeys([*notes, *_content_notes(obj)]))
    paragraph = Paragraph(tail, parent)
    if notes:
        paragraph.add_run(notes[0])
        for note in notes[1:]:
            tail = _insert_paragraph_after(tail, note, None, parent)
    _transfer_section_properties(anchor_p, tail)
    return tail


def _anchor_section(doc: Document, anchor_p) -> Section:
    """Word 分节属性在节末；锚点后的第一个 sectPr 属于该锚点所在节。"""
    found = False
    for element in doc.element.body.iter():
        if element is anchor_p:
            found = True
        if found and element.tag == qn("w:sectPr"):
            return Section(element, doc.part)
    raise TemplateAssemblyError("图片锚点不在模板正文的有效分节中")


def _insert_image_after(doc: Document, anchor_p, image_path: Path, *,
                        section: Section | None = None, page_break: bool = False) -> Any:
    parent = anchor_p.getparent()
    paragraph_parent = doc._body if parent is doc.element.body else parent
    anchor_para = Paragraph(anchor_p, paragraph_parent)
    section = section or _anchor_section(doc, anchor_p)
    width = min(Inches(6.2), section.page_width - section.left_margin - section.right_margin)
    # 预留图片所在行的基线空间，避免满页高的行被 Word 裁切或推出额外空页。
    height = section.page_height - section.top_margin - section.bottom_margin - Inches(0.4)
    if width <= 0 or height <= 0:
        raise TemplateAssemblyError("图片所在分节没有可用的页面空间")
    image = Image.from_file(str(image_path))
    scale = min(width / image.width, height / image.height, 1.0)
    _remove_runs(anchor_para)
    run = anchor_para.add_run()
    if page_break:
        run.add_break(WD_BREAK.PAGE)
    run.add_picture(str(image_path), width=int(image.width * scale), height=int(image.height * scale))
    anchor_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    anchor_para.paragraph_format.line_spacing = 1
    anchor_para.paragraph_format.space_before = 0
    anchor_para.paragraph_format.space_after = 0
    anchor_para.paragraph_format.keep_with_next = False
    anchor_para.paragraph_format.keep_together = True
    return anchor_p


def _image_files(node: dict, workdir: Path) -> list[Path]:
    files = node.get("image_files", [node["file"]])
    if not isinstance(files, list) or not files or any(not isinstance(value, str) for value in files):
        raise TemplateAssemblyError(f"模块 {node['id']} 的图片文件清单无效")
    return [sandbox.output_path(workdir, value) for value in files]


def _image_caption_lines(node: dict, count: int) -> tuple[list[list[str]], list[str]]:
    notes = node.get("image_notes", [[] for _ in range(count)])
    common = node.get("image_group_notes", [])
    if (not isinstance(notes, list) or len(notes) != count
            or any(not isinstance(group, list) or any(not isinstance(value, str) for value in group)
                   for group in notes)
            or not isinstance(common, list) or any(not isinstance(value, str) for value in common)):
        raise TemplateAssemblyError(f"模块 {node['id']} 的图题清单无效")
    return notes, common


def _insert_caption_lines_after(doc: Document, tail, notes: list[str], *,
                                keep_cluster: bool = False) -> Any:
    for index, note in enumerate(notes):
        tail = _insert_paragraph_after(tail, note, None, tail.getparent())
        paragraph = Paragraph(tail, doc._body)
        formatting = paragraph.paragraph_format
        formatting.first_line_indent = formatting.left_indent = formatting.right_indent = 0
        formatting.space_before = Pt(3)
        formatting.space_after = Pt(6)
        formatting.line_spacing = 1.15
        # 仅连接当前图的完整说明簇，末条不能连接下一张图或后续正文。
        formatting.keep_with_next = keep_cluster and index < len(notes) - 1
        formatting.keep_together = keep_cluster
        formatting.widow_control = True
        grid = OxmlElement("w:snapToGrid")
        grid.set(qn("w:val"), "false")
        tail.get_or_add_pPr().append(grid)
        for run in paragraph.runs:
            run.font.size = Pt(10.5)
    return tail


def _chart_caption_cluster_fits(section: Section, image_path: Path, notes: list[str]) -> bool:
    """能装入一页的图与说明保持整体；超长说明仍允许正常续页。"""
    width = min(Inches(6.2), section.page_width - section.left_margin - section.right_margin)
    page_height = section.page_height - section.top_margin - section.bottom_margin
    image = Image.from_file(str(image_path))
    scale = min(width / image.width, (page_height - Inches(0.4)) / image.height, 1.0)
    # 与图注的10.5pt、1.15倍行距及前后3/6pt间距一致，保留基线余量。
    capacity = max(1, width / 12700 / 10.5)
    notes_height = sum(sum(max(1, math.ceil(_text_units(line) / capacity))
                           for line in note.split("\n")) * 10.5 * 1.15 + 9
                       for note in notes)
    return image.height * scale / 12700 + notes_height + 18 < page_height / 12700


def _insert_image_group_after(doc: Document, anchor_p, image_paths: list[Path], *,
                              notes: list[list[str]] | None = None,
                              group_notes: list[str] | None = None) -> Any:
    section = _anchor_section(doc, anchor_p)
    tail = anchor_p
    notes = notes if notes is not None else [[] for _ in image_paths]
    for index, image_path in enumerate(image_paths):
        if index:
            tail = _insert_paragraph_after(tail, "", getattr(anchor_p, "pPr", None), tail.getparent())
            Paragraph(tail, tail.getparent()).paragraph_format.page_break_before = False
        _insert_image_after(doc, tail, image_path, section=section)
        captions = list(notes[index])
        if index == len(image_paths) - 1:
            captions.extend(group_notes or [])
        keep_cluster = bool(captions) and _chart_caption_cluster_fits(section, image_path, captions)
        if captions:
            Paragraph(tail, doc._body).paragraph_format.keep_with_next = True
        tail = _insert_caption_lines_after(doc, tail, captions, keep_cluster=keep_cluster)
    _transfer_section_properties(anchor_p, tail)
    return tail


def _insertion_children(nodes: list[dict]) -> dict[int, list[dict]]:
    node_map = {node["id"]: node for node in nodes}
    if len(node_map) != len(nodes):
        raise TemplateAssemblyError("模块 ID 重复，无法唯一定位锚点")
    children: dict[int, list[dict]] = {}
    for index, node in enumerate(nodes):
        anchor_id = node.get("anchor_task_id")
        if anchor_id is None:
            continue
        if node["type"] == "heading":
            raise TemplateAssemblyError("不支持将标题作为新增内容模块")
        if anchor_id not in node_map:
            raise TemplateAssemblyError(f"模块 {node['id']} 的插入锚点 {anchor_id} 不存在")
        if node_map[anchor_id]["type"] == "heading":
            raise TemplateAssemblyError(f"模块 {node['id']} 的插入锚点不能是标题")
        seen = {node["id"]}
        current = anchor_id
        while current is not None:
            if current in seen:
                raise TemplateAssemblyError(f"模块 {node['id']} 的插入锚点存在循环")
            seen.add(current)
            anchor = node_map.get(current)
            if anchor is None:
                raise TemplateAssemblyError(f"模块 {node['id']} 的插入锚点 {current} 不存在")
            current = anchor.get("anchor_task_id")
        children.setdefault(anchor_id, []).append((node.get("sort", index), index, node))
    return {
        anchor_id: [item[2] for item in sorted(items, key=lambda item: (item[0], item[1]))]
        for anchor_id, items in children.items()
    }


def _validate_body_contents(doc: Document, manifest: dict, workdir: Path) -> None:
    """结构门槛之外核验全部正文、表格和图像，防止装配时覆盖原模块。"""
    signature = manifest.get("signature", {})
    expected_paragraphs = Counter(signature.get("static_paragraphs", []))
    expected_tables = Counter(
        json.dumps(value, ensure_ascii=False) for value in signature.get("table_contents", [])
    )
    expected_images = Counter(signature.get("image_contents", []))
    for node in manifest["nodes"]:
        if node["type"] == "heading":
            continue
        if node.get("missing") or not node.get("file"):
            expected_paragraphs["（本模块内容待补充）"] += 1
            continue
        path = Path(workdir) / node["file"]
        if node["type"] == "text":
            lines = [line.strip() for line in re.split(r"\n+", path.read_text(encoding="utf-8")) if line.strip()]
            expected_paragraphs.update(lines or ["（本模块内容待补充）"])
        elif node["type"] == "table":
            payload = json.loads(path.read_text(encoding="utf-8"))
            table = payload.get("table", payload)
            columns = table.get("columns", [])
            width = len(columns)
            expected = [["" if value is None else str(value) for value in columns]]
            for row in table.get("rows", []):
                expected.append([
                    "" if value is None else str(value)
                    for value in list(row[:width]) + [""] * max(0, width - len(row))
                ])
            expected_tables[json.dumps(expected, ensure_ascii=False)] += 1
            title = str(table.get("title", "") or "").strip()
            if title:
                expected_paragraphs[title] += 1
            notes = _content_notes(table)
            if table is not payload:
                notes = list(dict.fromkeys([*notes, *_content_notes(payload)]))
            expected_paragraphs.update(notes)
        elif node["type"] == "image":
            image_files = _image_files(node, workdir)
            expected_images.update(hashlib.sha256(image.read_bytes()).hexdigest() for image in image_files)
            notes, common = _image_caption_lines(node, len(image_files))
            expected_paragraphs.update(note for group in notes for note in group)
            expected_paragraphs.update(common)
    actual_paragraphs = Counter(paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip())
    actual_tables = Counter(json.dumps(_table_signature(table), ensure_ascii=False) for table in doc.tables)
    actual_images = Counter(_image_signatures(doc))
    missing_paragraphs = expected_paragraphs - actual_paragraphs
    if missing_paragraphs:
        raise TemplateAssemblyError("装配后原正文或模块正文丢失：" + next(iter(missing_paragraphs))[:120])
    if expected_tables - actual_tables:
        raise TemplateAssemblyError("装配后原表格或模块表格内容丢失")
    if expected_images - actual_images:
        raise TemplateAssemblyError("装配后原图片或模块图片丢失")


def _remove_residual_instructions(doc: Document) -> int:
    removed = 0
    for paragraph in list(_instruction_paragraphs(doc)):
        if template_parse.is_note_instruction(paragraph.text):
            paragraph._p.getparent().remove(paragraph._p)
            removed += 1
        else:
            raise TemplateAssemblyError(
                f"仍有未匹配的模板编写指令：{paragraph.text.strip()[:120]}"
            )
    return removed


def _toc_paragraphs(nodes: list[dict]) -> list[Any]:
    """缓存当前标题而非旧页码；目录域不显示页码，打开时可以安全更新。"""
    headings = [node for node in nodes if node["type"] == "heading"]
    if not headings:
        raise TemplateAssemblyError("模板包含目录但当前任务没有标题")
    paragraphs = []
    for index, node in enumerate(headings):
        paragraph = OxmlElement("w:p")
        properties = OxmlElement("w:pPr")
        style = OxmlElement("w:pStyle")
        style.set(qn("w:val"), f"TOC{min(9, max(1, int(node.get('level', 1) or 1)))}")
        properties.append(style)
        paragraph.append(properties)
        if index == 0:
            for tag, value in (("w:fldChar", "begin"), ("w:instrText", None), ("w:fldChar", "separate")):
                run = OxmlElement("w:r")
                item = OxmlElement(tag)
                if value:
                    item.set(qn("w:fldCharType"), value)
                    if value == "begin":
                        item.set(qn("w:dirty"), "true")
                else:
                    item.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                    item.text = ' TOC \\o "1-9" \\h \\z \\u \\n "1-9" '
                run.append(item)
                paragraph.append(run)
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = str(node["title"]).strip()
        run.append(text)
        paragraph.append(run)
        if index == len(headings) - 1:
            run = OxmlElement("w:r")
            ending = OxmlElement("w:fldChar")
            ending.set(qn("w:fldCharType"), "end")
            run.append(ending)
            paragraph.append(run)
        paragraphs.append(paragraph)
    return paragraphs


def _field_paragraph(element):
    while element is not None and element.tag != qn("w:p"):
        element = element.getparent()
    return element


def _replace_toc_range(start, end, nodes: list[dict]) -> None:
    """只清除 TOC 域的缓存范围，保留目录前后文字、书签及分节属性。"""
    first, last = _field_paragraph(start), _field_paragraph(end)
    if first is None or last is None or first.getparent() is not last.getparent():
        raise TemplateAssemblyError("目录域结构不完整，无法安全刷新")
    parent = first.getparent()
    blocks = list(parent)
    first_index, last_index = blocks.index(first), blocks.index(last)
    if first_index > last_index:
        raise TemplateAssemblyError("目录域起止顺序错误")
    old = blocks[first_index:last_index + 1]
    if any(element.tag != qn("w:p") for element in old):
        raise TemplateAssemblyError("目录域混有非段落内容，无法安全刷新")
    if any(element.tag in (qn("w:drawing"), qn("w:pict")) for paragraph in old for element in paragraph.iter()):
        raise TemplateAssemblyError("目录域混有图片，无法安全刷新")
    if any(paragraph.pPr is not None and paragraph.pPr.find(qn("w:sectPr")) is not None for paragraph in old[:-1]):
        raise TemplateAssemblyError("目录域内部包含分节，无法安全刷新")

    def child_under(paragraph, element):
        while element.getparent() is not paragraph:
            element = element.getparent()
        return element

    start_child, end_child = child_under(first, start), child_under(last, end)
    prefix = [deepcopy(element) for element in list(first)[:list(first).index(start_child)] if element.tag != qn("w:pPr")]
    suffix = [deepcopy(element) for element in list(last)[list(last).index(end_child) + 1:] if element.tag != qn("w:pPr")]
    # 所有书签统一保留一次；边界之外的显示文字照常保留。
    for child in [*prefix, *suffix]:
        for bookmark in list(child.iter()):
            if bookmark.tag in (qn("w:bookmarkStart"), qn("w:bookmarkEnd")):
                if bookmark is child:
                    (prefix if child in prefix else suffix).remove(child)
                else:
                    bookmark.getparent().remove(bookmark)
    preserved = []
    for paragraph in old:
        skeleton = OxmlElement("w:p")
        if paragraph.pPr is not None and paragraph.pPr.find(qn("w:sectPr")) is not None:
            skeleton.append(deepcopy(paragraph.pPr))
        for element in paragraph.iter():
            if element.tag in (qn("w:bookmarkStart"), qn("w:bookmarkEnd")):
                skeleton.append(deepcopy(element))
        if list(skeleton):
            preserved.append(skeleton)
    replacement = _toc_paragraphs(nodes)
    def add_paragraph_properties(target, source):
        if source.pPr is not None:
            properties = deepcopy(source.pPr)
            for section in list(properties.iter(qn("w:sectPr"))):
                section.getparent().remove(section)
            target.append(properties)
    if prefix:
        before = OxmlElement("w:p")
        add_paragraph_properties(before, first)
        before.extend(prefix)
        replacement.insert(0, before)
    if suffix:
        after = OxmlElement("w:p")
        add_paragraph_properties(after, last)
        after.extend(suffix)
        replacement.append(after)
    replacement.extend(preserved)
    for element in old:
        parent.remove(element)
    for index, element in enumerate(replacement, start=first_index):
        parent.insert(index, element)


def _refresh_toc(doc: Document, nodes: list[dict]) -> int:
    """刷新复杂/简单目录域；明确识别的手写目录转为同一目录域合同。"""
    stack: list[dict] = []
    ranges = []
    for element in doc.element.body.iter():
        if element.tag == qn("w:fldSimple") and re.match(r"\s*TOC\b", element.get(qn("w:instr"), ""), re.I):
            ranges.append((element, element))
        elif element.tag == qn("w:fldChar"):
            kind = element.get(qn("w:fldCharType"))
            if kind == "begin":
                stack.append({"start": element, "toc": False})
            elif kind == "end" and stack:
                field = stack.pop()
                if field["toc"]:
                    ranges.append((field["start"], element))
        elif element.tag == qn("w:instrText") and stack and re.match(r"\s*TOC\b", element.text or "", re.I):
            stack[-1]["toc"] = True
    if any(field["toc"] for field in stack):
        raise TemplateAssemblyError("目录域缺少结束标记，无法安全刷新")
    for start, end in reversed(ranges):
        _replace_toc_range(start, end, nodes)
    if not ranges:
        paragraphs = doc.paragraphs
        header_index = next((index for index, paragraph in enumerate(paragraphs)
                             if re.fullmatch(r"目\s*录", paragraph.text.strip())), None)
        entries = []
        if header_index is not None:
            for paragraph in paragraphs[header_index + 1:]:
                text = paragraph.text.strip()
                if paragraph.style is not None and paragraph.style.name.startswith("Heading"):
                    break
                if not text:
                    continue
                toc_style = paragraph.style is not None and paragraph.style.name.lower().startswith("toc")
                if toc_style or re.fullmatch(r".+?(?:\t|\.{2,}|…+|\s+)\d+", text):
                    entries.append(paragraph)
                else:
                    break
        if entries:
            first, last = entries[0]._p, entries[-1]._p
            # 静态目录没有域边界，增加临时边界后复用同一保留结构的替换逻辑。
            opening = OxmlElement("w:r")
            closing = OxmlElement("w:r")
            first.insert(1 if first.pPr is not None else 0, opening)
            last.append(closing)
            _replace_toc_range(opening, closing, nodes)
            ranges.append((opening, closing))
    if ranges:
        settings = doc.settings.element
        update = settings.find(qn("w:updateFields"))
        if update is None:
            update = OxmlElement("w:updateFields")
            settings.append(update)
        update.set(qn("w:val"), "true")
    return len(ranges)


def _refresh_report_dates(doc: Document, generated_on: date) -> list[dict[str, Any]]:
    """更新可确认的报告制作落款日期，绝不据此推定调查或材料日期。

    制作日期必须是独立年月/年月日段落，前一非空段落为机构/编写组落款；
    位置仅限第一个正文标题之前，或全文最后一个非空段落。
    """
    paragraphs = doc.paragraphs
    first_heading = next((index for index, paragraph in enumerate(paragraphs)
                          if paragraph.style is not None and paragraph.style.name.startswith("Heading")
                          and paragraph.text.strip()), len(paragraphs))
    last_nonempty = next((index for index in range(len(paragraphs) - 1, -1, -1)
                          if paragraphs[index].text.strip()), None)
    updates = []
    for index, paragraph in enumerate(paragraphs):
        match = _REPORT_DATE_RE.fullmatch(paragraph.text)
        if match is None or not (index < first_heading or index == last_nonempty):
            continue
        # 无效日期也不能被悄悄纠正并伪装成模板中的制作日期。
        try:
            date(int(match[1]), int(match[2]), int(match[3] or 1))
        except ValueError:
            continue
        author_index = next((previous for previous in range(index - 1, -1, -1)
                             if paragraphs[previous].text.strip()), None)
        if author_index is None:
            continue
        author = paragraphs[author_index].text.strip()
        if len(author) > 80 or re.search(r"[。；！？]", author) or not _SIGNOFF_AUTHOR_RE.search(author):
            continue
        # 段落列表不包含表格，故再确认落款之间及文末没有隐藏的表格/图片。
        body = doc.element.body
        author_position, date_position = body.index(paragraphs[author_index]._p), body.index(paragraph._p)
        between = list(body)[author_position + 1:date_position]
        if any(element.tag != qn("w:p") or bool(list(element.iter(qn("w:drawing")))) for element in between):
            continue
        if index == last_nonempty and index >= first_heading:
            tail = list(body)[date_position + 1:]
            if any(element.tag not in (qn("w:p"), qn("w:sectPr"))
                   or bool(list(element.iter(qn("w:drawing")))) for element in tail):
                continue
        rendered = f"{generated_on.year}年{generated_on.month}月"
        if match[3] is not None:
            rendered += f"{generated_on.day}日"
        original = paragraph.text
        _replace_heading_title(paragraph._p, rendered)
        updates.append({"paragraph_index": index, "before": original, "after": rendered,
                        "location": "front_signoff" if index < first_heading else "final_signoff"})
    return updates


_CONFIRMED_ORGANIZATION = "贵州师范大学基础教育研究院"
_LEGACY_SIGNOFF_ORGANIZATION = "贵州省基础教育研究院"
_SIGNOFF_SOURCE_FILE = "signoff-source.docx"


def _confirms_signoff_organization(doc: Document) -> bool:
    paragraphs = doc.paragraphs
    exact = [index for index, paragraph in enumerate(paragraphs)
             if paragraph.text.strip() == _CONFIRMED_ORGANIZATION]
    organizer = [index for index, paragraph in enumerate(paragraphs)
                 if re.search(re.escape(_CONFIRMED_ORGANIZATION) + r"\s*组织开展(?:了)?", paragraph.text)]
    return bool(exact and any(index not in exact for index in organizer))


def _prepare_signoff_confirmation(db: Session, project: Project, workdir: Path,
                                  template_bytes: bytes, base_doc: Document) -> dict | None:
    """随导出保存原件确认副本，不改变存储原件或装配模板。"""
    if not project.parsed_template_path or _confirms_signoff_organization(base_doc):
        return None
    if not any(paragraph.text.strip() == _CONFIRMED_ORGANIZATION for paragraph in base_doc.paragraphs):
        return None
    try:
        original_bytes = _original_template_bytes(db, project)
        original_doc = Document(io.BytesIO(original_bytes))
    except Exception:
        logger.warning("original template unavailable for signoff confirmation", exc_info=True)
        return None
    if not _confirms_signoff_organization(original_doc):
        return None
    (workdir / _SIGNOFF_SOURCE_FILE).write_bytes(original_bytes)
    return {
        "file": _SIGNOFF_SOURCE_FILE,
        "sha256": hashlib.sha256(original_bytes).hexdigest(),
        "target_template_sha256": hashlib.sha256(template_bytes).hexdigest(),
    }


def _load_signoff_confirmation(template_path: Path, manifest: dict) -> Document | None:
    confirmation = manifest.get("signoff_confirmation")
    if not isinstance(confirmation, dict) or confirmation.get("file") != _SIGNOFF_SOURCE_FILE:
        return None
    source_path = template_path.parent / _SIGNOFF_SOURCE_FILE
    try:
        if source_path.is_symlink():
            return None
        source_bytes = source_path.read_bytes()
        if (hashlib.sha256(template_path.read_bytes()).hexdigest() != confirmation.get("target_template_sha256")
                or hashlib.sha256(source_bytes).hexdigest() != confirmation.get("sha256")):
            return None
        source_doc = Document(io.BytesIO(source_bytes))
    except Exception:
        logger.warning("signoff confirmation copy unavailable or invalid", exc_info=True)
        return None
    return source_doc if _confirms_signoff_organization(source_doc) else None


def _repair_confirmed_signoff(doc: Document, confirmation_doc: Document | None = None) -> list[dict[str, Any]]:
    """只以封面与组织开展正文的独立证据修正前置说明中的旧落款。"""
    paragraphs = doc.paragraphs
    source_doc = confirmation_doc if confirmation_doc is not None else doc
    if (not _confirms_signoff_organization(source_doc)
            or not any(paragraph.text.strip() == _CONFIRMED_ORGANIZATION for paragraph in paragraphs)):
        return []
    first_heading = next((index for index, paragraph in enumerate(paragraphs)
                          if paragraph.style is not None and paragraph.style.name.startswith("Heading")),
                         len(paragraphs))
    updates = []
    for index, paragraph in enumerate(paragraphs[:first_heading]):
        if paragraph.text.strip() != _LEGACY_SIGNOFF_ORGANIZATION:
            continue
        following = next((candidate for candidate in paragraphs[index + 1:] if candidate.text.strip()), None)
        date_match = _REPORT_DATE_RE.fullmatch(following.text.strip()) if following is not None else None
        if date_match is None:
            continue
        try:
            date(int(date_match[1]), int(date_match[2]), int(date_match[3] or 1))
        except ValueError:
            continue
        body = doc.element.body
        between = list(body)[body.index(paragraph._p) + 1:body.index(following._p)]
        if any(element.tag != qn("w:p") or any(
                child.tag in (qn("w:drawing"), qn("w:pict")) for child in element.iter())
               for element in between):
            continue
        original = paragraph.text
        if any(_LEGACY_SIGNOFF_ORGANIZATION in (run.text or "") for run in paragraph.runs):
            for run in paragraph.runs:
                run.text = (run.text or "").replace(_LEGACY_SIGNOFF_ORGANIZATION, _CONFIRMED_ORGANIZATION)
        else:
            _replace_heading_title(paragraph._p, _CONFIRMED_ORGANIZATION)
        updates.append({"paragraph_index": index, "before": original,
                        "after": _CONFIRMED_ORGANIZATION, "location": "front_signoff",
                        "confirmation_source": "original_template" if confirmation_doc is not None else "assembly_template"})
    return updates


def _has_page_boundary_before(paragraph: Paragraph) -> bool:
    """已有分页或非连续分节时不叠加分页，避免封面后的额外空页。"""
    if paragraph.paragraph_format.page_break_before:
        return True
    previous = paragraph._p.getprevious()
    while previous is not None:
        if previous.tag != qn("w:p"):
            return False
        properties = getattr(previous, "pPr", None)
        section = properties.find(qn("w:sectPr")) if properties is not None else None
        if section is not None:
            kind = section.find(qn("w:type"))
            if kind is None or kind.get(qn("w:val")) != "continuous":
                return True
        # 只有正文之后的末尾分页有效，段内分页后仍有文字不算页边界。
        boundary = False
        has_content = False
        for element in previous.iter():
            if (element.tag == qn("w:t") and (element.text or "").strip()) or element.tag in (qn("w:drawing"), qn("w:pict")):
                has_content, boundary = True, False
            elif element.tag == qn("w:br") and element.get(qn("w:type")) == "page":
                boundary = True
        if boundary:
            return True
        if has_content:
            return False
        previous = previous.getprevious()
    return False


def _format_front_sections(doc: Document) -> None:
    stream = io.BytesIO()
    doc.save(stream)
    front_titles = {_norm(block["text"]) for block in template_parse.extract_structure(stream.getvalue())
                    if block["kind"] == "front_title"}
    preceding_content = False
    for paragraph in doc.paragraphs:
        if paragraph.style is not None and paragraph.style.name.startswith("Heading"):
            break
        title = _norm(paragraph.text)
        if title in front_titles:
            paragraph.paragraph_format.keep_with_next = True
            paragraph.paragraph_format.keep_together = True
            if (title in template_parse.FRONT_SECTION_TITLES and preceding_content
                    and not _has_page_boundary_before(paragraph)):
                paragraph.paragraph_format.page_break_before = True
        if paragraph.text.strip() or list(paragraph._p.iter(qn("w:drawing"))):
            preceding_content = True


def _format_front_signoff_layout(doc: Document) -> None:
    """说明使用紧凑正文流，最后一段与原机构落款、日期保持相邻。"""
    paragraphs = doc.paragraphs
    first_heading = next((index for index, paragraph in enumerate(paragraphs)
                          if paragraph.style is not None and paragraph.style.name.startswith("Heading")),
                         len(paragraphs))
    for title_index in range(first_heading):
        if _norm(paragraphs[title_index].text) != "说明":
            continue
        end = next((index for index in range(title_index + 1, first_heading)
                    if _norm(paragraphs[index].text) in (
                        *template_parse.FRONT_SECTION_TITLES, *template_parse.FRONT_EXCLUDE_TITLES)),
                   first_heading)
        nonempty = [index for index in range(title_index + 1, end) if paragraphs[index].text.strip()]
        if len(nonempty) < 3:
            continue
        author_index, date_index = nonempty[-2:]
        author, date_paragraph = paragraphs[author_index], paragraphs[date_index]
        date_match = _REPORT_DATE_RE.fullmatch(date_paragraph.text)
        if (not date_match
                or len(author.text.strip()) > 80 or re.search(r"[。；！？]", author.text)
                or not _SIGNOFF_AUTHOR_RE.search(author.text)):
            continue
        try:
            date(int(date_match[1]), int(date_match[2]), int(date_match[3] or 1))
        except ValueError:
            continue
        body = doc.element.body
        section_elements = list(body)[body.index(paragraphs[title_index]._p) + 1:body.index(date_paragraph._p) + 1]
        if any(element.tag != qn("w:p") or any(
                descendant.tag in (qn("w:drawing"), qn("w:pict")) for descendant in element.iter())
               for element in section_elements):
            continue
        content_indices = [index for index in nonempty if index < author_index]
        for index in range(title_index + 1, date_index + 1):
            paragraph = paragraphs[index]
            formatting = paragraph.paragraph_format
            formatting.space_before = 0
            formatting.space_after = 0
            formatting.line_spacing = 1
            formatting.keep_together = index >= author_index
            formatting.keep_with_next = index >= content_indices[-1] and index < date_index
            # 原说明正文网格与落款28pt固定行距会把两行落款推出孤页。
            grid = paragraph._p.get_or_add_pPr().find(qn("w:snapToGrid"))
            if grid is None:
                grid = OxmlElement("w:snapToGrid")
                paragraph._p.get_or_add_pPr().append(grid)
            grid.set(qn("w:val"), "false")
        author.paragraph_format.space_before = Pt(6)


def _format_terminal_text_flow(doc: Document, text_spans: dict[int, tuple]) -> dict[str, Any]:
    """终端纯文字遵循模板单倍行距，避免网格把少量续文推出近空白页。"""
    body = doc.element.body
    elements = [element for element in body if element.tag != qn("w:sectPr")]
    if not elements:
        return {}
    matched = next(((task_id, start, tail) for task_id, (start, tail) in text_spans.items()
                    if tail is elements[-1]), None)
    if matched is None:
        return {}
    task_id, start, tail = matched
    span = elements[elements.index(start):]
    if len(span) < 3 or any(element.tag != qn("w:p") for element in span):
        return {}
    grid = doc.sections[-1]._sectPr.find(qn("w:docGrid"))
    if grid is None or grid.get(qn("w:type")) != "lines":
        return {}
    paragraphs = [Paragraph(element, doc._body) for element in span]
    for paragraph in paragraphs:
        if not paragraph.text.strip() or _SIGNOFF_AUTHOR_RE.search(paragraph.text) or _REPORT_DATE_RE.fullmatch(paragraph.text):
            return {}
        if any(element.tag in tuple(qn("w:" + tag) for tag in (
                "drawing", "pict", "br", "cr", "sectPr", "fldSimple", "fldChar", "instrText",
                "footnoteReference", "endnoteReference", "object", "sym", "tab")) +
                (qn("m:oMath"), qn("m:oMathPara"))
               for element in paragraph._p.iter()):
            return {}
        ppr = paragraph._p.pPr
        if ppr is not None and any(ppr.find(qn("w:" + tag)) is not None
                                   for tag in ("spacing", "snapToGrid", "pageBreakBefore", "keepNext", "keepLines")):
            return {}
        style = paragraph.style
        visited = set()
        effective_spacing = None
        while style is not None and style.style_id not in visited:
            visited.add(style.style_id)
            style_ppr = style.element.pPr
            if style_ppr is not None and any(style_ppr.find(qn("w:" + tag)) is not None
                                             for tag in ("snapToGrid", "pageBreakBefore", "keepNext", "keepLines")):
                return {}
            if effective_spacing is None:
                effective_spacing = style.paragraph_format.line_spacing
            style = style.base_style
        if effective_spacing != 1.0:
            # 未找到明确的单倍模板样式时保守保留。
            return {}
    for paragraph in paragraphs:
        snap = OxmlElement("w:snapToGrid")
        snap.set(qn("w:val"), "false")
        paragraph._p.get_or_add_pPr().append(snap)
    return {"task_id": task_id, "paragraphs": len(paragraphs),
            "change": "terminal_text_uses_inherited_single_spacing_without_grid",
            "font_size_and_text_unchanged": True}


def assemble_template_docx(template_path: Path, manifest: dict, output_path: Path,
                           generated_on: date | None = None) -> dict[str, Any]:
    doc = Document(str(template_path))
    author_updates = _repair_confirmed_signoff(doc, _load_signoff_confirmation(template_path, manifest))
    _manifest_section_paths(manifest["nodes"])
    original = _doc_signature(doc)
    report_date = generated_on or date.today()
    date_updates = _refresh_report_dates(doc, report_date)
    refreshed_tocs = _refresh_toc(doc, manifest["nodes"])
    before = _doc_signature(doc)
    if any(before[key] != original[key] for key in ("sections", "section_properties", "geometry", "bookmark_names")):
        raise TemplateAssemblyError("制作日期或目录刷新改变了模板分节或书签结构")
    insertion_children = _insertion_children(manifest["nodes"])
    slots = _resolve_slots(doc, manifest)
    node_slots = dict(slots)
    nodes = manifest["nodes"]
    expected_signature = _expected_signature(doc, nodes, slots)
    manifest["signature"] = {**manifest.get("signature", {}), **expected_signature}

    for node in nodes:
        if node["type"] != "heading":
            continue
        anchor = slots.get(node["id"])
        if anchor is None:
            if str(node["title"]).strip() != _template_title(node):
                raise TemplateAssemblyError(f"编辑后的标题无法定位原模板：{_template_title(node)}")
            continue
        _replace_heading_title(anchor, str(node["title"]).strip())

    _format_front_sections(doc)

    used_instruction_paragraphs = {
        element
        for node_id, element in slots.items()
        if next(
            (node for node in nodes if node["id"] == node_id and node["type"] != "heading"),
            None,
        )
        is not None
    }
    unassigned = [
        paragraph
        for paragraph in _instruction_paragraphs(doc)
        if paragraph._p not in used_instruction_paragraphs
        and not template_parse.is_note_instruction(paragraph.text)
    ]
    if unassigned:
        raise TemplateAssemblyError(
            f"模板存在无法唯一匹配的编写指令：{unassigned[0].text.strip()[:120]}"
        )

    text_spans = {}

    def fill_node(node, anchor):
        if node.get("missing") or not node.get("file"):
            return _replace_with_text(anchor, "（本模块内容待补充）")
        path = template_path.parent / node["file"]
        if node["type"] == "text":
            tail = _replace_with_text(
                anchor, path.read_text(encoding="utf-8")
            )
            text_spans[node["id"]] = (anchor, tail)
            return tail
        elif node["type"] == "table":
            return _insert_table_after(
                doc, anchor, path.read_text(encoding="utf-8")
            )
        elif node["type"] == "image":
            images = _image_files(node, template_path.parent)
            notes, common = _image_caption_lines(node, len(images))
            return _insert_image_group_after(doc, anchor, images, notes=notes, group_notes=common)
        raise TemplateAssemblyError(f"不支持的模块类型：{node['type']}")

    for node in nodes:
        if node["type"] == "heading" or node.get("anchor_task_id") is not None:
            continue
        anchor = node_slots.get(node["id"])
        if anchor is None:
            raise TemplateAssemblyError(f"任务无法定位模板 slot：{node['id']} {node.get('title', '')}")
        node_slots[node["id"]] = fill_node(node, anchor)

    def insert_descendants(node_id, tail):
        initial_tail = tail
        for child in insertion_children.get(node_id, []):
            # 每个新增模块拥有独立段落；同锚点模块紧接前一个模块及其子链。
            anchor = _insert_paragraph_after(tail, "", getattr(tail, "pPr", None), tail.getparent())
            node_slots[child["id"]] = fill_node(child, anchor)
            tail = insert_descendants(child["id"], node_slots[child["id"]])
        _transfer_section_properties(initial_tail, tail)
        return tail

    for node in nodes:
        if node["type"] != "heading" and node.get("anchor_task_id") is None:
            insert_descendants(node["id"], node_slots[node["id"]])

    _remove_residual_instructions(doc)
    _format_front_signoff_layout(doc)
    terminal_text_flow = _format_terminal_text_flow(doc, text_spans)
    after = _doc_signature(doc)
    if (
        before["sections"] != after["sections"]
        or before["section_properties"] != after["section_properties"]
        or before["geometry"] != after["geometry"]
    ):
        raise TemplateAssemblyError("装配后模板 section 或页面尺寸发生变化")
    if (
        after["bookmarks"] < before["bookmarks"]
        or any(name not in after["bookmark_names"] for name in before["bookmark_names"])
    ):
        raise TemplateAssemblyError("装配后模板书签结构减少或丢失")
    if before["toc"] and not after["toc"]:
        raise TemplateAssemblyError("装配后模板目录域丢失")
    if after["tables"] < before["tables"]:
        raise TemplateAssemblyError("装配后模板原有表格丢失")
    if after["images"] < before["images"]:
        raise TemplateAssemblyError("装配后模板原有图片丢失")
    if _instruction_paragraphs(doc):
        raise TemplateAssemblyError("仍有模板编写指令未清除")
    _validate_body_contents(doc, manifest, template_path.parent)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return {
        "before": before,
        "after": after,
        "slots": len(slots),
        "filled_slots": len([node for node in nodes if node["type"] != "heading"]),
        "refreshed_tocs": refreshed_tocs,
        "generated_on": report_date.isoformat(),
        "date_updates": date_updates,
        "author_updates": author_updates,
        "terminal_text_flow": terminal_text_flow,
    }


def make_report_validator(expected_headings_or_manifest):
    def _validate(payload: str, workdir):
        path = Path(workdir) / "out" / "report.docx"
        if not path.exists() or path.stat().st_size <= 5000:
            return False, "尚未生成 report.docx（或文件过小）", ""
        try:
            doc = Document(str(path))
        except Exception as exc:
            return False, f"report.docx 无法打开：{exc}", ""
        is_manifest = isinstance(expected_headings_or_manifest, dict)
        logical_headings = (
            [
                node["title"]
                for node in expected_headings_or_manifest["nodes"]
                if node["type"] == "heading"
            ]
            if is_manifest
            else list(expected_headings_or_manifest)
        )
        expected_signature = (
            expected_headings_or_manifest.get("signature", {})
            if is_manifest
            else {}
        )
        signature_heading_titles = expected_signature.get("heading_titles")
        expected_heading_titles = (
            list(signature_heading_titles)
            if isinstance(signature_heading_titles, list)
            else list(logical_headings)
        )
        headings = [
            paragraph.text.strip()
            for paragraph in doc.paragraphs
            if paragraph.style is not None
            and paragraph.style.name.startswith("Heading")
            and paragraph.text.strip()
        ]
        missing = list((Counter(expected_heading_titles) - Counter(headings)).elements())
        if missing:
            return False, f"报告缺少标题：{'、'.join(missing[:5])}", ""
        if is_manifest and isinstance(signature_heading_titles, list):
            all_texts = [
                paragraph.text.strip()
                for paragraph in doc.paragraphs
                if paragraph.text.strip()
            ]
            front_titles = [
                title
                for title in logical_headings
                if title not in expected_heading_titles
            ]
            missing_front = [title for title in front_titles if title not in all_texts]
            if missing_front:
                return False, f"报告缺少前置小节标题：{'、'.join(missing_front[:5])}", ""
        if _instruction_paragraphs(doc):
            return False, "报告仍包含模板编写指令", ""
        if is_manifest:
            actual_signature = _doc_signature(doc)
            for key in (
                "sections",
                "section_properties",
                "geometry",
                "bookmark_names",
                "toc",
                "toc_entries",
            ):
                if actual_signature.get(key) != expected_signature.get(key):
                    return False, f"DOCX 结构门槛失败：{key} 与模板不一致", ""
            if actual_signature["tables"] < expected_signature.get("tables", 0):
                return False, "DOCX 结构门槛失败：模板原有表格丢失", ""
            if actual_signature["images"] < expected_signature.get("images", 0):
                return False, "DOCX 结构门槛失败：模板原有图片丢失", ""
            expected_slots = [
                node for node in expected_headings_or_manifest["nodes"]
                if node["type"] != "heading"
            ]
            if any(not node.get("anchor_task_id") and not node.get("slot_key") for node in expected_slots):
                return False, "报告存在未追踪的模板 slot", ""
            try:
                _validate_body_contents(doc, expected_headings_or_manifest, Path(workdir))
            except (TemplateAssemblyError, OSError, ValueError) as exc:
                return False, str(exc), ""
        return True, "", payload.strip()

    return _validate


def _render_gate(report_path: Path, workdir: Path) -> dict[str, Any]:
    def resolve_tool(name: str, configured_name: str = "") -> str | None:
        configured = os.environ.get(configured_name, "") if configured_name else ""
        if configured and Path(configured).exists():
            return configured
        found = shutil.which(name)
        if found:
            return found
        for candidate in (
            Path("/Users/mac/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override") / name,
            Path("/Users/mac/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback") / name,
            Path("/Users/mac/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/bin") / name,
            Path("/Users/mac/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/bin") / name,
        ):
            if candidate.exists():
                return str(candidate)
        return None

    soffice = resolve_tool("soffice", "SOFFICE_PATH")
    pdfinfo = resolve_tool("pdfinfo")
    pdftoppm = resolve_tool("pdftoppm", "PDFTOPPM_PATH")
    pdftotext = resolve_tool("pdftotext")
    if not soffice or not pdfinfo or not pdftoppm or not pdftotext:
        require_native = os.environ.get("VISUAL_QA_REQUIRE_NATIVE_RENDERER", "true").strip().lower()
        if require_native in {"0", "false", "no", "off"}:
            # 桌面客户端默认不携带 LibreOffice/poppler：跳过原生渲染验收，仍保留文档结构校验。
            logger.warning("未找到 LibreOffice/poppler，已按配置跳过原生渲染验收")
            return {"ok": True, "skipped": True, "message": "已跳过原生渲染验收"}
        return {
            "ok": False,
            "message": "缺少 LibreOffice/pdfinfo/pdftoppm/pdftotext，无法完成渲染验收",
        }
    qa = workdir / "qa"
    qa.mkdir(parents=True, exist_ok=True)
    profile = qa / "lo_profile"
    fontconfig = qa / "fontconfig.conf"
    fontconfig.write_text(
        """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>
  <dir>/System/Library/Fonts</dir>
  <dir>/Library/Fonts</dir>
  <match target="pattern">
    <test name="family" compare="eq"><string>宋体</string></test>
    <edit name="family" mode="prepend"><string>STSong</string></edit>
  </match>
  <match target="pattern">
    <test name="family" compare="eq"><string>SimSun</string></test>
    <edit name="family" mode="prepend"><string>STSong</string></edit>
  </match>
  <match target="pattern">
    <test name="family" compare="eq"><string>黑体</string></test>
    <edit name="family" mode="prepend"><string>Heiti SC</string></edit>
  </match>
  <match target="pattern">
    <test name="family" compare="eq"><string>微软雅黑</string></test>
    <edit name="family" mode="prepend"><string>Hiragino Sans GB</string></edit>
  </match>
</fontconfig>
""",
        encoding="utf-8",
    )
    render_env = os.environ.copy()
    render_env["FONTCONFIG_FILE"] = str(fontconfig)
    result = subprocess.run(
        [
            soffice,
            f"-env:UserInstallation={profile.as_uri()}",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(qa),
            str(report_path),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=render_env,
    )
    pdf = qa / f"{report_path.stem}.pdf"
    if result.returncode != 0 or not pdf.exists():
        return {"ok": False, "message": f"LibreOffice 渲染失败：{result.stderr[-500:]}"}
    info = subprocess.run([pdfinfo, str(pdf)], capture_output=True, text=True, timeout=30)
    match = re.search(r"Pages:\s+(\d+)", info.stdout)
    pages = int(match.group(1)) if match else 0
    if pages <= 0:
        return {"ok": False, "message": "渲染后 PDF 没有有效页数"}
    extracted = subprocess.run(
        [pdftotext, "-enc", "UTF-8", str(pdf), "-"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if extracted.returncode != 0:
        return {"ok": False, "message": "无法提取渲染后 PDF 文本，疑似字体/编码异常"}
    if "\ufffd" in extracted.stdout or "□" in extracted.stdout:
        return {"ok": False, "message": "渲染后 PDF 存在缺失字形或替换字符"}
    bbox = subprocess.run(
        [pdftotext, "-bbox", "-enc", "UTF-8", str(pdf), "-"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if bbox.returncode != 0:
        return {"ok": False, "message": "无法读取渲染后 PDF 的文字边界"}
    try:
        root = ET.fromstring(bbox.stdout)
        pages_xml = [element for element in root.iter() if element.tag.endswith("page")]
        if len(pages_xml) != pages:
            return {"ok": False, "message": "PDF 页面边界信息不完整"}
        for index, page in enumerate(pages_xml, start=1):
            width = float(page.attrib.get("width", "0"))
            height = float(page.attrib.get("height", "0"))
            if width <= 0 or height <= 0:
                return {"ok": False, "message": f"第 {index} 页没有有效尺寸"}
            for word in (item for item in page.iter() if item.tag.endswith("word")):
                x_min = float(word.attrib.get("xMin", "0"))
                y_min = float(word.attrib.get("yMin", "0"))
                x_max = float(word.attrib.get("xMax", "0"))
                y_max = float(word.attrib.get("yMax", "0"))
                if x_min < -0.5 or y_min < -0.5 or x_max > width + 0.5 or y_max > height + 0.5:
                    return {
                        "ok": False,
                        "message": f"第 {index} 页存在越出页面边界的文字，疑似溢出或错位",
                    }
    except (ET.ParseError, ValueError) as exc:
        return {"ok": False, "message": f"无法解析 PDF 文字边界：{exc}"}
    page_prefix = qa / "page"
    raster = subprocess.run(
        [
            pdftoppm,
            "-png",
            "-r",
            "120",
            str(pdf),
            str(page_prefix),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    page_images = sorted(qa.glob("page-*.png"))
    if raster.returncode != 0 or len(page_images) != pages:
        return {"ok": False, "message": "PDF 页面栅格化失败，无法完成版式验收"}
    try:
        from PIL import Image, ImageStat

        for page_image in page_images:
            image = Image.open(page_image).convert("L")
            stat = ImageStat.Stat(image)
            mean = stat.mean[0] if isinstance(stat.mean, list) else stat.mean
            stddev = stat.stddev[0] if isinstance(stat.stddev, list) else stat.stddev
            # 只有几乎全白的页面才判定为空白页，避免误伤正常的稀疏封面。
            if mean > 254.8 and stddev < 0.8:
                return {
                    "ok": False,
                    "message": f"渲染结果包含近乎空白页：{page_image.name}",
                }
            ink = image.point(lambda value: 255 if value < 245 else 0).getbbox()
            if ink is not None:
                left, top, right, bottom = ink
                width, height = image.size
                if left <= 1 or top <= 1 or right >= width - 1 or bottom >= height - 1:
                    return {
                        "ok": False,
                        "message": f"渲染结果贴到页面边界：{page_image.name}，疑似内容被裁切",
                    }
    except Exception as exc:
        return {"ok": False, "message": f"页面图像验收失败：{exc}"}
    return {
        "ok": True,
        "pages": pages,
        "pdf": str(pdf),
        "page_images": [str(path) for path in page_images],
    }


async def assemble_report(db: Session, project: Project, run_token: str | None = None) -> str:
    """审核通过后，以原模板为基底装配、渲染并上传报告。"""
    project_id, user_id = project.id, project.user_id
    workdir = sandbox.fresh_workdir(project_id, 0, run_token)
    started = time.time()
    audit = await review.review_and_repair(db, project, run_token=run_token)
    if not audit.get("ok"):
        raise ValueError(
            "导出前审核未通过：" + review.format_issues(audit.get("issues", []))
        )

    operations.report_progress(project_id, run_token, phase="render",
                               label="组装与图表渲染", done=0, total=0)
    manifest = prepare_manifest(
        db, project, workdir, verified_chart_metadata=True,
        image_progress=lambda done, total: operations.report_progress(
            project_id, run_token, phase="render",
            label="组装与图表渲染", done=done, total=total))
    db.rollback()
    report = workdir / "out" / "report.docx"
    layout = assemble_template_docx(workdir / manifest["template"], manifest, report)
    (workdir / "layout.json").write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")
    ok, note, _ = make_report_validator(manifest)("模板装配", workdir)
    if not ok:
        raise TemplateAssemblyError(note)
    operations.report_progress(project_id, run_token, phase="qa", label="渲染验收")
    render = _render_gate(report, workdir)
    if not render.get("ok"):
        raise TemplateAssemblyError(render.get("message", "报告渲染验收失败"))

    from uuid import uuid4

    path = f"{user_id}/{project_id}/export/report_{uuid4().hex}.docx"
    storage.upload_bytes(
        path,
        report.read_bytes(),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    try:
        # 渲染/上传期间不持有行锁；发布时重新开短事务核验本次运行令牌。
        db.rollback()
        if run_token is not None:
            from .operations import ensure_run

            project = ensure_run(db, project_id, run_token, lock=True)
        old_path = project.report_path
        project.report_path = path
        if old_path and old_path != path:
            from .operations import queue_cleanup

            queue_cleanup(db, old_path)
        db.commit()
    except Exception:
        db.rollback()
        try:
            # commit 的连接错误可能发生在数据库已提交之后；无法确认孤立时保留对象。
            published = db.scalar(select(Project.report_path).where(Project.id == project_id))
            db.rollback()
            if published != path:
                storage.remove(path)
        except Exception:
            logger.warning("remove unpublished report failed", exc_info=True)
        raise
    logger.info(
        "project %s report assembled in %ss, pages=%s: %s",
        project_id,
        int(time.time() - started),
        render.get("pages"),
        path,
    )
    return path
