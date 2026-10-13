"""
项目初始化工具 — 注册为 Agent tools

提供:
- init_project_tool: 创建项目 + SOR文档转MD + 构建知识库
- # [已注释] build_structure_tree_tool（依赖 kb_query，见 MiniMax 退化问题）
- extract_structure_tree_tool: 渐进式遍历所有材料，分批提取结构树（自循环+自优化）
- extract_structure_tree_from_weknora_tool: 【推荐】从 WeKnora 知识库提取结构树
- # [已注释] query_kb_tool（依赖 kb_query，见 MiniMax 退化问题）
"""
import json
import logging
import os
import re
import time
import asyncio
import threading
from pathlib import Path
from typing import Optional, Dict

from knowledge.kb_service import get_kb_service
# [已注释] kb_query（MiniMax 模型退化循环问题）
# from knowledge.kb_query import get_query_engine
from engine.llm_client import get_llm_client

logger = logging.getLogger(__name__)

# 启动确认日志
logger.info("✅ project_init_tools 模块已加载 (含 extract_structure_tree_tool 等 7 个工具)")

# ═══════════════════════════════════════════════════════════
# 全局工具进度状态（供 SSE 端点读取）
# ═══════════════════════════════════════════════════════════

_tool_progress_lock = threading.RLock()
_tool_progress: Dict[str, dict] = {}
_init_progress: Dict[str, dict] = {}


def _set_tool_progress(project_id: str, data: dict):
    """写入工具进度（线程安全）

    v6.3.3: 自动同时写入目录名 key 和 db_pid key，
    确保前端 EventSource（用 db_pid 做 key）能拉到数据。
    """
    with _tool_progress_lock:
        _tool_progress[project_id] = data
        # 同时用 db_pid 再写一份（前端 SSE 用 db_pid 轮询）
        try:
            from engine.tools.fmea_helpers import resolve_project_id_to_db_pid
            db_pid = resolve_project_id_to_db_pid(project_id)
            if db_pid:
                _tool_progress[str(db_pid)] = data
        except Exception:
            pass  # DB 查询失败不影响正常写入


def _get_tool_progress(project_id: str) -> Optional[dict]:
    """读取工具进度（线程安全）"""
    with _tool_progress_lock:
        return _tool_progress.get(project_id)


def get_all_tool_progress() -> Dict[str, dict]:
    """读取全部工具进度（线程安全）"""
    with _tool_progress_lock:
        return dict(_tool_progress)


def _get_data_dir() -> Path:
    """获取项目数据根目录"""
    from pathlib import Path as _Path
    base = _Path(__file__).resolve().parent.parent.parent  # engine/tools -> fmea-agent root
    data_dir = _Path(os.getenv("FMEA_DATA_DIR", str(base / "data" / "projects")))
    return data_dir


def _resolve_project_dir(project_id: str) -> Path:
    """
    根据 project_id 解析出正确的项目数据目录。

    project_id 可能是以下任一格式：
    1. 项目目录名（如 "12V低压模块"）           — 直接使用
    2. 知识库 ID（如 "12V低压模块_20260605133802"）— 剥离时间戳后缀
    3. Pipeline ID（如 "proj-20260605131755"）    — 查 00-planning.json 中的 kb_project_id 反查

    ⛔ 返回路径严格限定在 data/projects/ 内，杜绝乱建文件夹。
    """
    import re as _re
    data_dir = _get_data_dir()

    # 情况 1: 直接匹配目录名
    if (data_dir / project_id).exists() and (data_dir / project_id).is_dir():
        return data_dir / project_id

    # 情况 2: 带时间戳后缀，剥离后匹配
    stripped = _re.sub(r'_\d{14}$', '', project_id)
    if stripped != project_id and (data_dir / stripped).exists():
        return data_dir / stripped

    # 情况 3: Pipeline ID（proj-xxx），扫描所有项目目录的 00-planning.json 查找匹配
    if project_id.startswith("proj-"):
        for proj_dir in data_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            planning_file = proj_dir / "fmea" / "00-planning.json"
            if planning_file.exists():
                try:
                    with open(planning_file, "r", encoding="utf-8") as f:
                        planning = json.load(f)
                    # 检查多种可能的 ID 字段
                    if (planning.get("project_id") == project_id or
                        planning.get("pipeline_id") == project_id):
                        return proj_dir
                except Exception:
                    pass

    # 情况 4: 都没找到，尝试在所有项目目录中模糊匹配
    for proj_dir in data_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        if project_id.startswith(proj_dir.name) or proj_dir.name.startswith(stripped):
            return proj_dir

    # 兜底：用剥离后的名称创建
    logger.warning(f"[ResolveDir] 未找到 project_id={project_id} 对应的项目目录，将创建: {data_dir / stripped}")
    return data_dir / stripped


def _save_step_result(project_id: str, step_name: str, data: dict) -> str:
    """
    v7: 只写入 DB（fmea_step_assignee_data），不再写本地 JSON 文件。

    参数:
        project_id: 项目ID（目录名）
        step_name:  步骤名，如 "01-structure-tree"
        data:       要保存的字典数据

    返回:
        "db://{step_name}" 格式的引用字符串
    """
    try:
        from engine.tools.fmea_helpers import upsert_baseline
        from main import _resolve_db_project_id
        _pid = _resolve_db_project_id(project_id)
        if _pid:
            ok = upsert_baseline(_pid, step_name.replace("01-", "").replace("-tree", ""), data)
            if ok:
                logger.info(f"[SaveStep] ✅ DB 写入成功: pid={_pid} step={step_name}")
            else:
                logger.error(f"[SaveStep] ❌ DB 写入失败(函数返回False): pid={_pid} step={step_name}")
    except Exception as e:
        logger.warning(f"[SaveStep] DB 写入失败: {e}")
    return f"db://{step_name}"


def _build_fmea_structure_table(nodes: list) -> list:
    """
    从 nodes 列表构建 FMEA 结构分析三列表。

    自动推断三列映射关系（通用逻辑，不依赖具体产品）：
    - 1. Next Higher Level: 节点的父节点名称
    - 2. Focus Element: 节点自身名称
    - 3. Next Lower Level: 节点的直接子节点列表

    对于零件级节点，第3列自动填充为"特性类 (Characteristic Type)"，
    并在下方列出该零件的关键特性（SC/CC标记+参数要求）。
    """
    # 构建 ID->name 查找表
    id_to_name = {n.get("id", ""): n.get("name", "") for n in nodes}
    # 构建 parent_id -> children 查找表
    parent_to_children = {}
    for n in nodes:
        pid = n.get("parent_id", "")
        if pid:
            parent_to_children.setdefault(pid, []).append(n)

    table = []
    for node in nodes:
        node_id = node.get("id", "")
        node_name = node.get("name", "")
        node_level = node.get("level", "")
        parent_id = node.get("parent_id", "")

        # 第1列: Next Higher Level
        next_higher = node.get("fmea_next_higher", "")
        if not next_higher and parent_id:
            next_higher = id_to_name.get(parent_id, "")

        # 第2列: Focus Element
        focus_element = node.get("fmea_focus_element", "") or node_name

        # 第3列: Next Lower Level or Characteristic Type
        next_lower = node.get("fmea_next_lower", "")
        if not next_lower:
            children = parent_to_children.get(node_id, [])
            if children:
                next_lower = ", ".join(c.get("name", "") for c in children[:5])
                if len(children) > 5:
                    next_lower += f"... (+{len(children)-5})"
            elif node_level == "零件":
                # 零件级节点，第3列填充特性类
                char_parts = []
                if node.get("specification"):
                    char_parts.append(f"规格: {node['specification']}")
                if node.get("material"):
                    char_parts.append(f"材料: {node['material']}")
                if node.get("is_special_char"):
                    sc_label = node.get("char_class", "SC")
                    char_parts.append(f"特性等级: {sc_label}")
                if node.get("char_requirement"):
                    char_parts.append(f"要求: {node['char_requirement']}")
                next_lower = "特性类: " + ("; ".join(char_parts) if char_parts else "待分析")

        row = {
            "node_id": node_id,
            "level": node_level,
            "fmea_next_higher": next_higher,
            "fmea_focus_element": focus_element,
            "fmea_next_lower": next_lower,
            "part_number": node.get("part_number", ""),
            "is_special_char": node.get("is_special_char", False),
            "char_class": node.get("char_class", ""),
        }
        table.append(row)

    return table


# ═══════════════════════════════════════════════════════════
# 工具 1: 初始化项目知识库
# ═══════════════════════════════════════════════════════════

