"""Report-wide editorial copies made after source and numeric validation.

Shared survey methods belong in the existing methodology section. This policy
keeps statistical cells and source evidence intact, moves only reviewed repeated
table locators, and simplifies one exact duplicated empty option placeholder on
the display copy. It does not suppress unknown or result-specific caveats.
"""
from __future__ import annotations

import re

from .operations import content_is_valid
from .report_text import (QUOTED_LITERALS, SourceTextFormatter, caption_droppable_as_repeat,
                          formal_chart_title)


_STRUCTURAL = (
    "题目按现有资料结构对应，尚未取得正式施测编码说明",
    "问卷题目与作答数据依据现有资料作结构对应，尚未取得正式施测编码字典",
    "上述对应依据现有资料结构建立，尚未取得正式施测编码说明",
    "题目与选项依据现有资料作结构对应，尚未取得正式施测编码字典",
    "题目与作答记录的对应依据现有资料作结构对应，尚未取得正式施测编码字典",
    "问卷与作答数据据现有资料结构对应，未取得正式施测编码表",
    "题目对应依据现有资料作结构对应，尚未取得正式施测编码字典",
    "据现有资料结构对应，未取得正式施测编码表",
    "据现有问卷结构对应，未取得正式施测编码表",
    "依据现有资料作结构对应，尚未取得正式施测编码字典",
    "地区及岗位选项依据现有资料作结构对应，尚未取得正式施测编码字典",
    "题目与作答数据依据现有资料作结构对应，尚未取得正式施测编码字典",
    "以下题目依据现有资料作结构对应，尚未取得正式施测编码字典",
    "以下结果依据现有资料作结构对应，尚未取得正式施测编码字典",
    "上述题目依据现有资料作结构对应，尚未取得正式施测编码字典",
    "相关题目依据现有资料作结构对应，尚未取得正式施测编码字典",
    "题目与作答记录依据现有资料作结构对应，尚未取得正式施测编码字典",
    "以下结果据现有资料结构对应，未取得正式施测编码表",
    "相关题目据现有资料结构对应，未取得正式施测编码表",
)

