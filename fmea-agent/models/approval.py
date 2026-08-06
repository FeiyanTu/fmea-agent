"""
阶段审批模型 — v5 状态机

状态机:
  工程师: pending(未提交) → submitted(已提交,待负责人审核)
  负责人: 逐人审核 → 通过(approved) / 驳回(rejected,重置提交状态)

  阶段推进条件: is_all_lead_approved (全部成员负责人通过)
"""
from datetime import datetime
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class MemberApproval(BaseModel):
    """单个成员对单个阶段的确认记录"""
    member_name: str
    stage_id: int
    stage_name: str
    # ── 工程师提交状态 ──
    status: str = "pending"    # pending(未提交) | submitted(已提交) | rejected(驳回)
    comment: str = ""
    approved_at: Optional[str] = None
    notified_at: Optional[str] = None
    # ── 负责人审核状态 ──
    lead_status: str = "pending"     # pending | read | approved | rejected
    lead_comment: str = ""
    lead_reviewed_at: Optional[str] = None

    @property
    def is_lead_approved(self) -> bool:
        return self.lead_status == "approved"


class StageApproval(BaseModel):
    """单阶段审批状态"""
    stage_id: int
    stage_name: str
    required_members: List[str] = Field(default_factory=list)
    member_approvals: Dict[str, MemberApproval] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None

    @property
    def submitted_count(self) -> int:
        """已提交(待审核)的工程师数"""
        return sum(1 for a in self.member_approvals.values() if a.status == "submitted")

    @property
    def total_count(self) -> int:
        return len(self.required_members)

    @property
    def is_all_submitted(self) -> bool:
        """全部工程师已提交"""
        if not self.required_members:
            return True
        return self.submitted_count >= self.total_count

    @property
    def is_all_lead_approved(self) -> bool:
        """v5: 负责人已全部通过"""
        if not self.required_members:
            return True
        return all(
            m in self.member_approvals and
            self.member_approvals[m].is_lead_approved
            for m in self.required_members
        )

    @property
    def pending_members(self) -> List[str]:
        """尚未提交/待审核的成员"""
        return [
            m for m in self.required_members
            if m not in self.member_approvals or
               self.member_approvals[m].status == "pending"
        ]

    @property
    def rejected_members(self) -> List[str]:
        return [
            m for m in self.required_members
            if m in self.member_approvals and
               self.member_approvals[m].status == "rejected"
        ]

    # ── 兼容旧属性（前端 / self_check / gate 可能引用）──
    @property
    def approved_count(self) -> int:
        """兼容旧代码: 已提交数"""
        return self.submitted_count

    @property
    def is_all_approved(self) -> bool:
        """兼容旧代码: 全员已提交"""
        return self.is_all_submitted

    def submit(self, member_name: str, comment: str = "") -> bool:
        """工程师提交审核 (status → submitted)"""
        if member_name not in self.required_members:
            return False
        self.member_approvals[member_name] = MemberApproval(
            member_name=member_name,
            stage_id=self.stage_id,
            stage_name=self.stage_name,
            status="submitted",
            comment=comment,
            approved_at=datetime.now().isoformat(),
        )
        return True

    def reject(self, member_name: str, comment: str = "") -> bool:
        """成员驳回"""
        if member_name not in self.required_members:
            return False
        self.member_approvals[member_name] = MemberApproval(
            member_name=member_name,
            stage_id=self.stage_id,
            stage_name=self.stage_name,
            status="rejected",
            comment=comment,
            approved_at=datetime.now().isoformat(),
        )
        return True

    def reset(self):
        self.member_approvals.clear()
        self.completed_at = None


class ProjectApprovalState(BaseModel):
    """项目级审批状态"""
    project_id: str
    project_name: str = ""
    stages: Dict[int, StageApproval] = Field(default_factory=dict)
    current_stage: int = 1

    def get_or_create_stage(self, stage_id: int, stage_name: str,
                            members: List[str]) -> StageApproval:
        if stage_id not in self.stages:
            self.stages[stage_id] = StageApproval(
                stage_id=stage_id,
                stage_name=stage_name,
                required_members=members,
            )
        return self.stages[stage_id]

    def get_summary(self) -> dict:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "current_stage": self.current_stage,
            "stages": {
                sid: {
                    "stage_name": s.stage_name,
                    "submitted": s.submitted_count,
                    "approved": s.approved_count,
                    "total": s.total_count,
                    "is_all_submitted": s.is_all_submitted,
                    "is_all_approved": s.is_all_submitted,     # 兼容
                    "is_all_lead_approved": s.is_all_lead_approved,
                    "pending": s.pending_members,
                    "rejected": s.rejected_members,
                    "members": {
                        name: {
                            "status": ma.status,
                            "lead_status": ma.lead_status,
                            "lead_comment": ma.lead_comment,
                            "comment": ma.comment,
                            "approved_at": ma.approved_at,
                            "lead_reviewed_at": ma.lead_reviewed_at,
                        }
                        for name, ma in s.member_approvals.items()
                    },
                }
                for sid, s in self.stages.items()
            },
        }
