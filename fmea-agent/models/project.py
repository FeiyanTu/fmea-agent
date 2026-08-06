"""FMEA Project Model - maps to Java FmeaModels.FmeaProject"""
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class FmeaProject(BaseModel):
    """FMEA 项目定义"""
    id: str = Field(default_factory=lambda: f"proj-{datetime.now().strftime('%Y%m%d%H%M%S')}")
    name: str
    fmea_type: str = "DFMEA"  # DFMEA | PFMEA
    description: str = ""
    product_model: str = ""  # 产品型号
    part_number: str = ""    # 零件号
    customer_name: str = ""  # 顾客名称
    production_location: str = ""  # 生产地址
    team_members: List[str] = Field(default_factory=list)
    model_year_program: str = ""  # 年型/项目
    confidentiality_level: str = ""  # 保密级别

    # 步骤状态
    current_step: int = 0  # 0=未开始, 1-7=已完成步骤
    has_structure: bool = False
    has_function: bool = False
    has_failure: bool = False
    has_risk: bool = False
    has_optimization: bool = False
    has_report: bool = False

    # SOR/BOM 文件
    sor_files: List[str] = Field(default_factory=list)  # SOR文档路径列表
    bom_files: List[str] = Field(default_factory=list)  # BOM文件路径列表

    # v4.2 新增：项目配置字段 (§2.5)
    agent_configs: List[dict] = Field(default_factory=list)   # Agent 配置列表
    regulation_list: List[str] = Field(default_factory=list)  # 法规标准清单
    boundary_conditions: List[str] = Field(default_factory=list)  # 典型工况
    scoring_criteria: dict = Field(default_factory=dict)      # S/O/D 评分标准
    high_risk_themes: List[dict] = Field(default_factory=list) # 高风险连锁主题

    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    class Config:
        json_schema_extra = {
            "example": {
                "name": "变速箱总成 DFMEA",
                "fmea_type": "DFMEA",
                "product_model": "DCT-7DCT300",
                "part_number": "PN-2024-001",
                "team_members": ["张工", "李工", "王工"],
            }
        }
