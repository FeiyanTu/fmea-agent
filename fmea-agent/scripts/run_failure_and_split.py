"""
1. 跑失效分析 → 写入 DB 系统基线
2. 按工程师拆分 → 写入 DB 每个工程师行
"""
import asyncio, os, sys, json, logging
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from dotenv import load_dotenv
load_dotenv(str(PROJECT_DIR / ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("run_failure_and_split")

HOST = os.getenv("URL_AGENT_DB_HOST", "10.180.17.11")
PORT = int(os.getenv("URL_AGENT_DB_PORT", "5433"))
DB = os.getenv("URL_AGENT_DB_NAME", "fmea")
USER = os.getenv("URL_AGENT_DB_USER", "postgres")
PWD = os.getenv("URL_AGENT_DB_PASSWORD", "postgres123!@#")


def get_baseline(db_pid: int, step_name: str, assignee_user_id: int = -1) -> dict:
    import psycopg2
    conn = psycopg2.connect(host=HOST, port=PORT, dbname=DB, user=USER, password=PWD)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM fmea_step_assignee_data "
                "WHERE project_id = %s AND step_name = %s AND assignee_user_id = %s",
                (db_pid, step_name, assignee_user_id),
            )
            row = cur.fetchone()
            return row[0] if row else {}
    finally:
        conn.close()


def upsert_baseline(db_pid: int, step_name: str, data: dict, assignee_user_id: int = -1):
    import psycopg2
    conn = psycopg2.connect(host=HOST, port=PORT, dbname=DB, user=USER, password=PWD)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO fmea_step_assignee_data
                    (project_id, step_name, assignee_user_id, data, version, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, 1, NOW())
                ON CONFLICT (project_id, step_name, assignee_user_id)
                DO UPDATE SET data = EXCLUDED.data, version = fmea_step_assignee_data.version + 1, updated_at = NOW()
            """, (db_pid, step_name, assignee_user_id, json.dumps(data, ensure_ascii=False)))
            conn.commit()
    finally:
        conn.close()


def get_db_pid(project_id: str) -> int:
    import psycopg2
    conn = psycopg2.connect(host=HOST, port=PORT, dbname=DB, user=USER, password=PWD)
    try:
        with conn.cursor() as cur:
            try:
                pid = int(project_id)
                cur.execute("SELECT id FROM projects WHERE id = %s", (pid,))
                row = cur.fetchone()
                if row: return row[0]
            except ValueError: pass
            cur.execute("SELECT id FROM projects WHERE name = %s", (project_id,))
            row = cur.fetchone()
            return row[0] if row else 0
    finally:
        conn.close()


async def run_failure_analysis(db_pid: int) -> dict:
    """跑失效分析，返回完整 items"""
    from models.structure import StructureTree
    from models.function import FunctionAnalysis
    from orchestrator.failure_multi_agent_runner import FailureMultiAgentRunner

    tree_data = get_baseline(db_pid, "structure", -1)
    func_data = get_baseline(db_pid, "function", -1)

    tree = StructureTree(**tree_data)
    func_analysis = FunctionAnalysis(**func_data)

    def progress(phase, ratio, msg, extra=None):
        logger.info(f"  [{phase}] {ratio*100:.0f}% {msg}")

    runner = FailureMultiAgentRunner()
    analysis = await runner.run(tree, func_analysis, str(db_pid), progress_callback=progress)
    return analysis.model_dump()


def build_node_to_engineer(tree_data: dict) -> dict:
    """从结构树构建 node_id → assignee_user_id 映射"""
    mapping = {}
    nodes = tree_data.get("nodes", [])
    for n in nodes:
        nid = n.get("id")
        assignee = n.get("assignee_user_id")
        if nid and assignee and assignee != -1:
            mapping[nid] = assignee
    return mapping


def split_failure_by_engineer(items: list, node_to_engineer: dict) -> dict:
    """按工程师拆分失效链"""
    engineer_items = {}  # assignee_user_id -> [items]

    for it in items:
        nid = it.get("node_id", "")
        assignee = node_to_engineer.get(nid, -1)

        if assignee == -1:
            # 未指派的失效放到系统基线（-1），由所有工程师可见
            engineer_items.setdefault(-1, []).append(it)
        else:
            engineer_items.setdefault(assignee, []).append(it)

    return engineer_items


def main():
    db_pid = get_db_pid("15")
    logger.info(f"项目 DB_PID = {db_pid}")

    # 读结构树，建立 node → engineer 映射
    tree_data = get_baseline(db_pid, "structure", -1)
    node_to_engineer = build_node_to_engineer(tree_data)
    logger.info(f"node→engineer 映射: {len(node_to_engineer)} 个已指派节点")
    logger.info(f"参与的工程师: {sorted(set(node_to_engineer.values()))}")

    # 读各工程师的结构树数据（只包含他们负责的节点）
    engineer_ids = sorted(set(node_to_engineer.values()))
    for eid in engineer_ids:
        e_data = get_baseline(db_pid, "structure", eid)
        if e_data:
            logger.info(f"  工程师 {eid} 的结构树节点数: {len(e_data.get('nodes', []))}")

    # 1. 跑失效分析
    logger.info("=" * 60)
    logger.info("步骤 1: 跑失效分析...")
    logger.info("=" * 60)
    analysis = asyncio.run(run_failure_analysis(db_pid))
    items = analysis.get("items", [])
    logger.info(f"失效链总数: {len(items)}")

    # 2. 写入系统基线（assignee_user_id=-1）
    system_data = {
        "items": items,
        "user_edited": False,
        "llm_raw_output": json.dumps({"method": "FailureMultiAgentRunner"}, ensure_ascii=False),
        "engineer_assignments": node_to_engineer,
    }
    upsert_baseline(db_pid, "failure", system_data, -1)
    logger.info(f"✅ 系统基线已写入 (assignee_user_id=-1), {len(items)} 条失效链")

    # 3. 按工程师拆分写入
    logger.info("=" * 60)
    logger.info("步骤 2: 按工程师拆分...")
    logger.info("=" * 60)

    engineer_items = split_failure_by_engineer(items, node_to_engineer)
    for eid, e_items in engineer_items.items():
        if eid == -1:
            continue  # 系统基线已写
        engineer_data = {
            "items": e_items,
            "user_edited": False,
            "llm_raw_output": "",
            "assigned_node_ids": [nid for nid, e in node_to_engineer.items() if e == eid],
            "view_only": True,  # 工程师只能查看，不能修改
        }
        upsert_baseline(db_pid, "failure", engineer_data, eid)
        logger.info(f"  ✅ 工程师 {eid} 数据已写入: {len(e_items)} 条失效链")

    logger.info("=" * 60)
    logger.info(f"🎉 完成！项目 {db_pid} 的失效分析已写入 DB")
    logger.info(f"  系统基线: {len(items)} 条失效链")
    logger.info(f"  工程师分片: {len(engineer_items) - 1} 个工程师")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()