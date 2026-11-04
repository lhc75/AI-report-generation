"""模版 AI 解析：结构识别、红色字体识别、章节任务拆解、AI 指令回写。

通用流程（对应开发计划第三节"模版 AI 解析能力"）：
1. extract_structure      —— 把 docx 按顺序提取为 heading/front_title/para/table/image 块
2. structure_summary      —— 把块渲染成给 AI 看的结构文本
3. summarize_reference / summarize_data_structure —— 参考资料与数据结构摘要
4. PARSE_PROMPT           —— 让 AI 为每个标题生成四类红色指令
5. write_instructions_to_docx —— 把指令以红色字体写回模版（标题不动，旧正文/图表清除）
6. tasks_from_ai_result / tasks_from_red_marks —— 拆解章节写作任务入库
"""
import csv
import difflib
import hashlib
import io
import json
import re
from collections import Counter

import openpyxl
from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

RED = RGBColor(0xFF, 0, 0)

# 前置小节（第一个 Word 标题之前）允许整体替换正文的短标题
FRONT_SECTION_TITLES = ("说明", "前言", "序言", "引言", "内容提要")
# 永不作为前置小节处理的短行
FRONT_EXCLUDE_TITLES = ("目录",)


def _para_is_red(para) -> bool:
    for run in para.runs:
        color = run.font.color
        if color is not None and color.rgb is not None and str(color.rgb).upper() == "FF0000":
            if run.text.strip():
                return True
    return False


def _norm(text: str) -> str:
    """标题归一化：去掉所有空白字符，用于匹配（如"说 明"→"说明"）。"""
    return re.sub(r"\s+", "", text or "")


def _is_title_like(text: str) -> bool:
    """短且无句读符号的行，视为前置小节标题候选。"""
    t = (text or "").strip()
    if not t or len(t) > 15:
        return False
    return not re.search(r"[。；，,;:!！?？]", t)


def extract_structure(docx_bytes: bytes) -> list[dict]:
    """把 docx 按顺序提取为块列表：heading / front_title / para(red?) / table / image。

    front_title：第一个 Word 标题之前的小节标题行（如"说 明"），
    其正文可以被 AI 指令整体替换；封面、落款、目录不受影响。
    """
    doc = Document(io.BytesIO(docx_bytes))
    blocks: list[dict] = []
    body = doc.element.body
    para_map = {p._p: p for p in doc.paragraphs}
    seen_heading = False
    prev_title_like = False
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            para = para_map.get(child)
            if para is None:
                continue
            text = para.text.strip()
            style = para.style.name if para.style else ""
            has_img = bool(child.findall(".//" + qn("w:drawing")) or child.findall(".//" + qn("w:pict")))
            m = re.match(r"Heading (\d)", style or "")
            if m:
                seen_heading = True
                prev_title_like = False
                blocks.append({"kind": "heading", "level": int(m.group(1)), "text": text})
            elif has_img:
                prev_title_like = False
                blocks.append({"kind": "image", "text": text})
            elif text:
                if not seen_heading:
                    norm = _norm(text)
                    if norm in FRONT_EXCLUDE_TITLES:
                        prev_title_like = False
                        blocks.append({"kind": "para", "text": text, "red": _para_is_red(para), "front": True})
                    elif norm in FRONT_SECTION_TITLES or (_is_title_like(text) and not prev_title_like):
                        blocks.append({"kind": "front_title", "text": text})
                        prev_title_like = True
                    else:
                        if not _is_title_like(text):
                            prev_title_like = False
                        blocks.append({"kind": "para", "text": text, "red": _para_is_red(para), "front": True})
                else:
                    prev_title_like = False
                    blocks.append({"kind": "para", "text": text, "red": _para_is_red(para)})
        elif child.tag == qn("w:tbl"):
            prev_title_like = False
            blocks.append({"kind": "table", "text": "[表格]"})
    return _mark_real_front_titles(blocks)


def _mark_real_front_titles(blocks: list[dict]) -> list[dict]:
    """front_title 候选只有满足"已知小节名 或 下一块是正文段落"才保留，否则降级为普通段落。"""
    for i, b in enumerate(blocks):
        if b["kind"] != "front_title":
            continue
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        followed_by_body = (
            nxt is not None and nxt["kind"] == "para" and not _is_title_like(nxt["text"])
        )
        if _norm(b["text"]) not in FRONT_SECTION_TITLES and not followed_by_body:
            blocks[i] = {"kind": "para", "text": b["text"], "red": False, "front": True}
    return blocks


def structure_summary(blocks: list[dict], max_chars: int = 12000) -> str:
    """把块渲染成给 AI 看的结构文本：# 标题、◇ 前置小节、[图片]/[表格]、正文摘要。"""
    lines = []
    for b in blocks:
        if b["kind"] == "heading":
            lines.append(f"{'#' * b['level']} {b['text']}")
        elif b["kind"] == "front_title":
            lines.append(f"◇ {b['text']}（前置小节标题，正文可整体替换）")
        elif b["kind"] == "image":
            lines.append("[图片]" + (f" {b['text'][:100]}" if b["text"] else ""))
        elif b["kind"] == "table":
            lines.append("[表格]")
        else:
            lines.append(b["text"][:200])
    return "\n".join(lines)[:max_chars]


def is_note_instruction(text: str) -> bool:
    """模板说明/数据来源/备注是控制信息，不产生写作任务。"""
    normalized = text.strip()
    return normalized.startswith((
        "【说明",
        "【本模版",
        "【本模板",
        "【数据来源",
        "【备注",
        "【注意",
    ))


def classify_instruction(text: str) -> str:
    if "生成表格" in text:
        return "table"
    if "生成图片" in text:
        return "image"
    return "text"