# Exact, number-free method clauses. New/unknown limitations are kept verbatim.
# Longer clauses go first so a shared tail cannot leave an orphan qualifier.
_COMMON_METHODS = (
    "按全部提交记录计数，未经有效性筛选，不按提交编号去重，不等同于有效独立人数",
    "统计单位为提交记录，不等同于有效独立人数",
    "地区题为原问卷未编号基本信息题",
    "现有材料未提供独立受访者识别及有效问卷筛选规则，因此提交总量及各题非空作答记录均不作为有效独立人数，分析仅反映所提供记录中的作答情况，不作全省代表性推断",
    "提交总量与各题非空作答记录数分别统计，均不等于有效独立人数",
    "选择单项的作答统计各选项频次，占比以该题非空作答记录数为分母",
    "多选题在每条记录内对选项去重，频次为勾选该项的记录数，勾选率以该题非空作答记录数为分母",
    "不同对象、不同题义不作共同量表排名",
    "改进意向和偏好不证明已经实施或实施不足，结果不用于声称总体代表性或因果效应",
    "现有统计反映提交及作答记录，不等于有效独立人数，也不据此认定总体代表性或因果效应",
    "不同对象的不同题义不作为共同量表排名",
    "各题非空作答记录不等于有效独立人数，结果不作总体代表性解释",
    "各题非空作答记录也不等于有效独立人数",
    "占比以本题可识别作答记录为基数，频次为作答记录数，不等同于有效独立人数",
    "非空作答记录亦不等同于有效独立人数",
    "按各题非空作答记录计算题内占比，保留全部原选项",
    "频次单位为作答记录",
    "非空作答记录不等于有效独立人数",
    "不同对象、不同题义不作共同量表排名，不推断总体代表性或因果效应",
    "各题基数均为非空作答记录数，不等于有效独立人数",
    "占比以各题非空作答记录为分母，标签同时列示频次与占比，保留原选项顺序及零频选项",
    "各题含义不同，不合并为共同量表或统一正向率",
    "非空作答记录不等于有效独立人数，结果不作总体代表性或因果解释",
    "以本题非空作答记录为分母",
    "各题以非空作答记录为统计基数，不等同于有效独立人数",
    "不同对象的不同题义不构成共同量表排名",
    "各题非空作答记录不等于有效独立人数，结果不作总体代表性或因果效应解释",
    "频次为作答记录数，占比以各题非空作答记录为分母，按原选项顺序呈现",
    "各题题义不同，不作共同量表排名",
    "题内占比为该选项提交记录频次除以本角色本题非空作答记录数，空值排除",
    "作答记录不等于有效独立人数",
    "各题以自身非空作答记录为分母，不等于有效独立人数，不能据此声称总体代表性",
    "条形表示题内占比，标注为频次（作答记录）与占比",
    "保留全部原选项，含零频选项",
    "基数为各题非空作答记录，不等于有效独立人数",
    "各对象题意分别保留，不进行共同量表排名或计算统一教学协同总分",
    "结果不证明总体代表性或因果效应",
    "占比以本题非空作答记录为分母",
    "作答记录不等于有效独立人数，不同题义不构成共同量表",
    "题内占比以该题非空作答记录为分母，空缺不计入",
    "以下基数均为各题非空作答记录，不等于有效独立人数，结果不作总体代表性推断",
    "条长为题内占比，条末标注频次与占比",
    "各题以非空作答记录为基数，均按单选统计，保留全部原选项",
    "不同对象的题义不作为共同量表排名",
    "本题按单选统计，以非空作答记录为基数，作答记录不等于有效独立人数",
    "每条记录内选项去重，以本题非空可识别作答记录为分母",
    "题内占比以各题非空作答记录为分母，缺失作答不计入",
    "频次与基数均为作答记录数，不等于有效独立人数",
    "统计单位为作答记录，非有效独立人数，不证明总体代表性或因果效应",
    "每记录选项去重，勾选率不能相加为整体人数比例",
    "各题基数均为非空作答记录，不等于有效独立人数",
    "单选占比，以本题非空作答记录为分母",
    "不同对象题义不同，不作为共同量表排名",
    "多选题按每条记录内选项去重，以各题非空作答记录为分母",
    "勾选率不能相加为整体人数占比，作答记录不等于有效独立人数",
    "不同对象题义不作共同量表排名",
    "各面板保留原问卷全部选项，条末显示选择记录数与题内多选勾选率",
    "条末标注选择记录数与勾选率",
    "衔接问题题均为多选，按每条记录内选项去重、以各题非空作答记录为分母，所列比例为选项勾选率，不能相加解释为整体人数占比",
    "不同对象的题义不同，不作共同量表排名",
    "题内多选占比为勾选率：每条记录内同一选项去重，分母为本对象整道题可识别的非空作答记录，缺失作答不计入",
    "各项比例不可相加解释为整体人数占比，作答记录不等于有效独立人数",
    "以下多选勾选率按每条记录内选项去重，以各题非空作答记录为分母",
    "同一记录可选多项，比例不能相加解释为整体人数占比，作答记录也不等于有效独立人数",
    "题内占比以各题非空作答记录为分母，空缺不计入",
    "频次与分母均为记录数，不等于有效独立人数",
    "多选按每条作答记录内的选项去重，以本题非空作答记录为分母计算勾选率",
    "同一记录可勾选多项，比例不可相加为整体人数占比",
    "上述措施题为多选，按每条记录内选项去重计算勾选率，同一记录可选多项，比例不能相加解释为整体人数占比",
    "多选按每条记录选项去重，以本题非空作答记录为分母，勾选率不能相加解释为整体人数占比",
    "统计以各题非空作答记录为基础，不等同于有效独立人数",
    "占比以本题非空作答记录为分母，保留全部原选项",
    "频次为作答记录数，占比以本题非空作答记录为分母",
    "作答记录不等于有效独立人数，不据此推断总体代表性",
    "各题基数为非空作答记录，不等于有效独立人数",
    "多选按每记录选项去重，以本题非空作答记录为分母",
    "勾选率为选择记录数除以本题分母，同一记录可勾选多项，各项比例不能相加为整体人数占比",
    "非空作答记录不等于有效独立人数，不作总体代表性或因果效应推断",
    "不同对象题义不同，不作共同量表排名",
    "按每记录选项去重，以各题非空作答记录为分母",
    "与跨学段衔接要求子节共用同一统计结果，不另定分母",
    "同一记录可勾选多项，勾选率不能相加解释为整体人数占比",
    "非空作答记录不等于有效独立人数，基数偏小时谨慎解释",
    "各对象题义不同，不作共同量表排名",
    "改进意向与偏好不证明已实施或实施不足，不推论总体代表性或因果效应",
    "按每条记录的选项去重，以本题非空作答记录为分母",
    "多选勾选率不能相加为整体人数占比",
    "两题均按实际可解析填报作多选统计，每记录同一选项去重，以各题非空作答记录为分母，空缺不计入",
    "以下基数为各题非空作答记录，不等于有效独立人数",
    "多选按每条记录去重统计勾选率，各项比例不能相加解释为整体人数占比",
    "非空作答记录不等于有效独立人数，多选按每记录选项去重，以本题非空作答记录为分母，勾选率不能相加为整体人数比例",
    "不同对象题义有别，不宜作共同量表排名或总体代表性推断",
    "各题非空作答记录不等于有效独立人数，不同对象的不同题义不作为共同量表比较",
    "各题非空作答记录不等于有效独立人数，不同对象不同题义不作为共同量表排名，多选按每记录选项去重计算勾选率，同一记录可选多项，各选项比例不能相加解释为整体人数比例",
    "各题非空作答记录不等于有效独立人数",
    "各题基数为非空作答记录，非有效独立人数",
    "多选按记录内选项去重，勾选率不可相加为整体人数占比",
    "以下基数均为各题非空作答记录，不等于有效独立人数",
    "多选按每记录去重计算勾选率，各项比例不可相加",
    "多选结果为每记录选项去重后的勾选率，同一记录可选多项，比例不能相加解释为整体人数占比",
    "同一记录可勾选多项，各项勾选率不能相加解释为整体人数占比",
    "各题分母分别核定，不以提交总量替代",
    "同一记录可勾选多项，各项比例不能相加解释为整体人数占比",
    "非空作答记录不等于有效独立人数，基数偏小时谨慎解释",
    "多选勾选率不能相加解释为整体人数占比",
    "选项勾选率不能相加解释为整体人数占比",
    "各选项勾选率不能相加解释为整体人数比例",
    "两类对象分别展示，不合并频次，不作为共同量表排名，偏好不证明政策效果或已实施",
)

