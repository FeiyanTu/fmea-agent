"""
FMEA Agent 服务入口
启动: python main.py
环境变量配置见 .env 文件
"""
import sys
import os
import re
import json
import shutil
import asyncio
import datetime
import logging
import smtplib
import ssl
import socket
import bcrypt
import contextlib
from email.mime.text import MIMEText
from email.header import Header
from pathlib import Path
from typing import Optional, Union

# ── 项目根目录 ──
PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

# ── 加载 .env 文件 ──
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=True)

# ── 配置日志（同时输出到控制台和文件）──
LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO"))
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "fmea-agent.log"

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)
logger = logging.getLogger("fmea.server")
logger.info(f"日志文件: {LOG_FILE}")

# ════════════════════════════════════════════════════════════════
# 自动发现 Agno 框架路径
# ════════════════════════════════════════════════════════════════

def _find_agno_path() -> Path:
    """自动查找 agno 框架路径"""
    candidates = [
        PROJECT_ROOT.parent / "agno" / "libs" / "agno",
        Path(os.path.expanduser("~")) / "agent-fmea" / "agno" / "libs" / "agno",
    ]
    for p in candidates:
        if (p / "agno" / "agent").exists():
            return p
    try:
        import agno
        return Path(agno.__file__).parent.parent
    except ImportError:
        pass
    return candidates[0]

# ════════════════════════════════════════════════════════════════
# 配置（全部从环境变量读取）
# ════════════════════════════════════════════════════════════════

AGNO_PATH = Path(os.getenv("AGNO_PATH", str(_find_agno_path())))

MODEL_BASE_URL = os.getenv("FMEA_MODEL_BASE_URL", "https://api.deepseek.com")
MODEL_API_KEY = os.getenv("FMEA_MODEL_API_KEY", os.getenv("DEEPSEEK_API_KEY", ""))
MODEL_NAME = os.getenv("FMEA_MODEL_NAME", "deepseek-chat")

DATA_DIR = Path(os.getenv("FMEA_DATA_DIR", str(PROJECT_ROOT / "data" / "projects")))
KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"  # 知识库根目录

DB_URL = os.getenv("FMEA_DB_URL", "")

SERVER_HOST = os.getenv("FMEA_HOST", "localhost")
SERVER_PORT = int(os.getenv("FMEA_PORT", "7777"))

# ════════════════════════════════════════════════════════════════
# 启动前配置校验 — 必须项缺失则拒绝启动
# ════════════════════════════════════════════════════════════════

def validate_config():
    """
    校验启动必需配置，任何缺失直接 sys.exit(1)。
    提示用户查阅 .env.example 补全配置。
    """
    errors = []

    # 1. LLM API Key — 必须
    if not MODEL_API_KEY:
        errors.append(
            "FMEA_MODEL_API_KEY 未设置\n"
            "  → 请设置环境变量 FMEA_MODEL_API_KEY 或 DEEPSEEK_API_KEY\n"
            "  → 示例: set FMEA_MODEL_API_KEY=sk-your-key-here"
        )

    # 2. LLM Model Name — 必须
    if not MODEL_NAME:
        errors.append(
            "FMEA_MODEL_NAME 未设置\n"
            "  → 请设置环境变量 FMEA_MODEL_NAME\n"
            "  → 示例: set FMEA_MODEL_NAME=deepseek-chat"
        )

    # 3. LLM Base URL — 必须（有默认值但做合理性检查）
    if not MODEL_BASE_URL:
        errors.append(
            "FMEA_MODEL_BASE_URL 未设置\n"
            "  → 请设置环境变量 FMEA_MODEL_BASE_URL\n"
            "  → 示例: set FMEA_MODEL_BASE_URL=https://api.deepseek.com"
        )

    # 4. Agno 框架路径 — 必须存在
    if not (AGNO_PATH / "agno" / "agent").exists():
        errors.append(
            f"Agno 框架未找到\n"
            f"  → 检测路径: {AGNO_PATH}\n"
            f"  → 请设置 AGNO_PATH 环境变量指向 agno/libs/agno 目录\n"
            f"  → 示例: set AGNO_PATH=D:/agent-fmea/agno/libs/agno"
        )

    # 5. 数据库 — PostgreSQL 连接串必须填写
    if not DB_URL:
        errors.append(
            "FMEA_DB_URL 未设置\n"
            "  → 请设置 PostgreSQL 连接串\n"
            "  → 示例: FMEA_DB_URL=postgresql+psycopg2://user:pass@host:5432/dbname"
        )
    elif "://" not in DB_URL or "@" not in DB_URL:
        logger.warning(f"FMEA_DB_URL 格式可能不正确: {DB_URL[:30]}...")
    else:
        logger.info(f"数据库: PostgreSQL ({DB_URL.split('@')[1] if '@' in DB_URL else DB_URL})")

    if errors:
        print("\n" + "=" * 60)
        print("  ❌ FMEA Agent 启动失败 — 缺少必需配置")
        print("=" * 60)
        for i, err in enumerate(errors, 1):
            print(f"\n  [{i}] {err}")
        print("\n" + "-" * 60)
        print("  请参考 .env.example 文件补全以下环境变量：")
        print("    FMEA_MODEL_API_KEY   (必需)")
        print("    FMEA_MODEL_NAME      (必需)")
        print("    FMEA_MODEL_BASE_URL  (必需)")
        print("    AGNO_PATH            (必需)")
        print("    FMEA_DB_URL          (必需，PostgreSQL 连接串)")
        print("=" * 60 + "\n")
        sys.exit(1)

    logger.info("✅ 配置校验通过")

# 执行校验
validate_config()

# ── 校验通过后才导入 Agno 框架 ──
sys.path.insert(0, str(AGNO_PATH))

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.os import AgentOS
from agno.os.interfaces.agui import AGUI

# ⚠️ 已禁用 agno 所有内置工具（Calculator/DuckDuckGo/Python/File）
# 仅使用下方自定义工具，避免 minimax 等国产模型不兼容 agno 工具 schema

# ── MiniMax / 国产模型兼容层 ──
# minimax 返回的 tool_calls 缺少 type="function" 字段，OpenAI 客户端校验会失败
# 此补丁在 pydantic 校验前自动补全缺失字段
import re
import openai._models

_original_validate_type = openai._models.validate_type

def _patched_validate_type(*, type_, value):
    """在 Pydantic 校验前修复 tool_calls 中缺失的 type 字段"""
    _fix_tool_calls_data(value)
    return _original_validate_type(type_=type_, value=value)

def _fix_tool_calls_data(obj):
    """递归修复 tool_calls 对象中缺少的 type 字段（minimax 返回 None 而非 'function'）"""
    if isinstance(obj, dict):
        # 修复 tool_call 缺少 type 的问题
        if "function" in obj and "id" in obj and ("type" not in obj or obj.get("type") is None):
            obj["type"] = "function"
        if "tool_calls" in obj:
            tcs = obj["tool_calls"]
            if isinstance(tcs, list):
                for tc in tcs:
                    if isinstance(tc, dict):
                        _fix_tool_calls_data(tc)
        for v in obj.values():
            _fix_tool_calls_data(v)
    elif isinstance(obj, list):
        for item in obj:
            _fix_tool_calls_data(item)

openai._models.validate_type = _patched_validate_type

# ── 导入 FMEA Pipeline ──
from workflow.fmea_pipeline import FmeaPipeline, get_pipeline

# ── 导入知识库工具 ──
from knowledge.kb_service import get_kb_service
# [已注释] kb_query（MiniMax 模型退化循环问题）
# from knowledge.kb_query import get_query_engine, StreamingQueryEngine
from engine.tools.project_init_tools import (
    init_project_tool,
    get_init_progress_tool,
    build_knowledge_base_tool,
    # [已注释] build_structure_tree_tool（依赖 kb_query）
    extract_structure_tree_tool,
    extract_structure_tree_from_weknora_tool,
    _set_tool_progress,
    # [已注释] query_kb_tool（依赖 kb_query）
    get_kb_status_tool,
    list_kb_pages_tool,
)

# ── 风险评估工具（文件驱动的 SOD/AP 计算）──
from engine.tools.risk_tools import (
    register_risk_criteria_tool,
    run_risk_analysis_tool,
    get_risk_status_tool,
)

# ── 优化分析工具（LLM生成措施 + 随机分配责任人）──
from engine.tools.optimization_tools import (
    run_optimization_tool,
    get_optimization_status_tool,
)

# ── 报告生成工具 ──
from engine.tools.report_tools import (
    generate_report_tool,
)
from engine.tools.notify_tools import (
    push_structure_to_engineers,
    list_assigned_nodes,
)

# ── LLM 数据查询工具 ──
from engine.tools.query_tools import query_project_data_tool

# ════════════════════════════════════════════════════════════════
# LLM 风险判断 + 命令执行（移植自 agno/server.py）
# ════════════════════════════════════════════════════════════════

import subprocess, time
import httpx

# ── 命令执行（已移除 HITL 审批，所有操作自动放行）──
_pending_approvals: dict = {}

def execute_command(command: str) -> str:
    """在 Windows 系统上执行 CMD 或 PowerShell 命令，并返回结果。
    使用此工具可以: 读取文件、查看目录、搜索文件、写入文件、删除文件、创建目录、读取系统信息、执行脚本等。"""
    return _do_execute(command)

def execute_approved(approval_id: str) -> str:
    """（已弃用）所有操作已自动放行，此工具仅保留以便向后兼容"""
    return "所有操作已自动放行，无需审批。"

def cancel_approval(approval_id: str) -> str:
    """（已弃用）所有操作已自动放行，此工具仅保留以便向后兼容"""
    return "所有操作已自动放行，无需取消。"

def _do_execute(command: str) -> str:
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True,
                                timeout=60, cwd=str(PROJECT_ROOT))
        output = (result.stdout or "").strip()
        if (result.stderr or "").strip():
            stderr_text = (result.stderr or "").strip()
            output = output + "\n\n[stderr]\n" + stderr_text if output else stderr_text
        if not output: output = "(命令执行成功，无输出)"
        return output[:8000] + (f"\n\n... (截断，共{len(output)}字符)" if len(output) > 8000 else "")
    except subprocess.TimeoutExpired: return "[超时] 命令执行超过 60 秒"
    except Exception as e: return f"[错误] {str(e)}"

# ── 文件读取（移植自 agno/server.py）──
def read_file(file_path: str, file_paths: Optional[list] = None) -> str:
    """读取本地文件内容。支持 PDF、DOCX、XLSX、TXT 等格式。
    参数:
        file_path: 文件完整路径（单个文件）
        file_paths: 文件路径列表（批量读取多个文件，可选）
    返回文件内容文本（最多 8000 字符/文件）"""
    from pathlib import Path

    paths = file_paths if file_paths else [file_path]

    results = []
    for fp in paths:
        path = Path(fp)
        if not path.exists():
            results.append(f"[错误] 文件不存在: {fp}")
            continue
        ext = path.suffix.lower(); text = ""
        try:
            if ext == ".pdf":
                from pypdf import PdfReader
                reader = PdfReader(str(path))
                pages = [p.extract_text() for p in reader.pages[:50]]
                text = "\n\n".join(p for p in pages if p)
                if not text.strip(): text = "(PDF 无文字内容或为扫描件)"
            elif ext == ".docx":
                from docx import Document
                doc = Document(str(path))
                text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
            elif ext in (".xlsx", ".xls"):
                import openpyxl
                wb = openpyxl.load_workbook(str(path), data_only=True)
                sheets = []
                for name in wb.sheetnames:
                    ws = wb[name]; rows = []
                    for row in ws.iter_rows(values_only=True):
                        rows.append(" | ".join(str(c) if c is not None else "" for c in row))
                    sheets.append(f"### Sheet: {name}\n" + "\n".join(rows))
                text = "\n\n".join(sheets)
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            text = f"[错误] 读取失败: {str(e)}"
        if len(text) > 8000: text = text[:8000] + f"\n\n... (截断，共{len(text)}字符)"
        text = text or "(无文字内容)"
        results.append(f"=== {path.name} ===\n{text}")

    return "\n\n".join(results)

# ── 工具实例化 ──
# ⚠️ 已禁用 agno 内置工具（CalculatorTools/DuckDuckGo/PythonTools/FileTools）
# 仅使用自定义工具，避免 minimax 不兼容 agno 工具 schema 的问题

# ════════════════════════════════════════════════════════════════
# FMEA Pipeline 工具（FMEA 特化扩展）
# ════════════════════════════════════════════════════════════════

_pipeline_instance = None

def get_or_create_pipeline() -> FmeaPipeline:
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = get_pipeline(str(DATA_DIR))
    return _pipeline_instance

def create_project_tool(name: str, fmea_type: str = "DFMEA", description: str = "",
                        part_number: str = "", team_members: str = "") -> str:
    """创建FMEA项目（v18: 纯 DB，不写本地文件）。参数: name(项目名称), fmea_type(DFMEA/PFMEA), description, part_number, team_members(逗号分隔)
    如果同名项目已存在，自动复用，不会创建重复项目。"""
    import re as _re
    safe_id = _re.sub(r'[^\w\u4e00-\u9fff\-]', '_', name).strip('_') or "project"

    # 检查 DB 是否已有同名项目，有则复用（v18: 不再看本地目录）
    try:
        conn = get_user_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, description, current_step FROM projects WHERE name = %s", (safe_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row:
            db_pid = int(row[0])
            project = {
                "id": safe_id, "db_id": db_pid, "name": name,
                "fmea_type": fmea_type,
                "description": row[1] or description,
                "part_number": part_number,
                "core_object": name,
                "current_step": row[2] or 0,
                "team_members": [t.strip() for t in team_members.split(",") if t.strip()],
            }
            get_or_create_pipeline().project = project
            logger.info(f"♻️ 复用已有项目: {safe_id} (name={name}, db_id={db_pid})")
            return json.dumps({"status": "reused", "project": project, "note": "项目已存在，自动复用"}, ensure_ascii=False)
    except Exception:
        pass  # DB 查询失败 → 继续尝试创建

    # 创建新项目：只写 DB，不建目录、不写 planning.json
    db_project_id = None
    try:
        conn = get_user_db_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO projects (name, description) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING RETURNING id",
            (safe_id, description)
        )
        result = cur.fetchone()
        if result:
            db_project_id = int(result[0])
        else:
            # 已存在（并发）：查回 id
            cur.execute("SELECT id FROM projects WHERE name = %s", (safe_id,))
            row2 = cur.fetchone()
            if row2:
                db_project_id = int(row2[0])
        conn.commit()
        cur.close(); conn.close()
        logger.info(f"[create_project_tool] projects 表已写入: {safe_id} (db_id={db_project_id})")
    except Exception as e:
        logger.error(f"[create_project_tool] 写 DB 失败: {e}", exc_info=True)

    project = {
        "id": safe_id, "db_id": db_project_id, "name": name, "fmea_type": fmea_type,
        "description": description, "part_number": part_number, "core_object": name,
        "current_step": 0,
        "team_members": [t.strip() for t in team_members.split(",") if t.strip()],
    }
    get_or_create_pipeline().project = project
    logger.info(f"✅ 项目已创建: {safe_id} (name={name})")
    return json.dumps({"status": "created", "project": project}, ensure_ascii=False)

def _sanitize_json_string(s: str) -> str:
    """清理 JSON 字符串中的非法控制字符（\x00-\x1f 中除 \t\n\r 外的字符）"""
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', ' ', s)

def run_function_analysis_tool(project_id: str = "") -> str:
    """运行功能分析（Step 2）。基于已有的结构树调用 Pipeline 进行完整功能分析（3层: 上级要求/关键功能/下级要求），构建功能网络。自动使用当前项目的结构树数据。

    Args:
        project_id: 项目ID（如 "3_6KW"），为空时自动检测
    """
    import sys
    print("\n" + "=" * 78, flush=True)
    print("  🧠 [run_function_analysis_tool] 启动  —  走多 Agent 编排框架", flush=True)
    print("  知识源: WeKnora 知识库 + 6 个专业 Agent 并发 + 系统集成专家汇总", flush=True)
    print("=" * 78, flush=True)
    from models.structure import StructureTree

    pipeline = get_or_create_pipeline()

    # ── 自动检测项目（agent 必须显式传 project_id，否则选错项目）──
    if not project_id:
        # v7: 从 DB 单条 SQL 取有结构树的项目（项目名 + id 都返回）
        try:
            _conn = get_user_db_conn()
            with _conn.cursor() as _cur:
                _cur.execute("""
                    SELECT p.id, COALESCE(p.name, p.id::text)
                    FROM projects p
                    JOIN fmea_step_assignee_data d
                      ON d.project_id = p.id
                     AND d.step_name = 'structure'
                     AND d.assignee_user_id = -1
                    ORDER BY p.id DESC
                    LIMIT 1
                """)
                _row = _cur.fetchone()
                if _row:
                    project_id = _row[1] if _row[1] else str(_row[0])
                    pipeline.project = {"id": project_id}
            _conn.close()
        except Exception:
            pass
    if not project_id:
        return ("未找到已存在的项目，请先在左侧面板创建项目，或使用 create_project_tool 创建。\n"
                "⚠️ 注意：如果之前已经创建过项目，调用 create_project_tool 时会自动复用，不会重复创建。")

    # v7: 全部从 DB 读结构树，不读本地 JSON
    tree_data = None
    from engine.tools.fmea_helpers import get_baseline
    _db_pid0 = _resolve_db_project_id(project_id)
    if _db_pid0:
        _db_data = get_baseline(_db_pid0, "structure")
        if _db_data and isinstance(_db_data, dict) and _db_data.get("nodes"):
            tree_data = _db_data
    if not tree_data:
        return f"❌ DB 中未找到结构树数据（pid={_db_pid0}），请先运行结构分析（Step 1）将结构树保存至 DB。不存在任何本地 JSON 回退。"


    # 兼容处理
    if isinstance(tree_data.get("llm_raw_output"), dict):
        tree_data["llm_raw_output"] = json.dumps(
            tree_data.pop("llm_raw_output"), ensure_ascii=False, default=str
        )

    # 清理 nodes
    str_defaults = {"part_number", "material", "specification", "source_files", "char_class", "char_requirement"}
    for node in tree_data.get("nodes", []):
        for key in str_defaults:
            if node.get(key) is None:
                node[key] = ""
        if node.get("quantity") is None:
            node["quantity"] = 1
        if node.get("confidence") is None:
            node["confidence"] = 0.0
        if node.get("parent_id") is None:
            node["parent_id"] = ""

    tree = StructureTree(**tree_data)

    # 推送进度：功能分析开始
    _set_tool_progress(project_id, {
        "tool": "run_function_analysis_tool",
        "phase": "功能分析",
        "message": f"正在分析 {len(tree.nodes)} 个节点的功能（多 Agent 编排）",
        "progress": 0.05,
    })

    # ── v5+: 多 Agent 编排路径（参考 MultiAgentFMEA 方案）
    # 优先走 FunctionMultiAgentRunner（系统集成专家 + 6 专业 Agent + 法规 Agent）
    # 失败时降级到原 FmeaPipeline.step2_function 单 Agent 路径
    use_multi_agent = os.getenv("FMEA_FUNCTION_USE_MULTI_AGENT", "1") == "1"
    analysis = None
    runner_exc: Optional[Exception] = None

    if use_multi_agent:
        try:
            from orchestrator.function_multi_agent_runner import FunctionMultiAgentRunner

            async def _progress(phase, ratio, msg, extra_data=None):
                payload = {
                    "tool": "run_function_analysis_tool",
                    "phase": phase,
                    "message": msg,
                    "progress": 0.05 + ratio * 0.9,
                }
                # v6.3: 包含 Agent 实时状态（前端进度卡渲染）
                if extra_data and extra_data.get("agent_events"):
                    payload["agent_events"] = extra_data["agent_events"]
                # v6.3.3: _set_tool_progress 会自动写入双 key（目录名 + db_pid）
                _set_tool_progress(project_id, payload)

            runner = FunctionMultiAgentRunner(data_dir=str(DATA_DIR))
            # v7: 从 DB 取结构树最新的工程师指派数据（JSON 文件可能未同步）
            node_assignee_map = {}
            try:
                from engine.tools.fmea_helpers import get_baseline
                _db_pid = _resolve_db_project_id(project_id)
                if _db_pid:
                    _db_data = get_baseline(_db_pid, "structure")
                    if _db_data and isinstance(_db_data, dict):
                        for _n in _db_data.get("nodes", []):
                            _nid = _n.get("id")
                            if _nid:
                                node_assignee_map[_nid] = {
                                    "assignee_user_id": _n.get("assignee_user_id"),
                                    "assignee_name": _n.get("assignee_name", ""),
                                }
            except Exception:
                pass
            analysis = asyncio.run(runner.run(tree, project_id, _progress, node_assignee_map=node_assignee_map))
        except Exception as e:
            import traceback
            runner_exc = e
            logger.warning(
                f"[run_function_analysis_tool] 多 Agent 路径失败，降级到单 Agent: {e}\n"
                f"{traceback.format_exc()}"
            )

    if analysis is None:
        # 降级到原 Pipeline 路径
        if runner_exc:
            _set_tool_progress(project_id, {
                "tool": "run_function_analysis_tool",
                "phase": "功能分析",
                "message": f"多 Agent 路径失败，降级到单 Agent: {runner_exc}",
                "progress": 0.05,
            })
        try:
            analysis = asyncio.run(pipeline.step2_function(tree, project_id))
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error(f"[run_function_analysis_tool] 单 Agent 路径也失败: {e}\n{tb}")
            _set_tool_progress(project_id, {
                "tool": "run_function_analysis_tool",
                "phase": "功能分析",
                "message": f"功能分析失败: {e}",
                "progress": -1.0,
            })
            return (f"功能分析失败: {str(e)}。"
                    f"结构树数据正常可用，请重试 run_function_analysis_tool，无需重新提取结构树。")  # fmt: skip

    # 构建写入 DB 的数据（不再写本地 JSON 文件，数据只存 DB）
    try:
        full_data = analysis.model_dump(exclude={
            'component_name', 'higher_level_function',
        })
        # items：为前端 auto-detect 补齐 focus_function（合并 functions[] 描述）
        # 前端 detectStepFromData 检查 first.focus_function 识别"function" tab
        if 'items' in full_data:
            for item in full_data['items']:
                item.pop('component_name', None)
                item.pop('higher_level_function', None)
                # 从 functions[] 合并 focus_function（v2 兼容）
                fn_list = item.get('functions', [])
                if isinstance(fn_list, list) and fn_list:
                    item['focus_function'] = '；'.join(
                        f.get('description', '') for f in fn_list if f.get('description')
                    )[:500]
                else:
                    item['focus_function'] = item.get('upper_level_function', '')
        # 确保 extra 字段也被序列化（Pydantic v2 model_dump 默认只包含 model fields，
        # 但 node_functions / function_network_detail 已定义为 Field，理论上应包含。
        # 显式合并以防万一。）
        if hasattr(analysis, 'node_functions') and analysis.node_functions:
            full_data['node_functions'] = analysis.node_functions
        if hasattr(analysis, 'function_network_detail') and analysis.function_network_detail:
            full_data['function_network_detail'] = analysis.function_network_detail
        # v5+ 多 Agent 元信息（存在 validation_report._multi_agent_meta）
        if (hasattr(analysis, 'validation_report')
                and analysis.validation_report
                and analysis.validation_report.get('_multi_agent_meta')):
            full_data['multi_agent_meta'] = analysis.validation_report['_multi_agent_meta']
        # v6: domain_rows（参考Excel格式扁平行）
        if hasattr(analysis, 'domain_rows') and analysis.domain_rows:
            full_data['domain_rows'] = analysis.domain_rows
        # v7: 强制 items[].node_name = 结构树名称（覆盖 LLM 带括号的版本）
        _struct_names = {n.id: n.name for n in tree.nodes}
        for _it in full_data.get('items', []):
            _nid = _it.get('node_id')
            if _nid and _nid in _struct_names:
                _it['node_name'] = _struct_names[_nid]

        # ── v7: 写入 DB（fmea_step_assignee_data，assignee=-1 基线行）
        # 前端 get_step_data 从 DB 读，不再依赖本地 JSON 文件
        try:
            db_pid = _resolve_db_project_id(project_id)
            if db_pid:
                from engine.tools.fmea_helpers import upsert_baseline, save_assignee_data
                upsert_baseline(db_pid, "function", full_data)
                # v7: 按工程师拆分 — items[].assignee_user_id 由 _consolidate_items 注入
                save_assignee_data(db_pid, "function", -1, full_data, filter_own=False, force_split=True)
                logger.info(f"[run_function_analysis_tool] DB 写入成功: project={db_pid} step=function")
            else:
                logger.warning(f"[run_function_analysis_tool] 未找到 DB project_id，跳过 DB 写入")
        except Exception as e:
            logger.warning(f"[run_function_analysis_tool] DB 写入失败（不影响本地文件）: {e}")

        # 同时生成摘要文件（兼容之前 chat.html 的快速预览）
        total_fn = sum(len(item.functions) for item in analysis.items)
        summary = {
            "step": "02",
            "project_id": project_id,
            "step_name": "功能分析",
            "total_components": len(analysis.items),
            "total_functions": total_fn,
            "total_relationships": len(analysis.function_network.relations) if analysis.function_network else 0,
            "engine": "multi_agent" if use_multi_agent and runner_exc is None else "single_agent_fallback",
        }

        logger.info(
            f"[run_function_analysis_tool] ✅ 功能分析完成 [{summary['engine']}]: "
            f"{len(analysis.items)} 个节点, {total_fn} 条功能, "
            f"{summary['total_relationships']} 条关系"
        )

        # 推送进度：功能分析完成
        _set_tool_progress(project_id, {
            "tool": "run_function_analysis_tool",
            "phase": "功能分析",
            "message": f"功能分析完成 [{summary['engine']}]：{len(analysis.items)} 个节点, {total_fn} 条功能, {summary['total_relationships']} 条关系",
            "progress": 1.0,
        })

        # v17.12: 阶段完成邮件（给项目负责人）
        try:
            from engine.tools.notify_tools import send_stage_complete_email
            send_stage_complete_email(2, project_id)
        except Exception:
            pass

        return json.dumps({
            "success": True,
            "status": "completed",
            "summary": summary,
            "message": f"✅ 功能分析完成：{len(analysis.items)} 个节点 / {total_fn} 条功能 / {summary['total_relationships']} 条关系（{summary['engine']}）",
            "project_id": project_id,
            "total_components": len(analysis.items),
            "total_functions": total_fn,
            "total_relationships": summary['total_relationships'],
            "engine": summary['engine'],
        }, ensure_ascii=False)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"[run_function_analysis_tool] 结果保存失败: {e}\n{tb}")
        _set_tool_progress(project_id, {
            "tool": "run_function_analysis_tool",
            "phase": "功能分析",
            "message": f"结果保存失败: {e}",
            "progress": -1.0,
        })
        return (f"功能分析完成但保存失败: {str(e)}。")


