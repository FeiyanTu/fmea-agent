"""
优化措施工具 — 注册为 Agent tools

架构（与 risk_tools 一致）：
┌─────────────────────────────────────────────┐
│ ① 按 RPN 排序，取前 N 条高风险项             │
├─────────────────────────────────────────────┤
│ ② LLM 并发提取（8并发，每批4条）              │
│   使用 ConcurrentFmeaEngine.process_batch    │
│   自动指数退避重试（2次）                     │
├─────────────────────────────────────────────┤
│ ③ 确定性分配：责任人从项目成员中随机分配        │
│   从 00-planning.json 读取 team_members      │
├─────────────────────────────────────────────┤
│ ④ 保存 05-optimization.json                 │
└─────────────────────────────────────────────┘
"""
import json
import logging
import random
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# LLM 提取层（单条/小批处理，供并发引擎调度）
# ═══════════════════════════════════════════════════════════════

_OPTIMIZATION_SYSTEM_PROMPT = """你是 DFMEA 优化分析专家。请为高风险失效项提出优化措施建议。

对每个需要优化的风险项输出：
- failureId: 对应的失效链ID（必须与输入一致）
- preventiveAction: DFMEA预防措施，如何预防失效原因(FC)的发生
  例如："增加冗余设计"、"改用高强度材料"、"优化设计参数"
- detectiveAction: DFMEA探测措施，如何更好地探测失效模式(FM)或失效原因(FC)
  例如："增加在线监测传感器"、"增加耐久测试"、"增加设计仿真验证"
- category: 优化方案分类，必须是 "short_term"（短期可落地设计变更，DV阶段内）或 "next_gen"（下一代车型迭代优化）
- cost_impact: 改造成本影响，必须是 "high"、"medium" 或 "low"
- maintenance_cost: 长期设计维护成本，必须是 "high"、"medium" 或 "low"
- design_change_type: 设计变更类型，必须是以下之一：
  "hardware_revision"（硬件改版）、"software_logic"（软件控制逻辑调整）、
  "structure_material"（结构尺寸/材料优化）、"cell_upgrade"（电芯规格选型升级）、
  "sor_correction"（SOR需求指标修正）
- targetDate: 目标完成日期，格式 YYYY-MM-DD。必须结合"当前日期"（在输入中给出）与该失效项严重程度推理：
  严重度高（S≥9 或 AP=H）→ 尽早，建议当前日期后 1-2 周内；
  中等（S 7-8 或 AP=M）→ 建议 1 个月内；
  一般 → 建议 1-3 个月内。
- remark: 优化理由和预期效果

输出要求（纯 JSON）：
{
  "optimizationItems": [
    {
      "failureId": "fm-001",
      "preventiveAction": "...",
      "detectiveAction": "...",
      "category": "short_term",
      "cost_impact": "medium",
      "maintenance_cost": "medium",
      "design_change_type": "hardware_revision",
      "remark": "..."
    }
  ]
}

⚠️ 优化措施范围硬性限定（仅限设计层面变更方案，禁止输出以下类型）：
- ❌ 产线检验加强、增加来料检测频次、100%全检（制程管控，非设计措施）
- ❌ 工装夹具改善、装配工艺优化（工艺措施）
- ❌ 来料管控加严、供应商管理（供应链措施）
- ❌ 售后维保策略、客户使用提醒（售后措施）
只允许：硬件改版、软件控制逻辑调整、结构尺寸/材料优化、电芯规格选型升级、SOR 需求指标修正。

核心原则：
1. 预防措施必须直接针对失效原因(FC)，探测措施直接针对失效模式(FM)或失效原因(FC)
2. 措施要具体可执行，不要空泛的
3. 不同失效模式需要不同的措施"""


