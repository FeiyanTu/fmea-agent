"""Failure Analysis Models - v4 增强版（KB感知 + 评分依据 + 链式方法论）

核心变更：
- FailureMode 新增 severity_reason 字段
- 失效模式可直接追溯到具体功能，并可查看 LLM 评分解读
- failure_network_type 记录失效在功能网中的传导类型
- v4 链式方法论：新增 child_node_id，标识失效针对的下一较低级别
"""
from typing import List, Optional, Dict, Tuple
from pydantic import BaseModel, Field


class FailureMode(BaseModel):
    """单条失效链 (FE → S → FM → FC)

    v4: 增加 severity_reason / failure_network_type
    - severity_reason: LLM 为 severity 赋值的依据简述
    - failure_network_type: 该失效在功能网中的传导类型
      （如 "功能丧失传导"、"功能降级传导"、"安全功能失效"、"冗余失效"）

    v4 链式方法论：每条失效链对应一个具体的 child_node（下一较低级别）。
    例如 12V低压电源系统 → 蓄电池 的失效链：
      fm-node-0001-01-01  →  child=蓄电池(锂离子电池包)  → 储能供电功能丧失
      fm-node-0001-01-02  →  child=蓄电池(锂离子电池包)  → 储能供电功能降级
    child_node_id 与 step 2 的 FunctionDetail.child_node_id 一一对应。

    v5: 与功能分析对齐的冗余字段（从结构树 + FunctionAnalysisItem 继承，
    保证失效分析表的前 4 列与功能分析表逐行一致，不依赖前端跨表查找）
    """
    id: str = ""
    node_id: str
    node_name: str
    child_node_id: str = ""                # v4: 该失效链针对的子级节点 ID（下一较低级别）
    function_id: str = ""
    function_description: str = ""
    failure_effect: str = ""           # 失效影响(FE)
    severity: int = 4                  # 严重度 S (1-10)
    severity_reason: str = ""          # v4: S 评分依据（参考AIAG/VDA标准）
    failure_mode: str = ""             # 失效模式(FM)
    failure_cause: str = ""            # 失效原因(FC)
    failure_network_type: str = ""     # v4: 功能网传导类型
    llm_generated: bool = True
    user_modified: bool = False

    # ── v5: 与功能分析对齐的冗余字段（Optional，默认空，不破坏已有数据）──
    higher_level_name: str = ""        # 上一较高级别名称（从结构树 parent 反查）
    child_node_name: str = ""          # 下一较低级别名称（从 child_node_id 反查）
    upper_function: str = ""           # 上一较高级别功能和要求（继承自 FunctionDetail.upper_function）
    lower_function: str = ""           # 下一较低级别功能和要求或特性（继承自 FunctionDetail.lower_function）
    assignee_user_id: int = -1         # 工程师 ID（继承自 FunctionAnalysisItem）
    assignee_name: str = ""            # 工程师名称（继承自 FunctionAnalysisItem）


class FailureAnalysis(BaseModel):
    """完整失效分析"""
    items: List[FailureMode] = Field(default_factory=list)
    llm_raw_output: str = ""
    user_edited: bool = False

    def get_high_severity(self, threshold: int = 7) -> List[FailureMode]:
        return [f for f in self.items if f.severity >= threshold]

    def group_by_node_and_function(self) -> Dict[str, Dict]:
        """按 node_name → function_id → [FailureMode] 分级分组

        返回结构：
        {
          "节点名称": {
            "node_id": "...",
            "functions": {
              "fn-xxx-01": {
                "function_id": "fn-xxx-01",
                "function_description": "...",
                "failures": [FailureMode, ...]
              }
            }
          }
        }
        """
        groups: Dict[str, Dict] = {}
        for fm in self.items:
            node_key = fm.node_name or fm.node_id
            if node_key not in groups:
                groups[node_key] = {
                    "node_id": fm.node_id,
                    "functions": {},
                }
            fn_key = fm.function_id or "_unknown"
            if fn_key not in groups[node_key]["functions"]:
                groups[node_key]["functions"][fn_key] = {
                    "function_id": fm.function_id,
                    "function_description": fm.function_description,
                    "failures": [],
                }
            groups[node_key]["functions"][fn_key]["failures"].append(fm)
        return groups

    def get_summary_stats(self) -> dict:
        """返回失效分析汇总统计"""
        total = len(self.items)
        high = self.get_high_severity(7)
        safety = [f for f in self.items if f.failure_network_type == "安全功能失效"]
        loss = [f for f in self.items if f.failure_network_type == "功能丧失传导"]
        degrade = [f for f in self.items if f.failure_network_type == "功能降级传导"]
        return {
            "total": total,
            "high_severity_count": len(high),
            "safety_failure_count": len(safety),
            "function_loss_count": len(loss),
            "function_degrade_count": len(degrade),
        }
