"""Step 4 风险评估 Agent — v5 需求对齐版

v5 变更:
- 接入阶段1打分基准交叉验证 (HC-5 基线锁定)
- LLM评分后与SodCriteria比对, 偏差>2级告警
"""
import json
import logging
from typing import List, Optional

from models.failure import FailureAnalysis
from models.risk import RiskAssessment, RiskAnalysis
from engine.llm_client import get_llm_client
from engine.concurrent import get_concurrent_engine
from engine.tools import calculate_rpn, calculate_ap

logger = logging.getLogger(__name__)


class RiskAgent:
    """Step 4: 风险评估 — SOD 评分 + RPN + AP 等级"""

    def __init__(self):
        self.llm = get_llm_client()
        self.engine = get_concurrent_engine()

    async def generate_risk_analysis(
        self,
        failure_analysis: FailureAnalysis,
        sod_criteria: Optional[dict] = None,
    ) -> RiskAnalysis:
        """基于失效分析生成风险评估

        v5: 可选 sod_criteria 参数，接入阶段1打分基准进行交叉验证
        """
        from engine.prompts import RISK_ANALYSIS_SYSTEM

        if not failure_analysis.items:
            return RiskAnalysis(items=[])

        # 批量处理失效链（每批3-5条，合并为一次LLM调用）
        batch_size = 4
        batches = [
            failure_analysis.items[i:i+batch_size]
            for i in range(0, len(failure_analysis.items), batch_size)
        ]

        logger.info(f"Starting risk analysis: {len(failure_analysis.items)} items in {len(batches)} batches")

        tools = [{
            "type": "function",
            "function": {
                "name": "assess_risk",
                "description": "评估失效链的风险(SOD评分)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "riskItems": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "failureId": {"type": "string"},
                                    "severity": {"type": "integer"},
                                    "preventionControl": {"type": "string"},
                                    "occurrence": {"type": "integer"},
                                    "occurrenceReason": {"type": "string"},
                                    "detectionControl": {"type": "string"},
                                    "detection": {"type": "integer"},
                                    "detectionReason": {"type": "string"},
                                },
                                "required": ["failureId", "severity", "occurrence", "detection"],
                            },
                        }
                    },
                    "required": ["riskItems"],
                },
            }
        }]

        async def analyze_batch(batch: list) -> List[RiskAssessment]:
            # 构建批量输入
            items_text = []
            for fm in batch:
                items_text.append(
                    f"失效链 #{fm.id}\n"
                    f"- 组件: {fm.node_name}\n"
                    f"- 失效影响(FE): {fm.failure_effect}\n"
                    f"- 失效模式(FM): {fm.failure_mode}\n"
                    f"- 失效原因(FC): {fm.failure_cause}\n"
                    f"- 严重度(S)初评: {fm.severity}\n"
                )
            user_msg = "请对以下失效链进行风险分析(SOD评分)：\n\n" + "\n---\n".join(items_text)

            result = await self.llm.safe_call(
                system_prompt=RISK_ANALYSIS_SYSTEM,
                user_message=user_msg,
                tools=tools,
                probe_key="riskItems",
            )

            data = self.llm.extract_json(result)
            risk_items = data.get("riskItems", [])

            assessments = []
            for item in risk_items:
                s = item.get("severity", 4)
                o = item.get("occurrence", 5)
                d = item.get("detection", 5)
                rpn = calculate_rpn(s, o, d)
                ap = calculate_ap(s, o, d)

                # ── v5: 阶段1打分基准交叉验证 ──
                sod_warnings = []
                if sod_criteria:
                    sod_warnings = self._validate_sod_against_criteria(
                        s, o, d, item, sod_criteria
                    )

                # 查找对应的 failureMode
                fm = next((f for f in batch if f.id == item.get("failureId")), None)

                assessments.append(RiskAssessment(
                    failure_id=item.get("failureId", ""),
                    failure_mode=fm.failure_mode if fm else "",
                    severity=s,
                    prevention_control=item.get("preventionControl", ""),
                    occurrence=o,
                    detection_control=item.get("detectionControl", ""),
                    detection=d,
                    rpn=rpn,
                    ap=ap,
                    # v4.1 链式方法论：透传 (focus, child) 关联
                    node_id=fm.node_id if fm else "",
                    node_name=fm.node_name if fm else "",
                    child_node_id=fm.child_node_id if fm else "",
                    function_id=fm.function_id if fm else "",
                ))

            return assessments

        # 8 并发处理批次
        results = await self.engine.process_batch(batches, analyze_batch)

        all_items = []
        for batch_results in results:
            all_items.extend(batch_results)

        analysis = RiskAnalysis(items=all_items)
        logger.info(f"Risk analysis complete: {len(all_items)} items")
        return analysis

    def format_for_display(self, analysis: RiskAnalysis) -> str:
        """格式化风险评估用于前端展示"""
        if not analysis.items:
            return "（无风险评估数据）"

        lines = [
            f"🎯 **风险评估** (共 {len(analysis.items)} 条)",
            "",
            "| # | S | O | D | RPN | AP | 预防措施(PC) | 探测措施(DC) |",
            "|---|---|---|---|---|---|---|---|",
        ]

        for i, ra in enumerate(analysis.items[:20], 1):
            ap_icon = {"H": "🔴", "M": "🟡", "L": "🟢"}.get(ra.ap, "⚪")
            lines.append(
                f"| {i} | {ra.severity} | {ra.occurrence} | {ra.detection} | "
                f"**{ra.rpn}** | {ap_icon}{ra.ap} | "
                f"{ra.prevention_control[:25]} | {ra.detection_control[:25]} |"
            )

        if len(analysis.items) > 20:
            lines.append(f"| ... | ...共{len(analysis.items)}条 | | | | | |")

        # 高风险项
        high_risk = analysis.get_high_risk(80)
        if high_risk:
            lines.append("")
            lines.append(f"🚨 **高风险项 (RPN≥80 或 AP=H)**: {len(high_risk)} 条")
            for ra in high_risk[:5]:
                lines.append(f"- [RPN={ra.rpn}|AP={ra.ap}] {ra.failure_mode[:50]}")

        return "\n".join(lines)

    # ════════════════════════════════════════════════════════════
    # v5: 阶段1打分基准交叉验证
    # ════════════════════════════════════════════════════════════

    def _validate_sod_against_criteria(
        self, s: int, o: int, d: int,
        item: dict, sod_criteria: dict,
    ) -> list:
        """将LLM评分与阶段1固化的SOD基准交叉验证

        偏差 > 2 级 → 告警日志
        返回: warnings list
        """
        warnings = []
        try:
            s_criteria = sod_criteria.get("s", {})
            o_criteria = sod_criteria.get("o", {})
            d_criteria = sod_criteria.get("d", {})

            failure_effect = item.get("failure_effect", "")
            failure_cause = item.get("failure_cause", "")
            prevention_ctrl = item.get("preventionControl", "")
            detection_ctrl = item.get("detectionControl", "")

            # 严重度交叉验证: 按失效影响关键词查表
            expected_s = self._lookup_sod_value(s_criteria, failure_effect, "severity")
            if expected_s and abs(s - expected_s) > 2:
                warnings.append(f"S偏差: LLM={s}, 基准={expected_s} (FE: {failure_effect[:30]})")

            # 发生度交叉验证: 按失效原因+预防措施查表
            combined_o = f"{failure_cause} {prevention_ctrl}"
            expected_o = self._lookup_sod_value(o_criteria, combined_o, "occurrence")
            if expected_o and abs(o - expected_o) > 2:
                warnings.append(f"O偏差: LLM={o}, 基准={expected_o} (FC: {failure_cause[:30]})")

            # 探测度交叉验证: 按失效模式+探测措施查表
            combined_d = f"{item.get('failure_mode', '')} {detection_ctrl}"
            expected_d = self._lookup_sod_value(d_criteria, combined_d, "detection")
            if expected_d and abs(d - expected_d) > 2:
                warnings.append(f"D偏差: LLM={d}, 基准={expected_d} (FM: {item.get('failure_mode', '')[:30]})")

            if warnings:
                logger.warning(
                    f"[v5] SOD交叉验证告警 (failureId={item.get('failureId', '?')}): "
                    + "; ".join(warnings)
                )
        except Exception as e:
            logger.debug(f"[v5] SOD交叉验证异常: {e}")

        return warnings

    @staticmethod
    def _lookup_sod_value(
        criteria_table: dict, context: str, metric: str,
    ) -> Optional[int]:
        """从打分基准表中查找匹配的S/O/D值

        策略: 关键词匹配 → 取对应的分值范围下限
        """
        if not criteria_table or not context:
            return None

        context_lower = context.lower()
        best_match = None
        best_score = 0

        for key, rules in criteria_table.items():
            if isinstance(rules, dict):
                for rule_key, rule_val in rules.items():
                    rule_str = str(rule_key).lower()
                    # 简单关键词重叠计数
                    overlap = sum(1 for w in rule_str.split() if w in context_lower)
                    if overlap > best_score:
                        best_score = overlap
                        if isinstance(rule_val, dict):
                            best_match = rule_val.get(metric, rule_val.get("value"))
                        elif isinstance(rule_val, (int, float)):
                            best_match = int(rule_val)
            elif isinstance(key, str) and key.lower() in context_lower:
                return rules if isinstance(rules, int) else None

        return best_match if isinstance(best_match, int) else None