async def _optimize_single_batch(batch: list) -> list:
    """并发引擎调用的处理函数：为一批高风险项生成优化建议

    Args:
        batch: 高风险项字典列表（每个含 failure_id, failure_mode 等）

    Returns:
        [{"failureId":"...", "preventiveAction":"...", "detectiveAction":"...", "remark":"..."}, ...]
    """
    from engine.llm_client import get_llm_client
    llm = get_llm_client()

    # 构建输入文本
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")

    items_text = []
    for item in batch:
        items_text.append(
            f"失效链 #{item.get('failure_id', '')}\n"
            f"  失效模式(FM): {item.get('failure_mode', '')}\n"
            f"  严重度 S={item.get('severity', 0)}, 发生度 O={item.get('occurrence', 0)}, "
            f"探测度 D={item.get('detection', 0)}\n"
            f"  RPN={item.get('rpn', 0)}, AP={item.get('ap', '')}\n"
            f"  当前PC: {item.get('prevention_control', '')}\n"
            f"  当前DC: {item.get('detection_control', '')}\n"
        )

    user_message = (
        f"今天是 {today}。请为以下高风险失效项提出优化措施建议；"
        f"其中 targetDate 必须结合今天日期与各项严重程度（S/AP）推理合理的目标完成日期。\n\n"
        + "\n---\n".join(items_text)
    )

    # Tool calling
    tools = [{
        "type": "function",
        "function": {
            "name": "suggest_optimization",
            "description": "为高风险失效提出优化措施建议",
            "parameters": {
                "type": "object",
                "properties": {
                    "optimizationItems": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "failureId": {"type": "string"},
                                "preventiveAction": {"type": "string"},
                                "detectiveAction": {"type": "string"},
                                "category": {"type": "string", "enum": ["short_term", "next_gen"]},
                                "cost_impact": {"type": "string", "enum": ["high", "medium", "low"]},
                                "maintenance_cost": {"type": "string", "enum": ["high", "medium", "low"]},
                                "design_change_type": {"type": "string", "enum": ["hardware_revision", "software_logic", "structure_material", "cell_upgrade", "sor_correction"]},
                                "targetDate": {"type": "string", "description": "目标完成日期 YYYY-MM-DD，结合当前日期与严重程度推理"},
                                "remark": {"type": "string"},
                            },
                            "required": ["failureId", "preventiveAction", "detectiveAction"],
                        }
                    }
                },
                "required": ["optimizationItems"],
            },
        }
    }]

    for attempt in range(2):
        try:
            result = await llm.call_tool(
                system_prompt=_OPTIMIZATION_SYSTEM_PROMPT,
                user_message=user_message,
                tools=tools,
                tool_choice="auto",
            )
            if result:
                # 模型可能不走 tool calling，直接在 content 返回带解释文字的 markdown JSON
                # （DeepSeek-V4-Flash 经 ollama 兼容层不响应 tools），必须用 extract_json 容错解析
                data = llm.extract_json(result) if isinstance(result, str) else result
                items = data.get("optimizationItems", [])
                # v15.3: 过滤非法元素（LLM 可能返回 str/None/缺失 failureId 的脏数据）
                if isinstance(items, list):
                    cleaned = [it for it in items if isinstance(it, dict) and it.get("failureId")]
                    if cleaned:
                        return cleaned
                    if items:
                        logger.warning(f"[LLM-Opt] optimizationItems 均为非法元素，丢弃: {type(items[0]).__name__}")
        except Exception as e:
            logger.warning(f"[LLM-Opt] 批次失败 (attempt {attempt+1}): {e}")

    # 回退 complete
    try:
        result = await llm.call_complete(
            system_prompt=_OPTIMIZATION_SYSTEM_PROMPT,
            user_message=user_message,
            force_json=True,
        )
        if result:
            data = llm.extract_json(result) if isinstance(result, str) else result
            items = data.get("optimizationItems", [])
            # v15.3: 同样过滤非法元素
            if isinstance(items, list):
                cleaned = [it for it in items if isinstance(it, dict) and it.get("failureId")]
                if cleaned:
                    return cleaned
                if items:
                    logger.warning(f"[LLM-Opt] Complete 回退优化Items 非法，丢弃: {type(items[0]).__name__}")
    except Exception as e:
        logger.warning(f"[LLM-Opt] Complete 回退失败: {e}")

    # 全失败 → 默认措施
    return [
        {
            "failureId": item.get("failure_id", ""),
            "preventiveAction": "设计评审(FC)；FMEA预防控制(FC)",
            "detectiveAction": "功能测试(FM)；EOL测试(FM)",
            "remark": "默认优化建议，建议工程师根据实际情况优化",
        }
        for item in batch
    ]


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def _load_team_members(project_id: str, db_pid=None) -> list:
    """团队成员列表：DB 优先（projects.leader + project_members engineers），本地 00-planning.json 兜底。

    Args:
        project_id: 字符串项目ID（目录名/项目名）
        db_pid: 已解析的 projects.id（传入避免重复查询；None 时自动解析）

    Returns:
        成员 display_name 列表；全失败返回默认角色名单
    """
    if db_pid is None:
        try:
            from engine.tools.fmea_helpers import resolve_project_id_to_db_pid
            db_pid = resolve_project_id_to_db_pid(project_id)
        except Exception:
            db_pid = None

    # ── DB 优先：projects.leader + project_members(engineer) ──
    if db_pid:
        try:
            import psycopg2.extras
            from main import get_user_db_conn
            conn = get_user_db_conn()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            members = []
            cur.execute("SELECT leader_user_id FROM projects WHERE id = %s", (db_pid,))
            row = cur.fetchone()
            if row and row.get("leader_user_id"):
                cur.execute("SELECT display_name FROM users WHERE id = %s", (row["leader_user_id"],))
                r = cur.fetchone()
                if r and r.get("display_name"):
                    members.append(r["display_name"])
            cur.execute(
                "SELECT u.display_name FROM project_members pm "
                "JOIN users u ON u.id = pm.user_id "
                "WHERE pm.project_id = %s AND pm.role_code = 'engineer'",
                (db_pid,),
            )
            for r in cur.fetchall():
                if r.get("display_name") and r["display_name"] not in members:
                    members.append(r["display_name"])
            cur.close(); conn.close()
            if members:
                logger.info(f"[Optimization] 团队成员(DB): {members}")
                return members
        except Exception as e:
            logger.warning(f"[Optimization] 从 DB 读取团队成员失败，走本地兜底: {e}")

    # ── 本地兜底：00-planning.json（旧架构兼容）──
    base = Path(__file__).resolve().parent.parent.parent
    data_dir = Path(__import__("os").getenv("FMEA_DATA_DIR", str(base / "data" / "projects")))
    planning_path = data_dir / project_id / "fmea" / "00-planning.json"

    if planning_path.exists():
        try:
            with open(planning_path, "r", encoding="utf-8-sig") as f:
                planning = json.load(f)
            members = planning.get("team_members", [])
            if isinstance(members, list) and len(members) > 0:
                return members
            if isinstance(members, str) and members.strip():
                return [m.strip() for m in members.split(",") if m.strip()]
        except Exception as e:
            logger.warning(f"[Optimization] 读取本地规划文件失败: {e}")

    return ["设计工程师", "质量工程师", "工艺工程师"]


