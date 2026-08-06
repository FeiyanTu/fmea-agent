"""
FMEA 知识库模块

支持:
- SOR 文档知识注入 (via 本地文件系统 + Markdown 转换)
- 基于 LLM 的智能知识库查询
- React 范式多轮推理查询
- SOD 评分标准 (AIAG/VDA 默认)
- 行业典型失效模式参考（跨行业通用分类 + 可扩展）
- 历史项目知识复用
"""
import json
import logging
from pathlib import Path
from typing import Optional, Dict, List

from .file_converter import (
    convert_file_to_markdown,
    convert_directory_to_markdown,
    is_supported_file,
    SUPPORTED_EXTENSIONS,
)
from .kb_service import KnowledgeService, get_kb_service
# [已注释] kb_query（MiniMax 模型退化循环问题）
# from .kb_query import (
#     KnowledgeQueryEngine,
#     StreamingQueryEngine,
#     get_query_engine,
# )

logger = logging.getLogger(__name__)


class FmeaKnowledgeBase:
    """FMEA 领域知识库 — 跨行业通用，不绑定特定产品"""

    def __init__(self, storage_dir: str = "data/knowledge"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    # ── SOD 评分标准 ──

    def get_sod_standard(self, standard_name: str = "AIAG/VDA Default") -> dict:
        """获取 SOD 评分标准"""
        filepath = self.storage_dir / f"sod_{standard_name.replace('/', '_')}.json"
        if filepath.exists():
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        return self._get_default_sod()

    def save_sod_standard(self, standard_name: str, standard: dict):
        """保存自定义 SOD 标准（支持各行业定制）"""
        filepath = self.storage_dir / f"sod_{standard_name.replace('/', '_')}.json"
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(standard, f, ensure_ascii=False, indent=2)

    # ── 典型失效模式参考库 ──

    def get_typical_failure_modes(self, category: str) -> List[dict]:
        """
        获取某类组件/过程的典型失效模式参考。

        分类体系为跨行业通用，例如：
        - "能量存储" (电池/电容/飞轮)
        - "电子控制" (ECU/BMS/PLC/嵌入式)
        - "机械结构" (壳体/支架/紧固件)
        - "流体/密封" (密封圈/管路/阀门)
        - "连接/接口" (连接器/线束/接插件)
        - "旋转/传动" (轴承/齿轮/皮带)
        - "热管理" (散热器/风扇/热管)
        - "软件/固件" (嵌入式软件/控制算法)

        用户可随时通过 add_typical_failure() 扩展自己的领域知识。
        """
        filepath = self.storage_dir / "typical_failures.json"
        if filepath.exists():
            with open(filepath, "r", encoding="utf-8") as f:
                all_failures = json.load(f)
                return all_failures.get(category, [])

        builtin = self._get_builtin_failure_reference()
        return builtin.get(category, [])

    def add_typical_failure(self, category: str, mode: str, cause: str,
                            severity: int = 4, occurrence: int = 5, detection: int = 5):
        """向知识库追加一条典型失效模式（用户扩展领域知识）"""
        filepath = self.storage_dir / "typical_failures.json"
        if filepath.exists():
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}

        if category not in data:
            data[category] = []
        data[category].append({
            "mode": mode, "cause": cause,
            "severity": severity, "occurrence": occurrence, "detection": detection,
        })

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"Added typical failure to category '{category}': {mode}")

    def list_categories(self) -> List[str]:
        """列出所有已知的失效模式分类"""
        filepath = self.storage_dir / "typical_failures.json"
        if filepath.exists():
            with open(filepath, "r", encoding="utf-8") as f:
                return list(json.load(f).keys())
        return list(self._get_builtin_failure_reference().keys())

    # ── 历史项目知识 ──

    def save_project_report(self, project_id: str, report_summary: dict):
        """保存历史项目摘要，供后续项目参考"""
        filepath = self.storage_dir / "project_history.json"
        if filepath.exists():
            with open(filepath, "r", encoding="utf-8") as f:
                history = json.load(f)
        else:
            history = {}

        history[project_id] = report_summary
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2, default=str)

    def get_project_history(self, project_id: str = None) -> dict:
        """获取历史项目记录"""
        filepath = self.storage_dir / "project_history.json"
        if filepath.exists():
            with open(filepath, "r", encoding="utf-8") as f:
                history = json.load(f)
            if project_id:
                return history.get(project_id, {})
            return history
        return {}

    # ═══════════════════════════════════════════════════════════════
    # 内置默认值（跨行业通用）
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _get_default_sod() -> dict:
        """AIAG/VDA 默认 SOD 评分标准（行业通用）"""
        return {
            "name": "AIAG/VDA Default",
            "version": "1st Edition",
            "severity": {
                "10": "安全/法规风险 - 无预警",
                "9": "安全/法规风险 - 有预警",
                "8": "功能丧失",
                "7": "功能降级 - 影响主要功能",
                "6": "功能降级 - 影响舒适性/便利性",
                "5": "外观/噪音 - 影响客户满意度",
                "4": "外观/噪音 - 多数客户注意到",
                "3": "外观/噪音 - 部分客户注意到",
                "2": "几乎无影响",
                "1": "无影响",
            },
            "occurrence": {
                "10": "每件/频繁 (>100次/千件)",
                "9": "每批 (50次/千件)",
                "8": "每小时 (20次/千件)",
                "7": "每天 (10次/千件)",
                "6": "每月 (2次/千件)",
                "5": "每季度 (0.5次/千件)",
                "4": "每年 (0.1次/千件)",
                "3": "每3年 (0.01次/千件)",
                "2": "每10年 (<0.001次/千件)",
                "1": "从未发生/极不可能",
            },
            "detection": {
                "10": "无法探测",
                "9": "随机人工检查",
                "8": "人工检查",
                "7": "人工计量",
                "6": "自动检测(手动记录)",
                "5": "自动检测(自动记录)",
                "4": "自动检测(在线监控)",
                "3": "防错(过程后)",
                "2": "防错(过程中)",
                "1": "防错(设计上)",
            },
        }

    @staticmethod
    def _get_builtin_failure_reference() -> Dict[str, List[dict]]:
        """跨行业失效模式参考库。"""
        return {
            "能量存储": [
                {"mode": "能量密度不足", "cause": "材料性能退化/设计容量不足", "severity": 7},
                {"mode": "热失控", "cause": "内部短路/过充/机械挤压", "severity": 10},
                {"mode": "自放电过大", "cause": "内部微短路/隔膜缺陷", "severity": 5},
                {"mode": "寿命衰减过快", "cause": "循环老化/环境温度过高", "severity": 6},
            ],
            "电子控制": [
                {"mode": "控制逻辑错误", "cause": "软件Bug/状态机设计缺陷", "severity": 8},
                {"mode": "通信中断", "cause": "总线故障/EMC干扰/连接器松动", "severity": 7},
                {"mode": "保护功能失效", "cause": "检测电路故障/阈值设定不当", "severity": 9},
                {"mode": "复位/重启异常", "cause": "电源波动/看门狗超时", "severity": 6},
            ],
            "机械结构": [
                {"mode": "强度不足", "cause": "材料缺陷/设计裕度不足/疲劳", "severity": 8},
                {"mode": "变形超差", "cause": "载荷超出设计范围/残余应力", "severity": 6},
                {"mode": "腐蚀", "cause": "防护涂层失效/环境腐蚀介质", "severity": 5},
                {"mode": "振动断裂", "cause": "共振/疲劳/应力集中", "severity": 8},
            ],
            "连接与接口": [
                {"mode": "接触不良", "cause": "端子氧化/插拔磨损/微动腐蚀", "severity": 6},
                {"mode": "短路", "cause": "绝缘破损/异物侵入/爬电距离不足", "severity": 9},
                {"mode": "过热", "cause": "接触电阻过大/过载/散热不足", "severity": 7},
                {"mode": "信号失真", "cause": "阻抗不匹配/串扰/接地不良", "severity": 6},
            ],
            "流体与密封": [
                {"mode": "泄漏", "cause": "密封圈老化/安装不当/压力超限", "severity": 7},
                {"mode": "堵塞", "cause": "杂质积聚/腐蚀产物/结垢", "severity": 6},
                {"mode": "流量不足", "cause": "管路压损过大/泵性能衰减", "severity": 5},
            ],
            "旋转与传动": [
                {"mode": "磨损过量", "cause": "润滑不足/载荷过大/对中不良", "severity": 6},
                {"mode": "卡死", "cause": "异物侵入/润滑失效/热膨胀", "severity": 8},
                {"mode": "噪音超标", "cause": "齿轮啮合不良/轴承点蚀", "severity": 4},
            ],
            "热管理": [
                {"mode": "散热能力不足", "cause": "翅片堵塞/风扇转速下降/热阻增大", "severity": 7},
                {"mode": "冷却液泄漏", "cause": "管路接头松动/密封老化", "severity": 7},
                {"mode": "温度传感器漂移", "cause": "传感器老化/安装位置不当", "severity": 6},
            ],
            "软件与固件": [
                {"mode": "逻辑缺陷", "cause": "边界条件未覆盖/状态机遗漏", "severity": 8},
                {"mode": "时序错误", "cause": "竞态条件/中断嵌套/优先级反转", "severity": 8},
                {"mode": "内存溢出", "cause": "动态分配未释放/栈溢出", "severity": 7},
                {"mode": "配置参数错误", "cause": "标定数据异常/EEPROM损坏", "severity": 6},
            ],
        }


# 全局单例
_kb: Optional[FmeaKnowledgeBase] = None


def get_knowledge_base() -> FmeaKnowledgeBase:
    global _kb
    if _kb is None:
        _kb = FmeaKnowledgeBase()
    return _kb
