"""Optimization Analysis Models - maps to Java FmeaModels.OptimizationAction"""
from typing import List, Optional
from pydantic import BaseModel, Field


class OptimizationAction(BaseModel):
    """单条优化措施"""
    id: str = ""
    failure_id: str
    preventive_action: str = ""  # DFMEA预防措施
    detective_action: str = ""  # DFMEA探测措施
    responsible_person: str = ""  # 负责人姓名
    target_date: str = ""  # 目标完成日期
    status: str = "待实施"  # 待实施 / 实施中 / 已完成
    evidence_action: str = ""  # 采取基于证据的措施
    completion_date: str = ""  # 完成日期
    # 改进后评分
    improved_s: int = 0
    improved_o: int = 0
    improved_d: int = 0
    improved_ap: str = ""
    remark: str = ""  # 备注
    # v4.1 链式方法论：从 RiskAssessment 透传（保持 DFMEA 链路完整）
    node_id: str = ""
    node_name: str = ""
    child_node_id: str = ""
    child_node_name: str = ""
    function_id: str = ""
    llm_generated: bool = True
    user_modified: bool = False

    # v4.2 新增：优化方案分类 & 成本标注 (§6)
    category: str = ""        # "short_term" | "next_gen"  短期方案 / 迭代方案
    cost_impact: str = ""     # "high" | "medium" | "low"  改造成本影响
    maintenance_cost: str = ""  # "high" | "medium" | "low"  长期维护成本
    design_change_type: str = ""  # 设计变更类型：
    #   "hardware_revision" | "software_logic" | "structure_material" |
    #   "cell_upgrade" | "sor_correction"


class OptimizationAnalysis(BaseModel):
    """完整优化分析"""
    items: List[OptimizationAction] = Field(default_factory=list)
    llm_raw_output: str = ""
    user_edited: bool = False
