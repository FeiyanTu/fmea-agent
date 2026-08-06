"""
多 Agent 失效分析 Runner — 对齐 MultiAgentFMEA 参考方案

设计：
- 不重写 step3_failure.py，保持单 Agent 路径可降级
- 与 function_multi_agent_runner.py 同一架构模式
- LLM 路由节点 → 专业 Agent 并发分析失效 → 系统集成专家汇总
- 法规 Agent 并行提供上下文
- 高风险主题传导链路专项复核
- 输出兼容 FailureAnalysis 模型
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from models.structure import StructureTree, StructureTreeNode
from models.function import FunctionAnalysis, FunctionAnalysisItem, FunctionDetail
from models.failure import FailureMode, FailureAnalysis

logger = logging.getLogger("fmea.multi_agent.failure")


def _console_banner(title: str, color: str = "=") -> None:
    line = color * 78
    print(f"\n{line}\n  {title}\n{line}", flush=True)


def _console_log(tag: str, msg: str) -> None:
    print(f"  [{tag}] {msg}", flush=True)


# ── 默认配置（可通过 env 或 init 参数覆盖） ──

DEFAULT_ROUTING_DISTRIBUTION_THRESHOLD = 0.50
DEFAULT_HIGH_SEVERITY_THRESHOLD = 7
DEFAULT_TRANSMISSION_CHAIN_MIN_LENGTH = 3


def _build_agent_capability_description(a: dict) -> str:
    """从 Agent 配置构建完整能力描述文本（用于路由 prompt）"""
    name = a.get("name", "?")
    domain = a.get("domain", "")
    responsibilities = a.get("responsibilities", [])
    boundaries = a.get("boundaries", [])
    parts = [f"Agent: {name}", f"领域: {domain}"]
    if responsibilities:
        parts.append("职责:")
        for r in responsibilities:
            parts.append(f"  - {r}")
    if boundaries:
        parts.append("边界（不涉及）:")
        for b in boundaries:
            parts.append(f"  - {b}")
    return "\n".join(parts)


# ════════════════════════════════════════════════════════════════
# FailureStepAgent: 失效分析专用 Agent（参考 FunctionStepAgent）
# ════════════════════════════════════════════════════════════════

class FailureStepAgent:
    """step3 失效分析专用 Agent

    每个专业 Agent 接收已按领域路由的节点列表，对每个节点上的每条功能，
    调用 LLM 输出失效链 (FE → S → FM → FC)。
    """

    def __init__(self, config, llm_client=None, weknora=None):
        self.config = config
        self.name = config.name
        self.display_name = config.display_name
        self.domain = config.domain
        self.responsibilities = config.responsibilities
        self.boundaries = config.boundaries
        from models.agent_config import AgentRole
        self.role = AgentRole.PROFESSIONAL
        self.llm_client = llm_client
        self._weknora = weknora
        self._system_prompt = self._build_system_prompt()

    def __repr__(self) -> str:
        return f"<{self.display_name} ({self.name}) [FailureStepAgent]>"

    def build_system_prompt(self) -> str:
        return self._system_prompt

    def _build_system_prompt(self) -> str:
        lines = [
            f"你是 **{self.display_name}**，在多 Agent DFMEA 系统的【失效分析】阶段工作。",
            "",
            f"## 你的专业领域",
            f"{self.domain}",
            "",
            "## 你的职责",
        ]
        for i, r in enumerate(self.responsibilities, 1):
            lines.append(f"{i}. {r}")
        lines.append("")
        lines.append("## 边界约束（严格禁止越界）")
        for b in self.boundaries:
            lines.append(f"- {b}")
        lines.extend([
            "",
            "## 当前任务",
            "系统集成专家已按你的专业领域把相关节点分配给你。",
            "你需要对每个节点上的每条功能（已有功能分析结果），识别其潜在的设计失效模式。",
            "",
            "## 失效分析方法论（AIAG/VDA 2019 链式方法论）",
            "",
            "### 1. 一对一推导原则",
            "每条设计功能对应一套完整失效三要素：",
            "- failureEffect (FE): 失效模式对上一较高级别要素和/或最终用户的失效影响",
            "  （按五级路径传递：下级子零件→子零件→子系统→总成→整车）",
            "- failureMode (FM): 关注要素在该功能维度上的失效模式（具体可观测表现）",
            "- failureCause (FC): 导致 FM 的设计层面根源缺陷",
            "",
            "### 2. 失效原因限定",
            "FC 必须仅限设计层面的根源缺陷，严禁纳入：",
            "- 制程/装配/产线/工装/来料/操作工等非设计因素",
            "- 不得使用 '焊接不良' '装配不到位' '来料不合格' '操作失误' 等表述",
            "",
            "### 3. 失效影响描述",
            "FE 按以下五级路径逐级传递：",
            "下级子零件失效 → 子零件功能降级 → 子系统性能受限 → 总成/系统降级 → 整车/最终用户影响",
            "",
            "### 4. 严重度 S 评分 (AIAG/VDA 标准)",
            "10=安全/法规无预警  9=安全/法规有预警  8=主要功能丧失  7=主要功能降级",
            "6=次要功能丧失  5=外观/噪音(明显)  4=外观/噪音(多数)  3=外观/噪音(部分)  2=几乎无影响  1=无影响",
            "",
            "### 5. 功能网传导类型",
            "每条失效必须标注 failureNetworkType，从以下六类选择：",
            "- 安全功能失效  - 功能丧失传导  - 功能降级传导",
            "- 冗余失效  - 接口失效传导  - 一般失效",
            "",
            "## 输出格式",
            "每个 (node, function) 对输出 1~3 条失效链，每条包含：",
            "childNodeId, childNodeName, functionId, functionDescription,",
            "failureEffect, severity (1-10), severityReason, failureMode, failureCause, failureNetworkType",
            "",
            "## 关键原则",
            "1. 【最重要】每条失效链必须带有 childNodeId，标识针对哪个下一较高级别",
            "2. FC 必须仅限设计缺陷，禁止制程/装配/来料描述",
            "3. FE 必须是 FM 的结果，FC 必须是 FM 的原因——链式逻辑必须清晰",
            "4. 严重度(S)必须参考 AIAG/VDA 标准并给出评分理由",
            "5. 功能网传导类型必须准确标注",
            "6. 跨功能级联失效：若本功能失效会引发其他功能失效，需在 FE 中描述其对上游/同级的影响",
        ])
        return "\n".join(lines)

    # ── 功能合并降本（P2） ──

    async def _group_functions_by_semantics(self, functions: list, llm) -> list:
        """对同节点下的功能做 LLM 语义分组，相同语义的功能合并为一次 LLM 调用

        返回分组列表，每组包含一个或多个 function_detail 的 dict。
        """
        import os
        if not os.getenv("FMEA_FUNCTION_GROUPING_ENABLED", "1") == "1":
            return [[fn] for fn in functions]

        # 按 node_id 分桶
        from collections import defaultdict
        buckets = defaultdict(list)
        for fn in functions:
            buckets[fn.get("node_id", "")].append(fn)

        grouped = []
        for node_id, fns in buckets.items():
            if len(fns) <= 1:
                grouped.extend([[fn] for fn in fns])
                continue

            desc_list = [
                (i, f.get("function_detail", {}).get("description", ""))
                for i, f in enumerate(fns)
            ]
            desc_text = "\n".join(
                f"  [{i}] {desc}" for i, desc in desc_list
            )

            group_prompt = (
                "你是 FMEA 功能语义分析专家。以下是一个节点上的所有功能描述。\n"
                "请判断哪些功能本质上描述同一件事（相同语义），应合并分析。\n"
                "同一件事的定义：功能描述的核心动词和对象相同，仅表述方式不同。\n\n"
                f"{desc_text}\n\n"
                "输出纯 JSON 数组，每个元素是一个分组，包含要合并的功能索引数组。\n"
                "例如：[[0, 2], [1], [3, 4]] 表示索引0和2合并，1单独，3和4合并。\n"
                "未明确属于同一组的功能保持独立。"
            )
            try:
                result = await llm.call_complete(
                    system_prompt="按语义分组合并功能描述。",
                    user_message=group_prompt,
                    force_json=True,
                )
                data = llm.extract_json(result)
                groups_raw = data if isinstance(data, list) else data.get("groups", [])
                # 展平：每组内的所有 function_detail 作为一个分析目标
                for grp in groups_raw:
                    merged_targets = []
                    for idx in grp:
                        if isinstance(idx, int) and 0 <= idx < len(fns):
                            merged_targets.append(fns[idx])
                    if merged_targets:
                        grouped.append(merged_targets)

                # 补齐未被任何分组包含的功能（LLM 可能漏掉某些索引）
                assigned = set()
                for grp in groups_raw:
                    for idx in grp:
                        if isinstance(idx, int) and 0 <= idx < len(fns):
                            assigned.add(idx)
                for i, fn in enumerate(fns):
                    if i not in assigned:
                        grouped.append([fn])
            except Exception:
                # LLM 分组失败时退回单条处理
                grouped.extend([[fn] for fn in fns])

        return grouped

    async def analyze(self, ctx) -> dict:
        """执行失效分析（与 dispatcher 接口对齐）

        ctx.input_data = {
            "nodes": [...],           # 分配给本 Agent 的节点列表
            "functions": [...],       # 对应节点的功能分析（flat 列表）
            "func_map": {...},        # node_id → FunctionAnalysisItem
            "weknora_specs": {...},   # 节点量化参数检索结果
            "kb_id": "...",           # 项目知识库 ID
            "project_id": "...",
        }
        """
        nodes = ctx.input_data.get("nodes", [])
        functions = ctx.input_data.get("functions", [])
        func_map = ctx.input_data.get("func_map", {})
        weknora_specs = ctx.input_data.get("weknora_specs", {})
        kb_id = ctx.input_data.get("kb_id", "")
        project_id = ctx.input_data.get("project_id", "")
        # v8.1: 从 DB 结构树基线加载的最新工程师指派（与功能分析 main.py:592-605 逻辑一致）
        node_assignee_map = ctx.input_data.get("node_assignee_map", {})

        if not functions:
            return {"items": [], "agent": self.name}

        from engine.llm_client import get_llm_client
        llm = self.llm_client or get_llm_client()
        from engine.concurrent import get_concurrent_engine
        engine = get_concurrent_engine()
        from engine.prompts import FAILURE_ANALYSIS_V4_SYSTEM

        # ── 预填结构骨架：对每个 function_detail，从结构树 + 功能分析预填全部结构/功能字段 ──
        # (node_id, fn_detail) → skeleton dict，LLM 只负责填充失效列
        from engine.tools import build_node_context

        # 建立 node_id → node 索引
        node_idx: Dict[str, dict] = {n.get("id", ""): n for n in nodes}
        # 建立 function_id → 功能项的完整 dict（含 child_node_id, upper_function, lower_function 等）
        fn_detail_all: Dict[str, dict] = {}
        # 建立 (node_id, function_id) → assignee 索引
        assignee_all: Dict[str, dict] = {}
        for fn_entry in functions:
            nid = fn_entry.get("node_id", "")
            fn_d = fn_entry.get("function_detail", {})
            if not fn_d:
                continue
            fid = fn_d.get("function_id", "")
            if fid:
                fn_detail_all[fid] = fn_d
                fi = func_map.get(nid)
                # v8.1: 四级 fallback — 与功能分析逻辑一致：
                # 1. DB 最新指派（node_assignee_map）— 与 main.py:592-605 相同
                # 2. FunctionAnalysisItem.assignee_user_id（func_map）
                # 3. 结构树节点.assignee_user_id（node_idx）
                # 4. -1
                _uid = -1
                _uname = ""
                _map = node_assignee_map.get(nid, {})
                if _map:
                    _uid = _map.get("assignee_user_id", -1) or -1
                    _uname = _map.get("assignee_name", "") or ""
                if _uid == -1 and fi:
                    _uid = getattr(fi, "assignee_user_id", -1) or -1
                    _uname = getattr(fi, "assignee_name", "") or ""
                if _uid == -1:
                    _tn = node_idx.get(nid, {})
                    _uid = _tn.get("assignee_user_id", -1) or -1
                    _uname = _tn.get("assignee_name", "") or ""
                assignee_all[f"{nid}|{fid}"] = {
                    "assignee_user_id": _uid,
                    "assignee_name": _uname,
                }
                if nid not in assignee_all:
                    assignee_all[nid] = {
                        "assignee_user_id": _uid,
                        "assignee_name": _uname,
                    }

        def _make_skeleton(node_id: str, fn_detail: dict) -> dict:
            n = node_idx.get(node_id, {})
            child_id = fn_detail.get("child_node_id", "") or ""
            child_name = ""
            if child_id:
                cn = node_idx.get(child_id, {})
                child_name = cn.get("name", "")
            parent_id = n.get("parent_id", "")
            parent_name = ""
            if parent_id:
                pn = node_idx.get(parent_id, {})
                parent_name = pn.get("name", "") if pn else ""
            fid = fn_detail.get("function_id", "")
            ainfo = assignee_all.get(f"{node_id}|{fid}") or assignee_all.get(node_id, {})
            return {
                "node_id": node_id,
                "node_name": n.get("name", ""),
                "child_node_id": child_id,
                "child_node_name": child_name,
                "higher_level_name": parent_name,
                "function_id": fid,
                "function_description": fn_detail.get("description", ""),
                "upper_function": fn_detail.get("upper_function", "") or "",
                "lower_function": fn_detail.get("lower_function", "") or "",
                "assignee_user_id": ainfo.get("assignee_user_id", -1),
                "assignee_name": ainfo.get("assignee_name", ""),
            }

        # 按分组构建目标：grouped_functions 返回 List[List[fn_entry]]
        grouped_functions = await self._group_functions_by_semantics(functions, llm)
        targets = []
        for grp in grouped_functions:
            if not grp:
                continue
            node_id = grp[0].get("node_id", "")
            node = node_idx.get(node_id)
            if not node:
                continue
            fn_details_in_grp = [fn.get("function_detail", {}) for fn in grp if fn.get("function_detail")]
            if not fn_details_in_grp:
                continue
            skeleton = _make_skeleton(node_id, fn_details_in_grp[0])
            if len(fn_details_in_grp) > 1:
                combined_desc = "；".join(
                    fd.get("description", "") for fd in fn_details_in_grp if fd.get("description")
                )
                skeleton["function_description"] = combined_desc
                skeleton["function_id"] = "|".join(
                    fd.get("function_id", "") for fd in fn_details_in_grp if fd.get("function_id")
                )
            targets.append((node, fn_details_in_grp, skeleton))

        # Tool 定义（LLM 只输出失效列，不再输出结构/功能字段）
        tools = [{
            "type": "function",
            "function": {
                "name": "analyze_failure",
                "description": "分析节点功能的潜在失效模式",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "failureModes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "failureEffect": {"type": "string"},
                                    "severity": {"type": "integer", "minimum": 1, "maximum": 10},
                                    "severityReason": {"type": "string"},
                                    "failureMode": {"type": "string"},
                                    "failureCause": {"type": "string"},
                                    "failureNetworkType": {
                                        "type": "string",
                                        "enum": ["安全功能失效", "功能丧失传导", "功能降级传导",
                                                  "冗余失效", "接口失效传导", "一般失效"],
                                    },
                                    "regulationRefs": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "引用的法规条款编号列表，无关联法规则为空数组",
                                    },
                                },
                                "required": ["failureEffect", "severity", "severityReason", "failureMode"],
                            },
                        }
                    },
                    "required": ["failureModes"],
                },
            }
        }]

        # ── 并发执行 ──
        grouped_functions = await self._group_functions_by_semantics(functions, llm)

        # ── 并发执行 ──
        async def analyze_one(target) -> List[dict]:
            node, fn_details, skeleton = target
            node_name = node.get("name", "")

            context = build_node_context(node, nodes)

            if len(fn_details) > 1:
                descs = "\n".join(
                    f"  - [{fd.get('function_category','?')}] {fd.get('description','')} (id={fd.get('function_id','')})"
                    for fd in fn_details
                )
                user_msg_parts = [
                    "分析以下节点上多个合并功能的潜在失效模式：",
                    f"节点: {node_name} ({node.get('level','')})",
                    f"层级上下文: {context}",
                    "已合并分析的功能：",
                    descs,
                ]
            else:
                fn_detail = fn_details[0]
                user_msg_parts = [
                    "分析以下节点功能的潜在失效模式：",
                    f"节点: {node_name} ({node.get('level','')})",
                    f"层级上下文: {context}",
                    f"功能 [{fn_detail.get('function_class','?')}/{fn_detail.get('function_category','?')}]: {fn_detail.get('description','')}",
                ]

            spec_text = weknora_specs.get(node.get("id", ""), "")
            if spec_text:
                user_msg_parts.append(f"\n参考规格参数:\n{spec_text[:1000]}")
                user_msg_parts.append(f"\n参考知识库信息:\n{spec_text[:2000]}")

            user_msg = "\n".join(p for p in user_msg_parts if p)

            try:
                result = await llm.safe_call(
                    system_prompt=FAILURE_ANALYSIS_V4_SYSTEM,
                    user_message=user_msg,
                    tools=tools,
                    probe_key="failureModes",
                )
                data = llm.extract_json(result)
                modes = data.get("failureModes", [])
                merged = []
                for m in modes:
                    merged.append({
                        **skeleton,
                        "failure_effect": m.get("failureEffect", ""),
                        "severity": int(m.get("severity", 4)),
                        "severity_reason": m.get("severityReason", ""),
                        "failure_mode": m.get("failureMode", ""),
                        "failure_cause": m.get("failureCause", ""),
                        "failure_network_type": m.get("failureNetworkType", "一般失效"),
                        "regulationRefs": m.get("regulationRefs", []),
                    })
                return merged
            except Exception as e:
                logger.warning(f"[FailureStepAgent] 分析失败 {node_name}/{skeleton.get('function_id','')}: {e}")
                return [{
                    **skeleton,
                    "failure_effect": f"{node_name}的[{fn_details[0].get('description','')[:30]}]失效",
                    "severity": 4,
                    "severity_reason": "默认严重度（LLM 分析失败）",
                    "failure_mode": "功能异常",
                    "failure_cause": "设计缺陷",
                    "failure_network_type": "一般失效",
                }]

        results = await engine.process_batch(targets, analyze_one)
        all_modes = []
        for mode_list in results:
            all_modes.extend(mode_list)

        _console_log(self.display_name, f"生成 {len(all_modes)} 条失效链")
        return {"items": all_modes, "agent": self.name}


# ════════════════════════════════════════════════════════════════
# FailureMultiAgentRunner
# ════════════════════════════════════════════════════════════════

class FailureMultiAgentRunner:
    """失效分析多 Agent Runner

    完整流程：
    1. 启动横幅 + 打印多 Agent 配置
    2. 从 func_analysis 提取功能列表
    3. LLM 按能力描述路由节点到专业 Agent（P0 改进）
    4. 法规 Agent 独立并行检索（提供法规上下文）
    5. WeKnora 双库联合检索（项目 KB + Agent 专属 KB）
    6. 并发跑专业 Agent：LLM 分析每条功能的失效链（P2 功能合并降本）
    7. 系统集成专家汇总：去重合并 + V4 校验（P1 LLM 审查）
    8. 跨 Agent 失效传导链分析（P0 新增 Step 5.5）
    9. 高风险主题传导链路专项复核（P1 深化）
    10. 组装 FailureAnalysis + 写入 DB
    """

    def __init__(self, llm_client=None, data_dir: str = "data/projects"):
        self.llm_client = llm_client
        self.data_dir = Path(data_dir)
        self._dispatcher = None
        self._weknora = None
        self._agent_events: List[dict] = []
        self._high_risk_themes: List[dict] = []
        self._node_assignee_map: Dict[str, dict] = {}

        # 可配置阈值（P1/P2）
        self.high_severity_threshold = DEFAULT_HIGH_SEVERITY_THRESHOLD
        self.routing_distribution_threshold = DEFAULT_ROUTING_DISTRIBUTION_THRESHOLD
        self.transmission_chain_min_length = DEFAULT_TRANSMISSION_CHAIN_MIN_LENGTH

    # ════════════════════════════════════════════════════════════
    # 对外主入口
    # ════════════════════════════════════════════════════════════

    async def run(
        self,
        tree: StructureTree,
        func_analysis: FunctionAnalysis,
        project_id: str = "",
        progress_callback=None,
        high_risk_themes: Optional[List[dict]] = None,
    ) -> FailureAnalysis:
        """执行多 Agent 失效分析

        Args:
            tree: 结构树
            func_analysis: 功能分析结果
            project_id: 项目 ID
            progress_callback: SSE 进度回调
            high_risk_themes: 高风险主题列表（如热失控/高压绝缘等）

        Returns:
            FailureAnalysis: 完整失效分析
        """
        self._high_risk_themes = high_risk_themes or []
        t0 = time.time()

        total_fn = sum(len(it.functions) for it in func_analysis.items)
        _console_banner(" 失效分析 — 启动（逐功能分析模式）", "=")
        _console_log("INFO", f"项目: {project_id or '(无)'}  节点数: {len(tree.nodes)}  功能数: {total_fn}")
        logger.info(f"[FailureMultiAgentRunner] 启动: pid={project_id} nodes={len(tree.nodes)} functions={total_fn}")

        # ── Step 1: 加载工程师指派 ──
        if not hasattr(self, '_node_assignee_map') or not self._node_assignee_map:
            self._node_assignee_map = {}
            try:
                _pid_int = int(project_id) if project_id and project_id.isdigit() else None
                if _pid_int:
                    from engine.tools.fmea_helpers import get_baseline
                    _db_data = get_baseline(_pid_int, "structure")
                    if _db_data and isinstance(_db_data, dict):
                        for _n in _db_data.get("nodes", []):
                            _nid = _n.get("id")
                            if _nid:
                                self._node_assignee_map[_nid] = dict(
                                    assignee_user_id=_n.get("assignee_user_id"),
                                    assignee_name=_n.get("assignee_name", ""),
                                )
                logger.info(f"[FailureMultiAgentRunner] 已加载 {len(self._node_assignee_map)} 个节点的工程师指派")
            except Exception:
                self._node_assignee_map = {}
                logger.warning(f"[FailureMultiAgentRunner] 工程师指派加载失败，全部为 -1")

        await self._safe_progress(progress_callback, "失效分析", 0.03, "正在检索 KB 参数...")

        # ── Step 2: WeKnora 深度参数检索（按功能所在的节点去重）──
        kb_id = self._resolve_kb_id(project_id)
        weknora_specs = {}
        if kb_id and self._get_weknora():
            wk = self._get_weknora()
            seen_node_ids = set()
            node_map = {n.id: n for n in tree.nodes}
            for item in func_analysis.items:
                nid = item.node_id
                if nid in seen_node_ids or not nid:
                    continue
                seen_node_ids.add(nid)
                n = node_map.get(nid)
                if n and hasattr(n, 'name'):
                    try:
                        result = await wk.spec_deep_search(kb_id, n.name)
                        if isinstance(result, str):
                            weknora_specs[nid] = result
                    except Exception:
                        pass
            _console_log("WeKnora", f"深度参数检索: {len(weknora_specs)}/{len(seen_node_ids)} 个节点命中")

        # ── Step 3: 构建分析任务 ──
        await self._safe_progress(progress_callback, "失效分析", 0.08, f"正在初始化 {total_fn} 个功能的分析任务...")

        tasks: List[dict] = []
        all_nodes_list = [n.model_dump() if hasattr(n, 'model_dump') else dict(n) for n in tree.nodes]
        node_map_dict = {n.get("id", ""): n for n in all_nodes_list}
        func_map = {item.node_id: item for item in func_analysis.items}
        skipped_system = 0

        for item in func_analysis.items:
            node = node_map_dict.get(item.node_id, {})
            if not node:
                continue
            # 与 function_multi_agent_runner.py _post_process_items 一致：
            # 系统级节点不参与失效分析（结构树顶层仅做组织，无实际失效对象）
            node_level = node.get("level", "")
            if node_level == "系统":
                skipped_system += len(item.functions)
                continue
            for fn in item.functions:
                tasks.append({
                    "node_id": item.node_id,
                    "node_name": item.node_name,
                    "function_id": fn.function_id,
                    "child_node_id": fn.child_node_id,
                    "func_description": fn.description,
                    "func_category": fn.function_category,
                    "func_class": fn.function_class,
                    "upper_function": (fn.upper_function or "").strip() or (item.upper_level_function or ""),
                    "lower_function": (fn.lower_function or "").strip() or (item.lower_level_function or ""),
                    "flow_type": fn.flow_type,
                    "weknora_spec": weknora_specs.get(item.node_id, ""),
                    "node_level": node_level,
                })

        logger.info(f"[FailureMultiAgentRunner] 任务数: {len(tasks)} (跳过系统级: {skipped_system}, 功能分析总数={total_fn})")
        _console_log("INFO", f"任务数: {len(tasks)} (跳过系统级 {skipped_system} 条, 总功能数 {total_fn})")

        # ── Step 4: 并发分析 ──
        await self._safe_progress(progress_callback, "失效分析", 0.1, f"并发分析 {len(tasks)} 条功能...")

        # 构建所有节点的结构树上下文（每个 node 只算一次）
        node_contexts = {}
        for n in all_nodes_list:
            nid = n.get("id", "")
            if nid and nid not in node_contexts:
                from engine.tools import build_node_context
                node_contexts[nid] = build_node_context(n, all_nodes_list)

        all_modes: List[FailureMode] = []
        completed = 0
        failed_count = 0

        from engine.concurrent import get_concurrent_engine
        engine = get_concurrent_engine()

        async def _analyze_one(task: dict) -> List[FailureMode]:
            nonlocal completed, failed_count
            try:
                modes = await self._analyze_single_function(task, node_contexts)
                completed += 1
                if progress_callback and completed % 10 == 0:
                    await self._safe_progress(progress_callback, "失效分析",
                        0.1 + 0.80 * completed / len(tasks),
                        f"已完成 {completed}/{len(tasks)}...")
                return modes
            except Exception as e:
                failed_count += 1
                logger.warning(f"[FailureMultiAgentRunner] 分析失败 {task.get('function_id','')}: {e}")
                completed += 1
                return self._make_fallback_modes(task)

        results = await engine.process_batch(tasks, _analyze_one)
        for r in results:
            all_modes.extend(r)

        _console_log("INFO", f"失效分析完成: {len(all_modes)} 条失效链, 失败={failed_count} 条功能")

        # ── Step 5: 注入工程师指派 ──
        node_assignee_map = getattr(self, "_node_assignee_map", {}) or {}
        injected = 0
        for fm in all_modes:
            ainfo = node_assignee_map.get(fm.node_id, {}) if node_assignee_map else {}
            try:
                uid = int(ainfo.get("assignee_user_id", -1)) if ainfo else -1
            except (ValueError, TypeError):
                uid = -1
            if uid and uid > 0:
                injected += 1
            else:
                uid = -1
            fm.assignee_user_id = uid
            fm.assignee_name = (ainfo.get("assignee_name") or "") if ainfo else ""
        logger.info(f"[FailureMultiAgentRunner] 注入工程师指派: {injected} 条有指派, {len(all_modes) - injected} 条未指派")

        analysis = FailureAnalysis(items=all_modes)

        elapsed = time.time() - t0
        stats = analysis.get_summary_stats()
        _console_banner(
            f" 失效分析完成  |  "
            f"{stats['total']} 条失效链  /  "
            f"S>=7: {stats['high_severity_count']}  /  "
            f"安全: {stats['safety_failure_count']}  /  "
            f"耗时 {elapsed:.1f}s",
            "="
        )

        await self._safe_progress(progress_callback, "失效分析", 1.0,
            f"完成: {stats['total']} 条失效链 / S>=7: {stats['high_severity_count']}")

        return analysis

    # ════════════════════════════════════════════════════════════
    # 逐功能分析方法
    # ════════════════════════════════════════════════════════════

    async def _analyze_single_function(
        self, task: dict, node_contexts: Dict[str, str]
    ) -> List[FailureMode]:
        """分析单条功能：LLM 产出 FE/S/FM/FC，基础字段写时快照"""
        from engine.llm_client import get_llm_client
        from engine.prompts import FAILURE_ANALYSIS_V4_SYSTEM

        llm = get_llm_client()

        node_id = task["node_id"]
        node_name = task["node_name"]
        fn_id = task["function_id"]
        child_id = task["child_node_id"]
        context = node_contexts.get(node_id, "")

        # ── 构造 LLM 输入 ──
        user_msg_parts = [
            f"分析以下功能的潜在失效模式：",
            f"组件: {node_name} ({task.get('node_level', '')})",
            f"层级上下文: {context}",
            f"功能 [{task.get('func_category', '?')}/{task.get('func_class', '?')}]: {task['func_description']}",
            f"功能ID: {fn_id}",
            f"上一较高级别功能: {task['upper_function'] or '(无)'}",
            f"下一较低级别功能: {task['lower_function'] or '(无)'}",
            f"功能流类型: {task.get('flow_type', '无')}",
        ]
        if task.get("weknora_spec"):
            user_msg_parts.append(f"\n参考知识库信息：\n{task['weknora_spec'][:2000]}")
        user_msg = "\n".join(p for p in user_msg_parts if p)

        tools = [{
            "type": "function",
            "function": {
                "name": "analyze_failure",
                "description": "分析该功能的失效链 (FE → S → FM → FC)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "failureModes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "failureEffect": {"type": "string"},
                                    "severity": {"type": "integer", "minimum": 1, "maximum": 10},
                                    "severityReason": {"type": "string"},
                                    "failureMode": {"type": "string"},
                                    "failureCause": {"type": "string"},
                                    "failureNetworkType": {"type": "string", "enum": [
                                        "安全功能失效","功能丧失传导","功能降级传导",
                                        "冗余失效","接口失效传导","一般失效",
                                    ]},
                                },
                                "required": ["failureEffect", "severity", "severityReason", "failureMode"],
                            },
                        }
                    },
                    "required": ["failureModes"],
                },
            }
        }]

        try:
            result = await llm.safe_call(
                system_prompt=FAILURE_ANALYSIS_V4_SYSTEM,
                user_message=user_msg,
                tools=tools,
                probe_key="failureModes",
            )
            data = llm.extract_json(result) if hasattr(llm, 'extract_json') else json.loads(result)
            modes_raw = data.get("failureModes", [])
        except Exception as e:
            logger.warning(f"[FailureMultiAgentRunner] LLM 调用失败 {fn_id}: {e}")
            modes_raw = []

        if not modes_raw:
            return self._make_fallback_modes(task)

        # ── 组装 FailureMode：基础字段写时快照 + LLM 推理字段 ──
        modes = []
        for i, m in enumerate(modes_raw):
            sev = max(1, min(10, int(m.get("severity", 4))))
            sev_reason = m.get("severityReason", "") or self._infer_severity_reason(sev, m.get("failureEffect", ""))
            net_type = m.get("failureNetworkType", "") or self._infer_failure_network_type(sev, m.get("failureEffect", ""))

            modes.append(FailureMode(
                id=f"fm-{fn_id}-{i+1:02d}",
                node_id=node_id,
                node_name=node_name,
                child_node_id=child_id,
                function_id=fn_id,
                # ↓↓↓ 写时快照：从 FunctionDetail 直接复制 ↓↓↓
                function_description=task["func_description"],
                upper_function=task["upper_function"],
                lower_function=task["lower_function"],
                # ↓↓↓ LLM 推理结果 ↓↓↓
                failure_effect=m.get("failureEffect", ""),
                severity=sev,
                severity_reason=sev_reason,
                failure_mode=m.get("failureMode", ""),
                failure_cause=m.get("failureCause", ""),
                failure_network_type=net_type,
            ))
        return modes

    def _make_fallback_modes(self, task: dict) -> List[FailureMode]:
        """当 LLM 完全失败时，用 DFMEA 通用模式生成至少 1 条失效链"""
        node_id = task["node_id"]
        node_name = task["node_name"]
        fn_id = task["function_id"]
        child_id = task["child_node_id"]
        desc = task["func_description"]
        level = task.get("node_level", "零件")
        base_sev = {"系统": 7, "子系统": 6, "组件": 5, "零件": 4}.get(level, 5)

        results = []
        for i, (label, sev_offset) in enumerate([("功能丧失", 0), ("功能降级", -1)]):
            sev = max(1, min(10, base_sev + sev_offset))
            fm_text = (
                f"{desc[:30]} — 功能完全丧失，无法输出预期结果"
                if label == "功能丧失"
                else f"{desc[:30]} — 功能性能低于设计指标"
            )
            fc_text = (
                "执行该功能所需的输入/能量/信号缺失或中断"
                if label == "功能丧失"
                else "执行条件偏离设计范围，如温度/压力/负载超出额定值"
            )
            results.append(FailureMode(
                id=f"fm-{fn_id}-{i+1:02d}-fb",
                node_id=node_id,
                node_name=node_name,
                child_node_id=child_id,
                function_id=fn_id,
                function_description=desc,
                upper_function=task["upper_function"],
                lower_function=task["lower_function"],
                failure_effect=(
                    f"{'功能丧失' if label == '功能丧失' else '功能降级'}: "
                    f"{node_name}的「{desc[:30]}」无法正常执行"
                ),
                severity=sev,
                severity_reason=(
                    f"S={sev}: 基于DFMEA方法论，{level}级"
                    f"{'功能丧失' if label == '功能丧失' else '功能降级'}的默认严重度范围。"
                ),
                failure_mode=fm_text,
                failure_cause=fc_text,
                failure_network_type=("功能丧失传导" if label == "功能丧失" else "功能降级传导"),
            ))
        return results

    @staticmethod
    def _infer_severity_reason(severity: int, failure_effect: str) -> str:
        if severity >= 9:
            return f"S={severity}: 失效涉及安全/法规风险"
        if severity == 8:
            return f"S={severity}: 主要功能丧失"
        if severity == 7:
            return f"S={severity}: 主要功能降级"
        if severity >= 5:
            return f"S={severity}: 客户明显感知的功能影响"
        return f"S={severity}: 影响轻微或无影响"

    @staticmethod
    def _infer_failure_network_type(severity: int, failure_effect: str) -> str:
        if severity >= 9:
            return "安全功能失效"
        if severity >= 8:
            return "功能丧失传导"
        if severity >= 6:
            return "功能降级传导"
        return "一般失效"

    # ════════════════════════════════════════════════════════════
    # 进度回调
    # ════════════════════════════════════════════════════════════

    async def _safe_progress(self, callback, *args) -> None:
        if not callback:
            return
        phase = args[0] if len(args) > 0 else ""
        ratio = args[1] if len(args) > 1 else 0.0
        msg = args[2] if len(args) > 2 else ""
        extra = args[3] if len(args) > 3 else None
        try:
            import inspect
            if inspect.iscoroutinefunction(callback):
                await callback(phase, ratio, msg, extra)
            else:
                callback(phase, ratio, msg, extra)
        except Exception as e:
            logger.debug(f"[multi-agent-failure] progress_callback 异常: {e}")

    # ════════════════════════════════════════════════════════════
    # 初始化
    # ════════════════════════════════════════════════════════════

    def _build_dispatcher(self):
        from orchestrator.multi_agent_builder import MultiAgentBuilder
        if self.llm_client is None:
            from engine.llm_client import get_llm_client
            self.llm_client = get_llm_client()
        builder = MultiAgentBuilder(llm_client=self.llm_client, data_dir=str(self.data_dir))
        dispatcher = builder.build_from_hev_preset()
        self._dispatcher = dispatcher
        _console_log("MultiAgent", f"已构建 {len(dispatcher.list_agents())} 个 Agent")
        return dispatcher

    def _inject_failure_step_agents(self, dispatcher, tree: StructureTree) -> None:
        """把通用 Agent 替换为 step3 专用的 FailureStepAgent"""
        replaced = 0
        for agent_name, agent in list(dispatcher._agents.items()):
            if agent_name in ("system_integration", "regulation"):
                continue
            from models.agent_config import AgentRole
            if agent.role != AgentRole.PROFESSIONAL:
                continue
            fail_agent = FailureStepAgent(
                config=agent.config,
                llm_client=self.llm_client,
                weknora=self._get_weknora(),
            )
            dispatcher._agents[agent_name] = fail_agent
            replaced += 1
        _console_log("MultiAgent", f"已注入 {replaced} 个 step3 专用 FailureStepAgent")

    def _get_weknora(self):
        if self._weknora is None:
            from knowledge.weknora_client import get_weknora_client
            self._weknora = get_weknora_client()
        return self._weknora

    def _resolve_kb_id(self, project_id: str) -> str:
        if not project_id:
            return ""
        try:
            import os
            import psycopg2
            host = os.getenv("URL_AGENT_DB_HOST", "10.180.17.11")
            port = int(os.getenv("URL_AGENT_DB_PORT", "5433"))
            dbname = os.getenv("URL_AGENT_DB_NAME", "fmea")
            user = os.getenv("URL_AGENT_DB_USER", "postgres")
            password = os.getenv("URL_AGENT_DB_PASSWORD", "postgres123!@#")
            conn = psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password)
            cur = conn.cursor()
            try:
                pid_int = int(project_id)
                cur.execute("SELECT weknora_kb_id FROM projects WHERE id = %s LIMIT 1", (pid_int,))
                row = cur.fetchone()
                return (row[0] or "").strip() if row else ""
            except ValueError:
                cur.execute("SELECT weknora_kb_id FROM projects WHERE name = %s LIMIT 1", (project_id,))
                row = cur.fetchone()
                return (row[0] or "").strip() if row else ""
            finally:
                cur.close(); conn.close()
        except Exception as e:
            logger.warning(f"[FailureMultiAgentRunner] 查询 kb_id 失败: {e}")
            return ""

    # ════════════════════════════════════════════════════════════
    # LLM 路由（P0 修复）
    # ════════════════════════════════════════════════════════════

    async def _route_by_llm(self, nodes: list, dispatcher) -> Dict[str, list]:
        """LLM 按完整能力描述把节点路由到各专业 Agent

        P0 改进：
        1. 路由 prompt 带上所有 Agent 的完整能力描述（领域 + 职责 + 边界）
        2. 路由后加分布校验：某 Agent 占比超阈值 → 对该 Agent 的节点做二次 LLM 确认
        3. 兜底保留 hash 轮询
        """
        from engine.llm_client import get_llm_client
        llm = self.llm_client or get_llm_client()

        agents = dispatcher.list_agents()
        professional_agents = [
            a for a in agents
            if a.get("name", "") not in ("system_integration", "regulation")
            and a.get("role") is not None
        ]

        # 完整能力描述（领域 + 职责 + 边界）
        agent_descriptions = "\n\n".join(
            _build_agent_capability_description(a) for a in professional_agents
        )

        routing: Dict[str, list] = {a["name"]: [] for a in professional_agents}
        agent_names = [a["name"] for a in professional_agents]

        batch_size = 50
        for batch_start in range(0, len(nodes), batch_size):
            batch = nodes[batch_start:batch_start + batch_size]
            node_list_str = "\n".join(
                f"  id={n.get('id','')}, name={n.get('name','')}, level={n.get('level','')}"
                for n in batch
            )

            prompt = f"""你是一个 FMEA 多 Agent 系统的路由法官。
