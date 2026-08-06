"""
系统集成专家 Agent — 对应需求规格 §3.2 总负责人 Agent

职责：
- 基线锁定：通读全套输入文件，输出《DFMEA策划基线文档》
- 打分统一下发：S/O/D 评分标准、AP 分级规则
- 阶段放行：逐阶段校验后签发 Gate Pass
- 冲突仲裁：跨专业冲突最终裁定

注意：本 Agent 不直接开展具体专业的失效分析。
"""
import logging
from typing import Any, Dict, List, Optional

from agents.base import (
    AgentContext, AgentOutput, SystemIntegrationAgent, GateResult
)
from models.agent_config import AgentConfig, AgentRole

logger = logging.getLogger(__name__)


class SystemIntegrationExpert(SystemIntegrationAgent):
    """HEV 电池包系统集成专家（总负责人 Agent）"""

    def __init__(self, llm_client=None):
        config = AgentConfig(
            name="system_integration",
            display_name="系统集成专家",
            role=AgentRole.SYSTEM_INTEGRATION,
            domain="HEV 电池包全系统集成",
            responsibilities=[
                "汇总全部专业 Agent 输出，统一校验、合并、版本锁定",
                "统一对齐客户 SOR、全套设计图纸、完整层级 BOM、初版 DFMEA 草稿、法规解读文件",
                "识别资料缺口并统一下发补充要求至对应专业 Agent",
                "全程主导 AIAG-VDA FMEA 2021 七步法完整流程",
                "逐阶段输出质量把关，统一协调跨专业冲突",
            ],
            boundaries=["不直接开展具体专业的失效分析（由各专业 Agent 承担）"],
        )
        super().__init__(config, llm_client)

    # ════════════════════════════════════════════════════════════
    # 核心接口：analyze()
    # ════════════════════════════════════════════════════════════

    async def analyze(self, context: AgentContext) -> AgentOutput:
        """系统集成专家的 analyze() 按阶段分派

        阶段 1：策划准备 & 基线锁定
        其他阶段：汇总校验各专业 Agent 输出 + 门控放行
        """
        if context.step == 1:
            return await self._step1_prepare_and_baseline(context)
        else:
            return await self._generic_review(context)

    # ════════════════════════════════════════════════════════════
    # 阶段 1：策划准备 & 前置输入锁定 (§4.1.2)
    # ════════════════════════════════════════════════════════════

    async def _step1_prepare_and_baseline(self, context: AgentContext) -> AgentOutput:
        """阶段 1 策划准备：通读输入文件，输出《DFMEA策划基线文档》"""
        input_data = context.input_data

        # 1. 分析边界划定
        boundary = self._define_analysis_boundary(input_data)

        # 2. 输入资料缺失项梳理
        gaps = self._identify_input_gaps(input_data)

        # 3. 全专业打分基准统一下发
        scoring_criteria = self._define_scoring_criteria(input_data)

        baseline = {
            "baseline_version": "1.0",
            "analysis_boundary": boundary,
            "input_gaps": gaps,
            "scoring_criteria": scoring_criteria,
            "project_info": input_data.get("project_info", {}),
        }

        # 如果配置了 LLM，使用 LLM 增强基线文档
        if self.llm_client:
            enhanced = await self._llm_enhance_baseline(baseline, input_data)
            if enhanced:
                baseline = enhanced

        return AgentOutput(
            agent_name=self.name,
            step=1,
            success=True,
            data={"baseline_doc": baseline},
            metadata={"phase": "planning_baseline"},
        )

    def _define_analysis_boundary(self, input_data: dict) -> dict:
        """划定分析边界"""
        project = input_data.get("project_info", {})
        return {
            "product_type": project.get("product_type", "HEV 混动动力电池包"),
            "development_phase": project.get("development_phase", "DV"),
            "typical_conditions": project.get("boundary_conditions", [
                "频繁启停工况",
                "高低温环境工况",
                "宽倍率充放电工况",
                "道路随机振动工况",
                "碰撞冲击工况",
                "电芯热失控蔓延工况",
                "整车高压上下电逻辑工况",
            ]),
            "analysis_exclusions": [
                "生产过程装配不良类失效",
                "来料零部件质量缺陷类失效",
                "客户不当使用类失效",
            ],
        }

    def _identify_input_gaps(self, input_data: dict) -> list:
        """梳理输入资料缺失项"""
        gaps = []
        if not input_data.get("bom_content"):
            gaps.append({"item": "完整层级 BOM", "status": "缺失", "action": "请上传 BOM 文件"})
        if not input_data.get("sor_content"):
            gaps.append({"item": "客户 SOR 文档", "status": "缺失", "action": "请上传 SOR 文件"})
        if not input_data.get("drawing_files"):
            gaps.append({"item": "设计图纸", "status": "建议补充", "action": "建议上传 Pack 及子零件图纸"})
        if not input_data.get("initial_dfmea"):
            gaps.append({"item": "初版 DFMEA 草稿", "status": "可选", "action": "有则可纳入基线对比"})

        # 检查 BOM 层级
        bom = input_data.get("bom_content", "")
        if bom and not self._check_bom_hierarchy(bom):
            gaps.append({"item": "BOM 层级完整性", "status": "需核实", "action": "请确认 BOM 是否包含完整四级层级"})

        return gaps

    def _check_bom_hierarchy(self, bom: str) -> bool:
        """简单检查 BOM 是否有多级结构"""
        levels = set()
        for line in bom.split('\n'):
            line = line.strip()
            if line.startswith('#'):
                levels.add(line.count('#'))
        return len(levels) >= 3

    # AIAG-VDA FMEA 2021 标准 S/O/D 评分分档表（结构化）
    SEVERITY_S_TABLE = {
        10: "最高严重度，安全相关，无预警 — 直接危及人身安全",
        9:  "非常严重，安全相关，有预警 — 主要安全功能失效",
        8:  "非常严重，主要功能丧失 — 车辆无法行驶",
        7:  "高严重度，主要功能降级 — 性能大幅下降",
        6:  "中等严重度，次要功能丧失 — 主要子系统失效",
        5:  "中等严重度，次要功能降级 — 性能下降需维修",
        4:  "较低严重度，影响舒适性 — 用户感知但可继续使用",
        3:  "轻微影响，驾驶体验下降 — 需要检修但不影响行驶",
        2:  "极轻微，仪表指示 — 故障灯亮起但功能正常",
        1:  "无影响 — 不可感知",
    }
    OCCURRENCE_O_TABLE = {
        10: "极高发生率（≥1/10 件，新品必发）",
        9:  "非常高（1/20）",
        8:  "高（1/50）",
        7:  "较高（1/100）",
        6:  "中等（1/500）",
        5:  "中低（1/2000）",
        4:  "低（1/10000）",
        3:  "较低（1/50000）",
        2:  "极低（1/200000）",
        1:  "极罕见（≤1/1000000）",
    }
    DETECTION_D_TABLE = {
        10: "几乎无法探测 — 无任何检测手段",
        9:  "探测可能性极低 — 极模糊的间接信号",
        8:  "探测可能性低 — 远程/统计级分析",
        7:  "较低 — 仿真或虚拟分析",
        6:  "中低 — 部件级测试（破坏性）",
        5:  "中等 — 部件级测试（非破坏性）",
        4:  "中高 — 子系统级测试",
        3:  "较高 — 系统级测试",
        2:  "高 — 在线监控 + 自动诊断",
        1:  "一定能探测 — 防错设计/100% 自动检测",
    }
    AP_RULES = {
        "H": {
            "description": "高风险 — 必须制定优化措施",
            "criteria": "S≥8 + (O≥4 或 D≥5)；或 S=9~10 + O≥3",
            "action": "必须高亮 + 强制配套设计优化方案（短期或迭代）",
        },
        "M": {
            "description": "中风险 — 建议制定优化措施",
            "criteria": "S=5~7 + O≥4；或 S=6~8 + D≥6；或 S=5~7 + O×D≥30",
            "action": "建议配套优化方案",
        },
        "L": {
            "description": "低风险 — 无需额外措施",
            "criteria": "不满足 H/M 条件",
            "action": "无需优化（除非涉及法规违规）",
        },
    }
    FAILURE_EFFECT_HIERARCHY = [
        "L1: 下级子零件级影响",
        "L2: 子零件级影响",
        "L3: 子系统级影响",
        "L4: 总成（Pack）级影响",
        "L5: 整车级影响",
    ]

    def _define_scoring_criteria(self, input_data: dict) -> dict:
        """统一定义全专业打分基准 — 结构化分档表（对应需求 §4.1.2 任务 2）

        输出字段：
        - severity_s / occurrence_o / detection_d: 1-10 分档含义表
        - ap_classification: H/M/L 三档判定规则
        - failure_effect_hierarchy: 失效影响层级 L1~L5
        - issued_by / issued_at: 签发信息（基线锁定）
        """
        # 允许用户从 input_data 覆盖默认表
        s_table = input_data.get("s_table") or self.SEVERITY_S_TABLE
        o_table = input_data.get("o_table") or self.OCCURRENCE_O_TABLE
        d_table = input_data.get("d_table") or self.DETECTION_D_TABLE
        ap_rules = input_data.get("ap_rules") or self.AP_RULES

        return {
            "severity_s": {
                "scale": "1-10",
                "description": "严重度 S 评分标准（AIAG-VDA 2021）",
                "scale_table": {str(k): v for k, v in s_table.items()},
                "note": "10=最高严重度（安全相关），1=无影响",
            },
            "occurrence_o": {
                "scale": "1-10",
                "description": "发生度 O 评分标准（AIAG-VDA 2021）",
                "scale_table": {str(k): v for k, v in o_table.items()},
                "note": "10=极高发生频率，1=极低",
            },
            "detection_d": {
                "scale": "1-10",
                "description": "探测度 D 评分标准（AIAG-VDA 2021，1=一定能探测）",
                "scale_table": {str(k): v for k, v in d_table.items()},
                "note": "10=几乎无法探测，1=一定能探测（注意 D 高分=难探测）",
            },
            "ap_classification": {
                "description": "AP 风险优先级分级规则（AIAG-VDA 2021 Action Priority）",
                "levels": ap_rules,
            },
            "failure_effect_hierarchy": {
                "description": "失效影响层级分级标准",
                "levels": self.FAILURE_EFFECT_HIERARCHY,
            },
            "issued_by": "系统集成专家",
            "issued_at": self._now_iso(),
            "lock_status": "LOCKED",
            "lock_note": "本基线一经锁定，后续阶段不得擅自修改（HC-5）",
        }

    def _now_iso(self) -> str:
        """当前时间 ISO 格式"""
        import datetime as _dt
        return _dt.datetime.now().isoformat(timespec="seconds")

    async def _llm_enhance_baseline(self, baseline: dict, input_data: dict) -> Optional[dict]:
        """使用 LLM 增强基线文档（可选）"""
        try:
            prompt = self.build_system_prompt()
            user_msg = f"""请通读以下输入文件信息，输出增强版的《DFMEA策划基线文档》。

## 当前基线
{baseline}

## 输入信息
- 项目名: {input_data.get('project_info', {}).get('name', '未知')}
- BOM长度: {len(input_data.get('bom_content', ''))} 字符
- SOR长度: {len(input_data.get('sor_content', ''))} 字符

请用 JSON 格式输出增强后的基线文档，确保字段与原始基线一致。
"""
            result = await self._call_llm(prompt, user_msg)
            return self._extract_json(result)
        except Exception as e:
            logger.warning(f"LLM 增强基线文档失败（使用基础基线）: {e}")
            return None

    # ════════════════════════════════════════════════════════════
    # 其他阶段：汇总校验 & 门控
    # ════════════════════════════════════════════════════════════

    async def _generic_review(self, context: AgentContext) -> AgentOutput:
        """通用阶段：汇总各专业 Agent 输出，执行校验"""
        # 如果传入的是多 Agent 输出列表，执行 merge
        multi_outputs = context.input_data.get("agent_outputs", [])
        if multi_outputs:
            return await self.review_and_merge(multi_outputs, context)

        # 否则，检查门控条件
        return await self._gate_check(context)

    async def _gate_check(self, context: AgentContext) -> AgentOutput:
        """阶段门控检查"""
        # 检查是否满足进入下一阶段的条件
        stage_output = context.input_data.get("stage_output", {})
        self_check_result = context.input_data.get("self_check_result", {})

        issues = self_check_result.get("issues", [])
        critical_issues = [i for i in issues if i.get("severity") == "error"]

        if critical_issues:
            return AgentOutput(
                agent_name=self.name,
                step=context.step,
                success=False,
                errors=[f"阶段 {context.step} 门控未通过: {len(critical_issues)} 个严重问题"],
                data={
                    "gate_result": GateResult.REJECT.value,
                    "critical_issues": critical_issues,
                },
            )

        return AgentOutput(
            agent_name=self.name,
            step=context.step,
            success=True,
            data={
                "gate_result": GateResult.PASS.value,
                "message": f"阶段 {context.step} ({context.step_name}) 门控通过",
                "stage_output": stage_output,
            },
            metadata={"gate": "passed"},
        )

    # ════════════════════════════════════════════════════════════
    # 打分统一下发
    # ════════════════════════════════════════════════════════════

    def issue_scoring_standard(self) -> dict:
        """统一下发 S/O/D 打分标准给所有专业 Agent"""
        return {
            "s_criteria": None,    # 由阶段 1 确定
            "o_criteria": None,
            "d_criteria": None,
            "ap_rules": None,
            "instruction": (
                "所有专业 Agent 必须使用此统一打分标准，"
                "禁止各自定义打分规则。"
            ),
        }
