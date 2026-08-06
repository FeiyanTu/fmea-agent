# FMEA 数据模型 - Pydantic v2
from .project import FmeaProject
from .structure import StructureTreeNode, StructureTree
from .function import FunctionAnalysisItem, FunctionRelation, FunctionNetwork, FunctionAnalysis
from .failure import FailureMode, FailureAnalysis
from .risk import RiskAssessment, RiskAnalysis, ApRule, ApTable
from .optimization import OptimizationAction, OptimizationAnalysis
from .report import FmeaReportRow, FmeaFinalReport
from .agent_config import (
    AgentConfig, AgentRole, ProjectAgentConfig, HEV_PROFESSIONAL_AGENTS
)

__all__ = [
    "FmeaProject",
    "StructureTreeNode", "StructureTree",
    "FunctionAnalysisItem", "FunctionRelation", "FunctionNetwork", "FunctionAnalysis",
    "FailureMode", "FailureAnalysis",
    "RiskAssessment", "RiskAnalysis", "ApRule", "ApTable",
    "OptimizationAction", "OptimizationAnalysis",
    "FmeaReportRow", "FmeaFinalReport",
    # v4.2 新增：多 Agent 配置模型
    "AgentConfig", "AgentRole", "ProjectAgentConfig", "HEV_PROFESSIONAL_AGENTS",
]