def _assign_responsible_person(team_members: list, seed: str = "") -> str:
    """从团队成员中用 seed 确定性随机分配责任人"""
    if not team_members:
        return "待分配"
    if seed:
        rng = random.Random(seed)
        return rng.choice(team_members)
    return random.choice(team_members)


def _resolve_assignee_user_id(display_name: str, db_pid=None) -> Optional[int]:
    """按 display_name 反查 users.id（用于优化措施按工程师拆分保存）。

    Args:
        display_name: 责任人姓名（responsible_person）
        db_pid: projects.id（None 时尝试解析）

    Returns:
        用户 id；找不到返回 None
    """
    if not display_name:
        return None
    if db_pid is None:
        try:
            from engine.tools.fmea_helpers import resolve_project_id_to_db_pid
            db_pid = resolve_project_id_to_db_pid(str(db_pid or ""))
        except Exception:
            db_pid = None
    try:
        import psycopg2.extras
        from main import get_user_db_conn
        conn = get_user_db_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # 先按 display_name 精确匹配（同项目工程师优先）
        uid = None
        if db_pid:
            cur.execute(
                "SELECT u.id FROM users u "
                "JOIN project_members pm ON pm.user_id = u.id "
                "WHERE pm.project_id = %s AND u.display_name = %s "
                "LIMIT 1",
                (db_pid, display_name),
            )
            r = cur.fetchone()
            if r:
                uid = int(r["id"])
        if uid is None:
            cur.execute("SELECT id FROM users WHERE display_name = %s LIMIT 1", (display_name,))
            r = cur.fetchone()
            if r:
                uid = int(r["id"])
        cur.close(); conn.close()
        return uid
    except Exception as e:
        logger.warning(f"[Optimization] 反查 assignee_user_id 失败: {e}")
        return None


def _is_high_risk(item: dict) -> bool:
    """需求 §4.6.2 触发条件: 中高 AP(H/M) + 违反强制法规项(无论 AP 等级)"""
    rpn = int(item.get("rpn", 0) or 0)
    ap = str(item.get("ap", "") or "")
    regulation_violation = bool(item.get("regulation_violation"))
    return ap in ("H", "M") or regulation_violation or rpn >= 80


def _calc_ap(s: int, o: int, d: int) -> str:
    from engine.tools import calculate_ap
    return calculate_ap(s, o, d)


