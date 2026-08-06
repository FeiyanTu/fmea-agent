"""Step 5 优化分析 Agent"""
import json
import logging
from typing import List

from models.risk import RiskAnalysis, RiskAssessment
from models.optimization import OptimizationAction, OptimizationAnalysis
from engine.llm_client import get_llm_client

logger = logging.getLogger(__name__)


class OptimizationAgent:
    """Step 5: 优化分析 — 为高风险项生成改进措施"""

    def __init__(self):
        self.llm = get_llm_client()

    async def generate_optimization(self, risk_analysis: RiskAnalysis) -> OptimizationAnalysis:
        """§4.6.2: 为 AP=H/M 项生成优化建议"""
        from engine.prompts import OPTIMIZATION_ANALYSIS_SYSTEM

        # 筛选需要优化的项目: 中高 AP(H/M) + 违反强制法规项（需求 §4.6.2 触发条件）
        need_optimization = [
            r for r in risk_analysis.items
            if r.ap in ("H", "M") or r.regulation_violation
        ]

        if not need_optimization:
            logger.info("No items require optimization")
            return OptimizationAnalysis(items=[])

        logger.info(f"Generating optimization for {len(need_optimization)} items")

        tools = [{
            "type": "function",
            "function": {
                "name": "suggest_optimization",
                "description": "为高风险失效提出优化措施建议",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "optimizationItems": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "failureId": {"type": "string"},
                                    "preventiveAction": {"type": "string"},
                                    "detectiveAction": {"type": "string"},
                                    "responsiblePerson": {"type": "string"},
                                    "targetDate": {"type": "string"},
                                    "category": {"type": "string", "enum": ["short_term", "next_gen"]},
                                    "cost_impact": {"type": "string", "enum": ["high", "medium", "low"]},
                                    "maintenance_cost": {"type": "string", "enum": ["high", "medium", "low"]},
                                    "design_change_type": {"type": "string", "enum": ["hardware_revision", "software_logic", "structure_material", "cell_upgrade", "sor_correction"]},
                                    "remark": {"type": "string"},
                                },
                                "required": ["failureId", "preventiveAction", "detectiveAction"],
                            },
                        }
                    },
                    "required": ["optimizationItems"],
                },
            }
        }]

        # 构建输入
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")

        items_text = []
        for ra in need_optimization:
            items_text.append(
                f"失效链 #{ra.failure_id}\n"
                f"- 失效模式: {ra.failure_mode}\n"
                f"- 严重度 S={ra.severity}, 频度 O={ra.occurrence}, 探测度 D={ra.detection}\n"
                f"- RPN={ra.rpn}, AP={ra.ap}\n"
                f"- 当前预防措施: {ra.prevention_control}\n"
                f"- 当前探测措施: {ra.detection_control}\n"
            )

        user_msg = (
            f"今天是 {today}。请为以下高风险失效项提出优化措施建议；"
            f"其中 targetDate 必须结合今天日期与各项严重程度（S/AP）推理合理的目标完成日期。\n\n"
            + "\n---\n".join(items_text)
        )

        result = await self.llm.safe_call(
            system_prompt=OPTIMIZATION_ANALYSIS_SYSTEM,
            user_message=user_msg,
            tools=tools,
            probe_key="optimizationItems",
        )

        data = self.llm.extract_json(result)
        opt_items = data.get("optimizationItems", [])

        actions = []
        for i, item in enumerate(opt_items):
            # 找到对应的 RiskAssessment 透传 (focus, child) 关联
            ra = next((r for r in need_optimization if r.failure_id == item.get("failureId")), None)
            actions.append(OptimizationAction(
                id=f"opt-{i+1:04d}",
                failure_id=item.get("failureId", ""),
                preventive_action=item.get("preventiveAction", ""),
                detective_action=item.get("detectiveAction", ""),
                # 责任人 = 该功能所在工程师（v8 assignee 透传），缺失时回退 LLM 建议/空
                responsible_person=ra.assignee_name if ra and ra.assignee_name else item.get("responsiblePerson", ""),
                target_date=item.get("targetDate", ""),
                status="待实施",
                evidence_action=item.get("evidenceAction", ""),
                remark=item.get("remark", ""),
                # v4.1 链式方法论：透传
                node_id=ra.node_id if ra else "",
                node_name=ra.node_name if ra else "",
                child_node_id=ra.child_node_id if ra else "",
                function_id=ra.function_id if ra else "",
                # v4.2 优化分类 & 成本标注
                category=item.get("category", "short_term"),
                cost_impact=item.get("cost_impact", "medium"),
                maintenance_cost=item.get("maintenance_cost", "medium"),
                design_change_type=item.get("design_change_type", ""),
            ))

        analysis = OptimizationAnalysis(items=actions)
        logger.info(f"Optimization analysis complete: {len(actions)} actions")
        return analysis

    async def recalculate_improved_sod(
        self, actions: List[OptimizationAction], risk_items: list
    ) -> List[OptimizationAction]:
        """在用户确认优化措施后，重新评估改进后的 S/O/D/AP"""
        for action in actions:
            ra = next((r for r in risk_items if r.failure_id == action.failure_id), None)
            if not ra:
                continue

            # 简单规则：实施了预防措施 → O 降低 2-3
            action.improved_s = ra.severity  # 严重度一般不变（除非设计变更）
            action.improved_o = max(1, ra.occurrence - 2) if action.preventive_action else ra.occurrence
            action.improved_d = max(1, ra.detection - 1) if action.detective_action else ra.detection
            action.improved_ap = self._recalculate_ap(action.improved_s, action.improved_o, action.improved_d)

        return actions

    @staticmethod
    def _recalculate_ap(s: int, o: int, d: int) -> str:
        from engine.tools import calculate_ap
        return calculate_ap(s, o, d)

    def format_for_display(self, analysis: OptimizationAnalysis) -> str:
        """格式化优化分析用于前端展示"""
        if not analysis.items:
            return "（无需优化 - 所有项目风险可接受）"

        lines = [
            f"🔄 **优化措施** (共 {len(analysis.items)} 条)",
            "",
            "| # | 预防措施 | 探测措施 | 责任人 | 目标日期 |",
            "|---|---|---|---|---|",
        ]

        for i, action in enumerate(analysis.items[:15], 1):
            lines.append(
                f"| {i} | {action.preventive_action[:30]} | "
                f"{action.detective_action[:30]} | "
                f"{action.responsible_person or '待定'} | "
                f"{action.target_date or '待定'} |"
            )

        if len(analysis.items) > 15:
            lines.append(f"| ... | ...共{len(analysis.items)}条 | | | |")

        return "\n".join(lines)
