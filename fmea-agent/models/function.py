"""Function Analysis Models - 多功能版 (v3)

核心变更：
- FunctionAnalysisItem.functions: List[FunctionDetail] — 支持一个关注要素有多个功能
- FunctionDetail: 每条功能有独立 ID、描述和类别
- 向后兼容：key_element_function / higher_level_function / focus_function 作为 property
"""
from typing import List, Optional, Any
from pydantic import BaseModel, Field, computed_field


class FunctionDetail(BaseModel):
    """单条功能描述 — 对应 DFMEA 表中一行功能，满足 §4.3 全部要求

    一个关注要素可以有多个功能，每个功能独立描述、独立分析失效模式。
    例如：防爆阀 → 功能1(开阀压力 8±2kPa) + 功能2(瞬时排气量 ≥5L/s)

    v4 链式方法论：每个 function 服务于一个具体的 child_node（下一较低级别）。
    例如 12V低压电源系统 的 functions 应该按 child 拆分：
      fn-node-0001-01  →  child=蓄电池(锂离子电池包)  → 储能供电
      fn-node-0001-02  →  child=BMS                    → 电池状态监控
    如此保证 step 3 的失效分析能按 (focus, child) 一对一展开。

    v5 需求文档对齐（§4.3）：
    - function_class: 四分类（主功能/辅助功能/安全强制功能/法规合规功能）
    - sor_binding + sor_metric: SOR 量化指标绑定（§4.3.2-3）
    - regulation_binding: 法规条款编号绑定（§4.3.2-3）
    - responsible_agent: 责任专业 Agent（§4.3.2-5）
    """
    function_id: str = ""                  # fn-{node_id}-{idx}，如 fn-node-003-01
    child_node_id: str = ""                # v4: 本功能服务的子级节点 ID（下一较高级别要素）
    description: str = ""                  # 功能描述（含量化指标）
    # ── v7: FMEA 三级功能链 ──
    upper_function: str = ""               # 上一较高级别功能及要求（LLM从WeKnora推理）
    lower_function: str = ""               # 下一较低级别功能及要求（LLM从WeKnora推理）

    # ── v5: 需求 §4.3.2-2 标准四分类 ──
    function_class: str = "主功能"         # 主功能/辅助功能/安全强制功能/法规合规功能
    function_category: str = "关键功能"    # 工程动作标签（"连接"/"支撑"/"传导"/"防护"/"控制"等）

    flow_type: str = "无"                  # 功能流类型：电气/机械力/热/流体/信号/无

    # ── v5: 需求 §4.3.2-3 双向绑定 ──
    sor_binding: str = ""                  # SOR 章节/条款编号，如 "SOR-3.2.1"
    sor_metric: str = ""                   # 绑定的量化指标，如 "绝缘电阻 >=100MOhm"
    regulation_binding: str = ""           # 法规条款编号，如 "GB38031-2020 §5.2.3"

    # ── v5: 需求 §4.3.2-5 Agent 归集 ──
    responsible_agent: str = ""            # 责任专业 Agent 名称
    is_safety_critical: bool = False       # 安全强制功能标记
    is_regulation_mandated: bool = False   # 法规合规功能标记
    # ── v6: 安全域/法规主题（参考 Excel 格式） ──
    safety_domain: str = ""                # 所属安全域，如"热蔓延"/"电安全(过充过放外短)"/"机械安全"
    parent_node_name: str = ""             # 上一级要素名称（本功能链的上一级）
    child_node_name: str = ""              # 下一级要素名称（本功能链的下一级）

    model_config = {"extra": "allow"}


class InterfaceRelation(BaseModel):
    """零部件之间的界面关系（v26 内部载体）

    分析阶段由各专业 Agent 产出，后处理时**转换为一条 FunctionDetail**（功能）追加到
    主要归属零部件的 functions 列表中 —— 界面就是一种功能，走既有的功能表/推送/
    失效分析链路，不引入独立的表结构。

    归属原则：界面关系归属于占主要部分的零部件（primary_owner_node_id），
    由 LLM 结合工程语义与知识库依据判定，并给出 attribution_reason。
    """
    interface_id: str = ""
    node_id: str = ""                      # 当前挂载的节点（通常为主要归属方）
    node_name: str = ""                    # 当前挂载的节点名称
    counterpart_node_id: str = ""          # 界面对端零部件 ID
    counterpart_node_name: str = ""        # 界面对端零部件名称
    interface_type: str = ""               # 界面类型（由 LLM 按工程本质确定，不限枚举）
    description: str = ""                  # 界面关系描述（界面在哪里、连接/接触什么）
    requirement: str = ""                  # 界面工程要求（量值来自知识库检索）
    primary_owner: str = "self"            # 主要归属方："self"=挂载节点 / "counterpart"=对端
    attribution_reason: str = ""           # 归属理由
    regulation_binding: str = ""           # 法规/标准条款
    responsible_agent: str = ""            # 产出该界面分析的专业 Agent

    model_config = {"extra": "allow"}


