"""
迁移脚本：将 agent.html 中硬编码的 AGENTS 数据写入 project_agents 表

用法：
    python scripts/migrate_agents_to_db.py [project_id]

默认写入第一个项目的 project_id。如果表里已有数据则跳过。
"""
import json
import os
import sys
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# ── 项目根目录 ──
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent

# 加载 .env 环境变量
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=True)

import psycopg2
import psycopg2.extras

DB_HOST = os.getenv("URL_AGENT_DB_HOST", "10.180.17.11")
DB_PORT = int(os.getenv("URL_AGENT_DB_PORT", "5433"))
DB_NAME = os.getenv("URL_AGENT_DB_NAME", "fmea")
DB_USER = os.getenv("URL_AGENT_DB_USER", "postgres")
DB_PASS = os.getenv("URL_AGENT_DB_PASSWORD", "postgres123!@#")


# ── AGENTS 数据（从 agent.html 的 JS 对象中提取） ──

AGENTS_DATA = [
    # ── Tier 1 ──
    {
        "tier": 1, "sort_order": 1,
        "name": "系统集成专家", "name_en": "System Integration Expert",
        "icon": "🎯", "color": "#1e1b4b", "tier_label": "总负责人",
        "domain": "HEV 电池包全系统集成 · 7 步法全流程",
        "status": "active", "status_label": "已就绪",
        "description": "多 Agent 协作架构的总负责人。负责基线锁定、S/O/D 打分统一下发、跨专业冲突仲裁、阶段门控放行。所有专业 Agent 的输出汇总至本角色统一校验合并。",
        "responsibilities": [
            "汇总全部专业 Agent 输出，统一校验、合并、版本锁定",
            "统一对齐 SOR / 图纸 / BOM / 法规解读文件，固化项目基线",
            "识别资料缺口，统一下发补充要求至对应专业 Agent",
            "全程主导 AIAG-VDA FMEA 2021 七步法，逐阶段质量把关",
            "统一定义 S/O/D 打分标准，禁止各专业自定义",
            "逐阶段签发门控放行许可",
        ],
        "boundaries": [
            "不直接开展具体专业的失效分析",
            "不替代专业 Agent 的领域判断",
        ],
        "tools": ["review_and_merge", "resolve_conflict", "issue_scoring_standard", "gate_check", "baseline_lock"],
        "source_file": "agents/system_integration.py",
        "weknora_kb_id": "",
    },
    # ── Tier 2 ──
    {
        "tier": 2, "sort_order": 1,
        "name": "工作流编排 Agent", "name_en": "Workflow Orchestrator Agent",
        "icon": "⏱", "color": "#1e3a8a", "tier_label": "工作流编排",
        "domain": "阶段 2-6 组员审批确认 · 流程自动化",
        "status": "active", "status_label": "已就绪",
        "description": "负责 DFMEA 阶段 2-6 的组员确认流程：阶段完成后发起审批 → 定时检查确认状态 → 对未确认成员推送提醒 → 全员确认后自动唤醒下一阶段引擎。",
        "responsibilities": [
            "阶段完成后自动创建审批请求",
            "通知全部项目成员审核确认",
            "定时检查审批状态（默认 5 分钟）",
            "对超时未确认成员推送提醒（10 分钟冷却）",
            "全员确认后自动回调唤醒下一阶段",
            "驳回时暂停流程并通知全体成员",
        ],
        "boundaries": [
            "不参与具体 DFMEA 分析",
            "不替代门控校验",
            "不发送与 FMEA 无关的通知",
        ],
        "tools": ["request_approval", "approve_stage", "reject_stage", "check_and_notify", "auto_proceed"],
        "source_file": "orchestrator/workflow_orchestrator.py",
        "weknora_kb_id": "",
    },
    # ── Tier 3 ──
    {
        "tier": 3, "sort_order": 1,
        "name": "法规专家", "name_en": "Regulation Agent",
        "icon": "📚", "color": "#dc2626", "tier_label": "法规",
        "domain": "法规标准检索与解读 · 7 大设计维度",
        "status": "active", "status_label": "已就绪",
        "description": "从 WeKnora 知识库检索 GB38031 / R100 / UN38.3 / GB/T 4208 / GB/T 34590 等法规条款，LLM 解读并映射到产品功能/风险，输出文件 1（法规条款映射清单）。",
        "responsibilities": [
            "覆盖七大设计维度完成法规检索",
            "标准条款原文解读（禁止概括、删减）",
            "强制条款与产品风险点匹配映射",
            "输出标准化文件 1 法规条款映射清单",
            "后续阶段做合规性校验",
        ],
        "boundaries": [
            "仅负责法规解读与映射",
            "不参与工程失效分析",
            "不介入具体设计变更方案",
        ],
        "tools": ["hybrid_search", "preview_knowledge", "build_document_1", "compliance_check"],
        "source_file": "agents/regulation.py",
        "weknora_kb_id": "",
    },
    {
        "tier": 3, "sort_order": 2,
        "name": "BMS 需求专家", "name_en": "BMS Requirement Agent",
        "icon": "🔌", "color": "#2563eb", "tier_label": "BMS 需求",
        "domain": "整车交互需求 · 高压功能需求 · 功能安全指标",
        "status": "active", "status_label": "已就绪",
        "description": "负责整车交互需求、高压功能需求、功能安全指标、客户性能指标拆解，从 WeKnora 检索需求类文档并识别需求定义类设计失效。",
        "responsibilities": [
            "需求定义类设计失效识别",
            "性能指标拆解与功能安全需求识别",
            "客户 SOR 量化指标解读",
        ],
        "boundaries": [
            "不介入 BMS 软件实现",
            "不涉及硬件电路设计",
        ],
        "tools": ["hybrid_search", "requirement_parse", "sor_extract"],
        "source_file": "agents/_professional.py  → bms_requirement",
        "weknora_kb_id": "",
    },
    {
        "tier": 3, "sort_order": 3,
        "name": "BMS 软件专家", "name_en": "BMS Software Agent",
        "icon": "💻", "color": "#7c3aed", "tier_label": "BMS 软件",
        "domain": "均衡算法 · 热管理控制 · 故障诊断策略 · 控制时序",
        "status": "active", "status_label": "已就绪",
        "description": "负责 BMS 均衡算法、热管理控制逻辑、故障诊断策略、整车通讯交互、控制时序设计失效分析。",
        "responsibilities": [
            "软件策略失效分析",
            "控制逻辑失效识别",
            "通讯交互失效分析",
        ],
        "boundaries": [
            "不涉及硬件电路",
            "不介入电芯电化学",
        ],
        "tools": ["hybrid_search", "llm_analyze", "logic_trace"],
        "source_file": "agents/_professional.py  → bms_software",
        "weknora_kb_id": "",
    },
    {
        "tier": 3, "sort_order": 4,
        "name": "BMS 硬件专家", "name_en": "BMS Hardware Agent",
        "icon": "⚡", "color": "#0d9488", "tier_label": "BMS 硬件",
        "domain": "采集回路 · 高压主板 · 电源电路 · 绝缘检测硬件",
        "status": "active", "status_label": "已就绪",
        "description": "负责电压/温度采集回路、高压主板、电源电路、绝缘检测硬件、板端接插件电路的元器件选型与电路拓扑失效分析。",
        "responsibilities": [
            "元器件选型失效分析",
            "电路拓扑失效识别",
            "硬件接口失效分析",
        ],
        "boundaries": [
            "不介入软件控制逻辑",
            "不涉及电芯本体",
        ],
        "tools": ["hybrid_search", "llm_analyze", "circuit_fault"],
        "source_file": "agents/_professional.py  → bms_hardware",
        "weknora_kb_id": "",
    },
    {
        "tier": 3, "sort_order": 5,
        "name": "电芯专家", "name_en": "Cell Agent",
        "icon": "🔋", "color": "#f59e0b", "tier_label": "电芯",
        "domain": "电芯电化学 · 析锂 · 产气 · 极片内短路 · 热失控",
        "status": "active", "status_label": "已就绪",
        "description": "负责电芯电化学设计特性、析锂、内部产气、极片内短路、热失控机理、循环衰减等电芯本体失效分析。",
        "responsibilities": [
            "电芯本体失效分析",
            "热失控机理分析",
            "循环寿命分析",
        ],
        "boundaries": [
            "不涉及 BMS 控制",
            "不涉及 Pack 结构",
        ],
        "tools": ["hybrid_search", "llm_analyze", "thermal_model"],
        "source_file": "agents/_professional.py  → cell",
        "weknora_kb_id": "",
    },
    {
        "tier": 3, "sort_order": 6,
        "name": "结构专家", "name_en": "Structure Agent",
        "icon": "🏗", "color": "#475569", "tier_label": "结构",
        "domain": "Pack 箱体 · 模组端侧板 · 密封 · 防爆阀 · 液冷流道",
        "status": "active", "status_label": "已就绪",
        "description": "负责 Pack 箱体、模组端侧板、密封结构、防爆阀、液冷冷却流道、紧固件的结构强度 / 密封防水 / 振动冲击失效分析。",
        "responsibilities": [
            "结构强度分析",
            "密封防水分析",
            "振动冲击分析",
        ],
        "boundaries": [
            "不涉及电气回路",
            "不涉及电芯化学",
        ],
        "tools": ["hybrid_search", "llm_analyze", "cad_reference"],
        "source_file": "agents/_professional.py  → structure",
        "weknora_kb_id": "",
    },
    {
        "tier": 3, "sort_order": 7,
        "name": "电气专家", "name_en": "Electrical Agent",
        "icon": "⚙", "color": "#ef4444", "tier_label": "电气",
        "domain": "高压铜铝排 · 高低压线束 · 连接器 · 熔断器 · 绝缘回路",
        "status": "active", "status_label": "已就绪",
        "description": "负责高压铜铝排、高低压线束、连接器、熔断器、主继电器、等电位设计、高压绝缘回路的高压回路失效分析。",
        "responsibilities": [
            "高压回路分析",
            "绝缘设计分析",
            "连接器选型分析",
        ],
        "boundaries": [
            "不涉及结构力学",
            "不涉及软件控制",
        ],
        "tools": ["hybrid_search", "llm_analyze", "insulation_check"],
        "source_file": "agents/_professional.py  → electrical",
        "weknora_kb_id": "",
    },
]


