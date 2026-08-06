"""Risk Analysis Models - maps to Java FmeaModels.RiskAssessment / ApTable"""
from typing import List, Optional
from pydantic import BaseModel, Field


class RiskAssessment(BaseModel):
    """单条风险评估 (PC/O/DC/D/RPN/AP)"""
    failure_id: str
    failure_mode: str = ""
    prevention_control: str = ""  # 当前预防措施(PC)
    occurrence: int = 5  # 发生频度 O (1-10)
    detection_control: str = ""  # 探测措施(DC)
    detection: int = 5  # 探测度 D (1-10)
    severity: int = 4  # 严重度 S (引用自FailureMode)
    rpn: int = 0  # RPN = S × O × D
    ap: str = ""  # DFMEA AP (H/M/L)
    special_characteristics: str = ""  # 特性类别
    # v12: 法规绑定与违规标记（来自 function.domain_rows JOIN，需求 §4.5.2-4 / §4.6.2）
    regulation_binding: str = ""  # 绑定的强制法规条款，如 "GB 38031-2020 §5.2"
    regulation_violation: bool = False  # 是否违反强制法规（无论 AP 等级均需优化）
    # v4.1 链式方法论：从 FailureMode 透传
    node_id: str = ""
    node_name: str = ""
    child_node_id: str = ""  # 对应的下一较低级别子级（AIAG-VDA 链式）
    child_node_name: str = ""  # 子级名（来自结构树，冗余便于展示）
    function_id: str = ""
    llm_generated: bool = True
    user_modified: bool = False

    # ── v8: 与功能分析对齐的工程师拆分字段 ──
    assignee_user_id: int = -1  # 工程师 ID（继承自 FailureMode）
    assignee_name: str = ""     # 工程师名称（继承自 FailureMode）


class RiskAnalysis(BaseModel):
    """完整风险分析"""
    items: List[RiskAssessment] = Field(default_factory=list)
    llm_raw_output: str = ""
    user_edited: bool = False

    def get_high_risk(self, rpn_threshold: int = 80) -> List[RiskAssessment]:
        return [r for r in self.items if r.rpn >= rpn_threshold or r.ap == "H"]


class ApRule(BaseModel):
    """单条AP规则"""
    s_min: int; s_max: int
    o_min: int; o_max: int
    d_min: int; d_max: int
    ap_level: str  # H / M / L
    description: str = ""


class ApTable(BaseModel):
    """AP表定义"""
    name: str = "AIAG/VDA Default AP Table"
    description: str = ""
    rules: List[ApRule] = Field(default_factory=list)

    def calculate_ap(self, s: int, o: int, d: int) -> str:
        """根据S/O/D查找AP等级"""
        for rule in self.rules:
            if rule.s_min <= s <= rule.s_max and \
               rule.o_min <= o <= rule.o_max and \
               rule.d_min <= d <= rule.d_max:
                return rule.ap_level
        # 默认规则：按RPN判断
        rpn = s * o * d
        if rpn >= 100:
            return "H"
        elif rpn >= 40:
            return "M"
        return "L"
