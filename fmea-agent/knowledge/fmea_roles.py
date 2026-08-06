"""
FMEA 文档角色规则 — fmea_indexer 与 structure_extractor 共享模块

设计准则：
1. 多源打分 — filename/title/summary/content/extension 各自加权，累加后决出最佳角色
2. 负关键词减分 — 避免"试验报告"被误分为 test_spec
3. 内容采样 — 对无法仅从元数据判定的文档，读取前 N 字符做模式匹配
4. L2 回退 — "other" 文档用内容特征模式做 best-guess，确保不被丢弃
5. 零硬编码 — 所有关键词均为通用术语，不依赖具体产品/项目名
"""

import re
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 正关键词规则（多源权重）
# ═══════════════════════════════════════════════════════════════
# (角色, 类别, 基础优先级, 正关键词列表, 关键词权重)
# 权重含义：filename/hit = 3.0, title/hit = 2.0, summary/hit = 1.0, content/hit = 4.0
ROLE_RULES: List[Tuple[str, str, float, List[str], float]] = [
    # BOM — 物料清单是最结构化的数据源
    ("bom", "bom", 10.0, [
        "bom", "物料清单", "零件表", "part list",
        "bill of material", "零件bom", "b.o.m",
        "child part", "分解零件",
        # 工业常用 BOM 别称
        "零件清单", "材料表", "零部件清单",
        "配套表", "明细表", "结构清单",
    ], 1.0),

    # special_chars — 特殊特性清单，FMEA 核心
    ("special_chars", "spec", 9.0, [
        "特殊特性", "special char", "sc清单",
        "特性清单", "关键特性", "重要特性",
        "产品特性", "过程特性",
        "sc/cc", "关键产品特性", "重要产品特性",
        "特性矩阵", "characteristic matrix",
    ], 1.0),

    # main_sor — 主文件/SOR，产品定义入口
    ("main_sor", "spec", 8.0, [
        "主文件", "main document", "sor主",
        "statement of requirement",
        "产品说明书", "总说明书",
        "overview", "产品概述", "项目概述",
        "技术要求书", "技术协议",
    ], 1.0),

    # eng_design — 工程设计要求 (含 DFMEA 相关)
    ("eng_design", "spec", 7.0, [
        "工程设计", "engineering design",
        "产品要求", "系统方案", "技术方案",
        "设计任务书", "技术条件", "设计规范",
        # DFMEA / PFMEA 核心文档关键词
        "dfmea", "pfmea", "fmea",
        "边界图", "边界分析", "boundary diagram",
        "p图", "参数图", "parameter diagram",
        "功能框图", "功能框图", "functional block",
        "接口矩阵", "interface matrix",
        "特性矩阵", "characteristic matrix",
        "dvp", "设计验证计划", "design verification plan",
        "过程流程图", "process flow",
        # 汽车电子通用设计文档
        "硬件设计", "hardware design", "硬件接口",
        "软件架构", "software architecture",
        "通讯协议", "communication protocol",
        "网络管理", "network management",
        "诊断开发", "diagnostic development",
        "电源管理", "power management",
        "can节点", "can node",
        "lin节点", "lin node",
        "控制器设计", "ecu design",
    ], 1.0),

    # schematic — 原理图/电路图
    ("schematic", "schematic", 6.0, [
        "原理图", "schematic", "电路图",
        "circuit", "pcb", "线路图",
        "接线图", "布线图", "电气图",
        "硬件接口电路", "接口电路",
    ], 1.0),

    # test_spec — 试验/测试规范
    ("test_spec", "test_report", 5.0, [
        "试验", "测试",
        "test spec", "测试规范", "试验规范",
        "dv试验", "dv test",
        "pv试验", "pv test",
        "验证", "测试大纲", "试验大纲",
        "emc测试", "电磁兼容测试",
        "诊断测试", "diagnostic test",
        "信息安全测试", "cyber security test",
        "网络管理测试", "network management test",
        "电连接器试验", "connector test",
    ], 1.0),

    # drawing — 图纸/数模（结构提取核心来源）
    ("drawing", "drawing", 8.5, [
        "图纸", "drawing", "图样", "数模",
        "3d", "cad", "catpart", "stp",
        "三维模型", "装配图", "零件图",
        # 图纸文件常见的零件编号格式 — 使用制造业通用描述词
        "壳体", "盖板", "外罩", "支架", "连接器",
        "缓冲", "密封", "导热", "隔板",
        "pack", "housing", "case", "cover",
        # CAD导出的图纸特征词
        "技术要求", "设计", "校对", "标准化",
        "图号", "图幅", "物料编码", "材料",
        "投影标记", "比例", "数量",
    ], 1.0),

    # simulation_report — 仿真报告（含官方系统分解）
    ("simulation_report", "spec", 7.5, [
        "仿真", "simulation",
        "有限元", "fea", "fe model",
        "结构仿真", "热仿真", "振动仿真",
        "机械冲击", "碰撞仿真", "跌落仿真",
        "过流仿真", "cae",
        "网格", "mesh", "边界条件",
        "模态", "modal", "应力",
        "定频振动", "冲击工况",
    ], 1.0),

    # icd — 接口控制文档（电气参数/BMS架构）
    ("icd", "schematic", 6.5, [
        "icd", "接口控制", "interface control",
        "电气参数调查", "电气特性",
        "接插件", "针脚", "pin",
        "系统框图", "硬件原理",
        "用电器", "负载",
    ], 1.0),

    # legal — 法规/标准
    ("legal", "spec", 2.0, [
        "法规", "legal", "标准", "standard",
        "gb ", "iso ",
        "规范要求", "行业标准", "国家标准",
        "法规要求", "认证要求", "强检",
        "reach法规", "pops法规",
        "禁用物质", "限用物质", "危险物质",
    ], 1.0),

    # checklist — 交付物/文档清单
    ("checklist", "spec", 3.0, [
        "交付物", "deliverable", "文档清单",
        "文件清单", "目录", "checklist",
        "文档列表", "交付清单",
        "文件目录", "技术文档清单",
    ], 1.0),

    # appendix — 附件（低优先级，最终由后处理推断具体角色）
    ("appendix", "spec", 3.0, [
        "附件", "appendix", "annex",
        "附录",
    ], 1.0),

    # label_spec — 标签/标识规范
    ("label_spec", "spec", 4.0, [
        "标签", "label",
        "标识", "产品标识",
        "追溯标签", "验证标签",
        "标签规范", "标签设计",
    ], 1.0),

    # manufacturing — 制造要求
    ("manufacturing", "spec", 4.0, [
        "制造要求", "manufacturing",
        "电子产品制造", "制造工艺",
        "生产要求", "加工要求",
    ], 1.0),
]