def run_failure_analysis_tool(project_id: str = "") -> str:
    """运行失效分析（Step 3）。基于已有的结构树和功能分析，为每个功能识别失效链（FE→S→FM→FC），含知识库增强和功能网感知。

    Args:
        project_id: 项目 ID 或名称（必填，自动从流水线上下文检测）
    """
    from models.structure import StructureTree
    from models.function import FunctionAnalysis

    pipeline = get_or_create_pipeline()

    # ── 自动检测项目（如果调用方未传入）──
    if not project_id:
        project_id = ""
        if pipeline and pipeline.project:
            project_id = pipeline.project.get("id", "")
        if not project_id:
            if DATA_DIR.exists():
                candidates = sorted(DATA_DIR.iterdir(), reverse=True)
                for d in candidates:
                    struct_path = d / "fmea" / "01-structure-tree.json"
                    func_path = d / "fmea" / "02-function.json"
                    if d.is_dir() and struct_path.exists() and func_path.exists() and not re.search(r'_\d+$', d.name):
                        project_id = d.name
                        try:
                            planning_path = d / "fmea" / "00-planning.json"
                            if planning_path.exists():
                                with open(planning_path, "r", encoding="utf-8-sig") as pf:
                                    planning = json.load(pf)
                                pipeline.project = planning
                        except Exception:
                            pass
                        break
        if not project_id:
            return "未找到已完成结构分析和功能分析的项目，请先完成前序步骤，或显式传入 project_id 参数。"

    # ── 从 DB 读取结构树和功能分析（优先 DB，兜底 JSON 文件）──
    db_pid = _resolve_db_project_id(project_id)
    if not db_pid:
        return "无法解析项目 ID"

    try:
        from engine.tools.fmea_helpers import get_baseline

        tree_data = get_baseline(db_pid, "structure")
        func_data = get_baseline(db_pid, "function")
    except Exception as e:
        logger.error(f"[run_failure_analysis_tool] DB 读取失败: {e}")
        return f"读取基线数据失败: {e}"

    # 兜底：如果 DB 没有数据，从 JSON 文件读
    if not tree_data or not func_data:
        logger.warning(f"[run_failure_analysis_tool] DB 无基线数据，尝试从 JSON 文件读取")
        structure_path = DATA_DIR / project_id / "fmea" / "01-structure-tree.json"
        function_path = DATA_DIR / project_id / "fmea" / "02-function.json"

        missing = []
        if not structure_path.exists(): missing.append("structure")
        if not function_path.exists(): missing.append("function")
        if missing:
            return f"必要基线数据缺失：{','.join(missing)}，请先完成前序步骤"

        try:
            with open(structure_path, "r", encoding="utf-8-sig") as f:
                tree_data = json.loads(_sanitize_json_string(f.read()))
            with open(function_path, "r", encoding="utf-8-sig") as f:
                func_data = json.loads(_sanitize_json_string(f.read()))
        except Exception as e:
            return f"数据加载失败: {e}"

    try:
        tree = StructureTree(**tree_data)
        func_analysis = FunctionAnalysis(**func_data)
    except Exception as e:
        return f"数据解析失败: {e}"

    # ── 推送进度 ──
    _set_tool_progress(project_id, {
        "tool": "run_failure_analysis_tool",
        "phase": "失效分析",
        "message": f"正在分析 {len(func_analysis.items)} 个节点的失效模式（多 Agent 编排）",
        "progress": 0.05,
    })

    # ── v8: 多 Agent 编排路径（参考 FunctionMultiAgentRunner）──
    # 优先走 FailureMultiAgentRunner（系统集成专家 + 6 专业 Agent + 法规 Agent）
    # 失败时降级到原 pipeline.step3_failure 单 Agent 路径
    use_multi_agent = os.getenv("FMEA_FAILURE_USE_MULTI_AGENT", "1") == "1"
    logger.info(f"[run_failure_analysis_tool] FMEA_FAILURE_USE_MULTI_AGENT={use_multi_agent}")
    print(f"\n{'='*60}\n  [run_failure_analysis_tool] 多 Agent 模式: {'启用' if use_multi_agent else '禁用'}\n{'='*60}", flush=True)
    analysis = None
    runner_exc: Optional[Exception] = None
    import traceback
    from models.failure import FailureAnalysis

    if use_multi_agent:
        try:
            from orchestrator.failure_multi_agent_runner import FailureMultiAgentRunner

            def _progress_sync(phase, ratio, msg, extra_data=None):
                payload = {
                    "tool": "run_failure_analysis_tool",
                    "phase": phase,
                    "message": msg,
                    "progress": 0.05 + ratio * 0.9,
                }
                if extra_data and extra_data.get("agent_events"):
                    payload["agent_events"] = extra_data["agent_events"]
                _set_tool_progress(project_id, payload)

            # 获取高风险主题（从规划文档）
            high_risk_themes = []
            try:
                planning_path = DATA_DIR / project_id / "fmea" / "00-planning.json"
                if planning_path.exists():
                    with open(planning_path, "r", encoding="utf-8-sig") as pf:
                        planning = json.load(pf)
                    high_risk_themes = planning.get("high_risk_themes", [])
            except Exception:
                pass

            runner = FailureMultiAgentRunner(data_dir=str(DATA_DIR))
            # 使用事件循环安全执行
            try:
                loop = asyncio.get_running_loop()
                # 已有事件循环 → 用 ensure_future + run_until_complete
                import threading
                if threading.current_thread() is threading.main_thread():
                    analysis = loop.run_until_complete(runner.run(
                        tree, func_analysis, project_id,
                        progress_callback=_progress_sync,
                        high_risk_themes=high_risk_themes,
                    ))
                else:
                    # 非主线程 → 新事件循环
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    analysis = new_loop.run_until_complete(runner.run(
                        tree, func_analysis, project_id,
                        progress_callback=_progress_sync,
                        high_risk_themes=high_risk_themes,
                    ))
                    new_loop.close()
            except RuntimeError:
                # 无事件循环
                analysis = asyncio.run(runner.run(
                    tree, func_analysis, project_id,
                    progress_callback=_progress_sync,
                    high_risk_themes=high_risk_themes,
                ))

            print(f"\n{'='*60}\n  [run_failure_analysis_tool] 多 Agent 失效分析完成!\n{'='*60}", flush=True)

        except Exception as e:
            runner_exc = e
            logger.warning(
                f"[run_failure_analysis_tool] 多 Agent 路径失败，降级到单 Agent: {e}\n"
                f"{traceback.format_exc()}"
            )

    if analysis is None:
        # 降级到原 Pipeline 路径
        if runner_exc:
            _set_tool_progress(project_id, {
                "tool": "run_failure_analysis_tool",
                "phase": "失效分析",
                "message": f"多 Agent 路径失败({str(runner_exc)[:60]})，降级到单 Agent...",
                "progress": 0.5,
            })

        analysis = asyncio.run(pipeline.step3_failure(tree, func_analysis, project_id))

        if not isinstance(analysis, FailureAnalysis):
            analysis = asyncio.run(pipeline.step3_failure(tree, func_analysis, project_id))

    # ── 保存结果（v7: 只存 DB，不写本地 JSON）──
    analysis_data = analysis.model_dump()
    try:
        db_pid = _resolve_db_project_id(project_id)
        logger.info(f"[run_failure_analysis_tool] SAVE_START: db_pid={db_pid} items={len(analysis_data.get('items', []))}")
        if db_pid:
            from engine.tools.fmea_helpers import upsert_baseline, save_assignee_data
            ok1 = upsert_baseline(db_pid, "failure", analysis_data)
            logger.info(f"[run_failure_analysis_tool] upsert_baseline returned: {ok1}")
            # v8: 按工程师拆分 — items[].assignee_user_id 由 consolidation 阶段注入
            ok2 = save_assignee_data(db_pid, "failure", -1, analysis_data, filter_own=False, force_split=True)
            logger.info(f"[run_failure_analysis_tool] save_assignee_data returned: {ok2}")
            if ok1 and ok2:
                logger.info(f"[run_failure_analysis_tool] DB 写入成功: project={db_pid} step=failure")
            else:
                logger.warning(f"[run_failure_analysis_tool] DB 写入部分失败: ok1={ok1} ok2={ok2}")
        else:
            logger.warning(f"[run_failure_analysis_tool] 未找到 DB project_id，跳过 DB 写入")
    except Exception as e:
        logger.warning(f"[run_failure_analysis_tool] DB 写入失败: {e}")
        import traceback
        logger.warning(traceback.format_exc())

    stats = analysis.get_summary_stats()

    _set_tool_progress(project_id, {
        "tool": "run_failure_analysis_tool",
        "phase": "失效分析",
        "message": f"失效分析完成：{stats['total']} 条失效链, S≥7:{stats['high_severity_count']}, 安全:{stats['safety_failure_count']}",
        "progress": 1.0,
    })

    logger.info(f"[run_failure_analysis_tool]  失效分析完成: {stats}")

    # v17.12: 阶段完成邮件（给项目负责人）
    try:
        from engine.tools.notify_tools import send_stage_complete_email
        send_stage_complete_email(3, project_id)
    except Exception:
        pass

    return json.dumps({
        "success": True,
        "status": "completed",
        "summary": stats,
        "message": (
            f"  失效分析完成：{stats['total']} 条失效链\n"
            f"   - 高严重度(S≥7): {stats['high_severity_count']}\n"
            f"   - 安全功能失效: {stats['safety_failure_count']}\n"
            f"   - 功能丧失传导: {stats['function_loss_count']}\n"
            f"   - 功能降级传导: {stats['function_degrade_count']}"
        ),
    }, ensure_ascii=False)


# ════════════════════════════════════════════════════════════════
# FMEA Agent（基础能力 = agno/server.py + FMEA 特化扩展）
# ════════════════════════════════════════════════════════════════

fmea_agent = Agent(
    name="FMEA 专家",
    description="专业的 FMEA（失效模式与影响分析）智能助手，引导完成7步DFMEA/PFMEA全流程",
    model=OpenAIChat(id=MODEL_NAME, base_url=MODEL_BASE_URL, api_key=MODEL_API_KEY),
    # ⚠️ 仅使用自定义工具，不使用任何 agno 内置工具
    # 已禁用的: CalculatorTools / DuckDuckGoTools / PythonTools / FileTools
    tools=[
        # ── 自定义命令执行工具 ──
        execute_command,
        # ── 自定义文件读取工具 ──
        read_file,
        # ── 自定义 FMEA Pipeline 工具 ──
        create_project_tool,
        run_function_analysis_tool,
        run_failure_analysis_tool,
        # ── 知识库 / 项目初始化工具 ──
        init_project_tool, get_init_progress_tool,
        get_kb_status_tool, list_kb_pages_tool,
        build_knowledge_base_tool, extract_structure_tree_tool,
        extract_structure_tree_from_weknora_tool,
        # ── 风险评估工具（文件驱动的 SOD/AP 计算）──
        register_risk_criteria_tool, run_risk_analysis_tool,
        get_risk_status_tool,
        # ── 优化分析工具（LLM生成措施 + 责任人分配）──
        run_optimization_tool, get_optimization_status_tool,
        # ── 报告生成工具 ──
        generate_report_tool,
        # ── 通知/推送工具（向指派工程师发送邮件）──
        push_structure_to_engineers, list_assigned_nodes,
        # ── LLM 数据查询工具（自然语言 → SQL → 数据库）──
        query_project_data_tool,
    ],
    instructions=[
        "# 角色定位",
        "你是一位资深的 FMEA 分析专家，精通 AIAG-VDA FMEA 方法论。",
        "支持 DFMEA（设计FMEA）和 PFMEA（过程FMEA），适用所有行业的产品/过程。",
        "引导工程师完成7步FMEA全流程。",
        "",
        "# 核心能力",
        "FMEA 七步法: 规划准备 → 结构分析 → 功能分析 → 失效分析 → 风险评估 → 优化措施 → 文件化",
        "评分: S(严重度 1-10), O(发生度 1-10), D(探测度 1-10), RPN=S×O×D",
        "操作优先级 AP: H(High)/M(Medium)/L(Low) 根据 S/O/D 组合按AIAG/VDA标准查表判断",
        "",
        "# 可用工具（全部为自定义工具，已禁用所有 agno 内置工具）",
        "- execute_command(command): 执行 CMD/PowerShell 命令。所有操作（包括文件增删改查）直接自动执行，无需用户确认。",
        "- read_file(file_path): 读取本地文件（PDF/DOCX/XLSX/TXT），参数名 file_path，支持 read_file(file_paths=[path1,path2]) 批量读取",
        "",
        "# ⚠️ 知识库专用工具（读取 knowledge/ 目录内容时优先用这些）",
        "# [已注释] query_kb_tool — 见 MiniMax 退化问题",
        "- extract_structure_tree_tool(project_id, extra_question): 【旧管线】从知识库所有文档中提取完整产品结构树。",
        "- extract_structure_tree_from_weknora_tool(project_id, weknora_kb_id, subject): 【推荐】从 WeKnora 知识库提取产品结构树。project_id=项目ID, weknora_kb_id=知识库UUID, subject=FMEA主体对象（如\"12V低压电池系统\"）。使用双通道（混合搜索+全文预览）比旧管线更准确。",
        "- list_kb_pages_tool(project_id): 列出知识库所有页面及其摘要",
        "- get_kb_status_tool(project_id): 查看知识库状态（页面数、大小）",
        "- init_project_tool(project_name, sor_folder, project_id): 初始化项目知识库。project_name=项目名, sor_folder=SOR文件夹路径, project_id=项目ID（必填！从 create_project_tool 返回的 project.id 中获取）",
        "- get_init_progress_tool(project_id): 查看初始化进度",
        "",
        "# 🤖 LLM-Wiki 知识库工具（功能分析阶段【禁止】调用）",
        "- build_knowledge_base_tool(project_id): 构建项目知识库（将SOR文档转换为可查询的Wiki页面）。⚠️ 仅在 Step1 首次提取结构树之前调用；功能分析阶段【禁止】再调用此工具！",
        "",
        "# FMEA 专用工具",
        "- create_project_tool: 创建FMEA项目，参数: name, fmea_type, description, part_number, team_members",
        "- run_function_analysis_tool(project_id): ⚠️【必传参数 project_id 必须传】（用户当前的项目ID，如'3_6KW'）。这是因为 DB 可能有多个项目，不传会自动选错。用户说'做功能分析'时立刻调用此工具，禁止任何前置操作。自动从 DB 读结构树、走【多 Agent 编排框架】（系统集成专家 + 6 个专业 Agent 并发 + 法规 Agent），知识源 = WeKnora。数据 100% 存 DB。",
        "- run_failure_analysis_tool: 运行失效分析（Step 3）。【必须使用！】参数 project_id（必填）。传入当前项目 ID 或名称后，工具自动从 DB 读取已有的结构树和功能分析（不要再 read_file 或 execute_command），为每条功能识别失效链（FE→S→FM→FC），含知识库增强和功能网感知。结果保存为 03-failure.json。⚠️ 禁止用 extract_structure_tree_tool 代替！",
        "",
        "# ⚠️ 风险评估工具（Step 4 / 阶段5 — LLM提取 + 确定性计算）",
        "⚠️ 混合架构：① LLM 一次提取用户上传的评分标准 → ② 打分/AP 计算纯 Python 执行",
        "- register_risk_criteria_tool(project_id): 【必须首先调用】从 DB（project_risk_files 表）读取用户上传的评分文件，调用LLM提取SOD评分标准和AP表规则，",
        "  存到 DB（fmea_risk_criteria 表）。支持任意文件格式（管道表、HTML表格、矩阵表等）。",
        "  如果用户未上传文件或LLM提取失败，自动回退到 AIAG/VDA 默认标准。",
        "  用户通过FMEA工作台的「风险评分文件」面板上传文件（存 DB，非本地目录）。",
        "- run_risk_analysis_tool(project_id, use_llm_controls=True): 运行风险评估（Step 4）。",
        "  v12: 失效数据从 DB 读（get_baseline failure）；评分标准从 DB 读（fmea_risk_criteria）。",
        "  逐条计算 S/O/D/RPN/AP（AIAG-VDA 2021 标准 AP 表，全覆盖，不 fallback RPN）。",
        "  每条标记 regulation_binding（从 function 法规绑定 JOIN，法规项高亮）；S/O/D 未命中标准时标记需人工确认。",
        "  PC/DC 控制措施默认由 LLM 生成（带缓存），失败回退模板。结果存 DB + 04-risk.json。",
        "- get_risk_status_tool(project_id): 查看项目风险评估状态。",
        "",
        "⚠️ 风险评估完整工作流：",
        "  1. 引导用户上传 SOD 标准和 AP 表的文件（FMEA 工作台「风险评分文件」面板，存 DB）",
        "  2. 调用 register_risk_criteria_tool(project_id) → LLM提取评分标准 → 存 DB → 后续不再用LLM",
        "  3. 调用 run_risk_analysis_tool(project_id) → 确定性计算 → 存 DB + 04-risk.json",
        "  4. 用 get_risk_status_tool(project_id) 确认结果",
        "",
        "# 📋 优化措施工具（Step 5 — LLM生成 + 责任人随机分配）",
        "- run_optimization_tool(project_id, max_items=0): 运行优化分析。自动筛选高风险项(AP=H/M 或 违反强制法规 regulation_violation=true)，按RPN降序全量处理（max_items=0不限制；需要分批时传N），分批次（每批4条）8并发调用LLM生成预防/探测措施+目标完成日期（LLM结合当前日期与严重度推理），责任人=该功能所在工程师，保存为 05-optimization.json。",
        "  如果所有项风险可接受（无高风险项），会自动跳过并保存空结果。",
        "- get_optimization_status_tool(project_id): 查看项目优化分析状态和结果。",
        "",
        "⚠️ 优化分析完整工作流：",
        "  1. 先确认风险评估已完成（检查 04-risk.json 是否存在）",
        "  2. 调用 run_optimization_tool(project_id, max_items=0) → 全量高风险项 → 8并发LLM → 责任人(该功能工程师) → 保存",
        "  3. 用 get_optimization_status_tool(project_id) 确认结果",
        "",
        "# 📋 报告生成工具（Step 6 — 汇总 + Excel导出）",
        "- generate_report_tool(project_id): 读取Step 1-5所有数据，按DFMEA方法论组装最终报告。",
        "  同时生成 06-report.json（前端渲染）和 .xlsx（导出）。",
        "",
        "# 🔍 数据查询工具（自然语言查数据库）",
        "- query_project_data_tool(question, project_id): 用自然语言查询项目数据库。",
        "  适用场景（仅查询类问题）:",
        "  · 项目信息: '有哪些项目？'、'XX项目是什么时候创建的？'",
        "  · 项目进展: 'XX项目FMEA进展到哪一步了？'、'各项目完成情况汇总'",
        "  · 统计数据: 'XX项目有多少失效模式？'、'RPN最高的5项'、'AP为H的有哪些？'",
        "  · 人员信息: 'XX项目有哪些成员？'、'各成员的审批状态'",
        "  · 风险分析: '严重度>=8的失效模式'、'各步骤数据量统计'",
        "  参数: question=自然语言问题（必填）, project_id=可选项目标识（名称关键词或ID）",
        "",
        "  ⚠️【v24.2 触发判断规则】以下场景严禁调 query_project_data_tool：",
        "  · 操作类指令: '生成/做/跑/执行/改/修复/构建/提取'等动词开头的——调对应 step 工具或 LLM 直接回答",
        "  · 代码/文件/日志类问题: '修复这个脚本报错'、'优化这段代码'——LLM 直接给出修复建议",
        "  · 任务/需求类描述: '请修复 X、Y、Z'、'帮我做 1 2 3'——分步骤回复，不调 SQL",
        "  · 模糊/无明确查询意图: '看看有什么问题'、'现在怎样'——先澄清意图，不调 SQL",
        "  调用前自检: 用户问题是否包含**明确查询目标**（'多少'、'哪些'、'最高/低'、'汇总'）？",
        "  否则严禁调 query_project_data_tool。",
        "  ⚠️ 【重要】工具返回的结果包含 markdown_table 字段（已是 markdown 表格），你的回复必须：",
        "     1. 先用 1-2 句话回答用户的核心问题",
        "     2. 然后原样引用 markdown_table 字段（去掉多余转义）展示给用户",
        "     3. 不要只说'已完成'或'已查询'——必须把数据展示出来",
        "",
        "# ⚠️ 关键规则（v7 — 严格执行）",
        "1. 需要提取结构树 → 必须调用 extract_structure_tree_from_weknora_tool，禁止用文字描述代替",
        "2. 结构树提取完成后（从Step2开始），禁止再次调用 extract_structure_tree_tool / extract_structure_tree_from_weknora_tool！",
        "3. 【Step2 功能分析】禁止调用 build_knowledge_base_tool（除非用户明确要求重建知识库）！结构树已在 DB，直接调 run_function_analysis_tool。",
        "4. 【禁止】使用 execute_command / findstr / dir / ls 去找结构树 JSON 文件！所有数据都在 DB 中，工具会自动读取。",
        "5. 【禁止】在用户说'做功能分析/跑功能分析/分析功能/请基于结构树分析功能'时，先做任何事前的命令执行或文件查找！",
        "   —— 用户要求功能分析 → 【立刻】直接调 run_function_analysis_tool(project_id)（或无参）",
        "6. 调用工具后等待工具返回结果，不要自己编造结果",
        "7. [已注释] query_kb_tool — 知识库智能查询已临时禁用",
        "",
        "重要: 所有结构树/功能/失效/风险/优化数据 100% 从 DB（fmea_step_assignee_data 表）读取。",
        "      不再依赖本地 data/projects/*.json 文件。任何工具调用都自带数据源处理。",
        "",
        "# FMEA 七步工作流",
        "0. 规划准备 — 创建项目。项目名称即为 FMEA 分析的核心对象（结构树的根节点），引导用户确认项目名称是否准确描述了待分析的产品/系统。",
        "1. 结构分析 — 调用 extract_structure_tree_from_weknora_tool 生成结构树（走 WeKnora 知识库），无需上传 BOM。",
        "2. 功能分析 — 【直接调 run_function_analysis_tool】！必须传 project_id（如 project_id='3_6KW'），无参数会从 DB 选错项目！内部已封装多 Agent 编排（系统集成专家 + 6 个专业 Agent + 法规 Agent），自动从 WeKnora 检索知识、自动按领域路由节点、自动汇总 + 校验 V3-1~V3-4。⚠️ 严格禁止再调 build_knowledge_base_tool / extract_structure_tree_tool / extract_structure_tree_from_weknora_tool！⚠️ 也禁止先执行任何 shell 命令找文件！用户说'功能分析' → 直接调 run_function_analysis_tool。",
        "3. 失效分析 — 【必须调用 run_failure_analysis_tool(project_id=xxx)】！必须显式传入当前项目 ID。工具会自动从 DB 读取结构树和功能分析数据（不要再 read_file 或 execute_command）。⚠️ 禁止调用 extract_structure_tree_tool（结构树已经建好），也禁止用文字描述代替工具调用！直接传 project_id 调 run_failure_analysis_tool 即可。",
        "4. 风险评估 — 先调 register_risk_criteria_tool 用 LLM 提取用户上传的 SOD 标准和 AP 表（任意格式），再调 run_risk_analysis_tool 基于缓存的标准计算 S/O/D/RPN/AP。计算阶段不调 LLM。",
        "5. 优化措施 — 调 run_optimization_tool(max_items=0) 按AP=H/M+法规违规项筛选高风险项全量处理，8并发LLM生成预防/探测措施+目标完成日期（LLM结合当前日期与严重度推理），责任人=该功能所在工程师，自动计算改进后S/O/D/AP预估。",
        "6. 文件化 — 调 generate_report_tool 汇总所有步骤数据生成完整DFMEA报告，前端自动渲染报告表格，同时生成可导出的xlsx文件。",
        "",
        "# ⚡ 自动执行规则",
        "所有工具调用都会自动执行，无需用户确认。包括文件操作、命令执行、知识库查询、数据保存、步骤推进等所有操作。",
        "执行完工具后直接使用结果继续工作，不要在调用工具后停下来询问用户。",
        "",
        "# 🔧 工具调用前通知规则（必须遵守！）",
        "【调用工具前】当你需要使用工具时，必须先用自然语言告诉用户你准备做什么。",
        "- 一句话说明：要调用什么工具、目的是什么、涉及哪些内容",
        "- 这条消息会稳定显示在聊天界面中，不会被后续工具执行日志覆盖",
        "- 用户通过这条消息了解当前进度，必须清晰具体，不能省略",
        "",
        "示例：",
        "  用户: 「帮我生成结构树」",
        "  你（调用工具前）: 「正在从知识库中提取产品结构树，将遍历所有SOR文档分析系统→子系统→组件→零件的层级关系，请稍候。」",
        "  [然后调用 extract_structure_tree_tool]",
        "",
        "# 🔇 工具调用后展示规则（最高优先级！违反将导致工作流失败）",
        "【铁律】调用任何工具后，只回复一句话：「已完成。」严禁输出任何额外内容。",
        "【铁律】禁止输出工具返回的任何数据、JSON、统计数字、文件路径、摘要。",
        "",
        "✅ 唯一允许的回复格式：",
        "  「已完成。」",
        "",
        "❌ 绝对禁止的行为：",
        "  - 禁止输出任何工具的返回内容、统计信息、文件路径",
        "  - 禁止用任何形式复述或总结工具返回内容",
        "  - 禁止在工具调用后输出表格、数字、列表",
        "",
        "⚠️ 此规则适用于所有工具调用，无一例外。",
        "",
        "正确示例：",
        "  用户: 「帮我分析结构树」",
        "  你（调用工具后）: 「已完成。」",
        "  ❌ 错误: 「✅ 结构树提取完成！提取节点: 50个...」",
        "  ❌ 错误: 「已保存到 D:/...」、「AP=H:5 M:3 L:2」",
        "",
        "工具调用前的预告也保持简洁，一句话说明要做什么即可，不需要长篇解释。",
        "",
        "# 🗂️ 文件路径规则（严格遵守！）",
        "⚠️ 不要将任何 FMEA 分析产物（JSON/Excel/报告等）写入 knowledge/ 目录。",
        "knowledge/ 目录只能存放原始文档（由 init_project_tool 管理），不要手动在此目录创建文件。",
        "所有分析产物由专用工具（extract_structure_tree_tool 等）自动保存到正确位置，你不需要额外执行 write/echo 等命令来保存。",
        "",
        "# 通用性原则",
        "不假设产品类型（汽车/电子/机械/医疗/软件等均可），由用户提供的信息驱动分析。",
        "BOM结构由LLM自动分析，失效模式基于功能自主推理，不依赖硬编码模板。",
        "SOD评分基于行业通用标准（AIAG/VDA），AP按标准查表规则计算。",
    ],
    add_history_to_context=True,
    markdown=True,
    stream=True,
)

