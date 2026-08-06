"""Agent 配置模型 — 对应需求规格 §2.5 项目配置模型 & §3 Agent 角色体系"""
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class AgentRole(str, Enum):
    """Agent 角色类型"""
    SYSTEM_INTEGRATION = "system_integration"  # 系统集成专家（总负责人）
    PROFESSIONAL = "professional"              # 支撑专业 Agent


class AgentConfig(BaseModel):
    """单个 Agent 的配置定义

    本项目初始化时通过 YAML/JSON 加载，定义每个 Agent 的身份、
    职责、边界约束。HEV 电池包案例中配置了 7 个专业 Agent。
    """
    name: str = Field(..., description="Agent 唯一标识，如 'bms_hardware'")
    display_name: str = Field(..., description="Agent 显示名称，如 'BMS硬件专家'")
    role: AgentRole = Field(default=AgentRole.PROFESSIONAL, description="角色类型")

    # 专业领域 & 职责
    domain: str = Field(..., description="专业领域描述，如 'BMS 硬件电路设计'")
    responsibilities: List[str] = Field(
        default_factory=list,
        description="职责列表，如 ['元器件选型分析', '电路拓扑失效分析']"
    )

    # 边界约束（禁止越界）
    boundaries: List[str] = Field(
        default_factory=list,
        description="禁止分析的领域列表，如 ['软件控制逻辑', '电芯电化学']"
    )

    # Prompt 配置
    system_prompt_extra: str = Field(
        default="",
        description="追加到基础 System Prompt 的角色专属指令"
    )
    llm_model_override: Optional[str] = Field(
        default=None,
        description="覆盖全局 LLM 模型（为空则使用全局配置）"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "name": "bms_hardware",
                "display_name": "BMS硬件专家",
                "role": "professional",
                "domain": "BMS 硬件电路设计",
                "responsibilities": [
                    "电压/温度采集回路分析",
                    "高压主板电路失效分析",
                    "绝缘检测硬件失效分析"
                ],
                "boundaries": [
                    "不介入BMS软件控制逻辑",
                    "不涉及电芯电化学特性"
                ],
            }
        }


class ProjectAgentConfig(BaseModel):
    """项目级 Agent 配置集合 — 对应 §2.5 中「专业 Agent 列表」配置项"""
    system_integration: AgentConfig = Field(
        default_factory=lambda: AgentConfig(
            name="system_integration",
            display_name="系统集成专家",
            role=AgentRole.SYSTEM_INTEGRATION,
            domain="全部专业领域统筹",
            responsibilities=[
                "汇总全部专业 Agent 输出，统一校验、合并、版本锁定",
                "统一对齐输入文件，固化项目全流程分析基线",
                "识别信息缺口，统一下发资料补充要求",
                "全程主导七步法流程，逐阶段质量把关",
                "统一协调跨专业冲突",
                "统一定义并下发 S/O/D 打分标准",
                "逐阶段签发放行许可",
            ],
            boundaries=["不直接开展具体专业的失效分析（由各专业 Agent 承担）"],
        ),
        description="系统集成专家配置（每个项目有且仅有一个）"
    )
    professional_agents: List[AgentConfig] = Field(
        default_factory=list,
        description="支撑专业 Agent 列表（数量、领域按项目配置）"
    )

    def get_all_agents(self) -> List[AgentConfig]:
        """返回全部 Agent 配置（含系统集成专家）"""
        return [self.system_integration] + self.professional_agents

    def get_agent_by_name(self, name: str) -> Optional[AgentConfig]:
        """按名称查找 Agent 配置"""
        for agent in self.get_all_agents():
            if agent.name == name:
                return agent
        return None


# ─── HEV 电池包案例预设 ───

HEV_PROFESSIONAL_AGENTS: List[AgentConfig] = [
    AgentConfig(
        name="bms_requirement",
        display_name="BMS需求专家",
        domain="整车交互需求、高压功能需求、功能安全指标、客户性能指标拆解",
        responsibilities=["需求定义分析", "性能指标拆解", "功能安全需求识别"],
        boundaries=["不介入BMS软件实现", "不涉及硬件电路设计"],
    ),
    AgentConfig(
        name="bms_software",
        display_name="BMS软件专家",
        domain="BMS 均衡算法、热管理控制逻辑、故障诊断策略、整车通讯交互、控制时序",
        responsibilities=["软件策略分析", "控制逻辑失效分析", "通讯交互失效分析"],
        boundaries=["不涉及硬件电路", "不介入电芯电化学"],
    ),
    AgentConfig(
        name="bms_hardware",
        display_name="BMS硬件专家",
        domain="电压/温度采集回路、高压主板、电源电路、绝缘检测硬件、板端接插件",
        responsibilities=["元器件选型分析", "电路拓扑失效分析", "硬件接口失效分析"],
        boundaries=["不介入软件控制逻辑", "不涉及电芯本体"],
    ),
    AgentConfig(
        name="cell",
        display_name="电芯专家",
        domain="电芯电化学设计、析锂、内部产气、极片内短路、热失控机理、循环衰减",
        responsibilities=["电芯本体失效分析", "热失控机理分析", "循环寿命分析"],
        boundaries=["不涉及BMS控制", "不涉及Pack结构"],
    ),
    AgentConfig(
        name="structure",
        display_name="结构专家",
        domain="Pack 箱体、模组端侧板、密封结构、防爆阀、液冷流道、紧固件",
        responsibilities=["结构强度分析", "密封防水分析", "振动冲击分析"],
        boundaries=["不涉及电气回路", "不涉及电芯化学"],
    ),
    AgentConfig(
        name="electrical",
        display_name="电气专家",
        domain="高压铜铝排、高低压线束、连接器、熔断器、主继电器、等电位设计、高压绝缘",
        responsibilities=["高压回路分析", "绝缘设计分析", "连接器选型分析"],
        boundaries=["不涉及结构力学", "不涉及软件控制"],
    ),
    AgentConfig(
        name="regulation",
        display_name="法规专家",
        domain="七大专业维度法规检索、标准条款原文解读、强制条款与产品风险点匹配映射",
        responsibilities=["法规检索", "条款解读", "合规映射"],
        boundaries=["仅负责法规解读与映射，不参与工程失效分析"],
    ),
]
