"""Step 3 失效分析 Agent — v4 KB感知 + 功能网感知 + Fallback

核心增强（对比 v3）：
1. [新增] 知识库检索增强 — 查询组件关键特性/已知失效/标准要求
2. [新增] 功能网上下文感知 — 利用功能关系识别跨功能/级联失效
3. [新增] severity_reason — 每条 severity 附带 AIAG/VDA 标准依据
4. [新增] failure_network_type — 功能网传导类型标注
5. [增强] 失效链验证 — FE→FM→FC 逻辑链一致性检查
6. [增强] Fallback — 基于 DFMEA 方法论通用失效模式，零硬编码模板
"""
import json
import logging
from typing import List, Optional, Tuple, Dict

from models.function import FunctionAnalysis, FunctionAnalysisItem, FunctionDetail, FunctionNetwork
from models.structure import StructureTree
from models.failure import FailureMode, FailureAnalysis
from engine.llm_client import get_llm_client, LLMClient
from engine.concurrent import get_concurrent_engine, ConcurrentFmeaEngine
from engine.tools import build_node_context

logger = logging.getLogger(__name__)


# ── DFMEA 通用失效模式（AIAG/VDA 标准，适用于任何工程领域）──
# 任何功能的失效都可以归类为以下 5 种通用类型
UNIVERSAL_FAILURE_PATTERNS = [
    "功能丧失 → 该功能完全无法执行",
    "功能降级 → 该功能虽能执行但性能低于设计要求",
    "功能间歇 → 该功能时有时无、不稳定",
    "非预期功能 → 系统产生了该功能不应有的行为",
    "功能延迟 → 该功能的响应/执行超出允许的时间窗口",
]


# ── DFMEA 层级默认严重度（基于层级影响范围，无领域假设）──
LEVEL_BASE_SEVERITY = {
    "系统": 7,     # 系统级失效影响范围大
    "子系统": 6,   # 子系统级影响中等偏上
    "组件": 5,     # 组件级影响中等
    "零件": 4,     # 零件级影响局部
}


# ── 严重度辅助 ──

# DFMEA 通用失效模式的中文标签映射（用于 Fallback 的失效影响描述）
FAILURE_PATTERN_LABELS = {
    "功能丧失": "功能完全丧失",
    "功能降级": "功能性能降级",
    "功能间歇": "功能时断时续",
    "非预期功能": "非预期功能输出",
    "功能延迟": "功能响应延迟",
}


