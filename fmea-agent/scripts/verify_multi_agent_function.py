"""
功能分析多 Agent 路径验证脚本

用法：
    python scripts/verify_multi_agent_function.py
        [--project PROJECT_ID]      # 默认 3_6KW
        [--mock-llm]                 # 用 mock LLM（不调真实接口）
        [--no-weknora]               # 跳过 WeKnora 检索
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# 把项目根加到 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def print_section(title: str):
    print("\n" + "=" * 78, flush=True)
    print(f"  {title}", flush=True)
    print("=" * 78, flush=True)


async def main_async(args):
    from models.structure import StructureTree, StructureTreeNode
    from orchestrator.function_multi_agent_runner import (
        FunctionMultiAgentRunner,
        run_function_analysis_multi_agent,
    )

    data_dir = ROOT / "data" / "projects"
    project_id = args.project or "3_6KW"

    # ── 1) 加载或构造结构树 ──
    print_section("1) 准备结构树")
    struct_path = data_dir / project_id / "fmea" / "01-structure-tree.json"

    if struct_path.exists() and not args.mock_llm:
        print(f"  从磁盘加载: {struct_path}", flush=True)
        with open(struct_path, "r", encoding="utf-8-sig") as f:
            tree_data = json.loads(f.read())
        # 清理 None 字段
        for n in tree_data.get("nodes", []):
            for k in ("part_number", "material", "specification"):
                if n.get(k) is None:
                    n[k] = ""
            if n.get("quantity") is None:
                n["quantity"] = 1
            if n.get("confidence") is None:
                n["confidence"] = 0.0
            if n.get("parent_id") is None:
                n["parent_id"] = ""
        tree = StructureTree(**tree_data)
    else:
        print(f"  ⚠️ 磁盘无结构树，使用内置 mock 结构树（含 6 个节点覆盖多领域）", flush=True)
        tree = StructureTree(
            project_id=project_id,
            project_name=project_id,
            nodes=[
                StructureTreeNode(id="n1", name="电池包系统", level="系统", parent_id=""),
                StructureTreeNode(id="n2", name="BMS硬件主板", level="组件", parent_id="n1"),
                StructureTreeNode(id="n3", name="电芯模组", level="组件", parent_id="n1"),
                StructureTreeNode(id="n4", name="结构箱体", level="组件", parent_id="n1"),
                StructureTreeNode(id="n5", name="高压连接器", level="零件", parent_id="n2"),
                StructureTreeNode(id="n6", name="电芯", level="零件", parent_id="n3"),
            ],
        )

    print(f"  节点数: {len(tree.nodes)}", flush=True)
    for n in tree.nodes:
        print(f"    - {n.id}  {n.name}  ({n.level})", flush=True)

    # ── 2) 准备 LLM ──
    print_section("2) 准备 LLM 客户端")
    llm = None
    if args.mock_llm:
        print("  使用 Mock LLM（不调真实接口）", flush=True)
        llm = MockLLM()
    else:
        try:
            from engine.llm_client import get_llm_client
            llm = get_llm_client()
            print(f"  LLM 客户端: {type(llm).__name__}", flush=True)
        except Exception as e:
            print(f"  ⚠️ LLM 客户端加载失败: {e}", flush=True)
            print(f"  → 自动降级到 Mock LLM", flush=True)
            llm = MockLLM()

    # ── 3) 跑多 Agent ──
    print_section("3) 执行多 Agent 功能分析")
    t0 = time.time()
    try:
        def _sync_progress(phase, ratio, msg):
            print(f"  📡 [{phase}] {ratio*100:.0f}% {msg}", flush=True)

        analysis = await run_function_analysis_multi_agent(
            tree=tree,
            project_id=project_id if not args.mock_llm else "",
            data_dir=str(data_dir),
            progress_callback=_sync_progress,
        )
    except Exception as e:
        import traceback
        print(f"\n  ❌ 多 Agent 路径异常: {e}", flush=True)
        traceback.print_exc()
        return 1
    elapsed = time.time() - t0

    # ── 4) 验证输出 ──
    print_section("4) 验证输出")
    print(f"  items: {len(analysis.items)}", flush=True)
    total_fn = sum(len(it.functions) for it in analysis.items)
    print(f"  functions: {total_fn}", flush=True)
    rel_count = len(analysis.function_network.relations) if analysis.function_network else 0
    print(f"  relations: {rel_count}", flush=True)

    if analysis.validation_report:
        print(f"  validation_report:", flush=True)
        for k, v in analysis.validation_report.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict):
                status = "✅" if v.get("passed") else "⚠️"
                desc = v.get("description", "")
                print(f"    {status} {k}: {desc}", flush=True)
                for issue in v.get("issues", [])[:3]:
                    print(f"        - {issue}", flush=True)
        # 多 Agent 元信息（在 _multi_agent_meta 字段里）
        meta = analysis.validation_report.get("_multi_agent_meta")
        if meta:
            print(f"  multi_agent_meta:", flush=True)
            print(f"    enabled: {meta.get('enabled')}", flush=True)
            print(f"    agent_count: {meta.get('agent_count')}", flush=True)
            print(f"    weknora_kb_id: {meta.get('weknora_kb_id') or '(未配置)'}", flush=True)
            print(f"    log_records: {len(meta.get('log_records', []))}", flush=True)

    # ── 5) 打印每个 Agent 的输出 ──
    if args.verbose and analysis.items:
        print_section("5) 详细输出（前 5 个节点）")
        for it in analysis.items[:5]:
            print(f"  节点 [{it.node_id}] {it.node_name}:", flush=True)
            for fn in it.functions[:3]:
                print(
                    f"    - [{fn.function_class}/{fn.flow_type}] {fn.description[:60]}",
                    flush=True,
                )
                if fn.responsible_agent:
                    print(f"      agent: {fn.responsible_agent}", flush=True)

    # ── 6) 保存输出文件 ──
    if args.save:
        out_dir = data_dir / project_id / "fmea"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "02-function-multi-agent-test.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(
                analysis.model_dump(exclude={"llm_raw_output"}),
                f, ensure_ascii=False, indent=2, default=str,
            )
        print(f"\n  💾 输出已保存: {out_path}", flush=True)

    print_section(f"✅ 验证完成  耗时 {elapsed:.1f}s")
    return 0


class MockLLM:
    """Mock LLM 客户端 — 返回结构化测试数据，不调真实接口"""

    async def safe_call(self, system_prompt: str, user_message: str,
                        tools=None, fallback_to_complete=True, **kwargs) -> str:
        # 根据 system_prompt 判断 Agent 角色
        if "BMS硬件" in system_prompt:
            return json.dumps({
                "functions": [
                    {
                        "node_id": "n2", "node_name": "BMS硬件主板",
                        "functions": [{
                            "child_node_id": "n5",
                            "description": "提供12V电源输出 电压范围 9-16V",
                            "function_class": "主功能",
                            "function_category": "能量输出",
                            "flow_type": "电气",
                            "responsible_agent": "bms_hardware",
                        }],
                    }
                ],
                "relations": [],
            })
        elif "电芯" in system_prompt:
            return json.dumps({
                "functions": [
                    {
                        "node_id": "n6", "node_name": "电芯",
                        "functions": [{
                            "description": "储存电能 容量3.6kWh",
                            "function_class": "主功能",
                            "function_category": "能量储存",
                            "flow_type": "电气",
                            "responsible_agent": "cell",
                        }],
                    }
                ],
                "relations": [],
            })
        elif "结构" in system_prompt:
            return json.dumps({
                "functions": [
                    {
                        "node_id": "n4", "node_name": "结构箱体",
                        "functions": [{
                            "description": "提供防护外壳 防护等级IP67",
                            "function_class": "安全强制功能",
                            "function_category": "防护",
                            "flow_type": "机械力",
                            "responsible_agent": "structure",
                        }],
                    }
                ],
                "relations": [],
            })
        else:
            # 兜底：返回空 functions
            return json.dumps({"functions": [], "relations": []})

    def extract_json(self, text: str) -> dict:
        try:
            return json.loads(text)
        except Exception:
            return {}

    async def call_complete(self, **kwargs):
        return "{}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", default="3_6KW")
    p.add_argument("--mock-llm", action="store_true", help="用 mock LLM（不调真实接口）")
    p.add_argument("--no-weknora", action="store_true", help="跳过 WeKnora 检索")
    p.add_argument("--save", action="store_true", help="保存 02-function-multi-agent-test.json")
    p.add_argument("--verbose", "-v", action="store_true", help="打印每个节点的详细输出")
    args = p.parse_args()

    exit_code = asyncio.run(main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
