"""自查结果模型 — 对应需求规格 §6.4 自查输出"""
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class SelfCheckIssue(BaseModel):
    """单条自查问题"""
    severity: str = "warning"    # error | warning | info
    issue_type: str = ""         # regulation_gap | bom_completeness | failure_logic | ...
    description: str = ""
    suggestion: str = ""          # 标准化修改建议
    location: str = ""            # 问题位置（如 "失效条目 #3"）
    context: dict = Field(default_factory=dict)


class SelfCheckReport(BaseModel):
    """自查报告"""
    stage_id: int = 0
    stage_name: str = ""
    passed: bool = False
    total_issues: int = 0
    critical_issues: int = 0
    issues: List[SelfCheckIssue] = Field(default_factory=list)
    summary: str = ""
    generated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