def _slot_key_for_task(
    task: dict,
    container_path: str,
    occurrence: int,
) -> str:
    seed = "|".join(
        [
            container_path,
            str(task.get("task_type", "")),
            str(occurrence),
        ]
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    return f"slot_{digest}"


def assign_slot_keys(tasks: list[dict]) -> list[dict]:
    """给模板任务分配稳定 slot。

    内容任务按“所属标题完整路径 + 类型 + 出现顺序”定位，不能把 AI 指令文本
    放进 slot 身份。这样同一模板位置的指令调整后，已有内容仍能被准确标回待生成，
    而不是被误判为一个全新的模块。
    """
    heading_paths: dict[int, str] = {}
    for item in sorted(tasks, key=lambda x: x["sort"]):
        parent = item.get("parent_tmp")
        parent_path = heading_paths.get(parent, "")
        if item.get("task_type") == "heading":
            heading_paths[item["tmp_id"]] = "/".join(
                part for part in (parent_path, str(item.get("title", "")).strip()) if part
            )
        else:
            heading_paths[item["tmp_id"]] = parent_path
    occurrences: dict[tuple[str, str], int] = {}
    for item in sorted(tasks, key=lambda x: x["sort"]):
        identity = (heading_paths[item["tmp_id"]], str(item.get("task_type", "")))
        occurrence = occurrences.get(identity, 0)
        occurrences[identity] = occurrence + 1
        item["slot_key"] = _slot_key_for_task(
            item, heading_paths[item["tmp_id"]], occurrence
        )
    return tasks


def tasks_from_red_marks(blocks: list[dict]) -> list[dict]:
    """模版已有红色指令时，直接按标题层级拆解为任务列表（【说明】备注不产生任务）。"""
    tasks: list[dict] = []
    heading_stack: list[int] = []
    sort = 0
    for block in blocks:
        if block["kind"] == "heading":
            level = block["level"]
            # Word 可以跳过某级标题，栈长度不等于标题级别。
            while heading_stack and tasks[heading_stack[-1]]["level"] >= level:
                heading_stack.pop()
            node = {
                "tmp_id": len(tasks),
                "parent_tmp": heading_stack[-1] if heading_stack else None,
                "level": level,
                "title": block["text"],
                "task_type": "heading",
                "instruction": "",
                "sort": sort,
            }
            sort += 1
            tasks.append(node)
            heading_stack.append(node["tmp_id"])
        elif block["kind"] == "para" and block.get("red"):
            if is_note_instruction(block["text"]):
                continue
            tasks.append({
                "tmp_id": len(tasks),
                "parent_tmp": heading_stack[-1] if heading_stack else None,
                "level": tasks[heading_stack[-1]]["level"] + 1 if heading_stack else 1,
                "title": block["text"][:60],
                "task_type": classify_instruction(block["text"]),
                "instruction": block["text"],
                "sort": sort,
            })
            sort += 1
    return assign_slot_keys(tasks)


OUTLINE_PROMPT = (
    "你是一名调查报告结构规划助手。报告采用“问卷设计维度驱动”的编写方式："
    "报告大纲应当围绕问卷/参考资料的设计维度组织，而不是沿用旧报告的框架。"
    "请阅读【模版大纲】（仅标题）、【参考资料摘要】和【数据摘要】，完成结构对齐规划：\n"
    "1. 先通读参考资料，归纳问卷/调查的设计维度（如问卷的“一、二、三……”部分）"
    "和调查对象群体；\n"
    "2. 将模版大纲逐章与设计维度对照，判断每个章节：保留 / 改名（名称过时或与维度口径不符）"
    "/ 删除（与参考资料和数据无关，如旧口径的分项、参考资料中不存在的维度）；\n"
    "3. 设计维度在模版大纲中缺失的，规划新增章节（写明插入位置和层级）。\n"
    "原则：\n"
    "1. 保持报告“概述→分析→建议”的总体骨架不动，只调整与设计维度对应的分析章节；\n"
    "2. 封面、目录、前置小节（如“说明”“前言”）不在调整范围；\n"
    "3. 新增章节必须有明确的参考资料或数据支撑，不得凭空编造维度；\n"
    "4. 改动以必要为度：能与设计维度对上的章节不要强行改名；\n"
    "5. 不得删除全部章节；拿不准的章节保留。\n"
    "\n"
    "只输出 JSON（不要输出其他内容），格式：\n"
    "[{\"heading\": \"模版中的标题原文\", \"rename_to\": \"新标题\"},\n"
    " {\"heading\": \"模版中的标题原文\", \"drop\": true, \"reason\": \"删除原因\"},\n"
    " {\"action\": \"new_section\", \"after\": \"插入位置锚点标题原文\", \"level\": 2, "
    "\"title\": \"新章节标题\"}]\n"
    "未提到的标题视为保留；模版大纲与设计维度已经一致、不需要任何调整时输出 []。\n"
    "\n"
    "【模版大纲】\n{outline}\n\n【参考资料摘要】\n{references}\n\n【数据摘要】\n{data_summary}\n"
)


def outline_summary(blocks: list[dict], max_chars: int = 6000) -> str:
    """大纲摘要：只列标题层级（# 标题 / ◇ 前置小节），供结构规划使用。"""
    lines = []
    for b in blocks:
        if b["kind"] == "heading":
            lines.append(f"{'#' * b['level']} {b['text']}")
        elif b["kind"] == "front_title":
            lines.append(f"◇ {b['text']}（前置小节，不调整）")
    return "\n".join(lines)[:max_chars]


def outline_plan_to_edits(plan: list[dict], blocks: list[dict]):
    """把结构规划 JSON 校验、归一化为 (renames, drops, new_sections)。

    防御规则：
    - 规划中提到的标题必须在模版中真实存在，否则忽略；
    - 改名目标与现有标题冲突时忽略该改名；
    - 删除章节的数量达到标题总数时整体放弃删除（防止清空报告）；
    - 新增章节的锚点若被改名则换成新名，若被删除则置空（追加到文末）。
    """
    valid = {b["text"] for b in blocks if b["kind"] == "heading"}
    renames: dict[str, str] = {}
    drops: dict[str, str] = {}
    new_sections: list[dict] = []
    if not isinstance(plan, list):
        return renames, drops, new_sections
    for item in plan:
        if not isinstance(item, dict):
            continue
        if item.get("action") == "new_section":
            title = str(item.get("title", "")).strip()
            if not title or title in valid:
                continue
            try:
                level = int(item.get("level", 2) or 2)
            except (TypeError, ValueError):
                level = 2
            new_sections.append({
                "after": str(item.get("after", "") or "").strip(),
                "level": max(1, min(level, 4)),
                "title": title,
                "instructions": [],
            })
            continue
        heading = str(item.get("heading", "")).strip()
        if heading not in valid:
            continue
        if item.get("drop"):
            drops[heading] = str(item.get("reason", "") or item.get("note", "")).strip()
            continue
        new_name = str(item.get("rename_to", "") or "").strip()
        if new_name and new_name != heading and new_name not in valid:
            renames[heading] = new_name
    if len(drops) >= len(valid):
        drops = {}  # 防御：不允许清空全部章节
    for ns in new_sections:
        if ns["after"] in drops:
            ns["after"] = ""
        elif ns["after"] in renames:
            ns["after"] = renames[ns["after"]]
    return renames, drops, new_sections


def merge_structure_and_instructions(
    ai_tasks: list[dict], renames: dict, drops: dict, new_sections: list[dict]
) -> list[dict]:
    """把第二趟（按新大纲生成的指令）与第一趟的结构编辑合并为统一的 AI 输出格式。

    第二趟看到的是重构后的大纲，其 heading 用的是新标题，需要映射回：
    - 改名章节：heading=原标题 + rename_to=新标题；
    - 新增章节：还原为 new_section 项（继承第一趟的锚点与层级）；
    - 第二趟漏掉的改名/新增/删除也要补上，保证结构编辑必定生效；
    - 第二趟擅自输出的结构操作（rename_to/drop/new_section）忽略。
    """
    new_title_to_orig = {new: orig for orig, new in renames.items()}
    new_sec_by_title = {ns["title"]: ns for ns in new_sections}
    merged: list[dict] = []
    covered_renames: set[str] = set()
    covered_new_secs: set[str] = set()
    for item in ai_tasks:
        if not isinstance(item, dict):
            continue
        if item.get("action") == "new_section":
            continue
        heading = str(item.get("heading", "")).strip()
        if not heading or item.get("drop") or item.get("rename_to"):
            continue
        instructions = list(item.get("instructions", []) or [])
        if heading in new_title_to_orig:
            orig = new_title_to_orig[heading]
            merged.append({"heading": orig, "rename_to": heading, "instructions": instructions})
            covered_renames.add(orig)
        elif heading in new_sec_by_title:
            ns = new_sec_by_title[heading]
            merged.append({
                "action": "new_section", "after": ns["after"], "level": ns["level"],
                "title": heading, "instructions": instructions,
            })
            covered_new_secs.add(heading)
        else:
            merged.append({"heading": heading, "instructions": instructions})
    for orig, new in renames.items():
        if orig not in covered_renames:
            merged.append({"heading": orig, "rename_to": new, "instructions": []})
    for ns in new_sections:
        if ns["title"] not in covered_new_secs:
            merged.append({
                "action": "new_section", "after": ns["after"], "level": ns["level"],
                "title": ns["title"], "instructions": [],
            })
    for orig, reason in drops.items():
        merged.append({"heading": orig, "drop": True, "note": reason})
    return merged


PARSE_PROMPT = (
    "你是一名调查报告模版解析助手。系统已根据问卷/参考资料的设计维度调整好报告大纲，"
    "会把你输出的指令以红色字体写回模版对应标题之下，作为后续写作 AI 的编写依据。"
    "请阅读【模版结构】、【参考资料摘要】和【数据摘要】，完成：\n"
    "1. 为每个标题判断需要填写什么内容，编写给写作 AI 看的指令；\n"
    "2. 模版中与参考资料、数据不符的内容（旧数据、旧口径、参考资料中不存在的题目、"
    "数据中没有的地区或群体分项等），用【说明】指令标记为已删除或应替换；\n"
    "3. 需要图表的位置，明确写“生成表格”还是“生成图片”，并指明图形类型"
    "（柱状图/条形图/饼图等）和所用数据；\n"
    "4. 不得编造参考资料和数据中不存在的内容；数据摘要中样本量明显过小的群体，"
    "要在相关指令中提醒谨慎解读。\n"
    "\n"
    "每条指令必须使用以下四种前缀之一（一条指令占一段）：\n"
    "- 【填写：…】——需要撰写文字内容，写明写哪些方面、依据哪份参考资料的哪道题或哪个数据字段；\n"
    "- 【生成表格“表名”：…】——需要生成表格，写明行、列结构和数据来源；\n"
    "- 【生成图片（图表类型）：…】——需要生成图片，写明图形类型、统计口径和数据来源；\n"
    "- 【说明：…】——对原模版内容的删除/替换备注（不产生写作任务）。\n"
    "\n"
    "要求：\n"
    "- 每个标题至少输出一条指令；某标题下原内容完全不符合参考资料时，至少输出一条"
    "【说明：…已删除】；\n"
    "- 模版开头如有以 ◇ 标出的前置小节（如“说明”“前言”），可以用它的小节名作为 heading "
    "输出指令，其正文将被整体替换；\n"
    "- 指令要具体、可直接执行：引用参考资料写清出自哪份资料第几题，引用数据写清用哪个文件"
    "哪个字段，统计口径（频次/占比/分组方式）要写清。\n"
    "- 问卷内容与题目对应概览采用主题维度为行、不同问卷对象为列的简洁矩阵，单元格仅列原问卷题号；"
    "岗位身份和结果评价分开，不把全部编码映射、长题干或重复统计限制放入表格。公共方法放表外正文。\n"
    "- 内部题目编码和数据字段定位只供执行指令与证据追溯；成果正文与表图中用中文对象及原问卷题号。"
    "图中的统计基数应简短，完整多选及资料限制说明在图下呈现，不塞入坐标轴标签。\n"
    "- 已有细分小节的父标题只安排简短总领段，说明本节观察重点和读法；"
    "具体题目分布放对应子节，不在父节把后文全部选项与数字再复述一遍。\n"
    "- 指令措辞规范：术语表述统一类要求直接写成“全文统一使用“×××”表述”，"
    "严禁出现“旧称”“原名”“统一规范为”等编辑性字样（这些话会被写作 AI 照抄进报告正文）；"
    "提到问卷群体时一律使用中文名称（见【数据摘要】的编码对照），不得出现英文编码。\n"
    "\n"
    "只输出 JSON（不要输出其他内容），格式：\n"
    "[{\"heading\": \"标题原文\", \"instructions\": [{\"type\": \"text|table|image|note\", "
    "\"instruction\": \"【填写：…】\"}]}]\n"
    "说明：大纲结构已由系统定稿，不要输出 rename_to/drop/new_section 等结构操作，"
    "只按上面的基本格式为每个标题生成指令。\n"
    "\n"
    "【模版结构】\n{structure}\n\n【参考资料摘要】\n{references}\n\n【数据摘要】\n{data_summary}\n"
)
def _column_profile(values: list) -> str:
    """单列画像：去重数 + 低基数字段的取值分布。"""
    vals = [str(v).strip() for v in values if v is not None and str(v).strip() != ""]
    if not vals:
        return "空"
    distinct = len(set(vals))
    if distinct <= 20:
        counts = Counter(vals).most_common(20)
        dist = "、".join(f"{v[:40]}({c})" for v, c in counts)
        return f"{distinct} 种取值：{dist}"
    return f"{distinct} 种取值（取值过多不展开）"


def _code_name_mappings(header: list, body: list) -> dict:
    """找出“XX编码 + XX名称”列对，返回 {编码列号: (名称列号, {编码: 名称})}。"""
    out: dict[int, tuple[int, dict]] = {}
    for ci, col in enumerate(header[:30]):
        if not ("编码" in col or "代码" in col or col.lower().endswith("code")):
            continue
        prefix = (
            col.replace("编码", "").replace("代码", "").removesuffix("code").strip()
        )
        name_ci = next(
            (
                cj
                for cj, other in enumerate(header[:30])
                if cj != ci
                and ("名称" in other or other.lower().endswith("name"))
                and (not prefix or prefix in other)
            ),
            None,
        )
        if name_ci is None:
            continue
        mapping: dict[str, str] = {}
        for r in body:
            code = str(r[ci]).strip() if ci < len(r) else ""
            cname = str(r[name_ci]).strip() if name_ci < len(r) else ""
            if code and cname and code not in mapping:
                mapping[code] = cname
        if mapping:
            out[ci] = (name_ci, mapping)
    return out


# 打包键值单元格（问卷平台回收数据常见）："q1：A；q2：B/C；" 拆成 [(键, 值)]
_KV_PAIR_RE = re.compile(r"^([^：:；;]{1,30})[：:]([^；;]*)$")


def _parse_packed_cell(cell: str) -> list | None:
    """把“键：值；键：值”单元格拆成键值对列表；不是这种结构时返回 None。"""
    parts = [p.strip() for p in re.split(r"[；;]", str(cell)) if p.strip()]
    pairs = []
    for part in parts:
        m = _KV_PAIR_RE.match(part)
        if not m or not m.group(1).strip():
            return None
        pairs.append((m.group(1).strip(), m.group(2).strip()))
    return pairs if len(pairs) >= 2 else None


def _packed_stats_block(header: list, body: list, ci: int,
                        group_ci: int | None, group_names: dict,
                        max_chars: int = 12000) -> str:
    """打包键值列的逐题选项统计：按 分组 × 键 统计各取值的频次与占比/勾选率。

    同一键任一取值含“/”时按多选处理（勾选率=勾选人数/作答该键人数），否则按
    单选处理（占比=人数/作答该键人数）。统计由系统预计算，作为成文引用的
    唯一口径，避免各章节各自重算导致数字不一致。
    """
    rows: dict[int, list] = {}
    for ri, r in enumerate(body):
        cell = str(r[ci]).strip() if ci < len(r) else ""
        pairs = _parse_packed_cell(cell)
        if pairs is not None:
            rows[ri] = pairs
    if len(rows) < 10:
        return ""
    groups: dict[str, list] = {}
    for ri in rows:
        if group_ci is None:
            g = ""
        else:
            raw = str(body[ri][group_ci]).strip() if group_ci < len(body[ri]) else ""
            g = group_names.get(raw, raw)
        groups.setdefault(g, []).append(ri)
    if len(groups) > 20:  # 分组列基数过高没有统计意义，退化为全量统计
        groups = {"": list(rows)}
    lines = [
        f"  列“{header[ci]}”逐题选项统计（系统按真实数据预计算，成文引用以此为准；"
        "键 qN 一般对应问卷第 N 题，请对照参考资料中的题目确认；标注“多选”的键"
        "按勾选率统计，其余按占比统计）："
    ]
    total = len(lines[0])
    for gname, ris in groups.items():
        head = f"  分组“{gname}”（{len(ris)} 份）：" if gname else f"  全部（{len(ris)} 份）："
        lines.append(head)
        total += len(head)
        key_vals: dict[str, list] = {}
        for ri in ris:
            for k, v in rows[ri]:
                if k and v:
                    key_vals.setdefault(k, []).append(v)
        for k, vals in key_vals.items():
            base = len(vals)
            if any("/" in v for v in vals):
                counter: Counter = Counter()
                for v in vals:
                    for part in v.split("/"):
                        if part.strip():
                            counter[part.strip()] += 1
                tag = "多选，勾选率"
            else:
                counter = Counter(vals)
                tag = "占比"
            items = "；".join(
                f"{opt} {n}人（{n / base * 100:.1f}%）"
                for opt, n in counter.most_common(10)
            )
            line = f"    {k}（{tag}，作答{base}人）：{items}"
            if total + len(line) > max_chars:
                lines.append("    ……（其余键/分组的统计从略，可用 run_python 自行计算）")
                return "\n".join(lines)
            lines.append(line)
            total += len(line)
    return "\n".join(lines)


def _decode_text(raw: bytes) -> str:
    """按常见调查导出编码解码 CSV，避免中文资料被静默丢弃。"""
    for encoding in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _clean_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none"} else text


def read_rows(name: str, raw: bytes) -> list[tuple[str, list[list[str]]]]:
    """把数据文件读成 (工作表名, 行列表)；支持 CSV/XLSX/XLSM/XLS。"""
    lower = name.lower()
    if lower.endswith(".csv"):
        rows = [[_clean_cell(cell) for cell in row]
                for row in csv.reader(io.StringIO(_decode_text(raw)))]
        return [("CSV", [row for row in rows if any(row)])]
    if lower.endswith((".xlsx", ".xlsm")):
        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        return [
            (sheet.title,
             [cells for cells in (
                 [_clean_cell(cell) for cell in row]
                 for row in sheet.iter_rows(values_only=True))
              if any(cells)])
            for sheet in workbook.worksheets
        ]
    if lower.endswith(".xls"):
        import pandas as pd

        workbook = pd.ExcelFile(io.BytesIO(raw))
        return [
            (sheet,
             [cells for cells in (
                 [_clean_cell(value) for value in row]
                 for row in pd.read_excel(workbook, sheet_name=sheet, header=None).values.tolist())
              if any(cells)])
            for sheet in workbook.sheet_names
        ]
    return []


def normalize_config(value=None) -> dict:
    """统计数据列配置的规范化（单选题/多选题、字面分隔符、分母口径）。"""
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("统计配置不是合法 JSON") from exc
    if value is None:
        value = {}
    if not isinstance(value, dict) or not isinstance(value.get("fields", []), list):
        raise ValueError("统计配置必须包含 fields 数组")
    if value.get("version", 1) != 1:
        raise ValueError("不支持的统计配置版本")
    fields, used = [], set()
    for item in value.get("fields", []):
        if not isinstance(item, dict):
            raise ValueError("统计字段配置必须是对象")
        ci = item.get("column_index")
        sheet = item.get("sheet")
        if isinstance(ci, bool) or not isinstance(ci, int) or ci < 0:
            raise ValueError("column_index 必须是从 0 开始的列索引")
        if not isinstance(sheet, str) or not sheet.strip():
            raise ValueError("统计字段必须指定 sheet")
        kind = item.get("question_type", "single")
        separator = item.get("separator")
        if kind not in {"single", "multiple"}:
            raise ValueError("question_type 必须是 single 或 multiple")
        if kind == "multiple" and (not isinstance(separator, str) or not separator.strip()):
            raise ValueError("多选题必须指定非空字面分隔符")
        if kind == "single" and separator is not None:
            raise ValueError("单选题不能设置分隔符")
        denominator = item.get("denominator", "answered")
        if denominator not in {"answered", "all_rows"}:
            raise ValueError("denominator 必须是 answered 或 all_rows")
        question_key = item.get("question_key")
        if question_key is not None and (not isinstance(question_key, str) or not question_key.strip()):
            raise ValueError("question_key 必须是非空字符串或 null")
        key = (sheet, ci, question_key)
        if key in used:
            raise ValueError("统计字段配置重复")
        used.add(key)
        normalized = {"sheet": sheet, "column_index": ci, "question_key": question_key,
                      "question_type": kind, "separator": separator,
                      "denominator": denominator}
        if item.get("column_name") is not None:
            if not isinstance(item["column_name"], str):
                raise ValueError("column_name 必须是字符串")
            normalized["column_name"] = item["column_name"]
        fields.append(normalized)
    fields.sort(key=lambda f: (f["sheet"], f["column_index"], f["question_key"] or ""))
    return {"version": 1, "fields": fields}


def _option_code_key_notes(header: list, body: list) -> list[str]:
    """识别“键：选项”式作答记录列，给出键位规律说明（由样本核验得出）。"""
    if not body:
        return []
    for ci in range(len(header)):
        values = [str(row[ci]).strip() for row in body[:30]
                  if ci < len(row) and str(row[ci]).strip()]
        if not values:
            continue
        parts = [part for part in re.split(r"[；;]", "；".join(values)) if part.strip()]
        pairs = []
        for part in parts:
            key, sep, value = part.partition("：")
            if sep and key.strip() and value.strip():
                pairs.append((key.strip(), value.strip()))
        if len(pairs) < len(parts) or not pairs:
            continue
        keys = {key for key, _value in pairs}
        for row in body[:600]:
            if ci >= len(row):
                continue
            for part in re.split(r"[；;]", str(row[ci])):
                key, sep, _value = part.partition("：")
                if sep:
                    keys.add(key.strip())
        numbers = sorted({int(key[1:]) for key in keys if re.fullmatch(r"q\d+", key)})
        if not numbers:
            continue
        note = (f"列“{header[ci]}”为“键：选项”作答记录；键 q{numbers[0]}…q{numbers[-1]}"
                + ("连续编号" if numbers == list(range(numbers[0], numbers[-1] + 1))
                   else "编号不连续"))
        if numbers[0] == 2 and numbers == list(range(2, numbers[-1] + 1)):
            note += ("；region 为地区题，假设配套问卷文档的题号从 1 开始，则键号整体比题号大 1"
                     "（q2 对应第 1 题）；各问卷题量不同、键范围以该问卷实际出现为准，"
                     "统计前仍先按选项数量与语义核对。")
        else:
            note += "；统计前先按选项数量与语义核对键与题目的对应关系。"
        return [note]
    return []


def summarize_data_structure(name: str, data: bytes) -> str:
    """数据文件的结构摘要：工作表、行数、列名与资料自带的编码对照（不含统计结果）。"""
    lines = [f"文件 {name}："]
    for sheet, rows in read_rows(name, data):
        if not rows:
            continue
        header, body = rows[0], rows[1:]
        lines.append(f"工作表“{sheet}”：{len(body)}条提交记录；列：{'、'.join(header)}")
        for _ci, (_name_ci, mapping) in _code_name_mappings(header, body).items():
            lines.append("编码对照（资料原有名称）：" + "；".join(f"{key}={value}" for key, value in mapping.items()))
        lines.extend(_option_code_key_notes(header, body))
        if body:
            sample = "；".join(f"{header[ci]}={str(value)[:60]}"
                              for ci, value in enumerate(body[0][:len(header)]))
            lines.append(f"原始样例（非统计结果）：{sample[:400]}")
    lines.append("全部统计数字必须用 run_python 从数据文件计算；提交记录不自动等于独立有效人数。")
    return "\n".join(lines)


def summarize_reference(name: str, data: bytes, max_chars: int = 6000) -> str:
    """参考资料摘要：提取 DOCX/PDF 文本（问卷类资料需包含全部题目）。"""
    lower = name.lower()
    if lower.endswith(".docx"):
        try:
            doc = Document(io.BytesIO(data))
            text = "\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())
            suffix = f"\n……（全文共 {len(text)} 字，已截断）" if len(text) > max_chars else ""
            return f"《{name}》\n{text[:max_chars]}{suffix}"
        except Exception as exc:
            return f"《{name}》读取失败（{exc}）"
    if lower.endswith(".pdf"):
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            pages = [
                (page.extract_text() or "").strip()
                for page in reader.pages
            ]
            text = "\n".join(page for page in pages if page)
            suffix = f"\n……（全文共 {len(text)} 字，已截断）" if len(text) > max_chars else ""
            return f"《{name}》\n{text[:max_chars]}{suffix}" if text else f"《{name}》无可提取文本"
        except Exception as exc:
            return f"《{name}》读取失败（{exc}）"
    return f"《{name}》（不支持的参考资料格式）"


# ---- 指令清洗：防止编辑性元语句和英文编码被写作 AI 照抄进报告正文 ----

# 术语统一类元语句：旧称“X”统一（规范）为“Y” → 全文统一使用“Y”表述
_META_UNIFY_RES = [
    re.compile(
        r"(?:将)?(?:原表述中的)?旧称[“\"]([^”\"]+)[”\"]"
        r"(?:在本报告中)?(?:统一规范为|统一为|规范为)[“\"]([^”\"]+)[”\"]"
    ),
    re.compile(r"将[“\"]([^”\"]+)[”\"](?:在本报告中)?统一规范为[“\"]([^”\"]+)[”\"]"),
]
# 残留的元语句片段（分句级删除用）
_META_WORDS_RE = re.compile(r"旧称|原名|统一规范为")


def extract_code_map(data_summary: str) -> dict[str, str]:
    """从数据摘要的“编码对照（…）：k=v；k=v”行中提取 编码→中文名称 映射。"""
    mapping: dict[str, str] = {}
    for line in (data_summary or "").splitlines():
        m = re.search(r"编码对照（[^）]*）：(.+)$", line.strip())
        if not m:
            continue
        for pair in m.group(1).split("；"):
            if "=" not in pair:
                continue
            code, name = pair.split("=", 1)
            code, name = code.strip(), name.strip()
            if code and name:
                mapping[code] = name
    return mapping


def sanitize_instruction(text: str, code_map: dict[str, str] | None = None) -> str:
    """清洗单条 AI 指令：
    1. “旧称X统一为Y”类编辑性表述改写为“全文统一使用“Y”表述”；
    2. 残留的元语句分句直接删除；
    3. 英文编码按编码对照替换成中文名称（写作 AI 照抄指令时不会带出英文）。
    """
    if not text:
        return text
    out = text
    for pat in _META_UNIFY_RES:
        out = pat.sub(lambda m: f"全文统一使用“{m.group(2)}”表述", out)
    if _META_WORDS_RE.search(out):
        # 分句删除仍含元语句字样的片段；删空则保留原文，避免误伤
        clauses = re.split(r"([；;。])", out)
        merged: list[str] = []
        buf = ""
        for seg in clauses:
            buf += seg
            if seg in "；;。":
                merged.append(buf)
                buf = ""
        if buf:
            merged.append(buf)
        kept = [c for c in merged if not _META_WORDS_RE.search(c)]
        cleaned = "".join(kept).strip()
        if cleaned:
            out = cleaned
    if code_map:
        # 编码对照的名称通常是完整问卷名（“……调查（教育主管部门问卷）”），
        # 替换进指令时取括号内的短名，避免正文出现冗长全称
        short_map = {code: _short_group_name(name) for code, name in code_map.items()}
        for code, name in sorted(short_map.items(), key=lambda kv: -len(kv[0])):
            if len(code) < 4 or not re.search(r"[A-Za-z]", code):
                continue  # 过短的编码（A/B/q1 等）不替换，防止误伤
            out = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(code)}(?![A-Za-z0-9_])", name, out)
    return out


