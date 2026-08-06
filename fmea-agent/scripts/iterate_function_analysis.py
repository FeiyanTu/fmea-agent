"""
多 Agent 功能分析运行器（纯运行，不评分）

用法：
    python scripts/iterate_function_analysis.py [--mock-llm]

输出保存在 data/projects/3_6KW/fmea/02-function.json
"""
import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 加载 .env（让 llm_client 读到 API Key）
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

PROJECT_ID = "3_6KW"


async def main_async(args):
    from models.structure import StructureTree
    from orchestrator.function_multi_agent_runner import FunctionMultiAgentRunner

    # 加载结构树
    struct_path = ROOT / "data" / "projects" / PROJECT_ID / "fmea" / "01-structure-tree.json"
    if struct_path.exists():
        with open(struct_path, "r", encoding="utf-8-sig") as f:
            raw = f.read()
        raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', raw)
        tree_data = json.loads(raw)
        tree = StructureTree(**tree_data)
    else:
        print("❌ 无结构树文件")
        return
    print(f"    → 结构树: {len(tree.nodes)} 个节点")

    # 构造 LLM 客户端
    if args.mock_llm:
        from scripts.verify_multi_agent_function import MockLLM
        llm = MockLLM()
    else:
        from engine.llm_client import get_llm_client
        llm = get_llm_client()

    runner = FunctionMultiAgentRunner(
        llm_client=llm,
        data_dir=str(ROOT / "data" / "projects"),
    )

    def progress_cb(phase, ratio, msg, extra=None):
        if extra and extra.get("agent_events"):
            status = ", ".join(
                f"{e.get('agent','')}={e.get('status','')}"
                for e in extra["agent_events"]
            )
            print(f"  📡 [{ratio*100:.0f}%] {status}", flush=True)

    print(f"\n🧠 运行多 Agent 功能分析...")
    t0 = time.time()
    analysis = await runner.run(tree, PROJECT_ID, progress_cb)
    elapsed = time.time() - t0

    # 保存输出
    out_path = ROOT / "data" / "projects" / PROJECT_ID / "fmea" / "02-function.json"
    full_data = analysis.model_dump(exclude={"component_name", "higher_level_function"})
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(full_data, f, ensure_ascii=False, indent=2, default=str)

    stats = {
        "node_count": len(full_data.get("items", [])),
        "function_count": sum(len(it.get("functions", [])) for it in full_data.get("items", [])),
        "relation_count": len(full_data.get("function_network", {}).get("relations", [])),
        "elapsed_sec": round(elapsed, 1),
    }

    print(f"\n{'='*60}")
    print(f"  ✅ 分析完成 | 耗时 {elapsed:.0f}s")
    print(f"  节点: {stats['node_count']} | 功能: {stats['function_count']} | 关系: {stats['relation_count']}")
    print(f"  输出: {out_path}")
    print(f"{'='*60}")

    # 保存统计摘要（供主 agent 读取）
    stats_path = ROOT / "data" / "projects" / PROJECT_ID / "fmea" / "02-function-stats.json"
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mock-llm", action="store_true")
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
