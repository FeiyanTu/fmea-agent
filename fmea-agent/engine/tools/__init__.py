"""
FMEA 领域工具函数 — 供 Agent 和 LLM Tool Calling 使用

包含:
- AP 等级计算（基于 AIAG/VDA 标准）
- RPN 计算
- 结构树操作
- 机械零件判断
- 项目初始化工具（文档转换 + 知识库）
- 知识库查询工具
"""
import logging
from typing import Optional
from engine.prompts import build_default_ap_table

logger = logging.getLogger(__name__)


# ── AP 表（内置 AIAG/VDA 默认标准）──

_AP_RULES = None


def get_ap_rules() -> list:
    """获取 AP 规则表"""
    global _AP_RULES
    if _AP_RULES is None:
        _AP_RULES = build_default_ap_table()
    return _AP_RULES


def calculate_rpn(s: int, o: int, d: int) -> int:
    """计算 RPN = S × O × D"""
    return s * o * d


def calculate_ap(s: int, o: int, d: int) -> str:
    """根据 AIAG/VDA 标准计算 AP 等级 (H/M/L)

    参考 AIAG & VDA FMEA Handbook 1st Edition 的 Action Priority 表
    """
    rules = get_ap_rules()
    for rule in rules:
        if rule["s_min"] <= s <= rule["s_max"] and \
           rule["o_min"] <= o <= rule["o_max"] and \
           rule["d_min"] <= d <= rule["d_max"]:
            return rule["ap"]

    # 默认：按 RPN 判断
    rpn = s * o * d
    if rpn >= 100:
        return "H"
    elif rpn >= 40:
        return "M"
    return "L"


def calculate_severity(failure_description: str) -> int:
    """从失效描述中提取严重度 S

    如果失效描述中包含类似于"(10)"或"(9)"的标注，直接使用
    否则根据关键词推断
    """
    import re

    # 直接匹配标注
    match = re.search(r"\((\d{1,2})\)", failure_description)
    if match:
        s = int(match.group(1))
        if 1 <= s <= 10:
            return s

    # 关键词推断
    desc_lower = failure_description.lower()
    if any(k in desc_lower for k in ["安全", "法规", "安全风险", "人身伤害"]):
        return 10
    if any(k in desc_lower for k in ["功能丧失", "无法使用", "不能工作"]):
        return 8
    if any(k in desc_lower for k in ["功能降级", "性能下降", "主要功能"]):
        return 6
    if any(k in desc_lower for k in ["客户抱怨", "无法交付", "外观"]):
        return 4
    return 4  # 默认


# ── 结构树工具 ──

def generate_node_ids(count: int, prefix: str = "node") -> list:
    """生成连续节点ID"""
    return [f"{prefix}-{i+1:04d}" for i in range(count)]


def is_mechanical_part(node_name: str, level: str, specification: str = "") -> bool:
    """判断是否需要分析的节点

    仅跳过纯标准件/紧固件/垫片等不需要功能分析的项目。
    所有层级（系统、子系统、组件、零件）均参与功能分析。
    """
    skip_keywords = [
        "螺丝", "螺母", "螺栓", "螺钉", "垫圈", "垫片",
        "卡簧", "挡圈", "销", "键", "铆钉",
        "screw", "nut", "bolt", "washer", "gasket",
        "spring washer", "circlip", "pin", "key", "rivet",
        "扎带", "线束标签", "标签",
    ]
    name_lower = node_name.lower()

    for kw in skip_keywords:
        if kw in name_lower:
            return False

    return True


def filter_analysis_nodes(nodes: list) -> list:
    """过滤出需要分析的节点（跳过纯标准件）

    Args:
        nodes: 结构树节点列表 (dict格式或StructureTreeNode)

    Returns:
        需要分析的节点列表
    """
    result = []
    for node in nodes:
        name = node.get("name", "") if isinstance(node, dict) else node.name
        level = node.get("level", "") if isinstance(node, dict) else node.level
        spec = node.get("specification", "") if isinstance(node, dict) else node.specification
        if is_mechanical_part(name, level, spec):
            result.append(node)

    skipped = [n for n in nodes if n not in result]
    if skipped:
        names = [n.get("name", "") if isinstance(n, dict) else n.name for n in skipped]
        logger.info(f"[filter_analysis_nodes] 跳过 {len(skipped)} 个标准件: {names[:10]}")

    return result


