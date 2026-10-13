"""
FMEA 全量 DB 操作帮助函数 — v7

所有 5 步 FMEA 数据（structure/function/failure/risk/optimization）都存储在
fmea_step_assignee_data 表中，核心设计：

   assignee_user_id = -1  → 系统基线（预合并缓存），读的时候只用这一行
   assignee_user_id = uid → 某个工程师的个人行

写入流程（防并发）：
   1. FOR UPDATE 锁系统基线行
   2. 按 item['id'] 合并工程师修改到基线
   3. 更新基线行
   4. 同时更新工程师个人行

读取流程：
   只查 assignee_user_id = -1 一行，不合并，零开销
"""
import json
import logging
from typing import Any, Dict, List, Optional

from main import get_user_db_conn

logger = logging.getLogger(__name__)


def sanitize_for_pg(obj: Any) -> Any:
    """递归清洗即将写入 PG 的 JSON 数据。

    PostgreSQL text 类型禁止 NUL 字符（\\u0000），乱码替换符 U+FFFD（\\ufffd）
    是解码失败的标志（如 Excel/PDF 里的非 UTF-8 字节）。统一在写库前清洗，
    保证任何来源（LLM 输出 / Excel 解析 / PDF 提取）的数据都能入库。
    """
    if isinstance(obj, dict):
        return {k: sanitize_for_pg(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_pg(v) for v in obj]
    if isinstance(obj, str):
        # \x00 = NUL（PG 硬性禁止）；\ufffd = 乱码替换符，换成 ? 保留可读性
        return obj.replace("\x00", "").replace("\ufffd", "?")
    return obj


def resolve_project_id_to_db_pid(project_name: str) -> Optional[int]:
    """将字符串 project_id (目录名/项目名/DB id) 解析为 projects 表的整数 ID.

    兼容两种传入方式（与 auto_optimize_failure.get_db_pid 行为一致）：
    1. project_id 是 DB 主键 id（如 "15"）→ 先按 id 精确匹配
    2. project_id 是项目名（如 "3_6KW"）→ 再按 name 匹配
    """
    if not project_name:
        return None
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                # ① 先按 id 匹配（DB 主键，数字字符串）
                try:
                    pid = int(project_name)
                    cur.execute("SELECT id FROM projects WHERE id = %s", (pid,))
                    row = cur.fetchone()
                    if row:
                        return int(row[0]) if not isinstance(row[0], int) else row[0]
                except (ValueError, TypeError):
                    pass
                # ② 再按 name 匹配（项目名/目录名）
                cur.execute("SELECT id FROM projects WHERE name = %s", (project_name,))
                row = cur.fetchone()
                if row:
                    return int(row[0]) if not isinstance(row[0], int) else row[0]
    except Exception as e:
        logger.warning(f"[DB] resolve_project_id_to_db_pid({project_name}) 失败: {e}")
    return None


# 文件名 → step_name 映射
STEP_NAME_MAP = {
    "structure":   "01-structure-tree",
    "function":    "02-function",
    "failure":     "03-failure",
    "risk":        "04-risk",
    "optimization": "05-optimization",
}
REVERSE_STEP_MAP = {v: k for k, v in STEP_NAME_MAP.items()}


# ════════════════════════════════════════════════════════════════
# 合并逻辑
# ════════════════════════════════════════════════════════════════

def merge_assignee_data(rows_data: List[dict], key: str = "id") -> dict:
    """按 assignee_user_id 升序合并多行 assignee_data。

    Args:
        rows_data: 每行 data JSONB 的内容（已 parse 为 dict），按查找顺序（系统基线先）
        key: 合并去重的键（结构树用 'id'，功能/失效/风险/优化统一用 'id'）

    Returns:
        合并后的完整 dict（以第一行的 meta 字段为基底）
    """
    if not rows_data:
        return {}

    # 用第一行作为基底（保留 hierarchy / source / meta 等非节点字段）
    base = dict(rows_data[0])

    # ── 找出所有列表字段（nodes / items / functions / failures / risks / optimizations）──
    # 按 key 做 merge：以 payload 的 key 集合为准（splice 删除必须生效）
    # 但 items/functions 字段：保留 baseline 中 payload 未涉及的项（并发安全）
    # 收集 payload 中的 keys
    payload_keys_by_field = {}  # field -> set of keys
    if len(rows_data) > 1 and isinstance(rows_data[1], dict):
        for field in ['nodes', 'items', 'functions', 'failures', 'risks', 'optimizations']:
            payload_keys_by_field[field] = set()
            for item in (rows_data[1].get(field, []) or []):
                item_key = item.get(key)
                if item_key:
                    payload_keys_by_field[field].add(item_key)

    # 用户显式删除的 node_id 集合（来自 payload._deleted_node_ids）
    # 这些 node_id 即使在 baseline 中，也要删除
    deleted_node_ids = set()
    if len(rows_data) > 1 and isinstance(rows_data[1], dict):
        deleted_node_ids = set(rows_data[1].get('_deleted_node_ids', []) or [])

    for field in ['nodes', 'items', 'functions', 'failures', 'risks', 'optimizations']:
        merged_items = {}
        payload_keys = payload_keys_by_field.get(field, set())
        # 1) baseline 中保留的（不在 deleted_node_ids 中的）
        baseline_items = rows_data[0].get(field, []) if isinstance(rows_data[0], dict) else []
        for item in baseline_items:
            item_key = item.get(key)
            # node_id 在用户显式删除列表中 → 跳过（被删除）
            # 注意：nodes（结构树）和 items（失效/风险等）都需要应用删除列表，
            # 否则画布删除的节点在 merge 时又从 baseline 复活
            if field in ('items', 'nodes') and item_key in deleted_node_ids:
                continue
            if item_key:
                merged_items[item_key] = item
        # 2) 用 payload 覆盖
        if len(rows_data) > 1 and isinstance(rows_data[1], dict):
            for item in (rows_data[1].get(field, []) or []):
                item_key = item.get(key)
                if item_key:
                    merged_items[item_key] = item
        if merged_items:
            base[field] = list(merged_items.values())

    # domain_rows：以用户显式删除的 row_index 集合为准（不是"payload 没传就是删除"）
    # 并发安全：A 提交测试并发3，B 在 t=0 加载的页面没有这条，B 提交时不应该删除它
    # 1) 收集 payload 中显式删除的 row_indexes
    deleted_row_indexes = set()
    if len(rows_data) > 1 and isinstance(rows_data[1], dict):
        for r in (rows_data[1].get('_deleted_row_indexes', []) or []):
            try:
                deleted_row_indexes.add(int(r))
            except (ValueError, TypeError):
                pass

    # 2) 收集 payload 中所有 row 的 keys
    payload_dr = []
    if len(rows_data) > 1 and isinstance(rows_data[1], dict):
        payload_dr = rows_data[1].get('domain_rows', []) or []
    payload_keys = set()
    for r in payload_dr:
        ri = r.get('row_index')
        if ri is not None and ri != '':
            try:
                payload_keys.add(f"idx:{int(ri)}")
            except (ValueError, TypeError):
                payload_keys.add(f"idx:{ri}")
        else:
            payload_keys.add(f"{r.get('parent_node', '')}|{r.get('middle_node', '')}|{r.get('child_node', '')}")

    # 3) 收集 baseline 中的行：保留（不在显式删除列表的）
    merged_domain = {}
    baseline_dr = rows_data[0].get('domain_rows', []) if isinstance(rows_data[0], dict) else []
    for r in baseline_dr:
        ri = r.get('row_index')
        # 显式删除的 row_index → 跳过
        if ri is not None and ri != '':
            try:
                if int(ri) in deleted_row_indexes:
                    continue  # 用户显式删除
            except (ValueError, TypeError):
                pass
            row_key = f"idx:{int(ri) if isinstance(ri, (int, float)) else ri}"
        else:
            row_key = f"{r.get('parent_node', '')}|{r.get('middle_node', '')}|{r.get('child_node', '')}"
        merged_domain[row_key] = r  # baseline 版本先占位，payload 会覆盖

    # 4) 用 payload 覆盖/新增
    for r in payload_dr:
        ri = r.get('row_index')
        if ri is not None and ri != '':
            try:
                row_key = f"idx:{int(ri)}"
            except (ValueError, TypeError):
                row_key = f"idx:{ri}"
        else:
            row_key = f"{r.get('parent_node', '')}|{r.get('middle_node', '')}|{r.get('child_node', '')}"
        merged_domain[row_key] = r

    if merged_domain:
        base['domain_rows'] = list(merged_domain.values())

    return base


def _split_by_assignee(data: dict, assignee_field: str = "assignee_user_id") -> Dict[int, list]:
    """将全量数据按 assignee_user_id 拆分为多份。

    Args:
        data: 完整的数据 dict（含 nodes / items 等列表）
        assignee_field: 列表元素中标识工程师 ID 的字段名

    Returns:
        {assignee_user_id: [items_of_this_user, ...]}
    """
    rows: Dict[int, list] = {}

    # 找出所有列表字段
    # domain_rows 视为共享数据（不含 assignee_user_id），跳过拆分
    for field in ['nodes', 'items', 'functions', 'failures', 'risks', 'optimizations']:
        items = data.get(field, [])
        if not isinstance(items, list):
            continue
        for item in items:
            uid = item.get(assignee_field, -1)
            if uid is None:
                uid = -1
            uid = int(uid)
            if uid not in rows:
                rows[uid] = []
            rows[uid].append(item)

    return rows


# ════════════════════════════════════════════════════════════════
# 核心读写函数
# ════════════════════════════════════════════════════════════════

def save_assignee_data(
    project_id: int,
    step_name: str,
    assignee_user_id: int,
    new_data: dict,
    item_key: str = "id",
    filter_own: bool = False,
    force_split: bool = False,
    skip_engineer_rows: bool = False,
) -> bool:
    """工程师保存自己的指派数据。

    流程：
    1. FOR UPDATE 锁系统基线行（assignee=-1）
    2. 按 item_key 合并工程师的修改到基线行（只合并属于该工程师的 item，避免覆盖他人修改）
    3. 更新基线行（所有人读到最新全量）
    4. 更新/插入工程师个人行（force_split=True 时即使是 uid=-1 也会执行拆分）

    Args:
        project_id: projects.id
        step_name: structure/function/failure/risk/optimization
        assignee_user_id: 工程师 users.id（传 -1 时仅更新基线）
        new_data: 工程师提交的新数据
        item_key: 合并去重的键，默认 'id'
        filter_own: True=只合并 assignee_user_id 匹配的 item（防并发覆盖他人修改）
        force_split: True=即使 uid=-1 也执行拆分（用于 AI 首次生成数据后按工程师拆分）
    """
    if assignee_user_id == -1 and not force_split:
        return upsert_baseline(project_id, step_name, new_data)

    # 从 new_data 中筛选出属于当前用户的 item
    def _filter_own_items(data: dict, uid: int) -> dict:
        """只保留 data 中 assignee_user_id 等于 uid 的 item，其他列字段不动"""
        filtered = dict(data)
        for field in ['nodes', 'items', 'functions', 'failures', 'risks', 'optimizations']:
            items = data.get(field, [])
            if not isinstance(items, list):
                continue
            if filter_own:
                filtered[field] = [it for it in items if int(it.get('assignee_user_id', -1)) == uid]
            else:
                filtered[field] = list(items)  # 不做筛选
        return filtered

    try:
        # 写库前统一清洗（去 \x00 / \ufffd），防止 PG 拒绝非法字符
        new_data = sanitize_for_pg(new_data)
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                # 1. 锁基线行
                cur.execute(
                    "SELECT data FROM fmea_step_assignee_data "
                    "WHERE project_id = %s AND step_name = %s AND assignee_user_id = -1 "
                    "FOR UPDATE",
                    (project_id, step_name),
                )
                baseline_row = cur.fetchone()

                if baseline_row:
                    baseline_data = baseline_row[0] if isinstance(baseline_row, tuple) else baseline_row.get("data")
                    if isinstance(baseline_data, str):
                        baseline_data = json.loads(baseline_data)
                    # 2. 合并（只合并属于当前用户的 item，避免覆盖他人并发修改）
                    filtered_data = _filter_own_items(new_data, assignee_user_id)
                    # v9: 合并去重 key 修复
                    #   - function step 顶层结构是 FunctionAnalysisItem，按 node_id 去重
                    #   - failure/risk/optimization 顶层是 FailureMode/RiskAssessment/OptimizationAction，按 id 去重
                    #   - 旧实现把所有 step 都用 node_id → 450 条失效链被合并成 60 条（按节点数）
                    if step_name == 'function':
                        effective_key = 'node_id'
                    elif step_name in ('failure', 'risk', 'optimization'):
                        effective_key = 'id'
                    else:
                        effective_key = item_key
                    merged = merge_assignee_data([baseline_data, filtered_data], key=effective_key)
                else:
                    merged = new_data
                # 防历史基线里残留的脏字符被合并进来
                merged = sanitize_for_pg(merged)

                # 3. 更新基线行（UPSERT）
                cur.execute(
                    "INSERT INTO fmea_step_assignee_data (project_id, step_name, assignee_user_id, data) "
                    "VALUES (%s, %s, -1, %s) "
                    "ON CONFLICT (project_id, step_name, assignee_user_id) "
                    "DO UPDATE SET data = %s, version = fmea_step_assignee_data.version + 1, updated_at = NOW()",
                    (project_id, step_name, json.dumps(merged), json.dumps(merged)),
                )

                # 4. 更新工程师个人行
                if skip_engineer_rows:
                    # （共享数据模式，如 function/failure/risk/optimization，不创建工程师行）
                    pass
                elif filter_own:
                    # 工程师自己保存：只更新自己的行
                    cur.execute(
                        "INSERT INTO fmea_step_assignee_data (project_id, step_name, assignee_user_id, data) "
                        "VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (project_id, step_name, assignee_user_id) "
                        "DO UPDATE SET data = %s, version = fmea_step_assignee_data.version + 1, updated_at = NOW()",
                        (project_id, step_name, assignee_user_id,
                         json.dumps(new_data), json.dumps(new_data)),
                    )
                else:
                    # 管理员/负责人保存：按 assignee_user_id 拆分解合并后的数据，同步更新所有人的行
                    # 确保工程师本地看到的与基线一致（防止管理员改指派后，工程师行不同步）
                    split_rows = _split_by_assignee(merged)
                    for uid_raw, items_list in split_rows.items():
                        uid_int = int(uid_raw)
                        if uid_int <= 0:
                            continue  # -1 是系统基线，已有完整数据，跳过
                        user_data = dict(merged)
                        for field in ['nodes', 'items', 'functions', 'failures', 'risks', 'optimizations']:
                            if field in user_data:
                                user_data[field] = [it for it in (merged.get(field, []) or [])
                                                    if int(it.get('assignee_user_id', -1)) == uid_int]
                        cur.execute(
                            "INSERT INTO fmea_step_assignee_data (project_id, step_name, assignee_user_id, data) "
                            "VALUES (%s, %s, %s, %s) "
                            "ON CONFLICT (project_id, step_name, assignee_user_id) "
                            "DO UPDATE SET data = %s, version = fmea_step_assignee_data.version + 1, updated_at = NOW()",
                            (project_id, step_name, uid_int,
                             json.dumps(user_data), json.dumps(user_data)),
                        )
            conn.commit()
        logger.info(f"[DB] save_assignee_data: pid={project_id} step={step_name} uid={assignee_user_id} own_filter={filter_own}")
        return True
    except Exception as e:
        logger.exception(f"[DB] save_assignee_data 失败: {e}")
        return False


def upsert_baseline(project_id: int, step_name: str, data: dict) -> bool:
    """系统/AI 写入/更新基线行（assignee=-1）。"""
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO fmea_step_assignee_data (project_id, step_name, assignee_user_id, data) "
                    "VALUES (%s, %s, -1, %s) "
                    "ON CONFLICT (project_id, step_name, assignee_user_id) "
                    "DO UPDATE SET data = %s, version = fmea_step_assignee_data.version + 1, updated_at = NOW()",
                    (project_id, step_name,
                     json.dumps(sanitize_for_pg(data)), json.dumps(sanitize_for_pg(data))),
                )
            conn.commit()
        return True
    except Exception as e:
        logger.exception(f"[DB] upsert_baseline 失败: {e}")
        return False