class FunctionAnalysisItem(BaseModel):
    """单个关注要素的功能分析 — 支持多功能

    v2: upper/key/lower 各一个字符串
    v3: functions 为 FunctionDetail 列表，支持多个关键功能
        upper_level_function / lower_level_function 仍为字符串（汇总描述层级关系）
    """
    node_id: str
    node_name: str
    upper_level_function: str = ""         # 上一较高级别功能及要求（汇总描述）
    functions: List[FunctionDetail] = Field(default_factory=list)  # 关注要素多功能列表
    lower_level_function: str = ""         # 下一较低级别功能及要求（汇总描述）
    llm_generated: bool = True
    user_modified: bool = False
    # ── v7: 用于DB按工程师拆分 ──
    assignee_user_id: int = -1
    assignee_name: str = ""

    # ── v26: 零部件间界面关系（本节点为主要归属方的界面）──
    interfaces: List[InterfaceRelation] = Field(default_factory=list)

    # 前端兼容别名（v2 遗留字段，通过 computed_field 自动填充）
    component_name: str = ""
    higher_level_function: str = ""
    focus_function: str = ""

    @computed_field
    @property
    def key_element_function(self) -> str:
        """向后兼容：合并所有功能描述为分号分隔的字符串"""
        if not self.functions:
            return ""
        return "; ".join(f.description for f in self.functions if f.description)

    model_config = {"extra": "allow"}


class FunctionRelation(BaseModel):
    """功能网络中的一条边 — DFMEA "需要-满足" 关系

    关系类型：
    - "需要-满足": 父功能(需要方) → 子功能(满足方)，自上而下的需求分解
      例：系统功能"提供侧面碰撞保护" 需要 组件功能"传递碰撞力" 来实现
    - "功能配合": 同级功能之间的耦合/依赖关系
      例：A"提供电力" 配合 B"消耗电力"
    """
    id: str
    source_node_id: str                    # 需要方（上层）节点 ID
    source_function_id: str = ""           # 需要方功能 ID
    target_node_id: str                    # 满足方（下层）节点 ID
    target_function_id: str = ""           # 满足方功能 ID
    relation_type: str                     # "需要-满足" / "功能配合"
    description: str                       # 关系的工程含义
    source_label: str = ""                 # 源端标签（供前端展示）
    target_label: str = ""                 # 目标端标签


class FunctionNetwork(BaseModel):
    """功能网络"""
    relations: List[FunctionRelation] = Field(default_factory=list)
    generated_at: str = ""


class FunctionAnalysis(BaseModel):
    """完整功能分析（v5: 符合 §4.3 全部要求）

    items: 功能分析条目列表（每个关注要素一个，含多功能）
    function_network: 功能网络边关系
    node_functions: node_id -> {node_name, functions[]} 映射（供结构树集成）
    function_network_detail: 每条功能的功能网详情（供功能网按钮弹窗）
    validation_report: v5 新增 — 校验报告（V3-1~V3-4 执行结果）
    """
    items: List[FunctionAnalysisItem] = Field(default_factory=list)
    function_network: Optional[FunctionNetwork] = None
    node_functions: dict = Field(default_factory=dict)
    function_network_detail: list = Field(default_factory=list)
    llm_raw_output: str = ""
    user_edited: bool = False

    # ── v5: 校验报告（需求 §4.3.4） ──
    validation_report: dict = Field(default_factory=dict)
    # { "V3-1": {"passed": True, "issues": []}, "V3-2": {...}, ... }
    consolidated: bool = False             # 是否已完成跨 Agent 合并去重
    regulation_data_source: str = ""        # 法规知识来源（WeKnora KB ID 或文案）

    # ── v6: 参考Excel格式的扁平行（三列三级链） ──
    domain_rows: list = Field(default_factory=list)