# 启动时验证工具注册
_tool_names = [getattr(t, '__name__', str(t)) for t in fmea_agent.tools] if hasattr(fmea_agent, 'tools') else []
logger.info(f"🔧 Agent 已注册 {len(_tool_names)} 个工具: {', '.join(_tool_names[:10])}...")
if 'extract_structure_tree_tool' not in _tool_names:
    logger.error("❌ extract_structure_tree_tool 未成功注册到 Agent！")

# ════════════════════════════════════════════════════════════════
# 组装 AgentOS 服务
# ════════════════════════════════════════════════════════════════

from agno.db.postgres import PostgresDb
db = PostgresDb(db_url=DB_URL, db_schema="agno")
logger.info(f"数据库: PostgreSQL (agno schema)")

agui = AGUI(agent=fmea_agent, prefix="/agui", tags=["AGUI"])

agent_os = AgentOS(
    name="FMEA AgentOS",
    agents=[fmea_agent],
    db=db,
    interfaces=[agui],
)

app = agent_os.get_app()

# ═══ 移除 AgentOS 默认根路由 (/) — 使用我们的自定义仪表盘 ═══
from fastapi.routing import APIRoute
app.router.routes = [
    r for r in app.router.routes
    if not (isinstance(r, APIRoute) and r.path == "/")
]


# ════════════════════════════════════════════════════════════════
# 用户认证系统
# ════════════════════════════════════════════════════════════════

import hashlib
import secrets
import jwt
import psycopg2
import psycopg2.extras
import psycopg2.pool

from fastapi import Request

JWT_SECRET = os.getenv("FMEA_JWT_SECRET", "fmea-agent-jwt-secret-key-2026-must-be-at-least-32-bytes")
JWT_ALGORITHM = "HS256"

# ── 用户数据库（仅 PostgreSQL）──
DB_HOST = os.getenv("URL_AGENT_DB_HOST", "10.180.17.11")
DB_PORT = int(os.getenv("URL_AGENT_DB_PORT", "5433"))
DB_NAME = os.getenv("URL_AGENT_DB_NAME", "fmea")
DB_USER = os.getenv("URL_AGENT_DB_USER", "postgres")
DB_PASSWORD = os.getenv("URL_AGENT_DB_PASSWORD", "postgres123!@#")

logger.info(f"用户数据库: PostgreSQL ({DB_HOST}:{DB_PORT}/{DB_NAME})")

# ── 全局 DB 连接池（避免每次请求新开连接） ──
_db_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


class _PooledConn:
    """包装 psycopg2 连接，close() 时自动归还到池中（避免破坏现有 with 语句）"""
    def __init__(self, conn):
        self._conn = conn
        self._returned = False
    def __getattr__(self, name):
        return getattr(self._conn, name)
    def close(self):
        if not self._returned:
            self._returned = True
            try:
                self._conn.rollback()
            except Exception:
                pass
            if _db_pool:
                _db_pool.putconn(self._conn)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()
        return False


def get_user_db_conn():
    """获取用户数据库连接（带连接池，close() 自动归还池）"""
    global _db_pool
    if _db_pool is None:
        _db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=20,
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
            user=DB_USER, password=DB_PASSWORD
        )
    return _PooledConn(_db_pool.getconn())

def _sql_param(placeholder: str) -> str:
    """PostgreSQL 用 %s 占位符"""
    return "%s"

def _lastrowid(cursor):
    """获取最后插入的 ID"""
    return cursor.fetchone()[0] if cursor.description else None

def _add_schema_comments(cur, table: str, comments: dict):
    """给 PostgreSQL 表的字段加注释（COMMENT ON COLUMN），静默跳过不存在的字段"""
    for col, desc in comments.items():
        try:
            cur.execute(f"COMMENT ON COLUMN {table}.{col} IS %s", (desc,))
        except Exception:
            pass

def init_user_db():
    """初始化用户表（统一 users，包含所有用户信息）"""
    conn = get_user_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                email_auth_code TEXT NOT NULL DEFAULT '',
                kb_auth_code TEXT NOT NULL DEFAULT '',
                role_code TEXT NOT NULL DEFAULT 'engineer',
                dept TEXT NOT NULL DEFAULT '',
                active BOOLEAN NOT NULL DEFAULT TRUE,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL DEFAULT (NOW())
            )
        """)
        # 兼容旧表：如果已有 users 表但缺少新字段，则补齐
        for col, col_def in [
            ("email", "TEXT NOT NULL DEFAULT ''"),
            ("email_auth_code", "TEXT NOT NULL DEFAULT ''"),
            ("kb_auth_code", "TEXT NOT NULL DEFAULT ''"),
            ("dept", "TEXT NOT NULL DEFAULT ''"),
            ("active", "BOOLEAN NOT NULL DEFAULT TRUE"),
            ("role_code", "TEXT NOT NULL DEFAULT 'engineer'"),
        ]:
            cur.execute(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_name='users' AND column_name='{col}')
                    THEN ALTER TABLE users ADD COLUMN {col} {col_def};
                    END IF;
                END $$;
            """)
        # 创建默认管理员
        cur.execute("SELECT id FROM users WHERE username = %s", ("admin",))
        exists = cur.fetchone()
        if not exists:
            admin_pw = hash_password("admin123")
            cur.execute(
                "INSERT INTO users (username, password_hash, display_name, role_code, role) VALUES (%s, %s, %s, %s, %s)",
                ("admin", admin_pw, "管理员", "admin", "admin")
            )
            logger.info("✅ 已创建默认管理员账号: admin / admin123")
        conn.commit()
        cur.close()
    finally:
        conn.close()

# ════════════════════════════════════════════════════════════════
# 角色与团队成员管理
# ════════════════════════════════════════════════════════════════

DEFAULT_ROLES = [
    ("admin",     "系统管理员",  '["*"]'),
    ("leader",    "项目负责人",  '["fmea.view","fmea.create","fmea.edit","fmea.delete","team.view"]'),
    ("engineer",  "工程师",      '["fmea.view","fmea.edit"]'),
    ("reviewer",  "评审员",      '["fmea.view"]'),
    ("manager",   "经理",        '["fmea.view","team.view","team.edit"]'),
]

DEFAULT_PERMISSIONS = [
    "fmea.view", "fmea.create", "fmea.edit", "fmea.delete",
    "team.view", "team.edit", "team.create", "team.delete",
    "agent.view", "agent.edit", "role.manage",
]

def init_role_tables():
    """初始化角色表、projects / project_members 表，并填充默认数据"""
    conn = get_user_db_conn()
    try:
        cur = conn.cursor()
        # 角色表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS roles (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
                description TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # projects 表（精简版：业务数据在 JSON 文件中，DB 只存元数据）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id          SERIAL PRIMARY KEY,
                name        TEXT UNIQUE NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # 字段注释（PostgreSQL COMMENT ON）
        _add_schema_comments(cur, "projects", {
            "id": "主键，自增整数",
            "name": "项目名称，UNIQUE 约束。映射到 data/projects/{安全化后的name}/ 目录",
            "description": "项目描述。前端列表展示用，冗余存储在 00-planning.json",
            "created_at": "创建时间",
            "updated_at": "最后更新时间",
        })
        # project_members 关联表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS project_members (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role_code TEXT NOT NULL DEFAULT 'engineer',
                joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(project_id, user_id)
            )
        """)
        _add_schema_comments(cur, "project_members", {
            "id": "主键，自增整数",
            "project_id": "关联 projects.id，级联删除",
            "user_id": "关联 users.id，级联删除",
            "role_code": "项目角色：owner / leader / engineer 等",
            "joined_at": "加入时间",
        })
        # fmea_approvals 阶段审批表 — 取代原来的 approval_state.json
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fmea_approvals (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                stage_no INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                comment TEXT NOT NULL DEFAULT '',
                lead_status TEXT NOT NULL DEFAULT 'pending',
                lead_comment TEXT NOT NULL DEFAULT '',
                approved_at TIMESTAMPTZ,
                lead_reviewed_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(project_id, user_id, stage_no)
            )
        """)
        # v5 migration: add lead_status / lead_comment columns if missing
        try:
            cur.execute("ALTER TABLE fmea_approvals ADD COLUMN IF NOT EXISTS lead_status TEXT NOT NULL DEFAULT 'pending'")
            cur.execute("ALTER TABLE fmea_approvals ADD COLUMN IF NOT EXISTS lead_comment TEXT NOT NULL DEFAULT ''")
            cur.execute("ALTER TABLE fmea_approvals ADD COLUMN IF NOT EXISTS lead_reviewed_at TIMESTAMPTZ")
        except Exception:
            pass  # PG 9.x compatibility fallback
        _add_schema_comments(cur, "fmea_approvals", {
            "id": "主键，自增整数",
            "project_id": "关联 projects.id，级联删除",
            "user_id": "关联 users.id，级联删除",
            "stage_no": "FMEA 阶段编号 (1=结构分析, 2=功能, 3=失效, 4=风险, 5=优化, 6=报告)",
            "status": "审批状态: pending / approved / rejected",
            "comment": "审批意见",
            "approved_at": "审批时间",
            "created_at": "记录创建时间",
            "updated_at": "最后更新时间",
        })
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_fmea_approvals_project_stage
            ON fmea_approvals (project_id, stage_no)
        """)

        # ═══ v7: 全量 DB 重构 — 新表 ═══

        # fmea_step_assignee_data: 5 步核心数据（每人一行，assignee=-1 为系统基线）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fmea_step_assignee_data (
                id                SERIAL PRIMARY KEY,
                project_id        INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                step_name         TEXT NOT NULL,
                assignee_user_id  INTEGER NOT NULL,
                data              JSONB NOT NULL,
                version           INTEGER NOT NULL DEFAULT 1,
                updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (project_id, step_name, assignee_user_id)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_assignee_data_lookup
            ON fmea_step_assignee_data (project_id, step_name)
        """)
        _add_schema_comments(cur, "fmea_step_assignee_data", {
            "id": "主键，自增整数",
            "project_id": "关联 projects.id，级联删除",
            "step_name": "步骤名: structure/function/failure/risk/optimization",
            "assignee_user_id": "-1=系统基线缓存, >0=工程师users.id",
            "data": "JSONB 数据",
            "version": "乐观锁版本号",
            "updated_at": "最后更新时间",
        })

        # fmea_step_data: 报告单独存放（无并发场景）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fmea_step_data (
                id          SERIAL PRIMARY KEY,
                project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                step_name   TEXT NOT NULL,
                data        JSONB NOT NULL,
                version     INTEGER NOT NULL DEFAULT 1,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (project_id, step_name, version)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_step_data_project
            ON fmea_step_data (project_id, step_name)
        """)
        _add_schema_comments(cur, "fmea_step_data", {
            "id": "主键，自增整数",
            "project_id": "关联 projects.id，级联删除",
            "step_name": "步骤名: report",
            "data": "JSONB 数据",
            "version": "乐观锁版本号",
        })

        # project_risk_files: 评分标准文件（替代 risk-files/*.md）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS project_risk_files (
                id          SERIAL PRIMARY KEY,
                project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                filename    TEXT NOT NULL,
                content     TEXT NOT NULL,
                file_type   TEXT NOT NULL DEFAULT 'markdown',
                uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (project_id, filename)
            )
        """)
        _add_schema_comments(cur, "project_risk_files", {
            "id": "主键，自增整数",
            "project_id": "关联 projects.id，级联删除",
            "filename": "文件名（含扩展名）",
            "content": "文件内容",
            "file_type": "文件类型: markdown/pdf/excel",
            "uploaded_at": "上传时间",
        })

        # fmea_risk_criteria: SOD/AP 评分标准
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fmea_risk_criteria (
                id          SERIAL PRIMARY KEY,
                project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                criteria    JSONB NOT NULL,
                version     INTEGER NOT NULL DEFAULT 1,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_risk_criteria_project
            ON fmea_risk_criteria (project_id)
        """)
        _add_schema_comments(cur, "fmea_risk_criteria", {
            "id": "主键，自增整数",
            "project_id": "关联 projects.id，级联删除",
            "criteria": "评分标准 JSON 对象",
            "version": "版本号",
        })

        # ═══ v8: 全局 Agent 配置表 ═══
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
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_agents_tier
            ON agents (tier)
        """)
        _add_schema_comments(cur, "agents", {
            "id": "主键，自增整数",
            "tier": "层级: 1=总负责人, 2=工作流编排, 3=领域专家",
            "name": "Agent 名称（全局唯一）",
            "name_en": "英文名称",
            "icon": "图标 emoji",
            "color": "卡片颜色值",
            "tier_label": "层级标签（总负责人/工作流编排/法规/BMS需求...）",
            "domain": "领域描述",
            "status": "状态: active/idle",
            "status_label": "状状态标签",
            "description": "完整描述文本",
            "responsibilities": "核心职责列表 JSON",
            "boundaries": "边界约束列表 JSON",
            "tools": "可用工具列表 JSON",
            "source_file": "代码文件路径",
            "weknora_kb_id": "专属知识库 ID",
            "sort_order": "排序序号",
        })

        # 填充默认角色
        for code, name, perms in DEFAULT_ROLES:
            cur.execute("SELECT 1 FROM roles WHERE code = %s", (code,))
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO roles (code, name, permissions, description) VALUES (%s, %s, %s, %s)",
                    (code, name, perms, name)
                )
                logger.info(f"✅ 已创建默认角色: {code} ({name})")
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _get_role_by_code(code: str):
    """通过 code 查角色"""
    conn = get_user_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM roles WHERE code = %s", (code,))
        return cur.fetchone()
    finally:
        try: conn.close()
        except: pass


def _user_has_permission(current_user: dict, perm: str) -> bool:
    """检查当前用户是否拥有指定权限（admin 拥有所有权限）"""
    if not current_user:
        return False
    role = current_user.get("role", "")
    if role == "admin":
        return True
    # 用户的 system role 是 admin/user，project role 是 leader/engineer/reviewer/manager
    # 这里通过 system role 判断：user 没特殊权限（默认只能看 FMEA）
    # 真正的 RBAC 通过 users.role_code 实现（每个成员都有自己的项目角色）
    return False


def hash_password(password: str) -> str:
    return hashlib.sha256((password + JWT_SECRET).encode()).hexdigest()

def verify_password(password: str, hash_str: str) -> bool:
    return hash_password(password) == hash_str

def create_token(user_data: dict) -> str:
    payload = {
        "user_id": user_data["id"],
        "username": user_data["username"],
        "role": user_data["role"],
        "display_name": user_data.get("display_name", ""),
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_token(token: str):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception:
        return None

async def get_current_user(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    payload = decode_token(auth[7:])
    return payload

def _load_planning_user_assoc(project_id: str):
    """读取项目的用户关联信息（从 project_members 表，兼容旧版 planning.json）"""
    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return None
    try:
        conn = get_user_db_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT user_id, role_code FROM project_members
            WHERE project_id = %s ORDER BY id
        """, (db_pid,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        owner_id = leader_id = None
        team_ids = []
        for r in rows:
            if r["role_code"] == "owner":
                owner_id = r["user_id"]
            elif r["role_code"] == "leader":
                leader_id = r["user_id"]
            else:
                team_ids.append(r["user_id"])
        return {
            "owner_user_id": owner_id,
            "leader_user_id": leader_id,
            "team_user_ids": team_ids,
        }
    except Exception:
        return None


def _is_node_changed(old: dict, new: dict) -> bool:
    """对比两个节点对象是否有变更（忽略 _collapsed 等 UI 内部状态）"""
    # 忽略 UI 内部状态字段
    ignore_keys = {"_collapsed"}
    old_filtered = {k: v for k, v in old.items() if k not in ignore_keys}
    new_filtered = {k: v for k, v in new.items() if k not in ignore_keys}
    # 比较关键字段
    return old_filtered != new_filtered


def _set_project_members(project_id: str, owner_user_id, leader_user_id, team_user_ids: list):
    """更新项目的成员关联（写入 project_members 表）"""
    conn = get_user_db_conn()
    try:
        pid = _resolve_db_project_id(project_id)
        if pid is None:
            conn.close()
            return False
        cur = conn.cursor()
        # 清空旧关联
        cur.execute("DELETE FROM project_members WHERE project_id = %s", (pid,))
        # 写入新关联
        # 1) owner
        if owner_user_id:
            cur.execute(
                "INSERT INTO project_members (project_id, user_id, role_code) VALUES (%s, %s, 'owner') "
                "ON CONFLICT (project_id, user_id) DO UPDATE SET role_code = 'owner'",
                (pid, owner_user_id)
            )
        # 2) leader
        if leader_user_id:
            cur.execute(
                "INSERT INTO project_members (project_id, user_id, role_code) VALUES (%s, %s, 'leader') "
                "ON CONFLICT (project_id, user_id) DO UPDATE SET role_code = 'leader'",
                (pid, leader_user_id)
            )
        # 3) team members（用各自的 role_code）
        for uid in team_user_ids or []:
            cur.execute("SELECT role_code FROM users WHERE id = %s", (uid,))
            r = cur.fetchone()
            if r:
                cur.execute(
                    "INSERT INTO project_members (project_id, user_id, role_code) VALUES (%s, %s, %s) "
                    "ON CONFLICT (project_id, user_id) DO UPDATE SET role_code = EXCLUDED.role_code",
                    (pid, uid, r[0])
                )
        conn.commit()
        cur.close(); conn.close()
        return True
    except Exception as e:
        logger.error(f"写入 project_members 失败: {e}")
        conn.rollback()
        cur.close(); conn.close()
        return False


def _resolve_db_project_id(project_id: str) -> Optional[int]:
    """将字符串 project_id (projects.id 或 projects.name) 解析为 projects 表的整数 ID."""
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                # 优先按 id（整数字符串）查
                try:
                    pid_int = int(project_id)
                    cur.execute("SELECT id FROM projects WHERE id = %s", (pid_int,))
                    row = cur.fetchone()
                    if row:
                        return int(row[0])
                except ValueError:
                    pass
                # fallback：按 name 查（兼容旧数据）
                cur.execute("SELECT id FROM projects WHERE name = %s", (project_id,))
                row = cur.fetchone()
                if row:
                    return int(row[0])
    except Exception as e:
        logger.warning(f"[DB] _resolve_db_project_id({project_id}) 失败: {e}")
    return None


def _update_leader_status(db_pid: int, stage_no: int, user_id: int,
                          lead_status: str, lead_comment: str = "",
                          also_approve_engineer: bool = False) -> bool:
    """在 fmea_approvals 表上更新负责人审核状态.

    Args:
        db_pid: projects.id (整数)
        stage_no: 阶段编号
        user_id: users.id (整数)
        lead_status: pending / read / approved / rejected
        lead_comment: 审核意见
        also_approve_engineer: True 时同步把工程师的 status 置为 'approved' (用于 leader-approve)

    Returns:
        True=更新成功, False=记录不存在或失败
    """
    if db_pid is None or not user_id:
        return False
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                if also_approve_engineer:
                    cur.execute("""
                        UPDATE fmea_approvals
                        SET lead_status = %s,
                            lead_comment = %s,
                            lead_reviewed_at = NOW(),
                            status = 'approved',
                            updated_at = NOW()
                        WHERE project_id = %s AND user_id = %s AND stage_no = %s
                    """, (lead_status, lead_comment, db_pid, user_id, stage_no))
                else:
                    cur.execute("""
                        UPDATE fmea_approvals
                        SET lead_status = %s,
                            lead_comment = %s,
                            lead_reviewed_at = NOW(),
                            updated_at = NOW()
                        WHERE project_id = %s AND user_id = %s AND stage_no = %s
                    """, (lead_status, lead_comment, db_pid, user_id, stage_no))
                updated = cur.rowcount > 0
            conn.commit()
        return updated
    except Exception as e:
        logger.exception(f"[Approval] _update_leader_status 失败: {e}")
        return False


def _get_member_approval_row(db_pid: int, stage_no: int, user_id: int) -> Optional[dict]:
    """从 fmea_approvals 表读出 (status, lead_status, ...) 行.
    返回 dict 或 None (无记录时).
    """
    if db_pid is None or not user_id:
        return None
    try:
        with get_user_db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT a.status, a.lead_status, a.lead_comment, a.comment,
                           a.approved_at, a.lead_reviewed_at,
                           u.display_name, u.username
                    FROM fmea_approvals a
                    JOIN users u ON u.id = a.user_id
                    WHERE a.project_id = %s AND a.user_id = %s AND a.stage_no = %s
                """, (db_pid, user_id, stage_no))
                row = cur.fetchone()
                if row:
                    return dict(row)
    except Exception as e:
        logger.exception(f"[Approval] _get_member_approval_row 失败: {e}")
    return None


def _is_all_lead_approved(db_pid: int, stage_no: int) -> bool:
    """检查某阶段全部指派工程师的负责人审核是否通过 (fmea_approvals.lead_status='approved')."""
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE lead_status = 'approved') AS approved,
                        COUNT(*) AS total
                    FROM fmea_approvals
                    WHERE project_id = %s AND stage_no = %s
                """, (db_pid, stage_no))
                row = cur.fetchone()
                if not row:
                    return False
                approved = int(row[0] or 0)
                total = int(row[1] or 0)
                return total > 0 and approved >= total
    except Exception as e:
        logger.exception(f"[Approval] _is_all_lead_approved 失败: {e}")
        return False


def _init_fmea_approvals(project_id: str, owner_user_id=None, leader_user_id=None,
                         team_user_ids: list = None, stage_no: int = None):
    """v20.5: 按阶段生成审批记录（idempotent）.

    stage_no 指定时只生成该阶段的审批行；为 None 时全部阶段 1-6。
    数据来源: 对应步骤 baseline 数据里的 assignee_user_id 去重
    (结构→nodes / 功能→domain_rows / 失效/风险/优化→items),
    而不是 project_members 里的全部成员——因为"实际参与该步骤的工程师"
    与"项目成员"不一定一致（例如新增成员但未指派任何节点）。
    """
    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        logger.warning(f"[Approval] _init_fmea_approvals: 项目 {project_id} 无整数 ID, 跳过")
        return
    stages = [stage_no] if stage_no else [1, 2, 3, 4, 5, 6]  # 1=结构 2=功能 3=失效 4=风险 5=优化 6=报告

    # stage_no → (step_name, 列表字段, items 中的 assignee 字段)
    # 结构: nodes[].assignee_user_id; 其他: items 或 domain_rows 中的 assignee_user_id
    STAGE_TO_DATA = {
        1: ("structure", "nodes", "assignee_user_id"),
        2: ("function",  "items", "assignee_user_id"),   # 也可能在 domain_rows
        3: ("failure",   "items", "assignee_user_id"),
        4: ("risk",      "items", "assignee_user_id"),
        5: ("optimization", "items", "assignee_user_id"),
        # 6 = 报告, 无推送流程, 不生成
    }

    def _extract_assignees(step_data: dict, list_key: str, uid_key: str) -> set:
        uids = set()
        items = step_data.get(list_key) or []
        for it in items:
            uid = it.get(uid_key)
            if uid is None or uid == "" or uid == -1:
                continue
            try:
                uids.add(int(uid))
            except (ValueError, TypeError):
                continue
        return uids

    inserted = 0
    try:
        conn = get_user_db_conn()
        try:
            with conn.cursor() as cur:
                # 1) 已有 (user_id, stage_no) 集合（仅限目标阶段）
                placeholders = ",".join(["%s"] * len(stages))
                cur.execute(f"""
                    SELECT user_id, stage_no FROM fmea_approvals
                    WHERE project_id = %s AND stage_no IN ({placeholders})
                """, (db_pid, *stages))
                existing = {(int(r[0]), int(r[1])) for r in cur.fetchall()}

                from engine.tools.fmea_helpers import get_baseline
                total_target = 0
                for stage in stages:
                    if stage not in STAGE_TO_DATA:
                        continue
                    step_name, list_key, uid_key = STAGE_TO_DATA[stage]
                    baseline = get_baseline(db_pid, step_name)
                    if not baseline or not isinstance(baseline, dict):
                        logger.info(f"[Approval] 项目 {db_pid} 阶段 {stage} 无 baseline 数据, 跳过")
                        continue
                    # 优先用 list_key, 如果 items 为空再试 domain_rows (function 步骤)
                    assignee_uids = _extract_assignees(baseline, list_key, uid_key)
                    if not assignee_uids and step_name == "function":
                        assignee_uids = _extract_assignees(baseline, "domain_rows", uid_key)
                    if not assignee_uids:
                        logger.info(f"[Approval] 项目 {db_pid} 阶段 {stage} ({step_name}) 无指派工程师")
                        continue
                    total_target += len(assignee_uids)
                    for uid in assignee_uids:
                        if (uid, stage) in existing:
                            continue
                        cur.execute("""
                            INSERT INTO fmea_approvals (project_id, user_id, stage_no, status)
                            VALUES (%s, %s, %s, 'pending')
                            ON CONFLICT (project_id, user_id, stage_no) DO NOTHING
                        """, (db_pid, uid, stage))
                        inserted += 1
            conn.commit()
        finally:
            conn.close()
        logger.info(f"[Approval] 项目 {db_pid} 阶段 {stages} 补齐审批行: 新增 {inserted} 条（目标 {total_target} 名指派工程师）")
    except Exception as e:
        logger.error(f"[Approval] _init_fmea_approvals 失败: {e}")


def user_can_access_project(current_user: dict, project_id: str) -> bool:
    """检查当前用户是否有权访问该项目（管理员全部可见；普通用户必须是 owner/leader/team）"""
    if not current_user:
        return False
    if current_user.get("role") == "admin":
        return True
    assoc = _load_planning_user_assoc(project_id)
    if not assoc:
        return False
    uid = current_user.get("user_id")
    if uid == assoc["owner_user_id"] or uid == assoc["leader_user_id"]:
        return True
    if uid in (assoc["team_user_ids"] or []):
        return True
    return False

# 初始化用户表
init_user_db()
init_role_tables()

from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse, Response

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=4, max_length=100)
    display_name: str = Field("", max_length=50)

class LoginRequest(BaseModel):
    username: str = Field(...)
    password: str = Field(...)

@app.post("/api/auth/register")
async def register(request: Request):
    """注册新用户"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是有效 JSON"}, status_code=400)
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", "")).strip()
    display_name = str(body.get("display_name", "")).strip() or username
    if len(username) < 2:
        return JSONResponse({"error": "用户名至少2个字符"}, status_code=400)
    if len(password) < 4:
        return JSONResponse({"error": "密码至少4个字符"}, status_code=400)
    conn = get_user_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        existing = cur.fetchone()
        if existing:
            return JSONResponse({"error": "用户名已存在"}, status_code=409)
        pw_hash = hash_password(password)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (username, password_hash, display_name, role) VALUES (%s, %s, %s, 'user') RETURNING id",
            (username, pw_hash, display_name)
        )
        user_id = cur.fetchone()[0]
        conn.commit()
        user_data = {"id": user_id, "username": username, "role": "user", "display_name": display_name}
        token = create_token(user_data)
        logger.info(f"新用户注册: {username} (ID: {user_id})")
        return JSONResponse({"token": token, "user": user_data})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        try: conn.close()
        except: pass


