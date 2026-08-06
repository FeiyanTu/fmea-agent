"""Step 0 规划准备 Agent

@deprecated since v5 (2026-07-22)
本模块未被任何实际工作流调用，§4.1 策划准备的实际实现位于:
  - workflow/fmea_pipeline.py → FmeaPipeline.step0_planning()
  - agents/system_integration.py → SystemIntegrationExpert._step1_prepare_and_baseline()
  - agents/regulation.py → RegulationAgent.analyze()
  - orchestrator/gate.py → Gate.check_stage()
"""
import json
import logging
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class PlanningAgent:
    """Step 0: 规划准备 — 项目创建、SOR文档解析、知识注入"""

    def __init__(self, project_path: str = "data/projects"):
        self.project_path = Path(project_path)

    async def extract_from_sor(self, sor_content: str, llm_client) -> dict:
        """从SOR文档中提取项目规划信息"""
        from engine.prompts import PLANNING_SYSTEM

        user_msg = f"请分析以下SOR文档，提取FMEA项目规划所需的关键信息：\n\n{sor_content[:8000]}"

        result = await llm_client.safe_call(
            system_prompt=PLANNING_SYSTEM,
            user_message=user_msg,
            fallback_to_complete=True,
        )

        try:
            data = llm_client.extract_json(result)
        except Exception:
            data = {"raw": result}

        return data

    async def create_planning_summary(self, project: dict, sor_data: dict) -> dict:
        """生成规划摘要"""
        name = project.get("name", "")
        return {
            "project_name": name,
            # 项目名称即为 FMEA 分析的核心对象，结构树以此作为根节点
            "core_object": project.get("core_object", name),
            "fmea_type": project.get("fmea_type", "DFMEA"),
            "part_number": project.get("part_number", ""),
            "product_type": sor_data.get("product_type", ""),
            "subsystems": sor_data.get("subsystems", []),
            "key_specs": sor_data.get("key_specifications", []),
            "applicable_standards": sor_data.get("applicable_standards", []),
            "boundary_conditions": sor_data.get("boundary_conditions", {}),
            "team_roles": sor_data.get("suggested_team_roles", []),
        }

    def format_summary_for_display(self, summary: dict) -> str:
        """格式化规划摘要用于用户界面展示"""
        lines = ["📋 **规划摘要**", ""]

        # 核心对象提示
        core_object = summary.get('core_object', summary.get('project_name', ''))
        if core_object:
            lines.append(f"> **FMEA 分析对象**: {core_object}")
            lines.append(f"> 结构树将以 **{core_object}** 作为根节点，逐层展开其组成结构。")
            lines.append("")

        lines.append(f"| 项目 | 内容 |")
        lines.append(f"|---|---|")
        lines.append(f"| 产品类型 | {summary.get('product_type', '待补充')} |")
        lines.append(f"| FMEA 类型 | {summary['fmea_type']} |")
        lines.append(f"| 零件号 | {summary.get('part_number', '待补充')} |")

        subsystems = summary.get("subsystems", [])
        if subsystems:
            lines.append(f"| 涉及子系统 | {', '.join(subsystems[:5])} |")

        key_specs = summary.get("key_specs", [])
        if key_specs:
            lines.append("")
            lines.append("**关键指标**：")
            for spec in key_specs[:8]:
                if isinstance(spec, dict):
                    lines.append(f"- {spec.get('name', '')}: {spec.get('value', '')}")
                else:
                    lines.append(f"- {spec}")

        standards = summary.get("applicable_standards", [])
        if standards:
            lines.append("")
            lines.append(f"**适用标准**: {', '.join(standards)}")

        conditions = summary.get("boundary_conditions", {})
        if conditions:
            lines.append("")
            lines.append("**边界条件**：")
            for k, v in conditions.items():
                lines.append(f"- {k}: {v}")

        lines.append("")
        lines.append("---")
        lines.append("## 🔑 请确认以下信息")
        lines.append("")
        lines.append("请仔细审核以上规划内容，以下决策点需要你确认：")
        lines.append("1. **项目名称/分析对象** — 是否准确描述了待分析的产品/系统？结构树将以此作为根节点。")
        lines.append("2. **产品类型和 FMEA 类型** — 是否正确？")
        lines.append("3. **涉及子系统** — 是否有遗漏或多余？")
        lines.append("4. **关键指标和适用标准** — 是否符合项目要求？")
        lines.append("5. **跨职能团队成员** — 请补充团队成员名单")
        lines.append("6. **SOD 评分标准** — 使用默认 AIAG/VDA 标准，或上传自定义标准")
        lines.append("")
        lines.append("确认后进入下一步：结构分析。后续步骤中，如遇到技术方向或关键知识需要确认，我会主动问你。")
        lines.append("文件增删改查等操作我会自动处理，不会反复打扰你。")

        return "\n".join(lines)