请基于每个 Agent 的完整能力描述，将以下节点分配到最合适的专业 Agent。

可用专业 Agent（含领域、职责、边界）:
{agent_descriptions}

节点列表:
{node_list_str}

分配原则：
- 节点名和层级是最重要的判断依据
- 选择与该节点领域最匹配的 Agent，职责重叠时优先职责匹配度高的 Agent
- 边界约束中明确提到的领域应避免分配给该 Agent

输出格式（严格 JSON）:
{{"routing": {{"node_id": "agent_name", ...}}}}
"""
            try:
                result = await llm.safe_call(
                    system_prompt="你是 FMEA 路由专家，根据节点名称、层级和 Agent 能力描述分配到最合适的专业 Agent。",
                    user_message=prompt,
                    tools=[{
                        "type": "function",
                        "function": {
                            "name": "route_nodes",
                            "description": "路由节点到专业 Agent",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "routing": {
                                        "type": "object",
                                        "additionalProperties": {"type": "string"},
                                    }
                                },
                                "required": ["routing"],
                            },
                        }
                    }],
                    probe_key="routing",
                )
                data = llm.extract_json(result)
                batch_routing = data.get("routing", {})
            except Exception as e:
                logger.warning(f"[route] LLM 路由失败 batch {batch_start}: {e}")
                batch_routing = {}

            for n in batch:
                nid = n.get("id", "")
                assigned = batch_routing.get(nid, "")
                if assigned and assigned in routing:
                    routing[assigned].append(n)
                else:
                    idx = hash(nid or "") % len(agent_names)
                    routing[agent_names[idx]].append(n)

        # ── P0 分布校验：某 Agent 占比超阈值 → 二次 LLM 确认 ──
        routing = await self._validate_routing_distribution(routing, nodes, agent_names, llm)

        return routing

    async def _validate_routing_distribution(
        self,
        routing: Dict[str, list],
        nodes: list,
        agent_names: list,
        llm,
    ) -> Dict[str, list]:
        """路由分布校验：某 Agent 占比超阈值 → 对该 Agent 的节点做二次 LLM 确认"""
        total = len(nodes)
        if total == 0:
            return routing

        threshold = self.routing_distribution_threshold

        for agent_name, assigned_nodes in list(routing.items()):
            ratio = len(assigned_nodes) / total
            if ratio <= threshold or len(assigned_nodes) <= 2:
                continue

            _console_log("Route", f"分布校验: {agent_name} 占比 {ratio:.0%} ≥ {threshold:.0%}，执行二次确认")

            # 对该 Agent 的节点，请 LLM 重新检查是否有更合适的 Agent
            node_list_str = "\n".join(
                f"  id={n.get('id','')}, name={n.get('name','')}, level={n.get('level','')}"
                for n in assigned_nodes
            )

            recheck_prompt = (
                f"以下节点当前全部分配给「{agent_name}」，请逐一检查是否有更适合分配给其他 Agent 的节点。\n"
                f"如果你认为某个节点分配给其他 Agent 更合理，请指出。\n\n"
                f"节点列表:\n{node_list_str}\n\n"
                "输出 JSON (只输出确实分配不当的纠正): "
                '{{"corrections": [{{"node_id": "...", "reassign_to": "agent_name", "reason": "..."}}]}}'
            )

            try:
                result = await llm.safe_call(
                    system_prompt="你是 FMEA 路由专家，检查节点分配是否合理。",
                    user_message=recheck_prompt,
                    tools=[{
                        "type": "function",
                        "function": {
                            "name": "reroute_nodes",
                            "description": "纠正节点分配",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "corrections": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "node_id": {"type": "string"},
                                                "reassign_to": {"type": "string"},
                                                "reason": {"type": "string"},
                                            },
                                            "required": ["node_id", "reassign_to", "reason"],
                                        },
                                    }
                                },
                                "required": ["corrections"],
                            },
                        }
                    }],
                    probe_key="corrections",
                )
                data = llm.extract_json(result)
                corrections = data.get("corrections", [])
                for corr in corrections:
                    nid = corr.get("node_id", "")
                    target = corr.get("reassign_to", "")
                    if nid and target in routing and target != agent_name:
                        to_move = [n for n in assigned_nodes if n.get("id") == nid]
                        for n in to_move:
                            routing[agent_name].remove(n)
                            routing[target].append(n)
                            _console_log("Route", f"  二次确认: {nid} → {target}")
            except Exception:
                pass

        return routing

    def _log_routing(self, routing: Dict[str, list], total: int) -> None:
        _console_log("Route", f"已分配 {total} 个节点到 {len(routing)} 个 Agent")
        for aname, nodes in routing.items():
            _console_log("Route", f"  {aname}: {len(nodes)} 个节点")

    # ════════════════════════════════════════════════════════════
    # 法规 Agent
    # ════════════════════════════════════════════════════════════

    async def _run_regulation_agent(self, dispatcher, kb_id: str, project_id: str) -> dict:
        """并行执行法规 Agent 检索"""
        reg_agent = dispatcher._agents.get("regulation")
        if not reg_agent:
            return {}
        try:
            from agents.base import AgentContext, AgentOutput
            ctx = AgentContext(
                step=4, step_name="失效分析",
                input_data={"kb_id": kb_id, "project_id": project_id, "regulation_list": []},
                project_config={"weknora_kb_id": kb_id},
            )
            result = await reg_agent.analyze(ctx)
            if result and result.success:
                clauses = result.data.get("document_1", {}).get("mapping_table", [])
                _console_log("RegulationAgent", f"输出 {len(clauses)} 条法规条款")
                return {"clauses": clauses}
        except Exception as e:
            logger.warning(f"[FailureMultiAgentRunner] 法规 Agent 异常: {e}")
        return {}

    # ════════════════════════════════════════════════════════════
    # 专业 Agent 并发执行
    # ════════════════════════════════════════════════════════════

    async def _run_professional_agents(
        self, dispatcher, routing: Dict[str, list],
        agent_functions: Dict[str, list], func_map: dict,
        tree: StructureTree, kb_id: str, project_id: str,
        reg_data: dict, weknora_specs: dict,
    ) -> Dict[str, list]:
        """并发执行所有专业 Agent 的失效分析"""
        from models.agent_config import AgentRole

        async def run_single_agent(agent_name: str) -> Tuple[str, list]:
            agent = dispatcher._agents.get(agent_name)
            if not agent:
                return agent_name, []
            if agent_name not in agent_functions:
                return agent_name, []

            nodes = routing.get(agent_name, [])
            fns = agent_functions[agent_name]

            self._agent_events.append({
                "agent": agent_name,
                "status": "running",
                "progress": 0,
                "message": f"正在分析 {len(fns)} 条功能的失效模式...",
            })

            t_start = time.time()
            _console_log(agent_name, f"开始失效分析: {len(fns)} 条功能")

            try:
                from agents.base import AgentContext
                ctx = AgentContext(
                        step=4, step_name="失效分析",
                        input_data={
                            "nodes": nodes,
                            "functions": fns,
                            "func_map": func_map,
                            "weknora_specs": weknora_specs,
                            "kb_id": kb_id,
                            "project_id": project_id,
                            "regulation": reg_data,
                            "node_assignee_map": getattr(self, '_node_assignee_map', {}),
                        },
                    project_config={"weknora_kb_id": kb_id},
                )
                result = await agent.analyze(ctx)
                items = []
                if isinstance(result, dict):
                    items = result.get("items", [])
                elif hasattr(result, 'data'):
                    items = result.data.get("items", [])

                elapsed = time.time() - t_start
                _console_log(agent_name, f"完成: {len(items)} 条失效链 (耗时 {elapsed:.1f}s)")

                self._agent_events.append({
                    "agent": agent_name,
                    "status": "done",
                    "progress": 1.0,
                    "message": f"生成 {len(items)} 条失效链",
                    "elapsed": round(elapsed, 1),
                })
                return agent_name, items

            except Exception as e:
                elapsed = time.time() - t_start
                logger.warning(f"[FailureMultiAgentRunner] Agent {agent_name} 失败: {e}")
                _console_log(agent_name, f" 失败: {e}")
                self._agent_events.append({
                    "agent": agent_name,
                    "status": "error",
                    "progress": -1,
                    "message": str(e)[:80],
                })
                return agent_name, []

        tasks = [run_single_agent(name) for name in agent_functions.keys()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        per_agent: Dict[str, list] = {}
        for r in results:
            if isinstance(r, tuple):
                name, items = r
                per_agent[name] = items
            elif isinstance(r, Exception):
                logger.warning(f"[FailureMultiAgentRunner] Agent 异常: {r}")

        return per_agent

    # ════════════════════════════════════════════════════════════
    # 系统集成专家汇总（P1 升级）
    # ════════════════════════════════════════════════════════════

    async def _consolidate_items(
        self, per_agent: Dict[str, list], tree: StructureTree,
    ) -> Tuple[List[FailureMode], Optional[dict]]:
        """汇总各 Agent 的失效链，按节点聚合

        Phase 1：机械去重（结构/功能字段已由 FailureStepAgent 从 baseline 预填）
        Phase 2（新增 P1）：高 severity 项 + 跨链项喂给 system_integration 做 LLM 审查
        审查内容：共因失效、severity 一致性、链路逻辑跳跃
        """
        from models.failure import FailureMode
        from engine.llm_client import get_llm_client

        # node_order 用于排序
        node_order = {}
        for i, n in enumerate(tree.nodes):
            nid = n.id if hasattr(n, 'id') else n.get("id", "")
            if nid:
                node_order[nid] = i

        # ── Phase 1: 语义去重（v9 重写） ──
        # 旧实现：机械去重，70% 丢失率（417→127），因为完全相同的 FM 文本只在完全重叠的不同 Agent 中才出现。
        # 新目标：按 (function_id, child_node_id) 分组，同组内用文本相似度 + LLM 语义
        #         判断是否重复，保留不同侧重的失效链。
        from collections import defaultdict
        from difflib import SequenceMatcher

        def _parse_candidate(item: dict):
            """把 LLM 原始 dict 解析为 FailureMode（仅保留 LLM 推理字段）"""
            node_id = item.get("node_id", "")
            func_id = item.get("functionId", item.get("function_id", ""))
            fm_text = item.get("failureMode", "") or ""
            child_node_id = (
                item.get("child_node_id")
                or item.get("childNodeId")
                or ""
            )
            return FailureMode(
                id=item.get("id", f"fm-{node_id}-{hash(f'{node_id}|{func_id}|{fm_text[:50]}') % 10000:04d}"),
                node_id=node_id,
                node_name=item.get("node_name", ""),
                child_node_id=child_node_id,
                function_id=func_id,
                function_description=item.get("function_description", item.get("functionDescription", "")),
                failure_effect=item.get("failure_effect", item.get("failureEffect", "")),
                severity=int(item.get("severity", 4)),
                severity_reason=item.get("severity_reason", item.get("severityReason", "")),
                failure_mode=fm_text,
                failure_cause=item.get("failure_cause", item.get("failureCause", "")),
                failure_network_type=item.get("failure_network_type", item.get("failureNetworkType", "一般失效")),
            )

        # 1) 收集所有候选
        all_candidates = []
        for agent_name, items in per_agent.items():
            for item in items:
                if isinstance(item, dict):
                    all_candidates.append(_parse_candidate(item))

        # 2) 按 (function_id, child_node_id) 分组
        groups: Dict[tuple, list] = defaultdict(list)
        for fm in all_candidates:
            key = (fm.function_id, fm.child_node_id)
            groups[key].append(fm)

        # 3) 同组内去重：先文本相似度快速合并；剩余 > 5 条时再调 LLM 语义去重
        SIM_THRESHOLD = 0.7
        # 上限：单组最多保留 10 条（避免 LLM 过度裁剪，但防止极端情况下产生上千条同组）
        # 上限只对超过阈值的组触发，避免短组被人工裁剪
        MAX_KEEP_PER_GROUP = 10

        def _text_similarity_dedup(items: list) -> list:
            """同组内按 FM 文本相似度合并：相似度 >= 0.85 视为重复，保留 severity 高 + 描述更详细的。
            短文本（<= 25 字）不做相似度比对，避免通用短语（'功能丧失'/'功能降级'）导致误删。
            """
            if len(items) <= 1:
                return items
            # 按 severity 降序、failure_mode 长度降序排
            ordered = sorted(items, key=lambda x: (-x.severity, -len(x.failure_mode or "")))
            keep = []
            for fm in ordered:
                # 关键：空 FM 或短 FM（<= 25 字）的 item 不参与相似度比对
                # 短 FM 通常是通用短语（'功能丧失'/'功能降级'），不应让它们吃掉其他 item
                if not fm.failure_mode or len(fm.failure_mode.strip()) <= 25:
                    keep.append(fm)
                    continue
                is_dup = False
                for kp in keep:
                    if not kp.failure_mode or len(kp.failure_mode.strip()) <= 25:
                        continue
                    ratio = SequenceMatcher(None, fm.failure_mode, kp.failure_mode).ratio()
                    if ratio >= 0.85:  # 提高阈值，避免误删
                        is_dup = True
                        break
                if not is_dup:
                    keep.append(fm)
                if len(keep) >= MAX_KEEP_PER_GROUP:
                    break
            return keep

        async def _llm_semantic_dedup(items: list, group_key: tuple) -> list:
            """同组内 > 6 条时调用 LLM 语义去重，保留语义独立的失效链"""
            if len(items) <= 6:
                return items
            try:
                from engine.llm_client import get_llm_client
                llm = get_llm_client()
                indexed = [
                    {"idx": i, "FE": it.failure_effect[:80], "FM": it.failure_mode[:80],
                     "FC": it.failure_cause[:80], "S": it.severity,
                     "type": it.failure_network_type}
                    for i, it in enumerate(items)
                ]
                user_msg = (
                    f"以下 {len(items)} 条失效链来自同一 (function_id, child_node_id)。\n"
                    f"请判断哪些是语义重复（不同 Agent 描述同一失效），哪些是不同侧重的失效。\n"
                    f"保留语义独立的失效（最多保留 {MAX_KEEP_PER_GROUP} 条）。\n\n"
                    f"输入：{json.dumps(indexed, ensure_ascii=False, indent=2)}\n\n"
                    f"输出 JSON：{{\"keep_indices\": [0, 2, 4], \"reason\": \"...\"}}"
                )
                result = await llm.call_complete(
                    system_prompt="你是 FMEA 失效分析去重专家。只做语义去重，不要修改内容。",
                    user_message=user_msg,
                    force_json=True,
                )
                if not result:
                    return _text_similarity_dedup(items)
                data = llm.extract_json(result) if hasattr(llm, 'extract_json') else json.loads(result)
                keep_indices = data.get("keep_indices", [])
                if not isinstance(keep_indices, list) or not keep_indices:
                    return _text_similarity_dedup(items)
                keep = [items[i] for i in keep_indices if 0 <= i < len(items)]
                return keep if keep else _text_similarity_dedup(items)
            except Exception as e:
                logger.warning(f"[FailureMultiAgentRunner] LLM 语义去重失败: {e}")
                return _text_similarity_dedup(items)

        all_items = []
        for group_key, items in groups.items():
            if len(items) == 1:
                all_items.extend(items)
            elif len(items) <= 5:
                # 小组：直接用文本相似度去重
                all_items.extend(_text_similarity_dedup(items))
            else:
                # 大组：先文本相似度快速合并
                quick = _text_similarity_dedup(items)
                if len(quick) <= 6:
                    all_items.extend(quick)
                else:
                    llm_merged = await _llm_semantic_dedup(quick, group_key)
                    all_items.extend(llm_merged)

        node_order = {n.id if hasattr(n, 'id') else n.get("id", ""): i for i, n in enumerate(tree.nodes)}
        all_items.sort(key=lambda x: (node_order.get(x.node_id, 999), x.function_id))

        _console_log("Consolidate", f"Phase1 语义去重后: {len(all_items)} 条失效链 (去重前: {len(all_candidates)})")

        # ── Phase 2: LLM 审查（P1 新增） ──
        system_review = None
        llm = get_llm_client()

        # 只审查高 severity + 跨链项，控成本
        review_candidates = []
        for fm in all_items:
            if fm.severity >= self.high_severity_threshold:
                review_candidates.append(fm)

        if review_candidates:
            # 构造审查输入：按 Agent 来源分组（从 per_agent 追踪）
            review_text = ""
            for i, fm in enumerate(review_candidates[:30]):  # 最多 30 条
                review_text += (
                    f"[{i}] node={fm.node_name} | func={fm.function_description[:30]} | "
                    f"FE={fm.failure_effect[:40]} | S={fm.severity} | "
                    f"FM={fm.failure_mode[:40]} | FC={fm.failure_cause[:40]} | "
                    f"net_type={fm.failure_network_type}\n"
                )

            review_prompt = (
                "你是 FMEA 系统集成专家。请对以下高严重度(S≥{threshold})失效链做智能审查。\n\n"
                "审查内容（用 LLM 判断，不用正则/关键词）：\n"
                "1. 共因失效：不同节点的失效原因(FC)是否指向同一根源设计缺陷，应合并或联动？\n"
                "2. Severity 一致性：同节点同功能的失效链，severity 是否自洽？\n"
                "3. 链路逻辑跳跃：FE→FM→FC 链是否存在逻辑断层？\n\n"
                "待审查条目（最多 30 条）：\n{items}\n\n"
                "输出 JSON：\n"
                '{{\n'
                '  "common_causes": [{{"cause": "...", "affected_indices": [0,2], "suggestion": "..."}}],\n'
                '  "severity_inconsistencies": [{{"indices": [1,3], "reason": "..."}}],\n'
                '  "logic_gaps": [{{"index": 5, "gap": "..."}}]\n'
                '}}\n'
            ).format(threshold=self.high_severity_threshold, items=review_text)

            try:
                result = await llm.call_complete(
                    system_prompt="FMEA 系统集成专家：审查失效链的一致性和完整性。",
                    user_message=review_prompt,
                    force_json=True,
                )
                data = llm.extract_json(result)
                system_review = {
                    "reviewed_count": len(review_candidates),
                    "common_causes": data.get("common_causes", []),
                    "severity_inconsistencies": data.get("severity_inconsistencies", []),
                    "logic_gaps": data.get("logic_gaps", []),
                }
                _console_log("SystemReview", f"Phase2 审查: {len(review_candidates)} 条高 severity 项")

                # 将审查结果附加到对应 FailureMode 的 metadata 字段（不破坏已有数据）
                review_map = {}
                for cc in system_review.get("common_causes", []):
                    for idx in cc.get("affected_indices", []):
                        if idx < len(review_candidates):
                            rm = review_candidates[idx]
                            review_map.setdefault(rm.id, []).append(f"共因: {cc.get('suggestion','')}")
                for si in system_review.get("severity_inconsistencies", []):
                    for idx in si.get("indices", []):
                        if idx < len(review_candidates):
                            rm = review_candidates[idx]
                            review_map.setdefault(rm.id, []).append(f"Severity不一致: {si.get('reason','')}")
                for lg in system_review.get("logic_gaps", []):
                    idx = lg.get("index", -1)
                    if 0 <= idx < len(review_candidates):
                        rm = review_candidates[idx]
                        review_map.setdefault(rm.id, []).append(f"逻辑跳跃: {lg.get('gap','')}")

                # 将审查标记写入 FailureMode（Optional 字段，不破坏已有数据）
                for fm in all_items:
                    if fm.id in review_map:
                        fm.user_modified = True  # 标记待人工确认
            except Exception as e:
                _console_log("SystemReview", f"Phase2 LLM 审查异常: {e}")

        return all_items, system_review

    # ════════════════════════════════════════════════════════════
    # 跨 Agent 失效传导链分析（P0 Step 5.5）
    # ════════════════════════════════════════════════════════════

    async def _run_failure_transmission_analysis(
        self,
        all_modes: List[FailureMode],
        tree: StructureTree,
    ) -> dict:
        """跨 Agent 失效传导链分析

        1. 把所有 Agent 输出的 failureEffect 和 failureCause 列表喂给 LLM
        2. LLM 判断哪些构成"A 的效应 → B 的原因"传导关系
        3. 按配对建有向图，提取连通链
        4. 对长链（≥3 环节）调 LLM 验证连贯性、补全缺失环节
        5. 未参与任何链的高 severity 项标记"孤立高风险"
        """
        from engine.llm_client import get_llm_client
        llm = self.llm_client or get_llm_client()

        if len(all_modes) < 2:
            return {"transmission_chains": [], "isolated_high_risk": []}

        # 只处理高 severity 项（控成本）
        high_sev_items = [fm for fm in all_modes if fm.severity >= self.high_severity_threshold]

        # Step 5.5-1: 构建 FE/FC 文本列表
        fe_list = []
        fc_list = []
        for i, fm in enumerate(high_sev_items):
            if fm.failure_effect:
                fe_list.append({"index": i, "text": fm.failure_effect, "node": fm.node_name, "func": fm.function_description[:30]})
            if fm.failure_cause:
                fc_list.append({"index": i, "text": fm.failure_cause, "node": fm.node_name, "func": fm.function_description[:30]})

        if not fe_list or not fc_list:
            return {"transmission_chains": [], "isolated_high_risk": []}

        # Step 5.5-2: LLM 判断传导关系
        fe_text = "\n".join(f"  [FE-{e['index']}] ({e['node']}/{e['func']}): {e['text'][:150]}" for e in fe_list[:20])
        fc_text = "\n".join(f"  [FC-{c['index']}] ({c['node']}/{c['func']}): {c['text'][:150]}" for c in fc_list[:20])

        relation_prompt = (
            "你是 DFMEA 失效传导链分析专家。\n"
            "请分析以下失效影响的 FE（失效影响）和 FC（失效原因）列表中，哪些构成"
            "「A 的失效影响(FE) → B 的失效原因(FC)」的传导关系。\n"
            "即：A 的失效影响是 B 的失效原因的上层触发条件。\n\n"
            "FE 列表（失效影响）：\n"
            f"{fe_text}\n\n"
            f"FC 列表（失效原因）：\n"
            f"{fc_text}\n\n"
            "输出 JSON (只输出确实存在传导关系的配对):\n"
            '{{"pairs": [{{"fe_index": 0, "fc_index": 5, "reason": "..."}}]}}'
        )

        pairs = []
        try:
            result = await llm.safe_call(
                system_prompt="分析失效传导关系。",
                user_message=relation_prompt,
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "identify_transmission_pairs",
                        "description": "识别 FE→FC 传导配对",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "pairs": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "fe_index": {"type": "integer"},
                                            "fc_index": {"type": "integer"},
                                            "reason": {"type": "string"},
                                        },
                                        "required": ["fe_index", "fc_index", "reason"],
                                    },
                                }
                            },
                            "required": ["pairs"],
                        },
                    }
                }],
                probe_key="pairs",
            )
            data = llm.extract_json(result)
            pairs = data.get("pairs", [])
            _console_log("Transmission", f"LLM 识别出 {len(pairs)} 个传导关系对")
        except Exception as e:
            logger.warning(f"[Transmission] LLM 传导分析失败: {e}")

        # Step 5.5-3: 按配对建有向图，提取连通链
        graph = {}
        for p in pairs:
            fe_i = p.get("fe_index", -1)
            fc_i = p.get("fc_index", -1)
            if fe_i >= 0 and fc_i >= 0:
                graph.setdefault(fe_i, []).append(fc_i)

        visited = set()
        chains = []

        def dfs(node, path):
            visited.add(node)
            path.append(node)
            if node in graph:
                for neighbor in graph[node]:
                    if neighbor not in visited:
                        dfs(neighbor, path)
            chains.append(list(path))
            path.pop()

        for node in graph:
            if node not in visited:
                dfs(node, [])

        # 去重：只保留最长的不重复路径
        chains.sort(key=len, reverse=True)
        unique_chains = []
        seen_chain_nodes = set()
        for chain in chains:
            key = tuple(chain)
            if key not in seen_chain_nodes:
                seen_chain_nodes.add(key)
                unique_chains.append(chain)

        # Step 5.5-4: 对长链（≥3 环节）调 LLM 验证连贯性
        long_chains = [c for c in unique_chains if len(c) >= self.transmission_chain_min_length]
        chain_issues = []
        for chain in long_chains:
            chain_text = " → ".join(
                f"[{i}] ({high_sev_items[i].node_name}) "
                f"FE:{high_sev_items[i].failure_effect[:30]} → "
                f"FC:{high_sev_items[i].failure_cause[:30]}"
                for i in chain if i < len(high_sev_items)
            )
            validate_prompt = (
                f"请验证以下失效传导链的连贯性，并补充缺失环节：\n\n{chain_text}\n\n"
                "输出 JSON:\n"
                '{{"is_coherent": true/false, "gaps": ["缺失环节描述"], '
                '"supplements": [{{"position": "位置描述", "new_failure": "..."}}]}}'
            )
            try:
                v_result = await llm.call_complete(
                    system_prompt="验证失效传导链连贯性。",
                    user_message=validate_prompt,
                    force_json=True,
                )
                v_data = llm.extract_json(v_result)
                if not v_data.get("is_coherent", True):
                    chain_issues.append({
                        "chain": chain,
                        "gaps": v_data.get("gaps", []),
                        "supplements": v_data.get("supplements", []),
                    })
            except Exception:
                pass

        # Step 5.5-5: 标记孤立高风险
        in_any_chain = set()
        for c in unique_chains:
            for node in c:
                in_any_chain.add(node)
        isolated = []
        for i, fm in enumerate(high_sev_items):
            if i not in in_any_chain:
                isolated.append({
                    "index": i,
                    "node": fm.node_name,
                    "failure_mode": fm.failure_mode[:50],
                    "severity": fm.severity,
                })

        result = {
            "transmission_pairs": pairs,
            "transmission_chains": unique_chains,
            "chain_issues": chain_issues,
            "isolated_high_risk": isolated,
        }

        _console_log("Transmission", f"传导链: {len(unique_chains)} 条, "
                      f"长链(≥{self.transmission_chain_min_length}): {len(long_chains)}, "
                      f"孤立高风险: {len(isolated)}")
        if isolated:
            for iso in isolated[:5]:
                _console_log("Transmission", f"  孤立高风险 [S={iso['severity']}] {iso['node']}: {iso['failure_mode']}")

        return result

    # ════════════════════════════════════════════════════════════
    # V4 校验（V4-1 ~ V4-5）（P2 新增 V4-5 法规后验）
    # ════════════════════════════════════════════════════════════

    async def _run_validation(
        self, items: List[FailureMode], tree: StructureTree,
        func_analysis: FunctionAnalysis, reg_data: Optional[dict] = None,
        per_agent_items: Optional[Dict[str, list]] = None,
    ) -> dict:
        """执行 V4-1~V4-5 校验，输出校验报告"""
        from engine.llm_client import get_llm_client
        llm = self.llm_client or get_llm_client()
        issues = []

        # V4-1: 每条功能配套完整 FE/FM/FC
        for i, fm in enumerate(items):
            if not fm.failure_effect:
                issues.append(f"V4-1 条目#{i} ({fm.node_name}): 缺少失效影响(FE)")
            if not fm.failure_mode:
                issues.append(f"V4-1 条目#{i} ({fm.node_name}): 缺少失效模式(FM)")
            if not fm.failure_cause:
                issues.append(f"V4-1 条目#{i} ({fm.node_name}): 缺少失效原因(FC)")

        # V4-2: 失效原因限定设计缺陷（LLM 判别，保留旧关键词兜底）
        MFG_KEYWORDS = ["装配", "焊接", "拧紧", "打胶", "涂胶", "注塑", "机加工", "冲压",
                        "铸造", "来料", "批次", "供应", "产线", "工装", "夹具", "操作工", "培训"]
        fc_keyword_items = []
        for i, fm in enumerate(items):
            for kw in MFG_KEYWORDS:
                if kw in (fm.failure_cause or ""):
                    fc_keyword_items.append((i, fm, kw))
                    break

        # 用 LLM 对关键词命中的做二次确认（避免误判）
        if fc_keyword_items:
            keyword_text = "\n".join(
                f"  [{i}] ({fm.node_name}) FC=\"{fm.failure_cause}\" 命中关键词 \"{kw}\""
                for i, fm, kw in fc_keyword_items[:20]
            )
            llm_check_prompt = (
                "以下失效链的失效原因(FC)命中了一些文本关键词。\n"
                "请用 LLM 判断这些内容是否确实是制程/装配类描述（不应出现在 DFMEA FC 中），\n"
                "还是被关键词误伤的合理设计缺陷描述。\n\n"
                f"{keyword_text}\n\n"
                "输出 JSON (只输出确实不合规的):\n"
                '{{"confirmed_manufacturing": [{"index": 0, "reason": "..."}]}}'
            )
            try:
                llm_result = await llm.safe_call(
                    system_prompt="判断失效原因是设计缺陷还是制程描述。",
                    user_message=llm_check_prompt,
                    tools=[{
                        "type": "function",
                        "function": {
                            "name": "confirm_mfg_issues",
                            "description": "确认制程类 FC",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "confirmed_manufacturing": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "index": {"type": "integer"},
                                                "reason": {"type": "string"},
                                            },
                                            "required": ["index", "reason"],
                                        },
                                    }
                                },
                                "required": ["confirmed_manufacturing"],
                            },
                        }
                    }],
                    probe_key="confirmed_manufacturing",
                )
                llm_data = llm.extract_json(llm_result)
                llm_confirmed = {c["index"] for c in llm_data.get("confirmed_manufacturing", [])}
            except Exception:
                llm_confirmed = set()

            for i, fm, kw in fc_keyword_items:
                if i in llm_confirmed or not llm_confirmed:
                    issues.append(f"V4-2 条目#{i} ({fm.node_name}): 失效原因含制程关键词 '{kw}'")

        # V4-3: FE→FM→FC 链式逻辑自洽性（用 LLM 判别）
        logic_candidates = []
        for i, fm in enumerate(items):
            if fm.failure_mode and (fm.failure_effect or fm.failure_cause):
                logic_candidates.append((i, fm))

        if logic_candidates:
            logic_text = "\n".join(
                f"  [{i}] node={fm.node_name} | FE={fm.failure_effect[:40]} | FM={fm.failure_mode[:40]} | FC={fm.failure_cause[:40]}"
                for i, fm in logic_candidates[:30]
            )
            llm_logic_prompt = (
                "请用 LLM 判断以下失效链的 FE→FM→FC 逻辑是否存在问题：\n"
                "1. FM 与 FE 是否实质相同（退化）\n"
                "2. FM 与 FC 是否实质相同（退化）\n"
                "3. FE、FM、FC 是否有逻辑断层\n\n"
                f"{logic_text}\n\n"
                "输出 JSON (只输出存在问题的):\n"
                '{{"issues": [{"index": 0, "problem": "FM与FE实质相同", "description": "..."}]}}'
            )
            try:
                llm_logic_result = await llm.safe_call(
                    system_prompt="检查失效链逻辑自洽性。",
                    user_message=llm_logic_prompt,
                    tools=[{
                        "type": "function",
                        "function": {
                            "name": "report_logic_issues",
                            "description": "报告逻辑问题",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "issues": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "index": {"type": "integer"},
                                                "problem": {"type": "string"},
                                                "description": {"type": "string"},
                                            },
                                            "required": ["index", "problem", "description"],
                                        },
                                    }
                                },
                                "required": ["issues"],
                            },
                        }
                    }],
                    probe_key="issues",
                )
                llm_logic_data = llm.extract_json(llm_logic_result)
                logic_issues = llm_logic_data.get("issues", [])
                for li in logic_issues:
                    issues.append(f"V4-3 条目#{li['index']}: {li['problem']} — {li.get('description','')}")
            except Exception:
                pass

        # V4-4: 功能一一匹配
        covered_fn_ids = set(fm.function_id for fm in items if fm.function_id)
        total_fn_ids = set()
        for it in func_analysis.items:
            for fn in it.functions:
                if fn.function_id:
                    total_fn_ids.add(fn.function_id)
        uncovered = total_fn_ids - covered_fn_ids
        if uncovered:
            issues.append(f"V4-4 有 {len(uncovered)} 条功能未覆盖失效分析")

        # V4-5: 关键法规条款覆盖检查（P2 法规后验）
        if reg_data and reg_data.get("clauses"):
            key_clauses = [c for c in reg_data["clauses"] if c.get("is_key", False)]
            if key_clauses and items:
                # 从 per_agent_items 原始 dict 数据中收集 regulationRefs
                all_refs = set()
                for agent_name, raw_list in per_agent_items.items():
                    for item in raw_list:
                        if isinstance(item, dict):
                            refs = item.get("regulationRefs", [])
                            if isinstance(refs, list):
                                all_refs.update(refs)

                uncovered_clauses = []
                for clause in key_clauses:
                    clause_id = clause.get("clause_id", clause.get("id", ""))
                    if clause_id and clause_id not in all_refs:
                        # LLM 判断该条款是否确实应该被至少一条失效链引用
                        uncovered_clauses.append(clause_id)

                if uncovered_clauses:
                    clause_text = "\n".join(f"  - {c}" for c in uncovered_clauses[:10])
                    llm_reg_prompt = (
                        "以下法规条款未在任何失效链中被引用。但有些条款可能与本项目不相关。\n"
                        "请用 LLM 判断哪些条款确实应该被至少一条失效链引用。\n\n"
                        f"{clause_text}\n\n"
                        "输出 JSON:\n"
                        '{{"should_cover": ["条款ID", ...], "reason": "..."}}'
                    )
                    try:
                        llm_reg_result = await llm.call_complete(
                            system_prompt="检查法规条款覆盖。",
                            user_message=llm_reg_prompt,
                            force_json=True,
                        )
                        llm_reg_data = llm.extract_json(llm_reg_result)
                        should_cover = llm_reg_data.get("should_cover", [])
                        for cid in should_cover:
                            issues.append(f"V4-5 法规条款 {cid} 未被任何失效链引用")
                    except Exception:
                        for cid in uncovered_clauses[:5]:
                            issues.append(f"V4-5 法规条款 {cid} 未被任何失效链引用")

        report = {
            "v4_1_passed": all(fm.failure_effect and fm.failure_mode and fm.failure_cause for fm in items),
            "v4_2_passed": not any("V4-2" in iss for iss in issues),
            "v4_3_passed": not any("V4-3" in iss for iss in issues),
            "v4_4_passed": len(uncovered) == 0,
            "v4_5_passed": not any("V4-5" in iss for iss in issues),
            "total_issues": len(issues),
            "issues": issues,
        }

        _console_log("Validation", f"V4-1={report['v4_1_passed']} V4-2={report['v4_2_passed']} "
                      f"V4-3={report['v4_3_passed']} V4-4={report['v4_4_passed']} "
                      f"V4-5={report['v4_5_passed']}")
        if issues:
            for iss in issues[:5]:
                _console_log("Validation", f"  Issue: {iss}")
            if len(issues) > 5:
                _console_log("Validation", f"  ... +{len(issues)-5} more")

        return report

    # ════════════════════════════════════════════════════════════
    # 高风险主题传导链路复核（P1 深化）
    # ════════════════════════════════════════════════════════════

    async def _check_high_risk_transmission(self, items: List[FailureMode]) -> List[str]:
        """高风险主题传导链路完整性检查（P1 深化）

        每个高风险主题在 DB 中存一段自然语言描述（标准传导路径应包含哪些层级）。
        复核时把描述 + 已生成的该主题失效链喂给 LLM。
        LLM 输出：已覆盖环节、缺失环节、是否需要补充。
        缺失环节调 LLM 补充生成。
        """
        from engine.llm_client import get_llm_client
        llm = self.llm_client or get_llm_client()
        findings = []
        if not self._high_risk_themes:
            return findings

        for theme in self._high_risk_themes:
            theme_name = theme.get("name", theme.get("theme", "未命名主题"))
            # P1: 主题的自然语言描述（标准传导路径应包含哪些层级）
            theme_description = theme.get("description", theme.get("transmission_path", ""))

            # 查找关联的失效条目（按 failure_network_type 或关键词匹配）
            related = []
            for fm in items:
                if fm.failure_network_type == "安全功能失效":
                    related.append(fm)
                elif theme_name in (fm.failure_effect or "") or theme_name in (fm.failure_mode or ""):
                    related.append(fm)

            if not related:
                # 无关联条目 → 用 LLM 判断是否需要补充
                if theme_description:
                    prompt_supplement = (
                        f"高风险主题「{theme_name}」的标准传导路径：{theme_description}\n\n"
                        "当前没有任何关联的失效条目。请判断是否需要补充？如果需要，请生成 1~3 条基础失效链。\n\n"
                        "输出 JSON:\n"
                        '{{"needs_supplement": true/false, "reason": "...", '
                        '"supplements": [{{"node": "...", "failureEffect": "...", '
                        '"severity": 8, "failureMode": "...", "failureCause": "..."}}]}}'
                    )
                    try:
                        result = await llm.call_complete(
                            system_prompt="高风险主题失效分析。",
                            user_message=prompt_supplement,
                            force_json=True,
                        )
                        data = llm.extract_json(result)
                        if data.get("needs_supplement", False):
                            findings.append(f"高风险主题「{theme_name}」无关联失效条目，LLM建议补充")
                            _console_log("HighRisk", f"  {theme_name}: LLM建议补充 "
                                          f"{len(data.get('supplements', []))} 条失效链")
                    except Exception:
                        findings.append(f"高风险主题「{theme_name}」无关联失效条目")
                else:
                    findings.append(f"高风险主题「{theme_name}」无关联失效条目")
                continue

            # 有相关条目 → 用 LLM 做深入复核
            related_text = "\n".join(
                f"  [{i}] S={fm.severity} FE={fm.failure_effect[:40]} | FM={fm.failure_mode[:40]} | FC={fm.failure_cause[:40]}"
                for i, fm in enumerate(related[:15])
            )

            llm_review_prompt = (
                f"高风险主题「{theme_name}」的标准传导路径：{theme_description}\n\n"
                "已生成的关联失效链：\n"
                f"{related_text}\n\n"
                "请用 LLM 分析：\n"
                "1. 已覆盖了标准路径中的哪些环节\n"
                "2. 哪些环节缺失\n"
                "3. 是否需要补充，如需补充请生成缺失环节的失效链\n\n"
                "输出 JSON:\n"
                '{{\n'
                '  "covered_stages": ["已覆盖环节列表"],\n'
                '  "missing_stages": ["缺失环节列表"],\n'
                '  "needs_supplement": true/false,\n'
                '  "supplements": [{{"stage": "缺失环节描述", '
                '"failureEffect": "...", "severity": 8, '
                '"failureMode": "...", "failureCause": "..."}}]\n'
                '}}\n'
            )
            try:
                result = await llm.call_complete(
                    system_prompt="FMEA 高风险主题传导链路复核专家。",
                    user_message=llm_review_prompt,
                    force_json=True,
                )
                data = llm.extract_json(result)
                covered = data.get("covered_stages", [])
                missing = data.get("missing_stages", [])
                needs_supplement = data.get("needs_supplement", False)

                if covered:
                    _console_log("HighRisk", f"  {theme_name} 已覆盖环节: {', '.join(covered)}")
                if missing:
                    findings.append(f"高风险主题「{theme_name}」缺失环节: {', '.join(missing)}")
                    _console_log("HighRisk", f"  {theme_name} 缺失: {', '.join(missing)}")
                if needs_supplement:
                    supplements = data.get("supplements", [])
                    findings.append(f"高风险主题「{theme_name}」LLM 生成了 {len(supplements)} 条补充失效链")
                    _console_log("HighRisk", f"  {theme_name} 补充 {len(supplements)} 条失效链")
                else:
                    _console_log("HighRisk", f"  {theme_name} 覆盖完整，无需补充")
            except Exception as e:
                _console_log("HighRisk", f"  {theme_name} LLM 复核异常: {e}")
                findings.append(f"高风险主题「{theme_name}」LLM 复核异常")

        _console_log("HighRisk", f"高风险主题复核完成: {len(findings)} 项发现")
        return findings