@app.post("/api/auth/login")
async def login(request: Request):
    """用户登录（邮箱+密码，兼容用户名密码）"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是有效 JSON"}, status_code=400)
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", "")).strip()
    if not username or not password:
        return JSONResponse({"error": "请填写邮箱和密码"}, status_code=400)
    conn = get_user_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # 优先按邮箱匹配, 兼容用户名
        cur.execute("SELECT * FROM users WHERE email = %s OR username = %s", (username, username))
        row = cur.fetchone()
        if row:
            row_pass = row["password_hash"]
            if not verify_password(password, row_pass):
                return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
            user_data = {
                "id": row["id"],
                "username": row["username"],
                "role": row["role_code"] or row["role"] or "engineer",
                "display_name": row["display_name"]
            }
            token = create_token(user_data)
            return JSONResponse({"token": token, "user": user_data})

        # ── 未找到本地用户 → 尝试 WeKnora 认证 ──
        weknora_user = _authenticate_via_weknora(username, password)
        if weknora_user:
            # 同步到 fmea 本地 users 表
            weknora_id = weknora_user.get("id")
            display_name = weknora_user.get("username", username)
            email = weknora_user.get("email", "")
            # 用 WeKnora 的密码哈希（如果可用）或生成一个随机密码
            wk_pw_hash = weknora_user.get("password_hash", "")
            if not wk_pw_hash:
                wk_pw_hash = hash_password(password)

            # 检查是否已有相同 email 或 weknora_id 的记录
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            existing = cur.fetchone()
            if existing:
                user_id = existing["id"]
                cur.execute(
                    "UPDATE users SET password_hash=%s, email=%s, display_name=%s WHERE id=%s",
                    (wk_pw_hash, email, display_name, user_id)
                )
            else:
                safe_username = username.lower().replace(" ", "_")
                pw_for_fmea = hash_password(password)
                cur.execute(
                    """INSERT INTO users (username, password_hash, display_name, email, role, role_code)
                       VALUES (%s, %s, %s, %s, 'user', 'engineer') RETURNING id""",
                    (safe_username, pw_for_fmea, display_name, email)
                )
                user_id = cur.fetchone()["id"]
            conn.commit()

            # 查本地 users 表的 role_code
            cur.execute("SELECT role_code FROM users WHERE username = %s", (safe_username,))
            local_role = cur.fetchone()
            role_val = local_role["role_code"] if local_role and local_role["role_code"] else "engineer"
            user_data = {
                "id": user_id,
                "username": username,
                "role": role_val,
                "display_name": display_name,
            }
            token = create_token(user_data)
            logger.info(f"[WeKnora] 用户 {username} 通过 WeKnora 认证登录 (fmea_id={user_id})")
            return JSONResponse({"token": token, "user": user_data})

        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    except Exception as e:
        logger.error(f"[Login] 登录失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        try: conn.close()
        except: pass


def _authenticate_via_weknora(username: str, password: str) -> Optional[dict]:
    """通过 WeKnora 数据库认证用户（共用用户表）"""
    try:
        wk_conn = psycopg2.connect(
            host=os.getenv("WEKNORA_DB_HOST", "10.180.17.11"),
            port=int(os.getenv("WEKNORA_DB_PORT", "5433")),
            dbname=os.getenv("WEKNORA_DB_NAME", "WeKnora"),
            user=os.getenv("WEKNORA_DB_USER", "postgres"),
            password=os.getenv("WEKNORA_DB_PASSWORD", "postgres123!@#"),
            connect_timeout=5,
        )
        wk_cur = wk_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # 支持用户名或邮箱登录
        wk_cur.execute(
            "SELECT * FROM users WHERE username = %s OR email = %s",
            (username, username)
        )
        wk_user = wk_cur.fetchone()
        wk_cur.close()
        wk_conn.close()

        if not wk_user:
            return None
        if not wk_user.get("is_active", True):
            return None

        # WeKnora 使用 bcrypt 验证密码
        from bcrypt import checkpw
        if not checkpw(bytes(password.encode()), bytes(wk_user["password_hash"].encode())):
            return None
        return dict(wk_user)
    except Exception as e:
        logger.warning(f"[WeKnora] 认证异常: {e}")
        return None

@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    return JSONResponse({"user": user})

@app.get("/api/auth/users")
async def list_users(request: Request):
    """列出所有用户（供前端@选择负责人/团队成员）"""
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    conn = get_user_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, username, display_name, role FROM users ORDER BY id")
        rows = cur.fetchall()
        users = []
        for r in rows:
            users.append({"id": r["id"], "username": r["username"], "display_name": r["display_name"], "role": r["role"]})
        return JSONResponse(users)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        try: conn.close()
        except: pass


# ════════════════════════════════════════════════════════════════
# 角色管理 CRUD（仅管理员可操作）
# ════════════════════════════════════════════════════════════════

def _require_admin(current_user: dict):
    """检查用户是否为管理员，否则返回错误响应"""
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if current_user.get("role") != "admin":
        return JSONResponse({"error": "需要管理员权限"}, status_code=403)
    return None


def _parse_json_field(value):
    """解析 JSON 字符串/列表为 Python 对象"""
    import json as _json
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return _json.loads(value)
        except Exception:
            return []
    return []


@app.get("/api/roles")
async def api_list_roles(request: Request):
    """列出所有角色"""
    user = await get_current_user(request)
    err = _require_admin(user)
    if err: return err
    conn = get_user_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, code, name, permissions, description, created_at FROM roles ORDER BY id")
        rows = cur.fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"], "code": r["code"], "name": r["name"],
                "permissions": r["permissions"] if isinstance(r["permissions"], list) else _parse_json_field(r["permissions"]),
                "description": r["description"], "created_at": str(r["created_at"])
            })
        return JSONResponse(result)
    finally:
        try: conn.close()
        except: pass


@app.post("/api/roles")
async def api_create_role(request: Request):
    """创建新角色"""
    user = await get_current_user(request)
    err = _require_admin(user)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是有效 JSON"}, status_code=400)
    import json as _json
    code = str(body.get("code", "")).strip()
    name = str(body.get("name", "")).strip()
    description = str(body.get("description", "")).strip()
    permissions = body.get("permissions", [])
    if not code or not name:
        return JSONResponse({"error": "code 和 name 不能为空"}, status_code=400)
    if not isinstance(permissions, list):
        return JSONResponse({"error": "permissions 必须是数组"}, status_code=400)
    conn = get_user_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT 1 FROM roles WHERE code = %s", (code,))
        if cur.fetchone():
            return JSONResponse({"error": "角色 code 已存在"}, status_code=409)
        cur.execute(
            "INSERT INTO roles (code, name, permissions, description) VALUES (%s, %s::jsonb, %s, %s) RETURNING id",
            (code, _json.dumps(permissions), name, description)
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        logger.info(f"创建角色: {code} ({name})")
        return JSONResponse({"id": new_id, "code": code, "name": name, "permissions": permissions, "description": description})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        try: conn.close()
        except: pass


@app.put("/api/roles/{role_id}")
async def api_update_role(role_id: int, request: Request):
    """更新角色"""
    user = await get_current_user(request)
    err = _require_admin(user)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是有效 JSON"}, status_code=400)
    import json as _json
    conn = get_user_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM roles WHERE id = %s", (role_id,))
        if not cur.fetchone():
            return JSONResponse({"error": "角色不存在"}, status_code=404)
        updates = []
        params = []
        if "name" in body:
            updates.append("name = %s")
            params.append(str(body["name"]).strip())
        if "description" in body:
            updates.append("description = %s")
            params.append(str(body["description"]).strip())
        if "permissions" in body:
            perms = body["permissions"]
            if not isinstance(perms, list):
                return JSONResponse({"error": "permissions 必须是数组"}, status_code=400)
            updates.append("permissions = %s::jsonb")
            params.append(_json.dumps(perms))
        if not updates:
            return JSONResponse({"error": "没有可更新的字段"}, status_code=400)
        params.append(role_id)
        sql = f"UPDATE roles SET {', '.join(updates)} WHERE id = %s"
        cur.execute(sql, params)
        conn.commit()
        return JSONResponse({"status": "updated", "id": role_id})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        try: conn.close()
        except: pass


@app.delete("/api/roles/{role_id}")
async def api_delete_role(role_id: int, request: Request):
    """删除角色"""
    user = await get_current_user(request)
    err = _require_admin(user)
    if err: return err
    conn = get_user_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT code FROM roles WHERE id = %s", (role_id,))
        row = cur.fetchone()
        if not row:
            return JSONResponse({"error": "角色不存在"}, status_code=404)
        code = row["code"]
        if code in ("admin", "leader", "engineer", "reviewer", "manager"):
            return JSONResponse({"error": f"内置角色 '{code}' 不能删除"}, status_code=400)
        cur.execute("DELETE FROM roles WHERE id = %s", (role_id,))
        conn.commit()
        logger.info(f"删除角色: {code}")
        return JSONResponse({"status": "deleted", "id": role_id})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        try: conn.close()
        except: pass


@app.get("/api/permissions")
async def api_list_permissions(request: Request):
    """列出所有可用权限"""
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    return JSONResponse(DEFAULT_PERMISSIONS)


# ════════════════════════════════════════════════════════════════
# 团队成员管理 CRUD（仅管理员可操作）
# ════════════════════════════════════════════════════════════════

@app.get("/api/projects/{project_id}/assignable-users")
async def api_assignable_users(project_id: str, request: Request):
    """v19.9: 列出本项目可指派的用户（工程师 + 负责人，来自 project_members 表）"""
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    pid = _resolve_db_project_id(project_id)
    if pid is None:
        return JSONResponse([])
    conn = get_user_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # 只取 engineer / leader（不含 owner，owner 是项目创建者一般不参与节点编辑）
        cur.execute("""
            SELECT u.id, u.display_name AS name, u.email, pm.role_code AS project_role,
                   r.name AS role_name
            FROM project_members pm
            JOIN users u ON u.id = pm.user_id
            LEFT JOIN roles r ON r.code = u.role_code
            WHERE pm.project_id = %s AND pm.role_code IN ('engineer', 'leader')
            ORDER BY CASE pm.role_code WHEN 'leader' THEN 0 ELSE 1 END, pm.user_id
        """, (pid,))
        rows = cur.fetchall()
        result = [{
            "id": int(r["id"]),
            "name": r["name"],
            "email": r["email"] or "",
            "role_code": r["project_role"],
            "role_name": r["role_name"] or r["project_role"],
        } for r in rows]
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"[assignable-users] 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        try: conn.close()
        except: pass


@app.get("/api/team/members")
async def api_list_team_members(request: Request):
    """列出所有团队成员（JOIN users + roles，一次拿全）"""
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    conn = get_user_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT u.id,
                   u.display_name AS name,
                   u.email,
                   u.email_auth_code,
                   u.kb_auth_code,
                   u.role_code,
                   r.name AS role_name,
                   u.dept,
                   u.active,
                   u.created_at
            FROM users u
            LEFT JOIN roles r ON r.code = u.role_code
            ORDER BY u.id
        """)
        rows = cur.fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"], "name": r["name"], "email": r["email"],
                "email_auth_code": r["email_auth_code"],
                "kb_auth_code": r["kb_auth_code"] or "",
                "role_code": r["role_code"],
                "role_name": r["role_name"] or r["role_code"],
                "dept": r["dept"], "active": bool(r["active"]),
                "created_at": str(r["created_at"])
            })
        return JSONResponse(result)
    finally:
        try: conn.close()
        except: pass


@app.post("/api/team/members")
async def api_create_team_member(request: Request):
    """新增团队成员（写入 users 表）"""
    user = await get_current_user(request)
    err = _require_admin(user)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是有效 JSON"}, status_code=400)
    name = str(body.get("name", "")).strip()
    email = str(body.get("email", "")).strip()
    email_auth_code = str(body.get("email_auth_code", "")).strip()
    kb_auth_code = str(body.get("kb_auth_code", "")).strip()
    role_code = str(body.get("role_code", "engineer")).strip()
    dept = str(body.get("dept", "")).strip()
    active = bool(body.get("active", True))
    if not name:
        return JSONResponse({"error": "姓名不能为空"}, status_code=400)
    if not email:
        return JSONResponse({"error": "邮箱不能为空"}, status_code=400)
    if role_code == "leader" and not email_auth_code:
        return JSONResponse({"error": "项目负责人必须填写邮箱授权码"}, status_code=400)
    if role_code == "leader" and not kb_auth_code:
        return JSONResponse({"error": "项目负责人必须填写知识库授权码"}, status_code=400)
    conn = get_user_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # 校验 role_code 存在
        cur.execute("SELECT 1 FROM roles WHERE code = %s", (role_code,))
        if not cur.fetchone():
            return JSONResponse({"error": f"角色 '{role_code}' 不存在"}, status_code=400)
        # 自动生成 username（邮箱前缀 + 随机后缀）
        username = (email.split("@")[0] if email else name.lower().replace(" ", "_"))
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        if cur.fetchone():
            import random
            username = f"{username}_{random.randint(1000,9999)}"
        import hashlib
        default_pw = hashlib.sha256("fmea123".encode()).hexdigest()
        cur.execute(
            """INSERT INTO users (username, password_hash, display_name, email, email_auth_code,
                                 kb_auth_code, role_code, dept, active, role)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'user') RETURNING id""",
            (username, default_pw, name, email, email_auth_code, kb_auth_code, role_code, dept, active)
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        logger.info(f"创建团队成员: {name} (role={role_code})")
        return JSONResponse({"id": new_id, "name": name, "email": email, "role_code": role_code, "dept": dept, "active": active})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        try: conn.close()
        except: pass


@app.put("/api/team/members/{member_id}")
async def api_update_team_member(member_id: int, request: Request):
    """更新团队成员（更新 users 表）"""
    user = await get_current_user(request)
    err = _require_admin(user)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是有效 JSON"}, status_code=400)
    conn = get_user_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM users WHERE id = %s", (member_id,))
        current = cur.fetchone()
        if not current:
            return JSONResponse({"error": "成员不存在"}, status_code=404)

        # ── 校验 ──
        if "email" in body:
            email_val = str(body["email"]).strip()
            if not email_val:
                return JSONResponse({"error": "邮箱不能为空"}, status_code=400)
        target_role = body.get("role_code", current["role_code"])
        target_auth = body.get("email_auth_code", current["email_auth_code"] or "")
        target_kb_auth = body.get("kb_auth_code", current["kb_auth_code"] or "")
        if target_role == "leader" and not target_auth:
            return JSONResponse({"error": "项目负责人必须填写邮箱授权码"}, status_code=400)
        if target_role == "leader" and not target_kb_auth:
            return JSONResponse({"error": "项目负责人必须填写知识库授权码"}, status_code=400)

        updates, params = [], []
        # 前端传的 'name' 对应 users.display_name
        if "name" in body:
            updates.append("display_name = %s")
            params.append(str(body["name"]).strip())
        for fld in ("email", "email_auth_code", "kb_auth_code", "role_code", "dept"):
            if fld in body:
                updates.append(f"{fld} = %s")
                params.append(str(body[fld]).strip())
        if "active" in body:
            val = bool(body["active"])
            updates.append("active = %s")
            params.append(val)
        if not updates:
            return JSONResponse({"error": "没有可更新的字段"}, status_code=400)
        params.append(member_id)
        sql = f"UPDATE users SET {', '.join(updates)} WHERE id = %s"
        cur.execute(sql, params)
        conn.commit()
        return JSONResponse({"status": "updated", "id": member_id})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        try: conn.close()
        except: pass


@app.delete("/api/team/members/{member_id}")
async def api_delete_team_member(member_id: int, request: Request):
    """删除团队成员（删除 users 表记录）"""
    user = await get_current_user(request)
    err = _require_admin(user)
    if err: return err
    conn = get_user_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT display_name FROM users WHERE id = %s AND role_code != 'admin'", (member_id,))
        row = cur.fetchone()
        if not row:
            return JSONResponse({"error": "成员不存在"}, status_code=404)
        name = row["display_name"]
        cur.execute("DELETE FROM users WHERE id = %s", (member_id,))
        conn.commit()
        logger.info(f"删除团队成员: {name}")
        return JSONResponse({"status": "deleted", "id": member_id})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        try: conn.close()
        except: pass


# ════════════════════════════════════════════════════════════════
# REST API (供 Chat UI 调用)
# ════════════════════════════════════════════════════════════════

from fastapi import Request, Body
from fastapi.responses import JSONResponse, Response, FileResponse
from pydantic import BaseModel, Field


class PipelineRunRequest(BaseModel):
    """批量运行请求 — 不设置默认值，所有参数由调用方提供"""
    project_name: str = Field(..., description="项目名称")
    fmea_type: str = Field("DFMEA", description="FMEA类型: DFMEA | PFMEA")
    part_number: str = Field("", description="零件号")
    team_members: str = Field("", description="团队成员，逗号分隔")
    bom_path: str = Field("", description="BOM文件绝对路径")
    sor_paths: list = Field(default_factory=list, description="SOR文件路径列表")


class CreateProjectRequest(BaseModel):
    """创建项目请求"""
    name: str = Field(..., description="项目名称")
    fmea_type: str = Field("DFMEA", description="FMEA 类型")
    description: str = Field("", description="项目描述")
    user_id: Optional[int] = Field(None, description="创建者用户ID")
    leader_user_id: Optional[int] = Field(None, description="负责人用户ID")
    team_user_ids: list = Field(default_factory=list, description="团队成员用户ID列表")


class RenameProjectRequest(BaseModel):
    """重命名项目请求"""
    name: str = Field(..., min_length=1, description="新项目名称")


@app.get("/")
async def serve_dashboard():
    """管理控制台首页"""
    index_html = PROJECT_ROOT / "index.html"
    if index_html.exists():
        return FileResponse(str(index_html))
    return JSONResponse({"error": "index.html not found"}, status_code=404)


# ── 静态资源 ──
LOGO_PATH = PROJECT_ROOT / "static" / "logo.png"

@app.get("/static/logo.png")
async def serve_logo():
    """服务企业 logo"""
    if LOGO_PATH.exists():
        return FileResponse(str(LOGO_PATH), media_type="image/png")
    return JSONResponse({"error": "logo not found"}, status_code=404)


@app.get("/login")
async def serve_login():
    """登录页面"""
    login_html = PROJECT_ROOT / "login.html"
    if login_html.exists():
        return FileResponse(str(login_html))
    return JSONResponse({"error": "login.html not found"}, status_code=404)


@app.get("/team")
async def serve_team():
    """团队管理页面"""
    team_html = PROJECT_ROOT / "team.html"
    if team_html.exists():
        return FileResponse(str(team_html))
    return JSONResponse({"error": "team.html not found"}, status_code=404)


@app.get("/agent")
async def serve_agent():
    """智能体管理页面"""
    agent_html = PROJECT_ROOT / "agent.html"
    if agent_html.exists():
        return FileResponse(str(agent_html))
    return JSONResponse({"error": "agent.html not found"}, status_code=404)


@app.get("/chat")
async def serve_chat():
    """聊天页面"""
    chat_html = PROJECT_ROOT / "chat.html"
    if chat_html.exists():
        return FileResponse(str(chat_html))
    return JSONResponse({"error": "chat.html not found"}, status_code=404)


@app.get("/api/pipeline/status")
async def pipeline_status(project_id: str = ""):
    """查询 Pipeline 运行状态"""
    pipeline = get_or_create_pipeline()
    if not pipeline.project:
        return JSONResponse({"status": "idle", "message": "无活跃项目"})

    return JSONResponse({
        "status": "running",
        "project": pipeline.project,
        "current_step": pipeline.project.get("current_step", 0),
    })


@app.post("/api/pipeline/run")
async def pipeline_run(req: PipelineRunRequest):
    """触发批量运行"""
    pipeline = get_or_create_pipeline()

    # 构建项目信息
    project = {
        "name": req.project_name,
        "fmea_type": req.fmea_type,
        "part_number": req.part_number,
        "team_members": [t.strip() for t in req.team_members.split(",") if t.strip()],
    }

    # 读取SOR
    sor_content = ""
    for sp in req.sor_paths:
        sor_content += read_file(sp) + "\n\n"

    # 验证结构树已生成（仅走 WeKnora 提取）
    pd = pipeline._resolve_project_dir(project.get("id", "temp"))
    struct_path = pd / "fmea" / "01-structure-tree.json"
    if not struct_path.exists():
        return JSONResponse({
            "success": False,
            "error": f"结构树文件不存在（{struct_path}），请先调用 extract_structure_tree_tool（走 WeKnora）",
        }, status_code=400)

    try:
        report = await pipeline.run_full_pipeline(project, sor_content)
        summary = report.get_summary()
        return JSONResponse({
            "success": True,
            "summary": summary,
            "report_id": report.project_id,
        })
    except Exception as e:
        logger.error(f"Pipeline run failed: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e),
        }, status_code=500)


