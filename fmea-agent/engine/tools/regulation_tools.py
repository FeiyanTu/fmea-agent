"""
法规工具函数 — 法规条款到产品功能/风险的映射

对应需求规格 §4.1.1 法规专家专项工作指令
"""
import logging
from typing import Any, Dict, List

from knowledge.regulation_library import RegulationClause

logger = logging.getLogger(__name__)


def map_regulation_to_function(
    regulation: RegulationClause,
    function_list: List[str],
) -> Dict[str, Any]:
    """将法规条款映射到产品功能列表

    Args:
        regulation: 法规条款
        function_list: 产品功能描述列表

    Returns:
        {"matched": [...], "unmatched": [...]}
    """
    matched = []
    pf_lower = regulation.product_function.lower()
    risk_lower = regulation.potential_risk.lower()

    for func in function_list:
        func_lower = func.lower()
        # 简单关键词匹配
        if any(kw in func_lower for kw in pf_lower.split()) or \
           any(kw in func_lower for kw in risk_lower.split()):
            matched.append(func)

    return {
        "regulation_id": regulation.regulation_id,
        "clause_id": regulation.clause_id,
        "matched_functions": matched,
        "match_count": len(matched),
        "has_match": len(matched) > 0,
    }


def identify_mandatory_risks(regulation: RegulationClause) -> Dict[str, Any]:
    """识别强制条款对应的风险项"""
    return {
        "regulation_id": regulation.regulation_id,
        "clause_id": regulation.clause_id,
        "risk_description": regulation.potential_risk,
        "responsible_module": regulation.responsible_module,
        "severity_hint": "安全相关法规条款 → 建议 S≥9",
        "mandatory": True,
    }


def check_regulation_coverage(
    regulations: List[RegulationClause],
    function_count: int,
    failure_count: int,
) -> Dict[str, Any]:
    """检查法规覆盖完整度"""
    total = len(regulations)
    mapped = sum(1 for r in regulations if r.product_function)
    unmatched = [
        {"regulation_id": r.regulation_id, "clause_id": r.clause_id}
        for r in regulations if not r.product_function
    ]

    return {
        "total_regulations": total,
        "mapped_to_functions": mapped,
        "coverage_rate": f"{mapped/total*100:.1f}%" if total > 0 else "N/A",
        "unmatched_clauses": unmatched,
        "total_functions": function_count,
        "total_failures": failure_count,
        "is_fully_covered": len(unmatched) == 0,
    }
