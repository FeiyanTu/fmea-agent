"""
最终报告生成工具 — 注册为 Agent tools

读取 Step 1-5 的数据，按 DFMEA 方法论组装最终报告：
- 结构分析 (01-structure-tree.json)
- 功能分析 (02-function.json)
- 失效分析 (03-failure.json)
- 风险分析 (04-risk.json)
- 优化措施 (05-optimization.json)

输出：
- 06-report.json（前端渲染用）
- .xlsx 文件（导出用，AIAG/VDA 标准格式）
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Agent Tools
# ═══════════════════════════════════════════════════════════════

def generate_report_tool(project_id: str = "") -> str:
    """生成 DFMEA 最终报告（Step 6）。

    读取 Step 1-5 的所有中间数据，按 DFMEA 方法论组装为完整报告，
    同时生成 JSON（前端渲染）和 xlsx（导出）两个输出文件。

    参数:
        project_id: 项目ID（可选，不提供则自动检测）

    返回:
        JSON 格式的报告生成结果
    """
    base = Path(__file__).resolve().parent.parent.parent
    data_dir = Path(__import__("os").getenv("FMEA_DATA_DIR", str(base / "data" / "projects")))

    if not project_id and data_dir.exists():
        candidates = sorted(data_dir.iterdir(), reverse=True)
        for d in candidates:
            if d.is_dir() and (d / "fmea" / "03-failure.json").exists():
                project_id = d.name
                break

    if not project_id:
        return "未找到项目数据"

    fmea_dir = data_dir / project_id / "fmea"

    # ── v16: DB 优先（fmea_step_assignee_data baseline 行）→ 本地 JSON 兜底 ──
    db_pid = None
    try:
        from engine.tools.fmea_helpers import resolve_project_id_to_db_pid, get_baseline
        db_pid = resolve_project_id_to_db_pid(project_id)
    except Exception:
        db_pid = None

    def _load_json(filename: str) -> dict:
        path = fmea_dir / filename
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[Report] 读取 {filename} 失败: {e}")
        return {}

    _STEP_NO = {"structure": 1, "function": 2, "failure": 3, "risk": 4, "optimization": 5}

    def _load_step(step_name: str) -> dict:
        """DB 优先读取步骤数据（baseline 行）"""
        if db_pid:
            try:
                data = get_baseline(db_pid, step_name)
                if isinstance(data, dict):
                    if data.get("items"):           # function/failure/risk/optimization
                        logger.info(f"[Report] 步骤 {step_name} 从 DB 读取: {len(data['items'])} 条")
                        return data
                    if data.get("nodes"):           # v17.5 fix: structure baseline 是 {nodes:[...],...} 非 {items:[...]}
                        logger.info(f"[Report] 步骤 {step_name} 从 DB 读取: {len(data['nodes'])} 节点")
                        return data
                if isinstance(data, list) and step_name == "structure":
                    return {"nodes": data}
            except Exception as e:
                logger.warning(f"[Report] DB 读取 {step_name} 失败，走本地 JSON: {e}")
        no = _STEP_NO.get(step_name, 0)
        return _load_json(f"{no:02d}-{step_name}.json")

    planning = _load_json("00-planning.json")
    structure = _load_step("structure")
    function_data = _load_step("function")
    failure = _load_step("failure")
    risk = _load_step("risk")
    optimization = _load_step("optimization")
    risk_criteria = _load_json("risk_criteria.json")

    # ── 校验关键数据 ──
    failures = failure.get("items", [])
    risk_items = risk.get("items", [])
    opt_items = optimization.get("items", [])
    nodes = structure.get("nodes", []) if isinstance(structure, dict) else (structure or [])

    if not failures:
        return "失效分析数据为空，请先完成失效分析（Step 3）"

    # ── 建立索引 ──
    # node_id → node
    node_map = {}
    for n in nodes:
        nid = n.get("id", "")
        if nid:
            node_map[nid] = n

    # node_id → parent node
    def _get_parent(node: dict) -> dict:
        pid = node.get("parent_id", "")
        return node_map.get(pid)  # v17.5: 找不到返回 None（不再用 {} 兜底，避免后续 parent.get("name") 取空）

    # function 索引: node_id → function item
    func_items = function_data.get("items", [])
    func_by_node = {}
    for fi in func_items:
        nid = fi.get("nodeId", fi.get("node_id", ""))
        if nid:
            func_by_node[nid] = fi

    # risk 索引: failure_id → risk_item
    risk_by_fid = {}
    for ri in risk_items:
        fid = ri.get("failure_id", "")
        if fid:
            risk_by_fid[fid] = ri

    # optimization 索引: failure_id → opt_item
    opt_by_fid = {}
    for oi in opt_items:
        fid = oi.get("failure_id", "")
        if fid:
            opt_by_fid[fid] = oi

    # ── 组装报告行 ──
    report_rows = []
    for idx, fm in enumerate(failures):
        fid = fm.get("id", "")
        nid = fm.get("node_id", "")
        node = node_map.get(nid, {})
        parent = _get_parent(node)

        # 功能描述
        func_item = func_by_node.get(nid, {})
        functions = func_item.get("functions", [])
        focus_func = ""
        func_fid = fm.get("function_id", "")
        if func_fid:
            for fn in functions:
                if fn.get("function_id") == func_fid:
                    focus_func = fn.get("description", "")
                    break
        if not focus_func and functions:
            focus_func = functions[0].get("description", "")

        ri = risk_by_fid.get(fid, {})
        oi = opt_by_fid.get(fid, {})

        # 安全转数字
        def _int(v, default=0):
            try:
                return int(v) if v is not None else default
            except (ValueError, TypeError):
                return default

        s = _int(fm.get("severity"), 4)
        o = _int(ri.get("occurrence"), 5)
        d = _int(ri.get("detection"), 5)
        # RPN 优先使用 risk.json 中的原始值，确保与 Step 4 完全一致
        rpn_orig = ri.get("rpn")
        rpn = _int(rpn_orig, s * o * d) if rpn_orig is not None else s * o * d

        # v4.1 链式：从 failure 自身的 child_node_id 反查对应 child 名（每个 failure 对应一个 child）
        failure_child_id = fm.get("child_node_id", "")
        failure_child_name = ""
        if failure_child_id:
            child_node = next((c for c in nodes if c.get("id") == failure_child_id), None)
            if child_node:
                failure_child_name = child_node.get("name", "")

        # v17.5: 防御 None→空串（避免 Excel 显示字符串 "None"）
        node_name = node.get("name") or fm.get("node_name") or ""
        parent_name = parent.get("name") if parent else ""

        # v17.5: 下一较高级别兜底：child_node_id 找不到时取该节点下第一个 child 名
        if not failure_child_name:
            kids = [c.get("name") for c in nodes if c.get("parent_id") == node.get("id")]
            if kids:
                failure_child_name = kids[0]

        row = {
            "issue_number": idx + 1,
            # v17.11: 加回 DRE 列（从结构分析节点指派的工程师取）
            "dre": (ri.get("assignee_name") or fm.get("assignee_name") or node.get("assignee_name") or ""),
            # v17.5: 上一较高级别 = 父节点名（不是自身 fmea_next_higher——根节点那个字段是空的）
            "next_higher_level": parent_name or "整车",
            "focus_element": node_name,
            # v17.5: 下一较高级别 = child_node_id 反查的子节点名（找不到时再用列表兜底）
            "next_lower_level": failure_child_name,

            "higher_level_function": func_item.get("upper_level_function", func_item.get("higherLevelFunction", "")),
            "focus_element_function": focus_func,
            "lower_level_function": func_item.get("lower_level_function", func_item.get("lowerLevelFunction", "")),

            "failure_effect": fm.get("failure_effect", ""),
            "severity": s,
            "failure_mode": fm.get("failure_mode", ""),
            "failure_cause": fm.get("failure_cause", ""),

            "prevention_control": ri.get("prevention_control", ""),
            "occurrence": o,
            "detection_control": ri.get("detection_control", ""),
            "detection": d,
            "rpn": rpn,
            "ap": ri.get("ap", ""),
            "special_characteristics": ri.get("special_characteristics", ""),

            "preventive_action": oi.get("preventive_action", ""),
            "detective_action": oi.get("detective_action", ""),
            "responsible_person": oi.get("responsible_person", ""),
            "target_date": oi.get("target_date", ""),
            "status": oi.get("status", ""),
            "evidence_action": oi.get("evidence_action", ""),
            "completion_date": oi.get("completion_date", ""),
            "improved_s": _int(oi.get("improved_s"), 0),
            "improved_o": _int(oi.get("improved_o"), 0),
            "improved_d": _int(oi.get("improved_d"), 0),
            "improved_ap": oi.get("improved_ap", ""),
        }
        report_rows.append(row)

    # ── 汇总 ──
    # v17: project_name 从 DB 读（project_info），不再依赖本地 planning.json 的 name
    project_info = _load_project_info(project_id, planning, project_id)
    project_name = project_info.get("project_name") or project_id
    fmea_type = planning.get("fmea_type", "DFMEA")
    ap_h = sum(1 for r in report_rows if r["ap"] == "H")
    ap_m = sum(1 for r in report_rows if r["ap"] == "M")
    ap_l = sum(1 for r in report_rows if r["ap"] == "L")
    # 高风险 = RPN≥80 或 AP=H/M（与 Step 4/5 保持一致的定义）
    high_risk_count = sum(1 for r in report_rows if r["rpn"] >= 80 or r["ap"] in ("H", "M"))
    opt_count = sum(1 for r in opt_items if r.get("preventive_action") or r.get("detective_action"))

    # v17.5: 防御 None→空串（避免 Excel 写入字符串 "None"），递归处理嵌套 dict/list
    def _sanitize(obj):
        if obj is None:
            return ""
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        return obj

    report_data = _sanitize({
        "project_id": project_id,
        "project_name": project_name,
        "fmea_type": fmea_type,
        "project_info": project_info,  # v16: Excel 项目信息区（Row 3-6）
        "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "structure_nodes": len(nodes),
        "function_items": len(func_items),
        "failure_chains": len(failures),
        "risk_items": len(risk_items),
        "optimization_items": len(opt_items),
        "report_rows": report_rows,
        "summary": {
            "total_rows": len(report_rows),
            "high_risk": high_risk_count,
            "ap_h": ap_h,
            "ap_m": ap_m,
            "ap_l": ap_l,
            "optimization_count": opt_count,
            "has_all_data": bool(failures and risk_items),
        },
    })

    # ── v17.2: 写 DB fmea_step_assignee_data（step_name='report', assignee=-1 系统基线行）
    # v18: 只写 DB，不写本地 JSON
    try:
        from engine.tools.fmea_helpers import upsert_baseline, resolve_project_id_to_db_pid
        db_pid = resolve_project_id_to_db_pid(project_id)
        if db_pid:
            db_ok = upsert_baseline(db_pid, "report", report_data)
            logger.info(f"[Report] DB 报告已写入: db_pid={db_pid} step=report ok={db_ok}")
        else:
            logger.warning(f"[Report] db_pid 未解析（project_id={project_id}），跳过 DB 写入")
    except Exception as e:
        logger.warning(f"[Report] DB 报告写入失败: {e}")

    # ── 生成 Excel ──
    # v17.9: 禁止写本地——Excel 由 /report/excel 接口从 DB 动态生成返回（内存 BytesIO）
    xlsx_path = ""

    # v17.12: 阶段完成邮件（给项目负责人）
    try:
        from engine.tools.notify_tools import send_stage_complete_email
        send_stage_complete_email(6, project_id)
    except Exception:
        pass

    return json.dumps({
        "status": "completed",
        "project_id": project_id,
        "summary": report_data["summary"],
        "saved_to": f"db://06-report (project={project_id})",
        "message": (
            f"✅ 报告生成完成\n"
            f"   - 报告行数: {len(report_rows)} 条\n"
            f"   - 高风险项: {high_risk_count} 条 (H={ap_h}, M={ap_m})  |  低风险: {ap_l} 条\n"
            f"   - 优化措施: {opt_count} 条\n"
            f"   - 数据已写入 DB（06-report step）\n"
            f"   - Excel: 导出请调用 /api/projects/{project_id}/report/excel 接口（DB 动态生成，不落盘）"
        ),
    }, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
# Excel 生成（复用 DocumentAgent 逻辑）
# ═══════════════════════════════════════════════════════════════

def _load_project_info(project_id: str, planning: dict, project_name: str) -> dict:
    """加载项目信息（v17: 纯 DB 读取，不再走本地 planning.json 兜底），用于 Excel Row 3-6 填充。

    样本 Excel 8 字段：公司名称 / 工程地点 / 顾客名称 / 年型平台 / 项目 / 开始日期 / 修订日期 / 团队
    """
    info = {
        "company_name": "",
        "production_location": "",
        "customer_name": "",
        "platform": "",
        "project_name": project_name,
        "start_date": "",
        "revision_date": "",
        "team": "",
    }
    # ── DB 优先：projects + leader/members（公司字段 v17 已入库）──
    try:
        from main import get_user_db_conn
        import psycopg2.extras
        conn = get_user_db_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        db_pid = None
        try:
            from engine.tools.fmea_helpers import resolve_project_id_to_db_pid
            db_pid = resolve_project_id_to_db_pid(project_id)
        except Exception:
            db_pid = None
        if db_pid:
            cur.execute(
                "SELECT name, subject, part_number, product_type, created_at, updated_at, leader_user_id, "
                "company_name, production_location, customer_name "
                "FROM projects WHERE id = %s", (db_pid,))
            prow = cur.fetchone()
            if prow:
                info["project_name"] = prow.get("subject") or prow.get("name") or project_name
                info["platform"] = prow.get("product_type") or prow.get("part_number") or ""
                info["company_name"] = prow.get("company_name") or ""
                info["production_location"] = prow.get("production_location") or ""
                info["customer_name"] = prow.get("customer_name") or ""
                if prow.get("created_at"):
                    info["start_date"] = str(prow["created_at"])[:10]
                if prow.get("updated_at"):
                    info["revision_date"] = str(prow["updated_at"])[:10]
                # 团队：leader + members
                names = []
                if prow.get("leader_user_id"):
                    cur.execute("SELECT display_name FROM users WHERE id = %s", (prow["leader_user_id"],))
                    rr = cur.fetchone()
                    if rr and rr.get("display_name"):
                        names.append(rr["display_name"])
                cur.execute(
                    "SELECT u.display_name FROM project_members pm JOIN users u ON u.id = pm.user_id "
                    "WHERE pm.project_id = %s ORDER BY pm.id", (db_pid,))
                for rr in cur.fetchall():
                    if rr.get("display_name") and rr["display_name"] not in names:
                        names.append(rr["display_name"])
                if names:
                    info["team"] = "、".join(names)
        cur.close(); conn.close()
    except Exception as e:
        logger.warning(f"[Report] 项目信息 DB 读取失败: {e}")
    return info


def _generate_excel(report_data: dict, project_name: str, output_dir: Path = None, planning: dict = None, save_to=None) -> str:
    """生成 DFMEA Excel 文件（v17.4: 28 列 + 边框补齐 + 列宽加宽）

    表格结构（28 列 A-AB）：
      - Row 1     : 主标题（A1:AB1 合并）
      - Row 2     : 策划和准备(步骤一)（D2:E2）
      - Row 3-6   : 项目信息 8 字段
      - Row 7     : 分隔空行
      - Row 8     : 分组表头（编号 + 5 大组）
      - Row 9     : 字段表头（28 列）
      - Row 10+   : 数据行

    列顺序（A-AB 共 28 列）：
        A  编号
        B-D  结构分析（3 列）：上一较高级别 / 关注要素 / 下一较低级别
        E-G  功能分析（3 列）：上级功能 / 本级功能 / 下级功能
        H-K  失效分析（4 列）：失效影响 FE / S / 失效模式 FM / 失效原因 FC
        L-P  风险分析（5 列）：预防控制 PC / O / 探测控制 DC / D / DFMEA AP
        Q-AB 优化（12 列）：DFMEA预防措施 / DFMEA探测措施 / 责任人姓名 / 目标完成日期 / 状态 /
                          采取基于证据的措施 / 完成日期 / 严重度S / 频度O / 探测度D / DFMEA AP / 备注
    """
    try:
        import openpyxl
        from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
    except ImportError:
        logger.warning("[Report] openpyxl 未安装，跳过 Excel 导出")
        return ""

    rows = report_data.get("report_rows", [])
    if not rows:
        return ""

    planning = planning or {}
    pinfo = report_data.get("project_info", {}) or {}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "DFMEA"

    # ── 公共样式 ──
    # v17.7: border 加 alpha=FF（ARGB 8位 hex）防止 Excel 渲染时认为透明
    thin = Side(style="thin", color="FF000000")
    medium = Side(style="medium", color="FF000000")
    border_thin = Border(left=thin, right=thin, top=thin, bottom=thin)
    border_med = Border(left=medium, right=medium, top=medium, bottom=medium)
    align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    align_left = Alignment(horizontal="left", vertical="center", wrap_text=True, indent=1)
    align_left_top = Alignment(horizontal="left", vertical="top", wrap_text=True)

    fill_dark = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    fill_mid = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
    fill_sub = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")
    fill_grey = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")
    fill_lbl = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
    fill_h = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    fill_m = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
    fill_l = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")

    font_title = Font(name="微软雅黑", size=14, bold=True)
    font_h1 = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
    font_label = Font(name="微软雅黑", size=10, bold=True)
    font_value = Font(name="微软雅黑", size=10)
    font_big = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
    font_sub = Font(name="微软雅黑", size=9, bold=True, color="1F4E79")
    font_body = Font(name="微软雅黑", size=9)

    # ── v17.4: helper 给一段连续单元格统一边框（避免合并区域其他单元格无边框）──
    def _fill_borders(ws, row: int, start_col: int, end_col: int, border):
        for c in range(start_col, end_col + 1):
            ws.cell(row, c).border = border

    # ─── Row 1: 主标题（A1:AB1）───
    ws.merge_cells("A1:AB1")
    c = ws.cell(1, 1, "设计失效模式与后果分析 DFMEA")
    c.font = font_title
    c.alignment = align_center
    c.fill = fill_grey
    ws.row_dimensions[1].height = 32
    # v17.8: Row 1 标题不强制边框（让它"飘"在白纸上，无边框）

    # ─── Row 2: 表格 1 标题（D2:G2 合并 = 表格 1 全宽）───
    ws.merge_cells("D2:G2")
    c = ws.cell(2, 4, "策划和准备(步骤一)")
    c.font = font_h1
    c.alignment = align_center
    c.fill = fill_dark
    c.border = border_med  # 表格 1 标题外框（粗）
    ws.row_dimensions[2].height = 22

    # ─── Row 3-6: 两个独立信息表格 ───
    # v17.8: 重构——表格 1 (D-G) 和表格 2 (P-T) 各自独立边框 + 浅填充
    # v17.10: 合并区内部 cell (F/G, R/S/T) 也显式设 border=medium，否则 Excel 渲染时只显示左上角 cell 的边框
    # 其他空白区域 (A-C, H-O, U-AB) 完全白纸（不设边框、不设填充）
    info_pairs = [
        (3, "公司名称：",     pinfo.get("company_name", ""),          "P", "项目：",          pinfo.get("project_name", "")),
        (4, "工程地点：",     pinfo.get("production_location", ""),  "P", "PFMEA开始日期：", pinfo.get("start_date", "")),
        (5, "顾客名称：",     pinfo.get("customer_name", ""),         "P", "PFMEA修订日期：", pinfo.get("revision_date", "")),
        (6, "年型/平台：",   pinfo.get("platform", ""),               "P", "跨职能团队：",   pinfo.get("team", "")),
    ]
    fill_value_light = PatternFill(start_color="FFF8F8F8", end_color="FFF8F8F8", fill_type="solid")  # 表格 1/2 value 浅灰底
    for rno, llbl, lval, _rcol, rlbl, rval in info_pairs:
        # ── 表格 1 左：label D(value) + merged E:G ──
        lc = ws.cell(rno, 4, llbl)
        lc.font = Font(name="微软雅黑", size=10, bold=True)
        lc.alignment = align_left; lc.fill = fill_lbl; lc.border = border_med
        ws.merge_cells(start_row=rno, start_column=5, end_row=rno, end_column=7)  # E:G 共 3 列
        vc = ws.cell(rno, 5, lval)
        vc.font = font_value; vc.alignment = align_left; vc.fill = fill_value_light
        # v17.10: 合并区内部 cell F/G 也设 border=medium（之前的 border_med 仅在 E，F/G 没设）
        for c in range(5, 8):
            ws.cell(rno, c).border = border_med
        # ── 表格 2 右：label P(value) + merged Q:T ──
        rc = ws.cell(rno, 16, rlbl)
        rc.font = Font(name="微软雅黑", size=10, bold=True)
        rc.alignment = align_left; rc.fill = fill_lbl; rc.border = border_med
        ws.merge_cells(start_row=rno, start_column=17, end_row=rno, end_column=20)  # Q:T 共 4 列
        vr = ws.cell(rno, 17, rval)
        vr.font = font_value; vr.alignment = align_left; vr.fill = fill_value_light
        for c in range(17, 21):
            ws.cell(rno, c).border = border_med
        ws.row_dimensions[rno].height = 26

    # ─── v17.10: 表格 1/2 底部封口线（Row 7 顶部边框 medium）───
    # v17.10 关键：合并区内部 cell 的边框 Excel 渲染时不会显示，所以"底部封口"必须画在表格外
    #          ——在 Row 6 下方 Row 7（空行）的顶部画 medium 横线，仅覆盖 D-G 和 P-T 矩形
    top_medium = Side(style="medium", color="FF000000")
    for c in range(4, 8):  # D-G 表格 1 底部封口线
        cell = ws.cell(7, c)
        cell.border = Border(top=top_medium, left=cell.border.left, right=cell.border.right, bottom=cell.border.bottom)
    for c in range(16, 21):  # P-T 表格 2 底部封口线
        cell = ws.cell(7, c)
        cell.border = Border(top=top_medium, left=cell.border.left, right=cell.border.right, bottom=cell.border.bottom)
    # ─── v17.10: 表格 1 顶部封口线（Row 2 标题上方 Row 1 底部边框）───
    # Row 1 是 A1:AB1 合并主标题，A1 的 bottom border 已是 medium——Row 2 标题行的顶部自然有边框
    # 但要确保 D2:G2 范围内的"顶封口线"显示——E2 cell 的 top border medium（合并左上角）足够

    # ─── Row 8: 分组表头（v17.11: 29 列布局；结构加 DRE 列 span 4，后续列右移 1）───
    big_titles = [
        ("A8:A9",   "编号"),
        ("B8:E8",   "结构分析(步骤2)"),  # span 4（含 DRE）
        ("F8:H8",   "功能分析(步骤3)"),
        ("I8:L8",   "失效分析(步骤4)"),
        ("M8:Q8",   "风险分析(步骤5)"),
        ("R8:AC8",  "优化(步骤6)"),
    ]
    for rng, text in big_titles:
        ws.merge_cells(rng)
        c = ws[rng.split(":")[0]]
        c.value = text
        c.font = font_big
        c.alignment = align_center
        c.fill = fill_dark
        c.border = border_med
    # Row 8 未合并的 colspan 边界 cell 补边框（v17.11 改 29 列）
    for col in range(1, 30):
        if ws.cell(8, col).border != border_med:
            ws.cell(8, col).border = border_thin
        ws.cell(8, col).fill = fill_dark
    ws.row_dimensions[8].height = 30

    # ─── Row 9: 字段表头（v17.11: 29 列；结构加 DRE）───
    sub_headers = [
        "编号",
        "① 上一较高级别", "② 关注要素", "③ 下一较高级别", "DRE(工程师)",  # v17.11 加 DRE
        "1. 上一较高级别功能及要求", "2. 关键要素功能及要求", "3. 下一较低级别功能及要求",
        "1. 上一较高级别要素和/或最终用户的失效影响(FE)",
        "失效影响的严重度(S)",
        "2. 关键要素的失效模式(FM)",
        "3. 下一较低级别要素或特性的失效原因(FC)",
        "当前的对失效原因的预防措施(PC)",
        "失效原因(FC)的发生频度(O)",
        "当前的失效原因(FC)或失效模式(FM)的探测措施(DC)",
        "失效原因(FC)或失效模式(FM)的探测度(D)",
        "DFMEA AP",
        "DFMEA预防措施", "DFMEA探测措施", "责任人姓名", "目标完成日期", "状态",
        "采取基于证据的措施", "完成日期", "严重度S", "频度O", "探测度D", "DFMEA AP", "备注",
    ]
    assert len(sub_headers) == 29, f"sub_headers 数量 {len(sub_headers)} != 29 (v17.11 加 DRE)"
    # v17.4: A 列属 A8:A9 合并区，A9 是 MergedCell 不可写；"编号"标题已在 A8 设过
    for i, txt in enumerate(sub_headers, 1):
        if i == 1:
            continue
        c = ws.cell(9, i, txt)
        c.font = font_sub
        c.alignment = align_center
        c.fill = fill_sub
        c.border = border_thin
    # A9 显式补边框（合并区下沿）
    ws.cell(9, 1).border = border_thin
    ws.cell(9, 1).fill = fill_sub
    ws.row_dimensions[9].height = 60

    # ─── Row 10+: 数据行（v17.11: 29 列；结构加 DRE）───
    for ridx, r in enumerate(rows):
        excel_row = 10 + ridx
        vals = [
            r.get("issue_number", ""),                 # 1  A 编号
            r.get("next_higher_level", ""),            # 2  B 上一较高级别
            r.get("focus_element", ""),                # 3  C 关注要素
            r.get("next_lower_level", ""),             # 4  D 下一较高级别
            r.get("dre", ""),                          # 5  E DRE(工程师)
            r.get("higher_level_function", ""),        # 6  F 上级功能
            r.get("focus_element_function", ""),       # 7  G 本级功能
            r.get("lower_level_function", ""),         # 8  H 下级功能
            r.get("failure_effect", ""),               # 9  I 失效影响FE
            _int_safe(r.get("severity")),              # 10 J 严重度S
            r.get("failure_mode", ""),                 # 11 K 失效模式FM
            r.get("failure_cause", ""),                # 12 L 失效原因FC
            r.get("prevention_control", ""),           # 13 M 预防控制PC
            _int_safe(r.get("occurrence")),            # 14 N 发生度O
            r.get("detection_control", ""),            # 15 O 探测控制DC
            _int_safe(r.get("detection")),             # 16 P 探测度D
            r.get("ap", ""),                           # 17 Q DFMEA AP
            r.get("preventive_action", ""),            # 18 R DFMEA预防措施
            r.get("detective_action", ""),             # 19 S DFMEA探测措施
            r.get("responsible_person", ""),           # 20 T 责任人姓名
            r.get("target_date", ""),                  # 21 U 目标完成日期
            r.get("status", ""),                       # 22 V 状态
            r.get("evidence_action", ""),              # 23 W 采取基于证据的措施
            r.get("completion_date", ""),              # 24 X 完成日期
            _int_safe(r.get("improved_s")),            # 25 Y 严重度S(改进)
            _int_safe(r.get("improved_o")),            # 26 Z 频度O(改进)
            _int_safe(r.get("improved_d")),            # 27 AA 探测度D(改进)
            r.get("improved_ap", ""),                  # 28 AB DFMEA AP(改进)
            r.get("remark", ""),                       # 29 AC 备注
        ]
        for col, val in enumerate(vals, 1):
            cell = ws.cell(excel_row, col, val)
            cell.font = font_body
            cell.alignment = align_left_top
            cell.border = border_thin

        # v17.11: AP 列高亮（Q=17 原 AP / AB=28 改进 AP；列右移 1）
        for ap_col, ap_field in ((17, "ap"), (28, "improved_ap")):
            ap_val = r.get(ap_field, "")
            ap_cell = ws.cell(excel_row, ap_col)
            if ap_val == "H":
                ap_cell.fill = fill_h
                ap_cell.font = Font(name="微软雅黑", size=9, bold=True, color="9C0006")
                ap_cell.alignment = align_center
            elif ap_val == "M":
                ap_cell.fill = fill_m
                ap_cell.alignment = align_center
            elif ap_val == "L":
                ap_cell.fill = fill_l
                ap_cell.alignment = align_center

        # v17.11: 评分列居中（J=10 / N=14 / P=16 / Y=25 / Z=26 / AA=27）
        for cidx in (10, 14, 16, 25, 26, 27):
            ws.cell(excel_row, cidx).alignment = align_center

        ws.row_dimensions[excel_row].height = 70

    # ─── 列宽（v17.11: 29 列；DRE 列 10 字符，后列右移 1）───
    col_widths = {
        "A": 5,
        "B": 20, "C": 22, "D": 22, "E": 10,             # 结构 4（v17.11 加 DRE）
        "F": 25, "G": 28, "H": 25,                     # 功能 3
        "I": 22, "J": 7, "K": 22, "L": 22,            # 失效 4
        "M": 22, "N": 7,  "O": 22, "P": 7, "Q": 10,   # 风险 5
        "R": 22, "S": 22, "T": 12, "U": 12, "V": 8,   # 优化 5
        "W": 22, "X": 12, "Y": 7, "Z": 7, "AA": 7, "AB": 10, "AC": 18,  # 优化 7
    }
    for col, w in col_widths.items():
        ws.column_dimensions[col].width = w

    # ─── 打印设置 ───
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
    ws.page_setup.paperSize = ws.PAPERSIZE_A3
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = True
    ws.page_margins.left = 0.3
    ws.page_margins.right = 0.3
    ws.page_margins.top = 0.4
    ws.page_margins.bottom = 0.4

    # ─── 保存（v17.9: save_to 传 BytesIO 则内存返回不落盘；否则写 output_dir 文件）───
    from datetime import datetime
    date_str = datetime.now().strftime("%Y%m%d")
    safe_name = project_name.replace(" ", "_").replace("/", "_")[:30]
    if save_to is not None:
        wb.save(save_to)
        logger.info(f"[Report] Excel 已生成到内存（未写本地）")
        return ""
    # v24.3: 禁止落盘——Excel 一律内存返回（/report/excel 接口传 BytesIO）
    logger.warning("[Report] 检测到无 save_to 的落盘调用（已拦截），请通过 /api/projects/{id}/report/excel 下载")
    return ""


def _int_safe(v, default=""):
    """安全转整数，失败返回 default"""
    try:
        return int(v) if v is not None else default
    except (ValueError, TypeError):
        return default