@app.get("/api/projects")
async def list_projects(request: Request):
    """列出所有项目（v7: 完全从 DB 读取）"""
    current_user = await get_current_user(request)
    is_admin = current_user and current_user.get("role") == "admin"
    user_id = current_user.get("user_id") if current_user else None
    result = []
    try:
        conn = get_user_db_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # 1) 从 projects 表查询所有项目（v7: 扩展字段）
        cur.execute("""
            SELECT id, name, description, subject, part_number, product_type,
                   fmea_type, current_step, status, owner_user_id, leader_user_id,
                   weknora_kb_id, sor_folder, created_at, updated_at
            FROM projects
            ORDER BY id DESC
        """)
        db_projects = cur.fetchall()

        # 2) 取出用户-项目关联（用于权限过滤）
        assoc_map = {}
        cur.execute("""
            SELECT pm.project_id, pm.user_id, pm.role_code
            FROM project_members pm
        """)
        for r in cur.fetchall():
            pid = r["project_id"]
            assoc = assoc_map.setdefault(pid, {"owner_id": None, "leader_id": None, "team_ids": []})
            if r["role_code"] == "owner":
                assoc["owner_id"] = r["user_id"]
            elif r["role_code"] == "leader":
                assoc["leader_id"] = r["user_id"]
            else:
                assoc["team_ids"].append(r["user_id"])

        # 3) 批量查所有项目的步骤存在性
        cur.execute("""
            SELECT project_id, step_name
            FROM fmea_step_assignee_data
            WHERE assignee_user_id = -1
        """)
        step_presence = {}  # {pid: {step_name: True}}
        for r in cur.fetchall():
            step_presence.setdefault(r["project_id"], {})[r["step_name"]] = True

        # 4) 批量查所有项目的 report 存在性
        # v17.5: 改查 fmea_step_assignee_data（v17.2 起报告写新表，旧 fmea_step_data 表不再有报告行）
        cur.execute("""
            SELECT DISTINCT project_id FROM fmea_step_assignee_data
            WHERE step_name = 'report' AND assignee_user_id = -1
        """)
        report_presence = {r["project_id"] for r in cur.fetchall()}

        # 5) 批量查所有项目的 risk_criteria 存在性
        cur.execute("SELECT DISTINCT project_id FROM fmea_risk_criteria")
        rc_presence = {r["project_id"] for r in cur.fetchall()}

        cur.close(); conn.close()
    except Exception as e:
        logger.error(f"[list_projects] 查询 projects 表失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

    for proj in db_projects:
        pid = proj["id"]
        # 权限过滤
        if not is_admin and user_id:
            assoc = assoc_map.get(pid, {})
            if (user_id != assoc.get("owner_id")
                and user_id != assoc.get("leader_id")
                and user_id not in assoc.get("team_ids", [])):
                continue

        assoc = assoc_map.get(pid, {})
        steps = step_presence.get(pid, {})
        has_report = pid in report_presence
        has_rc = pid in rc_presence

        # ── 推算项目状态 ──
        if has_report:
            project_status = "已完成"
        elif steps.get("optimization"):
            project_status = "进行中"
        elif steps.get("structure"):
            project_status = "已结构"
        elif pid in step_presence:  # 有任何步骤
            project_status = "已规划"
        else:
            project_status = "未规划"

        # step_name → 文件名映射
        step_file_map = {
            "planning": "00-planning",  # 规划在 projects 表，不在 fmea_step_assignee_data
            "structure": "01-structure-tree",
            "function": "02-function",
            "failure": "03-failure",
            "risk": "04-risk",
            "optimization": "05-optimization",
            "report": "06-report",  # v17.6: 补上；之前缺失导致 has_report 块的 06-report 重复累加，file_steps 变 8
        }
        # 规划一定存在（projects 表有这一行即代表已规划）
        file_steps = {"00-planning": True}
        # 各步骤存在性
        for s in steps:
            fk = step_file_map.get(s, s)
            if fk not in file_steps:
                file_steps[fk] = True
        # report 存在性（已含在 step_file_map 映射里；如果 has_report 出现，file_steps["06-report"] 已存在则不变）
        # v17.6: 移除重复累加逻辑——step_file_map["report"]="06-report" 已覆盖
        # if has_report:
        #     file_steps["06-report"] = True

        result.append({
            "id": pid,                  # URL 路径用 DB 主键
            "name": proj["name"],
            "description": proj.get("description", ""),
            "subject": proj.get("subject", ""),
            "partNumber": proj.get("part_number", ""),
            "productType": proj.get("product_type", ""),
            "fmeaType": proj.get("fmea_type", "DFMEA"),
            "currentStep": proj.get("current_step", 0),
            "status": proj.get("status", "planning"),
            "weknoraKbId": proj.get("weknora_kb_id", ""),
            "sorFolder": proj.get("sor_folder", ""),
            "steps": file_steps,
            "stepCount": len(file_steps),
            "projectStatus": project_status,
            "initStatus": "已初始化" if has_rc else None,
            "kbProjectId": proj.get("weknora_kb_id", ""),
            "kbDir": "",
            "ownerUserId": assoc.get("owner_id"),
            "leaderUserId": assoc.get("leader_id"),
            "teamUserIds": assoc.get("team_ids", []),
            "createdAt": str(proj.get("created_at")) if proj.get("created_at") else None,
            "updatedAt": str(proj.get("updated_at")) if proj.get("updated_at") else None,
        })
    return JSONResponse(result)


@app.get("/api/projects/search")
async def search_projects(request: Request, q: str = ""):
    """模糊搜索项目（从 projects 表 + 权限过滤）"""
    current_user = await get_current_user(request)
    is_admin = current_user and current_user.get("role") == "admin"
    user_id = current_user.get("user_id") if current_user else None
    query = q.strip().lower() if q else ""
    result = []
    try:
        conn = get_user_db_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # 1) 从 projects 表搜索
        if query:
            cur.execute("""
                SELECT p.id, p.name, p.description, p.created_at, p.updated_at
                FROM projects p
                WHERE LOWER(p.name) LIKE %s
                ORDER BY p.id DESC
            """, (f"%{query}%",))
        else:
            cur.execute("""
                SELECT p.id, p.name, p.description, p.created_at, p.updated_at
                FROM projects p
                ORDER BY p.id DESC
            """)
        db_projects = cur.fetchall()
        # 2) 用户-项目关联
        assoc_map = {}
        cur.execute("SELECT pm.project_id, pm.user_id, pm.role_code FROM project_members pm")
        for r in cur.fetchall():
            pid = r["project_id"]
            assoc = assoc_map.setdefault(pid, {"owner_id": None, "leader_id": None, "team_ids": []})
            if r["role_code"] == "owner":
                assoc["owner_id"] = r["user_id"]
            elif r["role_code"] == "leader":
                assoc["leader_id"] = r["user_id"]
            else:
                assoc["team_ids"].append(r["user_id"])
        # 3) v18: 各项目已完成步骤从 DB 统计（不再扫本地目录）
        steps_map = {}
        try:
            cur.execute("""
                SELECT s.project_id, s.step_name, COUNT(*) AS cnt
                FROM fmea_step_assignee_data s
                WHERE s.data IS NOT NULL AND s.data != 'null' AND s.data != ''
                GROUP BY s.project_id, s.step_name
            """)
            for r in cur.fetchall():
                pid = r[0]
                step_name = r[1]
                cnt = r[2]
                steps_map.setdefault(pid, {})[step_name] = cnt
        except Exception as e:
            logger.warning(f"[search_projects] steps 统计失败(DB): {e}")
        cur.close(); conn.close()
    except Exception as e:
        logger.error(f"[search_projects] 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

    for proj in db_projects:
        pid = proj["id"]
        if not is_admin and user_id:
            assoc = assoc_map.get(pid, {})
            if (user_id != assoc.get("owner_id")
                and user_id != assoc.get("leader_id")
                and user_id not in assoc.get("team_ids", [])):
                continue
        safe_id = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', proj["name"]).strip('_') or f"project_{pid}"
        # v18: steps 来自 DB（step_name 映射回旧文件名语义，兼容前端判断）
        step_map = {"01-structure-tree": "01-structure-tree", "02-function": "02-function",
                    "03-failure": "03-failure", "04-risk": "04-risk",
                    "05-optimization": "05-optimization", "report": "06-report"}
        db_steps = steps_map.get(pid, {})
        steps = {}
        for db_key, file_key in step_map.items():
            cnt = db_steps.get(db_key, 0)
            if cnt:
                steps[file_key] = f"db:{cnt}"
        result.append({
            "id": safe_id,
            "name": proj["name"],
            "dbId": pid,
            "dirId": safe_id,
            "path": "",
            "steps": steps,
            "stepCount": len(steps),
            "metaName": proj["name"],
        })
    return JSONResponse(result)


@app.post("/api/projects")
async def create_project(request: Request):
    """创建新项目文件夹"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是有效 JSON"}, status_code=400)
    project_name = str(body.get("name", "")).strip()
    fmea_type = str(body.get("fmea_type", "DFMEA"))
    description = str(body.get("description", ""))
    user_id = body.get("user_id")
    # v19: 创建项目时，负责人默认就是创建者（user_id 指向的用户）；
    #      前端未显式传 leader_user_id 时，默认用 user_id
    leader_user_id = body.get("leader_user_id") or user_id
    team_user_ids = body.get("team_user_ids", [])
    weknora_kb_id = str(body.get("weknora_kb_id", "")).strip()
    subject = str(body.get("subject", "")).strip()

    if not project_name:
        return JSONResponse({"error": "项目名称不能为空"}, status_code=400)

    # 生成安全的 ID
    safe_id = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', project_name)
    safe_id = safe_id.strip('_')
    if not safe_id:
        safe_id = "project"

    # v18: 重名判断查 DB（不再依赖本地目录，也不再自动拼 _1 后缀）
    try:
        _dup_conn = get_user_db_conn()
        _dup_cur = _dup_conn.cursor()
        _dup_cur.execute("SELECT id FROM projects WHERE name = %s", (safe_id,))
        _dup_row = _dup_cur.fetchone()
        _dup_cur.close(); _dup_conn.close()
        if _dup_row:
            return JSONResponse(
                {"error": f"项目「{project_name}」已存在（DB id={_dup_row[0]}），请删除旧项目或换一个名称"},
                status_code=409
            )
    except Exception as e:
        logger.error(f"[POST /api/projects] 查重失败: {e}")

    project_id = safe_id

    # 获取当前用户
    current_user = await get_current_user(request)
    owner_user_id = None
    if current_user and current_user.get("role") == "admin":
        # 管理员创建时使用指定的 user_id 或自己的 id
        owner_user_id = user_id if user_id else current_user.get("user_id")
    elif current_user:
        owner_user_id = current_user.get("user_id")

    # 写入 projects 表（v18: 只写 DB，不再创建本地目录 / planning.json；v19: 补 owner/leader）
    try:
        conn = get_user_db_conn()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO projects (name, description, subject, fmea_type, weknora_kb_id,
                                     owner_user_id, leader_user_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (name) DO NOTHING""",
            (project_id, description, subject or project_name, fmea_type, weknora_kb_id,
             owner_user_id, leader_user_id)
        )
        conn.commit()
        cur.close(); conn.close()
        logger.info(f"[POST /api/projects] projects 表已写入: {project_id} owner={owner_user_id} leader={leader_user_id}")
    except Exception as e:
        logger.error(f"[POST /api/projects] 写 DB 失败: {e}")

    # 写入 project_members 表（v17.15: project_id 与 DB name 一致，成员不会错位）
    _set_project_members(project_id, owner_user_id, leader_user_id, team_user_ids)

    logger.info(f"创建项目: {project_name} (ID: {project_id})")
    return JSONResponse({"status": "created", "id": project_id, "name": project_name})


@app.put("/api/projects/{project_id}/members")
async def update_project_members(project_id: str, request: Request):
    """更新项目成员关联（owner/leader/team）"""
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not user_can_access_project(current_user, project_id):
        return JSONResponse({"error": "无权访问此项目"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是有效 JSON"}, status_code=400)
    leader_user_id = body.get("leader_user_id")
    team_user_ids = body.get("team_user_ids", [])
    # 取当前 owner
    assoc = _load_planning_user_assoc(project_id) or {}
    owner_user_id = assoc.get("owner_user_id")
    # 写入 project_members
    ok = _set_project_members(project_id, owner_user_id, leader_user_id, team_user_ids)
    if not ok:
        return JSONResponse({"error": "更新项目成员失败"}, status_code=500)
    # v20.4: 不再全量预生成审批行；改为各阶段推送时才生成（见 push_structure_to_engineers_api / push_step_to_engineers_api）
    return JSONResponse({"status": "updated", "id": project_id,
                         "leader_user_id": leader_user_id,
                         "team_user_ids": team_user_ids})


@app.get("/api/projects/{project_id}/members")
async def get_project_members(project_id: str, request: Request):
    """获取项目成员（从 project_members 表 JOIN users）"""
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not user_can_access_project(current_user, project_id):
        return JSONResponse({"error": "无权访问此项目"}, status_code=403)
    try:
        db_pid = _resolve_db_project_id(project_id)
        if db_pid is None:
            return JSONResponse({"error": "项目不存在"}, status_code=404)
        conn = get_user_db_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT u.id, u.username, u.display_name AS name, u.email, u.role_code,
                   r.name AS role_name, u.dept, u.active, pm.role_code AS project_role
            FROM project_members pm
            JOIN users u ON u.id = pm.user_id
            LEFT JOIN roles r ON r.code = u.role_code
            WHERE pm.project_id = %s
            ORDER BY pm.id
        """, (db_pid,))
        members = cur.fetchall()
        cur.close(); conn.close()
        return JSONResponse({
            "project_id": project_id,
            "members": [dict(m) for m in members]
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/projects/{project_id}/rename")
async def rename_project(project_id: str, name: str = "", request: Request = None):
    """重命名项目（query parameter: ?name=xxx）"""
    current_user = await get_current_user(request) if request else None
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not user_can_access_project(current_user, project_id):
        return JSONResponse({"error": "无权访问此项目"}, status_code=403)
    if not name:
        return JSONResponse({"error": "缺少 name 参数"}, status_code=400)
    new_name = name.strip()
    if not new_name:
        return JSONResponse({"error": "新名称不能为空"}, status_code=400)

    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return JSONResponse({"error": f"项目 {project_id} 不存在"}, status_code=404)
    try:
        conn = get_user_db_conn()
        cur = conn.cursor()
        cur.execute("UPDATE projects SET name = %s WHERE id = %s", (new_name, db_pid))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        return JSONResponse({"error": f"重命名失败: {e}"}, status_code=500)

    logger.info(f"项目 {project_id} 已重命名为: {new_name}")
    return JSONResponse({"status": "renamed", "name": new_name})


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    """删除项目（只删数据库，不删本地文件）

    权限要求：admin 或 owner / leader 可删，普通成员不可删
    """
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)

    is_admin = current_user.get("role") == "admin"

    if not is_admin:
        # 非 admin：查 project_members，必须是 owner 或 leader
        db_pid = _resolve_db_project_id(project_id)
        if db_pid is None:
            return JSONResponse({"error": "项目不存在"}, status_code=404)
        role = _get_project_role(project_id, current_user.get("user_id"))
        if role not in ("owner", "leader"):
            return JSONResponse({"error": "只有项目负责人或管理员才能删除项目"}, status_code=403)

    # 删除 DB 记录（级联删除 project_members）
    try:
        db_pid = _resolve_db_project_id(project_id)
        if db_pid is None:
            return JSONResponse({"error": "项目不存在"}, status_code=404)
        conn = get_user_db_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM projects WHERE id = %s", (db_pid,))
        conn.commit()
        cur.close(); conn.close()
        logger.info(f"已从数据库删除项目: {project_id} (本地文件保留在 {DATA_DIR / project_id})")
        return JSONResponse({"status": "deleted", "id": project_id, "note": "仅删除数据库记录，本地文件未删除"})
    except Exception as e:
        logger.error(f"删除项目失败: {e}")
        return JSONResponse({"error": f"删除失败: {e}"}, status_code=500)


# ═══════════════════════════════════════════════════════════════
# 专业 Agent 配置 API
# ═══════════════════════════════════════════════════════════════

@app.get("/api/agents")
async def list_agents(request: Request = None):
    """获取全局 Agent 配置列表"""
    current_user = await get_current_user(request) if request else None
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)

    try:
        conn = get_user_db_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, tier, name, name_en, icon, color, tier_label,
                       domain, status, status_label, description,
                       responsibilities, boundaries, tools,
                       source_file, weknora_kb_id, sort_order,
                       created_at, updated_at
                FROM agents
                ORDER BY tier, sort_order, id
            """)
            rows = cur.fetchall()
        conn.close()

        agents = []
        for r in rows:
            a = dict(r)
            # datetime → string
            for dt_field in ("created_at", "updated_at"):
                if isinstance(a.get(dt_field), datetime.datetime):
                    a[dt_field] = a[dt_field].isoformat()
            for fld in ("responsibilities", "boundaries", "tools"):
                val = a.get(fld)
                if isinstance(val, str):
                    a[fld] = val
                elif isinstance(val, (memoryview, bytes)):
                    import json
                    try:
                        a[fld] = json.loads(bytes(val).decode())
                    except Exception:
                        a[fld] = []
                elif val is None:
                    a[fld] = []
            agents.append(a)

        return JSONResponse({"agents": agents, "total": len(agents)})
    except Exception as e:
        logger.error(f"[list_agents] {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.put("/api/agents/{agent_id}")
async def update_agent(agent_id: int, request: Request):
    """更新全局 Agent 配置（主要用于设置 weknora_kb_id）"""
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)

    body = await request.json()

    allowed_fields = {
        "name", "name_en", "icon", "color", "tier_label", "domain",
        "status", "status_label", "description",
        "responsibilities", "boundaries", "tools",
        "source_file", "weknora_kb_id", "sort_order",
    }

    set_clauses = []
    params = []
    for key, val in body.items():
        if key in allowed_fields:
            set_clauses.append(f"{key} = %s")
            if isinstance(val, (list, dict)):
                import json
                params.append(json.dumps(val, ensure_ascii=False))
            else:
                params.append(val)

    if not set_clauses:
        return JSONResponse({"error": "没有可更新的字段"}, status_code=400)

    set_clauses_str = ", ".join(set_clauses)
    params.append(agent_id)

    try:
        conn = get_user_db_conn()
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE agents
                SET {set_clauses_str}, updated_at = NOW()
                WHERE id = %s
            """, params)
            if cur.rowcount == 0:
                conn.rollback()
                conn.close()
                return JSONResponse({"error": "Agent 不存在"}, status_code=404)
            conn.commit()
        conn.close()
        return JSONResponse({"status": "updated"})
    except Exception as e:
        logger.error(f"[update_agent] {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/projects/{project_id}/step-data/{step_name}")
async def get_step_data(project_id: str, step_name: str, request: Request):
    """获取项目步骤数据（v7: 从 fmea_step_assignee_data / fmea_step_data 读取）"""
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not user_can_access_project(current_user, project_id):
        return JSONResponse({"error": "无权访问此项目"}, status_code=403)
    db_pid = _resolve_db_project_id(project_id)
    logger.info(f"[get_step_data] project_id={project_id!r} db_pid={db_pid} step_name={step_name!r}")
    if db_pid is None:
        return JSONResponse({"error": f"项目 {project_id} 不存在", "project_id": project_id}, status_code=404)

    # 标准化 step_name: 文件名 → step_name
    _STEP_MAP = {
        "00-planning": "planning", "planning": "planning",
        "01-structure-tree": "structure", "structure": "structure",
        "02-function": "function", "function": "function",
        "03-failure": "failure", "failure": "failure",
        "04-risk": "risk", "risk": "risk",
        "05-optimization": "optimization", "optimization": "optimization",
        "06-report": "report", "report": "report",  # v17: 报告步骤映射（之前缺这一组导致 404）
    }
    step_key = step_name.replace(".json", "")
    step_lookup = _STEP_MAP.get(step_key, step_key)
    logger.info(f"[get_step_data] step_key={step_key!r} step_lookup={step_lookup!r}")

    # ── 工程师访问门控：除结构分析外的步骤，未推送前工程师不能查看 ──
    STEP_TO_STAGE_ID = {
        "function": 2, "failure": 3, "risk": 4, "optimization": 5,
    }
    user_role = current_user.get("role") or current_user.get("role_code") or ""
    is_engineer_user = user_role == "engineer" or user_role == "non_member"
    if is_engineer_user and step_lookup in STEP_TO_STAGE_ID:
        stage_id = STEP_TO_STAGE_ID[step_lookup]
        if not _is_stage_pushed(project_id, stage_id):
            stage_names = {2:"功能分析",3:"失效分析",4:"风险评估",5:"优化措施"}
            stage_name = stage_names.get(stage_id, step_lookup)
            return JSONResponse({
                "error": f"{stage_name}暂未推送，请等待项目负责人/管理员推送后再查看",
                "code": "NOT_PUSHED",
                "step": step_lookup,
                "stage_id": stage_id,
                "project_id": project_id,
            }, status_code=403)

    from engine.tools.fmea_helpers import get_baseline, get_report

    # ── 特殊处理 00-planning（从 projects 表返回） ──
    if step_lookup == "planning":
        return await _get_planning_data(db_pid, project_id)

    # ── 特殊处理 00-planning（从 projects 表返回） ──
    if step_lookup == "planning":
        return await _get_planning_data(db_pid, project_id)

    if step_lookup == "report":
        data = get_report(db_pid)
    else:
        data = get_baseline(db_pid, step_lookup)

    # ── v11: 为 failure/risk/optimization 步骤内联 function.domain_rows
    # 解决"用户直接点失效分析 tab，state.stepData['function'] 未缓存导致表格 JOIN 失败显示 0 条"的 bug
    # 前端 renderFailure 依赖 function.domain_rows 做 function_id JOIN，改为后端一次性内联
    if isinstance(data, dict) and step_lookup in ("failure", "risk", "optimization"):
        try:
            _func_baseline = get_baseline(db_pid, "function")
            if isinstance(_func_baseline, dict) and _func_baseline.get("domain_rows"):
                data = {**data, "function_domain_rows": _func_baseline["domain_rows"]}
        except Exception as _e:
            logger.warning(f"[get_step_data] 内联 function.domain_rows 失败: {_e}")

    # v15: optimization 额外内联 failure_items / risk_items / structure_nodes（18 列表格关联字段 JOIN 依据）
    if isinstance(data, dict) and step_lookup == "optimization":
        try:
            _fail_baseline = get_baseline(db_pid, "failure")
            if isinstance(_fail_baseline, dict) and _fail_baseline.get("items"):
                data = {**data, "failure_items": _fail_baseline["items"]}
        except Exception as _e:
            logger.warning(f"[get_step_data] 内联 failure.items 失败: {_e}")
        try:
            _risk_baseline = get_baseline(db_pid, "risk")
            if isinstance(_risk_baseline, dict) and _risk_baseline.get("items"):
                data = {**data, "risk_items": _risk_baseline["items"]}
        except Exception as _e:
            logger.warning(f"[get_step_data] 内联 risk.items 失败: {_e}")
        try:
            _struct_baseline = get_baseline(db_pid, "structure")
            _nodes = _struct_baseline.get("nodes") if isinstance(_struct_baseline, dict) else _struct_baseline
            if isinstance(_nodes, list):
                data = {**data, "structure_nodes": _nodes}
        except Exception as _e:
            logger.warning(f"[get_step_data] 内联 structure.nodes 失败: {_e}")

    # v10: 调试日志
    if isinstance(data, dict):
        items_count = len(data.get('items', []))
        fn_dr_count = len(data.get('function_domain_rows', []) or [])
        print(f"[get_step_data] 📖 返回数据: pid={db_pid} step={step_lookup} items={items_count} function_domain_rows={fn_dr_count}", flush=True)
    else:
        print(f"[get_step_data] ⚠️ data={data}", flush=True)

    if data is None:
        # 调试：检查 DB 中是否有任何数据
        try:
            from engine.tools.fmea_helpers import get_user_db_conn
            with get_user_db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT step_name, assignee_user_id FROM fmea_step_assignee_data WHERE project_id = %s",
                        (db_pid,)
                    )
                    rows = cur.fetchall()
                    logger.warning(f"[get_step_data] 数据未找到: db_pid={db_pid} step_lookup={step_lookup!r}; 现有 step_name={rows}")
        except Exception as e:
            logger.warning(f"[get_step_data] 调试查询失败: {e}")
        return JSONResponse({
            "error": "数据不存在，请先生成该步骤数据",
            "project_id": project_id,
            "step_name": step_name,
        }, status_code=404)

    return JSONResponse(data)


async def _get_planning_data(db_pid: int, project_id: str):
    """从 projects 表读取项目规划数据，构造与旧版 00-planning.json 兼容的响应"""
    try:
        conn = get_user_db_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, name, description, subject, part_number, product_type,
                   fmea_type, current_step, status, owner_user_id, leader_user_id,
                   weknora_kb_id, sor_folder, created_at, updated_at,
                   company_name, production_location, customer_name
            FROM projects WHERE id = %s
        """, (db_pid,))
        p = cur.fetchone()
        cur.close(); conn.close()
        if not p:
            return JSONResponse({"error": "项目不存在"}, status_code=404)

        # 查团队名称 + IDs（v19.6: 返回 team_user_ids 用于编辑弹窗预填 chip）
        leader_name = ""
        team_names = []
        team_user_ids = []
        try:
            conn2 = get_user_db_conn()
            cur2 = conn2.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if p.get("leader_user_id"):
                cur2.execute("SELECT display_name, email FROM users WHERE id = %s", (p["leader_user_id"],))
                r = cur2.fetchone()
                if r: leader_name = f"{r['display_name']}<{r['email']}>" if r.get("email") else r["display_name"]
            if p.get("id"):
                cur2.execute("SELECT pm.user_id, u.display_name, u.email FROM project_members pm JOIN users u ON u.id=pm.user_id WHERE pm.project_id=%s AND pm.role_code='engineer' ORDER BY pm.user_id", (p["id"],))
                for r in cur2.fetchall():
                    team_user_ids.append(int(r["user_id"]))
                    entry = f"{r['display_name']}<{r['email']}>" if r.get("email") else r["display_name"]
                    team_names.append(entry)
            cur2.close(); conn2.close()
        except Exception:
            pass

        return JSONResponse({
            "id": project_id,
            "name": p.get("name", ""),
            "description": p.get("description", ""),
            "subject": p.get("subject", ""),
            "part_number": p.get("part_number", ""),
            "product_type": p.get("product_type", ""),
            "fmea_type": p.get("fmea_type", ""),
            "current_step": p.get("current_step") or 0,
            "currentStep": p.get("current_step") or 0,
            "status": p.get("status", ""),
            "owner_user_id": p.get("owner_user_id"),
            "leader_user_id": p.get("leader_user_id"),
            "leader": leader_name,
            "team_members": ", ".join(team_names),
            # v19.6: 团队成员 id 列表（编辑弹窗回显 chip 用）
            "team_user_ids": team_user_ids,
            "weknora_kb_id": p.get("weknora_kb_id", ""),
            "kb_project_id": p.get("weknora_kb_id", ""),
            "sor_folder": p.get("sor_folder", ""),
            "created_at": str(p.get("created_at") or ""),
            "updated_at": str(p.get("updated_at") or ""),
            # v17: 项目信息（Excel 导出用，DB 存储）
            "company_name": p.get("company_name") or "",
            "production_location": p.get("production_location") or "",
            "customer_name": p.get("customer_name") or "",
            "platform": p.get("product_type") or "",
        })
    except Exception as e:
        logger.exception(f"[Planning] 读取失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ════════════════════════════════════════════════════════════════
# 交付物清单 API（对应需求 §12 附录 — 7 份交付物）
# ════════════════════════════════════════════════════════════════

# 交付物元数据：文件名 → 业务名称、来源阶段、说明
DELIVERABLE_CATALOG = {
    "00-baseline-doc.json": {
        "document_id": "基线文档",
        "name": "DFMEA 策划基线文档",
        "stage": 1,
        "stage_name": "策划准备与前置输入锁定",
        "description": "分析边界 + S/O/D 打分基准 + 资料缺口",
    },
    "01-document-1-regulation.json": {
        "document_id": "文件 1",
        "name": "法规条款映射清单",
        "stage": 1,
        "stage_name": "策划准备与前置输入锁定",
        "description": "项目配置维度法规检索结果（HEV 案例：七大维度）",
    },
    "01-structure-tree.json": {
        "document_id": "文件 3",
        "name": "结构树分层拆解表",
        "stage": 2,
        "stage_name": "结构树分析",
        "description": "BOM 四层拆解 + 责任绑定",
    },
    "02-function.json": {
        "document_id": "文件 4",
        "name": "零部件功能要求明细表",
        "stage": 3,
        "stage_name": "功能分析",
        "description": "功能 + SOR + 法规绑定",
    },
    "03-failure.json": {
        "document_id": "文件 5",
        "name": "失效分析清单",
        "stage": 4,
        "stage_name": "失效分析",
        "description": "失效闭环 + 多级传导",
    },
    "04-risk.json": {
        "document_id": "风险分析",
        "name": "风险分析清单",
        "stage": 5,
        "stage_name": "风险分析",
        "description": "S/O/D 打分 + AP 分级 + 高亮标记",
    },
    "05-optimization.json": {
        "document_id": "优化分析",
        "name": "优化措施清单",
        "stage": 6,
        "stage_name": "优化分析",
        "description": "高 AP 项 + 法规违规项配套设计优化方案",
    },
    "06-report.json": {
        "document_id": "DFMEA 主表",
        "name": "DFMEA 报告主表",
        "stage": 7,
        "stage_name": "文件化",
        "description": "完整 DFMEA 报告 JSON 结构",
    },
    "07-quality-check.json": {
        "document_id": "质量验收报告",
        "name": "全流程质量自查与验收报告",
        "stage": 7,
        "stage_name": "文件化",
        "description": "六类漏洞自查结果 + 修正记录 + 六维度评分",
    },
}


@app.get("/api/projects/{project_id}/deliverables")
async def list_deliverables(project_id: str, request: Request):
    """列出项目全部交付物及其就绪状态

    对应需求 §12 附录的 7 份交付物清单。
    返回每个交付物的存在性、字节数、修改时间、来源阶段、业务名称。
    前端可基于此渲染"交付物清单"展示页。
    """
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not user_can_access_project(current_user, project_id):
        return JSONResponse({"error": "无权访问此项目"}, status_code=403)

    step_dir = DATA_DIR / project_id / "fmea"
    items = []
    for filename, meta in DELIVERABLE_CATALOG.items():
        filepath = step_dir / filename
        if filepath.exists():
            stat = filepath.stat()
            items.append({
                "filename": filename,
                "exists": True,
                "size_bytes": stat.st_size,
                "modified_at": datetime.datetime.fromtimestamp(
                    stat.st_mtime
                ).isoformat(timespec="seconds"),
                **meta,
            })
        else:
            items.append({
                "filename": filename,
                "exists": False,
                "size_bytes": 0,
                "modified_at": None,
                **meta,
            })

    # 同时扫描目录下其他未在 catalog 中的文件（兼容历史数据）
    if step_dir.exists():
        catalog_files = set(DELIVERABLE_CATALOG.keys())
        for p in sorted(step_dir.glob("*.json")):
            if p.name not in catalog_files:
                stat = p.stat()
                items.append({
                    "filename": p.name,
                    "exists": True,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.datetime.fromtimestamp(
                        stat.st_mtime
                    ).isoformat(timespec="seconds"),
                    "document_id": "其他",
                    "name": p.stem,
                    "stage": 0,
                    "stage_name": "未分类",
                    "description": "未在标准交付物清单中的历史文件",
                })

    items.sort(key=lambda x: (x["stage"], x["filename"]))
    return JSONResponse({
        "project_id": project_id,
        "total": len(items),
        "ready_count": sum(1 for x in items if x["exists"]),
        "items": items,
    })


@app.get("/api/projects/{project_id}/deliverables/{filename:path}")
async def get_deliverable(project_id: str, filename: str, request: Request):
    """获取单个交付物的完整内容

    支持 .json 文本查看，Excel 走原有 /report/excel 接口。
    """
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not user_can_access_project(current_user, project_id):
        return JSONResponse({"error": "无权访问此项目"}, status_code=403)

    # 防止路径穿越
    if Path(filename).name != filename or filename.startswith("."):
        return JSONResponse({"error": "非法文件名"}, status_code=400)

    step_dir = DATA_DIR / project_id / "fmea"
    filepath = step_dir / filename
    if not filepath.exists():
        return JSONResponse({"error": "交付物不存在"}, status_code=404)
    if not filepath.suffix == ".json":
        return JSONResponse(
            {"error": "该接口仅支持 JSON 文件，Excel 请用 /report/excel"},
            status_code=400,
        )

    with open(filepath, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    return JSONResponse({
        "filename": filename,
        "project_id": project_id,
        "meta": DELIVERABLE_CATALOG.get(filename, {}),
        "data": data,
    })


# ════════════════════════════════════════════════════════════════
# 结构树节点指派 API
# ════════════════════════════════════════════════════════════════

def _get_project_role(project_id: str, user_id: int) -> Optional[str]:
    """获取用户在项目中的角色：admin / owner / leader / engineer / None"""
    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return None
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT role_code FROM project_members WHERE project_id = %s AND user_id = %s",
                    (db_pid, user_id)
                )
                role = cur.fetchone()
                return role[0] if role else None
    except Exception as e:
        logger.warning(f"[DB] _get_project_role({project_id}, {user_id}) 查询失败: {e}")
        return None


def _is_admin_or_can_assign(project_id: str, current_user: dict) -> tuple:
    """检查用户是否有权指派/管理节点。返回 (is_authorized, role_name)"""
    if current_user.get("role") == "admin":
        return True, "admin"
    uid = current_user.get("user_id")
    if not uid:
        return False, None
    role = _get_project_role(project_id, uid)
    if role in ("owner", "leader"):
        return True, role
    return False, role


def _assign_node_recursive(nodes: list, node_id: str, assignee_user_id: int, assignee_name: str, assigned_by: int,
                           _all_nodes: Optional[list] = None):
    """递归设置节点及其所有子节点的 assignee（_all_nodes 用于扁平结构 parent_id 查找）"""
    if _all_nodes is None:
        _all_nodes = nodes
    found = False
    for node in nodes:
        if node.get("id") == node_id:
            node["assignee_user_id"] = assignee_user_id
            node["assignee_name"] = assignee_name
            node["assigned_by"] = assigned_by
            node["assigned_at"] = str(datetime.datetime.now())
            found = True
        # 嵌套结构：递归子节点数组
        if node.get("children"):
            if _assign_node_recursive(node["children"], node_id, assignee_user_id, assignee_name, assigned_by, _all_nodes):
                found = True
    # 扁平结构：用全量列表找 parent_id 子节点（避免子树查找遗漏）
    if found:
        children = [n for n in _all_nodes if n.get("parent_id") == node_id]
        for child in children:
            child["assignee_user_id"] = assignee_user_id
            child["assignee_name"] = assignee_name
            child["assigned_by"] = assigned_by
            child["assigned_at"] = str(datetime.datetime.now())
            # 递归该子节点的子节点（传入全量列表）
            _assign_node_recursive(_all_nodes, child["id"], assignee_user_id, assignee_name, assigned_by, _all_nodes)
    return found


def _clear_assign_recursive(nodes: list, node_id: str, _all_nodes: Optional[list] = None):
    """递归清除节点及其所有子节点的 assignee（_all_nodes 用于扁平结构 parent_id 查找）"""
    if _all_nodes is None:
        _all_nodes = nodes
    found = False
    for node in nodes:
        if node.get("id") == node_id:
            node.pop("assignee_user_id", None)
            node.pop("assignee_name", None)
            node.pop("assigned_by", None)
            node.pop("assigned_at", None)
            found = True
        if node.get("children"):
            if _clear_assign_recursive(node["children"], node_id, _all_nodes):
                found = True
    # 扁平结构：用全量列表找 parent_id 子节点（避免子树查找遗漏）
    if found:
        children = [n for n in _all_nodes if n.get("parent_id") == node_id]
        for child in children:
            child.pop("assignee_user_id", None)
            child.pop("assignee_name", None)
            child.pop("assigned_by", None)
            child.pop("assigned_at", None)
            _clear_assign_recursive(_all_nodes, child["id"], _all_nodes)
    return found


class AssignNodeRequest(BaseModel):
    assignee_user_id: int = Field(...)
    assignee_name: str = Field("")

@app.post("/api/projects/{project_id}/structure-tree/assign")
async def assign_structure_node(project_id: str, request: Request):
    """指派结构树节点给指定工程师（含所有子节点，递归）"""
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)

    # 权限检查
    authorized, role = _is_admin_or_can_assign(project_id, current_user)
    if not authorized:
        return JSONResponse({
            "error": "只有管理员、项目负责人才能指派节点",
            "your_role": role or "non_member"
        }, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "无效的请求体"}, status_code=400)

    node_id = str(body.get("node_id", "")).strip()
    assignee_user_id = int(body.get("assignee_user_id", 0))
    assignee_name = str(body.get("assignee_name", "")).strip()

    if not node_id or not assignee_user_id:
        return JSONResponse({"error": "node_id 和 assignee_user_id 必填"}, status_code=400)

    # 从 DB 读取结构树
    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return JSONResponse({"error": f"项目 {project_id} 不存在"}, status_code=404)
    from engine.tools.fmea_helpers import get_baseline, upsert_baseline
    raw = get_baseline(db_pid, "structure")
    if raw is None:
        return JSONResponse({"error": "结构树数据不存在"}, status_code=404)
    nodes = raw if isinstance(raw, list) else raw.get("nodes", raw.get("children", []))

    uid = current_user.get("user_id")
    success = _assign_node_recursive(nodes, node_id, assignee_user_id, assignee_name, uid)
    if not success:
        return JSONResponse({"error": f"未找到节点 {node_id}"}, status_code=404)

    # 写回 DB
    if isinstance(raw, dict):
        raw["nodes"] = nodes
        result_data = raw
    else:
        result_data = {"nodes": nodes}
    upsert_baseline(db_pid, "structure", result_data)

    logger.info(f"[Assign] 节点 {node_id} → 已指派给 #{assignee_user_id} ({assignee_name})")
    return JSONResponse({"status": "assigned", "node_id": node_id, "assignee_user_id": assignee_user_id})


