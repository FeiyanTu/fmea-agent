"""
工程师邮件推送工具 — 把指派给工程师的结构节点通过邮件通知。

策略:
  1. 读取项目结构树 (01-structure-tree.json)
  2. 按 assignee_user_id 聚合
  3. 为每个被指派人从 users 表查 email
  4. 复用现有 SMTP 发送通道
  5. 返回汇总 (谁/多少节点/状态)

注册为 Agent 工具, 允许管理员通过对话触发.
"""
import os
import json
import smtplib
import ssl
import logging
import psycopg2
import psycopg2.extras
from email.mime.text import MIMEText
from email.header import Header
from pathlib import Path
from typing import Dict, List, Optional, Any
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(os.getenv("FMEA_DATA_DIR", str(PROJECT_ROOT / "data" / "projects")))

# ── 数据库连接 ──
def _get_db_conn():
    host = os.getenv("URL_AGENT_DB_HOST", "localhost")
    port = int(os.getenv("URL_AGENT_DB_PORT", "5432"))
    name = os.getenv("URL_AGENT_DB_NAME", "fmea")
    user = os.getenv("URL_AGENT_DB_USER", "postgres")
    pwd = os.getenv("URL_AGENT_DB_PASSWORD", "")
    return psycopg2.connect(
        host=host, port=port, dbname=name, user=user, password=pwd,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )

# ── SMTP 智能路由 (复用 main.py 逻辑) ──
SMTP_HOST_MAP = {
    "qq.com": ("smtp.qq.com", 465, "ssl"),
    "163.com": ("smtp.163.com", 465, "ssl"),
    "126.com": ("smtp.126.com", 465, "ssl"),
    "gmail.com": ("smtp.gmail.com", 465, "ssl"),
    "outlook.com": ("smtp-mail.outlook.com", 587, "starttls"),
    "hotmail.com": ("smtp-mail.outlook.com", 587, "starttls"),
}

