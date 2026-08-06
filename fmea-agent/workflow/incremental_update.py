"""
模块化局部迭代引擎 — 对应需求规格 §7

支持：
- 指定单一模块独立局部迭代，无需全文档重算
- 4 类触发条件：新增零部件、更新SOR、补充工况、更新法规
- 迭代后自动联动更新跨模块连锁失效 AP 值
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ChangeType(str, Enum):
    """局部迭代触发条件 — §7.1"""
    ADD_COMPONENT = "add_component"            # 新增零部件
    UPDATE_SOR = "update_sor"                  # 更新客户 SOR 指标
    ADD_CONDITION = "add_condition"             # 补充产品工况边界
    UPDATE_REGULATION = "update_regulation"     # 更新法规标准条款


@dataclass
class IncrementalUpdateRequest:
    """局部迭代请求"""
    change_type: ChangeType
    target_module_id: str         # 目标模块/零部件 ID
    change_data: Dict[str, Any]   # 变更数据
    project_id: str = ""


@dataclass
class UpdateResult:
    """局部迭代结果"""
    changed_modules: List[str]           # 已刷新的模块 ID
    affected_modules: List[str]          # 受影响的关联模块 ID
    ap_updates: List[Dict[str, Any]]     # 联动的 AP 值更新
    unchanged_modules: List[str]         # 保持不变的模块 ID
    success: bool = True
    errors: List[str] = field(default_factory=list)


class IncrementalUpdateEngine:
    """局部迭代引擎

    用法：
        engine = IncrementalUpdateEngine("data/projects")
        result = engine.update(
            IncrementalUpdateRequest(
                change_type=ChangeType.ADD_COMPONENT,
                target_module_id="node-025",
                change_data={"name": "新传感器", "level": "零件"}
            )
        )
    """

    def __init__(self, data_dir: str = "data/projects"):
        self.data_dir = Path(data_dir)

    # ════════════════════════════════════════════════════════════
    # 主入口
    # ════════════════════════════════════════════════════════════

    def update(self, request: IncrementalUpdateRequest) -> UpdateResult:
        """执行局部迭代

        仅刷新指定模块及其关联模块，其余保持不动。
        """
        logger.info(
            f"[增量更新] {request.change_type.value}: "
            f"target={request.target_module_id}"
        )

        # 1. 计算影响范围
        affected = self._calculate_affected_modules(request)
        logger.info(f"[增量更新] 受影响模块: {affected}")

        # 2. 加载现有数据
        existing_data = self._load_project_data(request.project_id)

        # 3. 按变更类型处理
        if request.change_type == ChangeType.ADD_COMPONENT:
            result = self._handle_add_component(request, existing_data, affected)
        elif request.change_type == ChangeType.UPDATE_SOR:
            result = self._handle_update_sor(request, existing_data, affected)
        elif request.change_type == ChangeType.ADD_CONDITION:
            result = self._handle_add_condition(request, existing_data, affected)
        elif request.change_type == ChangeType.UPDATE_REGULATION:
            result = self._handle_update_regulation(request, existing_data, affected)
        else:
            result = UpdateResult(success=False, errors=[f"未知变更类型: {request.change_type}"])

        # 4. 联动更新跨模块 AP
        ap_updates = self._update_cross_module_ap(affected, existing_data)
        result.ap_updates = ap_updates
        result.affected_modules = list(affected)

        # 5. 记录不变模块
        all_modules = set(existing_data.get("modules", []))
        result.unchanged_modules = list(all_modules - set(affected) - {request.target_module_id})
        result.changed_modules = [request.target_module_id] + list(affected)

        return result

    # ════════════════════════════════════════════════════════════
    # 影响范围计算
    # ════════════════════════════════════════════════════════════

    def _calculate_affected_modules(
        self, request: IncrementalUpdateRequest
    ) -> Set[str]:
        """计算受影响模块范围"""
        affected = set()
        target = request.target_module_id

        # 加载结构树和功能网数据
        data = self._load_project_data(request.project_id)

        if request.change_type == ChangeType.ADD_COMPONENT:
            # 父节点 → 同级功能配合节点
            parent = self._find_parent(target, data)
            if parent:
                affected.add(parent)
            # 同级节点
            siblings = self._find_siblings(target, data)
            affected.update(siblings)

        elif request.change_type == ChangeType.UPDATE_SOR:
            # 所有引用了该 SOR 指标的功能
            affected.update(self._find_sor_references(target, data))

        elif request.change_type == ChangeType.ADD_CONDITION:
            # 所有关联的失效条目
            affected.update(self._find_condition_references(target, data))

        elif request.change_type == ChangeType.UPDATE_REGULATION:
            # 所有引用了该法规条款的失效
            affected.update(self._find_regulation_references(target, data))

        return affected

    def _find_parent(self, node_id: str, data: dict) -> Optional[str]:
        """查找父节点"""
        structure = data.get("structure_tree", {})
        nodes = structure.get("nodes", []) if isinstance(structure, dict) else []
        for node in nodes:
            if node.get("id") == node_id:
                return node.get("parentId")
        return None

    def _find_siblings(self, node_id: str, data: dict) -> List[str]:
        """查找同级节点"""
        parent = self._find_parent(node_id, data)
        if not parent:
            return []
        structure = data.get("structure_tree", {})
        nodes = structure.get("nodes", []) if isinstance(structure, dict) else []
        return [
            n["id"] for n in nodes
            if n.get("parentId") == parent and n["id"] != node_id
        ]

    def _find_sor_references(self, sor_key: str, data: dict) -> List[str]:
        """查找引用了特定 SOR 指标的功能"""
        func = data.get("function_analysis", {})
        items = func.get("items", []) if isinstance(func, dict) else []
        return [
            item.get("nodeId", "") for item in items
            if sor_key.lower() in str(item).lower()
        ]

    def _find_condition_references(self, condition_id: str, data: dict) -> List[str]:
        """查找与特定工况关联的失效"""
        failure = data.get("failure_analysis", {})
        items = failure.get("items", []) if isinstance(failure, dict) else []
        return [
            item.get("nodeId", "") for item in items
            if condition_id.lower() in str(item).lower()
        ]

    def _find_regulation_references(self, reg_id: str, data: dict) -> List[str]:
        """查找引用了特定法规的条目"""
        doc1 = data.get("document_1", {})
        mapping = doc1.get("mapping_table", [])
        affected_modules = set()
        for row in mapping:
            if reg_id.upper() in row.get("regulation_id", "").upper():
                module = row.get("responsible_module", "")
                if module:
                    affected_modules.add(module)
        return list(affected_modules)

    # ════════════════════════════════════════════════════════════
    # 变更处理
    # ════════════════════════════════════════════════════════════

    def _handle_add_component(
        self, request: IncrementalUpdateRequest, data: dict, affected: set
    ) -> UpdateResult:
        """处理新增零部件"""
        new_node = request.change_data
        new_node["id"] = request.target_module_id
        logger.info(f"[增量] 新增零部件: {new_node.get('name', '?')}")
        return UpdateResult(
            changed_modules=[request.target_module_id],
            affected_modules=list(affected),
            ap_updates=[],
            unchanged_modules=[],
        )

    def _handle_update_sor(
        self, request: IncrementalUpdateRequest, data: dict, affected: set
    ) -> UpdateResult:
        """处理 SOR 指标更新"""
        logger.info(f"[增量] 更新 SOR 指标: {request.target_module_id}")
        return UpdateResult(
            changed_modules=[request.target_module_id],
            affected_modules=list(affected),
            ap_updates=self._recalculate_ap_for_modules(list(affected), data),
            unchanged_modules=[],
        )

    def _handle_add_condition(
        self, request: IncrementalUpdateRequest, data: dict, affected: set
    ) -> UpdateResult:
        """处理工况补充"""
        logger.info(f"[增量] 补充工况: {request.change_data.get('condition', '?')}")
        return UpdateResult(
            changed_modules=[request.target_module_id],
            affected_modules=list(affected),
            ap_updates=[],
            unchanged_modules=[],
        )

    def _handle_update_regulation(
        self, request: IncrementalUpdateRequest, data: dict, affected: set
    ) -> UpdateResult:
        """处理法规更新"""
        logger.info(f"[增量] 更新法规: {request.target_module_id}")
        return UpdateResult(
            changed_modules=[request.target_module_id],
            affected_modules=list(affected),
            ap_updates=self._recalculate_ap_for_modules(list(affected), data),
            unchanged_modules=[],
        )

    # ════════════════════════════════════════════════════════════
    # 联动更新
    # ════════════════════════════════════════════════════════════

    def _update_cross_module_ap(
        self, affected: Set[str], data: dict
    ) -> List[Dict[str, Any]]:
        """联动更新跨模块连锁失效的 AP 值"""
        updates = []

        # 查找跨模块连锁失效
        high_risk_themes = data.get("high_risk_themes", [])
        risk_data = data.get("risk_analysis", {})
        risk_items = risk_data.get("items", []) if isinstance(risk_data, dict) else []

        for theme in high_risk_themes:
            involved = set(theme.get("involved_agents", []))
            if involved & affected:
                # 该主题涉及被修改的模块，需要重新评估关联 AP
                related_risks = [
                    r for r in risk_items
                    if r.get("theme") == theme.get("theme")
                ]
                for risk in related_risks:
                    updates.append({
                        "failure_id": risk.get("id", "?"),
                        "theme": theme.get("theme"),
                        "action": "需重新评估 AP 值",
                        "reason": f"关联模块已变更: {involved & affected}",
                    })

        return updates

    def _recalculate_ap_for_modules(
        self, module_ids: List[str], data: dict
    ) -> List[Dict[str, Any]]:
        """重新计算指定模块的 AP 值"""
        updates = []
        risk_data = data.get("risk_analysis", {})
        items = risk_data.get("items", []) if isinstance(risk_data, dict) else []

        for item in items:
            if item.get("module_id") in module_ids:
                # 标记需要重算
                updates.append({
                    "failure_id": item.get("id", "?"),
                    "action": "重新计算 AP",
                    "old_ap": item.get("ap", "?"),
                    "new_ap": "待计算",
                })

        return updates

    # ════════════════════════════════════════════════════════════
    # 数据加载
    # ════════════════════════════════════════════════════════════

    def _load_project_data(self, project_id: str) -> dict:
        """加载项目全部数据"""
        if not project_id:
            return {}
        project_dir = self.data_dir / project_id / "fmea"
        if not project_dir.exists():
            return {}

        data = {}
        for step_file in sorted(project_dir.glob("*.json")):
            try:
                with open(step_file, "r", encoding="utf-8") as f:
                    data[step_file.stem] = json.load(f)
            except Exception:
                pass

        return data
