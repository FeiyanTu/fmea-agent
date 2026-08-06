# -*- coding: utf-8 -*-
"""
程序化生成 03-failure.json（不依赖 LLM 工具调用）

基于 02-function.json 中已带 child_node_id 的 function，对每个 (focus, child) 一对一生成
1-3 条 fallback 失效链，保证 03-failure.json 数据严格符合 AIAG-VDA 链式方法论。

效果：
- 报告行数 ≈ function 数 × 平均失效模式数（~1.5）
- next_lower_level 列对应单个 child_node（不再是逗号拼接）
"""
import json
import logging
from pathlib import Path
from collections import Counter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("gen-failure-v41")

PROJECT_ID = "12V低压电池"
DATA = Path(r"D:/agent-fmea/fmea-agent/data/projects/12V低压电池/fmea")

# ── 1) 加载数据 ──
st = json.load(open(DATA / "01-structure-tree.json", encoding="utf-8"))["nodes"]
fc = json.load(open(DATA / "02-function.json", encoding="utf-8"))["items"]
node_map = {n["id"]: n for n in st}

# ── 2) 修 02-function.json 中的伪 function（无 child_node_id）──
fixed_count = 0
for item in fc:
    node_id = item.get("node_id")
    if not node_id:
        continue
    real_children = [c for c in st if (c.get("parent_id", "") or c.get("parentId", "")) == node_id]
    if not real_children:
        # 叶子节点：functions 应该为 []
        if item.get("functions"):
            log.info(f"清空叶子节点 {item.get('node_name')} 的 {len(item['functions'])} 个伪 function")
            item["functions"] = []
            fixed_count += 1
    else:
        # 非叶子节点：保留有 child_node_id 的，清掉 child_node_id 为空的（说明 LLM fallback 出错）
        before = len(item.get("functions", []))
        item["functions"] = [fn for fn in item.get("functions", []) if fn.get("child_node_id")]
        if len(item["functions"]) != before:
            log.info(f"清理 {item.get('node_name')} 的 {before - len(item['functions'])} 个无 child_node_id 的伪 function")
            fixed_count += 1

if fixed_count:
    with open(DATA / "02-function.json", "w", encoding="utf-8") as f:
        json.dump({"items": fc}, f, ensure_ascii=False, indent=2)
    log.info(f"修复 {fixed_count} 个节点的伪 function")

# ── 3) 重新统计 ──
total_fns = sum(len(i.get("functions", [])) for i in fc)
fn_with_child = sum(1 for i in fc for fn in i.get("functions", []) if fn.get("child_node_id"))
log.info(f"修复后: {len(fc)} nodes, {total_fns} functions, {fn_with_child} 含 child_node_id ({100*fn_with_child/max(total_fns,1):.1f}%)")

# ── 4) 程序化生成 03-failure.json ──
# 每个 (focus, child, function) 生成 1-2 条 fallback 失效链
# FM/FC 用模板填充，确保与 child 相关

FM_TEMPLATES = [
    ("功能丧失", "功能完全无法实现", 8, "安全功能失效"),
    ("功能降级", "性能参数超出允差范围", 6, "功能降级传导"),
    ("接口失效", "信号/物理连接中断", 5, "接口失效传导"),
]
FE_TEMPLATES = [
    "上级组件无法获得预期功能，关键性能指标超出设计允差",
    "下级组件性能异常传导至 focus element，导致功能异常",
    "整车层级出现可感知的性能降级或失效报警",
]
FC_TEMPLATES = [
    "下级 {child} 内部缺陷（如材料/工艺/老化）导致 focus element 的功能异常",
    "下级 {child} 接口参数不匹配（如尺寸/电气规格/EMC）传导至 focus element",
    "下级 {child} 装配误差累积导致 focus element 关键功能失效",
]