def _short_group_name(name: str) -> str:
    """从编码对照名称中提取短名：末尾括号内容优先，否则截断。"""
    m = re.search(r"（([^（）]+)）\s*$", name or "")
    short = m.group(1) if m else (name or "")
    return short[:30] or (name or "")[:30]


def sanitize_ai_tasks(ai_tasks: list[dict], code_map: dict[str, str] | None = None) -> list[dict]:
    """对 AI 解析结果中的全部指令（含新增章节自带指令、删除备注）做清洗。"""
    for item in ai_tasks:
        for ins in item.get("instructions", []) or []:
            if isinstance(ins, dict) and "instruction" in ins:
                ins["instruction"] = sanitize_instruction(str(ins["instruction"]), code_map)
        if item.get("note"):
            item["note"] = sanitize_instruction(str(item["note"]), code_map)
    return ai_tasks


def parse_ai_json(content: str) -> list[dict]:
    """从模型输出中稳健地解析 JSON 数组。"""
    text = content.strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1:
        text = text[start : end + 1]
    return json.loads(text)


def build_instr_queues(ai_tasks: list[dict]) -> dict[str, list[list[dict]]]:
    """把 AI 输出按 heading 分组为指令队列。

    同名标题（如多个"4、小结"）会按出现顺序各占一个队列元素，
    消费时先进先出，避免所有同名章节拿到重复指令。
    """
    queues: dict[str, list[list[dict]]] = {}
    for item in ai_tasks:
        if item.get("action") == "new_section" or item.get("drop"):
            continue  # 新增章节自带指令；被删除章节不产生指令
        key = str(item.get("heading", "")).strip()
        queues.setdefault(key, []).append(list(item.get("instructions", [])))
    return queues