# ═══════════════════════════════════════════════════════════════
# 负关键词规则 — 命中了这些关键词则不应该分到某角色
# ═══════════════════════════════════════════════════════════════
NEGATIVE_RULES: Dict[str, List[str]] = {
    # test_spec 负关键词：这些文档虽然含"试验"但实际不是测试规范
    "test_spec": [
        "试验报告", "试验记录", "试验结果",
        "test report", "test record", "test result",
        "分析报告",
        "测试报告", "测试记录", "测试结果",
    ],
    # eng_design 负关键词：这些不是工程设计文档
    "eng_design": [
        "会议纪要", "会议记录", "meeting minutes",
        "工作日志", "周报", "月报",
        "通讯录", "联系方式",
        "发票", "报价单", "合同",
    ],
    # main_sor 负关键词
    "main_sor": [
        "子文件", "分解", "拆解",
    ],
    # schematic 负关键词
    "schematic": [
        "示意图", "草图", "草稿",
    ],
    # legal 负关键词
    "legal": [
        "非标", "非标准",
    ],
    # bom 负关键词
    "bom": [
        "bom成本", "bom价格", "bom报价",
        "bom price", "bom cost",
    ],
    # drawing 负关键词：含"图样"关键词但实际是行业标准/规范，不是产品图纸
    "drawing": [
        "一般规定", "设计规范", "design guideline",
        "检验标准", "外观检验",
        "标签", "label",
        "禁用", "限用", "prohibited",
        "法规", "regulation",
        "试验方法", "试验规范",
    ],
}

# ═══════════════════════════════════════════════════════════════
# 结构化文件扩展名
# ═══════════════════════════════════════════════════════════════
STRUCTURED_EXTS = {".xlsx", ".xls", ".csv"}

# ── 扩展名到角色和类别映射 ──
EXT_TO_ROLE: Dict[str, Tuple[str, str, float]] = {
    ".xlsx": ("bom", "bom", 10.0),
    ".xls":  ("bom", "bom", 10.0),
    ".csv":  ("bom", "bom", 9.0),
    ".dwg":  ("drawing", "drawing", 8.5),
    ".dxf":  ("drawing", "drawing", 8.5),
    ".stp":  ("drawing", "drawing", 8.0),
    ".step": ("drawing", "drawing", 8.0),
    ".igs":  ("drawing", "drawing", 7.0),
    ".prt":  ("drawing", "drawing", 7.0),
    ".catpart": ("drawing", "drawing", 7.0),
    ".fem":  ("simulation_report", "spec", 7.0),
    ".inp":  ("simulation_report", "spec", 6.0),
    ".k":    ("simulation_report", "spec", 6.0),
}