def build_node_context(node: dict, all_nodes: list) -> str:
    """为单个节点构建 LLM 分析上下文

    包含：节点的父节点信息、子节点信息、同级节点概览
    """
    node_id = node.get("id", "")
    parent_id = node.get("parentId", "")

    context_parts = [f"当前节点: {node.get('name', '')} (ID: {node_id}, 层级: {node.get('level', '')})"]

    if parent_id:
        parent = next((n for n in all_nodes if n.get("id") == parent_id), None)
        if parent:
            context_parts.append(f"父节点: {parent.get('name', '')} (ID: {parent_id})")

    children = [n for n in all_nodes if n.get("parentId") == node_id]
    if children:
        child_names = [f"{c.get('name', '')} ({c.get('level', '')})" for c in children[:5]]
        context_parts.append(f"子节点 ({len(children)}个): {', '.join(child_names)}")

    material = node.get("material", "")
    spec = node.get("specification", "")
    if material:
        context_parts.append(f"材料: {material}")
    if spec:
        context_parts.append(f"规格: {spec}")

    return "\n".join(context_parts)


# ── 项目初始化 / 知识库工具 (从 project_init_tools 导入) ──

from .project_init_tools import (
    init_project_tool,
    get_init_progress_tool,
    extract_structure_tree_tool,
    get_kb_status_tool,
    list_kb_pages_tool,
)

__all__ = [
    "calculate_rpn",
    "calculate_ap",
    "calculate_severity",
    "get_ap_rules",
    "generate_node_ids",
    "is_mechanical_part",
    "filter_analysis_nodes",
    "build_node_context",
    "init_project_tool",
    "get_init_progress_tool",
    "extract_structure_tree_tool",
    "get_kb_status_tool",
    "list_kb_pages_tool",
    # 多知识库联合搜索
    "merge_multi_kb_results",
    "get_agent_weknora_kb_id",
]


# ── 多知识库结果合并（项目 KB + 专属 KB）──

def merge_multi_kb_results(
    project_results: list,
    own_results: list,
    top_n: int = 20,
) -> list:
    """合并项目 KB 和 Agent 专属 KB 的检索结果

    冲突规则：相同内容 → 以项目 KB 为准（项目 KB 优先）
    项目 KB 结果排在前面，专属 KB 结果仅补充项目 KB 未覆盖的内容。

    Args:
        project_results: 项目知识库的检索结果
        own_results: Agent 专属知识库的检索结果
        top_n: 最终返回的结果数量

    Returns:
        合并后的结果列表（项目 KB 优先）
    """
    seen_contents = set()
    merged = []

    for r in sorted(project_results, key=lambda x: x.get("score", 0), reverse=True):
        preview = str(r.get("content", ""))[:200]
        if preview not in seen_contents:
            seen_contents.add(preview)
            r["kb_priority"] = "project"
            merged.append(r)

    for r in sorted(own_results, key=lambda x: x.get("score", 0), reverse=True):
        preview = str(r.get("content", ""))[:200]
        if preview not in seen_contents:
            seen_contents.add(preview)
            r["kb_priority"] = "own"
            merged.append(r)

    return merged[:top_n]


def get_agent_weknora_kb_id(agent_name: str) -> str:
    """从 agents 表查询指定 Agent 的专属知识库 ID

    Args:
        agent_name: Agent 名称，如 "法规专家"、"BMS 需求专家"

    Returns:
        weknora_kb_id，如果未配置则返回空字符串
    """
    import os
    import psycopg2

    try:
        conn = psycopg2.connect(
            host=os.getenv("URL_AGENT_DB_HOST", "10.180.17.11"),
            port=int(os.getenv("URL_AGENT_DB_PORT", "5433")),
            dbname=os.getenv("URL_AGENT_DB_NAME", "fmea"),
            user=os.getenv("URL_AGENT_DB_USER", "postgres"),
            password=os.getenv("URL_AGENT_DB_PASSWORD", "postgres123!@#"),
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT weknora_kb_id FROM agents WHERE name = %s OR name_en = %s LIMIT 1",
                (agent_name, agent_name),
            )
            row = cur.fetchone()
            return row[0] if row else ""
        conn.close()
    except Exception as e:
        logger.warning(f"[get_agent_weknora_kb_id] query failed agent={agent_name}: {e}")
        return ""