def get_baseline(project_id: int, step_name: str) -> Optional[dict]:
    """读取系统基线行（预合并缓存），这是最快的读取方式。"""
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM fmea_step_assignee_data "
                    "WHERE project_id = %s AND step_name = %s AND assignee_user_id = -1",
                    (project_id, step_name),
                )
                row = cur.fetchone()
                if not row:
                    return None
                raw = row[0]
                if isinstance(raw, str):
                    return json.loads(raw)
                return raw
    except Exception as e:
        logger.exception(f"[DB] get_baseline 失败: {e}")
        return None


def get_assignee_row(project_id: int, step_name: str, assignee_user_id: int) -> Optional[dict]:
    """读取某工程师的个人行。"""
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM fmea_step_assignee_data "
                    "WHERE project_id = %s AND step_name = %s AND assignee_user_id = %s",
                    (project_id, step_name, assignee_user_id),
                )
                row = cur.fetchone()
                if not row:
                    return None
                raw = row[0]
                if isinstance(raw, str):
                    return json.loads(raw)
                return raw
    except Exception as e:
        logger.exception(f"[DB] get_assignee_row 失败: {e}")
        return None


def get_all_assignee_rows(project_id: int, step_name: str) -> List[dict]:
    """读取某步骤所有行（系统基线 + 所有工程师），返回解析后的 data 列表。"""
    rows = []
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data, assignee_user_id FROM fmea_step_assignee_data "
                    "WHERE project_id = %s AND step_name = %s "
                    "ORDER BY assignee_user_id",
                    (project_id, step_name),
                )
                for row in cur.fetchall():
                    raw = row[0] if isinstance(row, tuple) else row
                    if isinstance(raw, str):
                        rows.append(json.loads(raw))
                    else:
                        rows.append(raw)
        return rows
    except Exception as e:
        logger.exception(f"[DB] get_all_assignee_rows 失败: {e}")
        return []