def split_structure_edits(ai_tasks: list[dict]):
    """从 AI 输出中分离结构编辑：重命名、整章删除、新增章节。

    返回 (renames, drops, new_sections)：
    renames = {原标题: 新标题}；drops = {原标题: 原因}；
    new_sections = [{"after": 锚点标题, "level": int, "title": str, "instructions": [...]}]。
    """
    renames: dict[str, str] = {}
    drops: dict[str, str] = {}
    new_sections: list[dict] = []
    for item in ai_tasks:
        if item.get("action") == "new_section":
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            try:
                level = int(item.get("level", 2) or 2)
            except (TypeError, ValueError):
                level = 2
            new_sections.append({
                "after": str(item.get("after", "") or "").strip(),
                "level": max(1, min(level, 4)),
                "title": title,
                "instructions": list(item.get("instructions", [])),
            })
            continue
        key = str(item.get("heading", "")).strip()
        if not key:
            continue
        if item.get("drop"):
            drops[key] = str(item.get("note", "")).strip()
            continue
        new_name = str(item.get("rename_to", "") or "").strip()
        if new_name and new_name != key:
            renames[key] = new_name
    return renames, drops, new_sections


def _heading_level(para) -> int:
    m = re.match(r"Heading (\d+)", para.style.name)
    return int(m.group(1)) if m else 9