# ═══════════════════════════════════════════════════════════════
# Agent Tools
# ═══════════════════════════════════════════════════════════════

async def run_optimization_tool(project_id: str = "", max_items: int = 0) -> str:
    """运行优化分析（Step 5）。为高风险项生成优化措施建议。

    工作流程：
    1. 读取 04-risk.json 获取风险分析结果
    2. 筛选高风险项（AP=H/M 或 违反强制法规，需求 §4.6.2），按 RPN 降序排列
    3. 全量处理（max_items=0），或按传入的 max_items 取前 N 条
    4. 分批次（每批4条）并发调用 LLM（8并发），生成预防/探测措施 + 目标完成日期（LLM 结合当前日期与严重度推理）
    5. 责任人 = 该功能所在工程师（assignee_name），缺失时回退随机分配
    6. 自动计算改进后 S/O/D/AP 预估
    7. 保存为 05-optimization.json（并同步写 DB）

    参数:
        project_id: 项目ID（可选，不提供则自动检测）
        max_items: 最多处理的高风险项数量（默认0=不限制，全量处理；需要分批时传入 N）

    返回:
        JSON 格式的优化分析结果
    """
    base = Path(__file__).resolve().parent.parent.parent
    data_dir = Path(__import__("os").getenv("FMEA_DATA_DIR", str(base / "data" / "projects")))

    # 自动检测项目
    if not project_id and data_dir.exists():
        candidates = sorted(data_dir.iterdir(), reverse=True)
        for d in candidates:
            if d.is_dir() and (d / "fmea" / "04-risk.json").exists():
                project_id = d.name
                break

    if not project_id:
        return "未找到已完成风险评估的项目，请先完成风险评估（Step 4）"

    # ── 解析 DB 项目 ID（v13: DB 优先）──
    db_pid = None
    try:
        from engine.tools.fmea_helpers import resolve_project_id_to_db_pid
        db_pid = resolve_project_id_to_db_pid(project_id)
    except Exception as e:
        logger.warning(f"[Optimization] 解析 DB 项目 ID 失败: {e}")

    # ── 读取风险数据（v13: DB 优先 → 本地 JSON 兜底）──
    risk_data = None
    if db_pid:
        from engine.tools.fmea_helpers import get_baseline
        risk_data = get_baseline(db_pid, "risk")
        if risk_data and risk_data.get("items"):
            logger.info(f"[Optimization] 风险数据从 DB 读取: pid={db_pid} step=risk, {len(risk_data['items'])} 条")

    if not risk_data or not risk_data.get("items"):
        risk_path = data_dir / project_id / "fmea" / "04-risk.json"
        if not risk_path.exists():
            return f"风险评估数据不存在（DB 无 baseline 且本地文件缺失）: {risk_path}"
        try:
            with open(risk_path, "r", encoding="utf-8-sig") as f:
                risk_data = json.load(f)
            logger.info(f"[Optimization] DB 无风险数据，已从本地 JSON 兜底: {risk_path}")
        except Exception as e:
            return f"读取风险评估数据失败: {e}"

    all_items = risk_data.get("items", [])
    if not all_items:
        return "风险评估数据为空"

    # ── 筛选并排序高风险项 ──
    high_risk_items = [item for item in all_items if _is_high_risk(item)]
    if not high_risk_items:
        empty_result = {"items": [], "llm_raw_output": "", "user_edited": False}
        # v18: 只写 DB，不写本地 JSON
        try:
            from engine.tools.fmea_helpers import upsert_baseline
            if db_pid:
                upsert_baseline(db_pid, "optimization", empty_result)
        except Exception:
            pass
        return json.dumps({
            "success": True,
            "status": "skipped",
            "project_id": project_id,
            "message": "⏭️ 无需优化 - 所有项目风险可接受",
            "total_items": 0,
        }, ensure_ascii=False)

    # 按 RPN 降序排列（处理字符串型数值）
    high_risk_items.sort(key=lambda x: int(x.get("rpn", 0) or 0), reverse=True)

    # 限制处理数量
    total_high = len(high_risk_items)
    effective_max = max_items if max_items > 0 else total_high
    to_process = high_risk_items[:effective_max]

    logger.info(f"[Optimization] 高风险项共 {total_high} 条，本次处理前 {len(to_process)} 条")

    # ── 分批次并发调用 LLM ──
    BATCH_SIZE = 4
    CONCURRENCY = 8

    batches = [to_process[i:i+BATCH_SIZE] for i in range(0, len(to_process), BATCH_SIZE)]
    logger.info(f"[Optimization] 分 {len(batches)} 批（每批 {BATCH_SIZE} 条），"
                f"并发 {CONCURRENCY} 个 LLM 调用")

    from engine.concurrent import ConcurrentFmeaEngine
    engine = ConcurrentFmeaEngine(concurrency=CONCURRENCY)

    # 错误处理：失败时生成默认措施
    def _on_error(item_batch: list, error: Exception) -> list:
        logger.warning(f"[Optimization] 批次处理失败，使用默认措施: {error}")
        return [
            {
                "failureId": item.get("failure_id", ""),
                "preventiveAction": "设计评审(FC)；FMEA预防控制(FC)",
                "detectiveAction": "功能测试(FM)；EOL测试(FM)",
                "remark": "默认建议（LLM调用失败）",
            }
            for item in item_batch
        ]

    batch_results = await engine.process_batch(
        items=batches,
        processor=_optimize_single_batch,
        on_error=_on_error,
    )

    # ── 扁平化结果 ──
    all_llm_items = []
    for br in batch_results:
        all_llm_items.extend(br)

    # 建立 failureId → LLM 建议的映射
    # v15.3: 双保险——过滤非 dict 元素（_optimize_single_batch 已过滤，但 process_batch 展开时可能混入异常值）
    llm_map = {}
    for item in all_llm_items:
        if isinstance(item, dict) and item.get("failureId"):
            llm_map[item["failureId"]] = item

    # ── 加载团队成员（DB 优先）──
    team_members = _load_team_members(project_id, db_pid)
    logger.info(f"[Optimization] 团队成员: {team_members}")

    # ── 组装优化措施 ──
    optimization_items = []
    for idx, risk_item in enumerate(to_process):
        fid = risk_item.get("failure_id", "")
        llm_item = llm_map.get(fid, {})

        # 责任人 = 该功能所在工程师（v8 assignee 透传），缺失时回退随机分配
        responsible = risk_item.get("assignee_name") or _assign_responsible_person(team_members, seed=fid)
        # v15: 反查 assignee_user_id（按工程师拆分保存的关键）
        assignee_uid = _resolve_assignee_user_id(responsible, db_pid)

        s = int(risk_item.get("severity", 0) or 0)
        o = int(risk_item.get("occurrence", 0) or 0)
        d = int(risk_item.get("detection", 0) or 0)
        has_pa = bool(llm_item.get("preventiveAction", ""))
        has_da = bool(llm_item.get("detectiveAction", ""))

        improved_s = s
        improved_o = max(1, o - 2) if has_pa else o
        improved_d = max(1, d - 1) if has_da else d

        optimization_items.append({
            "id": f"opt-{idx+1:04d}",
            "failure_id": fid,
            # v15: 关联字段透传（18 列表格：功能分析 + 失效分析 + 风险分析核心字段）
            "focus_element": risk_item.get("focus_element", "") or risk_item.get("middle_node", ""),
            "focus_element_function": risk_item.get("focus_element_function", "") or risk_item.get("middle_function", ""),
            "failure_effect": risk_item.get("failure_effect", ""),
            "failure_mode": risk_item.get("failure_mode", ""),
            "severity": s,
            "rpn": risk_item.get("rpn", 0),
            "ap": risk_item.get("ap", ""),
            "occurrence": o,
            "detection": d,
            # v15: 责任人 + 工程师 ID（拆分保存）
            "assignee_user_id": assignee_uid or -1,
            "assignee_name": responsible,
            # ── 12 个 DFMEA 优化字段（用户指定，必填）──
            "preventive_action": llm_item.get("preventiveAction", ""),
            "detective_action": llm_item.get("detectiveAction", ""),
            "responsible_person": responsible,
            "target_date": llm_item.get("targetDate", ""),
            "status": "待实施",
            "evidence_action": "",
            "completion_date": "",
            "improved_s": improved_s,
            "improved_o": improved_o,
            "improved_d": improved_d,
            "improved_ap": _calc_ap(improved_s, improved_o, improved_d),
            "remark": llm_item.get("remark", ""),
            # v4.2 优化分类 & 成本标注（需求 §4.6.3.3/.4；LLM 漏输出时兜底，保证字段完整）
            "category": llm_item.get("category") or "short_term",
            "cost_impact": llm_item.get("cost_impact") or "medium",
            "maintenance_cost": llm_item.get("maintenance_cost") or "medium",
            "design_change_type": llm_item.get("design_change_type") or "",
            # v4.1 链式方法论：透传
            "node_id": risk_item.get("node_id", ""),
            "node_name": risk_item.get("node_name", ""),
            "child_node_id": risk_item.get("child_node_id", ""),
            "function_id": risk_item.get("function_id", ""),
            # v12 法规绑定透传（违规项高亮依据）
            "regulation_binding": risk_item.get("regulation_binding", ""),
            "regulation_violation": bool(risk_item.get("regulation_violation")),
        })

    # ── 保存（v18: 只写 DB，不写本地 JSON）──
    output_data = {"items": optimization_items, "llm_raw_output": "", "user_edited": False}

    # ── v15: 同步写入 DB（按工程师拆分保存 + baseline 行）──
    #   force_split=True 让 uid=-1 也走拆分流程 → _split_by_assignee(merged) 按 assignee_user_id 同步所有工程师行
    #   这样打开页面就有工程师行（否则只有 baseline，前端"只看我的"看不到任何数据）
    try:
        from engine.tools.fmea_helpers import save_assignee_data
        if db_pid:
            save_assignee_data(db_pid, "optimization", -1, output_data, force_split=True)
            logger.info(f"[Optimization] DB 写入成功（按工程师拆分）: project={db_pid} step=optimization")
        else:
            logger.warning("[Optimization] db_pid 未解析，跳过 DB 写入")
    except Exception as e:
        logger.warning(f"[Optimization] DB 写入失败: {e}")

    # ── 统计 ──
    pa_count = sum(1 for item in optimization_items if item.get("preventive_action"))
    da_count = sum(1 for item in optimization_items if item.get("detective_action"))
    person_dist = {}
    for item in optimization_items:
        p = item.get("responsible_person", "未知")
        person_dist[p] = person_dist.get(p, 0) + 1

    logger.info(f"[Optimization] ✅ 完成: {len(optimization_items)} 条, "
                f"预防 {pa_count}, 探测 {da_count}, "
                f"责任人: {len(person_dist)} 人")

    # v17.12: 阶段完成邮件（给项目负责人）
    try:
        from engine.tools.notify_tools import send_stage_complete_email
        send_stage_complete_email(5, project_id)
    except Exception:
        pass

    return json.dumps({
        "success": True,
        "status": "completed",
        "project_id": project_id,
        "summary": {
            "total": len(optimization_items),
            "total_high_risk": total_high,
            "processed": len(to_process),
            "with_preventive_action": pa_count,
            "with_detective_action": da_count,
            "responsible_distribution": person_dist,
        },
        "saved_to": "db",
        "message": (
            f"✅ 优化分析完成（处理前 {len(to_process)}/{total_high} 条高风险项）\n"
            f"   - 🛡 预防措施: {pa_count} 条\n"
            f"   - 🔍 探测措施: {da_count} 条\n"
            f"   - 👤 责任人: {'/'.join([f'{p}({c})' for p, c in person_dist.items()])}\n"
            f"   - 剩余 {total_high - len(to_process)} 条高风险项未处理"
        ),
    }, ensure_ascii=False)


