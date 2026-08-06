import asyncio, os, sys, psycopg2
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# 直接加载 .env
from dotenv import load_dotenv
load_dotenv(str(PROJECT_DIR / ".env"))

HOST = os.getenv("URL_AGENT_DB_HOST", "10.180.17.11")
PORT = int(os.getenv("URL_AGENT_DB_PORT", "5433"))
DB = os.getenv("URL_AGENT_DB_NAME", "fmea")
USER = os.getenv("URL_AGENT_DB_USER", "postgres")
PWD = os.getenv("URL_AGENT_DB_PASSWORD", "postgres123!@#")


def get_baseline_direct(db_pid: int, step_name: str) -> dict:
    """直接从 fmea_step_assignee_data 表读取基线"""
    conn = psycopg2.connect(host=HOST, port=PORT, dbname=DB, user=USER, password=PWD)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM fmea_step_assignee_data "
                "WHERE project_id = %s AND step_name = %s AND assignee_user_id = -1",
                (db_pid, step_name),
            )
            row = cur.fetchone()
            return row[0] if row else {}
    finally:
        conn.close()


def get_db_pid(project_id: str) -> int:
    conn = psycopg2.connect(host=HOST, port=PORT, dbname=DB, user=USER, password=PWD)
    try:
        with conn.cursor() as cur:
            try:
                pid = int(project_id)
                cur.execute("SELECT id FROM projects WHERE id = %s", (pid,))
                row = cur.fetchone()
                if row:
                    return row[0]
            except ValueError:
                pass
            cur.execute("SELECT id FROM projects WHERE name = %s", (project_id,))
            row = cur.fetchone()
            return row[0] if row else 0
    finally:
        conn.close()


async def main():
    db_pid = get_db_pid("15")
    print(f"DB_PID={db_pid}", flush=True)
    if not db_pid:
        print("ERROR: 无法找到项目 15", flush=True)
        return

    tree_data = get_baseline_direct(db_pid, "structure")
    func_data = get_baseline_direct(db_pid, "function")

    if not tree_data:
        print("ERROR: 结构树数据为空", flush=True)
        return
    if not func_data:
        print("ERROR: 功能分析数据为空", flush=True)
        return

    print(f"Tree nodes: {len(tree_data.get('nodes',[]))}, Func items: {len(func_data.get('items',[]))}", flush=True)

    from models.structure import StructureTree
    from models.function import FunctionAnalysis

    tree = StructureTree(**tree_data)
    func_analysis = FunctionAnalysis(**func_data)

    from orchestrator.failure_multi_agent_runner import FailureMultiAgentRunner

    def progress(phase, ratio, msg, extra=None):
        print(f"  [{phase}] {ratio*100:.0f}% {msg}", flush=True)

    runner = FailureMultiAgentRunner()
    analysis = await runner.run(tree, func_analysis, str(db_pid), progress_callback=progress)

    stats = analysis.get_summary_stats()
    print(f"\n{'='*60}", flush=True)
    print(f"✅ 失效分析完成: {stats}", flush=True)
    print(f"{'='*60}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
