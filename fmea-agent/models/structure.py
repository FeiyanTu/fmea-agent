"""Structure Tree Models - maps to Java FmeaModels.StructureTreeNode / StructureTree"""
import logging
from typing import List
from pydantic import BaseModel, Field, ConfigDict

logger = logging.getLogger(__name__)


# DFMEA 合法层级（仅此 4 级）
VALID_LEVELS = {"系统", "子系统", "组件", "零件"}


class StructureTreeNode(BaseModel):
    """结构树节点"""
    id: str  # 唯一标识 node-001
    name: str  # 部件名称
    part_number: str = ""
    level: str = "零件"  # 系统 / 子系统 / 组件 / 零件
    parent_id: str = ""  # 父节点ID，根节点为空
    quantity: int = 1
    material: str = ""
    specification: str = ""
    llm_generated: bool = True
    user_modified: bool = False
    model_config = ConfigDict(extra="allow")  # 兼容 assignee_user_id / assignee_name 等


class StructureTree(BaseModel):
    """完整结构树"""
    nodes: List[StructureTreeNode] = Field(default_factory=list)
    llm_raw_output: str = ""
    user_edited: bool = False

    def count_by_level(self, level: str) -> int:
        return len([n for n in self.nodes if n.level == level])

    def get_summary(self) -> dict:
        """按层级汇总节点数（DFMEA 标准四级：系统/子系统/组件/零件）"""
        return {
            "total": len(self.nodes),
            "系统": self.count_by_level("系统"),
            "子系统": self.count_by_level("子系统"),
            "组件": self.count_by_level("组件"),
            "零件": self.count_by_level("零件"),
        }


def clean_structure_nodes(nodes_raw: List[dict]) -> List[dict]:
    """清理 LLM 生成的原始结构树节点

    执行以下清理：
    1. 移除非法层级（如"特性"）—— 特性应作为父节点的属性
    2. 移除重复的模块（相同名称只保留第一个）
    3. 重新编号节点（node-0001 起）
    4. 填充 FMEA 三列表（next_higher / focus / next_lower）
    """
    if not nodes_raw:
        return []

    # ── 1. 筛选合法层级 ──
    valid = [n for n in nodes_raw if n.get("level") in VALID_LEVELS]
    removed_levels = [n.get("level") for n in nodes_raw if n.get("level") not in VALID_LEVELS]
    if removed_levels:
        unique_bad = set(removed_levels)
        logger.warning(
            f"[clean] 移除非法层级节点 {len(removed_levels)} 个"
            f"（层级: {', '.join(unique_bad)}），"
            "其信息应作为父节点的 specification/char_requirement 属性"
        )

    # ── 2. 去重（同名节点只保留第一个）──
    seen_names = set()
    deduped = []
    for n in valid:
        name = n.get("name", "")
        if name in seen_names:
            logger.warning(f"[clean] 移除重复节点: {name}")
            continue
        seen_names.add(name)
        deduped.append(n)

    # ── 3. 重新编号 ──
    old_to_new = {}
    for i, n in enumerate(deduped):
        old_id = n.get("id", n.get("nodeId", ""))
        new_id = f"node-{i+1:04d}"
        old_to_new[old_id] = new_id
        n["id"] = new_id
        # 兼容两种字段名
        if "nodeId" in n:
            n.pop("nodeId", None)

    for n in deduped:
        pid = n.get("parentId", n.get("parent_id", ""))
        if pid in old_to_new:
            n["parent_id"] = old_to_new[pid]
        else:
            n["parent_id"] = ""
        # 统一字段名
        n.pop("parentId", None)

    # ── 4. 构建 ID→name 和 parent→children ──
    id_to_name = {n["id"]: n["name"] for n in deduped}
    parent_to_children = {}
    for n in deduped:
        pid = n.get("parent_id", "")
        if pid:
            parent_to_children.setdefault(pid, []).append(n)

    # ── 5. 填充 FMEA 三列表 ──
    for n in deduped:
        nid = n["id"]
        pid = n.get("parent_id", "")
        n["fmea_next_higher"] = id_to_name.get(pid, "")
        n["fmea_focus_element"] = n["name"]

        children = parent_to_children.get(nid, [])
        if children:
            names = [c["name"] for c in children[:3]]
            n["fmea_next_lower"] = ", ".join(names)
            if len(children) > 3:
                n["fmea_next_lower"] += f" ...（+{len(children)-3}）"
        else:
            n["fmea_next_lower"] = ""

    return deduped