PUBLIC_METHODS = (
    "题目按所提供问卷及作答资料分别核对，施测版本的编码说明尚待确认；无法确定对应关系的题目不作实质分析。",
    "统计以问卷作答记录为单位。重复提交与问卷质量尚未完成核验，记录数不等同于独立受访者人数。各题分别计算频次和比例，未作答的记录不计入该题基数。",
    "多选题在每条作答中对重复选项去重，比例为选择该项的记录数占该题作答记录数的比重，各项比例不可相加。“其他”仅列示选择记录，未提供的开放填写内容不作解释。",
    "各对象的题目和观察层次分别分析。结果用于描述本次作答，不用于总体或因果推断；措施偏好表示改进意向，不等同于实施情况或实际成效。",
)

_QUOTED = QUOTED_LITERALS
_SENTENCE_PARTS = re.compile(r"([。；;\n]+)")
_QUESTION_ROLE = r"小学生|中学生|大学生|教师|教育主管部门"

# Reviewed, number-free wording changes retain the actual research limits.
_NARRATIVE_PHRASES = {
    "缺失或不可识别作答按实际材料逐项核验，不预设其存在，也不据此直接删除提交记录":
        "各题统计采用可识别的作答记录，缺失或不可识别的回答不计入该题基数，提交记录仍保留",
    "不生成访谈引语、有效独立人数或信效度结论，也不据此声称总体代表性或因果效应":
        "现有资料不足以支持访谈引语、有效独立人数及信效度结论",
}

# These complete sentences have been reviewed against the source questions.
# The one removable question locator is audit metadata, not a statistical value.
# All other numeric tokens remain protected by the narrative guard below.
_REVIEWED_SENTENCES = {
    "实际地区记录完整，未发现缺失或非法类别；贵州师范大学及其附属学校作为独立地区类别": (
        "“贵州师范大学及其附属学校”沿用问卷原分类，含义不同于行政地区", ""),
    "题干以参与后为前提；未核定参与资格，仅展示本题作答分布，不代表参与率": (
        "本题询问参与后的感受；现有回答尚未确认是否实际参与，不能据此估算参与率", ""),
    "原题以“学校无大中小学思政教育一体化校本课程”为条件，条件资格及跳题规则未能确认，不据教师第12题筛选，仅报告本题实际作答记录，不能解释为符合条件学校的活动分布": (
        "本题面向未开设一体化校本课程的学校；现有回答尚未按这一条件筛选，结果仅呈现本题反馈", "教师第12题"),
}

_OTHER_NOTES = (
    "“其他”不扩写为无原文的活动",
    "“其他”仅报告勾选，不推断具体内容",
)

