"""
Agent 基类 — 定义多 Agent 协作系统的统一接口

每个专业 Agent 必须继承 BaseAgent，实现：
- analyze(): 执行本专业领域的分析
- validate_boundary(): 检测输出是否越界

系统集成专家 Agent 额外实现：
- review_and_merge(): 汇总多 Agent 结果
- resolve_conflict(): 跨专业冲突仲裁

对应需求规格 §3.2 & §3.3
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum

from models.agent_config import AgentConfig, AgentRole

logger = logging.getLogger(__name__)


# ─── 数据类 ───

class GateResult(str, Enum):
    """阶段门控结果"""
    PASS = "pass"
    REJECT = "reject"
    NEEDS_REVISION = "needs_revision"


@dataclass
class AgentContext:
    """Agent 输入上下文

    包含当前阶段所需的全部输入数据，以及项目配置信息。
    """
    step: int                              # 当前阶段 (1-7)
    step_name: str                         # 阶段名称
    project_config: Dict[str, Any] = field(default_factory=dict)  # 项目配置
    agent_config: Optional[AgentConfig] = None  # 当前 Agent 的配置
    input_data: Dict[str, Any] = field(default_factory=dict)     # 阶段输入数据
    baseline_doc: Optional[Dict[str, Any]] = None  # 策划基线文档（阶段1输出）


@dataclass
class AgentOutput:
    """Agent 输出结果

    每个 Agent 的 analyze() 方法返回此结构。
    """
    agent_name: str                        # 输出此结果的 Agent 名称
    step: int                              # 所属阶段
    success: bool = True                   # 是否成功
    data: Dict[str, Any] = field(default_factory=dict)  # 分析结果数据
    errors: List[str] = field(default_factory=list)     # 错误信息
    warnings: List[str] = field(default_factory=list)   # 警告信息（如边界接近）
    metadata: Dict[str, Any] = field(default_factory=dict)  # 附加元数据


@dataclass
class BoundaryViolation:
    """越界检测结果"""
    agent_name: str
    violation_type: str                    # "domain_crossing" | "scope_expansion" | "phase_skip"
    description: str
    offending_content: str
    severity: str = "warning"              # "error" | "warning"


# ─── Agent 基类 ───

class BaseAgent(ABC):
    """专业 Agent 基类

    子类必须：
    1. 在 __init__ 中设置 config 和 llm_client
    2. 实现 analyze() 方法
    3. 可覆盖 build_system_prompt() 自定义角色提示词
    """

    def __init__(self, config: AgentConfig, llm_client=None):
        self.config = config
        self.llm_client = llm_client
        self._context: Optional[AgentContext] = None

    # ── 身份属性 ──

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def display_name(self) -> str:
        return self.config.display_name

    @property
    def role(self) -> AgentRole:
        return self.config.role

    @property
    def domain(self) -> str:
        return self.config.domain

    @property
    def responsibilities(self) -> List[str]:
        return self.config.responsibilities

    @property
    def boundaries(self) -> List[str]:
        return self.config.boundaries

    # ── 核心接口 ──

    @abstractmethod
    async def analyze(self, context: AgentContext) -> AgentOutput:
        """执行本专业领域的分析

        这是 Agent 的核心方法，子类必须实现。
        系统集成专家 Agent 会调用此方法获取各专业的分析结果。

        Args:
            context: 当前阶段的输入上下文

        Returns:
            AgentOutput: 本专业分析结果
        """
        ...

    # 硬边界关键词（出现这些词表示该边界为不可越界约束）
    HARD_BOUNDARY_KEYWORDS = ["严禁", "不得", "禁止", "仅负责", "仅分析", "不参与", "不介入", "不涉及"]

    def validate_boundary(self, output: AgentOutput) -> List[BoundaryViolation]:
        """检测输出是否越界

        默认实现基于关键词检测，子类可覆盖实现更复杂的检测逻辑。
        系统集成专家在汇总前会调用此方法。

        边界分级：
        - 硬边界（含"严禁/不得/禁止/仅负责/不参与"等词）：severity="error"
        - 软边界（其他）：severity="warning"

        Args:
            output: Agent 的输出结果

        Returns:
            越界项列表（空列表表示无越界）
        """
        violations = []
        text_to_check = str(output.data)

        for boundary in self.boundaries:
            # 提取边界中的关键词
            keywords = self._extract_boundary_keywords(boundary)
            # 判断是否硬边界
            is_hard = any(
                kw_hard in boundary
                for kw_hard in self.HARD_BOUNDARY_KEYWORDS
            )
            severity = "error" if is_hard else "warning"
            for kw in keywords:
                if kw and kw in text_to_check:
                    violations.append(BoundaryViolation(
                        agent_name=self.name,
                        violation_type="domain_crossing",
                        description=f"输出可能涉及禁止领域: {boundary}",
                        offending_content=f"检测到关键词: {kw}",
                        severity=severity,
                    ))
        return violations

    def _extract_boundary_keywords(self, boundary: str) -> List[str]:
        """从边界描述中提取关键词（用于简单文本匹配）

        子类可覆盖实现更智能的提取。
        """
        # 提取 "不涉及"、"不介入"、"仅负责" 后面的关键词
        import re
        keywords = []
        patterns = [
            r'不涉及(.*?)(?:[，,;；]|$)',
            r'不介入(.*?)(?:[，,;；]|$)',
            r'仅负责(.*?)(?:[，,;；]|$)',
        ]
        for pattern in patterns:
            matches = re.findall(pattern, boundary)
            keywords.extend(m.strip() for m in matches if m.strip())

        # 如果没有匹配到，使用边界原文的分词
        if not keywords:
            keywords = [w.strip() for w in boundary.replace('，', ',').split(',') if w.strip()]

        return keywords

    # ── System Prompt 构建 ──

    def build_system_prompt(self) -> str:
        """构建 Agent 的角色 System Prompt

        子类可覆盖此方法来自定义提示词。
        默认基于 AgentConfig 自动生成。
        """
        lines = [
            f"你是 **{self.display_name}**，在 FMEA 多 Agent 协作系统中承担专业分析角色。",
            "",
            f"## 你的专业领域",
            f"{self.domain}",
            "",
            "## 你的职责",
        ]
        for i, resp in enumerate(self.responsibilities, 1):
            lines.append(f"{i}. {resp}")

        lines.append("")
        lines.append("## 边界约束（严格禁止越界）")
        for i, boundary in enumerate(self.boundaries, 1):
            lines.append(f"- {boundary}")

        lines.append("")
        lines.append("## 协作规则")
        lines.append("1. 你的输出将提交给系统集成专家进行统一校验和合并")
        lines.append("2. 仅分析你专业领域内的内容，不越界到其他 Agent 的领域")
        lines.append("3. 如果发现需要其他专业协同的问题，在输出中标注 `[需协同: <专业名称>]`")
        lines.append("4. 所有分析必须基于阶段 1 确定的打分基准和分析边界")

        if self.config.system_prompt_extra:
            lines.append("")
            lines.append("## 额外指令")
            lines.append(self.config.system_prompt_extra)

        return "\n".join(lines)

    # ── LLM 调用辅助 ──

    async def _call_llm(
        self,
        system_prompt: str,
        user_message: str,
        fallback: bool = True,
    ) -> str:
        """调用 LLM（封装 llm_client.safe_call）"""
        if self.llm_client is None:
            raise RuntimeError(f"Agent '{self.name}' 未配置 LLM 客户端")

        return await self.llm_client.safe_call(
            system_prompt=system_prompt,
            user_message=user_message,
            fallback_to_complete=fallback,
        )

    def _extract_json(self, text: str) -> Dict[str, Any]:
        """从 LLM 输出中提取 JSON"""
        if self.llm_client is None:
            raise RuntimeError(f"Agent '{self.name}' 未配置 LLM 客户端")
        return self.llm_client.extract_json(text)

    def __repr__(self) -> str:
        return f"<{self.display_name} ({self.name}) role={self.role.value}>"


# ─── 系统集成专家基类 ───

class SystemIntegrationAgent(BaseAgent):
    """系统集成专家 Agent 基类

    继承 BaseAgent，额外提供跨 Agent 协调能力。
    对应需求规格 §3.2。
    """

    def __init__(self, config: AgentConfig, llm_client=None):
        if config.role != AgentRole.SYSTEM_INTEGRATION:
            raise ValueError("SystemIntegrationAgent 要求 AgentRole.SYSTEM_INTEGRATION")
        super().__init__(config, llm_client)

    async def review_and_merge(
        self,
        outputs: List[AgentOutput],
        context: AgentContext,
    ) -> AgentOutput:
        """汇总并校验多个专业 Agent 的输出

        Args:
            outputs: 各专业 Agent 的分析结果列表
            context: 当前阶段上下文

        Returns:
            合并校验后的统一输出
        """
        all_data = {}
        all_errors = []
        all_warnings = []

        for output in outputs:
            if not output.success:
                all_errors.append(f"[{output.agent_name}] {output.errors}")
                continue
            # 去重合并：同名 key 出现冲突时记录警告
            for key, value in output.data.items():
                if key in all_data:
                    all_warnings.append(
                        f"跨专业数据冲突: '{key}' 由 [{output.agent_name}] 和之前 Agent 同时输出"
                    )
                all_data[key] = value
            all_warnings.extend(output.warnings)

        return AgentOutput(
            agent_name=self.name,
            step=context.step,
            success=len(all_errors) == 0,
            data=all_data,
            errors=all_errors,
            warnings=all_warnings,
            metadata={"merged_from": [o.agent_name for o in outputs]},
        )

    def resolve_conflict(
        self,
        conflict_desc: str,
        agent_a: str,
        agent_b: str,
        data_a: Dict[str, Any],
        data_b: Dict[str, Any],
    ) -> Dict[str, Any]:
        """仲裁跨专业冲突

        系统集成专家拥有最终裁定权。

        Args:
            conflict_desc: 冲突描述
            agent_a/agent_b: 冲突双方 Agent 名称
            data_a/data_b: 冲突数据

        Returns:
            裁定后的最终数据
        """
        logger.warning(
            f"[Conflict] {conflict_desc} | {agent_a} vs {agent_b}"
        )
        # 默认策略：标记冲突，保留第一方数据，需要人工介入
        return {
            "resolved_by": self.name,
            "conflict": conflict_desc,
            f"from_{agent_a}": data_a,
            f"from_{agent_b}": data_b,
            "resolution": "manual_review_required",
            "selected": data_a,  # 默认保留先输出的 Agent 数据
        }