def _send_smtp(sender_email: str, sender_auth: str, to_email: str,
               subject: str, body: str) -> tuple:
    """发送一封邮件. 返回 (ok, error_msg)"""
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = sender_email
        msg["To"] = to_email
        msg["Subject"] = Header(subject, "utf-8")

        domain = sender_email.lower().split("@")[-1]
        host, port, mode = SMTP_HOST_MAP.get(domain, ("smtp.qq.com", 465, "ssl"))

        if mode == "ssl":
            ctx = ssl.create_default_context()
            server = smtplib.SMTP_SSL(host, port, timeout=15, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls()
        try:
            server.login(sender_email, sender_auth)
            server.sendmail(sender_email, [to_email], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ═══════════════════════════════════════════════════════════
# v17.12: 邮件通知开关（DB 控制）+ 阶段完成通知
# 每个触发点一个开关（email_notification_switch 表），默认全开
# 生效优先级：项目级 > 全局级
# ═══════════════════════════════════════════════════════════

STAGE_NAMES = {
    0: "规划准备", 1: "结构分析", 2: "功能分析",
    3: "失效分析", 4: "风险评估", 5: "优化措施", 6: "最终报告",
}

def _get_user_db():
    """复用主应用 DB 连接（notify_tools 的 _get_db_conn 默认 localhost 连不上主库）"""
    from main import get_user_db_conn
    return get_user_db_conn()

def is_email_enabled(project_id, stage_id, notification_type: str) -> bool:
    """查邮件开关：项目级优先，其次全局级（project_id NULL）。默认全开（查不到视为开启）"""
    try:
        conn = _get_user_db()
        try:
            with conn.cursor() as cur:
                # 项目级
                if project_id:
                    cur.execute(
                        "SELECT enabled FROM email_notification_switch "
                        "WHERE project_id = %s AND stage_id IS NOT DISTINCT FROM %s AND notification_type = %s",
                        (project_id, stage_id, notification_type),
                    )
                    row = cur.fetchone()
                    if row:
                        return bool(row[0] if not isinstance(row, dict) else row.get("enabled"))
                # 全局级
                cur.execute(
                    "SELECT enabled FROM email_notification_switch "
                    "WHERE project_id IS NULL AND stage_id IS NOT DISTINCT FROM %s AND notification_type = %s",
                    (stage_id, notification_type),
                )
                row = cur.fetchone()
                if row:
                    return bool(row[0] if not isinstance(row, dict) else row.get("enabled"))
                return True  # 未配置 → 默认开
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[EmailSwitch] 查询开关失败，默认开启: {e}")
        return True


def _get_admin_sender() -> Optional[dict]:
    """取管理员邮箱 + auth_code（全局统一发件人；管理员不作为任何项目负责人）"""
    try:
        conn = _get_user_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT u.email, u.email_auth_code, u.display_name FROM users u "
                    "WHERE u.role_code = 'admin' AND u.email IS NOT NULL AND u.email_auth_code IS NOT NULL "
                    "ORDER BY u.id LIMIT 1"
                )
                row = cur.fetchone()
                if not row:
                    return None
                if isinstance(row, dict):
                    return {"email": row.get("email"), "auth_code": row.get("email_auth_code"), "name": row.get("display_name")}
                return {"email": row[0], "auth_code": row[1], "name": row[2]}
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[EmailSwitch] 查管理员发件人失败: {e}")
        return None


def _get_leader_email(db_pid: int) -> Optional[str]:
    """取项目负责人邮箱（projects.leader_user_id → users.email）"""
    try:
        conn = _get_user_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT u.email FROM projects p JOIN users u ON u.id = p.leader_user_id "
                    "WHERE p.id = %s AND u.email IS NOT NULL",
                    (db_pid,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return row[0] if not isinstance(row, dict) else row.get("email")
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[EmailSwitch] 查项目负责人邮箱失败: {e}")
        return None


def _get_project_name(db_pid: int) -> str:
    try:
        conn = _get_user_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM projects WHERE id = %s", (db_pid,))
                row = cur.fetchone()
                if row:
                    return str(row[0] if not isinstance(row, dict) else row.get("name"))
        finally:
            conn.close()
    except Exception:
        pass
    return str(db_pid)


def send_stage_complete_email(stage_id: int, project_id) -> bool:
    """每步脚本跑完后调：查开关 → 管理员发件 → 负责人收件 → 发邮件"""
    try:
        db_pid = _resolve_db_project_id(str(project_id))
        if not db_pid:
            logger.warning(f"[EmailSwitch] 项目 {project_id} 无法解析 DB ID，跳过邮件")
            return False
        # 开关判断
        if not is_email_enabled(db_pid, stage_id, "stage_complete"):
            logger.info(f"[EmailSwitch] 阶段 {stage_id} 完成通知已关闭（project={db_pid}），跳过")
            return False
        # 发件人 = 管理员
        sender = _get_admin_sender()
        if not sender:
            logger.warning("[EmailSwitch] 未配置管理员邮箱/授权码，跳过邮件")
            return False
        # 收件人 = 项目负责人
        to_email = _get_leader_email(db_pid)
        if not to_email:
            logger.warning(f"[EmailSwitch] 项目 {db_pid} 负责人未配置邮箱，跳过邮件")
            return False

        project_name = _get_project_name(db_pid)
        stage_name = STAGE_NAMES.get(stage_id, f"步骤{stage_id}")
        subject = f"[FMEA] {project_name} - 步骤{stage_id}「{stage_name}」已完成"
        body = (
            f"负责人您好：\n\n"
            f"项目「{project_name}」的第 {stage_id} 步「{stage_name}」已完成，请登录系统查看。\n\n"
            f"—— FMEA Agent 自动通知\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        ok, err = _send_smtp(sender["email"], sender["auth_code"], to_email, subject, body)
        if ok:
            logger.info(f"[EmailSwitch] 阶段完成邮件已发送: {sender['email']} → {to_email} 「{subject}」")
        else:
            logger.warning(f"[EmailSwitch] 阶段完成邮件发送失败: {err}")
        return ok
    except Exception as e:
        logger.warning(f"[EmailSwitch] send_stage_complete_email 异常: {e}")
        return False


def send_stage_review_request_email(stage_id: int, project_id) -> bool:
    """v20.0: 所有工程师都已提交某阶段后, 给项目负责人发审核提醒邮件.

    触发点: 工程师调用 /api/approval/approve 提交后, 当 is_all_approved=True 时
    收件人: 项目负责人（leader）
    发件人: 管理员（admin）
    开关: email_notification_switch.notification_type='stage_review_request', stage_id=1-5
    """
    # 只在 1-5 阶段生效（结构/功能/失效/风险/优化；规划和报告不走此流程）
    if stage_id not in (1, 2, 3, 4, 5):
        return False
    try:
        db_pid = _resolve_db_project_id(str(project_id))
        if not db_pid:
            logger.warning(f"[EmailSwitch] 项目 {project_id} 无法解析 DB ID, 跳过审核提醒")
            return False
        # 开关判断
        if not is_email_enabled(db_pid, stage_id, "stage_review_request"):
            logger.info(f"[EmailSwitch] 阶段 {stage_id} 审核提醒已关闭 (project={db_pid}), 跳过")
            return False
        # 发件人 = 管理员
        sender = _get_admin_sender()
        if not sender:
            logger.warning("[EmailSwitch] 未配置管理员邮箱/授权码, 跳过审核提醒")
            return False
        # 收件人 = 项目负责人
        to_email = _get_leader_email(db_pid)
        if not to_email:
            logger.warning(f"[EmailSwitch] 项目 {db_pid} 负责人未配置邮箱, 跳过审核提醒")
            return False

        project_name = _get_project_name(db_pid)
        stage_name = STAGE_NAMES.get(stage_id, f"步骤{stage_id}")
        subject = f"[FMEA] {project_name} - 步骤{stage_id}「{stage_name}」全部工程师已提交, 请审核"
        body = (
            f"负责人您好：\n\n"
            f"项目「{project_name}」的第 {stage_id} 步「{stage_name}」所有工程师均已提交申请，"
            f"请您尽快登录系统进行审核。\n\n"
            f"—— FMEA Agent 自动通知\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        ok, err = _send_smtp(sender["email"], sender["auth_code"], to_email, subject, body)
        if ok:
            logger.info(f"[EmailSwitch] 阶段审核提醒已发送: {sender['email']} → {to_email} 「{subject}」")
        else:
            logger.warning(f"[EmailSwitch] 阶段审核提醒发送失败: {err}")
        return ok
    except Exception as e:
        logger.warning(f"[EmailSwitch] send_stage_review_request_email 异常: {e}")
        return False

# ═══════════════════════════════════════════════════════════
# 发件人凭据 — 从项目负责人的 email_auth_code 取
# ═══════════════════════════════════════════════════════════

def _resolve_db_project_id(project_id: str) -> Optional[int]:
    """将字符串 project_id (projects.id 或 projects.name) 解析为 projects 表的整数 ID."""
    try:
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # 优先按 id 查
                try:
                    pid_int = int(project_id)
                    cur.execute("SELECT id FROM projects WHERE id = %s", (pid_int,))
                    row = cur.fetchone()
                    if row:
                        # RealDictCursor 返回 dict
                        return int(row.get("id") if isinstance(row, dict) else row[0])
                except ValueError:
                    pass
                # fallback: 按 name 查
                cur.execute("SELECT id FROM projects WHERE name = %s", (project_id,))
                row = cur.fetchone()
                if row:
                    return int(row.get("id") if isinstance(row, dict) else row[0])
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[Push] 解析 DB 项目 ID 失败 (project_id={project_id}): {e}")
    return None


def _get_owner_credentials(project_id: str) -> Optional[dict]:
    """从 DB 查出项目负责人的邮箱+授权码.

    优先级: role_code='owner' → 'leader' → 'admin' 的项目成员.
    返回 {email, auth_code, display_name, role} 或 None.
    """
    db_pid = _resolve_db_project_id(project_id)
    if db_pid is None:
        logger.warning(f"[Push] 无法解析项目 {project_id} 的整数 DB ID")
        return None
    try:
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # 按优先级匹配项目负责人: owner > leader > admin
                cur.execute("""
                    SELECT u.email, u.email_auth_code, u.display_name, u.username, pm.role_code
                    FROM project_members pm
                    JOIN users u ON u.id = pm.user_id
                    WHERE pm.project_id = %s
                      AND pm.role_code IN ('owner', 'leader', 'admin')
                      AND u.email <> '' AND u.email_auth_code <> ''
                    ORDER BY CASE pm.role_code
                        WHEN 'owner' THEN 1
                        WHEN 'leader' THEN 2
                        WHEN 'admin' THEN 3
                    END
                    LIMIT 1
                """, (db_pid,))
                row = cur.fetchone()
        finally:
            conn.close()
        if row and row.get("email") and row.get("email_auth_code"):
            return {
                "email": row["email"].strip(),
                "auth_code": row["email_auth_code"].strip(),
                "display_name": (row.get("display_name") or row.get("username") or "").strip(),
                "role": row.get("role_code", ""),
            }
    except Exception as e:
        logger.error(f"[Push] 查询项目负责人失败: {e}")
    return None


def _get_fallback_credentials() -> Optional[dict]:
    """从环境变量取系统级发件人作为兜底. 返回 {email, auth_code} 或 None."""
    email = os.getenv("FMEA_NOTIFY_SENDER_EMAIL")
    auth = os.getenv("FMEA_NOTIFY_SENDER_AUTH")
    if email and auth:
        return {"email": email.strip(), "auth_code": auth.strip(), "display_name": ""}
    return None


def _load_step_data(project_id, step_name: str) -> Optional[dict]:
    """读取项目某步骤的基线数据（v7: 从 fmea_step_assignee_data 基线行读）

    Args:
        project_id: 项目 ID（整数或字符串）
        step_name: structure/function/failure/risk/optimization
    """
    db_pid = project_id
    if not isinstance(db_pid, int):
        try:
            db_pid = int(db_pid)
        except (ValueError, TypeError):
            # 字符串 project_id → 兼容整数 ID 和项目名
            try:
                conn = _get_db_conn()
                try:
                    with conn.cursor() as cur:
                        # 先按 id 查（数字字符串），fallback 按 name 查
                        try:
                            cur.execute("SELECT id FROM projects WHERE id = %s", (int(str(project_id)),))
                        except ValueError:
                            cur.execute("SELECT id FROM projects WHERE name = %s", (str(project_id),))
                        row = cur.fetchone()
                        if row:
                            db_pid = int(row["id"])
                finally:
                    conn.close()
            except Exception as e:
                logger.warning(f"[Push] 解析 project_id 失败: {e}")
                return None
    try:
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM fmea_step_assignee_data "
                    "WHERE project_id = %s AND step_name = %s AND assignee_user_id = -1",
                    (db_pid, step_name))
                row = cur.fetchone()
                if not row:
                    return None
                raw = row["data"]
                if isinstance(raw, str):
                    return json.loads(raw)
                return raw
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[Push] 读 DB {step_name} 数据失败: {e}")
        return None


# 保留向后兼容
def _load_structure_tree(project_id) -> Optional[dict]:
    return _load_step_data(project_id, "structure")


def _build_assignee_groups_from_items(items: list, structure_tree: Optional[dict] = None) -> Dict[str, List[dict]]:
    """从 items 中按 assignee_user_id 聚合，返回 {uid_str: [item, ...]}

    用于 function/failure/risk/optimization 步骤。

    支持两种数据格式：
    - 功能分析格式：items[].functions[].assignee_user_id（嵌套子列表）
    - 失效/风险/优化格式：items[].assignee_user_id（平铺字段）

    Fallback: 如果 item 没有 assignee_user_id 但 node_id 在结构树中有指派人，
    则继承结构树的指派人。
    """
    groups = defaultdict(list)

    # 建立 node_id → 结构树节点 的索引
    node_assignee_map = {}
    if structure_tree:
        for node in structure_tree.get("nodes", []):
            nid = node.get("id")
            uid = node.get("assignee_user_id")
            if nid and uid is not None and uid != "" and uid != -1:
                node_assignee_map[nid] = uid

    for item in items or []:
        item_node_id = item.get("node_id")
        item_node_name = item.get("node_name") or item_node_id

        # 优先尝试 functions[] 嵌套格式（功能分析）
        functions = item.get("functions") or []
        if functions:
            for fn in functions:
                uid = fn.get("assignee_user_id")
                if (uid is None or uid == "" or uid == -1) and item_node_id:
                    uid = node_assignee_map.get(item_node_id)
                if uid is not None and uid != "" and uid != -1:
                    groups[str(uid)].append({
                        "item_node_id": item_node_id,
                        "item_node_name": item_node_name,
                        "function": fn,
                    })
        else:
            # 平铺格式：直接从 item 读 assignee_user_id（失效/风险/优化）
            uid = item.get("assignee_user_id")
            if (uid is None or uid == "" or uid == -1) and item_node_id:
                uid = node_assignee_map.get(item_node_id)
            if uid is not None and uid != "" and uid != -1:
                groups[str(uid)].append({
                    "item_node_id": item_node_id,
                    "item_node_name": item_node_name,
                    "function": item,  # item 本身就是平铺的功能/失效条目
                })
    return groups


def _build_assignee_groups(tree: dict) -> Dict[str, List[dict]]:
    """结构树步骤专用：按 assignee_user_id 聚合节点. 返回 {user_id_str: [node, ...]}"""
    groups = defaultdict(list)
    for node in tree.get("nodes", []):
        uid = node.get("assignee_user_id")
        if uid is not None and uid != "":
            groups[str(uid)].append(node)
    return groups


def _lookup_user_emails(user_ids: List[str]) -> Dict[str, dict]:
    """从 users 表批量查询用户. 返回 {uid_str: {display_name, email, ...}}"""
    if not user_ids:
        return {}
    try:
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, display_name, username, email FROM users WHERE id = ANY(%s)",
                    ([int(uid) for uid in user_ids if uid.isdigit()],)
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return {str(r["id"]): dict(r) for r in rows}
    except Exception as e:
        logger.error(f"[Push] 查询用户邮箱失败: {e}")
        return {}


def _build_email_content(assignee: dict, nodes: List[dict], project_id: str,
                          project_name: str, base_url: str) -> tuple:
    """生成邮件主题和正文（结构树版 — v13: 精简为纯通知，不列节点明细）"""
    display = assignee.get("display_name") or assignee.get("username") or "工程师"
    subject = f"[FMEA] {project_name} — 您有 {len(nodes)} 个结构节点需要确认"

    body = f"""您好 {display}：

您被指派为 {len(nodes)} 个 FMEA 结构节点的负责人，请登录系统查看、补充信息并确认。

项目: {project_name}
项目 ID: {project_id}
待确认节点数: {len(nodes)} 个

操作步骤:
  1. 登录系统: {base_url}
  2. 进入「FMEA 智能分析工作台」
  3. 选择项目并切换到「结构分析」Tab
  4. 指派给您的节点会黄色高亮显示，双击节点编辑规格/材料等信息
  5. 完成编辑后点击「确认」按钮提交

如有疑问，请联系项目负责人。

—— FMEA Agent 系统自动通知
{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
"""
    return subject, body


# 步骤的中文名映射
STEP_DISPLAY_NAMES = {
    "structure": "结构分析",
    "function": "功能分析",
    "failure": "失效分析",
    "risk": "风险评估",
    "optimization": "优化措施",
}

# 步骤对应的 stage 编号
STEP_TO_STAGE = {
    "structure": 1,
    "function": 2,
    "failure": 3,
    "risk": 4,
    "optimization": 5,
}


async def _push_to_engineers_internal(
    project_id: str, project_name: str = "", base_url: Optional[str] = None,
    step_name: str = "structure",
) -> dict:
    """核心: 实际推送, 返回结构化数据 (供 API 和 Agent 工具复用)

    Args:
        project_id: 项目 ID
        project_name: 项目名
        base_url: 系统访问地址
        step_name: structure/function/failure/risk/optimization
    """
    base_url = base_url or os.getenv("FMEA_PUBLIC_URL", "http://10.180.17.11:7777")
    step_display = STEP_DISPLAY_NAMES.get(step_name, step_name)

    result = {
        "status": "ok",
        "project_id": project_id,
        "project_name": project_name or project_id,
        "step_name": step_name,
        "step_display": step_display,
        "sender": None,            # {email, name, source}
        "total": 0,
        "success_count": 0,
        "fail_count": 0,
        "skip_count": 0,
        "details": [],             # [{status, name, email, node_count, error}]
        "error": None,             # 顶层错误
    }

    # 1. 读取该步骤的基线数据
    step_data = _load_step_data(project_id, step_name)
    if not step_data:
        result["status"] = "error"
        result["error"] = f"项目 {project_id} 还没有{step_display}数据, 请先生成数据再推送。"
        return result

    # 2. 按指派人聚合
    structure_tree = None  # 用于 fallback: function/failure/risk/optimization 的指派人继承
    if step_name != "structure":
        structure_tree = _load_step_data(project_id, "structure")
    if step_name == "structure":
        groups = _build_assignee_groups(step_data)
    else:
        # function/failure/risk/optimization: 从 items[].functions[] 聚合，
        # 若 function 没有 assignee 则回退到结构树节点的 assignee
        groups = _build_assignee_groups_from_items(step_data.get("items", []), structure_tree)
    result["total"] = len(groups)
    if not groups:
        result["status"] = "warn"
        result["error"] = f"当前{step_display}数据中没有任何被指派的条目, 请先在结构树中指派工程师（function/failure/risk/optimization 的指派人继承自结构树）。"
        return result

    # 3. 查询用户邮箱
    user_ids = list(groups.keys())
    users = _lookup_user_emails(user_ids)

    # 4. 获取发件人 — 优先用项目负责人的邮箱授权码，兜底用 .env 全局配置
    owner = _get_owner_credentials(project_id)
    if owner:
        sender_email = owner["email"]
        sender_auth = owner["auth_code"]
        result["sender"] = {
            "email": sender_email, "name": owner["display_name"], "source": "project_owner",
            "role": owner.get("role", ""),
        }
    else:
        fallback = _get_fallback_credentials()
        if fallback:
            sender_email = fallback["email"]
            sender_auth = fallback["auth_code"]
            result["sender"] = {
                "email": sender_email, "name": fallback.get("display_name", ""),
                "source": "env_fallback",
            }
        else:
            result["status"] = "error"
            result["error"] = (
                "无法获取发件人凭据:\n"
                "  1. 项目负责人在 users 表中未配置 email_auth_code\n"
                "  2. .env 未配置 FMEA_NOTIFY_SENDER_EMAIL / FMEA_NOTIFY_SENDER_AUTH\n\n"
                "请按以下任一种方式配置:\n"
                "  A) 在「团队管理」页面编辑负责人信息，填写邮箱和邮箱授权码\n"
                "  B) 修改 .env 文件添加 FMEA_NOTIFY_SENDER_EMAIL 和 FMEA_NOTIFY_SENDER_AUTH"
            )
            return result

    # 5. 逐个发送
    proj_name = project_name or project_id
    for uid, items in groups.items():
        assignee = users.get(uid, {})
        email = (assignee.get("email") or "").strip()
        if not email:
            result["skip_count"] += 1
            result["details"].append({
                "status": "skip", "name": assignee.get("display_name", f"用户{uid}"),
                "email": "", "item_count": len(items), "error": "未配置邮箱",
            })
            continue
        subject, body = _build_email_content_step(assignee, items, project_id, proj_name, base_url, step_name, step_display)
        ok, err = _send_smtp(sender_email, sender_auth, email, subject, body)
        if ok:
            result["success_count"] += 1
            result["details"].append({
                "status": "success",
                "name": assignee.get("display_name", email),
                "email": email, "item_count": len(items), "error": None,
            })
        else:
            result["fail_count"] += 1
            result["details"].append({
                "status": "fail",
                "name": assignee.get("display_name", email),
                "email": email, "item_count": len(items), "error": err,
            })
    return result


def _build_email_content_step(
    assignee: dict, items: list, project_id: str,
    project_name: str, base_url: str, step_name: str, step_display: str,
) -> tuple:
    """生成邮件主题和正文（通用步骤版本 — v13: 精简为纯通知，不列条目明细）"""
    display = assignee.get("display_name") or assignee.get("username") or "工程师"
    subject = f"[FMEA] {project_name} — 您有 {len(items)} 个{step_display}条目需要确认"

    body = f"""您好 {display}：

您被指派为 {len(items)} 个 FMEA {step_display}条目的负责人，请登录系统查看、补充信息并确认。

项目: {project_name}
项目 ID: {project_id}
待确认条目数: {len(items)} 个

操作步骤:
  1. 登录系统: {base_url}
  2. 进入「FMEA 智能分析工作台」
  3. 选择项目并切换到「{step_display}」Tab
  4. 指派给您的条目会高亮显示
  5. 编辑信息后点击「✓ 确认本步骤」按钮提交

如有疑问，请联系项目负责人。

—— FMEA Agent 系统自动通知
{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
"""
    return subject, body


async def push_structure_to_engineers(
    project_id: str, project_name: str = "", base_url: Optional[str] = None,
) -> str:
    """Agent 工具: 推送结构树指派给工程师 (发邮件), 返回纯文本摘要."""
    data = await _push_to_engineers_internal(project_id, project_name, base_url)

    if data["status"] == "error":
        return f"❌ {data['error']}"
    if data["status"] == "warn":
        return f"⚠ {data['error']}"

    sender = data["sender"] or {}
    sender_info = f"发件人: {sender.get('name','')} <{sender.get('email','')}> (来源: {sender.get('source','')})"
    lines = [
        f"📨 推送完成 (共 {data['total']} 人 / 成功 {data['success_count']} / 失败 {data['fail_count']} / 跳过 {data['skip_count']})",
        f"   {sender_info}",
    ]
    for r in data["details"]:
        if r["status"] == "success":
            lines.append(f"   ✓ {r['name']}: 已发送 {r['node_count']} 个节点 -> {r['email']}")
        elif r["status"] == "fail":
            lines.append(f"   ✗ {r['name']}: 失败 - {r['error']}")
        else:
            lines.append(f"   ⚠ {r['name']}: {r['error']}")
    return "\n".join(lines)


# 同步版本 (用于 API endpoint)
def push_structure_to_engineers_sync(
    project_id: str, project_name: str = "", base_url: Optional[str] = None
) -> dict:
    """同步版本, 供 FastAPI 路由调用"""
    import asyncio
    return asyncio.run(push_structure_to_engineers(project_id, project_name, base_url))


# ── 工具函数 (供 Agent Tool Calling) ──
async def list_assigned_nodes(project_id: str, user_id: Optional[int] = None) -> str:
    """Agent 工具: 列出某项目已指派的节点 (按工程师聚合).

    Args:
        project_id: 项目 ID
        user_id: 工程师用户 ID, 留空则列出全部指派关系
    """
    tree = _load_structure_tree(project_id)
    if not tree:
        return f"❌ 项目 {project_id} 还没有结构树。"
    groups = _build_assignee_groups(tree)
    if not groups:
        return "当前没有指派关系."

    if user_id is not None:
        nodes = groups.get(str(user_id), [])
        if not nodes:
            return f"用户 {user_id} 没有任何指派节点."
        return f"用户 {user_id} 被指派 {len(nodes)} 个节点:\n" + "\n".join(
            f"- [{n.get('level','')}] {n.get('name','')}" for n in nodes
        )

    # 全部
    lines = [f"共 {len(groups)} 个被指派人:"]
    for uid, nodes in groups.items():
        lines.append(f"  - 用户 {uid}: {len(nodes)} 个节点")
    return "\n".join(lines)
