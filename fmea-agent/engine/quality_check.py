"""
质量验收评分引擎 — 对应需求规格 §5

六维度加权质量评分：
1. 合规完整性（最高权重 25%）
2. 结构&功能完整性（高权重 20%）
3. 失效逻辑严谨性（高权重 20%）
4. 风险打分规范性（中权重 15%）
5. 优化方案落地性（中权重 10%）
6. 格式标准化（基础权重 10%）
"""
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


class QualityScorer:
    """六维度加权质量评分"""

    WEIGHTS = {
        "compliance": 0.25,        # 合规完整性
        "structure_function": 0.20,  # 结构&功能完整性
        "failure_logic": 0.20,     # 失效逻辑严谨性
        "scoring_norm": 0.15,      # 风险打分规范性
        "optimization_viability": 0.10,  # 优化方案落地性
        "format_standard": 0.10,   # 格式标准化
    }

    def score(self, project_data: Dict[str, Any]) -> Dict[str, Any]:
        """计算全部六维度质量分数

        Returns:
            {"total_score": 85.5, "dimensions": {...}, "verdict": "PASS"}
        """
        dimensions = {}
        for dim, weight in self.WEIGHTS.items():
            scorer_name = f"_score_{dim}"
            if hasattr(self, scorer_name):
                raw = getattr(self, scorer_name)(project_data)
                dimensions[dim] = {
                    "raw_score": raw,
                    "weight": weight,
                    "weighted_score": raw * weight,
                    "max_score": 100,
                }

        total = sum(d["weighted_score"] for d in dimensions.values())

        # 判定
        if total >= 80:
            verdict = "PASS"
            verdict_text = "质量达标，可用于内部评审和客户交付"
        elif total >= 60:
            verdict = "NEEDS_IMPROVEMENT"
            verdict_text = "质量基本合格，建议优化后交付"
        else:
            verdict = "FAIL"
            verdict_text = "质量不达标，需全面修订"

        return {
            "total_score": round(total, 1),
            "dimensions": dimensions,
            "verdict": verdict,
            "verdict_text": verdict_text,
            "failed_dimensions": [
                dim for dim, d in dimensions.items() if d["weighted_score"] < d["weight"] * 60
            ],
        }

    # ════════════════════════════════════════════════════════════
    # 维度 1：合规完整性（25%）
    # ════════════════════════════════════════════════════════════

    def _score_compliance(self, data: dict) -> float:
        """评分：法规覆盖度 + 纯设计分析"""
        score = 100.0

        # 检查法规是否全部匹配
        regulation_list = data.get("regulation_list", [])
        doc1 = data.get("document_1", {})
        mapping = doc1.get("mapping_table", [])
        mapped_ids = {m.get("regulation_id", "") for m in mapping}

        if regulation_list:
            matched = len(set(regulation_list) & mapped_ids)
            coverage = matched / len(regulation_list)
            if coverage < 1.0:
                score -= (1.0 - coverage) * 40  # 每缺失 1 条标准扣 40 分

        # 检查是否混入制程类失效
        manufacturing_keywords = ["装配", "来料", "夹具", "产线"]
        failure_data = data.get("failure_analysis", {})
        failure_items = failure_data.get("items", []) if isinstance(failure_data, dict) else []
        mfg_count = 0
        for item in failure_items:
            cause = str(item.get("failure_cause", ""))
            if any(kw in cause for kw in manufacturing_keywords):
                mfg_count += 1

        if failure_items and mfg_count > 0:
            score -= min(30, mfg_count * 5)  # 每条扣 5 分，最多扣 30

        return max(0, score)

    # ════════════════════════════════════════════════════════════
    # 维度 2：结构&功能完整性（20%）
    # ════════════════════════════════════════════════════════════

    def _score_structure_function(self, data: dict) -> float:
        """评分：结构树完整 + 功能覆盖率"""
        score = 100.0

        # 结构树节点数
        structure = data.get("structure_tree", {})
        nodes = structure.get("nodes", []) if isinstance(structure, dict) else []
        if not nodes:
            score -= 50

        # 功能覆盖率
        func = data.get("function_analysis", {})
        func_items = func.get("items", []) if isinstance(func, dict) else []
        if nodes and func_items:
            non_root = [n for n in nodes if n.get("level") != "总成"]
            if non_root:
                coverage = len(func_items) / len(non_root)
                if coverage < 0.8:
                    score -= (0.8 - coverage) * 50

        return max(0, score)

    # ════════════════════════════════════════════════════════════
    # 维度 3：失效逻辑严谨性（20%）
    # ════════════════════════════════════════════════════════════

    def _score_failure_logic(self, data: dict) -> float:
        """评分：失效三要素完整 + 传导逻辑"""
        score = 100.0

        failure_data = data.get("failure_analysis", {})
        items = failure_data.get("items", []) if isinstance(failure_data, dict) else []

        incomplete = 0
        for item in items:
            if not item.get("failure_mode") or not item.get("failure_cause") or not item.get("failure_effect"):
                incomplete += 1

        if items:
            incomplete_rate = incomplete / len(items)
            if incomplete_rate > 0:
                score -= incomplete_rate * 50

        return max(0, score)

    # ════════════════════════════════════════════════════════════
    # 维度 4：风险打分规范性（15%）
    # ════════════════════════════════════════════════════════════

    def _score_scoring_norm(self, data: dict) -> float:
        """评分：S/O/D 统一 + 预防/探测分类清晰"""
        score = 100.0

        risk_data = data.get("risk_analysis", {})
        items = risk_data.get("items", []) if isinstance(risk_data, dict) else []

        # 检查空打分
        empty_scores = sum(
            1 for item in items
            if not item.get("s") and not item.get("severity")
            or not item.get("o") and not item.get("occurrence")
            or not item.get("d") and not item.get("detection")
        )
        if items and empty_scores > 0:
            score -= min(30, empty_scores * 5)

        # 检查预防/探测措施是否存在混淆
        # (简化实现：检查是否有空措施)
        no_preventive = sum(
            1 for item in items
            if not item.get("preventive_action")
        )
        if items and no_preventive > len(items) * 0.5:
            score -= 20

        return max(0, score)

    # ════════════════════════════════════════════════════════════
    # 维度 5：优化方案落地性（10%）
    # ════════════════════════════════════════════════════════════

    def _score_optimization_viability(self, data: dict) -> float:
        """评分：高AP覆盖 + 方案可落地 + 分类/成本"""
        score = 100.0

        risk_data = data.get("risk_analysis", {})
        risk_items = risk_data.get("items", []) if isinstance(risk_data, dict) else []
        opt_data = data.get("optimization_analysis", {})
        opt_items = opt_data.get("items", []) if isinstance(opt_data, dict) else []

        # 高 AP 项 + 法规违规项是否都有优化（需求 V6-1）
        high_ap = [
            r for r in risk_items
            if r.get("ap") in ("H", "高", "HIGH") or bool(r.get("regulation_violation"))
        ]
        if high_ap:
            opt_ids = {o.get("failure_id", "") for o in opt_items}
            unmatched = sum(1 for r in high_ap if r.get("id", "") not in opt_ids)
            if unmatched > 0:
                score -= min(40, unmatched * 15)

        # 优化方案是否有分类标注
        if opt_items:
            categorized = sum(1 for o in opt_items if o.get("category"))
            if categorized < len(opt_items):
                score -= 15

        return max(0, score)

    # ════════════════════════════════════════════════════════════
    # 维度 6：格式标准化（10%）
    # ════════════════════════════════════════════════════════════

    def _score_format_standard(self, data: dict) -> float:
        """评分：表格字段完整 + 按专业分类"""
        score = 100.0

        # 检查文件1是否存在
        if not data.get("document_1"):
            score -= 20

        # 检查报告是否存在
        report = data.get("report", data.get("final_report", {}))
        if not report:
            score -= 30

        # 检查高风险行是否高亮
        report_rows = report.get("rows", []) if isinstance(report, dict) else []
        if report_rows:
            highlighted = sum(
                1 for r in report_rows
                if r.get("highlighted") or r.get("ap") in ("H", "高")
            )
            if highlighted == 0:
                score -= 10

        return max(0, score)