def apply_structure_to_blocks(blocks: list[dict], renames: dict, drops: dict,
                              new_sections: list[dict]) -> list[dict]:
    """在 blocks 层面虚拟应用结构编辑，得到解析后的新大纲结构。

    drop 删除整章（含下属子章节与内容块）；rename 改标题文字并记录 renamed_from；
    new_section 作为带 own_instructions 的虚拟标题块插入锚点章节之后。
    """
    out: list[dict] = []
    skip_level: int | None = None
    for b in blocks:
        if b["kind"] == "heading":
            if skip_level is not None and b["level"] > skip_level:
                continue  # 被删除章节的子树
            skip_level = None
            if b["text"] in drops:
                skip_level = b["level"]
                continue
            nb = dict(b)
            if b["text"] in renames:
                nb["renamed_from"] = b["text"]
                nb["text"] = renames[b["text"]]
            out.append(nb)
        else:
            if skip_level is not None:
                continue
            out.append(b)
    for ns in new_sections:
        block = {"kind": "heading", "level": ns["level"], "text": ns["title"],
                 "own_instructions": ns["instructions"]}
        idx = None
        if ns["after"]:
            for k, b in enumerate(out):
                if b["kind"] == "heading" and b["text"] == ns["after"]:
                    lvl = b["level"]
                    idx = len(out)
                    for m in range(k + 1, len(out)):
                        if out[m]["kind"] == "heading" and out[m]["level"] <= lvl:
                            idx = m
                            break
                    break
        out.insert(idx if idx is not None else len(out), block)
    return out