# ════════════════════════════════════════════════════════════════
# 报告读写（report — 不涉及并发，单独存）
# ════════════════════════════════════════════════════════════════

def save_report(project_id: int, data: dict) -> bool:
    """保存报告。"""
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO fmea_step_data (project_id, step_name, data) "
                    "VALUES (%s, 'report', %s) "
                    "ON CONFLICT (project_id, step_name, version) "
                    "DO UPDATE SET data = %s, version = fmea_step_data.version + 1, updated_at = NOW()",
                    (project_id, json.dumps(data), json.dumps(data)),
                )
            conn.commit()
        return True
    except Exception as e:
        logger.exception(f"[DB] save_report 失败: {e}")
        return False


def get_report(project_id: int) -> Optional[dict]:
    """读取报告。v17.2: 优先 fmea_step_assignee_data（step_name='report' 基线行），旧 fmea_step_data 兜底"""
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM fmea_step_assignee_data "
                    "WHERE project_id = %s AND step_name = 'report' AND assignee_user_id = -1 "
                    "ORDER BY version DESC LIMIT 1",
                    (project_id,),
                )
                row = cur.fetchone()
                if row:
                    raw = row[0]
                    if isinstance(raw, str):
                        return json.loads(raw)
                    return raw
        # ── 旧表兜底 ──
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM fmea_step_data "
                    "WHERE project_id = %s AND step_name = 'report' "
                    "ORDER BY version DESC LIMIT 1",
                    (project_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                raw = row[0]
                if isinstance(raw, str):
                    return json.loads(raw)
                return raw
    except Exception as e:
        logger.exception(f"[DB] get_report 失败: {e}")
        return None


# ════════════════════════════════════════════════════════════════
# 评分标准读写
# ════════════════════════════════════════════════════════════════

def save_risk_criteria(project_id: int, criteria: dict) -> bool:
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO fmea_risk_criteria (project_id, criteria) "
                    "VALUES (%s, %s) "
                    "ON CONFLICT ON CONSTRAINT fmea_risk_criteria_project_id_key "
                    "DO UPDATE SET criteria = %s, version = fmea_risk_criteria.version + 1, updated_at = NOW()",
                    (project_id, json.dumps(criteria), json.dumps(criteria)),
                )
            conn.commit()
        return True
    except Exception:
        # 没有唯一约束时用  UPSERT 兜底
        try:
            with get_user_db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM fmea_risk_criteria WHERE project_id = %s", (project_id,))
                    row = cur.fetchone()
                    if row:
                        cur.execute(
                            "UPDATE fmea_risk_criteria SET criteria = %s, version = version + 1, updated_at = NOW() WHERE project_id = %s",
                            (json.dumps(criteria), project_id))
                    else:
                        cur.execute(
                            "INSERT INTO fmea_risk_criteria (project_id, criteria) VALUES (%s, %s)",
                            (project_id, json.dumps(criteria)))
                conn.commit()
            return True
        except Exception as e:
            logger.exception(f"[DB] save_risk_criteria 失败: {e}")
            return False


