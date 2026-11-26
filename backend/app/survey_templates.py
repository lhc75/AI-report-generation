"""Parse completed Word reports into ordered, replaceable survey sections."""

import io
import re

from docx import Document
from docx.oxml.ns import qn
from docx.shared import RGBColor
from docx.table import Table

from . import template_parse


ROLE_PATTERNS = {
    "advice": r"建议|改进|跟进|行动|对策|支持措施",
    "scope": r"范围|样本|概况|说明|方法|背景|概述|答卷构成",
    "findings": r"差异|对比|比较|发现|观察|覆盖|校际",
    "metrics": r"指标|结果|表现|学业|教学|问卷分析|学习态度|教师答卷|校长答卷",
}
DEFAULT_INSTRUCTIONS = {
    "scope": "说明本批问卷的统计范围、答卷量和解读边界",
    "metrics": "依据本批答卷和年度指标资料报告统计结果、分母及缺项",
    "findings": "依据本批证据解释观察到的情况和适用范围",
    "advice": "提出符合本级职责的行动、核验方法和跟进措施",
}
KIND_LABELS = {"text": "撰写正文", "table": "生成表格", "image": "生成图片"}


def inferred_measure(columns):
    if any("完全或基本符合" in column for column in columns) and any("不确定" in column for column in columns):
        return "course_interest"
    return None


def parse_report(raw: bytes) -> list[dict]:
    doc = Document(io.BytesIO(raw))
    structure = template_parse.extract_structure(raw)
    paragraphs = {p._p: p for p in doc.paragraphs}
    body = list(doc.element.body)
    headings = [(i, paragraphs[node]) for i, node in enumerate(body)
                if node in paragraphs and re.fullmatch(r"Heading [1-9]", paragraphs[node].style.name)]
    if not headings:
        raise ValueError("报告未识别到章节，请在 Word 中使用「标题 1」或下级标题样式")
    if len(headings) > 150 or any(not paragraph.text.strip() for _, paragraph in headings):
        raise ValueError("请检查报告是否存在空标题或超过 150 个章节")
    heading_blocks = [block for block in structure if block["kind"] == "heading"]
    red_tasks = template_parse.tasks_from_red_marks(structure)
    instructions = {}
    for task in red_tasks:
        if task["task_type"] != "heading":
            instructions.setdefault(task["parent_tmp"], []).append(task)
    heading_tasks = [task for task in red_tasks if task["task_type"] == "heading"]
    sections, stack = [], []
    final_index = len(body) - int(body[-1].tag == qn("w:sectPr"))
    for slot, ((start, _), block, task) in enumerate(zip(headings, heading_blocks, heading_tasks, strict=True)):
        title, depth = block["text"], block["level"]
        while stack and stack[-1]["depth"] >= depth:
            stack.pop()
        end = headings[slot + 1][0] if slot + 1 < len(headings) else final_index
        role = next((key for key, pattern in ROLE_PATTERNS.items() if re.search(pattern, title)),
                    stack[-1]["role"] if stack else "findings")
        modules = []
        red_modules = iter(instructions.get(task["tmp_id"], []))
        for index in range(start + 1, end):
            node, p = body[index], paragraphs.get(body[index])
            if node.tag == qn("w:tbl"):
                # Column labels identify the purpose of a source table without copying its example values.
                table = Table(node, doc._body)
                columns = [cell.text.strip()[:80] for cell in table.rows[0].cells] if table.rows else []
                modules.append({"kind": "table", "indices": [index], "columns": columns})
            elif node.xpath(".//w:drawing | .//w:pict"):
                modules.append({"kind": "image", "indices": [index]})
            elif p is not None and p.text.strip():
                if template_parse._para_is_red(p) and not template_parse.is_note_instruction(p.text):
                    instruction = next(red_modules, None)
                    if instruction:
                        modules.append({"kind": instruction["task_type"], "indices": [index],
                                        "instruction": instruction["instruction"], "inferred": False})
                        continue
                if modules and modules[-1]["kind"] == "text" and "instruction" not in modules[-1]:
                    modules[-1]["indices"].append(index)
                else:
                    modules.append({"kind": "text", "indices": [index]})
        if not modules and (slot + 1 == len(headings) or heading_blocks[slot + 1]["level"] <= depth):
            modules.append({"kind": "text", "indices": []})
        for module in modules:
            module.setdefault("instruction", f"【{KIND_LABELS[module['kind']]}】围绕“{title}”{DEFAULT_INSTRUCTIONS[role]}。")
            if module.get("columns"):
                module["instruction"] += " 原表列名：" + "、".join(module["columns"]) + "。"
        measure = next((matched for module in modules if (matched := inferred_measure(module.get("columns", [])))), None)
        section = {"title": title, "depth": depth, "heading": start, "role": role,
                   "path": [item["title"] for item in stack] + [title], "modules": modules, "measure": measure}
        sections.append(section)
        stack.append(section)
    return sections


def replace_paragraph_text(paragraph, text):
    if paragraph.runs:
        paragraph.runs[0].text = text
        for run in paragraph.runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(text)


def replace_module(nodes, heading, module, output):
    """Insert before the old block and preserve any source section properties."""
    source = [nodes[index] for index in module["indices"]]
    if not source:
        for node in reversed(output):
            heading.addnext(node)
        return
    for node in output:
        source[0].addprevious(node)
    for node in source:
        if node.xpath("./w:pPr/w:sectPr"):
            for child in list(node):
                if child.tag != qn("w:pPr"):
                    node.remove(child)
        else:
            node.getparent().remove(node)


def save_red_template(raw, sections, target):
    doc = Document(io.BytesIO(raw))
    nodes = list(doc.element.body)
    for section in sections:
        for module in section["modules"]:
            mark = doc.add_paragraph()
            run = mark.add_run(module["instruction"])
            run.font.color.rgb = RGBColor.from_string("FF0000")
            replace_module(nodes, nodes[section["heading"]], module, [mark._p])
    remove_unused_document_media(doc)
    doc.save(target)


def remove_unused_document_media(doc):
    referenced = {value for node in doc.element.iter() for key, value in node.attrib.items()
                  if key.startswith("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}")}
    media = {"image", "chart", "oleObject", "package"}
    for key, relationship in list(doc.part.rels.items()):
        if key not in referenced and relationship.reltype.rsplit("/", 1)[-1] in media:
            del doc.part.rels[key]