# ═══════════════════════════════════════════════════════════════
# L2 回退：内容特征规则（用于"other"文档的最后兜底）
# ═══════════════════════════════════════════════════════════════
L2_CONTENT_RULES = [
    # (角色名, 类别, 特征检测函数, 基础优先级)
]

# ── 图纸内容特征检测 ──

def _is_drawing_like(text: str) -> bool:
    """检测文本是否像工程图纸：含技术要求/材料/图号/视图标注"""
    drawing_kw = ["技术要求", "图号", "投影标记", "图幅", "图样标记",
                  "设计", "校对", "标准化", "审核", "批准",
                  "表面处理", "镀层", "成型工艺", "包装防护"]
    score = sum(1 for k in drawing_kw if k in text)
    # 图纸特有：材料标注模式
    material_patterns = re.findall(r'(PPE|PA\d|AL\d+|T2Y|PU\d+)', text)
    score += len(material_patterns) * 2
    return score >= 4

def _is_simulation_report_like(text: str) -> bool:
    """检测文本是否像仿真报告"""
    sim_kw = ["有限元模型", "网格", "边界条件", "仿真", "应力",
              "屈服强度", "弹性模量", "泊松比", "评价标准",
              "仿真工况", "分析结论", "仿真结果", "应力云图"]
    score = sum(1 for k in sim_kw if k in text)
    # 仿真报告特有：材料参数表
    param_patterns = re.findall(r'(弹性模量|泊松比|屈服强度|抗拉强度|延伸率)', text)
    score += len(param_patterns) * 3
    return score >= 5

# ── L2 内容特征检测 ──

def _is_bom_like(text: str) -> bool:
    """检测文本是否像 BOM/表格：高密度数字、管道符号、列式对齐"""
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) < 3:
        return False
    pipe_lines = sum(1 for l in lines if "|" in l)
    number_density = sum(1 for c in text if c.isdigit()) / max(len(text), 1)
    # BOM 特征：管道符号占比高 或 数字密度大（>8%）
    return (len(lines) >= 5 and pipe_lines / len(lines) > 0.3) or (number_density > 0.08 and pipe_lines > 3)

def _is_spec_like(text: str) -> bool:
    """检测文本是否像规范/要求文档：关键词密度高"""
    kw = ["要求", "应", "必须", "不得", "符合", "满足", "达到", "不大于", "不小于",
          "shall", "should", "must", "requirement", "specification"]
    score = sum(text.lower().count(k) for k in kw)
    return score > 5

def _is_test_like(text: str) -> bool:
    """检测文本是否像测试文档：测试相关词汇密度高"""
    kw = ["测试", "试验", "检验", "检测", "判定", "合格", "不合格",
          "test", "pass", "fail", "result", "measurement"]
    score = sum(text.lower().count(k) for k in kw)
    return score > 5

def _is_eng_spec_like(text: str) -> bool:
    """检测文本是否像工程设计规范：含大量技术参数/协议/接口描述"""
    kw = ["协议", "接口", "通讯", "通信", "总线", "can", "lin", "spi", "i2c",
          "诊断", "刷新", "刷写", "ecu", "控制器", "传感器", "执行器",
          "protocol", "interface", "bus", "diagnostic", "refresh", "flash",
          "信号", "电平", "电压", "电流", "阻抗", "频率",
          "signal", "voltage", "current", "impedance", "frequency",
          "唤醒", "休眠", "电源", "功耗", "sleep", "wake", "power"]
    score = sum(1 for k in kw if k in text.lower())
    return score > 6

def _is_table_like(text: str) -> bool:
    """检测文本是否含结构化表格数据（即使不像BOM也可能是特殊特性或测试参数表）"""
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) < 5:
        return False
    pipe_lines = sum(1 for l in lines if "|" in l)
    return pipe_lines / len(lines) > 0.25 and pipe_lines >= 5

L2_CLASSIFY_RULES: List[Tuple[str, str, callable, float]] = [
    ("bom", "bom", _is_bom_like, 6.0),
    ("drawing", "drawing", _is_drawing_like, 7.5),
    ("simulation_report", "spec", _is_simulation_report_like, 6.5),
    ("special_chars", "spec", _is_table_like, 5.5),
    ("eng_design", "spec", _is_eng_spec_like, 5.0),
    ("eng_design", "spec", _is_spec_like, 5.0),
    ("test_spec", "test_report", _is_test_like, 4.0),
]

