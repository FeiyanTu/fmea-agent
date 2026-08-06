"""
多 Agent 协作编排层

提供：
- AgentDispatcher: Agent 调度器 & 消息路由
- Gate: 阶段门控（AI自查 + 集成专家校验）
"""
from .agent_dispatcher import AgentDispatcher
from .gate import Gate, GateResult
from .workflow_orchestrator import WorkflowOrchestratorAgent, STAGE_NAMES, get_orchestrator

__all__ = [
    "AgentDispatcher", "Gate", "GateResult",
    "WorkflowOrchestratorAgent", "STAGE_NAMES", "get_orchestrator",
]