def gen_failures(focus_id, focus_name, child_id, child_name, fn_detail, fn_idx):
    """对单个 (focus, child, function) 生成 1-2 条失效链"""
    child_node = node_map.get(child_id, {})
    child_sub = child_node.get("sub_children", [])  # 不存在，留空
    fn_desc = fn_detail.get("description", "")
    fn_id = fn_detail.get("function_id", f"fn-{focus_id}-{fn_idx:02d}")

    failures = []
    # 模式 1：功能丧失
    failures.append({
        "id": f"fm-{focus_id}-{fn_idx:02d}-01",
        "node_id": focus_id,
        "node_name": focus_name,
        "child_node_id": child_id,
        "function_id": fn_id,
        "function_description": fn_desc,
        "failure_effect": FE_TEMPLATES[0],
        "severity": 8,
        "severity_reason": f"S=8: 该失效导致 focus element（{focus_name}）针对子级 {child_name} 的功能完全丧失，影响关键性能",
        "failure_mode": f"针对 {child_name} 的功能丧失：{fn_desc[:40] if fn_desc else '功能无法实现'}",
        "failure_cause": FC_TEMPLATES[0].format(child=child_name),
        "failure_network_type": "功能丧失传导",
        "llm_generated": False,
        "user_modified": False,
    })
    # 模式 2：功能降级（仅当 fn_desc 非空时）
    if fn_desc:
        failures.append({
            "id": f"fm-{focus_id}-{fn_idx:02d}-02",
            "node_id": focus_id,
            "node_name": focus_name,
            "child_node_id": child_id,
            "function_id": fn_id,
            "function_description": fn_desc,
            "failure_effect": FE_TEMPLATES[1],
            "severity": 5,
            "severity_reason": f"S=5: 该失效导致 {child_name} 相关性能降级，多数客户可感知",
            "failure_mode": f"针对 {child_name} 的功能降级：性能参数超出允差",
            "failure_cause": FC_TEMPLATES[2].format(child=child_name),
            "failure_network_type": "功能降级传导",
            "llm_generated": False,
            "user_modified": False,
        })
    return failures


log.info("=" * 60)
log.info("开始程序化生成 03-failure.json")
log.info("=" * 60)

all_failures = []
for item in fc:
    nid = item.get("node_id")
    nname = item.get("node_name", "")
    for idx, fn in enumerate(item.get("functions", []), 1):
        cid = fn.get("child_node_id", "")
        cname = node_map.get(cid, {}).get("name", cid)
        if not cid:
            continue
        all_failures.extend(gen_failures(nid, nname, cid, cname, fn, idx))

log.info(f"生成 {len(all_failures)} 条失效链")

# 统计
by_focus = Counter(f["node_id"] for f in all_failures)
by_child = Counter(f["child_node_id"] for f in all_failures)
fail_with_child = sum(1 for f in all_failures if f.get("child_node_id"))
log.info(f"  涉及 focus 节点: {len(by_focus)}")
log.info(f"  涉及 child 节点: {len(by_child)}")
log.info(f"  含 child_node_id: {fail_with_child}/{len(all_failures)} ({100*fail_with_child/max(len(all_failures),1):.1f}%)")

# ── 5) 保存 ──
with open(DATA / "03-failure.json", "w", encoding="utf-8") as f:
    json.dump({
        "items": all_failures,
        "generated_by": "rerun_step2_3_v41 + gen_failure_v41 (programmatic)",
        "version": "v4.1",
    }, f, ensure_ascii=False, indent=2)
log.info(f"已保存 03-failure.json: {len(all_failures)} 条")

# ── 6) 抽样展示 ──
log.info("=" * 60)
log.info("抽样：12V低压电源系统 → 蓄电池 链")
log.info("=" * 60)
for f in all_failures[:5]:
    log.info(f"  [{f['id']}] focus={f['node_name']} → child={node_map.get(f['child_node_id'],{}).get('name','?')}")
    log.info(f"    FM: {f['failure_mode'][:80]}")
    log.info(f"    FC: {f['failure_cause'][:80]}")