def pop_heading_instructions(queues: dict[str, list[list[dict]]], heading: str) -> list[dict]:
    """按顺序消费某标题的指令；精确匹配失败时用归一化和模糊匹配兜底。"""
    key = None
    if heading in queues and queues[heading]:
        key = heading
    else:
        norm = _norm(heading)
        norm_map = {_norm(k): k for k in queues if queues[k]}
        if norm in norm_map:
            key = norm_map[norm]
        else:
            close = difflib.get_close_matches(norm, list(norm_map), n=1, cutoff=0.6)
            if close:
                key = norm_map[close[0]]
    if key is None:
        return []
    return queues[key].pop(0)


def _add_red_para(anchor_para, text: str) -> None:
    p = anchor_para.insert_paragraph_before()
    run = p.add_run(text if text.startswith("【") else f"【{text}】")
    run.font.color.rgb = RED
    run.font.size = Pt(12)
    run.font.name = "Times New Roman"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")


def _add_red_para_end(doc, text: str) -> None:
    np = doc.add_paragraph()
    run = np.add_run(text if text.startswith("【") else f"【{text}】")
    run.font.color.rgb = RED
    run.font.size = Pt(12)
    run.font.name = "Times New Roman"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")


def _rewrite_front_sections(doc, blocks: list[dict], queues: dict[str, list[list[dict]]]) -> None:
    """把 AI 针对前置小节（说明/前言等）的指令写回：小节标题保留，正文整体替换。

    正文范围：小节标题之后，直到下一个短标题行（如下一个小节、落款、目录）或
    第一个 Word 标题为止。AI 未涉及的小节保持原样。
    """
    for b in [b for b in blocks if b["kind"] == "front_title"]:
        instructions = pop_heading_instructions(queues, b["text"])
        if not instructions:
            continue
        paras = doc.paragraphs
        title_para = next((p for p in paras if p.text.strip() == b["text"]), None)
        if title_para is None:
            continue
        para_map = {p._p: p for p in doc.paragraphs}
        el = title_para._p.getnext()
        while el is not None:
            nxt = el.getnext()
            if el.tag == qn("w:p"):
                para = para_map.get(el)
                if para is None:
                    break  # 不在段落映射里的结构不碰
                # 用 Paragraph.text 而不是 itertext()：后者会把
                # mc:Fallback 里的重复文本算进来，导致落款行被误判为正文
                txt = para.text.strip()
                if para is not None and para.style.name.startswith("Heading"):
                    break
                if _is_title_like(txt):
                    break  # 下一个小节标题/落款/目录，停止
                el.getparent().remove(el)
            elif el.tag == qn("w:tbl"):
                el.getparent().remove(el)
            el = nxt
        # 把指令插到标题之后（其后第一个段落之前；后面没有段落就追加到文末）
        anchor_el = title_para._p.getnext()
        while anchor_el is not None and anchor_el.tag != qn("w:p"):
            anchor_el = anchor_el.getnext()
        if anchor_el is not None:
            anchor = next(pp for pp in doc.paragraphs if pp._p is anchor_el)
            for ins in instructions:
                _add_red_para(anchor, str(ins.get("instruction", "")))
        else:
            for ins in instructions:
                _add_red_para_end(doc, str(ins.get("instruction", "")))