def init_project_tool(project_name: str, sor_folder: str, project_id: str = "") -> str:
    """
    初始化项目知识库。

    将 SOR 文件夹中的所有文档（PDF/Word/Excel/PPT等）转换为 Markdown 格式，
    存入知识库目录 knowledge/<project_id>/，并生成索引供后续查询。

    参数:
        project_name: 项目名称（如 "12V低压电池"）
        sor_folder: SOR 文件夹的绝对路径（如 "D:/projects/SOR-ZS32-X20"）
        project_id: 项目ID（可选，不提供则自动检测已有项目或自动生成）

    返回:
        JSON 格式的初始化结果，包含转换文件数、知识库状态等
    """
    import datetime
    import re as _re2

    global _init_progress

    # 生成项目ID — 优先使用已有项目目录的 ID
    if not project_id:
        # 1) 检查 data/projects/ 下是否有匹配项目名称的目录
        data_dir = _get_data_dir()
        if data_dir.exists():
            existing_dirs = sorted(data_dir.iterdir(), reverse=True)
            # 先精确匹配
            for d in existing_dirs:
                if d.is_dir() and d.name == project_name:
                    project_id = d.name
                    logger.info(f"[InitTool] 检测到已有项目目录，复用 project_id={project_id}")
                    break
            if not project_id:
                # 再模糊匹配：已有目录名以 project_name 开头（之前用 create_project_tool 创建的）
                for d in existing_dirs:
                    if d.is_dir() and d.name.startswith(project_name):
                        project_id = d.name
                        logger.info(f"[InitTool] 检测到已有项目目录（模糊匹配），复用 project_id={project_id}")
                        break

    if not project_id:
        safe_name = project_name.replace(" ", "_").replace("/", "_")
        project_id = f"{safe_name}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"

    _init_progress[project_id] = {
        "phase": "init",
        "progress": 0.0,
        "message": "准备初始化...",
        "started_at": datetime.datetime.now().isoformat(),
    }

    kb = get_kb_service()

    def _progress_callback(phase: str, progress: float, message: str):
        _init_progress[project_id] = {
            "phase": phase,
            "progress": progress,
            "message": message,
            "updated_at": datetime.datetime.now().isoformat(),
        }
        logger.info(f"[InitTool] {project_id}: [{phase}] {progress:.0%} - {message}")

    try:
        # 校验路径
        sor_path = Path(sor_folder)
        if not sor_path.exists():
            return json.dumps({
                "success": False,
                "error": f"SOR 文件夹不存在: {sor_folder}",
            }, ensure_ascii=False)

        # 执行初始化
        result = kb.init_project_knowledge(
            project_id=project_id,
            project_name=project_name,
            sor_folder=sor_folder,
            progress_callback=_progress_callback,
        )

        conv = result.get("conversion", {})
        idx = result.get("index", {})

        return json.dumps({
            "success": True,
            "project_id": project_id,
            "project_name": project_name,
            "total_files": conv.get("total", 0),
            "converted": conv.get("converted", 0),
            "failed": conv.get("failed", 0),
            "skipped": conv.get("skipped", 0),
            "kb_pages": idx.get("page_count", 0),
            "kb_dir": result.get("kb_dir", ""),
            "message": f"✅ 知识库初始化完成：{conv.get('converted', 0)}/{conv.get('total', 0)} 个文档已转换",
        }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"[InitTool] 初始化失败: {e}")
        _init_progress[project_id] = {
            "phase": "error",
            "progress": 0.0,
            "message": f"初始化失败: {str(e)}",
        }
        return json.dumps({
            "success": False,
            "error": str(e),
            "project_id": project_id,
        }, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
# 工具 2: 获取初始化进度
# ═══════════════════════════════════════════════════════════

def get_init_progress_tool(project_id: str) -> str:
    """
    获取项目知识库初始化进度。

    参数:
        project_id: 项目ID

    返回:
        JSON 格式的进度信息
    """
    global _init_progress
    progress = _init_progress.get(project_id, {})
    return json.dumps({
        "project_id": project_id,
        "progress": progress.get("progress", 1.0),
        "phase": progress.get("phase", "unknown"),
        "message": progress.get("message", "无初始化记录"),
    }, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
# [已注释] 工具 3: 基于知识库构建结构树 (React 范式) — 依赖 kb_query（MiniMax 退化问题）
# async def build_structure_tree_tool(project_id: str) -> str:
#     ...（已注释，完整源码见原始 git 历史）


async def extract_structure_tree_tool(project_id: str, extra_question: str = "") -> str:
    """
    从 WeKnora 知识库提取 FMEA 主体对象的产品结构树，自动保存为 JSON。

    前置条件：项目必须已通过 init_project_tool 初始化，并且 project_meta.json 中
    包含 weknora_kb_id 字段（指向 WeKnora 知识库 UUID）。

    v7: 如果 DB 中已有结构树数据，直接跳过，避免重复 KB 扫描。

    工作流（全部走 WeKnora 双通道）：
    1. 通过 WeKnora.hybrid_search() 识别与主体对象相关的文件
    2. 自适应分批 + 全文预览 + LLM 迭代提取（STRUCTURE_TREE_SYSTEM prompt）
    3. 节点合并 → FMEA 三列填充 → 最终 LLM 调整（不新增节点）
    4. clean_structure_nodes 清理（去重、重新编号、合法性校验）
    5. 保存为 01-structure-tree.json

    参数:
        project_id: 项目ID
        extra_question: 额外引导问题（保留兼容，当前未使用）

    返回:
        JSON 格式的结构树提取结果，包含 saved_to 字段指示保存位置
    """
    logger.info(f"[ExtractTreeTool] 开始 WeKnora 提取: project_id={project_id}")

    # v20.3: weknora_kb_id 优先从 projects 表读（v18 后不再写 project_meta.json），
    #         兜底从 project_meta.json 读
    weknora_kb_id = ""
    project_name = project_id
    subject = ""
    try:
        from main import get_user_db_conn
        conn = get_user_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT name, weknora_kb_id, subject
                    FROM projects
                    WHERE id::text = %s OR name = %s
                    LIMIT 1
                """, (str(project_id), str(project_id)))
                row = cur.fetchone()
                if row:
                    project_name = row[0] or project_id
                    weknora_kb_id = row[1] or ""
                    subject = row[2] or ""
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[ExtractTreeTool] 从 projects 表读 weknora_kb_id 失败: {e}")
    # 兜底：project_meta.json（历史项目兼容）
    if not weknora_kb_id:
        try:
            kb_svc = get_kb_service()
            meta = kb_svc._load_project_meta(project_id)
            project_name = meta.get("project_name", project_name) or project_name
            weknora_kb_id = meta.get("weknora_kb_id", "") or weknora_kb_id
            subject = meta.get("subject", "") or subject
        except Exception:
            pass

    if not weknora_kb_id:
        return json.dumps({
            "success": False,
            "error": "projects 表和 project_meta.json 都没有 weknora_kb_id，请先使用「🚀 初始化项目」关联 WeKnora 知识库。",
            "project_id": project_id,
        }, ensure_ascii=False)

    # 延迟引用（WeKnora 工具在文件后面定义）
    from engine.tools.project_init_tools import extract_structure_tree_from_weknora_tool
    return await extract_structure_tree_from_weknora_tool(
        project_id=project_id,
        weknora_kb_id=weknora_kb_id,
        subject=subject or project_name,
    )


# ═══════════════════════════════════════════════════════════
# 工具 4: 查询知识库
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
# 工具 4: 构建 LLM-Wiki 知识库（Karpathy LLM-Wiki 设计模式）
# ═══════════════════════════════════════════════════════════

async def build_knowledge_base_tool(project_id: str) -> str:
    """
    按 Karpathy LLM-Wiki 设计模式构建项目知识库。

    三层架构:
    - sources/  — 原始文档转换（不可变，LLM 只读）
    - entities/ — LLM 提取的实体页（产品/组件/材料/标准）
    - concepts/ — LLM 提取的概念页（要求/规范/方法/指南）

    工作流:
    Phase 1: 加载 SOR 文件清单
    Phase 2: 逐文件转换为 raw MD → 存入 sources/
    Phase 3: LLM 批量处理源文档 → 生成 entities/ 和 concepts/ wiki 页面
    Phase 4: 生成 index.md（按知识类别组织）+ log.md（追加式日志）

    参数:
        project_id: 项目ID
    """
    logger.info(f"[BuildKBTool] 按 LLM-Wiki 模式构建知识库: project_id={project_id}")

    kb_svc = get_kb_service()

    # ── Phase 1: 读取元数据 ──
    try:
        meta = kb_svc._load_project_meta(project_id)
        sor_folder = meta.get("sor_folder", "")
        project_name = meta.get("project_name", project_id)
    except Exception:
        sor_folder = ""

    if not sor_folder or not Path(sor_folder).exists():
        return json.dumps({
            "success": False,
            "error": f"无法找到项目 SOR 文件夹路径（project_id={project_id}）",
        }, ensure_ascii=False)

    kb_dir = kb_svc.base_dir / project_id
    kb_dir.mkdir(parents=True, exist_ok=True)

    # ── 校验：知识库已存在则跳过构建 ──
    sources_dir = kb_dir / "sources"
    index_path = kb_dir / "index.md"
    if sources_dir.exists() and index_path.exists():
        # 统计已有状态
        existing_sources = len(list(sources_dir.glob("*.md")))
        entities_dir = kb_dir / "entities"
        concepts_dir = kb_dir / "concepts"
        existing_entities = len(list(entities_dir.glob("*.md"))) if entities_dir.exists() else 0
        existing_concepts = len(list(concepts_dir.glob("*.md"))) if concepts_dir.exists() else 0
        logger.info(f"[BuildKBTool] ♻️ 知识库已存在，跳过构建: "
                     f"sources={existing_sources}, entities={existing_entities}, concepts={existing_concepts}")
        return json.dumps({
            "success": True,
            "project_id": project_id,
            "kb_dir": str(kb_dir),
            "sources": existing_sources,
            "entities": existing_entities,
            "concepts": existing_concepts,
            "total_pages": existing_sources + existing_entities + existing_concepts,
            "skipped": True,
            "message": (
                f"♻️ 知识库已存在，跳过构建（重复点击保护）\n"
                f"  - 源文档: {existing_sources} 篇\n"
                f"  - 实体页: {existing_entities} 个\n"
                f"  - 概念页: {existing_concepts} 个\n"
                f"\n⏭️ 下一步：【必须】立即调用 run_function_analysis_tool() 执行功能分析。"
            ),
        }, ensure_ascii=False)

    scan = meta.get("scan", {})
    files = scan.get("files", [])
    if not files:
        from knowledge.file_converter import scan_directory
        files = scan_directory(str(sor_folder))

    supported_exts = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".csv"}

    # ── Phase 2: 文档转换 → sources/ ──
    sources_dir = kb_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"[BuildKBTool] Phase 2: 转换 {len(files)} 个文件到 sources/...")
    converted = 0; failed = 0; skipped = 0
    sources_list = []   # 记录所有源文件，供后续 LLM 处理

    from knowledge.file_converter import load_document

    for i, f_info in enumerate(files):
        abs_path = f_info.get("abs_path", "")
        filename = f_info.get("filename", "")
        ext = f_info.get("extension", Path(filename).suffix).lower()
        if ext not in supported_exts or not abs_path:
            skipped += 1; continue
        if not Path(abs_path).exists():
            failed += 1; continue

        try:
            doc, _ = load_document(abs_path)
            text = doc.get("text", "")[:80000]
            safe_name = re.sub(r'[<>:"/\\|?*]', '_', Path(filename).stem) + ".md"
            md_path = sources_dir / safe_name

            md_content = (
                f"# {Path(filename).stem}\n\n"
                f"> 源文件: {filename}  |  类型: {ext}  |  "
                f"页数: {doc.get('meta', {}).get('page_count', '?')}\n\n"
                f"---\n\n{text}"
            )
            md_path.write_text(md_content, encoding="utf-8")
            sources_list.append({"name": Path(filename).stem, "file": safe_name, "ext": ext, "chars": len(text)})
            converted += 1
        except Exception as e:
            logger.warning(f"[BuildKBTool] 转换失败 {filename}: {e}")
            failed += 1

    logger.info(f"[BuildKBTool] Phase 2 完成: {converted} 转换, {failed} 失败, {skipped} 跳过")

    # ── Phase 3: LLM 批量处理源文档 → 生成 wiki 页面 ──
    logger.info(f"[BuildKBTool] Phase 3: LLM 生成 wiki 页面（实体+概念）...")
    entity_count, concept_count = 0, 0

    llm = get_llm_client()

    # 优先处理核心 SOR 文档（主文件+附件E），然后是其他文档
    core_docs = [s for s in sources_list
                 if "主文件" in s["name"] or "附件" in s["name"] or "零件信息" in s["name"]]
    other_docs = [s for s in sources_list if s not in core_docs]

    for batch_label, batch_docs in [("核心SOR文档", core_docs), ("SMTC标准规范", other_docs)]:
        if not batch_docs:
            continue
        logger.info(f"[BuildKBTool]   处理 {batch_label}: {len(batch_docs)} 篇")

        # 分批读取源文档内容（每批不超过 10 个，控制上下文大小）
        for b_idx in range(0, len(batch_docs), 10):
            batch = batch_docs[b_idx:b_idx+10]
            source_content = ""
            for s in batch:
                try:
                    src_path = sources_dir / s["file"]
                    if src_path.exists():
                        content = src_path.read_text(encoding="utf-8")
                        # 截断长文档
                        source_content += f"\n\n=== 源文档: {s['name']} ({s['ext']}, {s['chars']}字) ===\n{content[:15000]}\n"
                except Exception:
                    pass

            if not source_content.strip():
                continue

            # 让 LLM 从源文档中提取实体和概念
            wiki_pages = await _llm_generate_wiki_pages(llm, source_content, project_name)
            entity_count += len(wiki_pages.get("entities", []))
            concept_count += len(wiki_pages.get("concepts", []))

            # 写入实体页
            entities_dir = kb_dir / "entities"
            entities_dir.mkdir(parents=True, exist_ok=True)
            for ent in wiki_pages.get("entities", []):
                ename = re.sub(r'[<>:"/\\|?*]', '_', ent["name"]) + ".md"
                (entities_dir / ename).write_text(
                    f"# {ent['name']}\n\n"
                    f"> 类型: {ent.get('type', '实体')}  |  "
                    f"来源: {', '.join(ent.get('sources', [])[:3])}\n\n"
                    f"## 概述\n{ent.get('description', '')}\n\n"
                    f"## 关键参数\n{ent.get('key_params', '')}\n\n"
                    f"## 关联\n{ent.get('relations', '')}\n",
                    encoding="utf-8"
                )

            # 写入概念页
            concepts_dir = kb_dir / "concepts"
            concepts_dir.mkdir(parents=True, exist_ok=True)
            for con in wiki_pages.get("concepts", []):
                cname = re.sub(r'[<>:"/\\|?*]', '_', con["name"]) + ".md"
                (concepts_dir / cname).write_text(
                    f"# {con['name']}\n\n"
                    f"> 类别: {con.get('category', '概念')}  |  "
                    f"来源: {', '.join(con.get('sources', [])[:3])}\n\n"
                    f"## 说明\n{con.get('description', '')}\n\n"
                    f"## 要点\n{con.get('key_points', '')}\n\n"
                    f"## 关联实体\n{con.get('related_entities', '')}\n",
                    encoding="utf-8"
                )

    logger.info(f"[BuildKBTool] Phase 3 完成: {entity_count} 实体页, {concept_count} 概念页")

    # ── Phase 4: 生成 index.md + log.md ──
    logger.info(f"[BuildKBTool] Phase 4: 生成索引和日志...")

    # 4a: 收集所有 wiki 页面
    source_pages = []
    for f in sorted(sources_dir.glob("*.md")):
        content = f.read_text(encoding="utf-8")[:200]
        summary = content.split("\n")[0].replace("# ", "")[:80] if content else f.stem
        source_pages.append((f.name, summary))

    entity_pages = []
    edir = kb_dir / "entities"
    if edir.exists():
        for f in sorted(edir.glob("*.md")):
            entity_pages.append((f.name, f.stem))

    concept_pages = []
    cdir = kb_dir / "concepts"
    if cdir.exists():
        for f in sorted(cdir.glob("*.md")):
            concept_pages.append((f.name, f.stem))

    # 4b: 写 index.md（按知识类别组织）
    index_lines = [
        f"# {project_name} — 知识库索引",
        f"",
        f"> 生成: {time.strftime('%Y-%m-%d %H:%M')}  |  "
        f"源文档: {len(source_pages)}  |  实体: {len(entity_pages)}  |  概念: {len(concept_pages)}",
        f"",
        "---",
        f"",
        f"## 📄 源文档 ({len(source_pages)})",
        f"",
    ]
    for fname, summary in source_pages:
        index_lines.append(f"- [{summary}](sources/{fname})")

    index_lines.extend([f"", f"## 🏷️ 实体 ({len(entity_pages)})", f""])
    for fname, name in entity_pages:
        index_lines.append(f"- [{name}](entities/{fname})")

    index_lines.extend([f"", f"## 📐 概念 ({len(concept_pages)})", f""])
    for fname, name in concept_pages:
        index_lines.append(f"- [{name}](concepts/{fname})")

    index_lines.extend(["", "---", "", "## 🔍 使用方式", "",
        "1. 查询时先读本索引找到相关页面", "2. 再深入对应页面获取详细信息",
        "3. 有价值的答案可归档为新 wiki 页面", ""])

    (kb_dir / "index.md").write_text("\n".join(index_lines), encoding="utf-8")

    # 4c: 追加 log.md
    log_path = kb_dir / "log.md"
    log_entry = (
        f"## [{time.strftime('%Y-%m-%d %H:%M')}] ingest\n"
        f"- 源文档: {converted} 篇\n"
        f"- 生成实体页: {entity_count} 个\n"
        f"- 生成概念页: {concept_count} 个\n"
        f"- 索引更新: index.md\n\n"
    )
    existing = log_path.read_text(encoding="utf-8") if log_path.exists() else "# 知识库操作日志\n\n"
    log_path.write_text(existing + log_entry, encoding="utf-8")

    # 旧版兼容：同时生成 MOC.md
    (kb_dir / "MOC.md").write_text(
        f"# {project_name} — 内容地图\n\n"
        f"## 按类别浏览\n"
        f"- [源文档 (sources/)](sources/) — {len(source_pages)} 篇\n"
        f"- [实体 (entities/)](entities/) — {len(entity_pages)} 个\n"
        f"- [概念 (concepts/)](concepts/) — {len(concept_pages)} 个\n\n"
        f"## 全文搜索\n请使用查询功能搜索知识库内容。\n"
    )

    return json.dumps({
        "success": True,
        "project_id": project_id,
        "kb_dir": str(kb_dir),
        "sources": converted,
        "entities": entity_count,
        "concepts": concept_count,
        "total_pages": len(source_pages) + entity_count + concept_count,
        "message": (
            f"✅ LLM-Wiki 知识库构建完成！\n"
            f"  - 源文档: {converted} 篇 (sources/)\n"
            f"  - 实体页: {entity_count} 个 (entities/)\n"
            f"  - 概念页: {concept_count} 个 (concepts/)\n"
            f"  - 索引: index.md + log.md\n"
            f"  - 目录: {kb_dir}\n"
            f"\n⏭️ 下一步：【必须】立即调用 run_function_analysis_tool() 执行功能分析。"
        ),
    }, ensure_ascii=False)


async def _llm_generate_wiki_pages(llm, source_content: str, project_name: str) -> dict:
    """让 LLM 从源文档中提取实体和概念，生成 wiki 页面内容"""
    prompt = f"""你是 {project_name} 知识库的 Wiki 维护者。请从以下源文档中提取关键知识，生成结构化的 wiki 页面。

== 源文档内容（可能已截断） ==
{source_content[:60000]}

请按以下格式返回 JSON，每个实体/概念只提取一次（去重）：

{{
  "entities": [
    {{
      "name": "实体名称（产品名/组件名/零件名/标准编号等）",
      "type": "产品/组件/零件/标准/材料/接口",
      "description": "一句话概述这个实体是什么",
      "key_params": "关键参数或规格（如电压、容量、尺寸、温度范围等）",
      "sources": ["来源文档名列表"],
      "relations": "与哪些其他实体或概念有关联"
    }}
  ],
  "concepts": [
    {{
      "name": "概念名称（设计要求/测试方法/规范条款/技术方案等）",
      "category": "设计要求/测试规范/技术方案/法规要求/接口规范",
      "description": "一句话概述这个概念",
      "key_points": "关键要点，用分号分隔多个要点",
      "sources": ["来源文档名列表"],
      "related_entities": "与哪些实体相关"
    }}
  ]
}}

规则:
1. 只提取文档中明确出现的信息，不要编造
2. 实体应具体明确（如"MCU主控芯片"），概念应抽象通用（如"硬件接口设计要求"）
3. name 要简短（不超过30字），description 要精炼（1-2句）
4. 相似或重复的实体/概念应合并，不要出现两个不同名称指代同一事物
5. 优先提取与产品核心功能直接相关的内容"""

    from engine.llm_client import get_llm_client
    if not llm:
        llm = get_llm_client()

    for attempt in range(2):
        try:
            response = await llm.call_complete(
                system_prompt="你是知识库 Wiki 维护专家。只返回 JSON，不返回其他内容。",
                user_message=prompt,
                force_json=False,
            )
            if not response:
                continue
            data = llm.extract_json(response)
            if isinstance(data, dict) and ("entities" in data or "concepts" in data):
                return data
            # 尝试正则提取 JSON
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                data = json.loads(json_match.group(0))
                if isinstance(data, dict) and ("entities" in data or "concepts" in data):
                    return data
        except Exception as e:
            logger.warning(f"[BuildKBTool] LLM wiki 生成失败 (attempt {attempt+1}): {e}")

    return {"entities": [], "concepts": []}


# [已注释] query_kb_tool — 依赖 kb_query（MiniMax 退化问题）
# 完整源码见原始 git 历史
# def query_kb_tool(project_id: str, question: str) -> str:



# ═══════════════════════════════════════════════════════════
# 工具 5: 获取知识库状态
# ═══════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
# 工具 5: 获取知识库状态
# ═══════════════════════════════════════════════════════════

def get_kb_status_tool(project_id: str) -> str:
    """
    获取项目知识库状态。

    参数:
        project_id: 项目ID

    返回:
        JSON 格式的知识库状态
    """
    kb = get_kb_service()
    status = kb.get_status(project_id)

    return json.dumps({
        "success": True,
        "status": status,
    }, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
# 工具 6: 列出知识库页面
# ═══════════════════════════════════════════════════════════

def list_kb_pages_tool(project_id: str) -> str:
    """
    列出项目知识库中的所有页面。

    参数:
        project_id: 项目ID

    返回:
        JSON 格式的页面列表
    """
    kb = get_kb_service()
    pages = kb.list_pages(project_id)

    return json.dumps({
        "success": True,
        "total": len(pages),
        "pages": [
            {
                "filename": p.get("filename", ""),
                "title": p.get("title", ""),
                "summary": p.get("summary", "")[:100],
                "source_type": p.get("source_type", ""),
            }
            for p in pages
        ],
    }, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
# 工具 7: 从 WeKnora 知识库提取结构树（双通道：搜索 + 预览）
# ═══════════════════════════════════════════════════════════════

async def _extract_structure_tree_progressive_v41(
    project_id: str, weknora_kb_id: str, subject: str
) -> str:
    """
    从 WeKnora 知识库渐进式提取产品结构树（v4.1）。

    新逻辑：
    1. 获取知识库全部文件
    2. 按优先级分批（BOM/Excel 优先）
    3. 每批：LLM 提取节点 → 合并到已有树 → 检查收敛
    4. 收敛后：最终调整（禁止新增节点，只允许调整/合并）
    """
    from knowledge.weknora_client import get_weknora_client
    from engine.llm_client import get_llm_client, estimate_tokens, truncate_to_tokens
    from engine.prompts import STRUCTURE_TREE_SYSTEM

    # ⚠️ 一定要按 token 而不是字符限量：同样 40000 字符，ASCII（二进制乱码）约 1.2 万 token，
    #    真实中文约 4 万 token，差 3 倍。之前只按字符截断，换成真实文档后直接把模型打爆了。
    # 输入预算：字符上限沿用原值 40000，再叠一个 token 上限做硬约束。
    # 原实现只按字符截断，而同样字符数下 ASCII 与中文的 token 量能差 3 倍，量级不可控。
    # 上下文窗口 524288，整个语料合计约 16 万字符 / 8 万 token，
    # 默认预算下 2 批就能覆盖全部文档（原来是 5 批）。
    MAX_BATCH_CHARS = int(os.getenv("FMEA_STRUCT_BATCH_CHARS", "120000"))
    MAX_BATCH_TOKENS = int(os.getenv("FMEA_STRUCT_BATCH_TOKENS", "60000"))
    # 输出预算。⚠️ 别顶到上下文上限：实测 524288 会让推理模型思考 30 分钟不收敛，
    # 两次调用各撞超时，单批白等 1 小时。32768 实测 150s 内完成；
    # 再配合 FMEA_LLM_THINKING=off（默认关思考）通常几十秒就出结果。
    STRUCT_LLM_MAX_TOKENS = int(os.getenv("FMEA_STRUCT_MAX_TOKENS", "32768"))
    FILE_SIZE_THRESHOLD = 150000  # 单文件字符上限（原 5 万会截断"附件 I"这类长文档）

    logger.info(f"[WeKnoraExtract] 开始渐进提取: {subject}")

    _set_tool_progress(project_id, {
        "tool": "extract_structure_tree", "phase": "discovery",
        "message": "正在扫描知识库，识别相关文件...", "progress": 0.0,
        "nodes_count": 0, "updated_at": time.time(),
    })

    try:
        start_time = time.time()
        client = get_weknora_client()

        # ── 阶段 1：通过 WeKnora 搜索识别与产品相关的文件 ──
        all_files = client.list_all_knowledge(weknora_kb_id)
        if not all_files:
            return json.dumps({"success": False, "error": "知识库为空"}, ensure_ascii=False)

        logger.info(f"[WeKnoraExtract] 知识库共 {len(all_files)} 个文件，开始搜索相关文件")

        # 多轮搜索收集相关文件 ID（按"结构/电气/模组/BMS"四个总成关键词覆盖）
        relevant_ids: set = set()
        search_queries = [
            f"{subject} 结构总成 上盖 下箱体 底护板 防爆阀 导热 壳体 铭牌",
            f"{subject} 电气总成 铝排 高压连接 低压线束 BDU 连接器 MSD 接地",
            f"{subject} 模组总成 电芯 CCS 端板 侧板 气凝胶 隔热 结构胶",
            f"{subject} BMS总成 CMU BMU 电池管理系统 电压采样 温度 通讯",
            f"{subject} 物料清单 BOM 零件表 零部件 明细",
            f"{subject} 图纸 技术要求 工程图 装配图 材料牌号",
            f"{subject} DVP 试验规范 设计规格 特殊特性",
            f"{subject} 螺栓 扎带 垫片 线束 连接器 密封 泡棉",
        ]
        for query in search_queries:
            try:
                results = client.hybrid_search(weknora_kb_id, query_text=query, match_count=30)
                for r in results:
                    kid = r.get("knowledge_id", "")
                    if kid:
                        relevant_ids.add(kid)
            except Exception as e:
                logger.warning(f"[WeKnoraExtract] 搜索失败 '{query[:20]}': {e}")

        logger.info(f"[WeKnoraExtract] 搜索发现 {len(relevant_ids)} 个相关文件（共 {len(all_files)} 个）")

        # ── 阶段 2：过滤 + 排序相关文件 ──
        def file_priority(f):
            # 注意：WeKnora 的 file_type 是 "xlsx"/"docx"（不带点），这里要归一化
            ft = (f.get("file_type", "") or "").lower().lstrip(".")
            fn = (f.get("file_name", f.get("title", "")) or "").lower()
            if ft in ("xlsx", "xls", "csv"): return 10
            if "bom" in fn or "物料" in fn: return 9
            if "sor" in fn or "需求" in fn: return 8
            if "图纸" in fn or "电气" in fn: return 7
            if "方案" in fn or "设计报告" in fn: return 6
            return 2

        # 相关文件排前面，不相关的排后面（以防遗漏）
        relevant_files = []
        skipped_files = []
        for f in all_files:
            fid = f.get("id", "")
            if fid in relevant_ids:
                relevant_files.append(f)
            else:
                skipped_files.append(f)

        relevant_files.sort(key=file_priority, reverse=True)
        skipped_files.sort(key=file_priority, reverse=True)
        # 所有文件 = 相关文件 + 不相关文件（不相关的最多取 20 个作为兜底）
        queue = relevant_files + skipped_files[:20]
        logger.info(f"[WeKnoraExtract] 队列: {len(relevant_files)} 相关 + {len(skipped_files[:20])} 兜底 = {len(queue)} 个")

        # ── 阶段 3：自适应分批、全文读取、迭代提取 ──
        tree_nodes: dict = {}
        processed_ids: set = set()
        batch_idx = 0
        llm_failed_batches: list = []

        while True:
            # 取未处理的文件
            remaining = [f for f in queue if f["id"] not in processed_ids]
            if not remaining:
                logger.info(f"[WeKnoraExtract] 全部文件已处理（{batch_idx} 批）")
                break

            # 自适应组批：逐个加文件直到超出 MAX_BATCH_CHARS
            batch_files = []
            batch_chars = 0
            for f in remaining:
                fsize = f.get("file_size", 0)
                est_chars = fsize if fsize > 0 else FILE_SIZE_THRESHOLD
                if batch_chars + est_chars > MAX_BATCH_CHARS * 2 and batch_files:
                    break  # 这批复已足够
                batch_files.append(f)
                batch_chars += min(est_chars, FILE_SIZE_THRESHOLD)
                if batch_chars >= MAX_BATCH_CHARS:
                    break

            # 并发读取文件完整内容（不截断）
            async def _load_one(f):
                try:
                    fname = f.get("title") or f.get("file_name") or f["id"]
                    # ⚠️ preview 接口返回的是原始文件字节，必须走本地解析拿文本；
                    # 旧写法 client.preview_knowledge() 会被解码成二进制乱码。
                    # v24.2: 这一步是**同步阻塞**的（同步 httpx 下载 + pypdf/python-docx 解析），
                    #   直接跑在事件循环里会把整个 FastAPI 堵死——此刻前端发来的任何请求
                    #   （包括切 tab 取数据、/approvals-state）都会挂住，页面表现成"一直加载中"。
                    #   放进线程池执行，既不阻塞事件循环，也让多个文件真正并发。
                    text = await asyncio.to_thread(
                        client.preview_knowledge_text,
                        f["id"], fname, f.get("file_type", ""))
                    if not text:
                        logger.info(f"[WeKnoraExtract] 跳过（无可用文本）: {fname}")
                        return None
                    ft = (f.get("file_type", "") or "").lower().lstrip(".")
                    is_relevant = f["id"] in relevant_ids
                    tag = "" if is_relevant else " [兜底]"
                    # BOM Excel 文件截断至 60K（比一般文件略多，但避免溢出）
                    is_excel = ft in ("xlsx", "xls", "csv") or fname.lower().endswith(
                        (".xlsx", ".xls", ".csv"))
                    if is_excel:
                        trunc = text[:60000] if len(text) > 60000 else text
                        return {"type": "excel", "name": fname, "text": trunc, "is_relevant": is_relevant}
                    if len(text) > FILE_SIZE_THRESHOLD and is_relevant:
                        return f"=== {fname}{tag} ===\n{text[:FILE_SIZE_THRESHOLD]}"
                    return f"=== {fname}{tag} ===\n{text}"
                except Exception as e:
                    logger.warning(f"[WeKnoraExtract] 预览失败 {f['id']}: {e}")
                return None

            results = await asyncio.gather(*[_load_one(f) for f in batch_files])

            if not results:
                processed_ids.update(f["id"] for f in batch_files)
                continue

            # ── 分离 BOM Excel 和文本文件 ──
            excel_results = [r for r in results if isinstance(r, dict) and r.get("type") == "excel"]
            text_results = [r for r in results if isinstance(r, str)]
            batch_texts = [r for r in text_results if r]

            # BOM Excel 直接解析表格行 → 预提取零件节点（不用 LLM）
            bom_nodes_added = 0
            for excel_r in excel_results:
                raw_text = excel_r.get("text", "")
                fname = excel_r.get("name", "(BOM)")
                # 找表格行（以 | 分隔，至少 3 列）
                for line in raw_text.split("\n"):
                    parts = [c.strip() for c in line.split("|")]
                    if len(parts) < 3:
                        continue
                    # 跳过表头/汇总/空行
                    name_candidate = parts[2] if len(parts) > 2 else parts[1]
                    # 保留 U+FFFD 交给 _is_junk_node_name 识别（旧代码把它替换成 "?" 反而掩盖了乱码）
                    name_candidate = name_candidate.replace("\u200b", "").replace("\x00", "").strip()
                    if not name_candidate or len(name_candidate) > 80:
                        continue
                    if name_candidate.lower() in ("名称", "描述", "合计", "总计", "物料名称",
                        "part name", "description", "total", ""):
                        continue
                    if _is_junk_node_name(name_candidate):
                        logger.debug(f"[WeKnoraExtract] 丢弃疑似乱码行: {name_candidate[:60]!r}")
                        continue
                    # 判断是否已有：用 name+level 查重
                    node_name = name_candidate.lower()
                    exists = any(
                        (n.get("name","") or "").strip().lower() == node_name
                        and (n.get("level","") or "") in ("零件", "组件")
                        for n in tree_nodes.values()
                    )
                    if not exists:
                        # 默认判定为零件级（后续可被 LLM 升级到组件）
                        nid = f"bom-{len(tree_nodes)+1:04d}"
                        tree_nodes[nid] = {
                            "id": nid, "name": name_candidate, "level": "零件",
                            "parentId": "", "partNumber": "", "quantity": 1,
                            "material": "", "specification": "",
                        }
                        bom_nodes_added += 1
                logger.info(f"[WeKnoraExtract] BOM分析: {fname[:30]} → +{bom_nodes_added} 零件节点")

            if not batch_texts and not bom_nodes_added:
                processed_ids.update(f["id"] for f in batch_files)
                continue

            combined = "\n\n".join(batch_texts) if batch_texts else ""
            if len(combined) > MAX_BATCH_CHARS:
                combined = combined[:MAX_BATCH_CHARS]
            before_tokens = estimate_tokens(combined)
            if before_tokens > MAX_BATCH_TOKENS:
                combined = truncate_to_tokens(combined, MAX_BATCH_TOKENS)
                logger.warning(
                    f"[WeKnoraExtract] 第{batch_idx + 1}批输入 {before_tokens} tokens "
                    f"超过预算 {MAX_BATCH_TOKENS}，已按 token 截断至 {len(combined)} 字符"
                )

            tree_summary = _build_tree_summary_for_llm(tree_nodes, subject)

            # 只有文本内容时才调 LLM
            new_nodes = []
            if combined:
                existing_hint = ""
                if tree_nodes:
                    existing_hint = (
                        f"\n## 当前已有结构树（{len(tree_nodes)} 个节点）\n{tree_summary}\n"
                        f"请继续补充缺失的节点。如果本批文档中没有新节点，new_nodes 返回空数组。"
                    )

                tools = [{
                    "type": "function", "function": {
                        "name": "extract_structure_nodes",
                        "description": "从文档中提取新的结构树节点",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "new_nodes": {"type": "array", "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"}, "name": {"type": "string"},
                                        "level": {"type": "string", "description": "系统/子系统/组件/零件"},
                                        "parentId": {"type": "string"}, "partNumber": {"type": "string"},
                                        "quantity": {"type": "integer"}, "material": {"type": "string"},
                                        "specification": {"type": "string"},
                                    }, "required": ["id", "name", "level"],
                                }},
                            }, "required": ["new_nodes"],
                        },
                    }
                }]

                user_msg = (
                    f"FMEA 主体对象：「{subject}」。根节点为「{subject}」，parentId为空。\n"
                    f"请从以下文档中提取产品结构树节点（系统→子系统→组件→零件），\n"
                    f"重要：文档中列出的每一个具体零部件（螺栓、垫片、线束、连接器、铝排、扎带、泡棉、密封圈……）都要作为独立的节点提取出来，\n"
                    f"不要只停在子系统/组件级别。确保 BOM 或表格中的每一条目都有对应的节点。\n"
                    f"【关键】：提取时尽量保留文档中的原始零部件完整名称（如\"主正铝排\"、\"BDU总成\"、\"MSD/DCDC/低压连接器安装螺栓\"），\n"
                    f"不要简化为\"铝排\"、\"BDU\"、\"螺栓\"等泛化名称。\n"
                    f"{existing_hint}\n## 本批文档\n{combined}"
                )

                batch_idx += 1
                llm = get_llm_client()
                # 推理模型（qwen3 等）的思考内容与正式输出共用 max_tokens，
                # 8192 会被思考吃光导致 content 为空、误报"LLM 调用失败"
                llm.max_tokens = STRUCT_LLM_MAX_TOKENS
                try:
                    result = await llm.safe_call(
                        system_prompt=STRUCTURE_TREE_SYSTEM,
                        user_message=user_msg, tools=tools,
                        probe_key="new_nodes", fallback_to_complete=True,
                    )
                    data = llm.extract_json(result)
                    new_nodes = data.get("new_nodes", [])
                except Exception as batch_err:
                    # 单批失败不能拖垮整轮：老逻辑下 26 批里挂 1 批就整轮失败、一个节点都存不下。
                    # 这里记录后继续处理下一批，最后在结果里汇总失败的批次号。
                    llm_failed_batches.append(batch_idx)
                    logger.error(
                        f"[WeKnoraExtract] 第{batch_idx}批 LLM 调用失败，跳过该批继续: {batch_err}",
                        exc_info=True,
                    )

            prev_count = len(tree_nodes)
            for node in new_nodes:
                nid = node.get("id", f"wn-{len(tree_nodes)+1:04d}")
                raw_name = (node.get("name", "") or "").strip()
                node_level = (node.get("level", "") or "").strip()
                if not raw_name or not node_level:
                    continue
                if _is_junk_node_name(raw_name):
                    logger.warning(f"[WeKnoraExtract] 丢弃疑似乱码节点名: {raw_name[:60]!r}")
                    continue
                node_name = raw_name.lower()
                # 用 name+level 去重（id 是 LLM 编的，不可靠）
                exists = any(
                    (n.get("name","") or "").strip().lower() == node_name
                    and (n.get("level","") or "").strip() == node_level
                    for n in tree_nodes.values()
                )
                if not exists:
                    tree_nodes[nid] = node
            added = len(tree_nodes) - prev_count

            processed_ids.update(f["id"] for f in batch_files)
            pct = min(0.15 + 0.65 * len(processed_ids) / len(queue), 0.8)

            _set_tool_progress(project_id, {
                "tool": "extract_structure_tree", "phase": "iterating",
                "message": f"第{batch_idx}批: +{added}节点, 合计{len(tree_nodes)} ({len(processed_ids)}/{len(queue)}文件)",
                "progress": pct, "nodes_count": len(tree_nodes), "updated_at": time.time(),
            })
            logger.info(f"[WeKnoraExtract] 第{batch_idx}批: +{added} → 合计{len(tree_nodes)}")

        # ── 阶段 4：最终调整（禁止新节点）──
        if tree_nodes and batch_idx > 0:
            _set_tool_progress(project_id, {
                "tool": "extract_structure_tree", "phase": "final_adjust",
                "message": f"全部文件已处理，正在进行最终调整...",
                "progress": 0.85, "nodes_count": len(tree_nodes), "updated_at": time.time(),
            })

            tree_summary = _build_tree_summary_for_llm(tree_nodes, subject)

            final_msg = (
                f"FMEA 主体对象是「{subject}」。以下是已提取的结构树（{len(tree_nodes)} 个节点）。\n\n"
                f"## 结构树\n{tree_summary}\n\n"
                f"## 你的任务\n"
                f"1. 检查层级是否合理（系统→子系统→组件→零件）\n"
                f"2. 合并重复节点（名称和层级相同）\n"
                f"3. 修正错误的 parentId\n"
                f"4. 修正错误的 level\n"
                f"5. 补充缺失的 partNumber/material/specification\n"
                f"6. 如果发现有明显遗漏的零部件（如 BOM 中列出的螺栓、线束、垫片等小件），允许在对应父节点下补充 new_nodes\n\n"
                f"以 JSON 格式输出: {{\"adjustments\": [...], \"new_nodes\": [...]}}"
            )

            try:
                llm = get_llm_client()
                llm.max_tokens = STRUCT_LLM_MAX_TOKENS
                final_result = await llm.safe_call(
                    system_prompt=STRUCTURE_TREE_SYSTEM,
                    user_message=final_msg, fallback_to_complete=True,
                )
                final_data = llm.extract_json(final_result)
                adjustments = final_data.get("adjustments", [])

                for adj in adjustments:
                    nid = adj.get("node_id", "")
                    if nid in tree_nodes:
                        action = adj.get("action", "")
                        val = adj.get("new_value", "")
                        if action == "rename":
                            tree_nodes[nid]["name"] = val
                        elif action == "fix_level":
                            tree_nodes[nid]["level"] = val
                        elif action == "change_parent":
                            tree_nodes[nid]["parentId"] = val
                        elif action == "update_material":
                            tree_nodes[nid]["material"] = val
                        elif action == "merge_duplicate":
                            tree_nodes[nid]["_merge_into"] = val

                logger.info(f"[WeKnoraExtract] 最终调整: {len(adjustments)} 项")

                # 处理最终阶段新增的节点（如有明显遗漏）
                new_nodes_final = final_data.get("new_nodes", [])
                if new_nodes_final:
                    for node in new_nodes_final:
                        nid = node.get("id", f"wn_final-{len(tree_nodes)+1:04d}")
                        raw_name = (node.get("name", "") or "").strip()
                        node_level = (node.get("level", "") or "").strip()
                        if not raw_name or not node_level:
                            continue
                        if _is_junk_node_name(raw_name):
                            logger.warning(f"[WeKnoraExtract] 丢弃疑似乱码节点名: {raw_name[:60]!r}")
                            continue
                        node_name = raw_name.lower()
                        exists = any(
                            (n.get("name","") or "").strip().lower() == node_name
                            and (n.get("level","") or "").strip() == node_level
                            for n in tree_nodes.values()
                        )
                        if not exists:
                            tree_nodes[nid] = node
                    logger.info(f"[WeKnoraExtract] 最终补充: +{len(new_nodes_final)} 个遗漏节点")
            except Exception as e:
                logger.warning(f"[WeKnoraExtract] 最终调整失败（跳过）: {e}")

        # ── 阶段 5：清理、保存 ──
        nodes_list = list(tree_nodes.values())
        from models.structure import clean_structure_nodes
        nodes_clean = clean_structure_nodes(nodes_list)

        if not nodes_clean:
            return json.dumps({"success": False, "error": "清理后结构树为空"}, ensure_ascii=False)

        elapsed = int(time.time() - start_time)

        save_data = {
            "step": "01-structure-tree", "step_name": "结构分析",
            "project_id": project_id, "subject": subject,
            "source": "weknora_progressive",
            "weknora_kb_id": weknora_kb_id,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "nodes_count": len(nodes_clean), "elapsed_seconds": elapsed,
            "files_processed": len(processed_ids), "total_files": len(all_files),
            "relevant_files": len(relevant_ids), "batches": batch_idx,
            "llm_failed_batches": llm_failed_batches,
            "nodes": nodes_clean,
            "fmea_structure_table": _build_fmea_structure_table(nodes_clean),
        }
        save_path = _save_step_result(project_id, "01-structure-tree", save_data)
        # v7: 同步写入 DB（所有后续步骤从 DB 读）
        try:
            from engine.tools.fmea_helpers import upsert_baseline
            from main import _resolve_db_project_id
            _pid = _resolve_db_project_id(project_id)
            if _pid:
                upsert_baseline(_pid, "structure", save_data)
        except Exception:
            pass

        fail_note = (f"，{len(llm_failed_batches)} 批 LLM 失败已跳过"
                     f"（批次 {llm_failed_batches}）" if llm_failed_batches else "")
        _set_tool_progress(project_id, {
            "tool": "extract_structure_tree", "phase": "done",
            "message": f"完成: {len(nodes_clean)}节点 ({batch_idx}批, {elapsed}秒){fail_note}",
            "progress": 1.0, "nodes_count": len(nodes_clean),
            "elapsed_seconds": elapsed, "updated_at": time.time(),
        })
        logger.info(f"[WeKnoraExtract] ✅ 完成: {len(nodes_clean)} 节点, {batch_idx} 批, "
                    f"{elapsed}秒{fail_note}")
        return json.dumps({
            "success": True, "project_id": project_id, "nodes_count": len(nodes_clean),
            "files_processed": len(processed_ids), "batches": batch_idx,
            "llm_failed_batches": llm_failed_batches,
            "elapsed_seconds": elapsed, "saved_to": save_path,
            "message": f"✅ 结构树提取完成：{len(nodes_clean)} 个节点（{batch_idx} 批，{elapsed} 秒）{fail_note}",
        }, ensure_ascii=False)
        return json.dumps({
            "success": True, "project_id": project_id, "nodes_count": len(nodes_clean),
            "files_processed": len(processed_ids), "elapsed_seconds": elapsed,
            "saved_to": save_path,
            "message": f"✅ 结构树提取完成：{len(nodes_clean)} 个节点（{len(processed_ids)} 个文件，{elapsed} 秒）",
        }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"[WeKnoraExtract] 失败: {e}", exc_info=True)
        _set_tool_progress(project_id, {
            "tool": "extract_structure_tree", "phase": "error",
            "message": f"提取失败: {e}", "progress": 1.0, "updated_at": time.time(),
        })
        return json.dumps({"success": False, "error": str(e), "project_id": project_id}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
# 工具 7 v4.2: BOM 优先的结构树提取（确定性表格解析 + LLM 凝结）
# ═══════════════════════════════════════════════════════════════

# BOM 表头关键词（通用，不针对具体项目）
_BOM_HEADER_KEYS = {
    "name": ("零部件名称", "零件名称", "物料名称", "中文名称", "部件名称", "名称",
             "part name", "description", "item"),
    "level": ("层级", "层次", "级别", "level"),
    "qty": ("用量", "数量", "qty", "quantity"),
    "unit": ("单位", "unit"),
    "material": ("材料", "牌号", "规格", "material", "spec"),
    "remark": ("备注", "用途", "说明", "remark", "note"),
}

# 表格页脚/非零件行特征词
_BOM_FOOTER_WORDS = ("审核", "批准", "编制", "校对", "会签", "日期", "版本记录", "修订")

_LEVEL_CODE_RE = re.compile(r"^\d+(?:\.\d+)*$")


def _looks_like_spreadsheet(fname: str) -> bool:
    fn = (fname or "").lower()
    return fn.endswith((".xlsx", ".xls", ".csv"))


def _is_junk_node_name(name: str) -> bool:
    """判断节点名是否为不可用内容（二进制乱码 / 控制字符 / 纯符号）

    背景：历史上 preview 接口被当文本读取，二进制乱码流入解析链路后
    会生成大量 `?V?\\x0c.???0&/?x$??Q??m9...` 这类"零件名"。这里做兜底过滤，
    确保即使上游再出问题，也不会把乱码写进结构树。
    """
    if not name:
        return True
    s = name.strip()
    if len(s) < 2:
        # 单字符：中文/数字可能是"门"这类短名，保留；
        # 单个 ASCII 字母多为表格标记（RASIC 表的 R/S/I、勾选列），丢弃。
        return not (s.isalnum() and not s.isascii())
    # 含替换符或控制字符（除换行/制表）→ 一定是二进制产物
    if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in s):
        return True
    if "\ufffd" in s:
        return True
    # "?" 占比过高（解码失败的典型特征）
    if s.count("?") / len(s) > 0.15:
        return True
    # 有效字符（中英文/数字）占比过低
    meaningful = sum(1 for ch in s if ch.isalnum())
    return meaningful / len(s) < 0.4


def _bom_file_score(fname: str) -> int:
    """评估一个表格文件是产品 BOM 的可能性（通用文件名关键词）"""
    fn = (fname or "").lower()
    score = 0
    for kw, pts in (("bom", 10), ("物料", 8), ("清单", 6), ("明细", 6),
                    ("零件", 4), ("部件", 4), ("pack", 3), ("电池", 2),
                    ("eol", -6), ("测试", -8), ("报告", -4), ("模板", -8),
                    ("can", -6), ("通知", -4)):
        if kw in fn:
            score += pts
    return score


def _parse_qty(val) -> float:
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return 0.0


def _parse_bom_spreadsheet(content: bytes, filename: str) -> list:
    """确定性解析 BOM 电子表格 → [{level, name, qty, unit, material, remark}]

    通用逻辑（不针对具体项目）：
    1. 扫描前 30 行，取命中表头关键词最多的行作为表头
    2. 按表头定位 名称/层级/用量/单位/材料/备注 列
    3. 无层级列表头时，按内容探测"层级码"列（多行匹配 1.2.3 形式）
    4. 抽取表头以下名称非空、非页脚的数据行
    """
    import io as _io
    try:
        import openpyxl
    except ImportError:
        logger.warning("[BOMParse] openpyxl 不可用，跳过 Excel 解析")
        return []

    try:
        wb = openpyxl.load_workbook(_io.BytesIO(content), data_only=True, read_only=True)
    except Exception as e:
        logger.warning(f"[BOMParse] 无法解析 {filename}: {e}")
        return []

    entries = []
    for ws in wb.worksheets:
        rows = []
        try:
            for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row or 1, 2000),
                                    max_col=25, values_only=True):
                def _cell(v):
                    # 清洗 NUL 与乱码替换符，防止写入 PG 时报 UntranslatableCharacter
                    s = "" if v is None else str(v).replace("\n", " ").strip()
                    return s.replace("\x00", "").replace("\ufffd", "?")
                rows.append([_cell(v) for v in row])
        except Exception:
            continue
        if len(rows) < 3:
            continue

        # ── 1. 找表头行 ──
        best_hdr, best_score = -1, 0
        for i, r in enumerate(rows[:30]):
            score = 0
            for cell in r:
                c = cell.lower()
                for keys in _BOM_HEADER_KEYS.values():
                    if any(k in c for k in keys):
                        score += 1
                        break
            if score > best_score:
                best_hdr, best_score = i, score
        if best_score < 2:
            continue  # 该 sheet 不是结构化 BOM 表

        hdr = rows[best_hdr]

        def _find_col(group):
            for idx, cell in enumerate(hdr):
                c = cell.lower()
                if any(k in c for k in _BOM_HEADER_KEYS[group]):
                    return idx
            return None

        col_name = _find_col("name")
        col_level = _find_col("level")
        col_qty = _find_col("qty")
        col_unit = _find_col("unit")
        col_material = _find_col("material")
        col_remark = _find_col("remark")

        if col_name is None:
            continue

        # ── 2. 无层级表头时，按内容探测层级码列 ──
        if col_level is None:
            col_level = -1
            best_col, best_hits = -1, 0
            for ci in range(len(hdr)):
                hits = 0
                for r in rows[best_hdr + 1: best_hdr + 40]:
                    if ci < len(r) and _LEVEL_CODE_RE.match(r[ci] or ""):
                        hits += 1
                if hits > best_hits:
                    best_col, best_hits = ci, hits
            if best_hits >= 3:
                col_level = best_col

        # ── 3. 抽取数据行 ──
        for r in rows[best_hdr + 1:]:
            name = r[col_name] if col_name < len(r) else ""
            if not name or len(name) > 120:
                continue
            if len(name) <= 12 and any(w in name for w in _BOM_FOOTER_WORDS):
                continue

            lvl = ""
            if isinstance(col_level, int) and 0 <= col_level < len(r):
                cand = (r[col_level] or "").strip()
                if _LEVEL_CODE_RE.match(cand):
                    lvl = cand

            qty = 0.0
            if isinstance(col_qty, int) and 0 <= col_qty < len(r):
                qty = _parse_qty(r[col_qty])

            def _cell(ci, limit=120):
                if isinstance(ci, int) and 0 <= ci < len(r):
                    return (r[ci] or "")[:limit]
                return ""

            entries.append({
                "level": lvl, "name": name,
                "qty": qty, "unit": _cell(col_unit, 12),
                "material": _cell(col_material), "remark": _cell(col_remark),
                "source": filename, "sheet": ws.title,
            })

    logger.info(f"[BOMParse] {filename}: {len(entries)} 行")
    return entries


def _fmt_qty(q: float) -> str:
    try:
        return str(int(q)) if float(q) == int(float(q)) else str(q)
    except (TypeError, ValueError):
        return str(q)


def _format_bom_tree(entries: list) -> str:
    """把 BOM 行格式化为带缩进的层级文本（供 LLM 阅读）"""
    lines = []
    for i, e in enumerate(entries, 1):
        depth = e["level"].count(".") if e["level"] else 0
        parts = [f"{i:>3}.", "  " * depth + e["name"]]
        if e["level"]:
            parts.append(f"[{e['level']}]")
        if e["qty"]:
            parts.append(f"x{_fmt_qty(e['qty'])}")
        if e["material"]:
            parts.append(f"材料:{e['material'][:60]}")
        if e["remark"]:
            parts.append(f"备注:{e['remark'][:40]}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _collect_supplement_chunks(client, kb_id: str, subject: str,
                               max_chunks: int = 24, max_chars: int = 9000) -> str:
    """混合搜索收集产品结构相关文档片段（服务端已解析为文本）"""
    queries = [
        f"{subject} 结构组成 零部件 总成",
        f"{subject} 模组 电芯 设计",
        f"{subject} 电池管理 控制器 硬件",
        f"{subject} 线束 连接器 布置",
    ]
    seen, chunks = set(), []
    for q in queries:
        try:
            for r in client.hybrid_search(kb_id, query_text=q, match_count=12):
                cid = r.get("id") or r.get("chunk_id") or (r.get("content") or "")[:80]
                if cid in seen:
                    continue
                seen.add(cid)
                title = r.get("knowledge_title") or r.get("knowledge_filename") or ""
                content = (r.get("content") or "").strip()
                if content:
                    chunks.append(f"[{title}] {content[:800]}")
        except Exception as e:
            logger.warning(f"[WeKnoraExtract] 补充检索失败 '{q[:16]}': {e}")
        if len(chunks) >= max_chunks:
            break
    return "\n\n".join(chunks[:max_chunks])[:max_chars]


_STRUCTURE_CONDENSE_TOOL = [{
    "type": "function",
    "function": {
        "name": "build_fmea_structure",
        "description": "输出凝结后的 FMEA 结构树（产品→总成→零部件）",
        "parameters": {
            "type": "object",
            "properties": {
                "product": {"type": "string",
                            "description": "产品根节点通用名（剥离型号/阶段限定词）"},
                "assemblies": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string",
                                     "description": "总成名称，「XX总成」风格"},
                            "owner": {"type": "string",
                                      "description": "负责人（仅当文档明确给出，否则空串）"},
                            "components": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "quantity": {"type": "number"},
                                        "material": {"type": "string"},
                                        "source": {"type": "string",
                                                   "description": "来源依据（BOM行号/文档名）"},
                                    },
                                    "required": ["name"],
                                },
                            },
                        },
                        "required": ["name", "components"],
                    },
                },
                "excluded": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "被剔除的 BOM 行及原因（非物理零部件）",
                },
            },
            "required": ["product", "assemblies"],
        },
    },
}]

_STRUCTURE_CONDENSE_PROMPT = """你是汽车行业 DFMEA 结构分析专家。下面给出从知识库解析出的产品 BOM 层级表（主证据）和相关设计文档片段（辅助证据）。

任务：把 BOM 凝结成一棵 FMEA 结构树：产品（根）→ 若干总成（中间层）→ 零部件（叶）。

【规则】
1. 根节点 product：取 BOM 顶层行的产品通用名，剥离容量/型号/驱动形式/样件阶段等限定词（含括号标注），只保留产品通称。
2. 总成层：以 BOM 顶层分组为基础，按工程专业域归纳为 3~6 个总成，命名统一为「XX总成」风格，同样剥离型号限定词。
   - 电池管理相关的主控/从控单元（BMS主控/BMS从控等）归入同一个电池管理总成（「BMS总成」风格）。
   - 高压配电/电池断路单元（BDU）不是电池管理部件，归入电气类总成，作为零部件节点出现。
   - 输出为扁平两层结构：总成下直接挂全部零部件。BOM 中的中间组合件层级（"XX组件"/"XX合件"）不单独成节点，其下属零件直接挂到对应总成下。
3. 覆盖性：每个 BOM 物理零部件行都必须归入某个总成；确实不是物理零部件的行（签字栏、工艺辅料、文件记录等）放入 excluded 并简述原因。
4. 命名规范化：
   - 剥离型号/项目后缀（"-某型号"、"（某阶段）"、容量/驱动形式后缀），保留位置/序号/方向等区分信息（如 "支架1"、"支架2" 这类序号后缀）。
   - 同一零件仅因材料牌号/批次/供应商不同拆成多行的，合并为一个节点（如双组份胶的 A/B 行合并）。
   - 同类标识小件（各种用途的标签）合并为一个节点；但铭牌（金属/硬质标识牌）与标签是不同类型，必须保留独立的铭牌节点。
   - 多行同名但备注/材料列写明不同用途的（如紧固件），用用途区分命名，格式「螺栓-<被固定件>安装」。取备注中主要（首个）被固定对象命名，并保留其功能括注（如 "固定管路（排气侧）..." → 「螺栓-排气侧管路安装」）；依据必须来自 BOM 的备注/材料列，不得编造。
   - 某行用量≥2 且文档明确其为不同位置实例的，可拆分为位置命名的节点（依据写进 source）；否则保持单节点。
   - 电池管理的主控单元通用缩写为 BMU、从控单元为 CMU；此类部件命名用「缩写（原文名）」格式保留双标识（如 "VCU（整车控制器）" 的格式）。
   - 成套外购的功能系统（如冷却/加热/热管理系统）作为零部件节点时命名为「<功能>总成」。
5. 辅助证据仅用于：命名校准、总成划分、补充 BOM 缺失但文档明确列为产品组成的物理件（含虚拟件/硬件划分项）。不得添加证据中不存在的零部件。
6. owner：仅当文档明确给出某总成负责人时填写，否则空串。

【输入】
FMEA 主体对象：{subject}

### BOM 层级表（主证据，缩进=层级）
{bom_text}

### 设计文档片段（辅助证据）
{supplement}

调用 build_fmea_structure 输出结果。"""


def _normalize_llm_structure(data: dict, subject: str) -> dict:
    """校验/清洗 LLM 凝结结果"""
    product = (data.get("product") or "").strip() or subject
    assemblies = []
    seen_asm = set()
    for a in data.get("assemblies", []) or []:
        name = (a.get("name") or "").strip()
        if not name or name.lower() in seen_asm:
            continue
        seen_asm.add(name.lower())
        comps = []
        seen_c = set()
        for c in a.get("components", []) or []:
            if isinstance(c, dict):
                cname = (c.get("name") or "").strip()
            else:
                cname = str(c).strip()
            if not cname or len(cname) > 80:
                continue
            key = cname.lower()
            if key in seen_c:
                continue
            seen_c.add(key)
            qty = c.get("quantity", 1) if isinstance(c, dict) else 1
            try:
                qty = int(float(qty))
            except (TypeError, ValueError):
                qty = 1
            comps.append({
                "name": cname, "quantity": max(qty, 1),
                "material": ((c.get("material", "") or "")[:80] if isinstance(c, dict) else ""),
            })
        if comps:
            assemblies.append({"name": name, "owner": (a.get("owner") or "").strip(),
                               "components": comps})
    return {"product": product, "assemblies": assemblies,
            "excluded": data.get("excluded", []) or []}


def _normalize_structure_names(structure: dict) -> dict:
    """后处理规范化：应用领域规则修正 LLM 凝结的命名偏差。

    通用规则（不针对具体项目）：
    1. 合并 A/B 双组份材料 → 单一节点（如 导热结构胶A + 导热结构胶B → 导热结构胶）
    2. 归并同类标签 → 统称"标签"（如 E-MARK/警示/防撕/MSD/物理标签 → 标签）
    3. 规范螺栓命名 → 统一格式「螺栓-<被固定件>安装」
    4. 去项目后缀 → 去掉型号/配置后缀（如 -四驱、-3.6kWh 等）
    5. 纯零件合并 → 同名不同材料仅保留第一个，把材料/规格信息合并
    """
    import re as _re

    # ── 规则 1: A/B 双组份合并 ──
    _ab_pattern = _re.compile(r'^(.+?)[AB]$')

    def _merge_ab(components: list) -> list:
        """合并 A/B 变体：同前缀 + 仅后缀 A/B 差异 → 去掉 A/B 后缀"""
        result = []
        seen = set()
        # 预处理：建立 A/B 组映射
        ab_groups = {}  # base_name → [(idx, comp_name)]
        for i, c in enumerate(components):
            m = _ab_pattern.match(c["name"])
            if m:
                base = m.group(1).strip()
                ab_groups.setdefault(base, []).append(i)
        merged_indices = set()
        for base, indices in ab_groups.items():
            if len(indices) >= 2:
                merged_indices.update(indices)
        for i, c in enumerate(components):
            if i in merged_indices:
                m = _ab_pattern.match(c["name"])
                if m:
                    base = m.group(1).strip()
                    key = base.lower()
                    if key not in seen:
                        seen.add(key)
                        # 用原 A 或 B 的材料（选第一个非空）
                        result.append({"name": base, "quantity": c.get("quantity", 1),
                                       "material": (c.get("material", "") or "").rstrip("AB")})
                continue
            key = c["name"].lower()
            if key not in seen:
                seen.add(key)
                result.append(c)
        return result

    # ── 规则 2: 标签归并 ──
    _label_keywords = ("标签", "名牌", "铭牌")

    def _is_label(name: str) -> bool:
        """判断是否为标签类零件"""
        nl = name.lower()
        if "铭牌" in nl:
            return True
        return any(kw in nl for kw in _label_keywords)

    def _merge_labels(components: list) -> list:
        """多种标签 → 合并为 '标签'；铭牌单独保留（不同材质/用途）"""
        labels = [c for c in components if _is_label(c["name"])]
        non_labels = [c for c in components if not _is_label(c["name"])]
        if labels:
            # 铭牌单独保留
            nameplates = [c for c in labels if "铭牌" in c["name"].lower()]
            non_nameplates = [c for c in labels if "铭牌" not in c["name"].lower()]
            # 非铭牌标签合并为一个
            if len(non_nameplates) > 1:
                label_qty = sum(c.get("quantity", 1) for c in non_nameplates)
                label_materials = list(dict.fromkeys(
                    (c.get("material", "") or "")[:30]
                    for c in non_nameplates if c.get("material")
                ))
                merged = {
                    "name": "标签",
                    "quantity": label_qty,
                    "material": "; ".join(label_materials) if label_materials else "PET 覆膜类",
                }
                return non_labels + [merged] + nameplates
            # 只有铭牌，直接用规则5处理
            label_names = set()
            deduped = []
            for c in non_labels:
                key = c["name"].lower()
                if key not in label_names:
                    label_names.add(key)
                    deduped.append(c)
            return deduped + labels
        return components

    # ── 规则 3: 螺栓命名规范 ──
    _bolt_pattern = _re.compile(r'^螺栓[-—](.+)$')

    def _normalize_bolt_name(name: str) -> str:
        """规范化螺栓名称：螺栓-固定XX → 螺栓-XX安装"""
        m = _bolt_pattern.match(name)
        if not m:
            return name
        target = m.group(1).strip()
        # "固定上盖" → "上盖安装"
        target = _re.sub(r'^固定(.+?)$', r'\1安装', target)
        # 多个固定对象用 / 连接
        target = target.replace("和", "/").replace("及", "/").replace("、", "/")
        # 去掉空格
        target = target.replace(" ", "")
        return f"螺栓-{target}"

    # ── 规则 4: 去项目前后缀 ──
    # 前缀模式（型号/容量/状态标识，如 HEV3.6 → 去掉）
    _prefix_patterns = [
        (_re.compile(r'^HEV\d*\.?\d*'), ''),    # HEV3.6 → ''
        (_re.compile(r'^\d+\.?\d*[Kk][Ww]?[Hh]?\s*'), ''),  # 3.6Kwh → ''
        (_re.compile(r'^空白'), ''),             # "空白铭牌" → "铭牌"
    ]
    # 后缀模式（配置/阶段标识）
    _suffix_patterns = [
        (_re.compile(r'[-—]四驱$'), ''),
        (_re.compile(r'[-—]两驱$'), ''),
        (_re.compile(r'[-—]\d+\.?\d*k[Ww]h\w*$'), ''),  # -3.6kWh / -3.6kWh4驱
        (_re.compile(r'[-—]C样$'), ''),
    ]
    # 中间描述词剥离（材料/属性前置描述词，剥离后取核心名词）
    _descriptor_strip = _re.compile(
        r'^(?:铝壳|钢壳|塑料|橡胶|硅胶|铜|不锈钢|碳钢|铸铁|铸铝)\s*'
    )

    def _strip_suffix(name: str) -> str:
        for pat, repl in _prefix_patterns:
            name = pat.sub(repl, name)
        name = _descriptor_strip.sub('', name)
        for pat, repl in _suffix_patterns:
            name = pat.sub(repl, name)
        return name.strip()

    # ── 规则 5: 同名零件合并 ──
    def _dedup_by_name(components: list) -> list:
        seen = {}
        for c in components:
            key = c["name"].lower()
            if key in seen:
                seen[key]["quantity"] += c.get("quantity", 1)
                prev_mat = seen[key].get("material", "")
                cur_mat = c.get("material", "")
                if cur_mat and cur_mat != prev_mat:
                    seen[key]["material"] = f"{prev_mat}; {cur_mat}" if prev_mat else cur_mat
            else:
                seen[key] = dict(c)
        return list(seen.values())

    # ── 应用所有规则 ──
    for asm in structure.get("assemblies", []) or []:
        comps = asm.get("components", [])
        if not comps:
            continue
        # 先做命名规范化
        for c in comps:
            old = c["name"]
            new = _strip_suffix(old)        # 去后缀
            new = _normalize_bolt_name(new)  # 螺栓规范
            c["name"] = new
        # 合并 A/B
        comps = _merge_ab(comps)
        # 标签归并
        comps = _merge_labels(comps)
        # 同名去重
        comps = _dedup_by_name(comps)
        asm["components"] = comps

    logger.info(f"[Normalize] 后处理完成: {len(structure.get('assemblies', []))} 总成")
    return structure


def _structure_to_raw_nodes(structure: dict) -> list:
    """把凝结结构转成 clean_structure_nodes 可用的 raw nodes"""
    nodes = []
    root_id = "n-0001"
    nodes.append({
        "id": root_id, "name": structure["product"], "level": "系统",
        "parentId": "", "partNumber": "", "quantity": 1, "material": "", "specification": "",
    })
    for ai, asm in enumerate(structure["assemblies"], 1):
        aid = f"n-1{ai:03d}"
        nodes.append({
            "id": aid, "name": asm["name"], "level": "子系统",
            "parentId": root_id, "partNumber": "", "quantity": 1,
            "material": "", "specification": "",
        })
        for ci, comp in enumerate(asm["components"], 1):
            cid = f"n-2{ai:02d}{ci:03d}"
            nodes.append({
                "id": cid, "name": comp["name"], "level": "零件",
                "parentId": aid, "partNumber": "",
                "quantity": comp.get("quantity", 1),
                "material": comp.get("material", ""), "specification": "",
            })
    return nodes


async def extract_structure_tree_from_weknora_tool(
    project_id: str, weknora_kb_id: str, subject: str
) -> str:
    """从 WeKnora 知识库提取产品结构树（v4.2：BOM 优先 + LLM 凝结）。

    策略：
    1. 在知识库中定位产品 BOM 电子表格，下载字节流并本地确定性解析
       （层级码/名称/用量/材料/备注），保证零部件全覆盖、命名忠于原文。
    2. 混合搜索补充设计文档片段作为辅助证据。
    3. 单次 LLM 调用把 BOM 凝结为「产品→总成→零部件」的 FMEA 结构树。
    4. 找不到可解析的 BOM 时，回退到渐进式全文提取（v4.1）。

    v4.2：BOM 优先 + LLM 凝结（允许重复提取，点击即重新生成覆盖）。
    """
    from knowledge.weknora_client import get_weknora_client
    from engine.llm_client import get_llm_client
    import httpx

    start_time = time.time()
    logger.info(f"[WeKnoraExtract] v4.2 BOM 优先提取: {subject}")
    _set_tool_progress(project_id, {
        "tool": "extract_structure_tree", "phase": "discovery",
        "message": "正在扫描知识库，定位产品 BOM...", "progress": 0.0,
        "nodes_count": 0, "updated_at": time.time(),
    })

    try:
        client = get_weknora_client()
        all_files = client.list_all_knowledge(weknora_kb_id)
        if not all_files:
            return json.dumps({"success": False, "error": "知识库为空"}, ensure_ascii=False)

        # ── 阶段 1：定位并解析 BOM 电子表格 ──
        candidates = []
        for f in all_files:
            title = f.get("title") or f.get("file_name") or ""
            # 标题可能不带扩展名，用 file_type 兜底判断是否为表格
            ftype = (f.get("file_type") or "").lower().lstrip(".")
            if not (_looks_like_spreadsheet(title) or ftype in ("xlsx", "xls", "csv")):
                continue
            score = _bom_file_score(title)
            if score > 0:
                candidates.append((score, f))

        candidates.sort(key=lambda x: -x[0])
        bom_sets = []
        for _score, f in candidates[:4]:
            try:
                resp = httpx.get(
                    f"{client.base_url}/knowledge/{f['id']}/preview",
                    headers={**client.headers, "Content-Type": "*/*"}, timeout=120)
                resp.raise_for_status()
                entries = _parse_bom_spreadsheet(resp.content, f.get("title", f["id"]))
                if len(entries) >= 5:
                    bom_sets.append({"file": f.get("title", f["id"]), "entries": entries})
            except Exception as e:
                logger.warning(f"[WeKnoraExtract] BOM 下载/解析失败 {f.get('title', f['id'])}: {e}")

        if not bom_sets:
            logger.warning("[WeKnoraExtract] 未找到可解析的 BOM 表格，回退到渐进式提取 (v4.1)")
            return await _extract_structure_tree_progressive_v41(
                project_id, weknora_kb_id, subject)

        # 多份 BOM（配置变体）时取行数最多的作为主证据（最完整），其余仅记录
        bom_sets.sort(key=lambda b: -len(b["entries"]))
        primary = bom_sets[0]
        logger.info(f"[WeKnoraExtract] 主 BOM: {primary['file']} ({len(primary['entries'])} 行)，"
                    f"备选 {len(bom_sets) - 1} 份")

        _set_tool_progress(project_id, {
            "tool": "extract_structure_tree", "phase": "condensing",
            "message": f"BOM 解析完成（{len(primary['entries'])} 行），LLM 凝结结构树...",
            "progress": 0.4, "nodes_count": 0, "updated_at": time.time(),
        })

        # ── 阶段 2：补充文档片段（辅助证据）──
        supplement = _collect_supplement_chunks(client, weknora_kb_id, subject)

        # ── 阶段 3：LLM 凝结 ──
        bom_text = _format_bom_tree(primary["entries"])
        prompt = _STRUCTURE_CONDENSE_PROMPT.format(
            subject=subject, bom_text=bom_text,
            supplement=supplement or "（无）",
        )
        llm = get_llm_client()
        result = await llm.safe_call(
            system_prompt="你是汽车行业 DFMEA 结构分析专家，严格按证据输出结构化结果。",
            user_message=prompt, tools=_STRUCTURE_CONDENSE_TOOL,
            probe_key="assemblies", fallback_to_complete=True,
        )
        data = llm.extract_json(result)
        structure = _normalize_llm_structure(data, subject)
        structure = _normalize_structure_names(structure)  # v4.3: 后处理规范化

        if not structure["assemblies"]:
            raise RuntimeError("LLM 凝结结果为空")

        # ── 阶段 4：构建节点 + 保存 ──
        nodes_raw = _structure_to_raw_nodes(structure)
        from models.structure import clean_structure_nodes
        nodes_clean = clean_structure_nodes(nodes_raw)
        if not nodes_clean:
            return json.dumps({"success": False, "error": "清理后结构树为空"},
                              ensure_ascii=False)

        elapsed = int(time.time() - start_time)
        total_components = sum(len(a["components"]) for a in structure["assemblies"])
        save_data = {
            "step": "01-structure-tree", "step_name": "结构分析",
            "project_id": project_id, "subject": subject,
            "source": "weknora_bom_condense_v42",
            "weknora_kb_id": weknora_kb_id,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "nodes_count": len(nodes_clean), "elapsed_seconds": elapsed,
            "bom_file": primary["file"], "bom_rows": len(primary["entries"]),
            "bom_variants": [b["file"] for b in bom_sets],
            "product": structure["product"],
            "version": "1.0",
            "date": time.strftime("%Y-%m-%d"),
            "hierarchy": {
                "level1": "上一级级别",
                "level2": "关键要素",
                "structure": {
                    a["name"]: {"owner": a.get("owner", ""),
                                "components": [c["name"] for c in a["components"]]}
                    for a in structure["assemblies"]
                },
            },
            "nodes": nodes_clean,
            "fmea_structure_table": _build_fmea_structure_table(nodes_clean),
        }
        save_path = _save_step_result(project_id, "01-structure-tree", save_data)
        # v7: 同步写入 DB（所有后续步骤从 DB 读）
        try:
            from engine.tools.fmea_helpers import upsert_baseline
            from main import _resolve_db_project_id
            _pid = _resolve_db_project_id(project_id)
            if _pid:
                ok = upsert_baseline(_pid, "structure", save_data)
                if not ok:
                    logger.error(f"[WeKnoraExtract] ❌ DB 写入失败: pid={_pid}")
                else:
                    logger.info(f"[WeKnoraExtract] ✅ DB 写入成功: pid={_pid} step=structure nodes={len(nodes_clean)}")
        except Exception as e:
            logger.error(f"[WeKnoraExtract] DB 写入异常: {e}", exc_info=True)

        _set_tool_progress(project_id, {
            "tool": "extract_structure_tree", "phase": "done",
            "message": f"完成: {len(nodes_clean)} 节点 / {len(structure['assemblies'])} 总成 ({elapsed}秒)",
            "progress": 1.0, "nodes_count": len(nodes_clean),
            "elapsed_seconds": elapsed, "updated_at": time.time(),
        })
        logger.info(f"[WeKnoraExtract] ✅ v4.2 完成: {len(nodes_clean)} 节点, "
                    f"{len(structure['assemblies'])} 总成, {total_components} 零部件, {elapsed}秒")
        # v17.12: 阶段完成邮件（给项目负责人）
        try:
            from engine.tools.notify_tools import send_stage_complete_email
            send_stage_complete_email(1, project_id)
        except Exception:
            pass
        return json.dumps({
            "success": True, "project_id": project_id,
            "message": f"结构树构建完成：{len(nodes_clean)} 节点 / {len(structure['assemblies'])} 总成",
            "nodes_count": len(nodes_clean),
            "assemblies": len(structure["assemblies"]),
            "components": total_components,
            "bom_file": primary["file"],
            "elapsed_seconds": elapsed, "saved_to": save_path,
            "message": f"✅ 结构树提取完成：{len(structure['assemblies'])} 个总成 / "
                       f"{total_components} 个零部件（BOM 驱动，{elapsed} 秒）",
        }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"[WeKnoraExtract] v4.2 失败: {e}", exc_info=True)
        _set_tool_progress(project_id, {
            "tool": "extract_structure_tree", "phase": "error",
            "message": f"提取失败: {e}", "progress": 1.0, "updated_at": time.time(),
        })
        return json.dumps({"success": False, "error": str(e), "project_id": project_id},
                          ensure_ascii=False)


def _build_tree_summary_for_llm(tree_nodes: dict, subject: str) -> str:
    """将已有结构树格式化为 LLM 可读的摘要"""
    if not tree_nodes:
        return f"（空，根节点应为「{subject}」）"
    nodes = list(tree_nodes.values())
    lines = [f"根节点: {subject}"]
    for n in nodes:
        pn = n.get("partNumber", "") or ""
        mat = n.get("material", "") or ""
        extra = f" PN={pn}" if pn else ""
        extra += f" 材料={mat}" if mat else ""
        lines.append(f"  [{n.get('level','?')}] {n['id']}: {n['name']} → parent={n.get('parentId','')}{extra}")
    return "\n".join(lines)
