"""
阶段门控 — 对应需求规格 §8 硬性执行约束 (HC-4)

流程：
1. AI 自动自查 → SelfCheck Engine
2. 系统集成专家逻辑校验
3. 签发/驳回
"""
import logging
from typing import Any, Dict, List, Optional
from enum import Enum

from engine.self_check import SelfCheckEngine

logger = logging.getLogger(__name__)


class GateResult(str, Enum):
    PASS = "pass"
    REJECT = "reject"
    NEEDS_REVISION = "needs_revision"


class Gate:
    """阶段门控

    每阶段完成后，必须先通过门控才能进入下一阶段。
    """

    def __init__(self):
        self._checker = SelfCheckEngine()
        self._gate_log: List[Dict[str, Any]] = []

    def check_stage(
        self,
        stage_id: int,
        stage_name: str,
        stage_output: Dict[str, Any],
        require_human_review: bool = True,
    ) -> Dict[str, Any]:
        """核查一个阶段的输出是否通过门控

        Args:
            stage_id: 阶段编号
            stage_name: 阶段名称
            stage_output: 阶段输出数据
            require_human_review: 是否需要人工复核

        Returns:
            门控结果
        """
        # 1. AI 自动自查
        check_result = self._checker.run_all_checks(stage_id, stage_name, stage_output)

        # 2. 判断是否通过
        if check_result["passed"]:
            gate_result = GateResult.PASS
        elif check_result["critical_issues"] <= 2:
            gate_result = GateResult.NEEDS_REVISION
            logger.warning(
                f"[Gate] 阶段 {stage_id} 需修正: {check_result['critical_issues']} 个严重问题"
            )
        else:
            gate_result = GateResult.REJECT
            logger.error(
                f"[Gate] 阶段 {stage_id} 驳回: {check_result['critical_issues']} 个严重问题"
            )

        # 3. 记录
        record = {
            "stage_id": stage_id,
            "stage_name": stage_name,
            "gate_result": gate_result.value,
            "check_result": check_result,
            "human_review_required": require_human_review and gate_result != GateResult.PASS,
            "timestamp": None,  # 由调用方填充
        }
        self._gate_log.append(record)

        return record

    def get_gate_log(self) -> List[Dict[str, Any]]:
        """获取门控日志"""
        return self._gate_log

    def get_stage_status(self, stage_id: int) -> Optional[Dict[str, Any]]:
        """获取指定阶段的最后一次门控状态"""
        for record in reversed(self._gate_log):
            if record["stage_id"] == stage_id:
                return record
        return None

    def can_proceed_to_next_stage(self, stage_id: int) -> bool:
        """检查是否可以进入下一阶段"""
        status = self.get_stage_status(stage_id)
        if status is None:
            return False
        return status["gate_result"] == GateResult.PASS.value

    def clear_log(self):
        """清空门控日志"""
        self._gate_log.clear()
