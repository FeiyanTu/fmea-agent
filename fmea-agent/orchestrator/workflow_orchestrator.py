"""
工作流编排 Agent — 阶段确认驱动引擎

职责：
1. 阶段完成后，创建审批请求，等待全部组员确认
2. 定时检查确认状态，对未确认组员推送提醒
3. 全员确认后自动唤醒下一阶段引擎
4. 被驳回时通知相关人员并暂停流程

对应需求规格 §8 HC-4 双重校验（AI自查 + 人工确认）
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Awaitable

from models.approval import StageApproval, ProjectApprovalState

logger = logging.getLogger(__name__)

# 阶段名称映射
STAGE_NAMES = {
    2: "结构树分析",
    3: "功能分析",
    4: "失效分析",
    5: "风险分析",
    6: "优化分析",
    7: "标准化文件导出",
}

# 通知回调类型
NotifyCallback = Callable[[str, str, str], Awaitable[None]]
# member_name, stage_name, message → None


class WorkflowOrchestratorAgent:
    """工作流编排 Agent

    用法：
        orch = WorkflowOrchestratorAgent(data_dir, notify_callback)
        orch.set_project("proj-001", ["张工", "李工", "王工"])

        # 阶段完成时
        await orch.request_approval(2, "结构树分析完成，共45个节点")

        # 定时检查（可设为 automation）
        status = await orch.check_and_notify()
        if status["ready_to_proceed"]:
            await pipeline.step2_function(...)  # 进入下一阶段
    """

    def __init__(self, data_dir: str = "data/projects",
                 notify_callback: Optional[NotifyCallback] = None):
        self.data_dir = Path(data_dir)
        self.notify_callback = notify_callback
        self._state: Optional[ProjectApprovalState] = None
        self._project_id: str = ""
        self._on_all_approved: Optional[Callable[[int], Awaitable[None]]] = None
        self._check_interval: int = 300  # 默认 5 分钟检查一次
        self._last_notify_time: Dict[str, datetime] = {}  # 成员→上次通知时间
        self._notify_cooldown: int = 600  # 同一成员 10 分钟内不重复通知

    # ════════════════════════════════════════════════════════════
    # 项目设置
    # ════════════════════════════════════════════════════════════

    def set_project(self, project_id: str, project_name: str = "",
                    members: List[str] = None):
        """设置当前项目"""
        self._project_id = project_id
        self._members = members or []
        self._state = ProjectApprovalState(
            project_id=project_id, project_name=project_name
        )
        # 尝试从磁盘加载
        self._load_state()
        logger.info(f"[编排Agent] 项目已设置: {project_id}, {len(self._members)} 名成员")

    def set_auto_proceed_handler(self, handler: Callable[[int], Awaitable[None]]):
        """设置全员确认后的自动执行回调"""
        self._on_all_approved = handler

    # ════════════════════════════════════════════════════════════
    # 审批请求
    # ════════════════════════════════════════════════════════════

    async def request_approval(self, stage_id: int, summary: str) -> dict:
        """阶段完成后发起审批请求，通知全部组员

        Args:
            stage_id: 阶段编号 (2-6)
            summary: 阶段完成摘要（将包含在通知中）

        Returns:
            审批状态
        """
        if not self._state:
            return {"error": "未设置项目"}

        stage_name = STAGE_NAMES.get(stage_id, f"阶段{stage_id}")
        approval = self._state.get_or_create_stage(stage_id, stage_name, self._members)
        approval.reset()  # 重置旧审批记录
        self._state.current_stage = stage_id

        self._save_state()
        logger.info(f"[编排Agent] 阶段{stage_id} 审批已发起: {len(self._members)} 名待确认")

        # 通知全部成员
        await self._notify_all_members(stage_id, stage_name, summary)

        return approval.model_dump()

    async def approve_stage(self, stage_id: int, member_name: str,
                            comment: str = "") -> dict:
        """工程师提交审核确认（v5: 不再自动推进下一阶段，需等负责人审核）"""
        if not self._state:
            return {"error": "未设置项目", "success": False}

        stage_name = STAGE_NAMES.get(stage_id, f"阶段{stage_id}")
        approval = self._state.get_or_create_stage(stage_id, stage_name, self._members)

        ok = approval.submit(member_name, comment)
        if not ok:
            return {"success": False, "error": f"{member_name} 不是该阶段审批人"}

        self._save_state()
        logger.info(f"[编排Agent] {member_name} 已提交(待审核) 阶段{stage_id} ({approval.submitted_count}/{approval.total_count})")

        # 通知负责人：有新提交待审核
        if approval.is_all_submitted:
            logger.info(f"[编排Agent] 阶段{stage_id} 全员已提交，等待负责人审核")

        return {
            "success": True,
            "stage_id": stage_id,
            "member": member_name,
            "submitted_count": approval.submitted_count,
            "total_count": approval.total_count,
            "is_all_submitted": approval.is_all_submitted,
            "is_all_lead_approved": approval.is_all_lead_approved,
        }

    async def reject_stage(self, stage_id: int, member_name: str,
                           comment: str = "") -> dict:
        """成员驳回（标记阶段需修改）"""
        if not self._state:
            return {"error": "未设置项目", "success": False}

        stage_name = STAGE_NAMES.get(stage_id, f"阶段{stage_id}")
        approval = self._state.get_or_create_stage(stage_id, stage_name, self._members)

        ok = approval.reject(member_name, comment)
        if not ok:
            return {"success": False, "error": f"{member_name} 不是该阶段审批人"}

        self._save_state()
        logger.warning(f"[编排Agent] ⚠️ {member_name} 驳回 阶段{stage_id}: {comment}")

        # 通知全部成员有人驳回
        await self._notify_all_members(
            stage_id, stage_name,
            f"⚠️ {member_name} 驳回了阶段{stage_id}「{stage_name}」: {comment}\n需要修改后重新提交审批。"
        )

        return {
            "success": True,
            "stage_id": stage_id,
            "member": member_name,
            "rejected": True,
            "comment": comment,
        }

    # ════════════════════════════════════════════════════════════
    # v5: 负责人一对一审核（per-member，支持无 state 模式）
    # ════════════════════════════════════════════════════════════

    async def _ensure_state(self, project_id: str = None, members: list = None) -> bool:
        """v5: 确保 orchestrator state 已初始化（无 state 时从 DB 加载）"""
        if self._state:
            return True
        from pathlib import Path
        import json as _json
        pid = project_id or self._project_id
        if not pid:
            return False

        # 1) 尝试从磁盘加载
        state_path = self.data_dir / pid / "approval_state.json"
        if state_path.exists():
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                self._state = ProjectApprovalState(**data)
                # 加载成员列表
                if members:
                    self._members = members
                if not self._members:
                    for stage in self._state.stages.values():
                        if stage.required_members:
                            self._members = list(stage.required_members)
                            break
                logger.info(f"[编排Agent] 从磁盘恢复 state: project={pid}, members={len(self._members or [])}")
                return True
            except Exception as e:
                logger.warning(f"[编排Agent] 磁盘加载 state 失败: {e}")

        # 2) 兜底：仅设置 project_id，让上层 API 自行处理
        self._project_id = pid
        if members:
            self._members = members
        return False  # 没有可用 state

    async def leader_read_stage(self, stage_id: int, member_name: str) -> dict:
        """负责人已读某工程师的提交"""
        if not await self._ensure_state():
            return {"error": "未设置项目", "success": False}

        stage_name = STAGE_NAMES.get(stage_id, f"阶段{stage_id}")
        approval = self._state.get_or_create_stage(stage_id, stage_name, self._members)

        if member_name not in approval.member_approvals:
            return {"success": False, "error": f"{member_name} 尚未提交"}
        if approval.member_approvals[member_name].status != "approved":
            return {"success": False, "error": f"{member_name} 尚未提交审核"}

        from datetime import datetime
        approval.member_approvals[member_name].lead_status = "read"
        approval.member_approvals[member_name].lead_reviewed_at = datetime.now().isoformat()
        self._save_state()
        logger.info(f"[编排Agent] 负责人已读 {member_name} 阶段{stage_id}")

        return {"success": True, "stage_id": stage_id, "member": member_name, "lead_status": "read"}

    async def leader_approve_stage(self, stage_id: int, member_name: str,
                                     comment: str = "") -> dict:
        """负责人批准某工程师的提交（一对一通过）"""
        if not await self._ensure_state():
            return {"error": "未设置项目", "success": False}

        stage_name = STAGE_NAMES.get(stage_id, f"阶段{stage_id}")
        approval = self._state.get_or_create_stage(stage_id, stage_name, self._members)

        if member_name not in approval.member_approvals:
            return {"success": False, "error": f"{member_name} 尚未提交"}
        if approval.member_approvals[member_name].status not in ("submitted", "approved"):
            return {"success": False, "error": f"{member_name} 尚未提交审核"}

        from datetime import datetime
        # 负责人通过: 最终状态 → approved
        approval.member_approvals[member_name].lead_status = "approved"
        approval.member_approvals[member_name].status = "approved"
        approval.member_approvals[member_name].lead_comment = comment
        approval.member_approvals[member_name].lead_reviewed_at = datetime.now().isoformat()
        self._save_state()

        logger.info(
            f"[编排Agent] 负责人通过 {member_name} 阶段{stage_id} "
            f"(已通过: {sum(1 for m in approval.member_approvals.values() if m.is_lead_approved)}/{approval.total_count})"
        )

        # 全部通过 → 自动推进下一阶段
        if approval.is_all_lead_approved:
            approval.completed_at = datetime.now().isoformat()
            self._save_state()
            logger.info(f"[编排Agent] 🎉 阶段{stage_id} 负责人审核全部通过！")
            await self._notify_all_members(
                stage_id, stage_name,
                f"🎉 阶段{stage_id}「{stage_name}」负责人审核全部通过，即将进入下一阶段！"
            )
            if self._on_all_approved:
                await self._on_all_approved(stage_id)

        return {
            "success": True,
            "stage_id": stage_id,
            "member": member_name,
            "lead_status": "approved",
            "is_all_lead_approved": approval.is_all_lead_approved,
        }

    async def leader_reject_stage(self, stage_id: int, member_name: str,
                                    comment: str = "") -> dict:
        """负责人驳回某工程师的提交（一对一驳回），该工程师可重新提交"""
        if not await self._ensure_state():
            return {"error": "未设置项目", "success": False}

        stage_name = STAGE_NAMES.get(stage_id, f"阶段{stage_id}")
        approval = self._state.get_or_create_stage(stage_id, stage_name, self._members)

        if member_name not in approval.member_approvals:
            return {"success": False, "error": f"{member_name} 尚未提交"}

        from datetime import datetime
        # 驳回：重置该成员的工程师提交状态 + 负责人审核状态
        approval.member_approvals[member_name].lead_status = "rejected"
        approval.member_approvals[member_name].lead_comment = comment
        approval.member_approvals[member_name].lead_reviewed_at = datetime.now().isoformat()
        # 驳回后工程师可以重新提交，重置工程师审核状态
        approval.member_approvals[member_name].status = "pending"
        approval.member_approvals[member_name].approved_at = None
        self._save_state()

        logger.warning(
            f"[编排Agent] 负责人驳回 {member_name} 阶段{stage_id}: {comment}"
        )

        # 通知该成员（通过 notify_callback）
        if self.notify_callback:
            try:
                await self.notify_callback(
                    member_name, stage_name,
                    f"⚠️ 负责人驳回了你在阶段{stage_id}「{stage_name}」的提交: {comment}\n请修改后重新提交。"
                )
            except Exception as e:
                logger.error(f"[编排Agent] 驳回通知失败: {e}")

        return {
            "success": True,
            "stage_id": stage_id,
            "member": member_name,
            "lead_status": "rejected",
            "comment": comment,
        }

    # ════════════════════════════════════════════════════════════
    # 定时检查 & 推送
    # ════════════════════════════════════════════════════════════

    async def check_and_notify(self) -> dict:
        """定时检查：对未确认成员推送提醒，检查是否全员通过

        返回状态供调用方判断是否可以进入下一阶段。
        设计为可被 automation 定时调用。
        """
        if not self._state:
            return {"ready_to_proceed": False, "error": "未设置项目"}

        current = self._state.current_stage
        if current not in self._state.stages:
            return {"ready_to_proceed": False, "stage_id": current, "message": "无活跃审批"}

        approval = self._state.stages[current]

        # 如果已经全员通过
        if approval.is_all_submitted:
            return {
                "ready_to_proceed": True,
                "stage_id": current,
                "stage_name": approval.stage_name,
                "message": f"全员已确认，可以进入下一阶段",
            }

        # 对未确认成员推送提醒（有冷却时间）
        now = datetime.now()
        pending = approval.pending_members
        notified_count = 0
        for member in pending:
            last_time = self._last_notify_time.get(member)
            if last_time and (now - last_time).total_seconds() < self._notify_cooldown:
                continue  # 冷却中，跳过
            self._last_notify_time[member] = now
            await self._notify_single_member(
                member, current, approval.stage_name,
                f"⏰ 提醒：阶段{current}「{approval.stage_name}」等待你的确认 "
                f"({approval.submitted_count}/{approval.total_count} 已确认)"
            )
            notified_count += 1

        return {
            "ready_to_proceed": False,
            "stage_id": current,
            "stage_name": approval.stage_name,
            "submitted_count": approval.submitted_count,
            "total_count": approval.total_count,
            "pending_members": pending,
            "notified_this_round": notified_count,
            "message": f"等待 {len(pending)} 名成员确认: {', '.join(pending[:5])}",
        }

    def get_status(self, stage_id: Optional[int] = None) -> dict:
        """获取审批状态"""
        if not self._state:
            return {"error": "未设置项目"}

        if stage_id:
            approval = self._state.stages.get(stage_id)
            if not approval:
                return {"error": f"阶段{stage_id} 无审批记录"}
            return {
                "stage_id": stage_id,
                "stage_name": approval.stage_name,
                "submitted_count": approval.submitted_count,
                "total_count": approval.total_count,
                "is_all_submitted": approval.is_all_submitted,
                "pending": approval.pending_members,
                "rejected": approval.rejected_members,
                "members": {
                    m: a.model_dump()
                    for m, a in approval.member_approvals.items()
                },
            }

        return self._state.get_summary()

    # ════════════════════════════════════════════════════════════
    # 通知方法
    # ════════════════════════════════════════════════════════════

    async def _notify_all_members(self, stage_id: int, stage_name: str, message: str):
        """通知全部成员"""
        for member in self._members:
            await self._notify_single_member(member, stage_id, stage_name, message)

    async def _notify_single_member(self, member: str, stage_id: int,
                                     stage_name: str, message: str):
        """通知单个成员"""
        if self.notify_callback:
            try:
                await self.notify_callback(member, stage_name, message)
                logger.info(f"[编排Agent] 已通知 {member}: {message[:50]}...")
            except Exception as e:
                logger.error(f"[编排Agent] 通知 {member} 失败: {e}")
        else:
            # 无回调时仅记录日志
            logger.info(f"[编排Agent] [通知-{member}] {message[:80]}")

    # ════════════════════════════════════════════════════════════
    # 持久化
    # ════════════════════════════════════════════════════════════

    def _get_state_path(self) -> Path:
        project_dir = self.data_dir / self._project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        return project_dir / "approval_state.json"

    def _save_state(self):
        if not self._state:
            return
        path = self._get_state_path()
        data = self._state.model_dump()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    def _load_state(self):
        path = self._get_state_path()
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._state = ProjectApprovalState(**data)
                logger.info(f"[编排Agent] 已加载审批状态: {len(self._state.stages)} 个阶段")
            except Exception as e:
                logger.warning(f"[编排Agent] 加载审批状态失败: {e}")


# ─── 全局单例 ───

_orchestrator: Optional[WorkflowOrchestratorAgent] = None


def get_orchestrator(data_dir: str = "data/projects") -> WorkflowOrchestratorAgent:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = WorkflowOrchestratorAgent(data_dir)
    return _orchestrator
