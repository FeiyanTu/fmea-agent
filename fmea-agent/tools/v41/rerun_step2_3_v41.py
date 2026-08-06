# -*- coding: utf-8 -*-
"""
重跑 Step 2 (功能分析) + Step 3 (失效分析) — v4.1 链式方法论
把数据按 (focus_node, child_node) 重新生成，写入 02-function.json / 03-failure.json
"""
import sys
import json
import asyncio
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("rerun-v4.1")

# 加载项目根目录
sys.path.insert(0, str(Path(__file__).resolve().parent))
import os
os.chdir(Path(__file__).resolve().parent)
# 加载 .env
from pathlib import Path as P
for line in open(P("D:/agent-fmea/fmea-agent/.env"), encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip())

PROJECT_ID = "12V低压电池"
DATA_DIR = P("D:/agent-fmea/fmea-agent/data/projects") / PROJECT_ID / "fmea"

# 加载结构树
from models.structure import StructureTree
tree_data = json.load(open(DATA_DIR / "01-structure-tree.json", encoding="utf-8"))
tree = StructureTree.model_validate(tree_data)
log.info(f"加载结构树: {len(tree.nodes)} 个节点")

# Step 2: 功能分析
from agents.step2_function import FunctionAgent
fa = FunctionAgent(enable_kb_search=True)
log.info("=" * 60)
log.info("Step 2: 功能分析（v4.1 链式）")
log.info("=" * 60)
fa_result = asyncio.run(fa.generate_function_analysis(tree=tree, project_id=PROJECT_ID))

# 保存 02-function.json
fa_data = fa_result.model_dump() if hasattr(fa_result, "model_dump") else fa_result
with open(DATA_DIR / "02-function.json", "w", encoding="utf-8") as f:
    json.dump(fa_data, f, ensure_ascii=False, indent=2)
log.info(f"Step 2 完成: {len(fa_data.get('items', []))} 个 node, "
         f"{sum(len(i.get('functions', [])) for i in fa_data.get('items', []))} 个 function")

# 验证 child_node_id 填充
fa_items = fa_data.get("items", [])
fn_total = sum(len(i.get("functions", [])) for i in fa_items)
fn_with_child = sum(1 for i in fa_items for fn in i.get("functions", []) if fn.get("child_node_id"))
log.info(f"  child_node_id 填充: {fn_with_child}/{fn_total} ({100*fn_with_child/max(fn_total,1):.1f}%)")

# Step 3: 失效分析
from agents.step3_failure import FailureAgent, run_failure_analysis
log.info("=" * 60)
log.info("Step 3: 失效分析（v4.1 链式）")
log.info("=" * 60)
from models.function import FunctionAnalysis
func_analysis = FunctionAnalysis.model_validate(fa_data)
fail_result = asyncio.run(run_failure_analysis(tree=tree, func_analysis=func_analysis, project_id=PROJECT_ID))

# 保存 03-failure.json
fl_data = fail_result.model_dump() if hasattr(fail_result, "model_dump") else fail_result
with open(DATA_DIR / "03-failure.json", "w", encoding="utf-8") as f:
    json.dump(fl_data, f, ensure_ascii=False, indent=2)
log.info(f"Step 3 完成: {len(fl_data.get('items', []))} 条失效链")

# 验证 child_node_id
fl_items = fl_data.get("items", [])
fail_with_child = sum(1 for f in fl_items if f.get("child_node_id"))
log.info(f"  child_node_id 填充: {fail_with_child}/{len(fl_items)} ({100*fail_with_child/max(len(fl_items),1):.1f}%)")

log.info("=" * 60)
log.info("完成。可执行 generate_report_tool() 验证。")