@app.post("/api/projects/{project_id}/structure-tree/unassign")
async def unassign_structure_node(project_id: str, request: Request):
    """撤销结构树节点指派（含所有子节点，递归）"""
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    authorized, _ = _is_admin_or_can_assign(project_id, current_user)
    if not authorized:
        return JSONResponse({"error": "只有管理员、项目负责人才能撤销指派"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "无效的请求体"}, status_code=400)
    node_id = str(body.get("node_id", "")).strip()
    if not node_id:
        return JSONResponse({"error": "node_id 必填"}, status_code=400)
    # 从 DB 读取结构树
    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return JSONResponse({"error": f"项目 {project_id} 不存在"}, status_code=404)
    from engine.tools.fmea_helpers import get_baseline, upsert_baseline
    raw = get_baseline(db_pid, "structure")
    if raw is None:
        return JSONResponse({"error": "结构树数据不存在"}, status_code=404)
    nodes = raw if isinstance(raw, list) else raw.get("nodes", raw.get("children", []))
    _clear_assign_recursive(nodes, node_id)
    if isinstance(raw, dict):
        raw["nodes"] = nodes
        result_data = raw
    else:
        result_data = {"nodes": nodes}
    upsert_baseline(db_pid, "01-structure-tree", result_data)
    logger.info(f"[Assign] 节点 {node_id} 已撤销指派")
    return JSONResponse({"status": "unassigned", "node_id": node_id})


# ── 推送结构树指派给工程师（发邮件）──
@app.post("/api/projects/{project_id}/structure-tree/push")
async def push_structure_to_engineers_api(project_id: str, request: Request):
    """把结构树中已指派的节点, 通过邮件通知对应的工程师。

    要求: 调用者必须能指派节点（admin/owner/leader）。
    """
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    authorized, role = _is_admin_or_can_assign(project_id, current_user)
    if not authorized:
        return JSONResponse({"error": f"当前角色 ({role}) 无权推送, 请联系项目负责人或管理员"},
                            status_code=403)
    try:
        body = await request.json() or {}
    except Exception:
        body = {}
    # 项目名 (可选)
    project_name = body.get("project_name", "").strip()
    base_url = body.get("base_url", "").strip() or None
    # 取项目名
    if not project_name:
        try:
            with get_user_db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT name FROM projects WHERE id = %s", (project_id,))
                    row = cur.fetchone()
                    if row:
                        project_name = row[0] if isinstance(row, tuple) else row.get("name", project_id)
        except Exception:
            project_name = project_id

    from engine.tools.notify_tools import _push_to_engineers_internal
    try:
        data = await _push_to_engineers_internal(project_id, project_name, base_url, step_name="structure")
        # 推送成功后标记该阶段已推送（持久化到 planning JSON，所有用户共享）
        _mark_stage_pushed(project_id, 1)  # stage 1 = 结构树分析
        # v20.4: 推送时生成当前阶段的审批行（不再初始化时全量生成）
        try:
            _init_fmea_approvals(project_id, stage_no=1)
        except Exception:
            pass
        data["pushed"] = True
        return JSONResponse(data)
    except Exception as e:
        logger.exception(f"[Push] 推送失败: {e}")
        return JSONResponse({
            "status": "error", "error": str(e), "total": 0,
            "success_count": 0, "fail_count": 0, "skip_count": 0, "details": [],
        }, status_code=500)


@app.post("/api/projects/{project_id}/step-push")
async def push_step_to_engineers_api(project_id: str, request: Request):
    """通用推送 API: 把某步骤已指派的条目通过邮件通知工程师

    Body:
        step_name: 'structure' | 'function' | 'failure' | 'risk' | 'optimization'
        project_name: 可选
        base_url: 可选

    要求: 调用者必须能指派（admin/owner/leader）
    """
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    authorized, role = _is_admin_or_can_assign(project_id, current_user)
    if not authorized:
        return JSONResponse({"error": f"当前角色 ({role}) 无权推送"}, status_code=403)

    try:
        body = await request.json() or {}
    except Exception:
        body = {}

    step_name = (body.get("step_name") or "").strip().lower()
    valid_steps = {"structure", "function", "failure", "risk", "optimization"}
    if step_name not in valid_steps:
        return JSONResponse({
            "error": f"无效的 step_name: {step_name}, 必须是 {valid_steps}"
        }, status_code=400)

    project_name = body.get("project_name", "").strip()
    base_url = body.get("base_url", "").strip() or None
    if not project_name:
        try:
            with get_user_db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT name FROM projects WHERE id = %s", (project_id,))
                    row = cur.fetchone()
                    if row:
                        project_name = row[0] if isinstance(row, tuple) else row.get("name", project_id)
        except Exception:
            project_name = project_id

    from engine.tools.notify_tools import _push_to_engineers_internal, STEP_TO_STAGE, is_email_enabled
    try:
        # v17.12: 阶段推送邮件开关（stage_push，全局/项目级）
        stage_id = STEP_TO_STAGE.get(step_name, 1)
        if not is_email_enabled(project_id, None, "stage_push"):
            _mark_stage_pushed(project_id, stage_id)
            return JSONResponse({
                "status": "skipped", "total": 0, "pushed": True,
                "message": "阶段推送邮件已关闭（email_notification_switch），仅标记为已推送",
            })
        data = await _push_to_engineers_internal(project_id, project_name, base_url, step_name=step_name)
        # 标记该阶段已推送
        _mark_stage_pushed(project_id, stage_id)
        # v20.4: 推送时生成当前阶段的审批行（不再初始化时全量生成）
        try:
            _init_fmea_approvals(project_id, stage_no=stage_id)
        except Exception:
            pass
        data["pushed"] = True
        return JSONResponse(data)
    except Exception as e:
        logger.exception(f"[Push] 推送失败: {e}")
        return JSONResponse({
            "status": "error", "error": str(e), "total": 0,
            "success_count": 0, "fail_count": 0, "skip_count": 0, "details": [],
        }, status_code=500)


def _resolve_pid_in_db(project_id) -> Optional[int]:
    """内部用：接受 str/int project_id，统一转 DB 主键"""
    if isinstance(project_id, int):
        return project_id
    if isinstance(project_id, str):
        try:
            return int(project_id)
        except (ValueError, TypeError):
            return _resolve_db_project_id(project_id)
    return None


def _mark_stage_pushed(project_id: str, stage_id: int):
    """标记某阶段已推送（v7: 写入 projects.stage_pushes JSONB）"""
    import json as _json
    from datetime import datetime
    db_pid = _resolve_pid_in_db(project_id)
    if db_pid is None:
        return
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT stage_pushes FROM projects WHERE id = %s", (db_pid,))
                row = cur.fetchone()
                pushes = {}
                if row:
                    raw = row[0] if isinstance(row, tuple) else row.get("stage_pushes")
                    if isinstance(raw, str):
                        pushes = _json.loads(raw)
                    elif isinstance(raw, dict):
                        pushes = raw
                pushes[str(stage_id)] = datetime.now().isoformat()
                cur.execute(
                    "UPDATE projects SET stage_pushes = %s WHERE id = %s",
                    (_json.dumps(pushes), db_pid),
                )
            conn.commit()
        logger.info(f"[Push] 标记项目 pid={db_pid} 阶段{stage_id} 已推送")
    except Exception as e:
        logger.error(f"[Push] 标记推送状态失败: {e}")


def _is_stage_pushed(project_id: str, stage_id: int) -> bool:
    """检查某阶段是否已推送（v7: 读 projects.stage_pushes）"""
    db_pid = _resolve_pid_in_db(project_id)
    if db_pid is None:
        return False
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT stage_pushes FROM projects WHERE id = %s", (db_pid,))
                row = cur.fetchone()
                if not row:
                    return False
                raw = row[0] if isinstance(row, tuple) else row.get("stage_pushes")
                if isinstance(raw, str):
                    pushes = json.loads(raw)
                elif isinstance(raw, dict):
                    pushes = raw
                else:
                    return False
                return str(stage_id) in pushes
    except Exception:
        return False


def _get_all_stage_pushes(project_id: str) -> dict:
    """获取项目所有阶段的推送状态（v7: 从 projects.stage_pushes 读）"""
    db_pid = _resolve_pid_in_db(project_id)
    if db_pid is None:
        return {}
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT stage_pushes FROM projects WHERE id = %s", (db_pid,))
                row = cur.fetchone()
                if not row:
                    return {}
                raw = row[0] if isinstance(row, tuple) else row.get("stage_pushes")
                if isinstance(raw, str):
                    return json.loads(raw)
                return raw or {}
    except Exception:
        return {}


@app.post("/api/projects/{project_id}/mark-pushed/{stage_id}")
async def mark_stage_pushed_api(project_id: str, stage_id: int, request: Request):
    """标记某阶段已推送（管理员/负责人可调用）"""
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    role = current_user.get("role", "")
    if role not in ("admin", "owner", "leader"):
        return JSONResponse({"error": "仅管理员/负责人可标记推送状态"}, status_code=403)
    _mark_stage_pushed(project_id, stage_id)
    return JSONResponse({"success": True, "stage_id": stage_id, "pushed": True})


@app.get("/api/projects/{project_id}/structure-tree/assignees")
async def list_assignees_api(project_id: str, request: Request):
    """列出项目结构树中所有被指派的节点 (按工程师聚合)"""
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not user_can_access_project(current_user, project_id):
        return JSONResponse({"error": "无权访问此项目"}, status_code=403)
    from engine.tools.notify_tools import _load_structure_tree, _build_assignee_groups, _lookup_user_emails
    tree = _load_structure_tree(project_id)
    if not tree:
        return JSONResponse({"nodes": [], "groups": []})
    groups = _build_assignee_groups(tree)
    user_ids = list(groups.keys())
    users = _lookup_user_emails(user_ids)
    out_groups = []
    for uid, nodes in groups.items():
        u = users.get(uid, {})
        out_groups.append({
            "user_id": int(uid) if uid.isdigit() else uid,
            "display_name": u.get("display_name", ""),
            "username": u.get("username", ""),
            "email": u.get("email", ""),
            "node_count": len(nodes),
            "nodes": [{"id": n.get("id"), "name": n.get("name"),
                       "level": n.get("level", ""), "part_number": n.get("part_number", "")}
                      for n in nodes],
        })
    return JSONResponse({"groups": out_groups, "total_assignees": len(out_groups),
                         "total_nodes": sum(g["node_count"] for g in out_groups)})