_COLUMN_LABELS = {
    "短主题": "调查内容", "原件题号": "问卷题号", "原问卷题号": "问卷题号",
    "原选项": "选项", "选项原文": "选项",
    "提交记录频次": "选择记录数", "频次（作答记录）": "选择记录数",
    "作答记录分母": "本题作答记录数", "该题作答记录数": "本题作答记录数",
    "作答基数": "本题作答记录数", "题内多选占比": "多选勾选率",
    "教师问卷原题号": "教师题号", "教育主管部门问卷原题号": "主管部门题号",
    "原件题号（教师问卷）": "教师题号", "原件题号（教育主管部门问卷）": "主管部门题号",
    "教师题目短主题": "教师调查内容", "教育主管部门题目短主题": "主管部门调查内容",
    "教师频次（作答记录）": "教师选择记录数", "教育主管部门频次（作答记录）": "主管部门选择记录数",
    "教师题内占比": "教师占比", "教育主管部门题内占比": "主管部门占比",
    "教师题内勾选率": "教师勾选率", "教育主管部门题内勾选率": "主管部门勾选率",
    "教育主管部门选择记录数": "主管部门选择记录数",
    "教师该题作答记录数": "教师本题作答记录数", "教育主管部门该题作答记录数": "主管部门本题作答记录数",
}


# Only these reviewed comparison schemas can move repeated question locators
# into a note. In particular, the varying survey-content column of the
# ten-column schema remains in the table.
_COMPARISON_COLUMN_ALIASES = {
    "教育主管部门题号": "主管部门题号",
    "教育主管部门调查内容": "主管部门调查内容",
    "教育主管部门占比": "主管部门占比",
    "教育主管部门勾选率": "主管部门勾选率",
    "教育主管部门本题作答记录数": "主管部门本题作答记录数",
}
_COMPARISON_TABLE_SCHEMAS = {
    ("教师题号", "教师调查内容", "主管部门题号", "主管部门调查内容", "选项",
     "教师选择记录数", "教师占比", "主管部门选择记录数", "主管部门占比",
     "教师本题作答记录数", "主管部门本题作答记录数"): (
        4, ("选项", "教师记录数", "教师占比", "主管部门记录数", "主管部门占比",
            "教师作答数", "主管部门作答数")),
    ("教师题号", "主管部门题号", "调查内容", "选项", "教师选择记录数", "教师勾选率",
     "主管部门选择记录数", "主管部门勾选率", "教师本题作答记录数",
     "主管部门本题作答记录数"): (
        2, ("调查内容", "选项", "教师记录数", "教师勾选率", "主管部门记录数",
            "主管部门勾选率", "教师作答数", "主管部门作答数")),
}
_EMPTY_OTHER_PAIR = re.compile(
    r"其他（请简要填写：[ \t\u3000]*）；其他（请简要填写：[ \t\u3000]*）\Z"
)


def _compact_comparison_table(table: dict) -> None:
    """压缩对比表中重复出现的定位列（仅作用于展示副本，统计单元格原样保留）。"""
    columns, rows = table.get("columns"), table.get("rows")
    if not isinstance(columns, list) or not all(isinstance(item, str) for item in columns):
        return
    schema = tuple(_COMPARISON_COLUMN_ALIASES.get(item, item) for item in columns)
    configuration = _COMPARISON_TABLE_SCHEMAS.get(schema)
    if configuration is None or not isinstance(rows, list) or not rows:
        return
    removed, new_columns = configuration
    if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
        return
    locators = rows[0][:removed]
    if (not all(isinstance(value, str) and value.strip() for value in locators)
            or any(row[:removed] != locators for row in rows)):
        return
    notes = table.get("notes")
    if notes is not None and not isinstance(notes, (str, list)):
        return
    if isinstance(notes, list) and not all(isinstance(note, str) for note in notes):
        return

    if removed == 4:
        context = (f"题目：教师问卷{locators[0]}（{locators[1]}）；"
                   f"教育主管部门问卷{locators[2]}（{locators[3]}）。")
    else:
        context = f"题目：教师问卷{locators[0]}；教育主管部门问卷{locators[1]}。"
    existing_notes = notes if isinstance(notes, list) else [notes] if notes else []
    table["notes"] = [context, *existing_notes]
    table["columns"] = list(new_columns)
    table["rows"] = [row[removed:] for row in rows]


