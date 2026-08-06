
import asyncio, sys, json
sys.path.insert(0, '.')
sys.path.insert(0, 'D:/agent-fmea/agno/.venv/Lib/site-packages')

from models.structure import StructureTree
from models.function import FunctionAnalysis
from engines.llm_client import get_llm_client
from knowledge.weknora_client import get_weknora_client
from orchestrator.failure_multi_agent_runner import FailureMultiAgentRunner

# 使用已有的 get_baseline 从 DB 读取
from engine.tools.fmea_helpers import get_baseline, resolve_project_id_to_db_pid

db_pid = resolve_project_id_to_db_pid("15")
print(f"DB_PID: {db_pid}", flush=True)
tree_data = get_baseline(db_pid, "structure")
func_data = get_baseline(db_pid, "function")

if not tree_data:
    print("ERROR: tree_data is empty", flush=True)
    sys.exit(1)
if not func_data:
    print("ERROR: func_data is empty", flush=True)
    sys.exit(1)

print(f"tree nodes: {len(tree_data.get('nodes',[]))}, func items: {len(func_data.get('items',[]))}", flush=True)

tree = StructureTree(**tree_data)
func_analysis = FunctionAnalysis(**func_data)

def _progress(phase, ratio, msg, extra=None):
    print(f"[{phase}] {ratio*100:.0f}% {msg}", flush=True)

runner = FailureMultiAgentRunner()
analysis = asyncio.run(runner.run(
    tree, func_analysis, "15", progress_callback=_progress,
))
print(f"RESULT: {len(analysis.items)} failure modes", flush=True)
