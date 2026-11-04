# -*- coding: utf-8 -*-
"""离线验证 template_parse：用合成模版/问卷/数据文件测试，不调用模型。"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from offline_support import install_offline_environment
env = install_offline_environment()

from docx import Document
from docx.oxml.ns import qn

from app import template_parse as tp

TEMPLATE = env.template
XLSX = env.xlsx
REF = env.reference
REF_BIG = env.reference_big


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def main():
    template_bytes = TEMPLATE.read_bytes()

    # 1) 结构提取
    blocks = tp.extract_structure(template_bytes)
    headings = [b for b in blocks if b["kind"] == "heading"]
    front_titles = [b for b in blocks if b["kind"] == "front_title"]
    images = [b for b in blocks if b["kind"] == "image"]
    tables = [b for b in blocks if b["kind"] == "table"]
    print(f"headings={len(headings)} front_titles={[b['text'] for b in front_titles]} "
          f"images={len(images)} tables={len(tables)}")
    check(len(headings) == 27, "识别到 27 个标题")
    check([b["text"] for b in front_titles] == ["说 明"], "前置小节只识别出“说 明”（目录/落款不算）")
    check(len(images) == 90 and len(tables) == 4, "识别到 90 张图和 4 个表")

    # 2) 结构摘要文本
    summary = tp.structure_summary(blocks)
    check("# 第一部分 概述" in summary, "结构摘要含一级标题")
    check("◇ 说 明" in summary, "结构摘要含前置小节标记")
    check("[图片]" in summary and "[表格]" in summary, "结构摘要含图表占位")

    # 3) 数据结构摘要（不再包含台账统计，只描述表结构与资料自带编码对照）
    data_summary = tp.summarize_data_structure(XLSX.name, XLSX.read_bytes())
    print("---- data summary (head) ----")
    print(data_summary[:900])
    check("1066" in data_summary, "数据摘要含总行数 1066")
    check("education_supervisor" in data_summary, "数据摘要含资料自带的编码对照")
    check("原始样例" in data_summary, "数据摘要含样例行")
    check("run_python" in data_summary, "数据摘要提示统计需由代码计算")
    # 普通 CSV（无编码对照列）不应出现编码对照块
    plain_csv = "姓名,成绩\n张三,90\n李四,85\n" * 6
    plain_summary = tp.summarize_data_structure("成绩表.csv", plain_csv.encode("utf-8"))
    check("编码对照" not in plain_summary, "无编码对照列时不生成对照块")

    # 4) 参考资料摘要
    ref_summary = tp.summarize_reference(REF.name, REF.read_bytes())
    check("你觉得学校的思政课堂和活动" in ref_summary, "参考摘要含问卷题目")
    check("最需要改进的地方" in ref_summary, "小问卷摘要覆盖到最后一题（未提前截断）")
    big_summary = tp.summarize_reference(REF_BIG.name, REF_BIG.read_bytes())
    check("亟须补齐的短板" in big_summary, "大问卷摘要覆盖到第六部分末尾题目（未截断）")

    # 5) AI 指令回写（模拟 AI 输出；故意留“3、行为感知”不覆盖，验证安全兜底）
    ai_tasks = [
        {"heading": "说明", "instructions": [
            {"type": "text", "instruction": "【填写：根据新数据改写调查背景与样本情况，共1066份问卷。】"},
        ]},
        {"heading": "一、目的", "instructions": [
            {"type": "text", "instruction": "【填写调查目的：依据五类问卷说明调查目的。】"},
        ]},
        {"heading": "二、调查内容", "instructions": [
            {"type": "table", "instruction": "【生成表格“调查维度与监测工具对应表”：维度/内容/工具。】"},
        ]},
        {"heading": "1、认知理解", "instructions": [
            {"type": "text", "instruction": "【填写文字分析：依据大学生问卷第1题分析。】"},
            {"type": "image", "instruction": "【生成图片（柱状图）：大学生问卷第1题各选项占比。】"},
            {"type": "note", "instruction": "【说明：原红领巾认知题目在新问卷中无对应内容，已删除。】"},
        ]},
        # 同名标题：三条不同的小结指令应按顺序落到三个“4、小结”
        {"heading": "4、小结", "instructions": [
            {"type": "text", "instruction": "【填写小结：主体认知部分的总结。】"},
        ]},
        {"heading": "4、小结", "instructions": [
            {"type": "text", "instruction": "【填写小结：要素协同部分的总结。】"},
        ]},
        {"heading": "4、小结", "instructions": [
            {"type": "text", "instruction": "【填写小结：评估与衔接部分的总结。】"},
        ]},
    ]
    new_bytes = tp.write_instructions_to_docx(template_bytes, ai_tasks)
    doc = Document(io.BytesIO(new_bytes))
    heads2 = [p.text.strip() for p in doc.paragraphs if p.style.name.startswith("Heading")]
    check(heads2 == [b["text"] for b in headings], "回写后标题完全不变")

    paras = doc.paragraphs

    def red_paras():
        return [p for p in paras if any(
            r.font.color and r.font.color.rgb is not None and str(r.font.color.rgb) == "FF0000"
            for r in p.runs)]

    reds = red_paras()
    check(len(reds) == 9, f"写入 9 段红字（实际 {len(reds)}）")

    def section_of(para):
        idx = next(i for i, p in enumerate(paras) if p._p is para._p)
        for k in range(idx, -1, -1):
            if paras[k].style.name.startswith("Heading"):
                return paras[k].text.strip()
        return "(front)"

    red_sections = [(section_of(p), p.text[:30]) for p in reds]
    for sec, t in red_sections:
        print("  red under:", sec, "|", t)
    check(sum(1 for s, _ in red_sections if s == "1、认知理解") == 3, "认知理解下 3 段红字")
    check(sum(1 for s, _ in red_sections if s == "(front)") == 1, "说明小节 1 段红字")

    # 同名“4、小结”按顺序各得其所
    xiaojie = [t for s, t in red_sections if "填写小结" in t]
    check(len(xiaojie) == 3 and "主体认知" in xiaojie[0] and "要素协同" in xiaojie[1]
          and "评估与衔接" in xiaojie[2], f"同名小结按顺序分配（{xiaojie}）")

    # 前置说明：标题保留、旧正文删除、落款和目录保留
    texts = [p.text.strip() for p in paras]
    check("说 明" in texts, "“说 明”标题保留")
    check(not any("3647" in t for t in texts), "旧说明中的老数据已删除")
    check("贵州省基础教育研究院" in texts, "落款单位保留")
    check("目 录" in texts, "目录保留")

    # 覆盖到的章节：旧图表清除
    img_count = sum(1 for p in paras if p._p.findall(".//" + qn("w:drawing")))
    tbl_count = len(doc.tables)
    print(f"remaining images={img_count} tables={tbl_count}")
    check(img_count < 90, "覆盖章节的旧图已删除")

    # 未覆盖章节“3、行为感知”内容保留（安全兜底）
    idx_behavior = next(i for i, p in enumerate(paras) if p.text.strip() == "3、行为感知")
    nxt = paras[idx_behavior + 1].text
    check("前学段对现学段的影响量表描述" in nxt, "未覆盖章节内容保持原样")

    # 6) 任务拆解
    blocks2 = tp.extract_structure(template_bytes)
    tasks = tp.tasks_from_ai_result(blocks2, ai_tasks)
    notes = [t for t in tasks if "【说明" in t["instruction"]]
    check(not notes, "【说明】备注不产生任务")
    front_nodes = [t for t in tasks if t["title"] == "说 明" and t["task_type"] == "heading"]
    check(len(front_nodes) == 1, "前置小节生成任务节点")
    types = [t["task_type"] for t in tasks if t["task_type"] != "heading"]
    check(types.count("table") == 1 and types.count("image") == 1 and types.count("text") == 6,
          f"任务类型分类正确（{types}）")

    # 7) 结构编辑回写：rename / drop / new_section
    struct_ai = [
        {"heading": "三、要素协同", "rename_to": "三、课程体系协同", "instructions": [
            {"type": "text", "instruction": "【填写：依据问卷课程体系维度撰写。】"},
        ]},
        {"heading": "五、综合反馈", "drop": True, "note": "新问卷无对应维度"},
        {"action": "new_section", "after": "四、评估与衔接", "level": 2,
         "title": "五、本土资源利用", "instructions": [
            {"type": "text", "instruction": "【填写：依据问卷第五维度撰写。】"},
            {"type": "image", "instruction": "【生成图片（柱状图）：本土资源融入效果。】"},
        ]},
    ]
    edited = tp.write_instructions_to_docx(template_bytes, struct_ai)
    edoc = Document(io.BytesIO(edited))
    eheads = [p.text.strip() for p in edoc.paragraphs if p.style.name.startswith("Heading")]
    check("三、课程体系协同" in eheads and "三、要素协同" not in eheads, "标题重命名生效")
    check("五、综合反馈" not in eheads, "drop 章节标题已删除")
    check("1、满意程度" not in eheads, "drop 章节的子标题一并删除")
    check("五、本土资源利用" in eheads, "新增章节标题已插入")
    i_eval = eheads.index("四、评估与衔接")
    i_new = eheads.index("五、本土资源利用")
    i_part3 = next(k for k, h in enumerate(eheads) if h.startswith("第三部分"))
    check(i_eval < i_new < i_part3, "新增章节插入位置正确")
    paras_e = edoc.paragraphs
    idx_new = next(k for k, p in enumerate(paras_e) if p.text.strip() == "五、本土资源利用")
    nxt = paras_e[idx_new + 1]
    is_red = any(
        r.font.color and r.font.color.rgb is not None and str(r.font.color.rgb) == "FF0000"
        for r in nxt.runs
    )
    check("依据问卷第五维度" in nxt.text and is_red, "新增章节含红色指令")

    # 8) 结构编辑后的任务拆解
    etasks = tp.tasks_from_ai_result(tp.extract_structure(template_bytes), struct_ai)
    etitles = [t["title"] for t in etasks]
    check("三、课程体系协同" in etitles and "三、要素协同" not in etitles, "任务树标题已重命名")
    check("五、综合反馈" not in etitles and "1、满意程度" not in etitles, "drop 章节不产生任务")
    check("五、本土资源利用" in etitles, "新增章节进入任务树")
    ns_node = next(t for t in etasks if t["title"] == "五、本土资源利用")
    kids = [t for t in etasks if t["parent_tmp"] == ns_node["tmp_id"]]
    check(len(kids) == 2 and kids[0]["task_type"] == "text" and kids[1]["task_type"] == "image",
          "新增章节自带指令生成子任务")
    rn_node = next(t for t in etasks if t["title"] == "三、课程体系协同")
    rn_kids = [t for t in etasks
               if t["parent_tmp"] == rn_node["tmp_id"] and t["task_type"] != "heading"]
    check(len(rn_kids) == 1 and "课程体系维度" in rn_kids[0]["instruction"],
          "重命名章节指令按原标题匹配")

    # 9) 指令清洗：元语句改写 + 英文编码转中文短名
    code_map = tp.extract_code_map(data_summary)
    check(code_map.get("education_supervisor", "").startswith("贵州省"),
          "从数据摘要提取编码对照")
    dirty = ("【填写：其中 education_supervisor 453、teacher 336；"
             "将旧称“中小学思想政治一体化建设”统一为“大中小学思想政治教育一体化建设”；"
             "大学样本仅4份，必须提示谨慎解读。】")
    cleaned = tp.sanitize_instruction(dirty, code_map)
    check("旧称" not in cleaned and "统一规范为" not in cleaned, "元语句已改写")
    check("全文统一使用“大中小学思想政治教育一体化建设”表述" in cleaned, "改写为直接表述要求")
    check("education_supervisor" not in cleaned and "教育主管部门问卷" in cleaned,
          "英文编码替换为中文短名")
    check("必须提示谨慎解读" in cleaned, "正常分句保留")
    # 残留元语句的分句删除兜底
    leftover = tp.sanitize_instruction("【填写：写样本构成；旧称见附录说明；不得编造数据。】", {})
    check("旧称" not in leftover and "不得编造数据" in leftover, "残留元语句分句删除")
    # sanitize_ai_tasks 覆盖 instructions 与 note
    ai_dirty = [
        {"heading": "一、概述", "instructions": [{"type": "text", "instruction": dirty}]},
        {"heading": "五、综合反馈", "drop": True, "note": "旧称“X”统一为“Y”，维度不存在"},
        {"action": "new_section", "after": "一、概述", "level": 2, "title": "新章节",
         "instructions": [{"type": "text", "instruction": dirty}]},
    ]
    tp.sanitize_ai_tasks(ai_dirty, code_map)
    check("旧称" not in ai_dirty[0]["instructions"][0]["instruction"], "普通指令已清洗")
    check("旧称" not in ai_dirty[1]["note"] and "统一使用“Y”" in ai_dirty[1]["note"],
          "删除备注已清洗")
    check("教育主管部门问卷" in ai_dirty[2]["instructions"][0]["instruction"],
          "新增章节自带指令已清洗")

    # 10) 两趟解析：结构规划 → 校验归一化 → 指令合并 → 端到端回写
    outline_text = tp.outline_summary(blocks)
    check("# 第一部分 概述" in outline_text and "◇ 说 明" in outline_text,
          "大纲摘要只含标题层级")
    check("[图片]" not in outline_text and "[表格]" not in outline_text,
          "大纲摘要不含正文/图表占位")

    plan = [
        {"heading": "三、要素协同", "rename_to": "三、课程体系协同"},
        {"heading": "五、综合反馈", "drop": True, "reason": "问卷无对应维度"},
        {"heading": "不存在的章节", "drop": True},                      # 无效：忽略
        {"heading": "一、目的", "rename_to": "第一部分 概述"},           # 冲突：忽略
        {"action": "new_section", "after": "三、要素协同", "level": 2,
         "title": "四、本土资源利用"},                                   # 锚点被改名→换成新名
        {"action": "new_section", "after": "五、综合反馈", "level": 2,
         "title": "五、提质优化"},                                       # 锚点被删除→置空
    ]
    renames, drops, new_secs = tp.outline_plan_to_edits(plan, blocks)
    check(renames == {"三、要素协同": "三、课程体系协同"}, "改名校验（冲突/无效被忽略）")
    check(drops == {"五、综合反馈": "问卷无对应维度"}, "删除校验（不存在的章节被忽略）")
    ns_after = {ns["title"]: ns["after"] for ns in new_secs}
    check(ns_after["四、本土资源利用"] == "三、课程体系协同", "锚点随改名归一化")
    check(ns_after["五、提质优化"] == "", "锚点随删除置空")

    edited = tp.apply_structure_to_blocks(blocks, renames, drops, new_secs)
    etexts = [b["text"] for b in edited if b["kind"] == "heading"]
    check("三、课程体系协同" in etexts and "三、要素协同" not in etexts, "重构后大纲已改名")
    check("五、综合反馈" not in etexts and "四、本土资源利用" in etexts, "重构后大纲删除/新增")

    # 第二趟按新大纲输出指令（含对新章节的指令），合并后映射回原标题
    pass2 = [
        {"heading": "三、课程体系协同", "instructions": [
            {"type": "text", "instruction": "【填写：依据课程体系维度撰写。】"}]},
        {"heading": "四、本土资源利用", "instructions": [
            {"type": "text", "instruction": "【填写：依据本土资源维度撰写。】"}]},
        {"heading": "二、调查内容", "instructions": [
            {"type": "text", "instruction": "【填写：概述。】"}]},
        {"heading": "二、调查内容", "rename_to": "乱改"},  # 第二趟的结构操作必须被忽略
    ]
    merged = tp.merge_structure_and_instructions(pass2, renames, drops, new_secs)
    m_rename = next(m for m in merged if m.get("rename_to") == "三、课程体系协同")
    check(m_rename["heading"] == "三、要素协同" and m_rename["instructions"], "改名章节指令映射回原标题")
    m_new = next(m for m in merged if m.get("action") == "new_section"
                 and m["title"] == "四、本土资源利用")
    check(m_new["instructions"] and m_new["after"] == "三、课程体系协同", "新增章节带指令还原")
    m_drop = next(m for m in merged if m.get("drop"))
    check(m_drop["heading"] == "五、综合反馈", "删除项补入合并结果")
    m_ns2 = next(m for m in merged if m.get("action") == "new_section"
                 and m["title"] == "五、提质优化")
    check(m_ns2["instructions"] == [], "第二趟漏掉的新增章节补空指令")
    check(all(not m.get("rename_to") == "乱改" for m in merged), "第二趟结构操作被忽略")

    # 端到端：merged 写回模版 + 拆解任务
    new_docx = tp.write_instructions_to_docx(template_bytes, merged)
    ndoc = Document(io.BytesIO(new_docx))
    nheads = [p.text.strip() for p in ndoc.paragraphs if p.style.name.startswith("Heading")]
    check("三、课程体系协同" in nheads and "三、要素协同" not in nheads, "回写模版标题已改名")
    check("五、综合反馈" not in nheads and "1、满意程度" not in nheads, "回写模版章节已删除")
    check("四、本土资源利用" in nheads and "五、提质优化" in nheads, "回写模版新章节已插入")
    ntasks = tp.tasks_from_ai_result(blocks, merged)
    ntitles = [t["title"] for t in ntasks]
    check("三、课程体系协同" in ntitles and "四、本土资源利用" in ntitles
          and "五、综合反馈" not in ntitles, "任务树与新大纲一致")
    ns_tasks = [t for t in ntasks if "本土资源维度" in t["instruction"]]
    check(len(ns_tasks) == 1, "新增章节的指令生成写作任务")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        env.close()