def get_optimization_status_tool(project_id: str) -> str:
    """获取项目优化分析状态（DB 优先 → 本地 JSON 兜底）。"""
    base = Path(__file__).resolve().parent.parent.parent
    data_dir = Path(__import__("os").getenv("FMEA_DATA_DIR", str(base / "data" / "projects")))

    data = None
    source = ""
    try:
        from engine.tools.fmea_helpers import resolve_project_id_to_db_pid, get_baseline
        db_pid = resolve_project_id_to_db_pid(project_id)
        if db_pid:
            data = get_baseline(db_pid, "optimization")
            if data and data.get("items"):
                source = "db"
    except Exception:
        pass

    if data is None or not data.get("items"):
        opt_path = data_dir / project_id / "fmea" / "05-optimization.json"
        if opt_path.exists():
            try:
                with open(opt_path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                source = "local"
            except Exception:
                data = None

    info = {"project_id": project_id, "optimization_exists": bool(data and data.get("items")),
            "data_source": source}

    if data and data.get("items"):
        items = data.get("items", [])
        info["total_items"] = len(items)
        info["pending_count"] = sum(1 for r in items if r.get("status") == "待实施")
        info["in_progress_count"] = sum(1 for r in items if r.get("status") == "实施中")
        info["completed_count"] = sum(1 for r in items if r.get("status") == "已完成")

    return json.dumps(info, ensure_ascii=False)