# ═══════════════════════════════════════════════════════════════
# 多维度打分分类函数
# ═══════════════════════════════════════════════════════════════

# 各来源命中的权重
SOURCE_WEIGHTS = {
    "filename": 3.0,
    "title": 2.0,
    "summary": 1.0,
    "source_file": 3.0,
    "content": 4.0,
    "extension": 2.0,
}

def classify_document(
    filename: str = "",
    title: str = "",
    summary: str = "",
    source_file: str = "",
    source_type: str = "",
    size_kb: int = 0,
    content_sample: str = "",
) -> Dict:
    """
    多维度打分分类一个文档。

    Args:
        filename: MD 文件名
        title: 文档标题
        summary: 文档摘要
        source_file: 原始文件名（如 SOR-XXX-主文件.pdf）
        source_type: 原始扩展名（如 .pdf, .xlsx）
        size_kb: 文件大小（KB）
        content_sample: 文档内容前 N 字符（可选，用于 L2 回退）

    Returns:
        {"role": str, "category": str, "priority": float,
         "is_structured": bool, "confidence": str  # "high" | "medium" | "low" | "l2"
         }
    """
    # ── 准备各源文本 ──
    title_lower = title.lower().strip()
    summary_lower = summary.lower().strip()
    filename_lower = filename.lower().strip()
    source_lower = source_file.lower().strip()
    ext_lower = source_type.lower().strip()

    # ── 按角色累加得分 ──
    # score_map: role -> {"category", "base_priority", "score", "hits"}
    score_map: Dict[str, Dict] = {}

    for role, category, base_priority, keywords, _kw_weight in ROLE_RULES:
        score_map[role] = {
            "category": category,
            "base_priority": base_priority,
            "score": 0.0,
            "hits": 0,
        }

    # 各来源匹配
    sources = [
        (filename_lower, "filename"),
        (title_lower, "title"),
        (summary_lower, "summary"),
        (source_lower, "source_file"),
    ]
    if content_sample:
        sources.append((content_sample.lower(), "content"))

    for source_text, source_name in sources:
        weight = SOURCE_WEIGHTS.get(source_name, 1.0)
        if not source_text:
            continue
        for role, category, base_priority, keywords, _kw_weight in ROLE_RULES:
            hit_count = 0
            for kw in keywords:
                if kw in source_text:
                    hit_count += 1
            if hit_count > 0:
                score_map[role]["score"] += hit_count * weight
                score_map[role]["hits"] += hit_count

    # ── 扩展名匹配 ──
    if ext_lower in EXT_TO_ROLE:
        ext_role, ext_cat, ext_pri = EXT_TO_ROLE[ext_lower]
        if ext_role in score_map:
            score_map[ext_role]["score"] += SOURCE_WEIGHTS["extension"]
            score_map[ext_role]["hits"] += 1

    # ── 负关键词减分 ──
    all_text = f"{filename_lower} {title_lower} {summary_lower} {source_lower}"
    if content_sample:
        all_text += f" {content_sample.lower()}"

    for role, neg_keywords in NEGATIVE_RULES.items():
        if role in score_map:
            for nk in neg_keywords:
                if nk in all_text:
                    score_map[role]["score"] -= 5.0  # 负关键词扣分（从-10调为-5，避免误杀）
                    break  # 同一角色扣一次即可

    # ── 选题：得分最高的角色 ──
    best_role = "other"
    best_category = "other"
    best_base_priority = 1.0
    best_score = 0.0

    for role, info in score_map.items():
        if info["score"] > best_score:
            best_score = info["score"]
            best_role = role
            best_category = info["category"]
            best_base_priority = info["base_priority"]
        elif info["score"] == best_score and best_score > 0:
            # 得分相同，选基础优先级更高的
            if info["base_priority"] > best_base_priority:
                best_role = role
                best_category = info["category"]
                best_base_priority = info["base_priority"]

    # ── 置信度判定 ──
    if best_score >= 6.0:
        confidence = "high"
    elif best_score >= 3.0:
        confidence = "medium"
    else:
        confidence = "low"

    # ── 优先级微调 ──
    priority = best_base_priority

    # 1) 结构性通用词加分
    structural_kw = ["结构", "产品", "系统", "组成", "总成",
                     "部件", "组件", "模块", "总装",
                     "控制器", "ecu", "bms", "传感器",
                     "管理",
                     # 机械/电子通用结构描述词
                     "壳体", "盖板", "外罩", "支架", "连接器",
                     "板卡", "面板", "底座", "托架",
                     "pack", "housing", "cover", "plate", "case"]
    structural_hits = sum(1 for kw in structural_kw if kw in title_lower)
    priority += structural_hits * 0.5

    # 2) 结构化格式加分
    is_structured = ext_lower in STRUCTURED_EXTS
    if is_structured:
        priority += 2.0

    # 3) 大文件加分
    if size_kb and size_kb > 100:
        priority += 1.0

    # 4) "总成"/"总装" 加分
    if any(kw in title_lower for kw in ["总成", "总装", "assembly", "pack"]):
        priority += 1.5

    # 5) 附件编号 — 通用附件E倾向工程设计
    if re.search(r'附件\s*[eE]|appendix\s*[eE]', all_text):
        if best_role in ("other", "unknown", "appendix"):
            best_role = "eng_design"
            best_category = "spec"
            best_base_priority = 7.0
        priority += 2.0

    # 6) 含“技术要求”/“技术规范”的文档优先级提升
    if any(kw in title_lower for kw in ["技术要求", "技术规范", "设计规范",
                                         "技术条件", "通用要求"]):
        priority += 1.0

    # 7) 低优先级 other 文档如果有内容采样，给额外机会
    if best_role == "other" and priority < 3.0 and size_kb and size_kb > 20:
        priority += 1.0  # 有内容的 other 文档不应被完全忽略

    # 8) SOR 项目文档加分 — SOR 文件是产品级文档，优先级应最高
    #    这类文档包含实际产品的结构、规格、零件信息
    is_sor_doc = bool(re.search(r'sor[-_]', filename_lower)) or bool(re.search(r'sor[-_]', source_lower))
    if is_sor_doc:
        if re.search(r'主文件|main.?doc', filename_lower):
            priority += 2.0  # SOR 主文件是产品定义入口
        elif re.search(r'附件|appendix|annex', filename_lower):
            priority += 3.0  # SOR 附件含具体产品规格（工程设计要求、特殊特性等）
        elif re.search(r'零件信息|part.?info|更新通知', filename_lower):
            priority += 2.5  # SOR 零件信息含 BOM 数据
        else:
            priority += 1.5  # 其他 SOR 文件也有较高价值

    # 9) 图纸角色大幅提升优先级 — 图纸是结构提取的核心物理来源
    if best_role == "drawing":
        priority += 3.0  # 图纸含完整零件清单和材料信息

    # 10) 仿真报告也提升优先级 — 含官方系统分解表
    if best_role == "simulation_report":
        priority += 2.5

    # 11) 通用行业标准降权 — 标准编号开头的文件（GB/ISO/EN/SAE/行业企业标准等），
    #    不含特定产品的结构信息，不应作为结构树提取的主要来源
    is_generic_standard = bool(re.search(
        r'^(smtc|gb|iso|din|en|iec|sae|cispr|mil|qs|iatf|vda)',
        filename_lower
    ))
    if is_generic_standard and not is_sor_doc:
        # 通用标准优先级降低，确保产品级文档优先
        priority -= 2.0
        # 但保留最低可用优先级（仍可能含特殊特性/法规信息）
        priority = max(priority, 3.0)

    priority = min(priority, 10.0)

    result = {
        "role": best_role,
        "category": best_category,
        "priority": round(priority, 1),
        "is_structured": is_structured,
        "confidence": confidence,
        "raw_score": round(best_score, 1),
    }

    return result


def classify_document_l2(
    role_info: Dict,
    content_sample: str,
) -> Dict:
    """
    L2 回退分类：对 role="other" 或置信度低的文档，
    基于内容特征模式做 best-guess。

    Args:
        role_info: classify_document 的返回结果
        content_sample: 文档内容前 N 字符

    Returns:
        更新后的 role_info（可能修改 role/category/priority/confidence）
    """
    if role_info.get("confidence") in ("high", "medium"):
        return role_info  # 无需 L2

    if not content_sample or len(content_sample) < 100:
        return role_info

    best_role = role_info["role"]
    best_category = role_info["category"]
    best_priority = role_info["priority"]

    for role, category, detector, base_priority in L2_CLASSIFY_RULES:
        try:
            if detector(content_sample):
                if base_priority > best_priority or best_role == "other":
                    best_role = role
                    best_category = category
                    best_priority = base_priority
        except Exception:
            continue

    if best_role != role_info["role"]:
        logger.info(f"[fmea_roles] L2 回退: other → {best_role} (置信度: low)")

    return {
        **role_info,
        "role": best_role,
        "category": best_category,
        "priority": round(max(role_info["priority"], best_priority * 0.9), 1),
        "confidence": "l2" if best_role != "other" else "low",
    }
