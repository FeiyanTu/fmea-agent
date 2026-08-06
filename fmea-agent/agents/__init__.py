"""FMEA 多 Agent 协作系统

包含：
- 基础层：BaseAgent、SystemIntegrationAgent、AgentContext、AgentOutput
- 专 家：SystemIntegrationExpert（系统集成专家）、RegulationAgent（法规专家）
- 专业 Agent：7 个步骤 Agent（Planning/Structure/Function/Failure/Risk/Optimization/Document）

v4.2 新增（断点1）：
- base.py: Agent 基类 & 越界检测
- system_integration.py: 系统集成专家
- regulation.py: 法规专家
"""
from .base import (
    BaseAgent,
    SystemIntegrationAgent,
    AgentContext,
    AgentOutput,
    BoundaryViolation,
    GateResult,
)
from .system_integration import SystemIntegrationExpert
from .regulation import RegulationAgent
from .step0_planning import PlanningAgent
from .step2_function import FunctionAgent
from .step3_failure import FailureAgent
from .step4_risk import RiskAgent
from .step5_optimization import OptimizationAgent
from .step6_document import DocumentAgent

__all__ = [
    # 基础层
    "BaseAgent",
    "SystemIntegrationAgent",
    "AgentContext",
    "AgentOutput",
    "BoundaryViolation",
    "GateResult",
    # 集成专家 & 法规专家
    "SystemIntegrationExpert",
    "RegulationAgent",
    # 7 个步骤 Agent
    "PlanningAgent",
    "FunctionAgent",
    "FailureAgent",
    "RiskAgent",
    "OptimizationAgent",
    "DocumentAgent",
]