def write_instructions_to_docx(docx_bytes: bytes, ai_tasks: list[dict]) -> bytes:
    """把 AI 生成的指令以红色字体写回模版。

    - 封面、目录不动；标题默认不动，但支持 AI 输出的结构编辑：
      rename_to 重命名、drop 整章删除、new_section 新增章节；
    - AI 给出指令的标题：清空旧正文/图片/表格，写入红色指令；
    - AI 未涉及的标题：保持原样（安全兜底，避免误删）；
    - 前置小节（说明/前言等）：AI 给出指令时正文整体替换。
    """
    doc = Document(io.BytesIO(docx_bytes))
    blocks = extract_structure(docx_bytes)
    queues = build_instr_queues(ai_tasks)
    renames, drops, new_sections = split_structure_edits(ai_tasks)

    _rewrite_front_sections(doc, blocks, queues)

    i = 0
    while i < len(doc.paragraphs):
        paras = doc.paragraphs
        p = paras[i]
        if not p.style.name.startswith("Heading"):
            i += 1
            continue
        text = p.text.strip()
        if text in drops:
            # 整章删除：本标题 + 下属内容和子章节（直到同级或更高级标题）
            lvl = _heading_level(p)
            j = i + 1
            while j < len(paras) and (
                not paras[j].style.name.startswith("Heading") or _heading_level(paras[j]) > lvl
            ):
                j += 1
            next_el = paras[j]._p if j < len(paras) else None
            prev_el = p._p.getprevious()
            for dp in paras[i:j]:
                dp._p.getparent().remove(dp._p)
            sib = prev_el.getnext() if prev_el is not None else None
            while sib is not None and sib is not next_el:
                nxt = sib.getnext()
                if sib.tag == qn("w:tbl"):
                    sib.getparent().remove(sib)
                sib = nxt
            if next_el is not None:
                i = next(k for k, pp in enumerate(doc.paragraphs) if pp._p is next_el)
            else:
                i = len(doc.paragraphs)
            continue
        instructions = pop_heading_instructions(queues, text)
        new_name = renames.get(text)
        if not instructions and not new_name:
            i += 1
            continue  # 未匹配到指令的章节保持原样
        j = i + 1
        while j < len(paras) and not paras[j].style.name.startswith("Heading"):
            j += 1
        next_el = paras[j]._p if j < len(paras) else None
        for dp in paras[i + 1 : j]:
            dp._p.getparent().remove(dp._p)
        sib = p._p.getnext()
        while sib is not None and sib.tag != qn("w:p"):
            nxt = sib.getnext()
            if sib.tag == qn("w:tbl"):
                sib.getparent().remove(sib)
            sib = nxt
        if next_el is not None:
            anchor = next(pp for pp in doc.paragraphs if pp._p is next_el)
            for ins in instructions:
                _add_red_para(anchor, str(ins.get("instruction", "")))
            i = next(k for k, pp in enumerate(doc.paragraphs) if pp._p is next_el)
        else:
            for ins in instructions:
                _add_red_para_end(doc, str(ins.get("instruction", "")))
            i = len(doc.paragraphs)
        if new_name:
            # 重命名标题：清掉原有 runs 换新文字，段落样式保持不动
            for r in list(p.runs):
                r._element.getparent().remove(r._element)
            p.add_run(new_name)

    _insert_new_sections(doc, new_sections)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _insert_new_sections(doc, new_sections: list[dict]) -> None:
    """把 AI 新增的章节插入模版：新标题（Heading 样式）+ 红色指令段落。"""
    for ns in new_sections:
        anchor = None
        if ns["after"]:
            anchor = next((p for p in doc.paragraphs if p.text.strip() == ns["after"]), None)
        insert_before = None
        if anchor is not None:
            lvl = _heading_level(anchor)
            found = False
            for p in doc.paragraphs:
                if p._p is anchor._p:
                    found = True
                    continue
                if found and p.style.name.startswith("Heading") and _heading_level(p) <= lvl:
                    insert_before = p
                    break
        style_name = None
        for cand in (f"Heading {ns['level']}", f"标题 {ns['level']}"):
            try:
                doc.styles[cand]
                style_name = cand
                break
            except KeyError:
                continue

        if insert_before is not None:
            hp = insert_before.insert_paragraph_before(style=style_name)
            hp.add_run(ns["title"])
            for ins in ns["instructions"]:
                np = insert_before.insert_paragraph_before()
                run = np.add_run(str(ins.get("instruction", "")))
                run.font.color.rgb = RED
                run.font.size = Pt(12)
                run.font.name = "Times New Roman"
                run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
        else:
            doc.add_paragraph(ns["title"], style=style_name)
            for ins in ns["instructions"]:
                _add_red_para_end(doc, str(ins.get("instruction", "")))


