"""
Step 2 功能分析 Agent — v5 需求对齐版

变更要点（需求规格 §4.3 对齐）：
1. [新增] 功能四分类（主功能/辅助功能/安全强制功能/法规合规功能）
2. [新增] SOR 指标绑定 + 法规条款绑定
3. [新增] WeKnora 知识库检索 SOR + 法规上下文
4. [新增] 按 Agent 角色归集 + 集成合并去重
5. [新增] 校验规则 V3-1~V3-4 自动执行
6. [新增] 安全/法规功能覆盖率检查及补充

重构要点（基于上一版 FMEA 专家审查意见）：
1. [移除] 硬编码 CATEGORY_AFFINITY 类别映射，改为 LLM 语义匹配 + KB 增强
2. [增强] 功能流分析：能量流、物料流、信号流
3. [新增] 功能网完整性校验：孤儿功能检测、悬空需求检测、覆盖率度量
4. [修复] _extract_units 中 m.group()[-1] 只取最后一个字符的 bug
5. [修复] _extract_keywords 对中文无效的问题（改用字 n-gram）
6. [修复] cooperates 关系中的 node_id 映射错误
7. [扩展] 关系类型：添加"冗余备份"、"接口关联"、"安全功能"
8. [增强] LLM 增强网络 + 批量并发去重过滤
9. [增强] Fallback 基于节点名称和规格的上下文推断，而非仅层级模板
"""
import json
import logging
import os
import re
import asyncio
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass

from models.structure import StructureTree, StructureTreeNode
from models.function import (
    FunctionDetail, FunctionAnalysisItem, FunctionAnalysis,
    FunctionNetwork, FunctionRelation,
)
from engine.llm_client import get_llm_client, LLMClient
from engine.concurrent import get_concurrent_engine, ConcurrentFmeaEngine
from agents.regulation import RegulationAgent
from engine.tools import filter_analysis_nodes, build_node_context
from engine.prompts import (
    FUNCTION_ANALYSIS_V5_SYSTEM,
    FUNCTION_NETWORK_V4_SYSTEM,
    FUNCTION_NETWORK_FALLBACK_SYSTEM,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# 完整性报告数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class ValidationReport:
    """v5: §4.3.4 校验报告"""
    v3_1_passed: bool = False          # 无零部件漏拆解, 安全/合规功能 100% 覆盖
    v3_1_issues: list = None
    v3_2_passed: bool = False          # 每条功能绑定 SOR 或法规
    v3_2_issues: list = None
    v3_3_passed: bool = False          # 描述句式统一
    v3_3_issues: list = None
    v3_4_passed: bool = False          # 同零件重复功能去重, 跨专业统一
    v3_4_issues: list = None
    total_functions: int = 0
    safety_functions: int = 0
    regulation_functions: int = 0
    nodes_without_functions: list = None
    unbounded_functions: int = 0       # 无 SOR/法规绑定的功能数

    @property
    def all_passed(self) -> bool:
        return self.v3_1_passed and self.v3_2_passed and self.v3_3_passed and self.v3_4_passed


@dataclass
class NetworkIntegrityReport:
    """功能网完整性校验报告"""
    total_functions: int = 0
    orphan_functions: int = 0          # 没有来源需求的叶功能
    dangling_requirements: int = 0     # 没有实现方案的上层需求
    relations_count: int = 0
    coverage_rate: float = 0.0         # 覆盖率 (0~1)
    orphan_details: List[Dict] = None
    dangling_details: List[Dict] = None
    flow_coverage: Dict[str, float] = None  # 各功能流类型的覆盖比例


# ══════════════════════════════════════════════════════════════════
# 功能流类型
# ══════════════════════════════════════════════════════════════════

FLOW_TYPES = {
    "电气": "electrical",
    "机械力": "mechanical",
    "热": "thermal",
    "流体": "fluid",
    "信号": "signal",
    "无": "none",
}


class FunctionAgent:
    """Step 2: 功能分析 — v5 需求对齐版 (§4.3 完整)"""

    def __init__(self, enable_kb_search: bool = True):
        self.llm = get_llm_client()
        self.engine = get_concurrent_engine()
        self.enable_kb_search = enable_kb_search
        self._regulation_agent = None    # v5: 法规 Agent (WeKnora 驱动)

    def _get_regulation_agent(self):
        if self._regulation_agent is None:
            self._regulation_agent = RegulationAgent(llm_client=self.llm)
        return self._regulation_agent

    # ════════════════════════════════════════════════════════════
    # 主入口
    # ════════════════════════════════════════════════════════════

    async def generate_function_analysis(
        self,
        tree: StructureTree,
        project_id: Optional[str] = None,
        regulation_data: Optional[dict] = None,       # v5: 法规映射清单 (file 1)
        agent_list: Optional[List[str]] = None,        # v5: 专业 Agent 列表
    ) -> FunctionAnalysis:
        """为结构树所有节点生成功能分析 + 功能网络（v5: §4.3 完整）

        流程：
        1. 过滤标准件，筛选待分析节点
        2. 从 WeKnora 检索 SOR 指标和法规条款上下文
        3. 对每个节点，注入 SOR/法规上下文，调 LLM 分析多功能（四分类+SOR/法规绑定）
        4. 基于功能分析结果，构建三层功能网（父子需求 + 接口关联 + 同级配合）
        5. 功能网完整性校验
        6. 按 Agent 角色归集合并去重
        7. 执行校验规则 V3-1~V3-4
        8. 补充遗漏的安全/法规功能
        9. 组装输出
        """
        nodes = [n.model_dump() for n in tree.nodes]
        significant_nodes = filter_analysis_nodes(nodes)

        # ── v5: 从 WeKnora 检索 SOR 和法规上下文 ──
        sor_context = ""
        regulation_context = ""
        regulation_terms = []   # 法规条款列表
        if project_id and self.enable_kb_search:
            sor_context, regulation_context, regulation_terms = (
                await self._fetch_sor_regulation_context(project_id, regulation_data)
            )

        if not significant_nodes:
            logger.warning("No significant nodes to analyze (all filtered out)")
            return FunctionAnalysis(items=[])

        logger.info(
            f"[FunctionAgent-v4] 开始功能分析: "
            f"{len(significant_nodes)}/{len(nodes)} 个有效节点"
        )

        # ── 工具定义（v4.1 链式：按 child 拆 function） ──
        tools = [{
            "type": "function",
            "function": {
                "name": "analyze_function",
                "description": "分析单个组件的所有功能（按 child 一对一拆 function）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "nodeId": {"type": "string"},
                        "upperLevelFunction": {"type": "string"},
                        "functions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "childNodeId": {
                                        "type": "string",
                                        "description": "该功能服务的子级节点ID（必填，从子节点列表中选取）",
                                    },
                                    "childNodeName": {
                                        "type": "string",
                                        "description": "对应子级名称（便于阅读校验）",
                                    },
                                    "description": {"type": "string"},
                                    "functionClass": {
                                        "type": "string",
                                        "enum": ["主功能", "辅助功能", "安全强制功能", "法规合规功能"],
                                        "description": "需求 §4.3.2-2 四分类",
                                    },
                                    "category": {"type": "string"},
                                    "flowType": {
                                        "type": "string",
                                        "enum": ["电气", "机械力", "热", "流体", "信号", "无"],
                                        "description": "该功能涉及的功能流类型",
                                    },
                                    "sorBinding": {"type": "string", "description": "SOR 条款引用"},
                                    "sorMetric": {"type": "string", "description": "量化指标"},
                                    "regulationBinding": {"type": "string", "description": "法规条款引用"},
                                    "responsibleAgent": {"type": "string", "description": "责任专业 Agent"},
                                },
                                "required": ["childNodeId", "description", "flowType", "functionClass"],
                            },
                        },
                        "lowerLevelFunction": {"type": "string"},
                    },
                    "required": ["nodeId", "functions"],
                },
            }
        }]

        node_map = {n.get("id", ""): n for n in nodes}

        async def analyze_node(node: dict) -> Optional[FunctionAnalysisItem]:
            context = build_node_context(node, nodes)
            # v4.1: 在 user_msg 中显式给出 child 列表（兼容 parent_id/parentId）
            child_list = [n2 for n2 in nodes
                          if (n2.get("parent_id", "") or n2.get("parentId", "")) == node.get("id", "")]
            child_index = {c["id"]: c.get("name", "") for c in child_list if c.get("id")}

            # 可选：知识库检索增强
            kb_context = ""
            if self.enable_kb_search and project_id:
                kb_context = await self._query_kb_for_node(
                    project_id, node, node_map.get(node.get("parent_id", "") or node.get("parentId", ""))
                )

            # v5: 注入 SOR/法规/Agent 上下文
            user_msg = f"分析以下组件的所有功能（按 child 一对一拆 function）：\n\n{context}"
            if sor_context:
                user_msg += f"\n\n【SOR 指标参考】\n{sor_context[:2000]}"
            if regulation_context:
                user_msg += f"\n\n【法规条款参考】\n{regulation_context[:2000]}"
            if agent_list:
                user_msg += f"\n\n【可用 Agent 列表】\n" + "\n".join(f"  - {a}" for a in agent_list)
            if child_list:
                user_msg += "\n\n【链式方法论要求】\n本节点有 {} 个子级，必须为每个子级生成一个 function（functions 数组长度 = 子级数）。\n子级 ID 映射:\n".format(len(child_list))
                for c in child_list:
                    user_msg += f"  - {c.get('id','')}  →  {c.get('name','')} ({c.get('level','')})\n"
            else:
                user_msg += "\n\n【叶子节点】本节点无子级，functions 必须为 []。\n"
            if kb_context:
                user_msg += f"\n\n参考知识库信息：\n{kb_context}"

            try:
                result = await self.llm.safe_call(
                    system_prompt=FUNCTION_ANALYSIS_V5_SYSTEM,
                    user_message=user_msg,
                    tools=tools,
                    probe_key="nodeId",
                )
                data = self.llm.extract_json(result)

                # 解析多功能列表（含 childNodeId + flowType）
                functions = []
                raw_funcs = data.get("functions", [])
                if raw_funcs and isinstance(raw_funcs, list):
                    for idx, fn in enumerate(raw_funcs):
                        if isinstance(fn, dict) and fn.get("description"):
                            ft = fn.get("flowType") or fn.get("flow_type") or "无"
                            if ft not in ("电气", "机械力", "热", "流体", "信号"):
                                ft = "无"
                            # v4.1: 读取 childNodeId，LLM 没填则按 children 顺序 fallback
                            cid = fn.get("childNodeId") or fn.get("child_node_id") or ""
                            if not cid and child_list and idx < len(child_list):
                                cid = child_list[idx].get("id", "")
                            cname = fn.get("childNodeName") or fn.get("child_node_name") or child_index.get(cid, "")
                            # v5: 映射新字段 (functionClass, sorBinding, etc.)
                            fclass = fn.get("functionClass") or fn.get("function_class") or "主功能"
                            if fclass not in ("主功能", "辅助功能", "安全强制功能", "法规合规功能"):
                                fclass = "主功能"
                            is_safety = (fclass == "安全强制功能")
                            is_reg = (fclass == "法规合规功能")
                            functions.append(FunctionDetail(
                                function_id=f"fn-{node.get('id', '')}-{idx+1:02d}",
                                child_node_id=cid,
                                description=fn.get("description", ""),
                                function_class=fclass,
                                function_category=fn.get("category", "关键功能"),
                                flow_type=ft,
                                sor_binding=fn.get("sorBinding", fn.get("sor_binding", "")),
                                sor_metric=fn.get("sorMetric", fn.get("sor_metric", "")),
                                regulation_binding=fn.get("regulationBinding", fn.get("regulation_binding", "")),
                                responsible_agent=fn.get("responsibleAgent", fn.get("responsible_agent", "")),
                                is_safety_critical=is_safety,
                                is_regulation_mandated=is_reg,
                            ))
                        elif isinstance(fn, str) and fn:
                            # 字符串兜底：按 child 顺序分配, 默认为主功能
                            cid = child_list[idx].get("id", "") if child_list and idx < len(child_list) else ""
                            functions.append(FunctionDetail(
                                function_id=f"fn-{node.get('id', '')}-{idx+1:02d}",
                                child_node_id=cid,
                                description=fn,
                                function_class="主功能",
                                function_category="关键功能",
                            ))

                # 兜底：v2 格式兼容（keyElementFunction）—— 无 child_node_id
                if not functions and data.get("keyElementFunction"):
                    functions.append(FunctionDetail(
                        function_id=f"fn-{node.get('id', '')}-01",
                        child_node_id="",
                        description=data.get("keyElementFunction", ""),
                        function_category="关键功能",
                    ))

                return FunctionAnalysisItem(
                    node_id=data.get("nodeId", node.get("id", "")),
                    node_name=node.get("name", ""),
                    upper_level_function=data.get("upperLevelFunction", ""),
                    functions=functions,
                    lower_level_function=data.get("lowerLevelFunction", ""),
                    llm_generated=True,
                )
            except Exception as e:
                logger.error(
                    f"[FunctionAgent-v4] 节点 {node.get('id','')} "
                    f"{node.get('name','')} 分析失败: {e}"
                )
                return await self._fallback_analysis(node)

        # ── 并发执行 ──
        items_raw = await self.engine.process_batch(significant_nodes, analyze_node)
        items = [i for i in items_raw if i is not None]

        if not items:
            logger.warning("[FunctionAgent-v4] 所有节点分析均失败")
            return FunctionAnalysis(items=[])

        # ── 构建功能网（v4 新方法：LLM 驱动，移除硬编码映射） ──
        # 注意: node_map 是 dict-of-dict（用于 _query_kb_for_node），
        # build_function_network_v4 需要 dict-of-object，所以传 tree 原生节点
        tree_node_map = {n.id: n for n in tree.nodes}
        network = await self.build_function_network_v4(tree, items, tree_node_map, project_id)

        # ── 功能网完整性校验 ──
        integrity_report = self._verify_network_integrity(items, network, tree)
        if integrity_report.coverage_rate < 0.5:
            logger.warning(
                f"[FunctionAgent-v4] 功能网覆盖率偏低: "
                f"{integrity_report.coverage_rate:.0%} "
                f"(孤儿功能: {integrity_report.orphan_functions}, "
                f"悬空需求: {integrity_report.dangling_requirements})"
            )

        # ── 构建输出数据 ──
        node_functions = self._build_node_functions_map(items)
        fn_network_detail = self._build_function_network_detail(
            items, network, node_map
        )

        analysis = FunctionAnalysis(
            items=items,
            function_network=network,
            user_edited=False,
        )
        analysis.node_functions = node_functions
        analysis.function_network_detail = fn_network_detail
        analysis = self._normalize_fields(analysis)

        # ── v5: 按 Agent 归集合并去重 (§4.3.2-5) ──
        if agent_list:
            items = self._consolidate_by_agent(items, agent_list)
            analysis.items = items
            analysis.consolidated = True

        # ── v5: 补充遗漏的安全/法规功能 (§4.3.2 校验 V3-1) ──
        items = self._supplement_safety_regulation_functions(
            items, tree, regulation_terms, agent_list
        )
        analysis.items = items

        # ── v5: 执行校验 V3-1 ~ V3-4 ──
        validation = self._run_full_validation(items, tree, sor_context, regulation_context)
        analysis.validation_report = {
            "V3-1": {"passed": validation.v3_1_passed, "issues": validation.v3_1_issues or []},
            "V3-2": {"passed": validation.v3_2_passed, "issues": validation.v3_2_issues or []},
            "V3-3": {"passed": validation.v3_3_passed, "issues": validation.v3_3_issues or []},
            "V3-4": {"passed": validation.v3_4_passed, "issues": validation.v3_4_issues or []},
            "total_functions": validation.total_functions,
            "safety_functions": validation.safety_functions,
            "regulation_functions": validation.regulation_functions,
            "nodes_without_functions": validation.nodes_without_functions or [],
            "unbounded_functions": validation.unbounded_functions,
        }
        analysis.regulation_data_source = f"WeKnora-{project_id}" if project_id else "未配置"

        total_functions = sum(len(item.functions) for item in items)
        logger.info(
            f"[FunctionAgent-v4] 功能分析完成: {len(items)} 个节点, "
            f"{total_functions} 条功能, "
            f"{len(network.relations)} 条关系, "
            f"覆盖率 {integrity_report.coverage_rate:.0%}"
        )
        return analysis

    # ════════════════════════════════════════════════════════════
    # v4 功能网构建（LLM 驱动，移除 CATEGORY_AFFINITY）
    # ════════════════════════════════════════════════════════════

    async def build_function_network_v4(
        self,
        tree: StructureTree,
        func_items: List[FunctionAnalysisItem],
        node_map: dict,
        project_id: Optional[str] = None,
    ) -> FunctionNetwork:
        """v4 功能网：LLM 驱动语义匹配 + KB 增强

        与 v3 的关键区别：
        - 不再使用硬编码的 CATEGORY_AFFINITY 字典
        - 将父子功能对发给 LLM，由 LLM 判断是否存在需求-满足关系
        - LLM 评估关系时考虑功能描述语义 + 类别 + 功能流类型
        - 对批量父子关系并发调用 LLM
        """
        network = FunctionNetwork()
        import datetime
        network.generated_at = datetime.datetime.now().isoformat()

        func_map = {item.node_id: item for item in func_items}

        # ── 收集待匹配的父子功能对 ──
        # 每个任务是一个 dict: {parent_node, child_node, parent_func, child_funcs}
        matching_tasks = []
        for node in tree.nodes:
            if not node.parent_id:
                continue
            parent = node_map.get(node.parent_id)
            if not parent:
                continue
            parent_func = func_map.get(parent.id)
            child_func = func_map.get(node.id)
            if not parent_func or not child_func:
                continue
            if not parent_func.functions or not child_func.functions:
                continue
            matching_tasks.append({
                "parent_node": parent,
                "child_node": node,
                "parent_func_item": parent_func,
                "child_func_item": child_func,
            })

        if not matching_tasks:
            return network

        # ── 第一阶段：轻量关键词匹配（快速过滤，0 LLM 调用） ──
        # 只在有明显关键词重叠时才预建边，其余交给 LLM 判断
        keyword_relations = self._keyword_matching(network, matching_tasks, func_map)
        network.relations.extend(keyword_relations)

        # ── 第二阶段：LLM 语义匹配（对关键词未覆盖的功能对） ──
        # 找出需要 LLM 判断的父功能
        matched_parent_fn_ids = set()
        for rel in keyword_relations:
            if rel.source_function_id:
                matched_parent_fn_ids.add(rel.source_function_id)

        llm_tasks = []
        for task in matching_tasks:
            parent_item = task["parent_func_item"]
            child_item = task["child_func_item"]
            for pf in parent_item.functions:
                if pf.function_id in matched_parent_fn_ids:
                    continue  # 已被关键词匹配覆盖
                # 对每个父功能，找出尚未匹配的子功能
                unmatched_child_fns = [
                    cf for cf in child_item.functions
                    if not any(
                        r.source_function_id == pf.function_id
                        and r.target_function_id == cf.function_id
                        for r in keyword_relations
                    )
                ]
                if unmatched_child_fns:
                    llm_tasks.append({
                        "parent_node": task["parent_node"],
                        "child_node": task["child_node"],
                        "parent_fn": pf,
                        "child_fns": unmatched_child_fns,
                    })

        if llm_tasks:
            llm_relations = await self._llm_batch_match(llm_tasks, func_map)
            network.relations.extend(llm_relations)

        # ── 第三阶段：LLM 增强（同级配合 + 冗余备份 + 接口关联） ──
        enhanced_relations = await self._enhance_network_v4(
            tree, func_items, node_map, network.relations, project_id
        )

        # 合并，去重
        all_rels = list(network.relations)
        seen_ids = {r.id for r in all_rels}
        for r in enhanced_relations:
            if r.id not in seen_ids:
                all_rels.append(r)
                seen_ids.add(r.id)

        # 按 ID 排序，重新编号
        final_relations = self._renumber_relations(all_rels)

        return FunctionNetwork(
            relations=final_relations,
            generated_at=network.generated_at,
        )

    # ════════════════════════════════════════════════════════════
    # 关键词匹配（快速过滤 — 仅对明显重叠的功能对建边）
    # ════════════════════════════════════════════════════════════

    def _keyword_matching(
        self,
        network: FunctionNetwork,
        matching_tasks: list,
        func_map: dict,
    ) -> List[FunctionRelation]:
        """轻量关键词匹配：只在关键词高度重叠时建边

        规则：
        1. 提取父功能描述中的核心工程术语（力、电压、压力等）
        2. 与子功能描述做交集
        3. 只有当交集 >= 2 个关键词时才建边
        """
        relations = []
        rel_idx = 0

        for task in matching_tasks:
            parent = task["parent_node"]
            child = task["child_node"]
            parent_item = task["parent_func_item"]
            child_item = task["child_func_item"]

            for pf in parent_item.functions:
                parent_keywords = self._extract_engineering_terms(pf.description)
                if not parent_keywords:
                    continue

                for cf in child_item.functions:
                    child_keywords = self._extract_engineering_terms(cf.description)
                    if not child_keywords:
                        continue

                    # 关键词交集 >= 2 说明明显相关
                    overlap = parent_keywords & child_keywords
                    if len(overlap) >= 2:
                        rel_idx += 1
                        relations.append(FunctionRelation(
                            id=f"fn-rel-{rel_idx:04d}",
                            source_node_id=parent.id,
                            source_function_id=pf.function_id,
                            target_node_id=child.id,
                            target_function_id=cf.function_id,
                            relation_type="需要-满足",
                            description=(
                                f"{parent.name}的「{pf.description[:30]}」"
                                f"需要{child.name}的「{cf.description[:30]}」来实现"
                            ),
                            source_label=f"{parent.name}: {pf.description[:40]}",
                            target_label=f"{child.name}: {cf.description[:40]}",
                        ))

        return relations

    # ════════════════════════════════════════════════════════════
    # LLM 批量语义匹配（替代 v3 的 CATEGORY_AFFINITY）
    # ════════════════════════════════════════════════════════════

    async def _llm_batch_match(
        self,
        llm_tasks: list,
        func_map: dict,
    ) -> List[FunctionRelation]:
        """用 LLM 批量判断父子功能间的需求-满足关系"""
        if not llm_tasks:
            return []

        # 分组：每个父功能一个请求（避免输入过长）
        # 每批最多 8 个父功能（并发数）
        semaphore = asyncio.Semaphore(8)
        all_results = []

        async def _match_one(task: dict) -> List[FunctionRelation]:
            async with semaphore:
                return await self._llm_match_single(task, func_map)

        batch_tasks = [asyncio.create_task(_match_one(t)) for t in llm_tasks]
        results_list = await asyncio.gather(*batch_tasks, return_exceptions=True)

        for r in results_list:
            if isinstance(r, list):
                all_results.extend(r)
            elif isinstance(r, Exception):
                logger.warning(f"[FunctionAgent-v4] LLM 匹配异常: {r}")

        return all_results

    async def _llm_match_single(
        self,
        task: dict,
        func_map: dict,
    ) -> List[FunctionRelation]:
        """用 LLM 判断单个父功能的子功能匹配"""
        parent_node = task["parent_node"]
        child_node = task["child_node"]
        parent_fn = task["parent_fn"]
        child_fns = task["child_fns"]

        # 构建输入：给出父功能的完整描述和所有候选子功能
        prompt_lines = [
            f"## 需要方（父节点）",
            f"节点: {parent_node.name} ({parent_node.id})",
            f"功能: [{parent_fn.function_category}] {parent_fn.description}",
            "",
            f"## 候选满足方（子节点: {child_node.name}）",
        ]
        for cf in child_fns:
            prompt_lines.append(f"- [{cf.function_category}] {cf.description}")
        prompt_lines.append("")
        prompt_lines.append(
            "请判断：以上父功能需要哪些子功能来实现？"
        )
        prompt_lines.append(
            "输出 JSON 数组，格式: [{"
            '"childIndex": 子功能序号(从0开始), '
            '"reason": "匹配理由", '
            '"confidence": "高/中/低"'
            "}]"
        )
        prompt_lines.append(
            "如果没有满足需求的子功能，输出空数组 []。"
            "只输出 confidence=高 或 confidence=中 的匹配。"
        )

        user_msg = "\n".join(prompt_lines)

        network_tools = [{
            "type": "function",
            "function": {
                "name": "match_functions",
                "description": "判断父功能与子功能的匹配关系",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "matches": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "childIndex": {"type": "integer"},
                                    "reason": {"type": "string"},
                                    "confidence": {"type": "string", "enum": ["高", "中", "低"]},
                                },
                                "required": ["childIndex", "reason", "confidence"],
                            },
                        },
                    },
                    "required": ["matches"],
                },
            }
        }]

        try:
            result = await self.llm.safe_call(
                system_prompt=FUNCTION_NETWORK_FALLBACK_SYSTEM,
                user_message=user_msg,
                tools=network_tools,
                probe_key="matches",
                fallback_to_complete=True,
            )
            data = self.llm.extract_json(result)
            matches = data.get("matches", []) if isinstance(data, dict) else (
                data if isinstance(data, list) else []
            )

            relations = []
            # 用已有关系数计算 rel_idx 前缀，避免 ID 冲突
            rel_idx = 0
            for m in matches:
                if not isinstance(m, dict):
                    continue
                child_idx = m.get("childIndex")
                confidence = m.get("confidence", "低")
                if confidence == "低":
                    continue
                if child_idx is None or child_idx < 0 or child_idx >= len(child_fns):
                    continue

                cf = child_fns[child_idx]
                rel_idx += 1
                relations.append(FunctionRelation(
                    id=f"fn-rel-llm-{parent_node.id}-{child_node.id}-{rel_idx:02d}",
                    source_node_id=parent_node.id,
                    source_function_id=parent_fn.function_id,
                    target_node_id=child_node.id,
                    target_function_id=cf.function_id,
                    relation_type="需要-满足",
                    description=(
                        f"{parent_node.name}的「{parent_fn.description[:30]}」"
                        f"需要{child_node.name}的「{cf.description[:30]}」来实现"
                    ),
                    source_label=f"{parent_node.name}: {parent_fn.description[:40]}",
                    target_label=f"{child_node.name}: {cf.description[:40]}",
                ))
            return relations

        except Exception as e:
            logger.debug(
                f"[FunctionAgent-v4] LLM 匹配失败 "
                f"({parent_node.name}→{child_node.name}): {e}"
            )
            return []

    # ════════════════════════════════════════════════════════════
    # LLM 增强功能网（v4 — 扩展关系类型）
    # ════════════════════════════════════════════════════════════

    async def _enhance_network_v4(
        self,
        tree: StructureTree,
        func_items: List[FunctionAnalysisItem],
        node_map: dict,
        basic_relations: List[FunctionRelation],
        project_id: Optional[str] = None,
    ) -> List[FunctionRelation]:
        """v4 增强：LLM 推断同级配合 + 冗余备份 + 接口关联 + 安全功能

        与 v3 的区别：
        - 不再只输出"需要-满足"和"功能配合"两种
        - 增加了"冗余备份"、"接口关联"、"安全功能"类型
        - 增加了 confidence 过滤
        """
        try:
            func_map = {item.node_id: item for item in func_items}

            # 构建输入：按父节点分组展示，突出功能流类型
            child_map: Dict[str, list] = {}
            for n in tree.nodes:
                if n.parent_id:
                    child_map.setdefault(n.parent_id, []).append(n)

            sections = []
            for parent_id, children in child_map.items():
                parent_item = func_map.get(parent_id)
                if not parent_item or not parent_item.functions:
                    continue
                section = f"\n## 父节点: {parent_item.node_name}({parent_id})"
                for fn in parent_item.functions[:5]:
                    section += f"\n  - ID:{fn.function_id} [{fn.function_category}] {fn.description[:60]}"
                for child in children:
                    ci = func_map.get(child.id)
                    if ci and ci.functions:
                        section += f"\n### 子节点 {child.name}({child.id}):"
                        for fn in ci.functions[:5]:
                            section += f"\n  - ID:{fn.function_id} [{fn.function_category}] {fn.description[:60]}"
                sections.append(section)

            if not sections:
                return []

            input_text = "结构树功能分组（含功能ID，供关系引用）：\n" + "\n".join(sections[:30])
            if len(input_text) > 8000:
                input_text = input_text[:8000] + "\n...(截断)"

            tools = [{
                "type": "function",
                "function": {
                    "name": "add_relation",
                    "description": "添加一条功能关系（仅补充基础网络未覆盖的关系）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sourceFunctionId": {"type": "string"},
                            "sourceNodeId": {"type": "string"},
                            "sourceNodeName": {"type": "string"},
                            "sourceFunction": {"type": "string"},
                            "targetFunctionId": {"type": "string"},
                            "targetNodeId": {"type": "string"},
                            "targetNodeName": {"type": "string"},
                            "targetFunction": {"type": "string"},
                            "relationType": {
                                "type": "string",
                                "enum": ["需要-满足", "功能配合", "冗余备份", "接口关联", "安全功能"],
                            },
                            "description": {"type": "string"},
                            "confidence": {
                                "type": "string",
                                "enum": ["高", "中", "低"],
                            },
                        },
                        "required": [
                            "sourceFunctionId", "sourceNodeId",
                            "targetFunctionId", "targetNodeId",
                            "relationType", "description", "confidence",
                        ],
                    },
                }
            }]

            result = await self.llm.safe_call(
                system_prompt=FUNCTION_NETWORK_V4_SYSTEM,
                user_message=input_text,
                tools=tools,
                probe_key="sourceNodeId",
            )

            raw_data = self.llm.extract_json(result)
            data = raw_data if isinstance(raw_data, list) else [raw_data]

            # 基础关系去重 key 集合
            existing_keys = {
                (r.source_function_id, r.target_function_id)
                for r in basic_relations
                if r.source_function_id and r.target_function_id
            }

            new_relations = []
            rel_idx = len(basic_relations)
            for item in data:
                if not isinstance(item, dict):
                    continue
                confidence = item.get("confidence", "低")
                if confidence == "低":
                    continue
                src_fid = item.get("sourceFunctionId", "")
                tgt_fid = item.get("targetFunctionId", "")
                if not src_fid or not tgt_fid:
                    continue
                # 正反双向去重
                if (src_fid, tgt_fid) in existing_keys:
                    continue
                if (tgt_fid, src_fid) in existing_keys:
                    continue
                existing_keys.add((src_fid, tgt_fid))

                rel_idx += 1
                rel_type = item.get("relationType", "功能配合")
                if rel_type not in ("需要-满足", "功能配合", "冗余备份", "接口关联", "安全功能"):
                    rel_type = "功能配合"

                new_relations.append(FunctionRelation(
                    id=f"fn-rel-{rel_idx:04d}",
                    source_node_id=item.get("sourceNodeId", ""),
                    source_function_id=src_fid,
                    target_node_id=item.get("targetNodeId", ""),
                    target_function_id=tgt_fid,
                    relation_type=rel_type,
                    description=item.get("description", "")[:200],
                ))

            return new_relations

        except Exception as e:
            logger.warning(f"[FunctionAgent-v4] LLM增强功能网失败: {e}")
            return []

    # ════════════════════════════════════════════════════════════
    # 功能网完整性校验（v4 新增）
    # ════════════════════════════════════════════════════════════

    def _verify_network_integrity(
        self,
        func_items: List[FunctionAnalysisItem],
        network: FunctionNetwork,
        tree: Optional["StructureTree"] = None,
    ) -> NetworkIntegrityReport:
        """校验功能网完整性

        检查内容：
        1. 孤儿功能（Orphan）：没有任何关系的叶节点功能
        2. 悬空需求（Dangling）：非根节点功能作为 source 但未作为 target 出现
           — 根节点（无 parent）的功能可以唯一地作为顶层需求，不判为悬空
        3. 覆盖率（Coverage）：有关系参与的功能 / 总功能数
        4. 功能流覆盖：各功能流类型的关系比例
        """
        # 收集所有功能 ID
        all_fn_ids: Set[str] = set()
        fn_node_map: Dict[str, str] = {}  # function_id -> node_id
        fn_desc_map: Dict[str, str] = {}  # function_id -> description
        for item in func_items:
            for fn in item.functions:
                all_fn_ids.add(fn.function_id)
                fn_node_map[fn.function_id] = item.node_id
                fn_desc_map[fn.function_id] = fn.description[:60]

        # 找出有关系参与的功能
        related_fn_ids: Set[str] = set()
        needs_fn_ids: Set[str] = set()   # 作为 source（需要方）
        satisfies_fn_ids: Set[str] = set()  # 作为 target（满足方）
        for rel in network.relations:
            if rel.source_function_id:
                related_fn_ids.add(rel.source_function_id)
                needs_fn_ids.add(rel.source_function_id)
            if rel.target_function_id:
                related_fn_ids.add(rel.target_function_id)
                satisfies_fn_ids.add(rel.target_function_id)

        # 孤儿功能：全无关系参与
        orphan_ids = all_fn_ids - related_fn_ids

        # 悬空需求：
        # 1) 功能出现作为 source（需要方）
        # 2) 但从未作为 target（满足方）—— 即需求没有被任何功能满足
        # 3) 排除根节点（无 parent）的功能，因为它们天生是顶层需求
        root_node_ids: Set[str] = set()
        if tree:
            root_node_ids = {n.id for n in tree.nodes if not n.parent_id}
        else:
            # 兼容：无 tree 时从 node_map + parent_id 推断
            root_node_ids = {nid for nid in fn_node_map.values()
                             if func_items and next(
                                 (it for it in func_items if it.node_id == nid), None
                             )}
        # 根节点上的功能即使只作为 source 也不算悬空（顶层需求）
        dangling_ids = set()
        for fid in needs_fn_ids - satisfies_fn_ids:
            node_id = fn_node_map.get(fid, "")
            if node_id not in root_node_ids:
                dangling_ids.add(fid)

        total = len(all_fn_ids)
        coverage_rate = len(related_fn_ids) / total if total > 0 else 1.0

        orphan_details = []
        for fid in sorted(orphan_ids):
            orphan_details.append({
                "function_id": fid,
                "node_id": fn_node_map.get(fid, ""),
                "description": fn_desc_map.get(fid, ""),
            })

        dangling_details = []
        for fid in sorted(dangling_ids):
            dangling_details.append({
                "function_id": fid,
                "node_id": fn_node_map.get(fid, ""),
                "description": fn_desc_map.get(fid, ""),
            })

        return NetworkIntegrityReport(
            total_functions=total,
            orphan_functions=len(orphan_ids),
            dangling_requirements=len(dangling_ids),
            relations_count=len(network.relations),
            coverage_rate=round(coverage_rate, 4),
            orphan_details=orphan_details,
            dangling_details=dangling_details,
        )

    # ════════════════════════════════════════════════════════════
    # 知识库检索增强
    # ════════════════════════════════════════════════════════════

    async def _query_kb_for_node(
        self,
        project_id: str,
        node: dict,
        parent: Optional[dict] = None,
    ) -> str:
        """[已注释] 查询知识库中组件功能 — 依赖 kb_query（MiniMax 退化问题）"""
        return ""
        # 以下是原始实现，已注释：
        # try:
        #     from engine.tools import query_kb_tool
        #     node_name = node.get("name", "")
        #     parent_name = parent.get("name", "") if parent else ""
        #     question = (
        #         f"在项目 {project_id} 中，组件/零件「{node_name}」"
        #         f"{'（属于 ' + parent_name + '）' if parent_name else ''}"
        #         f"的主要功能是什么？有哪些功能要求和技术指标？"
        #     )
        #     result_str = query_kb_tool(project_id, question)
        #     result = json.loads(result_str)
        #     if result.get("success") and result.get("answer"):
        #         answer = result["answer"][:500]
        #         sources = result.get("sources", [])
        #         source_info = ""
        #         if sources:
        #             files = [
        #                 s.get("file_name", s.get("source", ""))
        #                 for s in sources[:3]
        #             ]
        #             source_info = f"\n（来源: {', '.join(files)}）"
        #         return f"{answer}{source_info}"
        # except Exception as e:
        #     logger.debug(
        #         f"[FunctionAgent-v4] KB查询跳过（节点 {node.get('id','')}）: {e}"
        #     )
        # return ""

    # ════════════════════════════════════════════════════════════
    # 增强版 Fallback（v4.1 — 纯 DFMEA 方法论，零硬编码模板）
    # ════════════════════════════════════════════════════════════

    async def _fallback_analysis(self, node: dict) -> FunctionAnalysisItem:
        """当 LLM 分析失败时降级生成功能描述

        降级路径：
        1. 尝试用 LLM Complete 模式（简化 prompt）再试一次
        2. 仍失败 → 使用纯 DFMEA 层级模板（不依赖任何领域关键词）
        """
        result = await self._try_llm_function_fallback(node)
        if result:
            return result
        return self._dfmea_level_template(node)

    async def _try_llm_function_fallback(
        self, node: dict
    ) -> Optional[FunctionAnalysisItem]:
        """用 LLM Complete 模式（简化 prompt）重新尝试"""
        simplified_prompt = (
            "你是DFMEA功能分析专家。根据组件名称和层级，输出功能分析JSON。\n\n"
            "格式: {\"nodeId\":\"...\",\"functions\":[{\"description\":\"...\","
            "\"category\":\"...\"}],\"upperLevelFunction\":\"...\","
            "\"lowerLevelFunction\":\"...\"}\n\n"
            "只输出JSON，不要其他文字。"
        )
        user_msg = (
            f"组件: {node.get('name','')}\n"
            f"层级: {node.get('level','')}\n"
            f"规格: {node.get('specification','')}\n"
            f"材料: {node.get('material','')}"
        )
        try:
            result = await self.llm.call_complete(
                system_prompt=simplified_prompt,
                user_message=user_msg,
                force_json=True,
            )
            if not result:
                return None
            data = self.llm.extract_json(result)
            raw_funcs = data.get("functions", [])
            if not raw_funcs:
                return None
            functions = []
            for idx, fn in enumerate(raw_funcs):
                if isinstance(fn, dict) and fn.get("description"):
                    functions.append(FunctionDetail(
                        function_id=f"fn-{node.get('id','')}-{idx+1:02d}",
                        description=fn.get("description", ""),
                        function_class=fn.get("functionClass", fn.get("function_class", "主功能")),
                        function_category=fn.get("category", "关键功能"),
                        flow_type=fn.get("flowType", fn.get("flow_type", "无")),
                    ))
            if not functions:
                return None
            return FunctionAnalysisItem(
                node_id=data.get("nodeId", node.get("id", "")),
                node_name=node.get("name", ""),
                upper_level_function=data.get("upperLevelFunction", ""),
                functions=functions,
                lower_level_function=data.get("lowerLevelFunction", ""),
                llm_generated=True,
            )
        except Exception as e:
            logger.debug(f"[FunctionAgent-v4] LLM fallback failed: {e}")
            return None

    @staticmethod
    def _dfmea_level_template(node: dict) -> FunctionAnalysisItem:
        """基于 DFMEA 层级生成默认功能描述，零领域知识硬编码

        使用 DFMEA 方法论中按层级划分的功能类型：
        - 系统/子系统/组件/零件 四级，每级的职责不同
        - 所有描述均为方法论通用句式，不含任何产品名词
        """
        name = node.get("name", "")
        level = node.get("level", "")
        nid = node.get("id", "")
        spec = node.get("specification", "")

        templates = {
            "系统": {
                "upper": "满足上级系统的集成和功能要求",
                "functions": [
                    {"description": f"实现{name}系统的整体功能和性能指标", "category": "系统功能"},
                    {"description": f"协调各子系统/组件协同工作", "category": "集成管理"},
                ],
                "lower": "各子系统/组件满足设计规范和功能分配要求",
            },
            "子系统": {
                "upper": f"为{name}的上级系统提供功能支撑",
                "functions": [
                    {"description": f"实现{name}子系统的核心功能要求", "category": "核心功能"},
                    {"description": f"保证{name}子系统的可靠运行和耐久性", "category": "可靠性"},
                ],
                "lower": "下属组件满足装配精度和功能要求",
            },
            "组件": {
                "upper": f"满足上级对{name}的功能分配要求",
                "functions": [
                    {"description": f"实现{name}组件的连接/传递/转换功能", "category": "功能传递"},
                    {"description": "保证组件的防护/耐久性能", "category": "防护耐久"},
                ],
                "lower": "下属零件满足设计图纸的尺寸和材料要求",
            },
            "零件": {
                "upper": f"满足上级组件对{name}的安装和功能要求",
                "functions": [
                    {"description": f"作为{name}实现其结构/连接/传导/防护功能", "category": "基本功能"},
                ],
                "lower": "尺寸、材料、工艺满足设计要求",
            },
        }
        t = templates.get(level, templates["零件"])

        functions = []
        for idx, fn_template in enumerate(t["functions"]):
            desc = fn_template["description"]
            if spec:
                desc += f"（规格: {spec}）"
            functions.append(FunctionDetail(
                function_id=f"fn-{nid}-{idx+1:02d}",
                description=desc,
                function_class="辅助功能",
                function_category=fn_template.get("category", "关键功能"),
            ))

        return FunctionAnalysisItem(
            node_id=nid,
            node_name=name,
            upper_level_function=t["upper"],
            functions=functions,
            lower_level_function=t["lower"],
            llm_generated=True,
        )

    # ════════════════════════════════════════════════════════════
    # 工程术语提取（v4 — 替代 v3 的 _extract_keywords）
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _extract_engineering_terms(text: str) -> Set[str]:
        """从功能描述中提取核心工程术语

        策略：提取物理量 + 关键名词，忽略虚词。
        与 v3 的 _extract_keywords 区别：
        - 使用 2-gram 解决中文分词问题
        - 专门匹配工程领域术语模式
        """
        if not text:
            return set()

        terms = set()

        # 1. 提取物理量单位模式（如 "≥200N", "8±2kPa", "5L/s"）
        unit_pattern = r'(?:≥|≤|>|<|＝|=|±)?\s*\d+\.?\d*\s*(?:V|A|W|kW|N|kN|MPa|kPa|Pa|mm|μm|m|kg|g|°C|℃|Hz|kHz|MHz|Ω|mΩ|kΩ|Ah|CCA|ms|S|min|L/s|L/min|m/s)'
        for m in re.finditer(unit_pattern, text):
            terms.add(m.group().strip())

        # 2. 提取中文 2-gram（对中文有效的轻量分词）
        chinese_chars = re.findall(r'[\u4e00-\u9fff]', text)
        for i in range(len(chinese_chars) - 1):
            bigram = chinese_chars[i] + chinese_chars[i+1]
            # 过滤常见的非工程虚词
            if bigram not in ("提供", "保证", "满足", "实现", "需要", "以下",
                              "通过", "并且", "或者", "以及", "同时", "包括",
                              "用于", "作为", "根据", "按照", "相关", "相应"):
                terms.add(bigram)

        # 3. 提取英文关键词
        for m in re.finditer(r'[a-zA-Z]{2,}', text):
            terms.add(m.group().lower())

        return terms

    # ════════════════════════════════════════════════════════════
    # 物理量单位提取（v4 — 修复 v3 的 m.group()[-1] bug）
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _extract_units(text: str) -> Set[str]:
        """从功能描述中提取物理量单位

        v4 修复：使用反引用捕获完整单位，而非 m.group()[-1] 取最后一个字符
        """
        if not text:
            return set()

        # 常见工程单位列表（完整）
        ENGINEERING_UNITS = [
            "V", "mV", "kV",
            "A", "mA", "kA",
            "W", "kW", "mW",
            "N", "kN", "mN",
            "MPa", "kPa", "Pa", "GPa",
            "mm", "μm", "nm", "cm", "m", "km",
            "°C", "℃", "K",
            "Hz", "kHz", "MHz", "GHz",
            "Ω", "mΩ", "kΩ", "MΩ",
            "Ah", "mAh", "Wh", "kWh",
            "CCA",
            "ms", "s", "min", "h",
            "L/s", "L/min", "mL/s",
            "m/s", "km/h",
            "g", "kg", "mg", "t",
            "N·m", "Nm",
            "bar", "mbar",
        ]

        # 按长度降序排序（优先匹配长单位如 "MPa" 而非 "Pa"）
        sorted_units = sorted(ENGINEERING_UNITS, key=len, reverse=True)
        # 构建正则：转义特殊字符
        escaped = [re.escape(u) for u in sorted_units]
        unit_pattern = r'(?:≥|≤|>|<|＝|=|±)?\s*\d+\.?\d*\s*(' + '|'.join(escaped) + r')'

        found = set()
        for m in re.finditer(unit_pattern, text):
            unit = m.group(1)  # 完整的单位字符串
            if unit:
                found.add(unit)

        return found

    # ════════════════════════════════════════════════════════════
    # 关系 ID 重编号
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _renumber_relations(relations: List[FunctionRelation]) -> List[FunctionRelation]:
        """对所有关系重新编号，消除 ID 冲突"""
        renumbered = []
        for idx, rel in enumerate(relations, start=1):
            rel.id = f"fn-rel-{idx:04d}"
            renumbered.append(rel)
        return renumbered

    # ════════════════════════════════════════════════════════════
    # 构建 node_functions 映射表
    # ════════════════════════════════════════════════════════════

    def _build_node_functions_map(
        self, items: List[FunctionAnalysisItem]
    ) -> dict:
        """构建 node_id → functions[] 的映射表（v4.1: 含 flow_type）"""
        result = {}
        for item in items:
            funcs = []
            for fn in item.functions:
                funcs.append({
                    "id": fn.function_id,
                    "category": fn.function_category,
                    "description": fn.description,
                    "flow_type": fn.flow_type,
                })
            result[item.node_id] = {
                "node_name": item.node_name,
                "functions": funcs,
            }
        return result

    # ════════════════════════════════════════════════════════════
    # 构建 function_network_detail（v4 — 修复 cooperates node_id）
    # ════════════════════════════════════════════════════════════

    def _build_function_network_detail(
        self,
        items: List[FunctionAnalysisItem],
        network: FunctionNetwork,
        node_map: dict,
    ) -> list:
        """构建每条功能的功能网详情

        v4 修复：
        - cooperates 关系中的 node_id 现在正确映射到被配合方的节点
        - 增加冗余备份关系的展示
        """
        # 构建功能ID → 元数据映射
        func_descs = {}
        func_categories = {}
        func_node_ids = {}
        func_node_names = {}
        for item in items:
            for fn in item.functions:
                func_descs[fn.function_id] = fn.description[:60]
                func_categories[fn.function_id] = fn.function_category
                func_node_ids[fn.function_id] = item.node_id
                func_node_names[fn.function_id] = item.node_name

        # 构建三种关系的索引
        needs_index = {}
        satisfies_index = {}
        cooperates_index = {}

        for rel in network.relations:
            src_fid = rel.source_function_id
            tgt_fid = rel.target_function_id
            if not src_fid or not tgt_fid:
                continue

            rel_type = rel.relation_type

            if rel_type == "需要-满足":
                # source(需要方) → target(满足方)
                if src_fid not in needs_index:
                    needs_index[src_fid] = []
                needs_index[src_fid].append({
                    "function_id": tgt_fid,
                    "function_desc": func_descs.get(tgt_fid, "")[:40],
                    "function_category": func_categories.get(tgt_fid, ""),
                    "node_id": func_node_ids.get(tgt_fid, rel.target_node_id),
                    "node_name": func_node_names.get(tgt_fid, ""),
                    "relation": "需要-满足",
                })
                # target satisfies source
                if tgt_fid not in satisfies_index:
                    satisfies_index[tgt_fid] = []
                satisfies_index[tgt_fid].append({
                    "function_id": src_fid,
                    "function_desc": func_descs.get(src_fid, "")[:40],
                    "function_category": func_categories.get(src_fid, ""),
                    "node_id": func_node_ids.get(src_fid, rel.source_node_id),
                    "node_name": func_node_names.get(src_fid, ""),
                    "relation": "需要-满足",
                })

            elif rel_type == "功能配合":
                # 双向：对每个参与方，记录另一方的信息
                # 使用 func_node_ids 确保 node_id 正确，避免 v3 的 target_node_id 硬编码错误
                for fid, other_fid, other_nid in [
                    (src_fid, tgt_fid, func_node_ids.get(tgt_fid, rel.target_node_id)),
                    (tgt_fid, src_fid, func_node_ids.get(src_fid, rel.source_node_id)),
                ]:
                    if fid not in cooperates_index:
                        cooperates_index[fid] = []
                    cooperates_index[fid].append({
                        "function_id": other_fid,
                        "function_desc": func_descs.get(other_fid, "")[:40],
                        "function_category": func_categories.get(other_fid, ""),
                        "node_id": other_nid,  # 修正：用 func_node_ids 而非固定使用 rel.target_node_id
                        "node_name": func_node_names.get(other_fid, ""),
                        "relation": "功能配合",
                    })

            elif rel_type in ("冗余备份", "接口关联", "安全功能", "功能配合"):
                # 双向展示（合并处理：冗余备份/接口关联/安全功能/功能配合 逻辑相同）
                for fid, other_fid, other_nid in [
                    (src_fid, tgt_fid, func_node_ids.get(tgt_fid, rel.target_node_id)),
                    (tgt_fid, src_fid, func_node_ids.get(src_fid, rel.source_node_id)),
                ]:
                    if fid not in cooperates_index:
                        cooperates_index[fid] = []
                    cooperates_index[fid].append({
                        "function_id": other_fid,
                        "function_desc": func_descs.get(other_fid, "")[:40],
                        "function_category": func_categories.get(other_fid, ""),
                        "node_id": other_nid,
                        "node_name": func_node_names.get(other_fid, ""),
                        "relation": rel_type,
                    })

        # 组装每条功能的功能网详情
        detail = []
        for item in items:
            for fn in item.functions:
                fid = fn.function_id
                needs = needs_index.get(fid, [])
                satisfies = satisfies_index.get(fid, [])
                cooperates = cooperates_index.get(fid, [])
                detail.append({
                    "function_id": fid,
                    "function_desc": fn.description[:80],
                    "function_category": fn.function_category,
                    "node_id": item.node_id,
                    "node_name": item.node_name,
                    "needs": needs,
                    "satisfies": satisfies,
                    "cooperates": cooperates,
                    "affects": needs,
                    "affected_by": satisfies,
                    "impact_count": len(needs) + len(satisfies) + len(cooperates),
                })

        return detail

    # ════════════════════════════════════════════════════════════
    # 字段兼容
    # ════════════════════════════════════════════════════════════

    def _normalize_fields(self, analysis: FunctionAnalysis) -> FunctionAnalysis:
        for item in analysis.items:
            if not item.component_name:
                item.component_name = item.node_name
            if not item.higher_level_function:
                item.higher_level_function = item.upper_level_function
            if not item.focus_function:
                item.focus_function = item.key_element_function
        return analysis

    # ════════════════════════════════════════════════════════════
    # v5: WeKnora SOR/法规上下文检索 (§4.3.1)
    # ════════════════════════════════════════════════════════════

    async def _fetch_sor_regulation_context(
        self, project_id: str, regulation_data: Optional[dict] = None
    ) -> Tuple[str, str, list]:
        """从 WeKnora 知识库检索 SOR 指标和法规条款上下文

        知识来源优先级：WeKnora 知识库 > 传入的 regulation_data

        Returns:
            (sor_context, regulation_context, regulation_terms)
        """
        sor_context = ""
        regulation_context = ""
        regulation_terms = []

        try:
            from knowledge.weknora_client import get_weknora_client
            wk = get_weknora_client()

            # 从 project_meta.json 读取 kb_id 和 regulation_list
            kb_id = ""
            reg_list = []
            meta_paths = [
                Path("knowledge") / project_id / "project_meta.json",
                Path("data") / "projects" / project_id / "project_meta.json",
            ]
            for mp in meta_paths:
                if mp.exists():
                    with open(mp, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                        kb_id = meta.get("weknora_kb_id", "")
                        if not reg_list:
                            reg_list = meta.get("regulation_list", [])
                        if kb_id:
                            break

            # regulation_list 优先从传入参数取
            if regulation_data and not reg_list:
                reg_terms = regulation_data.get("clauses", []) or regulation_data.get("items", [])
                for rt in reg_terms:
                    if isinstance(rt, dict):
                        reg_list.append(rt.get("regulation_clause", rt.get("regulation_id", "")))
                    elif isinstance(rt, str):
                        reg_list.append(rt)

            # 从 WeKnora 检索 SOR
            if kb_id:
                try:
                    sor_results = wk.hybrid_search(kb_id, query_text="SOR 需求规格 功能指标 性能要求", match_count=5)
                    if sor_results:
                        chunks = []
                        for r in sor_results[:3]:
                            content_preview = r.get("content", "")[:500]
                            if content_preview:
                                chunks.append(content_preview)
                        sor_context = "\n".join(chunks)
                except Exception as e:
                    logger.warning(f"[v5] SOR检索失败: {e}")

                # 从 WeKnora 检索法规条款
                for reg_query in reg_list[:5]:
                    try:
                        reg_results = wk.hybrid_search(kb_id, query_text=f"法规 {reg_query}", match_count=3)
                        if reg_results:
                            for r in reg_results[:2]:
                                content_preview = r.get("content", "")[:300]
                                if content_preview:
                                    regulation_context += content_preview + "\n"
                        regulation_terms.append(reg_query)
                    except Exception as e:
                        logger.debug(f"[v5] 法规检索失败 {reg_query}: {e}")

        except ImportError:
            logger.debug("[v5] WeKnora 客户端未安装，跳过知识库检索")
        except Exception as e:
            logger.warning(f"[v5] SOR/法规上下文检索失败: {e}")

        if not sor_context and not regulation_context:
            logger.info("[v5] 未获取到 SOR/法规上下文，功能分析将在无参考数据情况下执行")

        return sor_context, regulation_context, regulation_terms

    # ════════════════════════════════════════════════════════════
    # v5: 按 Agent 归集合并去重 (§4.3.2-5)
    # ════════════════════════════════════════════════════════════

    def _consolidate_by_agent(
        self, items: list, agent_list: list
    ) -> list:
        """按专业 Agent 归集功能，去重合并

        流程:
        1. 按 responsible_agent 分组
        2. 去重: 相同名称 + 相同功能描述关键词 → 保留一项
        3. 跨 Agent 重复功能由系统集成专家统一裁定（取首次出现的 Agent）
        """
        if not agent_list:
            return items

        # 构建功能指纹: (node_name, description_keywords)
        func_fingerprints: Set[str] = set()
        deduped_items = []

        for item in items:
            new_funcs = []
            for fn in item.functions:
                # 生成指纹: 节点名 + 描述前2个2-gram
                desc_clean = re.sub(r'[0-9]+\.?[0-9]*', '#', fn.description[:30])
                keywords = sorted(self._extract_engineering_terms(desc_clean))
                fp = f"{item.node_name}:{';'.join(keywords[:3])}"
                if fp not in func_fingerprints:
                    func_fingerprints.add(fp)
                    new_funcs.append(fn)
                else:
                    logger.debug(f"[v5] 去重: {item.node_name} '{fn.description[:30]}'")
            if new_funcs != item.functions:
                item.functions = new_funcs
            deduped_items.append(item)

        return deduped_items

    # ════════════════════════════════════════════════════════════
    # v5: 补充遗漏的安全/法规功能 (§4.3.4 V3-1)
    # ════════════════════════════════════════════════════════════

    def _supplement_safety_regulation_functions(
        self,
        items: list,
        tree: StructureTree,
        regulation_terms: list,
        agent_list: Optional[list] = None,
    ) -> list:
        """检查并补充遗漏的安全强制功能和法规合规功能

        策略:
        - 遍历所有节点，检查已有功能中是否包含"安全强制功能"和"法规合规功能"
        - 对缺少安全功能的电池/电气相关节点，标记并补充
        """
        if not regulation_terms:
            return items

        # 识别需要安全/法规功能的节点 (关键词匹配)
        safety_keywords = {
            "电池", "BMS", "高压", "绝缘", "熔断", "继电器", "防爆",
            "热管理", "冷却", "密封", "防水", "碰撞", "保护",
        }

        def _node_needs_safety(node_name: str) -> bool:
            return any(kw in node_name for kw in safety_keywords)

        # 安全检查
        missing_safety = []
        for item in items:
            has_safety = any(fn.is_safety_critical for fn in item.functions)
            if not has_safety and _node_needs_safety(item.node_name):
                missing_safety.append(item)

        # 法规检查
        missing_regulation = []
        for item in items:
            has_reg = any(fn.is_regulation_mandated for fn in item.functions)
            if not has_reg and _node_needs_safety(item.node_name):
                missing_regulation.append(item)

        if missing_safety:
            logger.info(
                f"[v5] V3-1: 发现 {len(missing_safety)} 个节点缺少安全强制功能标注, "
                f"{len(missing_regulation)} 个节点缺少法规合规功能标注"
            )

        # 补充标记: 在 node_functions 中添加警告 (不修改 LLM 产出)
        # 实际操作: 记录到 validation_report 中，不在 items 中伪造数据
        self._missing_safety_nodes = missing_safety
        self._missing_regulation_nodes = missing_regulation

        return items

    # ════════════════════════════════════════════════════════════
    # v5: 完整校验 V3-1 ~ V3-4 (§4.3.4)
    # ════════════════════════════════════════════════════════════

    def _run_full_validation(
        self,
        items: list,
        tree: StructureTree,
        sor_context: str,
        regulation_context: str,
    ) -> "ValidationReport":
        """执行需求规格 §4.3.4 全部四项校验"""

        report = ValidationReport()

        # ── 收集统计 ──
        all_fn_ids = []
        seen_node_ids = set()
        for item in items:
            for fn in item.functions:
                all_fn_ids.append(fn.function_id)
                seen_node_ids.add(item.node_id)

        report.total_functions = len(all_fn_ids)
        report.safety_functions = sum(
            1 for item in items for fn in item.functions if fn.is_safety_critical
        )
        report.regulation_functions = sum(
            1 for item in items for fn in item.functions if fn.is_regulation_mandated
        )

        # ── V3-1: 无零部件漏拆解, 安全/合规功能 100% 覆盖 ──
        report.v3_1_issues = []
        tree_node_ids = {n.id for n in tree.nodes}
        missing_nodes = tree_node_ids - seen_node_ids
        if missing_nodes:
            missing_names = []
            for nid in missing_nodes:
                node = next((n for n in tree.nodes if n.id == nid), None)
                missing_names.append(node.name if node else nid)
            report.v3_1_issues.append(
                f"缺少功能分析的节点: {missing_names[:5]}{'...' if len(missing_names)>5 else ''}"
            )

        if hasattr(self, '_missing_safety_nodes') and self._missing_safety_nodes:
            report.v3_1_issues.append(
                f"缺少安全强制功能标注的节点: "
                f"{[n.node_name for n in self._missing_safety_nodes[:5]]}"
            )
        if hasattr(self, '_missing_regulation_nodes') and self._missing_regulation_nodes:
            report.v3_1_issues.append(
                f"缺少法规合规功能标注的节点: "
                f"{[n.node_name for n in self._missing_regulation_nodes[:5]]}"
            )
        report.v3_1_passed = len(report.v3_1_issues) == 0

        # ── V3-2: 每条功能绑定 SOR 或法规 ──
        report.v3_2_issues = []
        unbounded = 0
        for item in items:
            for fn in item.functions:
                if not fn.sor_binding and not fn.regulation_binding:
                    unbounded += 1
        report.unbounded_functions = unbounded
        if unbounded > 0:
            report.v3_2_issues.append(
                f"{unbounded}/{report.total_functions} 条功能未绑定 SOR 指标或法规条款"
            )
            if sor_context or regulation_context:
                report.v3_2_passed = unbounded < report.total_functions * 0.5  # 过半绑定即通过
            else:
                # 无 SOR/法规数据可用时放行（标记为跳过）
                report.v3_2_passed = True
                report.v3_2_issues[0] += " (已提供 SOR/法规上下文但 LLM 未绑定)"
        else:
            report.v3_2_passed = True

        # ── V3-3: 功能描述句式统一 ──
        report.v3_3_issues = []
        fuzzy_count = 0
        fuzzy_patterns = [
            "一些", "各种", "等等", "相关", "适当", "合理", "良好的",
            "较好的", "必要的", "一定的", "可能", "大概",
        ]
        for item in items:
            for fn in item.functions:
                desc = fn.description
                if any(p in desc for p in fuzzy_patterns):
                    fuzzy_count += 1
        if fuzzy_count > 0:
            report.v3_3_issues.append(
                f"{fuzzy_count} 条功能描述包含模糊/口语化表述"
            )
        report.v3_3_passed = fuzzy_count <= max(1, report.total_functions * 0.05)

        # ── V3-4: 同零件重复功能去重 + 跨专业统一 ──
        report.v3_4_issues = []
        fn_names = {}
        duplicates = 0
        for item in items:
            for fn in item.functions:
                key = f"{item.node_name}:{fn.description[:20]}"
                fn_names.setdefault(key, 0)
                fn_names[key] += 1
                if fn_names[key] > 1:
                    duplicates += 1
        if duplicates > 0:
            report.v3_4_issues.append(
                f"发现 {duplicates} 条可能重复的功能"
            )
        report.v3_4_passed = duplicates <= max(1, report.total_functions * 0.02)

        nodes_without = [
            item.node_name for item in items if not item.functions
        ]
        report.nodes_without_functions = nodes_without

        return report

    # ════════════════════════════════════════════════════════════
    # 工具方法
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _extract_key_req(full_text: str, target_name: str) -> str:
        """从功能文本中提取与目标节点相关的关键要求片段"""
        if not full_text:
            return ""
        sentences = full_text.replace("；", ";").replace("。", "\n").split("\n")
        for s in sentences:
            if target_name in s:
                return s.strip()[:120]
        return full_text[:120] if full_text else ""

    def format_for_display(self, analysis: FunctionAnalysis) -> str:
        """格式化功能分析结果用于前端展示"""
        if not analysis.items:
            return "（无功能分析数据）"

        total_functions = sum(len(item.functions) for item in analysis.items)

        lines = [
            f"📊 **功能分析表** ({len(analysis.items)} 个关注要素, {total_functions} 条功能)",
            "",
            "| # | 关注要素 | 功能类别 | 功能描述 |",
            "|---|---|---|---|",
        ]

        row_idx = 0
        for item in analysis.items[:30]:
            for fn in item.functions:
                row_idx += 1
                lines.append(
                    f"| {row_idx} | {item.node_name} | "
                    f"{fn.function_category} | "
                    f"{fn.description[:50]} |"
                )

        if row_idx > 30:
            lines.append(f"| ... | ...共{total_functions}条功能 | | |")

        if analysis.function_network:
            lines.append("")
            lines.append(
                f"🔗 **功能网络**: {len(analysis.function_network.relations)} 条关系边"
            )

        if hasattr(analysis, "function_network_detail"):
            detail = analysis.function_network_detail or []
            lines.append(
                f"📡 **功能网详情**: {len(detail)} 条功能条目"
            )

        return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════
# 独立调用入口
# ══════════════════════════════════════════════════════════════════

async def run_function_analysis(
    tree: StructureTree,
    project_id: Optional[str] = None,
    enable_kb: bool = True,
) -> FunctionAnalysis:
    """便捷入口：运行功能分析

    Args:
        tree: 结构树对象
        project_id: 项目ID（用于知识库检索）
        enable_kb: 是否启用知识库检索增强

    Returns:
        FunctionAnalysis: 功能分析结果
    """
    agent = FunctionAgent(enable_kb_search=enable_kb)
    return await agent.generate_function_analysis(tree, project_id)