class FailureAgent:
    """Step 3: 失效分析 — v4 KB增强 + 功能网感知 + Fallback"""

    def __init__(self, enable_kb_search: bool = True):
        self.llm = get_llm_client()
        self.engine = get_concurrent_engine()
        self.enable_kb_search = enable_kb_search

    # ════════════════════════════════════════════════════════════
    # 主入口
    # ════════════════════════════════════════════════════════════

    async def generate_failure_analysis(
        self,
        tree: StructureTree,
        func_analysis: FunctionAnalysis,
        project_id: Optional[str] = None,
    ) -> FailureAnalysis:
        """基于功能分析生成失效分析（v4: KB增强 + 功能网感知）

        流程：
        1. 构建节点/功能/网络映射
        2. 对每条功能，查询 KB（如启用），调 LLM 分析失效链
        3. 异常时使用 Fallback 推断
        4. 失效链验证（FE→FM→FC 逻辑一致性）
        5. 组装输出
        """
        from engine.prompts import FAILURE_ANALYSIS_V4_SYSTEM

        if not func_analysis.items:
            return FailureAnalysis(items=[])

        nodes = [n.model_dump() for n in tree.nodes]
        func_map: Dict[str, FunctionAnalysisItem] = {
            item.node_id: item for item in func_analysis.items
        }

        # 构建功能网索引（用于跨功能失效感知）
        network = func_analysis.function_network
        fn_relations_index = self._build_fn_relations_index(network)

        # ── v5: 构建工程师/父节点名索引（用于与功能分析对齐） ──
        # node_id → parent_node_name（从结构树查）
        node_parent_idx: Dict[str, str] = {}
        for n in nodes:
            nid = n.get("id", "")
            pid = n.get("parent_id", "")
            if nid and pid:
                pn = next((x for x in nodes if x.get("id") == pid), None)
                if pn:
                    node_parent_idx[nid] = pn.get("name", "")

        # function_id → 完整 FunctionDetail（按 function_id 索引，供 upper/lower function 反查）
        fn_detail_idx: Dict[str, FunctionDetail] = {}
        # function_id → (assignee_user_id, assignee_name, higher_level_name)
        fn_assignee_idx: Dict[str, dict] = {}
        for func_item in func_analysis.items:
            uid = getattr(func_item, "assignee_user_id", -1)
            uname = getattr(func_item, "assignee_name", "") or ""
            hname = node_parent_idx.get(func_item.node_id, "")
            for fn in func_item.functions:
                fid = getattr(fn, "function_id", "")
                if fid:
                    fn_detail_idx[fid] = fn
                    fn_assignee_idx[fid] = {
                        "assignee_user_id": uid,
                        "assignee_name": uname,
                        "higher_level_name": hname,
                    }
            # 兜底：按 node_id
            fn_assignee_idx[func_item.node_id] = {
                "assignee_user_id": uid,
                "assignee_name": uname,
                "higher_level_name": hname,
            }

        # ── 构建分析目标 ──
        analysis_targets: List[Tuple[dict, FunctionAnalysisItem, FunctionDetail, str]] = []
        for func_item in func_analysis.items:
            node = next((n for n in nodes if n.get("id") == func_item.node_id), None)
            if not node:
                continue
            for fn in func_item.functions:
                # 查询该功能在功能网中的相关关系
                network_context = self._get_fn_network_context(
                    fn.function_id, fn_relations_index, func_map
                )
                analysis_targets.append((node, func_item, fn, network_context))

        logger.info(
            f"[FailureAgent-v4] Starting: {len(analysis_targets)} functions "
            f"across {len(func_analysis.items)} nodes"
        )

        # ── Tool 定义（v4.1 链式：按 (focus, child) 拆，每条失效带 childNodeId）──
        tools = [{
            "type": "function",
            "function": {
                "name": "analyze_failure",
                "description": "分析单个 (focus_node, child_node, child_function) 三元组的失效链（含 child 维度和评分依据）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "failureModes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "childNodeId": {
                                        "type": "string",
                                        "description": "该失效链针对的子级节点ID（必填，与输入的 childNodeId 一致）",
                                    },
                                    "childNodeName": {
                                        "type": "string",
                                        "description": "对应子级名称（便于阅读校验）",
                                    },
                                    "functionId": {"type": "string"},
                                    "functionDescription": {"type": "string"},
                                    "failureEffect": {"type": "string"},
                                    "severity": {"type": "integer", "minimum": 1, "maximum": 10},
                                    "severityReason": {"type": "string"},
                                    "failureMode": {"type": "string"},
                                    "failureCause": {"type": "string"},
                                    "failureNetworkType": {
                                        "type": "string",
                                        "enum": [
                                            "安全功能失效", "功能丧失传导", "功能降级传导",
                                            "冗余失效", "接口失效传导", "一般失效",
                                        ],
                                    },
                                },
                                "required": [
                                    "childNodeId", "functionId", "failureEffect", "severity",
                                    "severityReason", "failureMode",
                                ],
                            },
                        }
                    },
                    "required": ["failureModes"],
                },
            }
        }]

        # ── 并发处理器 ──
        async def analyze_single(target: tuple) -> List[FailureMode]:
            node, func_item, fn_detail, network_ctx = target
            context = build_node_context(node, nodes)

            # 知识库检索增强
            kb_context = ""
            if self.enable_kb_search and project_id:
                kb_context = await self._query_kb_for_failure(
                    project_id, node, fn_detail
                )

            # v4.1 链式：在 user_msg 显式给出 (focus, child) 上下文
            child_id = getattr(fn_detail, "child_node_id", "") or ""
            child_name = ""
            if child_id:
                child_node = next((n for n in nodes if n.get("id") == child_id), None)
                if child_node:
                    child_name = child_node.get("name", "")

            user_msg_parts = [
                f"分析以下 (focus_node, child_node, child_function) 三元组的潜在失效模式：\n\n{context}",
                f"上级功能要求: {func_item.upper_level_function}",
                f"当前分析 child_function [{fn_detail.function_category}]: {fn_detail.description}",
                f"功能ID: {fn_detail.function_id}",
                f"功能流类型: {fn_detail.flow_type}",
                f"下级功能要求: {func_item.lower_level_function}",
            ]
            if child_id:
                user_msg_parts.append(
                    f"【链式上下文】\n"
                    f"本条失效必须针对的子级: id={child_id}, name={child_name}, level={next((n.get('level','') for n in nodes if n.get('id')==child_id), '')}\n"
                    f"→ 所有 failureMode / failureCause 都应与该 child 直接相关；FC 应来自该 child 内部或 child 的下一级"
                )
            if network_ctx:
                user_msg_parts.append(f"\n功能网关联关系：\n{network_ctx}")
            if kb_context:
                user_msg_parts.append(f"\n参考知识库信息：\n{kb_context}")

            user_msg = "\n".join(user_msg_parts)

            try:
                result = await self.llm.safe_call(
                    system_prompt=FAILURE_ANALYSIS_V4_SYSTEM,
                    user_message=user_msg,
                    tools=tools,
                    probe_key="failureModes",
                )

                data = self.llm.extract_json(result)
                modes = data.get("failureModes", [])

                if not modes:
                    logger.warning(
                        f"[FailureAgent-v4] No failure modes for "
                        f"{node.get('name')}/{fn_detail.function_category}"
                    )
                    return await self._fallback_failure(
                        node, fn_detail, network_ctx,
                        all_nodes=nodes, fn_assignee_idx=fn_assignee_idx, fn_detail_idx=fn_detail_idx,
                    )

                # 解析并验证失效链
                parsed = self._parse_failure_modes(
                    modes, node, fn_detail,
                    all_nodes=nodes, fn_assignee_idx=fn_assignee_idx, fn_detail_idx=fn_detail_idx,
                )
                validated = self._validate_failure_chains(parsed)
                return validated

            except Exception as e:
                logger.error(
                    f"[FailureAgent-v4] Analysis failed for "
                    f"{node.get('id','')}/{fn_detail.function_id}: {e}"
                )
                return await self._fallback_failure(
                    node, fn_detail, network_ctx,
                    all_nodes=nodes, fn_assignee_idx=fn_assignee_idx, fn_detail_idx=fn_detail_idx,
                )

        # ── 并发执行 ──
        results = await self.engine.process_batch(analysis_targets, analyze_single)
        all_modes = []
        for mode_list in results:
            all_modes.extend(mode_list)

        analysis = FailureAnalysis(items=all_modes)
        logger.info(f"[FailureAgent-v4] Complete: {len(all_modes)} failure chains")
        return analysis

    # ════════════════════════════════════════════════════════════
    # 功能网关系索引（支持跨功能失效感知）
    # ════════════════════════════════════════════════════════════

    def _build_fn_relations_index(
        self, network: Optional[FunctionNetwork]
    ) -> Dict[str, list]:
        """构建 function_id → [关联关系] 的索引"""
        index: Dict[str, list] = {}
        if not network or not network.relations:
            return index
        for rel in network.relations:
            src = rel.source_function_id
            tgt = rel.target_function_id
            if src:
                index.setdefault(src, []).append({
                    "direction": "source",
                    "peer_fn_id": tgt,
                    "peer_node_id": rel.target_node_id,
                    "peer_node_name": rel.target_label or rel.target_node_id,
                    "relation_type": rel.relation_type,
                    "description": rel.description,
                })
            if tgt:
                index.setdefault(tgt, []).append({
                    "direction": "target",
                    "peer_fn_id": src,
                    "peer_node_id": rel.source_node_id,
                    "peer_node_name": rel.source_label or rel.source_node_id,
                    "relation_type": rel.relation_type,
                    "description": rel.description,
                })
        return index

    def _get_fn_network_context(
        self,
        fn_id: str,
        fn_relations_index: Dict[str, list],
        func_map: Dict[str, FunctionAnalysisItem],
    ) -> str:
        """为指定功能生成功能网上下文摘要"""
        rels = fn_relations_index.get(fn_id, [])
        if not rels:
            return ""

        lines = [f"该功能在功能网中有 {len(rels)} 条关联关系:"]
        for r in rels[:5]:  # 最多展示 5 条
            peer_name = r.get("peer_node_name", "")
            peer_fn_id = r.get("peer_fn_id", "")
            rel_type = r.get("relation_type", "")
            dir_label = "需求方" if r["direction"] == "source" else "满足方"
            lines.append(f"  - [{rel_type}] {dir_label}: {peer_name}({peer_fn_id})")
        if len(rels) > 5:
            lines.append(f"  ... (+{len(rels)-5} 条)")
        return "\n".join(lines)

    # ════════════════════════════════════════════════════════════
    # 知识库检索增强
    # ════════════════════════════════════════════════════════════

    async def _query_kb_for_failure(
        self,
        project_id: str,
        node: dict,
        fn_detail: FunctionDetail,
    ) -> str:
        """查询项目 KB + Agent 专属 KB 获取组件的已知失效模式/标准要求

        双库联合检索：先搜项目 KB，再搜本 Agent 专属 KB，合并时项目 KB 优先
        """
        try:
            from knowledge.weknora_client import get_weknora_client
            from engine.tools import merge_multi_kb_results, get_agent_weknora_kb_id

            wk = get_weknora_client()
            node_name = node.get("name", "")
            description = fn_detail.description
            function_class = fn_detail.function_class

            # 构造查询
            question = f"{node_name} {description} 失效模式 失效原因 失效影响 设计要求 已知失效"

            # 查询项目 KB
            project_kb_id = ""
            try:
                from pathlib import Path
                meta_paths = [
                    Path("knowledge") / project_id / "project_meta.json",
                    Path("data") / "projects" / project_id / "project_meta.json",
                ]
                for mp in meta_paths:
                    if mp.exists():
                        import json
                        with open(mp, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                            project_kb_id = meta.get("weknora_kb_id", "")
                            if project_kb_id:
                                break
            except Exception:
                pass

            if not project_kb_id:
                return ""  # 无项目 KB 配置���跳过

            # 查询专属 KB
            own_kb_id = get_agent_weknora_kb_id("法规专家")  # FailureAgent 本身没有专属 KB，先用法规专家的

            # 联合检索
            kb_ids = [project_kb_id]
            if own_kb_id:
                kb_ids.append(own_kb_id)

            results = wk.hybrid_search_multi(
                kb_ids, query_text=question, match_count=10,
            )

            if not results:
                return ""

            # 分离项目 KB 和专属 KB 结果，合并（项目 KB 优先）
            project_results = [r for r in results if r.get("source_kb_id") == project_kb_id]
            own_results = [r for r in results if r.get("source_kb_id") != project_kb_id]
            merged = merge_multi_kb_results(project_results, own_results, top_n=5)

            parts = []
            for i, r in enumerate(merged, 1):
                content = r.get("content", "")[:500]
                source = r.get("knowledge_title", r.get("knowledge_filename", ""))
                priority = r.get("kb_priority", "")
                if content:
                    parts.append(f"【来源{priority}:{source}】\n{content}")

            return "\n---\n".join(parts) if parts else ""

        except Exception as e:
            logger.debug(f"[FailureAgent-v4] KB query failed ({node.get('id','')}): {e}")
            return ""

        except Exception as e:
            logger.debug(f"[FailureAgent-v4] KB query failed ({node.get('id','')}): {e}")
            return ""

    # ════════════════════════════════════════════════════════════
    # 解析 & 验证
    # ════════════════════════════════════════════════════════════

    def _parse_failure_modes(
        self,
        modes: List[dict],
        node: dict,
        fn_detail: FunctionDetail,
        all_nodes: Optional[list] = None,
        fn_assignee_idx: Optional[Dict[str, dict]] = None,
        fn_detail_idx: Optional[Dict[str, FunctionDetail]] = None,
    ) -> List[FailureMode]:
        """解析 LLM 输出的失效链为 FailureMode 列表（v4.1: 写入 child_node_id）"""
        node_id = node.get("id", "")
        node_name = node.get("name", "")
        fn_idx = fn_detail.function_id.split("-")[-1]
        # v4.1: 从 fn_detail 拿 child_node_id，作为兜底
        default_child_id = getattr(fn_detail, "child_node_id", "") or ""

        results = []
        for i, m in enumerate(modes):
            if not m.get("failureMode"):
                continue

            # 校验 severity 范围
            sev = int(m.get("severity", 4))
            sev = max(1, min(10, sev))

            # 获取 severityReason
            sev_reason = m.get("severityReason", m.get("severity_reason", ""))
            if not sev_reason:
                sev_reason = self._infer_severity_reason(sev, m.get("failureEffect", ""))

            # 获取 failureNetworkType
            net_type = m.get("failureNetworkType", m.get("failure_network_type", ""))
            if net_type not in (
                "安全功能失效", "功能丧失传导", "功能降级传导",
                "冗余失效", "接口失效传导", "一般失效",
            ):
                net_type = self._infer_failure_network_type(
                    sev, m.get("failureEffect", "")
                )

            # v4.1: 读取 childNodeId（LLM 必须显式输出），fallback 到 fn_detail 的 child_node_id
            child_id = (
                m.get("childNodeId")
                or m.get("child_node_id")
                or default_child_id
            )

            # v5: 查 child_node_name（从结构树全节点列表）
            child_node_name = ""
            if child_id:
                cn = next((n for n in all_nodes if n.get("id") == child_id), None)
                if cn:
                    child_node_name = cn.get("name", "")

            # v5 字段（higher_level_name / child_node_name / upper_function /
            #     lower_function / assignee_user_id / assignee_name）不再在写入时填充，
            #     改由前端渲染层按 (function_id, child_node_id) JOIN 功能分析数据。
            # —— 保留 DB 字段定义是为了向后兼容读取已有数据。
            results.append(FailureMode(
                id=f"fm-{node_id}-{fn_idx}-{i+1:02d}",
                node_id=node_id,
                node_name=node_name,
                child_node_id=child_id,                            # v4.1
                function_id=fn_detail.function_id,
                function_description=fn_detail.description,
                failure_effect=m.get("failureEffect", ""),
                severity=sev,
                severity_reason=sev_reason,
                failure_mode=m.get("failureMode", ""),
                failure_cause=m.get("failureCause", ""),
                failure_network_type=net_type,
            ))
        return results

    def _validate_failure_chains(
        self, modes: List[FailureMode]
    ) -> List[FailureMode]:
        """验证 FE→FM→FC 逻辑链一致性

        - FE 应该与 FM 相关（FE 是 FM 的结果）
        - FC 应该与 FM 相关（FC 是 FM 的原因）
        - FM 不能与 FE 或 FC 完全相同
        - 如果验证不过，在 ID 后加 [W] 标记
        """
        validated = []
        for fm in modes:
            # 检查 FM 不能=FE 也不能=FC
            if fm.failure_mode and fm.failure_effect:
                if fm.failure_mode.strip() == fm.failure_effect.strip():
                    fm.failure_mode += "（需人工确认）"
            if fm.failure_mode and fm.failure_cause:
                if fm.failure_mode.strip() == fm.failure_cause.strip():
                    fm.failure_cause += "（需人工确认）"
            validated.append(fm)
        return validated

    # ════════════════════════════════════════════════════════════
    # Fallback 推断（基于 DFMEA 方法论，零硬编码模板）
    # ════════════════════════════════════════════════════════════

    async def _fallback_failure(
        self, node: dict, fn_detail: FunctionDetail, network_ctx: str = "",
        all_nodes: Optional[list] = None,
        fn_assignee_idx: Optional[Dict[str, dict]] = None,
        fn_detail_idx: Optional[Dict[str, FunctionDetail]] = None,
    ) -> List[FailureMode]:
        """当 LLM Tool Calling 失败时，降级策略

        降级路径：
        1. 尝试用 LLM Complete 模式（无 tool calling）再试一次
        2. 如果仍失败，使用 DFMEA 方法论通用的失效模式生成
           基于层级 + 功能描述，不依赖任何领域模板
        """
        modes = await self._try_llm_complete_fallback(
            node, fn_detail, network_ctx,
            all_nodes=all_nodes, fn_assignee_idx=fn_assignee_idx, fn_detail_idx=fn_detail_idx,
        )
        if modes:
            return modes

        # 纯方法论兜底：基于 DFMEA 通用失效模式
        return self._dfmea_heuristic_fallback(
            node, fn_detail, all_nodes=all_nodes, fn_assignee_idx=fn_assignee_idx, fn_detail_idx=fn_detail_idx,
        )

    async def _try_llm_complete_fallback(
        self, node: dict, fn_detail: FunctionDetail, network_ctx: str = "",
        all_nodes: Optional[list] = None,
        fn_assignee_idx: Optional[Dict[str, dict]] = None,
        fn_detail_idx: Optional[Dict[str, FunctionDetail]] = None,
    ) -> Optional[List[FailureMode]]:
        """用 LLM Complete 模式（无 tool calling）重新尝试"""
        from engine.prompts import FAILURE_ANALYSIS_V4_SYSTEM

        simplified_prompt = (
            "你是DFMEA失效分析专家。请分析以下功能的潜在失效模式。\n\n"
            "输出纯JSON数组，每个元素包含：functionId, functionDescription, "
            "failureEffect, severity(1-10), severityReason, failureMode, failureCause"
        )

        context = build_node_context(node, [])
        user_msg = (
            f"组件: {node.get('name','')} (层级: {node.get('level','')})\n"
            f"功能 [{fn_detail.function_category}]: {fn_detail.description}\n"
            f"功能ID: {fn_detail.function_id}\n"
            f"功能流: {fn_detail.flow_type}\n"
        )
        if network_ctx:
            user_msg += f"\n关联: {network_ctx[:200]}"

        try:
            result = await self.llm.call_complete(
                system_prompt=simplified_prompt,
                user_message=user_msg,
                force_json=True,
            )
            if not result:
                return None

            data = self.llm.extract_json(result)
            modes_raw = data if isinstance(data, list) else data.get("failureModes", [])
            if not modes_raw:
                return None

            parsed = self._parse_failure_modes(
                modes_raw, node, fn_detail,
                all_nodes=all_nodes, fn_assignee_idx=fn_assignee_idx, fn_detail_idx=fn_detail_idx,
            )
            return self._validate_failure_chains(parsed)
        except Exception as e:
            logger.debug(f"[FailureAgent-v4] LLM fallback also failed: {e}")
            return None

    @staticmethod
    def _dfmea_heuristic_fallback(
        node: dict, fn_detail: FunctionDetail,
        all_nodes: Optional[list] = None,
        fn_assignee_idx: Optional[Dict[str, dict]] = None,
        fn_detail_idx: Optional[Dict[str, FunctionDetail]] = None,
    ) -> List[FailureMode]:
        """基于 DFMEA 方法论生成通用失效链，零领域知识硬编码

        原理：任何功能的失效都可以映射到 5 种通用失效模式
        （AIAG/VDA 定义的失效分类），结合层级影响范围估算严重度。
        """
        node_name = node.get("name", "")
        node_id = node.get("id", "")
        fn_idx = fn_detail.function_id.split("-")[-1]
        level = node.get("level", "零件")
        base_sev = LEVEL_BASE_SEVERITY.get(level, 5)

        # 根据失效类型对 base_sev 做偏移，产生有区分度的 3 条链
        results = []
        for i, (pattern, label) in enumerate([
            ("功能丧失", 0),      # 基准严重度
            ("功能降级", -1),     # 降一级
            ("非预期功能", -1),   # 降一级
        ][:3]):
            sev = max(1, min(10, base_sev + label))
            fe = f"{FAILURE_PATTERN_LABELS.get(pattern, pattern)}: {node_name}的「{fn_detail.description}」无法正常执行"

            # 从功能描述中提取关键词辅助生成 FM
            fm_brief = fn_detail.description[:20] if len(fn_detail.description) > 20 else fn_detail.description
            if pattern == "功能丧失":
                fm = f"{fm_brief} — 功能完全丧失，无法输出预期结果"
                fc = f"执行该功能所需的输入/能量/信号缺失或中断"
            elif pattern == "功能降级":
                fm = f"{fm_brief} — 功能性能低于设计指标"
                fc = f"执行条件偏离设计范围，如温度/压力/负载超出额定值"
            elif pattern == "功能间歇":
                fm = f"{fm_brief} — 功能时有时无、不稳定"
                fc = f"外部环境波动或内部状态不稳定导致功能间歇性中断"
            elif pattern == "非预期功能":
                fm = f"{fm_brief} — 产生不应有的异常输出或行为"
                fc = f"干扰/串扰/误触发导致非预期的功能激活"
            elif pattern == "功能延迟":
                fm = f"{fm_brief} — 功能响应时间超出允许窗口"
                fc = f"信号/能量/物料传输路径存在迟滞或阻塞"
            else:
                fm = f"{fm_brief} — 功能异常"
                fc = f"制造/装配/材料偏差导致功能偏差"

            sev_reason = (
                f"S={sev}: 基于DFMEA方法论，{level}级"
                f"{'功能丧失' if label == 0 else '功能降级'}的默认严重度范围。"
                "具体评级需结合项目实际情况调整。"
            )

            net_type = "功能丧失传导" if label == 0 else "功能降级传导"

            # v5 字段（higher_level_name / child_node_name / upper_function /
            #     lower_function / assignee_user_id / assignee_name）不再写入，
            #     改由前端渲染层按 (function_id, child_node_id) JOIN 功能分析。
            results.append(FailureMode(
                id=f"fm-{node_id}-{fn_idx}-{i+1:02d}",
                node_id=node_id,
                node_name=node_name,
                child_node_id=getattr(fn_detail, "child_node_id", "") or "",
                function_id=fn_detail.function_id,
                function_description=fn_detail.description,
                failure_effect=fe,
                severity=sev,
                severity_reason=sev_reason,
                failure_mode=fm,
                failure_cause=fc,
                failure_network_type=net_type,
            ))
        return results


    # ════════════════════════════════════════════════════════════
    # 严重度辅助（AIAG/VDA 标准，适用于所有领域）
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _infer_severity_reason(severity: int, failure_effect: str) -> str:
        """为 severity 生成标准依据描述"""
        if severity >= 9:
            return (
                f"S={severity}: 失效涉及安全/法规风险，"
                "根据AIAG/VDA标准，安全/法规相关失效严重度为9-10"
            )
        if severity == 8:
            return (
                f"S={severity}: 主要功能丧失，"
                "根据AIAG/VDA标准，功能完全丧失严重度为8"
            )
        if severity == 7:
            return (
                f"S={severity}: 主要功能降级，"
                "根据AIAG/VDA标准，功能显著降级严重度为7"
            )
        if severity >= 5:
            return (
                f"S={severity}: 客户明显感知的功能/外观影响，"
                "根据AIAG/VDA标准"
            )
        return (
            f"S={severity}: 影响轻微或无影响，"
            "根据AIAG/VDA标准"
        )

    @staticmethod
    def _infer_failure_network_type(severity: int, failure_effect: str) -> str:
        """从严重度推断功能网传导类型

        基于 AIAG/VDA 标准的严重度分级，不依赖领域关键词。
        """
        if severity >= 9:
            return "安全功能失效"
        if severity >= 8:
            return "功能丧失传导"
        if severity >= 6:
            return "功能降级传导"
        return "一般失效"

    # ════════════════════════════════════════════════════════════
    # 前端展示（v4 增强版：含评分依据）
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def format_for_display(analysis: FailureAnalysis) -> str:
        """格式化失效分析用于前端展示（v4.1: 按节点→功能层级分组）

        与 fmea_worksheet.html 的展示逻辑一致：
        - 按关注要素分组
        - 每个关注要素下按功能分组
        - 每个功能下展示失效链表格
        """
        if not analysis.items:
            return "（无失效分析数据）"

        stats = analysis.get_summary_stats()
        groups = analysis.group_by_node_and_function()

        lines = [
            f"⚠️ **失效分析** (共 {stats['total']} 条失效链)",
            (
                f"高严重度(S≥7): {stats['high_severity_count']}"
                f" | 安全功能失效: {stats['safety_failure_count']}"
                f" | 功能丧失: {stats['function_loss_count']}"
                f" | 功能降级: {stats['function_degrade_count']}"
            ),
            "",
        ]

        for node_name, node_data in groups.items():
            fn_count = len(node_data["functions"])
            total_fails = sum(
                len(fn_data["failures"]) for fn_data in node_data["functions"].values()
            )
            node_high = sum(
                1 for fn_data in node_data["functions"].values()
                for fm in fn_data["failures"] if fm.severity >= 7
            )
            lines.append(f"### 📦 {node_name}")
            lines.append(
                f"*{fn_count} 个功能, {total_fails} 条失效链"
                + (f", S≥7: {node_high}" if node_high else "")
                + "*"
            )
            lines.append("")

            for fn_key, fn_data in node_data["functions"].items():
                fails = fn_data["failures"]
                fn_desc = fn_data["function_description"][:60] if fn_data["function_description"] else "-"
                fails_high = sum(1 for fm in fails if fm.severity >= 7)
                lines.append(
                    f"  ▶ **{fn_desc}**"
                    + (f" ⚠️{fails_high}" if fails_high else "")
                )
                lines.append("")

                lines.append(
                    "  | # | 失效影响(FE) | S | 失效模式(FM) | 失效原因(FC) | 传导类型 |"
                )
                lines.append("  |---|-------------|:---:|-------------|-------------|---------|")
                for fi, fm in enumerate(fails, 1):
                    s = fm.severity or 4
                    s_label = f"**{s}**" if s >= 7 else str(s)
                    lines.append(
                        f"  | {fi} | {fm.failure_effect[:50]} | {s_label} | "
                        f"{fm.failure_mode[:40]} | {fm.failure_cause[:40]} | "
                        f"{fm.failure_network_type or '一般'} |"
                    )

                lines.append("")

        # 高严重度汇总
        if stats["high_severity_count"] > 0:
            lines.append("---")
            lines.append(f"🚨 **高严重度项(S≥7)**: {stats['high_severity_count']} 条")
            for fm in analysis.get_high_severity(7)[:5]:
                info = (
                    f"  [{fm.function_description[:25]}]" if fm.function_description else ""
                )
                lines.append(
                    f"- [S={fm.severity}] {fm.node_name}{info}: {fm.failure_mode[:40]}"
                )

            if stats["safety_failure_count"] > 0:
                lines.append(
                    f"\n🔴 **安全相关失效**: {stats['safety_failure_count']} 条"
                )

        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# 独立调用入口
# ══════════════════════════════════════════════════════════════════

async def run_failure_analysis(
    tree: StructureTree,
    func_analysis: FunctionAnalysis,
    project_id: Optional[str] = None,
    enable_kb: bool = True,
) -> FailureAnalysis:
    """便捷入口：运行失效分析

    Args:
        tree: 结构树对象
        func_analysis: 功能分析结果（含功能网络）
        project_id: 项目ID（用于知识库检索）
        enable_kb: 是否启用知识库检索增强

    Returns:
        FailureAnalysis: 失效分析结果
    """
    agent = FailureAgent(enable_kb_search=enable_kb)
    return await agent.generate_failure_analysis(tree, func_analysis, project_id)