def get_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS,
    )


def migrate():
    conn = get_connection()
    try:
        cur = conn.cursor()

        # 先创建表（如果不存在）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                id                SERIAL PRIMARY KEY,
                tier              INTEGER NOT NULL,
                name              TEXT UNIQUE NOT NULL,
                name_en           TEXT NOT NULL DEFAULT '',
                icon              TEXT NOT NULL DEFAULT '',
                color             TEXT NOT NULL DEFAULT '#6366f1',
                tier_label        TEXT NOT NULL DEFAULT '',
                domain            TEXT NOT NULL DEFAULT '',
                status            TEXT NOT NULL DEFAULT 'active',
                status_label      TEXT NOT NULL DEFAULT '',
                description       TEXT NOT NULL DEFAULT '',
                responsibilities  JSONB NOT NULL DEFAULT '[]'::jsonb,
                boundaries        JSONB NOT NULL DEFAULT '[]'::jsonb,
                tools             JSONB NOT NULL DEFAULT '[]'::jsonb,
                source_file       TEXT NOT NULL DEFAULT '',
                weknora_kb_id     TEXT NOT NULL DEFAULT '',
                sort_order        INTEGER NOT NULL DEFAULT 0,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.commit()

        # 建索引
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_agents_tier
            ON agents (tier)
        """)
        conn.commit()

        # 检查是否已有数据
        cur.execute("SELECT COUNT(*) FROM agents")
        count = cur.fetchone()[0]
        if count > 0:
            logger.info(f"⚠  agents 表已有 {count} 条数据，跳过迁移")
            return

        inserted = 0
        for agent in AGENTS_DATA:
            cur.execute("""
                INSERT INTO agents
                    (tier, name, name_en, icon, color, tier_label,
                     domain, status, status_label, description,
                     responsibilities, boundaries, tools,
                     source_file, weknora_kb_id, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s)
            """, (
                agent["tier"],
                agent["name"],
                agent["name_en"],
                agent["icon"],
                agent["color"],
                agent["tier_label"],
                agent["domain"],
                agent["status"],
                agent["status_label"],
                agent["description"],
                json.dumps(agent["responsibilities"], ensure_ascii=False),
                json.dumps(agent["boundaries"], ensure_ascii=False),
                json.dumps(agent["tools"], ensure_ascii=False),
                agent["source_file"],
                agent["weknora_kb_id"],
                agent["sort_order"],
            ))
            inserted += 1

        conn.commit()
        logger.info(f"✅ 成功写入 {inserted} 个 Agent 到 agents")
        logger.info("   Agent 列表：")
        for a in AGENTS_DATA:
            logger.info(f"    - [Tier {a['tier']}] {a['name']} ({a['tier_label']})")

    except Exception as e:
        conn.rollback()
        logger.error(f"❌ 迁移失败: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    migrate()