def get_risk_criteria(project_id: int) -> Optional[dict]:
    """v21: 项目级优先, 全局 fallback.

    1) 项目级行: fmea_risk_criteria.project_id = :project_id
    2) 全局行: fmea_risk_criteria.project_id IS NULL (AIAG-VDA 2021 默认, 所有项目共用)
    """
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                # 1) 项目级
                cur.execute(
                    "SELECT criteria FROM fmea_risk_criteria WHERE project_id = %s ORDER BY version DESC LIMIT 1",
                    (project_id,),
                )
                row = cur.fetchone()
                if row:
                    return json.loads(row[0]) if isinstance(row[0], str) else row[0]
                # 2) 全局 fallback (AIAG-VDA 标准评分)
                cur.execute(
                    "SELECT criteria FROM fmea_risk_criteria WHERE project_id IS NULL ORDER BY version DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    return json.loads(row[0]) if isinstance(row[0], str) else row[0]
                return None
    except Exception as e:
        logger.exception(f"[DB] get_risk_criteria 失败: {e}")
        return None


# ════════════════════════════════════════════════════════════════
# 评分文件读写
# ════════════════════════════════════════════════════════════════

def list_risk_files(project_id: int) -> list:
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT filename, file_type, uploaded_at FROM project_risk_files "
                    "WHERE project_id = %s ORDER BY filename",
                    (project_id,),
                )
                rows = cur.fetchall()
                return [
                    {"filename": r[0], "file_type": r[1], "uploaded_at": str(r[2])}
                    for r in rows
                ]
    except Exception as e:
        logger.exception(f"[DB] list_risk_files 失败: {e}")
        return []


def get_risk_file_content(project_id: int, filename: str) -> Optional[str]:
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT content, file_type FROM project_risk_files "
                    "WHERE project_id = %s AND filename = %s",
                    (project_id, filename),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        logger.exception(f"[DB] get_risk_file_content 失败: {e}")
        return None


def save_risk_file(project_id: int, filename: str, content: str, file_type: str = "markdown") -> bool:
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO project_risk_files (project_id, filename, content, file_type) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (project_id, filename) "
                    "DO UPDATE SET content = %s, file_type = %s, uploaded_at = NOW()",
                    (project_id, filename, content, file_type, content, file_type),
                )
            conn.commit()
        return True
    except Exception as e:
        logger.exception(f"[DB] save_risk_file 失败: {e}")
        return False


def delete_risk_file(project_id: int, filename: str) -> bool:
    try:
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM project_risk_files WHERE project_id = %s AND filename = %s",
                    (project_id, filename),
                )
            conn.commit()
        return True
    except Exception as e:
        logger.exception(f"[DB] delete_risk_file 失败: {e}")
        return False