def _simplify_empty_other_option(table: dict) -> None:
    """Hide only a duplicated, unfilled questionnaire placeholder pair."""
    columns, rows = table.get("columns"), table.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return
    option_columns = [index for index, value in enumerate(columns) if value == "选项"]
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            continue
        for index in option_columns:
            value = row[index]
            if isinstance(value, str) and _EMPTY_OTHER_PAIR.fullmatch(value):
                row[index] = "其他"


# Individual disclosures permit natural source wording and minimal completion.
# The matcher requires an actual limitation, rather than merely a keyword.
_METHOD_RULES = (
    ("mapping", (r"(?:现有资料[^。\n]*?(?:对应|结构)|(?:题目|问卷|数据)[^。\n]*?(?:核对|对应))",),
     "题目按现有问卷和作答资料分别对应，无法确定对应关系的题目不作实质解释。"),
    ("coding", (r"(?:尚未|未)(?:取得|获得|提供)[^。\n]*?(?:正式施测|正式编码)[^。\n]*?(?:字典|说明|表)",
                r"(?:施测版本的编码|正式施测编码)说明尚待(?:确认|补充)"),
     "正式施测编码说明尚待确认。"),
    ("record_unit", (r"(?:统计[^。\n]*?以[^。\n]*?记录为(?:统计)?单位|统计单位[^。\n]*?记录|以提交记录及各题作答记录为统计单位)",),
     "统计以问卷作答记录为单位。"),
    ("identification", (r"(?:不等(?:同)?于[^。\n]*?(?:有效)?独立[^。\n]*?(?:人数|受访者)|(?:未(?:进行|作|完成|提供)|尚未|缺少|缺乏|无)[^。，；\n]*?独立受访者识别)",),
     "记录数不等同于独立受访者人数。"),
    ("screening", (r"(?:未(?:进行|作|完成|提供)|未经|缺少|缺乏|无)[^。\n]*?(?:有效(?:性|问卷)[^。\n]*?筛选|有效性规则|问卷质量筛选)",
                   r"(?:重复提交|问卷质量)[^。\n]*?(?:尚未|未)[^。\n]*?(?:核验|筛选)"),
     "重复提交与问卷质量尚未完成核验。"),
    ("denominator", (r"(?:非空[^。\n]*?作答记录|未作答(?:或无法识别)?[^。\n]*?不计入[^。\n]*?(?:基数|分母))",),
     "各题比例以该题非空且可识别的作答记录为基数，未作答或无法识别的回答不计入该题分母。"),
    ("multiple", (r"多选[^。\n]*?(?:每[^。\n]*?记录|每条作答)[^。\n]*?去重",),
     "多选题按每条记录内的选项去重，比例以该题作答记录数为基数。"),
    ("nonadditive", (r"(?:不能|不可)[^。\n]*?相加",),
     "同一记录可选择多项，不能将各项比例相加解释为整体记录占比。"),
    ("other", (r"“其他”[^。\n]*?(?:不作解释|不解释|不延伸解释|不推断|不扩写|不作[^。\n]*?解释)",),
     "“其他”仅报告选择情况，不解释未提供的开放回答。"),
    ("separate", (r"(?:不作|不作为|不构成|不进行|不合并为)[^。\n]*?共同量表",
                  r"各对象[^。\n]*?(?:分别分析|分别报告)",
                  r"各对象的题目和观察层次分别分析",
                  r"不同对象按各自题义分别报告"),
     "不同对象按各自题义分别报告，不作共同量表排名。"),
    ("population", (r"(?:不作|不推断|不用于|不能据此|不能推广|不代表)[^。\n]*?(?:总体代表性|全省总体|全省代表性|总体推断)",
                    r"不用于总体或因果推断"),
     "结果仅描述本次作答，不代表全省总体状况。"),
    ("causal", (r"(?:不作|不推断|不用于|不能据此)[^。\n]*?因果",),
     "结果不用于推断因果关系。"),
    ("intentions", (r"(?:意向|偏好)[^。\n]*?(?:不等同于|不证明)[^。\n]*?(?:实施|成效)",
                    r"改进选择反映需求和意向"),
     "改进选择反映需求和意向，不等同于实施情况或实际成效。"),
)
_METHOD_GROUPS = (
    {"mapping", "coding"},
    {"record_unit", "identification", "screening", "denominator"},
    {"multiple", "nonadditive", "other"},
    {"separate", "population", "causal", "intentions"},
)

