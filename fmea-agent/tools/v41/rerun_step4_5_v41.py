"""重跑 step 4-5 v4.1：chain child_node_id 已透传到 risk/optimization
- step 4: 不调 LLM，纯计算（run_risk_analysis_tool）
- step 5: 调 LLM（run_optimization_tool），max_items=10

运行: python tools/v41/rerun_step4_5_v41.py
"""
import sys
import os
import json
import asyncio
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# 显式加载 .env（main.py 才加载，独立调用时必须）
from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

PROJECT_ID = "12V低压电池"


def run_step4():
    """Step 4: 风险评估（不调 LLM）"""
    from engine.tools.risk_tools import run_risk_analysis_tool
    print("=" * 60)
    print(f"Step 4: 风险评估（不调 LLM）— {PROJECT_ID}")
    print("=" * 60)
    result = run_risk_analysis_tool(PROJECT_ID)
    print(result)

    # 验证
    risk_path = ROOT / f"data/projects/{PROJECT_ID}/fmea/04-risk.json"
    risk = json.load(open(risk_path, encoding="utf-8"))
    items = risk.get("items", [])
    with_child = sum(1 for i in items if i.get("child_node_id"))
    print(f"\n[验证] {len(items)} 条, child_node_id 透传: {with_child}/{len(items)}")
    return items


async def run_step5(max_items: int = 10):
    """Step 5: 优化分析（调 LLM）"""
    from engine.tools.optimization_tools import run_optimization_tool
    print()
    print("=" * 60)
    print(f"Step 5: 优化分析（调 LLM，max_items={max_items}）— {PROJECT_ID}")
    print("=" * 60)
    result = await run_optimization_tool(PROJECT_ID, max_items=max_items)
    print(result)

    # 验证
    opt_path = ROOT / f"data/projects/{PROJECT_ID}/fmea/05-optimization.json"
    opt = json.load(open(opt_path, encoding="utf-8"))
    items = opt.get("items", [])
    with_child = sum(1 for i in items if i.get("child_node_id"))
    print(f"\n[验证] {len(items)} 条, child_node_id 透传: {with_child}/{len(items)}")
    return items


def main():
    items_4 = run_step4()
    items_5 = asyncio.run(run_step5(max_items=10))
    print()
    print("=" * 60)
    print("完成！")
    print(f"  step 4: {len(items_4)} 条 risk 评估")
    print(f"  step 5: {len(items_5)} 条 optimization（高风险前 10）")


if __name__ == "__main__":
    main()