# ── v20.1: 结构树一键构建（跳过 LLM，直接调后端提取工具）──
@app.post("/api/projects/{project_id}/structure-tree/extract")
async def extract_structure_tree_api(project_id: str, request: Request):
    """🌳 构建结构树按钮触发：直接调 extract_structure_tree_tool，绕过 LLM.

    用于前端 '🌳 构建结构树' 按钮：避免 LLM 错误调 query_tools 查 SOR 路径.
    """
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not user_can_access_project(current_user, project_id):
        return JSONResponse({"error": "无权访问此项目"}, status_code=403)
    # 仅 admin/owner/leader 可触发
    role = current_user.get("role") or "non_member"
    if role not in ("admin", "owner", "leader"):
        return JSONResponse({"error": f"仅管理员/项目负责人/项目所有者可触发（当前角色: {role}）"}, status_code=403)

    from engine.tools.project_init_tools import extract_structure_tree_tool
    try:
        result_str = await extract_structure_tree_tool(project_id=project_id)
        # 工具返回 JSON 字符串，解析后回传
        try:
            data = json.loads(result_str)
        except Exception:
            data = {"raw": result_str}
        # v20.2: 把工具内部的 success / error 透传给前端
        tool_ok = bool(data.get("success")) if isinstance(data, dict) else False
        return JSONResponse({
            "success": tool_ok,
            "result": data,
            "error": data.get("error") if isinstance(data, dict) and not tool_ok else None,
        })
    except Exception as e:
        logger.error(f"[StructureExtract] API 失败: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ── v23.1: 风险评估一键执行（绕过 LLM，直接调后端工具）──
@app.post("/api/projects/{project_id}/run-risk-analysis")
async def run_risk_analysis_api(project_id: str, request: Request):
    """🎯 风险评估按钮触发：直接调 run_risk_analysis_tool，绕过 LLM.

    用于前端 '🎯 风险评估' 按钮：避免 LLM 误调 query_tools 查 SOR 路径.
    依赖 v21 全局评分标准 fallback（无项目评分文件时自动用 AIAG-VDA 标准）.
    """
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not user_can_access_project(current_user, project_id):
        return JSONResponse({"error": "无权访问此项目"}, status_code=403)
    role = current_user.get("role") or "non_member"
    if role not in ("admin", "owner", "leader"):
        return JSONResponse({"error": f"仅管理员/项目负责人/项目所有者可触发（当前角色: {role}）"}, status_code=403)

    from engine.tools.risk_tools import run_risk_analysis_tool
    try:
        # v23.2: run_risk_analysis_tool 是同步函数且内部会用 asyncio.run() 起 loop —
        # 不能在 FastAPI 事件循环里直接同步调（会报 "asyncio.run() cannot be called from a running event loop"），
        # 用 asyncio.to_thread 丢到线程池执行
        import asyncio
        result_str = await asyncio.to_thread(
            run_risk_analysis_tool, project_id=project_id, use_llm_controls=True
        )
        try:
            data = json.loads(result_str)
        except Exception:
            data = {"raw": result_str}
        return JSONResponse({"success": True, "result": data})
    except Exception as e:
        logger.error(f"[RiskAnalysis] API 失败: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ── v24.1: 生成报告一键执行（绕过 LLM，直接调后端工具）──
@app.post("/api/projects/{project_id}/run-report")
async def run_report_api(project_id: str, request: Request):
    """📊 生成报告按钮触发：直接调 generate_report_tool，绕过 LLM.

    用于前端 '📊 生成报告' 按钮：避免 LLM 误调 query_tools 查 SOR 路径.
    """
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not user_can_access_project(current_user, project_id):
        return JSONResponse({"error": "无权访问此项目"}, status_code=403)
    role = current_user.get("role") or "non_member"
    if role not in ("admin", "owner", "leader"):
        return JSONResponse({"error": f"仅管理员/项目负责人/项目所有者可触发（当前角色: {role}）"}, status_code=403)

    from engine.tools.report_tools import generate_report_tool
    try:
        result_str = await asyncio.to_thread(generate_report_tool, project_id=project_id)
        try:
            data = json.loads(result_str)
        except Exception:
            data = {"raw": result_str}
        return JSONResponse({"success": True, "result": data})
    except Exception as e:
        logger.error(f"[ReportGen] API 失败: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ── 步骤门控检查: 基于结构树指派 + 审批状态交叉校验 ──
# ── 检查当前步骤是否可以执行 (上一步指派的工程师是否全部确认) ──
@app.get("/api/projects/{project_id}/step-gate-check")
async def step_gate_check(project_id: str, stage_id: int = 2, request: Request = None):
    """检查当前步骤的上一步是否所有指派工程师都已确认。

    逻辑:
    1. 读取 structure 基线数据, 按 assignee_user_id 去重得到「待确认工程师列表」
    2. 读取 fmea_approvals 表, 检查这些工程师在上一阶段的确认状态
    3. 返回 {can_proceed, prev_stage, total_assignees, approved_count, pending[]}

    当项目没有审批状态时, 视为无需审批 (can_proceed=true).
    """
    prev_stage = stage_id - 1
    if prev_stage < 1:
        return JSONResponse({"can_proceed": True, "reason": "first_step"})

    # 1. 从 DB 基线读取结构树，提取指派工程师 (去重)
    assigned_engineers = []
    try:
        from engine.tools.notify_tools import _load_step_data
        tree = _load_step_data(project_id, "structure")
        if tree:
            seen_uids = set()
            for node in tree.get("nodes", []):
                uid = node.get("assignee_user_id")
                name = node.get("assignee_name", "")
                if uid is not None and uid != "" and str(uid) not in seen_uids:
                    seen_uids.add(str(uid))
                    assigned_engineers.append({
                        "user_id": uid, "name": name.strip(),
                    })
    except Exception as e:
        logger.warning(f"[Gate] 读取结构树失败: {e}")
    total_assignees = len(assigned_engineers)

    # 2. 如果没有指派工程师 → 直接放行
    if total_assignees == 0:
        return JSONResponse({
            "can_proceed": True, "reason": "no_assignees",
            "prev_stage": prev_stage, "total_assignees": 0,
            "approved_count": 0, "pending": [],
        })

    # 3. 解析 DB 项目 ID
    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return JSONResponse({"can_proceed": True, "reason": "no_db_project", "prev_stage": prev_stage,
                             "total_assignees": total_assignees, "approved_count": 0, "pending": []})

    # 4. 从 fmea_approvals 表批量查审批状态
    approved_count = 0
    pending = []
    if assigned_engineers:
        with get_user_db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                user_ids_int = [int(e["user_id"]) for e in assigned_engineers if str(e["user_id"]).isdigit()]
                if user_ids_int:
                    cur.execute("""
                        SELECT user_id, status
                        FROM fmea_approvals
                        WHERE project_id = %s AND stage_no = %s AND user_id = ANY(%s)
                    """, (db_pid, prev_stage, user_ids_int))
                    approval_map = {row["user_id"]: row["status"] for row in cur.fetchall()}
                else:
                    approval_map = {}
        for eng in assigned_engineers:
            uid_int = int(eng["user_id"]) if str(eng["user_id"]).isdigit() else None
            approved = approval_map.get(uid_int) == "approved" if uid_int is not None else False
            if approved:
                approved_count += 1
            else:
                pending.append({"user_id": eng["user_id"], "name": eng["name"]})

    can_proceed = len(pending) == 0
    return JSONResponse({
        "can_proceed": can_proceed,
        "reason": "all_approved" if can_proceed else "pending_confirmations",
        "prev_stage": prev_stage,
        "total_assignees": total_assignees,
        "approved_count": approved_count,
        "pending": pending,
    })


# ── 工程师确认/拒绝当前阶段 (写入 fmea_approvals) ──
class StepApprovalRequest(BaseModel):
    stage_no: int
    status: str = "submitted"  # v5: submitted(待审核)/rejected(驳回)
    comment: str = ""


@app.post("/api/projects/{project_id}/step-approval")
async def submit_step_approval(project_id: str, req: StepApprovalRequest, request: Request):
    """工程师对当前阶段提交确认/拒绝意见。"""
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not user_can_access_project(current_user, project_id):
        return JSONResponse({"error": "无权访问此项目"}, status_code=403)
    if req.status not in ("submitted", "rejected"):
        return JSONResponse({"error": "status 必须是 approved 或 rejected"}, status_code=400)
    if not 1 <= req.stage_no <= 9:
        return JSONResponse({"error": "stage_no 必须在 1-9 之间"}, status_code=400)

    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return JSONResponse({"error": f"项目 {project_id} 不存在"}, status_code=404)

    user_id = current_user.get("id") or current_user.get("user_id")
    if not user_id:
        return JSONResponse({"error": "用户 ID 缺失"}, status_code=400)
    try:
        user_id_int = int(user_id)
    except (ValueError, TypeError):
        return JSONResponse({"error": "用户 ID 格式错误"}, status_code=400)

    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                # ON CONFLICT 更新 (project_id, user_id, stage_no 唯一)
                cur.execute("""
                    INSERT INTO fmea_approvals
                        (project_id, user_id, stage_no, status, comment, approved_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (project_id, user_id, stage_no)
                    DO UPDATE SET
                        status = EXCLUDED.status,
                        comment = EXCLUDED.comment,
                        approved_at = NOW(),
                        updated_at = NOW()
                """, (db_pid, user_id_int, req.stage_no, req.status, req.comment))
                conn.commit()
        # 计算本阶段当前已确认人数（v20.10: 全部按 DB 统计，不再读本地 JSON）
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE status IN ('submitted', 'approved')) AS approved,
                        COUNT(*) AS total
                    FROM fmea_approvals
                    WHERE project_id = %s AND stage_no = %s
                """, (db_pid, req.stage_no))
                row = cur.fetchone()
        approved_count = int(row["approved"] or 0) if isinstance(row, dict) else int(row[0] or 0)
        total_count     = int(row["total"] or 0) if isinstance(row, dict) else int(row[1] or 0)

        # v20.10: 该阶段所有工程师都已提交（submitted/approved）→ 发邮件通知负责人审核
        # 统计口径：fmea_approvals 中该 (project_id, stage_no) 的全部行（推送时按指派工程师生成）
        is_all_approved = total_count > 0 and approved_count >= total_count
        if is_all_approved:
            try:
                from engine.tools.notify_tools import send_stage_review_request_email
                send_stage_review_request_email(req.stage_no, db_pid)
            except Exception as e:
                logger.warning(f"[Approval] 阶段审核提醒邮件发送失败: {e}")

        return JSONResponse({
            "status": "ok",
            "user_id": user_id_int,
            "stage_no": req.stage_no,
            "approval_status": req.status,
            "approved_count": approved_count,
            "total_count": total_count,
            "total_assignees": total_count,
            "is_all_approved": is_all_approved,
        })
    except Exception as e:
        logger.exception(f"[Approval] 写入失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/projects/{project_id}/step-approval")
async def get_step_approval(project_id: str, request: Request, stage_no: int = 0,
                             user_id: Optional[int] = None):
    """查询某阶段/某用户的审批状态.
    - user_id 不传: 返回该阶段所有审批
    - stage_no=0: 返回该用户所有阶段
    - 都传: 返回该用户该阶段的审批
    """
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not user_can_access_project(current_user, project_id):
        return JSONResponse({"error": "无权访问此项目"}, status_code=403)

    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return JSONResponse({"error": f"项目 {project_id} 不存在"}, status_code=404)

    if user_id is None:
        user_id = current_user.get("id") or current_user.get("user_id")
    try:
        target_uid = int(user_id)
    except (ValueError, TypeError):
        return JSONResponse({"error": "user_id 格式错误"}, status_code=400)

    try:
        with get_user_db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if stage_no:
                    cur.execute("""
                        SELECT stage_no, status, comment, approved_at,
                               lead_status, lead_comment, lead_reviewed_at
                        FROM fmea_approvals
                        WHERE project_id = %s AND user_id = %s AND stage_no = %s
                    """, (db_pid, target_uid, stage_no))
                    row = cur.fetchone()
                    if not row:
                        return JSONResponse({
                            "approved": False, "status": "pending",
                            "stage_no": stage_no, "user_id": target_uid,
                        })
                    return JSONResponse({
                        "stage_no": row["stage_no"],
                        "status": row["status"],
                        "comment": row["comment"],
                        "approved_at": str(row["approved_at"]) if row.get("approved_at") else None,
                        "lead_status": row.get("lead_status", "pending"),
                        "lead_comment": row.get("lead_comment", ""),
                        "lead_reviewed_at": str(row["lead_reviewed_at"]) if row.get("lead_reviewed_at") else None,
                        "user_id": target_uid,
                    })
                else:
                    cur.execute("""
                        SELECT stage_no, status, comment, approved_at
                        FROM fmea_approvals
                        WHERE project_id = %s AND user_id = %s
                        ORDER BY stage_no
                    """, (db_pid, target_uid))
                    rows = cur.fetchall()
                    return JSONResponse({
                        "user_id": target_uid,
                        "approvals": [
                            {"stage_no": r["stage_no"], "status": r["status"],
                             "comment": r["comment"],
                             "approved_at": str(r["approved_at"]) if r.get("approved_at") else None}
                            for r in rows
                        ],
                    })
    except Exception as e:
        logger.exception(f"[Approval] 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── 完整审批状态 (供前端 panel/badges 渲染) ──
@app.get("/api/projects/{project_id}/approvals-state")
async def get_approvals_state(project_id: str, request: Request, stage_id: Optional[int] = None):
    """从 fmea_approvals 表读取所有阶段的完整审批状态 (替代旧的 approval_state.json).

    返回结构兼容旧版前端:
    {
      project_id, current_stage,
      stages: {
        "2": {stage_id, stage_name, members: {uid: {status, approved_at}}, is_all_approved, approved_count, total_count},
        "3": {...}, ...
      }
    }

    Args:
        stage_id: 如果传入，只返回该 stage 的数据（不传则返回所有）
    """
    current_user = await get_current_user(request)
    if not current_user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not user_can_access_project(current_user, project_id):
        return JSONResponse({"error": "无权访问此项目"}, status_code=403)

    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return JSONResponse({"error": f"项目 {project_id} 不存在"}, status_code=404)

    STAGE_NAMES = {1: "结构分析", 2: "功能分析", 3: "失效分析", 4: "风险评估", 5: "优化措施", 6: "生成报告"}

    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                if stage_id:
                    # 只查询指定 stage
                    cur.execute("""
                        SELECT a.stage_no, a.user_id, a.status, a.approved_at::text,
                               u.display_name, u.username
                        FROM fmea_approvals a
                        JOIN users u ON u.id = a.user_id
                        WHERE a.project_id = %s AND a.stage_no = %s
                        ORDER BY a.stage_no, a.user_id
                    """, (db_pid, stage_id))
                else:
                    # 查询所有 stage
                    cur.execute("""
                        SELECT a.stage_no, a.user_id, a.status, a.approved_at::text,
                               u.display_name, u.username
                        FROM fmea_approvals a
                        JOIN users u ON u.id = a.user_id
                        WHERE a.project_id = %s
                        ORDER BY a.stage_no, a.user_id
                    """, (db_pid,))
                rows = cur.fetchall()

        # 按 stage 分组 (plain cursor, tuple index)
        stages = {}
        for r in rows:
            sid = r[0]; user_id = r[1]; status = r[2] or "pending"
            approved_at = r[3]; display_name = r[4]; username = r[5]
            name = (display_name or username or str(user_id)).strip()
            skey = str(sid)
            if skey not in stages:
                stages[skey] = {
                    "stage_id": sid, "stage_name": STAGE_NAMES.get(sid, f"阶段{sid}"),
                    "members": {}, "is_all_approved": False,
                    "approved_count": 0, "total_count": 0, "rejected": [],
                }
            stages[skey]["members"][name] = {
                "status": status, "approved_at": approved_at, "user_id": user_id,
            }
            stages[skey]["total_count"] += 1
            if status == "approved":
                stages[skey]["approved_count"] += 1
            elif status == "rejected":
                stages[skey]["rejected"].append(name)

        # 计算 is_all_approved
        for s in stages.values():
            s["is_all_approved"] = s["total_count"] > 0 and s["approved_count"] == s["total_count"]
            # 兼容旧字段: required_members, member_approvals
            s["required_members"] = list(s["members"].keys())
            s["member_approvals"] = {n: {"status": m["status"]} for n, m in s["members"].items()}

        # current_stage: 取最近有交互的 stage
        current_stage = max([int(s) for s in stages.keys()], default=1)

        return JSONResponse({
            "project_id": project_id,
            "current_stage": current_stage,
            "stages": stages,
            "pushed": _is_stage_pushed(project_id, 1),       # v5: 阶段1(结构树)推送状态
            "stage_pushes": _get_all_stage_pushes(project_id),  # v5: 全部阶段推送状态
        })
    except Exception as e:
        logger.exception(f"[Approval] 读取状态失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


_original_save_step_data = None

@app.post("/api/projects/{project_id}/step-data/{step_name}")
async def save_step_data(project_id: str, step_name: str, request: Request):
    """保存项目步骤数据（v7: 写入 fmea_step_assignee_data）"""
    print(f"[SAVE_STEP_DATA] 🔥 路由被调用: project_id={project_id} step_name={step_name}", flush=True)
    logger.info(f"[SAVE_STEP_DATA] 🔥 路由被调用: project_id={project_id} step_name={step_name}")
    current_user = await get_current_user(request)
    print(f"[SAVE_STEP_DATA] 👤 current_user: {current_user}", flush=True)
    if not current_user:
        print(f"[SAVE_STEP_DATA] ❌ 未登录", flush=True)
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not user_can_access_project(current_user, project_id):
        print(f"[SAVE_STEP_DATA] ❌ 无权访问", flush=True)
        return JSONResponse({"error": "无权访问此项目"}, status_code=403)

    db_pid = _resolve_db_project_id(project_id)
    print(f"[SAVE_STEP_DATA] 🆔 db_pid={db_pid}", flush=True)
    if db_pid is None:
        print(f"[SAVE_STEP_DATA] ❌ 项目不存在", flush=True)
        return JSONResponse({"error": f"项目 {project_id} 不存在"}, status_code=404)

    # 标准化 step_name
    _SMAP = {"00-planning": "planning", "planning": "planning",
             "01-structure-tree": "structure", "structure": "structure",
             "02-function": "function", "function": "function",
             "03-failure": "failure", "failure": "failure",
             "04-risk": "risk", "risk": "risk",
             "05-optimization": "optimization", "optimization": "optimization"}
    step_key = step_name.replace(".json", "")
    step_lookup = _SMAP.get(step_key, step_key)
    print(f"[SAVE_STEP_DATA] 🏷 step_lookup={step_lookup}", flush=True)

    # 读取请求体
    try:
        payload = await request.json()
        print(f"[SAVE_STEP_DATA] 📦 payload keys: {list(payload.keys()) if isinstance(payload, dict) else 'NOT DICT'}", flush=True)
    except Exception:
        print(f"[SAVE_STEP_DATA] ❌ 无效的请求体", flush=True)
        return JSONResponse({"error": "无效的请求体"}, status_code=400)

    uid = current_user.get("user_id")
    is_admin = current_user.get("role") == "admin"
    from engine.tools.fmea_helpers import save_assignee_data, upsert_baseline, get_baseline

    # ── 结构树保存：工程师只能编辑自己被指派的节点 ──
    if step_lookup == "structure" and not is_admin:
        authorized, role = _is_admin_or_can_assign(project_id, current_user)
        if not authorized and role not in ('owner', 'leader'):
            new_nodes = payload if isinstance(payload, list) else payload.get("nodes", [])
            # 读 DB 基线
            old_baseline = get_baseline(db_pid, "structure")
            old_nodes_dict = {}
            if old_baseline:
                old_list = old_baseline if isinstance(old_baseline, list) else old_baseline.get("nodes", [])
                for n in old_list:
                    if n.get("id"):
                        old_nodes_dict[n["id"]] = n

            new_ids = set()
            for n in new_nodes:
                nid = n.get("id")
                if not nid:
                    continue
                new_ids.add(nid)
                old = old_nodes_dict.get(nid)
                if old is None:
                    parent_id = n.get("parent_id")
                    if not parent_id:
                        return JSONResponse({"error": "工程师不能新增根节点"}, status_code=403)
                    parent = next((x for x in new_nodes if x.get("id") == parent_id), None)
                    if not parent:
                        parent = old_nodes_dict.get(parent_id)
                    if not parent:
                        continue
                    if parent.get("assignee_user_id") != uid:
                        return JSONResponse({"error": "工程师不能在被指派给别人的节点下新增子节点"}, status_code=403)
                    if n.get("assignee_user_id") is None:
                        n["assignee_user_id"] = parent.get("assignee_user_id")
                        n["assignee_name"] = parent.get("assignee_name", "")
                        n["assigned_by"] = parent.get("assigned_by", uid)
                        n["assigned_at"] = str(datetime.datetime.now())
                else:
                    if (n.get("parent_id") or "") != (old.get("parent_id") or ""):
                        if n.get("assignee_user_id") != uid:
                            return JSONResponse({"error": "工程师不能移动未被自己指派的节点"}, status_code=403)
                    if old.get("assignee_user_id") != n.get("assignee_user_id"):
                        return JSONResponse({"error": "工程师不能修改节点的指派关系"}, status_code=403)

            for old_id in old_nodes_dict:
                if old_id not in new_ids:
                    old_node = old_nodes_dict[old_id]
                    if old_node.get("assignee_user_id") != uid:
                        return JSONResponse({"error": "工程师不能删除被指派给别人的节点"}, status_code=403)

    # ── 工程师访问门控：未推送前工程师不能保存 (function/failure/risk/optimization) ──
    if step_lookup in ("function", "failure", "risk", "optimization") and not is_admin:
        STEP_TO_STAGE_ID_FOR_SAVE = {
            "function": 2, "failure": 3, "risk": 4, "optimization": 5,
        }
        stage_id_for_save = STEP_TO_STAGE_ID_FOR_SAVE[step_lookup]
        if not _is_stage_pushed(project_id, stage_id_for_save):
            stage_names = {2:"功能分析",3:"失效分析",4:"风险评估",5:"优化措施"}
            stage_name = stage_names.get(stage_id_for_save, step_lookup)
            return JSONResponse({
                "error": f"{stage_name}暂未推送，工程师无法保存。请等待项目负责人/管理员推送后再操作。",
                "code": "NOT_PUSHED",
            }, status_code=403)

    # ── 特殊处理 00-planning：写 projects 表 ──
    if step_lookup in ("planning", "00-planning"):
        try:
            with get_user_db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE projects SET
                            subject = %s,
                            description = %s,
                            part_number = %s,
                            product_type = %s,
                            company_name = %s,
                            production_location = %s,
                            customer_name = %s,
                            current_step = %s,
                            updated_at = NOW()
                        WHERE id = %s
                    """, (
                        payload.get("subject", ""),
                        payload.get("description", ""),
                        payload.get("part_number", ""),
                        payload.get("product_type", payload.get("platform", "")),
                        payload.get("company_name", ""),
                        payload.get("production_location", ""),
                        payload.get("customer_name", ""),
                        int(payload.get("current_step", payload.get("currentStep", 0))),
                        db_pid,
                    ))
                conn.commit()
            logger.info(f"[DB] 保存规划数据 pid={db_pid}")
            return JSONResponse({"status": "saved", "step": "planning", "db_pid": db_pid})
        except Exception as e:
            logger.exception(f"[Planning] 保存失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ── function/failure/risk/optimization：调用通用保存 ──
    if step_lookup in ("function", "failure", "risk", "optimization"):
        # v10: 调试日志
        if isinstance(payload, dict):
            items = payload.get('items') or []
            print(f"[SAVE_STEP_DATA] 📊 payload: items={len(items)} keys={list(payload.keys())}", flush=True)
            if items:
                sample_aids = set(int(it.get('assignee_user_id', -1)) for it in items[:50])
                print(f"[SAVE_STEP_DATA] 📊 payload assignee_user_ids (前50): {sample_aids}", flush=True)
        # v15: optimization 走"按工程师拆分"模式（skip_engineer_rows=False），其余 3 步保持共享模式
        if step_lookup == "optimization":
            save_ok = save_step_baseline_split(db_pid, step_lookup, payload, uid, is_admin)
        else:
            save_ok = save_step_baseline(db_pid, step_lookup, payload, uid, is_admin)
        if not save_ok:
            print(f"[SAVE_STEP_DATA] ❌ save_step_baseline 失败", flush=True)
            return JSONResponse({"error": "保存失败"}, status_code=500)
        logger.info(f"[DB] 通用保存 pid={db_pid} step={step_lookup} uid={uid}")
        print(f"[SAVE_STEP_DATA] 🎉 通用保存成功 {step_lookup} pid={db_pid}", flush=True)
        return JSONResponse({"status": "saved", "step": step_lookup, "db_pid": db_pid})

    # ── structure step：保留完整的 merge/split 逻辑 ──
    should_filter = not is_admin and step_lookup == "structure"

    # 检查审批状态
    if should_filter:
        try:
            conn_approval = get_user_db_conn()
            cur_approval = conn_approval.cursor()
            cur_approval.execute(
                "SELECT status FROM fmea_approvals "
                "WHERE project_id = %s AND user_id = %s AND stage_no = 1",
                (db_pid, uid)
            )
            row = cur_approval.fetchone()
            cur_approval.close()
            conn_approval.close()
            if row and row[0] == 'approved':
                return JSONResponse({"error": "结构树已审批通过，无法修改"}, status_code=403)
        except Exception as e:
            logger.warning(f"[Approval Check] 查询失败: {e}")

    print(f"[SAVE_STEP_DATA] 💾 调用 save_assignee_data: step={step_lookup} uid={uid} filter_own={should_filter}", flush=True)
    save_ok = save_assignee_data(db_pid, step_lookup, uid or -1, payload, filter_own=should_filter)
    print(f"[SAVE_STEP_DATA] ✅ save_assignee_data 返回: {save_ok}", flush=True)
    if not save_ok:
        print(f"[SAVE_STEP_DATA] ❌ save_assignee_data 失败", flush=True)
        return JSONResponse({"error": "保存失败"}, status_code=500)

    logger.info(f"[DB] 保存步骤 {step_lookup} pid={db_pid} uid={uid}")
    print(f"[SAVE_STEP_DATA] 🎉 保存成功 {step_lookup} pid={db_pid}", flush=True)
    return JSONResponse({"status": "saved", "step": step_lookup, "db_pid": db_pid})


# ════════════════════════════════════════════════════════════════
# 通用步骤基线保存（function/failure/risk/optimization 共用）
# ════════════════════════════════════════════════════════════════

def save_step_baseline(db_pid: int, step_lookup: str, payload: dict, uid: Optional[int], is_admin: bool) -> bool:
    """通用步骤基线保存 - 用于 function/failure/risk/optimization

    特性：
    - FOR UPDATE 行级锁防并发
    - filter_own：工程师只改自己的 items，管理员改全部
    - skip_engineer_rows：不创建工程师个人行（共享数据模式）

    Args:
        db_pid: projects.id
        step_lookup: function/failure/risk/optimization
        payload: 前端提交的完整 data（含 items / domain_rows）
        uid: 当前用户 ID
        is_admin: 当前用户是否为管理员

    Returns:
        True=保存成功, False=保存失败
    """
    from engine.tools.fmea_helpers import save_assignee_data
    return save_assignee_data(
        db_pid, step_lookup, uid or -1, payload,
        filter_own=not is_admin,
        skip_engineer_rows=True,
    )


def save_step_baseline_split(db_pid: int, step_lookup: str, payload: dict, uid: Optional[int], is_admin: bool) -> bool:
    """按工程师拆分保存（v15: 优化措施专用）

    与 save_step_baseline 的区别：
    - skip_engineer_rows=False：会创建/更新工程师个人行（assignee_user_id=实际用户ID）
    - filter_own=not is_admin：工程师只改自己指派行的字段，管理员改全部

    Args:
        db_pid: projects.id
        step_lookup: optimization
        payload: 前端提交的完整 data（含 items）
        uid: 当前用户 ID
        is_admin: 当前用户是否为管理员

    Returns:
        True=保存成功, False=保存失败
    """
    from engine.tools.fmea_helpers import save_assignee_data
    return save_assignee_data(
        db_pid, step_lookup, uid or -1, payload,
        filter_own=not is_admin,
        skip_engineer_rows=False,
    )


# ════════════════════════════════════════════════════════════════
# 项目初始化 API（SSE 进度推送）
# ════════════════════════════════════════════════════════════════

from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse


class InitProjectRequest(BaseModel):
    """项目初始化请求（WeKnora 版本）"""
    project_name: str = Field(..., description="项目名称")
    subject: str = Field("", description="FMEA 主体对象")
    weknora_kb_id: str = Field("", description="WeKnora 知识库 ID")
    description: str = Field("", description="项目描述")
    part_number: str = Field("", description="零件号")
    product_type: str = Field("", description="产品类型/年型/平台")
    # v19.3: 与规划页字段对齐
    company_name: str = Field("", description="公司名称")
    production_location: str = Field("", description="工程地点")
    customer_name: str = Field("", description="顾客名称")
    leader_user_id: Optional[int] = Field(None, description="负责人用户ID")
    team_user_ids: list = Field(default_factory=list, description="团队成员用户ID列表")
    # 旧字段保留兼容
    sor_folder: str = Field("", description="SOR 文件夹路径（遗留）")
    project_id: Union[str, int] = Field("", description="项目ID（可选，兼容数字/字符串）")


@app.post("/api/projects/init")
async def init_project(req: InitProjectRequest, request: Request):
    """
    初始化项目知识库（SSE 流式推送进度）

    SSE 事件类型:
    - progress: 进度更新 (phase, progress, message)
    - done:     初始化完成
    - error:    错误信息
    """
    import datetime
    import unicodedata

    project_name = req.project_name.strip()
    weknora_kb_id = req.weknora_kb_id.strip()
    subject = req.subject.strip()
    description = req.description.strip()
    part_number = req.part_number.strip()
    product_type = req.product_type.strip()
    # v19.3: 与规划页字段对齐
    company_name = req.company_name.strip()
    production_location = req.production_location.strip()
    customer_name = req.customer_name.strip()
    leader_user_id = req.leader_user_id
    team_user_ids = req.team_user_ids or []
    sor_folder = req.sor_folder.strip()

    if not project_name:
        return JSONResponse({"error": "项目名称不能为空"}, status_code=400)

    # ── 单一接口：根据 project_id 区分创建 / 更新（v19.7: 全程用 db_pid，不再用 name 匹配）──
    project_id = str(req.project_id or "").strip()
    db_pid = None
    if project_id:
        try:
            db_pid = _resolve_db_project_id(project_id)
        except Exception:
            db_pid = None

    # ── 获取当前用户（admin 自己就是 owner）──
    current_user = await get_current_user(request)
    owner_user_id = None
    if current_user:
        owner_user_id = current_user.get("user_id")
    # v19: 未显式指定负责人时，默认创建者为负责人（与 POST /api/projects 一致）
    if not leader_user_id and owner_user_id:
        leader_user_id = owner_user_id

    # ── 写入 projects 表（db_pid 存在 → UPDATE；不存在 → 先查重再 INSERT）──
    try:
        conn = get_user_db_conn()
        cur = conn.cursor()
        if db_pid is not None:
            cur.execute(
                """UPDATE projects SET description = %s, subject = %s, part_number = %s,
                       product_type = %s, weknora_kb_id = %s,
                       company_name = %s, production_location = %s, customer_name = %s,
                       leader_user_id = %s, updated_at = NOW()
                   WHERE id = %s""",
                (description, subject or project_name, part_number, product_type,
                 weknora_kb_id, company_name, production_location, customer_name,
                 leader_user_id, db_pid)
            )
            if cur.rowcount == 0:
                cur.close(); conn.close()
                return JSONResponse({"error": f"项目 DB id={db_pid} 不存在"}, status_code=404)
        else:
            # 创建新项目：生成项目 ID
            if not project_id:
                safe_name = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', project_name).strip('_')
                project_id = safe_name if safe_name else f"proj-{int(time.time())}"
            # 显式检查重名（按 name）— 不建目录，只报错
            cur.execute("SELECT 1 FROM projects WHERE name = %s", (project_id,))
            if cur.fetchone():
                cur.close(); conn.close()
                return JSONResponse(
                    {"error": f"项目 {project_id} 在数据库中已存在，请先删除或改名"},
                    status_code=409
                )
            cur.execute(
                """INSERT INTO projects (name, description, subject, part_number,
                                         product_type, weknora_kb_id,
                                         company_name, production_location, customer_name,
                                         owner_user_id, leader_user_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (project_id, description, subject or project_name, part_number,
                 product_type, weknora_kb_id, company_name, production_location, customer_name,
                 owner_user_id, leader_user_id)
            )
            ins_row = cur.fetchone()
            if ins_row:
                db_pid = int(ins_row[0])
        conn.commit()
        cur.close(); conn.close()
        logger.info(f"[Init] projects 表已写入: db_pid={db_pid} name={project_id or '(由db_pid决定)'}")
    except Exception as e:
        logger.error(f"[Init] projects 表写入失败: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"数据库写入失败: {e}"},
            status_code=500
        )

    # 写入/同步 project_members 表（owner / leader / team）— 工程师变动时全量同步
    # v19.7: 传 db_pid（整数），确保 DELETE+INSERT 落在正确的项目行上
    _set_project_members(str(db_pid), owner_user_id, leader_user_id, team_user_ids)

    action = "updated" if req.project_id else "created"
    logger.info(f"[Init] 项目 {action}: {project_name} (ID: {project_id or db_pid})")
    return JSONResponse({
        "status": action,
        "project_id": str(db_pid) if db_pid is not None else project_id,
        "project_name": project_name,
        "success": True,
    })


@app.get("/api/projects/init/status")
async def init_project_status(project_id: str = ""):
    """查询项目初始化进度"""
    if not project_id:
        return JSONResponse({"error": "缺少 project_id 参数"}, status_code=400)

    kb = get_kb_service()
    status = kb.get_status(project_id)

    # 也检查 progress_tool 的进度
    from engine.tools.project_init_tools import _init_progress
    tool_progress = _init_progress.get(project_id, {})

    return JSONResponse({
        "project_id": project_id,
        "status": status,
        "tool_progress": tool_progress,
    })


# ════════════════════════════════════════════════════════════════
# 添加文件 & 重建索引 API
# ════════════════════════════════════════════════════════════════

class AddFilesRequest(BaseModel):
    """添加文件请求"""
    project_id: str = Field(..., description="项目ID")
    files_folder: str = Field(..., description="新增文件所在文件夹路径")


@app.post("/api/projects/add-files")
async def add_files(req: AddFilesRequest):
    """
    向已有项目添加新文件。

    流程:
    1. 扫描文件夹中的新文件
    2. 转换为 MD 并存入知识库
    3. 增量更新 FMEA 索引
    4. 重建知识库 index.md
    """
    import datetime
    import unicodedata

    project_id = req.project_id.strip()
    files_folder = req.files_folder.strip()

    # 清理路径
    files_folder = ''.join(
        c for c in files_folder
        if unicodedata.category(c) not in ('Cf', 'Cc')
        or c in ('\r', '\n')
    )
    files_folder = files_folder.strip('"').strip("'")

    if not project_id:
        return JSONResponse({"error": "项目ID不能为空"}, status_code=400)

    if not files_folder:
        return JSONResponse({"error": "文件夹路径不能为空"}, status_code=400)

    folder_path = Path(files_folder)
    if not folder_path.exists():
        return JSONResponse({"error": f"文件夹不存在: {files_folder}"}, status_code=404)

    kb = get_kb_service()
    project_kb_dir = kb.base_dir / project_id
    if not project_kb_dir.exists():
        return JSONResponse({"error": f"项目知识库不存在: {project_id}"}, status_code=404)

    # Step 1-2: 使用 convert_directory_to_markdown 批量转换
    from knowledge.file_converter import convert_directory_to_markdown

    try:
        conv_result = convert_directory_to_markdown(
            str(folder_path),
            str(project_kb_dir),
        )
    except Exception as e:
        logger.error(f"[AddFiles] 文件转换失败: {e}")
        return JSONResponse({"success": False, "error": f"文件转换失败: {str(e)}"}, status_code=500)

    added_files = [f["output"] for f in conv_result.get("files", []) if f.get("status") == "ok"]
    failed_files = [f for f in conv_result.get("files", []) if f.get("status") == "fail"]
    skipped_files = [f["output"] for f in conv_result.get("files", []) if f.get("status") == "skip"]

    if not added_files and not skipped_files:
        return JSONResponse({
            "success": True,
            "message": "未发现可添加的新文件（可能已存在）",
            "added": 0,
            "skipped": len(skipped_files),
        })

    # Step 3: 增量更新 FMEA 索引
    index_updated = False
    if added_files:
        try:
            from knowledge.fmea_indexer import get_fmea_indexer
            fmea_indexer = get_fmea_indexer()
            await fmea_indexer.incremental_update(project_id, added_files)
            index_updated = True
            logger.info(f"[AddFiles] FMEA 索引增量更新完成: +{len(added_files)} 文件")
        except Exception as e:
            logger.error(f"[AddFiles] FMEA 索引增量更新失败: {e}")

    # Step 4: 重建知识库 index.md
    try:
        kb.build_index(project_id)
    except Exception as e:
        logger.error(f"[AddFiles] 知识库索引重建失败: {e}")

    return JSONResponse({
        "success": True,
        "project_id": project_id,
        "added": len(added_files),
        "failed": len(failed_files),
        "added_files": added_files,
        "failed_files": failed_files,
        "index_updated": index_updated,
    })


class RebuildIndexRequest(BaseModel):
    """重建索引请求"""
    project_id: str = Field(..., description="项目ID")


@app.post("/api/projects/rebuild-index")
async def rebuild_index(req: RebuildIndexRequest):
    """
    重建项目的 FMEA 语义索引。

    用于以下场景:
    - 模型升级后需要重新提取要素
    - 添加文件后索引异常
    - 手动触发重建
    """
    project_id = req.project_id.strip()

    if not project_id:
        return JSONResponse({"error": "项目ID不能为空"}, status_code=400)

    kb = get_kb_service()
    status = kb.get_status(project_id)

    if not status.get("exists"):
        return JSONResponse({"error": f"项目知识库不存在: {project_id}"}, status_code=404)

    try:
        from knowledge.fmea_indexer import get_fmea_indexer
        fmea_indexer = get_fmea_indexer()

        index_result = await fmea_indexer.build_index(project_id)

        stats = {
            "doc_count": index_result.get("doc_count", 0),
            "structure_nodes": len(index_result.get("fmea_elements", {}).get("structure_nodes", [])),
            "functions": len(index_result.get("fmea_elements", {}).get("functions", [])),
            "special_characteristics": len(index_result.get("fmea_elements", {}).get("special_characteristics", [])),
            "regulations": len(index_result.get("fmea_elements", {}).get("regulations", [])),
        }

        # 同时重建知识库 index.md
        kb.build_index(project_id)

        return JSONResponse({
            "success": True,
            "project_id": project_id,
            "stats": stats,
            "message": f"FMEA 索引重建完成: {stats['structure_nodes']} 结构节点, "
                       f"{stats['functions']} 功能, {stats['special_characteristics']} 特性, "
                       f"{stats['regulations']} 法规",
        })

    except Exception as e:
        logger.error(f"[API] FMEA 索引重建失败: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# [已注释] 知识库查询 API — 依赖 kb_query（MiniMax 退化问题）
# class KbQueryRequest(BaseModel):
#     """知识库查询请求"""
#     project_id: str = Field(..., description="项目ID")
#     question: str = Field(..., description="查询问题")


# @app.post("/api/kb/query")
# async def kb_query(req: KbQueryRequest):
#     """查询项目知识库"""
#     engine = get_query_engine()

#     try:
#         result = await engine.query(req.project_id.strip(), req.question.strip())
#         return JSONResponse({
#             "success": True,
#             "answer": result.get("answer", ""),
#             "selected_pages": result.get("selected_pages", []),
#             "sources": result.get("sources", []),
#         })
#     except Exception as e:
#         logger.error(f"[API] 知识库查询失败: {e}")
#         return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# @app.post("/api/kb/query/stream")
# async def kb_query_stream(req: KbQueryRequest):
#     """流式查询项目知识库（SSE）"""
#     engine = StreamingQueryEngine()

#     async def event_generator():
#         async for event in engine.query_stream(req.project_id.strip(), req.question.strip()):
#             yield {
#                 "event": event["type"],
#                 "data": json.dumps(event["data"], ensure_ascii=False),
#             }

#     return EventSourceResponse(event_generator())


@app.get("/api/tools/progress/{project_id}")
async def tools_progress(project_id: str):
    """工具执行进度 SSE 推送。
    
    前端在收到 TOOL_CALL_START 事件后，
    连接此端点获取工具内部执行进度（如 extract_structure_tree_tool 的分批遍历状态）。
    
    SSE 事件类型:
    - progress: 进度更新 (phase, round, message, progress, nodes_count, docs_processed, total_docs)
    - done:    工具执行完成
    - error:   工具执行出错
    """
    from engine.tools.project_init_tools import get_all_tool_progress

    async def event_generator():
        last_hash = ""
        stale_count = 0

        while True:
            await asyncio.sleep(0.8)  # 800ms 轮询间隔

            all_progress = get_all_tool_progress()
            data = all_progress.get(project_id)

            if not data:
                stale_count += 1
                # v6.3.2: 延长超时至 960s（1200×0.8s），支持 16 分钟的长作业
                if stale_count > 1200:  # 原来 60（49秒），多 Agent 跑 135+ 秒需要延长
                    break
                continue

            stale_count = 0

            # 避免重复发送相同内容
            current_hash = hash(json.dumps(data, sort_keys=True, default=str))
            if current_hash == last_hash:
                continue
            last_hash = current_hash

            phase = data.get("phase", "")
            if phase == "done":
                yield {
                    "event": "done",
                    "data": json.dumps(data, ensure_ascii=False, default=str),
                }
                break
            elif phase == "error":
                yield {
                    "event": "error",
                    "data": json.dumps(data, ensure_ascii=False, default=str),
                }
                break
            else:
                yield {
                    "event": "progress",
                    "data": json.dumps(data, ensure_ascii=False, default=str),
                }

    return EventSourceResponse(event_generator())


@app.get("/api/kb/pages")
async def kb_pages(project_id: str = ""):
    """列出项目知识库的所有页面"""
    if not project_id:
        return JSONResponse({"error": "缺少 project_id 参数"}, status_code=400)

    kb = get_kb_service()
    pages = kb.list_pages(project_id)

    return JSONResponse({
        "success": True,
        "total": len(pages),
        "pages": [
            {
                "filename": p.get("filename", ""),
                "title": p.get("title", ""),
                "summary": p.get("summary", "")[:200],
                "source_type": p.get("source_type", ""),
                "size_kb": p.get("size_kb", 0),
            }
            for p in pages
        ],
    })


@app.get("/api/kb/status")
async def kb_status(project_id: str = ""):
    """获取项目知识库状态"""
    if not project_id:
        return JSONResponse({"error": "缺少 project_id 参数"}, status_code=400)

    kb = get_kb_service()
    status = kb.get_status(project_id)

    return JSONResponse({"success": True, "status": status})


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "service": "FMEA AgentOS"})


# ════════════════════════════════════════════════════════════════
# 邮件发送 API
# ════════════════════════════════════════════════════════════════


class SendEmailRequest(BaseModel):
    """发送邮件请求"""
    sender_email: str = Field(..., description="发件人邮箱")
    sender_auth_code: str = Field(..., description="发件人邮箱授权码")
    recipient_email: str = Field(..., description="收件人邮箱")
    subject: str = Field(..., description="邮件主题")
    body: str = Field(..., description="邮件正文（纯文本）")
    # v17.12: 邮件开关参数（可选）——传了则先查 email_notification_switch
    notification_type: Optional[str] = Field(None, description="邮件类型（如 optimize_assignee / stage_confirm_request），触发开关判断")
    project_id: Optional[str] = Field(None, description="项目ID（字符串），配合 notification_type 查开关")
    stage_id: Optional[int] = Field(None, description="阶段号（0-6），配合 notification_type 查开关")


@app.post("/api/email/send")
async def send_email(req: SendEmailRequest):
    """通过 QQ 邮箱 SMTP 发送通知邮件"""
    if not req.sender_email or not req.sender_auth_code or not req.recipient_email:
        return JSONResponse({"success": False, "error": "发件人/收件人信息不完整"}, status_code=400)
    # v17.12: 开关判断（传了 notification_type 才检查；未配置/查不到 → 默认开）
    if req.notification_type:
        try:
            from engine.tools.notify_tools import is_email_enabled, _resolve_db_project_id
            db_pid = _resolve_db_project_id(str(req.project_id)) if req.project_id else None
            if db_pid and not is_email_enabled(db_pid, req.stage_id, req.notification_type):
                return JSONResponse({"success": False, "skipped": True,
                                     "message": f"邮件已关闭（{req.notification_type}），未发送"})
        except Exception:
            pass
    try:
        msg = MIMEText(req.body, "plain", "utf-8")
        msg["From"] = req.sender_email
        msg["To"] = req.recipient_email
        msg["Subject"] = Header(req.subject, "utf-8")

        # 根据发件人邮箱域名自动选择 SMTP 服务器
        sender_lower = req.sender_email.lower()
        if 'qq.com' in sender_lower:
            smtp_host = "smtp.qq.com"
            smtp_port = 465
        elif '163.com' in sender_lower:
            smtp_host = "smtp.163.com"
            smtp_port = 465
        elif '126.com' in sender_lower:
            smtp_host = "smtp.126.com"
            smtp_port = 465
        elif 'gmail.com' in sender_lower:
            smtp_host = "smtp.gmail.com"
            smtp_port = 465
        elif 'outlook.com' in sender_lower or 'hotmail.com' in sender_lower:
            smtp_host = "smtp-mail.outlook.com"
            smtp_port = 587
        else:
            smtp_host = "smtp.qq.com"
            smtp_port = 465

        logger.info(f"[Email] 开始发送: {req.sender_email} → {req.recipient_email} via {smtp_host}:{smtp_port}")

        context = ssl.create_default_context()
        if smtp_port == 587:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
            server.starttls(context=context)
        else:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30)
        server.set_debuglevel(1)
        server.ehlo()
        server.login(req.sender_email, req.sender_auth_code)
        logger.info("[Email] SMTP 登录成功")

        # 发送
        server.sendmail(req.sender_email, [req.recipient_email], msg.as_string())
        server.quit()

        logger.info(f"[Email] 发送成功: {req.sender_email} → {req.recipient_email} 「{req.subject}」")
        return JSONResponse({"success": True})
    except smtplib.SMTPAuthenticationError:
        logger.error("[Email] 认证失败 — 邮箱授权码错误或已过期，请重新生成")
        return JSONResponse({"success": False, "error": "邮箱授权码错误或已过期，请在 QQ 邮箱设置中重新生成授权码"}, status_code=500)
    except smtplib.SMTPServerDisconnected:
        logger.error("[Email] 服务器断开连接 — 可能是 IP 被限制或 SSL 配置问题")
        return JSONResponse({"success": False, "error": "SMTP 服务器断开连接，请检查网络或联系管理员"}, status_code=500)
    except socket.timeout:
        logger.error("[Email] 连接超时 — 无法连接到 smtp.qq.com:465")
        return JSONResponse({"success": False, "error": "连接 QQ 邮箱服务器超时，请检查网络"}, status_code=500)
    except Exception as e:
        logger.error(f"[Email] 发送失败: {type(e).__name__}: {e}")
        return JSONResponse({"success": False, "error": f"{type(e).__name__}: {str(e)}"}, status_code=500)


# ════════════════════════════════════════════════════════════════
# SOD / AP 评分表文件管理 API (v7: DB)
# ════════════════════════════════════════════════════════════════

import shutil as _shutil
from fastapi import UploadFile, File as FastAPIFile


@app.get("/api/projects/{project_id}/risk-files")
async def list_risk_files(project_id: str):
    """列出项目 SOD/AP 评分表文件（v7: 从 DB）"""
    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return JSONResponse({"files": []})
    from engine.tools.fmea_helpers import list_risk_files as _db_list
    files = _db_list(db_pid)
    return JSONResponse({"files": files})


@app.get("/api/projects/{project_id}/risk-files/{filename:path}")
async def get_risk_file(project_id: str, filename: str):
    """读取单个评分表文件内容（v7: 从 DB）"""
    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    from engine.tools.fmea_helpers import get_risk_file_content
    content = get_risk_file_content(db_pid, filename)
    if content is None:
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    return JSONResponse({"filename": filename, "content": content})


@app.post("/api/projects/{project_id}/risk-files/upload")
async def upload_risk_files(project_id: str, files: list[UploadFile] = FastAPIFile(...)):
    """上传评分表文件（v7: 写入 DB）"""
    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    from engine.tools.fmea_helpers import save_risk_file

    uploaded = []
    failed = []
    for f in files:
        if not f.filename:
            continue
        if not f.filename.lower().endswith(".md"):
            failed.append({"filename": f.filename, "error": "仅支持 .md 格式"})
            continue
        try:
            content = (await f.read()).decode("utf-8", errors="replace")
            save_risk_file(db_pid, f.filename, content)
            uploaded.append(f.filename)
        except Exception as e:
            failed.append({"filename": f.filename, "error": str(e)})

    return JSONResponse({"success": len(uploaded) > 0, "uploaded": uploaded, "failed": failed})


@app.post("/api/projects/{project_id}/risk-files/{filename:path}")
async def save_risk_file(project_id: str, filename: str, request: Request):
    """保存（覆盖）评分表文件（v7: 写入 DB）"""
    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    from engine.tools.fmea_helpers import save_risk_file as _db_save
    try:
        payload = await request.json()
        content = payload.get("content", "")
        _db_save(db_pid, filename, content)
        return JSONResponse({"status": "saved", "filename": filename})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/projects/{project_id}/risk-files/{filename:path}")
async def delete_risk_file(project_id: str, filename: str):
    """删除评分表文件（v7: 从 DB 删除）"""
    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    from engine.tools.fmea_helpers import delete_risk_file as _db_del
    ok = _db_del(db_pid, filename)
    if not ok:
        return JSONResponse({"error": "删除失败"}, status_code=500)
    return JSONResponse({"status": "deleted", "filename": filename})


# ════════════════════════════════════════════════════════════════
# 报告 API (v7: DB)
# ════════════════════════════════════════════════════════════════

@app.get("/api/projects/{project_id}/report")
async def get_report_api(project_id: str):
    """获取 DFMEA 最终报告 JSON 数据（v7: 从 DB）"""
    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    from engine.tools.fmea_helpers import get_report as _db_report
    data = _db_report(db_pid)
    if data is None:
        return JSONResponse({"error": "报告未生成，请先调用 generate_report_tool"}, status_code=404)
    return JSONResponse(data)


@app.get("/api/projects/{project_id}/report/excel")
async def download_report_excel(project_id: str):
    """下载 DFMEA 报告 xlsx（v17.9: 从 DB 动态生成，内存返回，不写本地）"""
    import io as _io
    from datetime import datetime as _dt
    from urllib.parse import quote as _quote
    from engine.tools.fmea_helpers import get_report, resolve_project_id_to_db_pid
    from engine.tools.report_tools import _generate_excel

    db_pid = resolve_project_id_to_db_pid(project_id)
    if not db_pid:
        return JSONResponse({"error": "项目不存在"}, status_code=404)

    report_data = get_report(db_pid)
    if not report_data or not report_data.get("report_rows"):
        return JSONResponse({"error": "报告未生成，请先调用 generate_report_tool"}, status_code=404)

    # 从 DB 报告数据动态生成 Excel（内存，不落盘）
    bio = _io.BytesIO()
    _generate_excel(report_data, report_data.get("project_name", project_id), None, None, save_to=bio)
    bio.seek(0)

    date_str = _dt.now().strftime("%Y%m%d")
    safe_name = str(report_data.get("project_name", project_id)).replace(" ", "_").replace("/", "_")[:30]
    filename = f"DFMEA_{safe_name}_{date_str}.xlsx"
    return Response(
        content=bio.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote(filename)}"},
    )


# ════════════════════════════════════════════════════════════════
# 阶段审批 API — 组员确认/驳回
# ════════════════════════════════════════════════════════════════

class StageApprovalRequest(BaseModel):
    """阶段确认请求 — v6: project_id 兼容整数主键和项目名

    - project_id: int (projects.id) 或 str (项目名/目录名, 自动解析)
    - member_id:  users.id (整数主键)
    - stage_id:   阶段编号 (1=结构分析, 2=功能, 3=失效, 4=风险, 5=优化, 6=报告)
    """
    project_id: Union[int, str] = Field(..., description="项目ID（整数主键或项目名）")
    stage_id: int = Field(..., description="阶段编号 (1-6)")
    member_id: int = Field(..., description="成员主键 users.id")
    comment: str = Field("", description="审批意见")


def _resolve_pid(req) -> Optional[int]:
    """从请求中解析 project_id 为 DB 整数主键，兼容 int / str。
    
    如果已经是 int 直接返回；如果是 str 则通过 _resolve_db_project_id 查询。
    查不到返回 None。
    """
    if isinstance(req.project_id, int):
        return req.project_id
    if isinstance(req.project_id, str):
        db_pid = _resolve_db_project_id(req.project_id)
        if db_pid is not None:
            return db_pid
        # 也许传入的就是 DB 主键的字符串表示
        try:
            return int(req.project_id)
        except (ValueError, TypeError):
            pass
    return None


@app.post("/api/approval/approve")
async def approval_approve(req: StageApprovalRequest):
    """成员确认通过某个阶段 — 写 fmea_approvals.status='submitted' (统一数据源)."""
    db_pid = _resolve_pid(req)
    if db_pid is None:
        return JSONResponse({"success": False, "error": "项目不存在"}, status_code=404)
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO fmea_approvals
                        (project_id, user_id, stage_no, status, comment, approved_at, updated_at)
                    VALUES (%s, %s, %s, 'submitted', %s, NOW(), NOW())
                    ON CONFLICT (project_id, user_id, stage_no)
                    DO UPDATE SET
                        status = 'submitted',
                        comment = EXCLUDED.comment,
                        approved_at = NOW(),
                        lead_status = 'pending',
                        lead_comment = '',
                        lead_reviewed_at = NULL,
                        updated_at = NOW()
                """, (db_pid, req.member_id, req.stage_id, req.comment or ""))
            conn.commit()
    except Exception as e:
        logger.exception(f"[Approval] approval_approve 失败: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    # 统计
    with get_user_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status IN ('submitted', 'approved')) AS approved,
                    COUNT(*) AS total
                FROM fmea_approvals
                WHERE project_id = %s AND stage_no = %s
            """, (db_pid, req.stage_id))
            row = cur.fetchone()
    approved_count = int(row[0] or 0) if row else 0
    total_count = int(row[1] or 0) if row else 0
    is_all = total_count > 0 and approved_count >= total_count
    return {
        "success": True, "stage_id": req.stage_id, "member_id": req.member_id,
        "status": "submitted",
        "approved_count": approved_count, "total_count": total_count,
        "is_all_approved": is_all,
    }


@app.post("/api/approval/reject")
async def approval_reject(req: StageApprovalRequest):
    """成员驳回某个阶段 — 写 fmea_approvals.status='rejected'."""
    db_pid = _resolve_pid(req)
    if db_pid is None:
        return JSONResponse({"success": False, "error": "项目不存在"}, status_code=404)
    if not req.comment or not req.comment.strip():
        return JSONResponse({"success": False, "error": "驳回必须填写理由"}, status_code=400)
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO fmea_approvals
                        (project_id, user_id, stage_no, status, comment, approved_at, updated_at)
                    VALUES (%s, %s, %s, 'rejected', %s, NOW(), NOW())
                    ON CONFLICT (project_id, user_id, stage_no)
                    DO UPDATE SET
                        status = 'rejected',
                        comment = EXCLUDED.comment,
                        approved_at = NOW(),
                        updated_at = NOW()
                """, (db_pid, req.member_id, req.stage_id, req.comment))
            conn.commit()
    except Exception as e:
        logger.exception(f"[Approval] approval_reject 失败: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    return {"success": True, "stage_id": req.stage_id, "member_id": req.member_id, "status": "rejected"}


@app.get("/api/approval/status")
async def approval_status(project_id: str = "", stage_id: Optional[int] = None):
    """获取审批状态"""
    pipeline = get_or_create_pipeline()
    result = pipeline.get_approval_status(stage_id)
    return JSONResponse(result)


@app.post("/api/approval/check")
async def approval_check(project_id: str = ""):
    """定时检查审批状态（触发推送提醒 + 检查是否全员通过）"""
    pipeline = get_or_create_pipeline()
    result = await pipeline.check_approval_and_proceed()
    return JSONResponse(result)


# ═══ v5: 负责人一对一审核 API ═══

def _ensure_orchestrator_project(pipeline, project_id: str) -> list:
    """v5: 确保 orchestrator 设置了项目，返回成员列表"""
    orch = pipeline.orchestrator
    # 从 project_users / project_members 拿成员
    members = []
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT u.display_name, u.username FROM project_members pm
                    JOIN users u ON u.id = pm.user_id
                    WHERE pm.project_id = (
                      SELECT id FROM projects WHERE id = %s OR name = %s LIMIT 1
                    )
                """, (project_id, project_id))
                rows = cur.fetchall() or []
                for r in rows:
                    name = (r[0] or r[1]) if isinstance(r, tuple) else (r.get("display_name") or r.get("username"))
                    if name:
                        members.append(str(name).strip())
    except Exception as e:
        logger.warning(f"[Approval] 读项目成员失败: {e}")

    # 从 planning.json 兜底
    if not members:
        try:
            plan_path = DATA_DIR / project_id / "fmea" / "00-planning.json"
            if plan_path.exists():
                import json as _json
                with open(plan_path, "r", encoding="utf-8") as f:
                    plan = _json.load(f)
                tms = plan.get("team_members", [])
                if isinstance(tms, str):
                    members = [m.strip() for m in tms.replace(",", " ").split() if m.strip()]
                else:
                    members = [str(m).strip() for m in tms if m]
        except Exception:
            pass

    # 设置 orchestrator
    orch.set_project(project_id, project_id, members)
    return members


# ═══ v6: 负责人一对一审核 (直接读写 fmea_approvals 表) ═══
#
# 接口参数:
#   project_id: int  → projects.id 主键
#   member_id:  int  → users.id 主键
#   stage_id:   int  → 阶段编号
# 数据流:
#   工程师确认 → fmea_approvals.status = 'submitted'   (step-approval 接口)
#   负责人读   → fmea_approvals.lead_status = 'read'
#   负责人通过 → fmea_approvals.lead_status = 'approved' + status='approved'
#   负责人驳回 → fmea_approvals.lead_status = 'rejected' + 工程师 status 重置为 'pending'
# 注意: 不再读写 orchestrator._state / approval_state.json (那套早已废弃)


@app.post("/api/approval/leader-read")
async def approval_leader_read(req: StageApprovalRequest):
    """负责人已读某工程师的提交 — 在 fmea_approvals 表上更新 lead_status='read'."""
    db_pid = _resolve_pid(req)
    if db_pid is None:
        return JSONResponse({"success": False, "error": "项目不存在"}, status_code=404)
    row = _get_member_approval_row(db_pid, req.stage_id, req.member_id)
    if not row:
        return JSONResponse(
            {"success": False, "error": f"该成员在阶段{req.stage_id}暂无提交记录"},
            status_code=404,
        )
    if row["status"] not in ("submitted", "approved"):
        return JSONResponse(
            {"success": False, "error": f"该成员尚未提交，无法标记已读"},
            status_code=400,
        )
    ok = _update_leader_status(db_pid, req.stage_id, req.member_id,
                                lead_status="read", lead_comment="")
    if not ok:
        return JSONResponse({"success": False, "error": "更新失败，请重试"}, status_code=500)
    return {
        "success": True,
        "stage_id": req.stage_id,
        "member_id": req.member_id,
        "lead_status": "read",
    }


@app.post("/api/approval/leader-approve")
async def approval_leader_approve(req: StageApprovalRequest):
    """负责人通过某工程师的提交 — 更新 fmea_approvals.lead_status='approved'."""
    db_pid = _resolve_pid(req)
    if db_pid is None:
        return JSONResponse({"success": False, "error": "项目不存在"}, status_code=404)
    row = _get_member_approval_row(db_pid, req.stage_id, req.member_id)
    if not row:
        return JSONResponse(
            {"success": False, "error": f"该成员在阶段{req.stage_id}暂无提交记录"},
            status_code=404,
        )
    if row["status"] not in ("submitted", "approved"):
        return JSONResponse(
            {"success": False, "error": f"该成员尚未提交，无法审核"},
            status_code=400,
        )
    ok = _update_leader_status(
        db_pid, req.stage_id, req.member_id,
        lead_status="approved", lead_comment=req.comment or "",
        also_approve_engineer=True,
    )
    if not ok:
        return JSONResponse({"success": False, "error": "更新失败，请重试"}, status_code=500)

    is_all = _is_all_lead_approved(db_pid, req.stage_id)
    logger.info(
        f"[Approval] 负责人通过 user_id={req.member_id} 阶段{req.stage_id} "
        f"(全员已通过: {is_all})"
    )
    return {
        "success": True,
        "stage_id": req.stage_id,
        "member_id": req.member_id,
        "lead_status": "approved",
        "is_all_lead_approved": is_all,
    }


@app.post("/api/approval/leader-reject")
async def approval_leader_reject(req: StageApprovalRequest):
    """负责人驳回某工程师的提交 — 更新 fmea_approvals.lead_status='rejected'.

    驳回后把工程师的 status 重置为 'pending'，让该工程师可重新提交。
    """
    db_pid = _resolve_pid(req)
    if db_pid is None:
        return JSONResponse({"success": False, "error": "项目不存在"}, status_code=404)
    if not req.comment or not req.comment.strip():
        return JSONResponse(
            {"success": False, "error": "驳回必须填写理由"},
            status_code=400,
        )
    row = _get_member_approval_row(db_pid, req.stage_id, req.member_id)
    if not row:
        return JSONResponse(
            {"success": False, "error": f"该成员在阶段{req.stage_id}暂无提交记录"},
            status_code=404,
        )

    # 驳回: lead_status='rejected' + status 也设为 rejected（工程师看到"已驳回"状态栏）
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE fmea_approvals
                    SET lead_status = 'rejected',
                        lead_comment = %s,
                        lead_reviewed_at = NOW(),
                        status = 'rejected',
                        updated_at = NOW()
                    WHERE project_id = %s AND user_id = %s AND stage_no = %s
                """, (req.comment, db_pid, req.member_id, req.stage_id))
                updated = cur.rowcount > 0
            conn.commit()
    except Exception as e:
        logger.exception(f"[Approval] leader-reject 失败: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    if not updated:
        return JSONResponse({"success": False, "error": "驳回失败，记录未更新"}, status_code=500)

    logger.info(
        f"[Approval] 负责人驳回 user_id={req.member_id} 阶段{req.stage_id}: {req.comment}"
    )
    return {
        "success": True,
        "stage_id": req.stage_id,
        "member_id": req.member_id,
        "lead_status": "rejected",
        "comment": req.comment,
    }


# ════════════════════════════════════════════════════════════════
# 启动
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  FMEA AgentOS — 失效模式与影响分析智能体")
    print(f"  模型: {MODEL_NAME} | 7步 FMEA Pipeline")
    print(f"  项目路径: {PROJECT_ROOT}")
    print("=" * 60)
    print(f"\n  登录页面:          http://{SERVER_HOST}:{SERVER_PORT}/login")
    print(f"  管理控制台:       http://{SERVER_HOST}:{SERVER_PORT}/")
    print(f"  FMEA 工作台:      http://{SERVER_HOST}:{SERVER_PORT}/chat")
    print(f"  团队管理:         http://{SERVER_HOST}:{SERVER_PORT}/team")
    print(f"  智能体管理:       http://{SERVER_HOST}:{SERVER_PORT}/agent")
    print(f"  Swagger API:      http://{SERVER_HOST}:{SERVER_PORT}/docs")
    print(f"  项目初始化:       POST /api/projects/init  (SSE)")
    print(f"  # [已注释] 知识库查询: POST /api/kb/query")
    print(f"  知识库状态:       GET /api/kb/status")
    print(f"  AG-UI SSE:        POST /agui/agui")
    print(f"  健康检查:         http://{SERVER_HOST}:{SERVER_PORT}/health\n")

    # 确保数据目录存在
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Path("tmp").mkdir(parents=True, exist_ok=True)

    agent_os.serve(app="main:app", host=SERVER_HOST, port=SERVER_PORT)