def tasks_from_ai_result(blocks: list[dict], ai_tasks: list[dict]) -> list[dict]:
    """AI 解析结果 + 模版标题，拆解为任务列表（【说明】备注不产生任务）。"""
    queues = build_instr_queues(ai_tasks)
    renames, drops, new_sections = split_structure_edits(ai_tasks)
    blocks = apply_structure_to_blocks(blocks, renames, drops, new_sections)
    tasks: list[dict] = []
    heading_stack: list[int] = []
    sort = 0
    for block in blocks:
        if block["kind"] == "front_title":
            instructions = [
                ins for ins in pop_heading_instructions(queues, block["text"])
                if not is_note_instruction(str(ins.get("instruction", "")))
            ]
            if not instructions:
                continue
            node = {
                "tmp_id": len(tasks),
                "parent_tmp": None,
                "level": 1,
                "title": block["text"],
                "task_type": "heading",
                "instruction": "",
                "sort": sort,
            }
            sort += 1
            tasks.append(node)
            for ins in instructions:
                instruction = str(ins.get("instruction", "")).strip()
                tasks.append({
                    "tmp_id": len(tasks),
                    "parent_tmp": node["tmp_id"],
                    "level": 2,
                    "title": instruction[:60],
                    "task_type": classify_instruction(instruction),
                    "instruction": instruction,
                    "sort": sort,
                })
                sort += 1
            continue
        if block["kind"] != "heading":
            continue
        level = block["level"]
        while heading_stack and tasks[heading_stack[-1]]["level"] >= level:
            heading_stack.pop()
        node = {
            "tmp_id": len(tasks),
            "parent_tmp": heading_stack[-1] if heading_stack else None,
            "level": level,
            "title": block["text"],
            "task_type": "heading",
            "instruction": "",
            "sort": sort,
        }
        sort += 1
        tasks.append(node)
        heading_stack.append(node["tmp_id"])
        if "own_instructions" in block:
            instructions = block["own_instructions"]  # 新增章节自带指令
        else:
            instructions = pop_heading_instructions(
                queues, block.get("renamed_from", block["text"])
            )
        for ins in instructions:
            instruction = str(ins.get("instruction", "")).strip()
            if not instruction or is_note_instruction(instruction):
                continue
            tasks.append({
                "tmp_id": len(tasks),
                "parent_tmp": node["tmp_id"],
                "level": level + 1,
                "title": instruction[:60],
                "task_type": classify_instruction(instruction),
                "instruction": instruction,
                "sort": sort,
            })
            sort += 1
    return assign_slot_keys(tasks)
