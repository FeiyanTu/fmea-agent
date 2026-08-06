"""FMEA Final Report Model - maps to Java FmeaModels.FmeaReportRow / FmeaFinalReport"""
from typing import List, Optional
from pydantic import BaseModel, Field
from .structure import StructureTree
from .function import FunctionAnalysis
from .failure import FailureAnalysis
from .risk import RiskAnalysis, ApTable
from .optimization import OptimizationAnalysis


class FmeaReportRow(BaseModel):
    """DFMEA报告中的一行 (对应Excel的46列)"""
    # 结构分析
    next_higher_level: str = ""  # 上一较高级别
    focus_element: str = ""  # 关注要素
    next_lower_level: str = ""  # 下一较低级别

    # 功能分析
    higher_level_function: str = ""  # 上一级功能和要求
    focus_element_function: str = ""  # 关注要素功能和要求
    lower_level_function: str = ""  # 下一级功能和要求

    # 失效分析
    failure_effect: str = ""  # 失效影响(FE)
    severity: int = 4  # 严重度(S)
    failure_mode: str = ""  # 失效模式(FM)
    failure_cause: str = ""  # 失效原因(FC)

    # 风险分析
    prevention_control: str = ""  # 预防措施(PC)
    occurrence: int = 5  # 频度(O)
    detection_control: str = ""  # 探测措施(DC)
    detection: int = 5  # 探测度(D)
    rpn: int = 0
    ap: str = ""  # DFMEA AP
    special_characteristics: str = ""  # 特性类别

    # 优化
    preventive_action: str = ""  # 预防措施
    detective_action: str = ""  # 探测措施
    responsible_person: str = ""  # 负责人
    target_date: str = ""  # 目标完成日期
    status: str = ""  # 状态
    evidence_action: str = ""  # 基于证据的措施
    completion_date: str = ""  # 完成日期
    improved_s: int = 0
    improved_o: int = 0
    improved_d: int = 0
    improved_ap: str = ""

    # 基本信息
    issue_number: str = ""  # 问题编号
    history: str = ""  # 历史/变更授权
    remark: str = ""  # 备注


class FmeaFinalReport(BaseModel):
    """完整FMEA最终报告"""
    project_id: str
    project_name: str
    fmea_type: str
    generated_at: str = ""
    # 规划信息
    company_name: str = ""
    product_model: str = ""
    part_number: str = ""
    customer_name: str = ""
    production_location: str = ""
    model_year_program: str = ""
    customer_pn: str = ""
    confidentiality_level: str = ""
    team_members: List[str] = Field(default_factory=list)
    customer_drawing_revision: str = ""

    # 各步骤数据
    structure_tree: Optional[StructureTree] = None
    function_analysis: Optional[FunctionAnalysis] = None
    failure_analysis: Optional[FailureAnalysis] = None
    risk_analysis: Optional[RiskAnalysis] = None
    ap_table: Optional[ApTable] = None
    optimization_analysis: Optional[OptimizationAnalysis] = None

    # 组装后的报告行
    report_rows: List[FmeaReportRow] = Field(default_factory=list)

    def get_summary(self) -> dict:
        return {
            "project_name": self.project_name,
            "fmea_type": self.fmea_type,
            "part_number": self.part_number,
            "structure_nodes": len(self.structure_tree.nodes) if self.structure_tree else 0,
            "function_items": len(self.function_analysis.items) if self.function_analysis else 0,
            "failure_chains": len(self.failure_analysis.items) if self.failure_analysis else 0,
            "risk_items": len(self.risk_analysis.items) if self.risk_analysis else 0,
            "optimization_items": len(self.optimization_analysis.items) if self.optimization_analysis else 0,
            "report_rows": len(self.report_rows),
            "high_risk_count": len([r for r in self.report_rows if r.ap == "H"]),
            "generated_at": self.generated_at,
        }
