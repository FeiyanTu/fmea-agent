"""
项目配置模型 — 对应需求规格 §2.5

项目初始化时通过 YAML/JSON 加载，定义：
- 产品类型、开发阶段
- Agent 列表
- 法规清单
- 典型工况
- 高风险连锁主题
- SOD 打分标准
- DFMEA 模板
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from models.agent_config import AgentConfig, ProjectAgentConfig

logger = logging.getLogger(__name__)


@dataclass
class ProjectConfig:
    """项目配置 — 对应 §2.5 全部配置项"""

    # ── 基本信息 ──
    project_name: str = ""
    product_type: str = ""                    # 如 "HEV 混动动力电池包"
    development_phase: str = "DV"             # DV / PV / 其他

    # ── 产品结构 ──
    product_hierarchy: List[str] = field(default_factory=lambda: [
        "总成", "子系统", "子零件", "下级子零件"
    ])

    # ── Agent 配置 ──
    agent_config: ProjectAgentConfig = field(default_factory=ProjectAgentConfig)

    # ── 法规 & 标准 ──
    regulation_list: List[str] = field(default_factory=list)

    # ── 分析边界 ──
    boundary_conditions: List[str] = field(default_factory=list)  # 典型工况
    analysis_exclusions: List[str] = field(default_factory=lambda: [
        "生产过程装配不良类失效",
        "来料零部件质量缺陷类失效",
        "客户不当使用类失效",
    ])

    # ── 高风险主题 ──
    high_risk_themes: List[Dict[str, Any]] = field(default_factory=list)
    # 每项: {"theme": "热失控蔓延", "involved_agents": ["cell", "structure", "bms_software"]}

    # ── 打分标准 ──
    sod_criteria: Dict[str, Any] = field(default_factory=dict)
    # {"s": {...}, "o": {...}, "d": {...}, "ap": {...}}

    # ── 模板 ──
    dfmea_template: str = "aiag_vda_2021"     # DFMEA 模板标识

    # ── 输入文件 ──
    input_files: Dict[str, str] = field(default_factory=dict)
    # {"sor": "path/to/sor.pdf", "bom": "path/to/bom.xlsx", ...}

    # ════════════════════════════════════════════════════════════
    # 序列化
    # ════════════════════════════════════════════════════════════

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "project_name": self.project_name,
            "product_type": self.product_type,
            "development_phase": self.development_phase,
            "product_hierarchy": self.product_hierarchy,
            "agents": [a.model_dump() for a in self.agent_config.professional_agents],
            "system_integration": self.agent_config.system_integration.model_dump(),
            "regulation_list": self.regulation_list,
            "boundary_conditions": self.boundary_conditions,
            "analysis_exclusions": self.analysis_exclusions,
            "high_risk_themes": self.high_risk_themes,
            "sod_criteria": self.sod_criteria,
            "dfmea_template": self.dfmea_template,
        }

    def save_json(self, path: str):
        """保存为 JSON 文件"""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    # ════════════════════════════════════════════════════════════
    # 工厂方法
    # ════════════════════════════════════════════════════════════

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectConfig":
        """从字典加载"""
        agents_data = data.get("agents", [])
        if agents_data:
            agent_config = ProjectAgentConfig(
                professional_agents=[AgentConfig(**a) for a in agents_data]
            )
        else:
            agent_config = ProjectAgentConfig()

        return cls(
            project_name=data.get("project_name", ""),
            product_type=data.get("product_type", ""),
            development_phase=data.get("development_phase", "DV"),
            product_hierarchy=data.get("product_hierarchy",
                ["总成", "子系统", "子零件", "下级子零件"]),
            agent_config=agent_config,
            regulation_list=data.get("regulation_list", []),
            boundary_conditions=data.get("boundary_conditions", []),
            analysis_exclusions=data.get("analysis_exclusions",
                ["生产过程装配不良类失效", "来料零部件质量缺陷类失效", "客户不当使用类失效"]),
            high_risk_themes=data.get("high_risk_themes", []),
            sod_criteria=data.get("sod_criteria", {}),
            dfmea_template=data.get("dfmea_template", "aiag_vda_2021"),
        )

    @classmethod
    def from_json(cls, path: str) -> "ProjectConfig":
        """从 JSON 文件加载"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    @classmethod
    def from_yaml(cls, path: str) -> "ProjectConfig":
        """从 YAML 文件加载"""
        try:
            import yaml
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return cls.from_dict(data)
        except ImportError:
            logger.warning("PyYAML 未安装，尝试 JSON 格式")
            return cls.from_json(path)

    # ════════════════════════════════════════════════════════════
    # 校验
    # ════════════════════════════════════════════════════════════

    def validate(self) -> List[str]:
        """校验配置完整性，返回错误列表"""
        errors = []

        if not self.product_type:
            errors.append("product_type 不能为空")

        if len(self.agent_config.professional_agents) < 1:
            errors.append("至少需要 1 个专业 Agent")

        if not self.regulation_list:
            errors.append("regulation_list 为空，建议至少配置 1 个法规标准")

        if not self.boundary_conditions:
            errors.append("boundary_conditions 为空，建议至少配置 1 个典型工况")

        # 检查 Agent 名称唯一性
        names = [a.name for a in self.agent_config.professional_agents]
        if len(names) != len(set(names)):
            errors.append(f"专业 Agent 名称重复: {names}")

        return errors