_METHOD_EXCLUSIONS = {
    "coding": (
        r"(?:正式施测|正式编码)[^。\n]*(?:已|已经)(?:取得|获得|提供)",
        r"(?:已|已经)(?:取得|获得|提供)[^。\n]*(?:正式施测|正式编码)[^。\n]*(?:字典|说明|表)",
    ),
    "multiple": (
        r"(?:未|不|尚未)[^。\n]*(?:按|对)[^。\n]*(?:记录|选项)[^。\n]*去重",
        r"(?:不去重|未去重|尚未去重)",
    ),
    "denominator": (
        r"(?:非空作答记录|作答记录)[^。\n]*(?:不作为|不作|不是|并非)[^。\n]*(?:分母|基数)",
        r"(?:不在|未在)[^。\n]*(?:交代|说明)[^。\n]*(?:分母|基数)",
    ),
}


def _methodology_coverage(text: str) -> set[str]:
    covered = set()
    for key, patterns, _ in _METHOD_RULES:
        if any(re.search(pattern, text) for pattern in patterns):
            exclusions = _METHOD_EXCLUSIONS.get(key, ())
            if not any(re.search(pattern, text) for pattern in exclusions):
                covered.add(key)
    return covered


def has_public_methodology(text: str) -> bool:
    """Only consolidate when a real survey-method section covers every rule."""
    confirmed = (
        r"(?:已|已经)(?:取得|获得|提供)[^。，；\n]*?(?:正式施测|正式编码)[^。，；\n]*?(?:字典|说明|表)",
        r"(?:已|已经)(?:进行|完成)[^。，；\n]*?(?:独立受访者识别|有效(?:性|问卷)[^。，；\n]*?筛选)",
    )
    if any(re.search(pattern, text) for pattern in confirmed):
        return False
    # Confirmation can appear after the subject ("编码说明已经取得"), so
    # check both word orders before considering the natural wording complete.
    if any(re.search(pattern, text) for pattern in _METHOD_EXCLUSIONS["coding"]):
        return False
    return len(_methodology_coverage(text)) == len(_METHOD_RULES)


def make_project_formatter(project, tasks, current, data_file_names):
    """Use the same valid current methodology for report and page previews.

    A missing or outdated preamble must not authorize removing disclosures
    from another module. Historical versions retain their original wording.
    """
    ordered = sorted(tasks, key=lambda task: (task.sort, task.id))
    first_body = next((task.sort for task in ordered if task.task_type == "heading"
                       and task.title.strip().replace(" ", "") not in {"说明", "前言", "序言"}), None)
    preamble = [task for task in ordered if task.task_type == "text"
                and first_body is not None and task.sort < first_body]
    complete = bool(preamble) and all(
        (content := current.get(task.id)) is not None
        and content.is_current and content.content_type == "text"
        and content_is_valid(content, task, project)
        for task in preamble
    )
    methods = [task for task in preamble if re.search(r"统计(?:口径|方法)|调查方法", task.title)]
    method_task = methods[0] if complete and len(methods) == 1 else None
    method_text = "\n".join(current[task.id].content for task in preamble) if complete else ""
    consolidate = bool(method_task and has_public_methodology(method_text))
    context = "\n".join(current[task.id].content for task in preamble
                        if task.id != method_task.id) if consolidate else ""
    formatter = ReportEditorialFormatter(list(data_file_names), consolidate_methods=consolidate,
                                         methodology_context=context)
    return formatter, method_task if consolidate else None


def _outside_quotes(text: str, change, *, with_offset: bool = False) -> str:
    parts, cursor = [], 0
    def apply(segment, offset):
        return change(segment, offset) if with_offset else change(segment)
    for match in _QUOTED.finditer(text):
        parts.extend((apply(text[cursor:match.start()], cursor), match.group()))
        cursor = match.end()
    parts.append(apply(text[cursor:], cursor))
    return "".join(parts)


def _rewrite_reviewed_sentences(value: str) -> tuple[str, str]:
    """Return edited prose and its numeric reference; protect literal quotations."""
    quoted = [match.span() for match in _QUOTED.finditer(value)]
    pattern = re.compile("|".join(re.escape(sentence) for sentence in _REVIEWED_SENTENCES))
    edited, numeric_reference, cursor = [], [], 0
    for match in pattern.finditer(value):
        # A quoted option or quotation containing the whole wording is a source
        # literal. The reviewed condition sentence may itself contain an option.
        if any(start <= match.start() and match.end() <= end for start, end in quoted):
            continue
        preceding = value[:match.start()].rstrip(" \t\r")
        if preceding and preceding[-1] not in "。；;\n":
            continue
        if match.end() < len(value) and value[match.end()] not in "。；;\n":
            continue
        replacement, locator = _REVIEWED_SENTENCES[match.group()]
        edited.extend((value[cursor:match.start()], replacement))
        numeric_reference.extend((value[cursor:match.start()], match.group().replace(locator, "", 1) if locator else match.group()))
        cursor = match.end()
    edited.append(value[cursor:])
    numeric_reference.append(value[cursor:])
    return "".join(edited), "".join(numeric_reference)


class ReportEditorialFormatter(SourceTextFormatter):
    def __init__(self, data_file_names: list[str], *, consolidate_methods: bool = False,
                 methodology_context: str = ""):
        super().__init__(data_file_names)
        self.consolidate_methods = consolidate_methods
        self.methodology_context = methodology_context
        phrases = sorted(set(_STRUCTURAL + _COMMON_METHODS), key=len, reverse=True)
        assert all(not re.search(r"\d", phrase) for phrase in phrases)
        self._method_pattern = re.compile(
            r"(?:(?<=^)|(?<=[，,：:]))[ \t]*(?:" + "|".join(map(re.escape, phrases)) + r")[ \t]*(?=$|[，,：:])"
        )

    def _edit(self, text: str) -> str:
        if not self.consolidate_methods:
            return text
        def change(segment):
            parts = _SENTENCE_PARTS.split(segment)
            for index in range(0, len(parts), 2):
                edited, count = self._method_pattern.subn("\ue000", parts[index])
                if not count:
                    continue
                edited = re.sub(r"^[ \t]*\ue000[，,：:]?", "", edited)
                edited = re.sub(r"[，,：:]?\ue000(?=[ \t]*$)", "", edited)
                edited = re.sub(r"[，,：:]\ue000[，,：:]", "，", edited)
                parts[index] = edited.replace("\ue000", "")
                parts[index] = re.sub(r"[，,：:]{2,}", "，", parts[index])
            return "".join(parts)
        result = _outside_quotes(text, change)
        if result == text:
            return text
        # Mask literal option text while repairing only deletion boundaries.
        # Its punctuation is source material, even when it looks irregular.
        marker = "\ue100"
        while marker in result:
            marker += "\ue100"
        quoted = {}
        def protect(match):
            key = f"{marker}{len(quoted)}\ue101"
            quoted[key] = match.group()
            return key
        result = _QUOTED.sub(protect, result)
        # Remove the punctuation of emptied clauses/paragraphs without merging
        # adjacent results or modifying decimal points and option punctuation.
        result = re.sub(r"[。；;][。；;]+", "。", result)
        result = re.sub(r"(?m)^[\s。；;，,:：]+(?=\S)", "", result)
        result = re.sub(r"[。；;，,:：]+\s*(?=\n|$)", "。", result)
        result = re.sub(r"\n[\s。；;，,:：]*\n", "\n\n", result)
        result = result.strip(" \n。；;，,:：")
        if result and result[-1] not in "。！？?!":
            result += "。"
        for key, original in quoted.items():
            result = result.replace(key, original)
        if re.findall(r"\d+(?:\.\d+)?", text) != re.findall(r"\d+(?:\.\d+)?", result):
            raise ValueError("报告编辑改变了数字，拒绝导出")
        return result

    def _narrative(self, value: str) -> str:
        if not self.consolidate_methods:
            return value
        value, numeric_reference = _rewrite_reviewed_sentences(value)
        unquoted = _QUOTED.sub(lambda match: " " * len(match.group()), value)
        def change(text, offset):
            preceding = unquoted[:offset]
            initial_depth = sum(preceding.count(c) for c in "（(") - sum(preceding.count(c) for c in "）)")
            for old, new in _NARRATIVE_PHRASES.items():
                text = text.replace(old, new)
            text = re.sub(
                rf"(?P<role>{_QUESTION_ROLE})全部提交记录为(?P<count>\d+)条，本题未见缺失或不可解析作答[；;]比例以(?P=role)全部提交记录为分母(?=$|[。；;\n])",
                r"\g<role>本题作答\g<count>条", text,
            )
            text = re.sub(rf"({_QUESTION_ROLE})(?:问卷)?原第", r"\1问卷第", text)
            def denominator(match):
                prefix = match.string[:match.start()]
                inside = initial_depth + sum(prefix.count(c) for c in "（(") - sum(prefix.count(c) for c in "）)") > 0
                label = f"本题作答{match.group(1)}条"
                return label if inside else f"（{label}）"
            text = re.sub(r"以本题非空作答记录数(\d+)条为分母", denominator, text)
            text = re.sub(r"以本题(\d+)条非空作答记录为分母", denominator, text)
            text = re.sub(r"以(\d+)条非空作答记录为分母", denominator, text)
            text = re.sub(r"本题分母为(\d+)条非空作答记录", denominator, text)
            text = re.sub(r"非空作答记录(?:分母|基数)为(\d+)条", r"作答记录为\1条", text)
            text = re.sub(r"本题(\d+)条非空作答记录为分母", r"本题作答\1条", text)
            text = re.sub(r"(问卷第\d+题)[，,](（本题作答\d+条）)[，,]", r"\1\2，", text)
            text = text.replace("非空作答记录", "作答记录")
            text = text.replace("频次最高的选项为", "选择最多的是")
            text = text.replace("最常见选项为", "选择最多的是")
            return text
        result = _outside_quotes(value, change, with_offset=True)
        if re.findall(r"\d+(?:\.\d+)?", numeric_reference) != re.findall(r"\d+(?:\.\d+)?", result):
            raise ValueError("报告措辞调整改变了数字，拒绝导出")
        return result

    def text(self, value: str, *, methodology: bool = False) -> str:
        original = super().text(value)
        result = self._narrative(self._edit(original))
        if methodology and self.consolidate_methods:
            # The source description may legitimately live in another preamble
            # module. Check its display wording too, so an omitted old clause
            # cannot incorrectly count as an already visible disclosure.
            context = self._narrative(self._edit(super().text(self.methodology_context)))
            covered = _methodology_coverage(result + "\n" + context)
            missing = ["".join(sentence for key, _, sentence in _METHOD_RULES
                               if key in group and key not in covered) for group in _METHOD_GROUPS]
            result = "\n\n".join(filter(None, (result, *missing)))
        return result

    def _note_value(self, value):
        if isinstance(value, str):
            if self.consolidate_methods:
                for phrase in _OTHER_NOTES:
                    # Complete note clauses only; option cells/labels and any
                    # longer, result-specific explanation remain untouched.
                    value = re.sub(r"(^|[。；;\n])[ \t]*" + re.escape(phrase) + r"[ \t]*([。；;\n]|$)",
                                   r"\1", value)
            edited = self._narrative(self._edit(value))
            return edited.strip() if self.consolidate_methods else edited
        if isinstance(value, list):
            return [edited for item in value if (edited := self._note_value(item))]
        return value

    def table(self, payload: dict, *, module_title: str = "") -> dict:
        display = super().table(payload)
        def edit(table, parents=()):
            if isinstance(table.get("columns"), list):
                table["columns"] = [_COLUMN_LABELS.get(item, item)
                                    if isinstance(item, str) else item for item in table["columns"]]
            number = re.match(r"^表\s*\d+\b", module_title)
            if number and isinstance(table.get("title"), str) and not re.match(r"^表\s*\d+", table["title"]):
                table["title"] = f"{number.group()} {table['title']}"
            for field in ("note", "notes"):
                if field in table:
                    table[field] = self._note_value(table[field])
            _compact_comparison_table(table)
            _simplify_empty_other_option(table)
            if isinstance(table.get("table"), dict):
                edit(table["table"], (table, *parents))
        edit(display)
        return display

    def chart(self, metadata: dict) -> dict:
        display = super().chart(metadata)
        def edit(chart):
            original_title = chart.get("title")
            if isinstance(chart.get("title"), str):
                chart["title"] = formal_chart_title(chart["title"])
                chart["title"] = re.sub(r"：分对象、分题作答分布$", "", chart["title"])
            for field in ("note", "notes", "caption"):
                if field in chart:
                    chart[field] = self._note_value(chart[field])
            if isinstance(chart.get("caption"), str):
                # The locator remains in the original metadata; a caption that
                # restates the topic adds no information below a titled chart.
                original_caption = chart["caption"]
                caption = formal_chart_title(original_caption)
                chart["caption"] = ("" if caption_droppable_as_repeat(original_title, original_caption)
                                    else original_caption)
            if isinstance(chart.get("source"), str):
                source = chart["source"]
                source = re.sub(
                    rf"({_QUESTION_ROLE})(?:问卷)?(?:原)?第[0-9一二三四五六七八九十、—至]+题(?:及其作答记录)?",
                    r"\1问卷", source,
                )
                chart["source"] = (source if source.startswith(("来源：", "数据来源：")) else f"来源：{source}。") if source.strip() else ""
            if isinstance(chart.get("charts"), list):
                for child in chart["charts"]:
                    if isinstance(child, dict):
                        edit(child)
        edit(display)
        return display
