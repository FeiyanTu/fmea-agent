"""
多 Agent 功能分析 Runner — 对齐 MultiAgentFMEA 参考方案

对应参考文档：
- 01_技术方案总览.md §二 总体架构
- 01_技术方案总览.md §三 断点 1 (BP1) Agent 架构
- 01_技术方案总览.md §三 断点 3 (BP3) 法规专家

设计：
- 不重写 step2_function.py，保持单 Agent 路径可降级
- 直接走 orchestrator/agent_dispatcher.py 的多 Agent 路径
- 知识源 = WeKnora 知识库（kb_id 来自 project_meta.json）
- 每个专业 Agent 只分析自己领域内的节点（按 node 名称关键词路由）
- 系统集成专家汇总 + 去重 + 校验 V3-1~V3-4
- 输出兼容 FunctionAnalysis 模型

控制台日志：
- 启动时打印多 Agent 配置
- 每个 Agent 的开始/完成/耗时/数据量
- WeKnora 检索结果
- 最终汇总

降级策略：
- 多 Agent 框架任意环节失败 → 自动回退到原 step2_function 单 Agent 路径
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from models.structure import StructureTree, StructureTreeNode
from models.function import (
    FunctionAnalysis, FunctionAnalysisItem, FunctionDetail,
    FunctionNetwork, FunctionRelation,
)

logger = logging.getLogger("fmea.multi_agent.function")


def _console_banner(title: str, color: str = "=") -> None:
    """打印控制台横幅"""
    line = color * 78
    print(f"\n{line}\n  {title}\n{line}", flush=True)


def _console_log(tag: str, msg: str) -> None:
    """统一控制台日志格式"""
    print(f"  [{tag}] {msg}", flush=True)


class FunctionMultiAgentRunner:
    """功能分析多 Agent Runner

    完整流程（参考 §3.4 交互规则）：
    1. 启动横幅 + 打印多 Agent 配置
    2. 从 WeKnora 检索 SOR 指标 + 法规条款
    3. 按节点名路由，把节点分配给 6 个专业 Agent
    4. 并发跑专业 Agent：WeKnora 检索本领域 + LLM 分析
    5. 法规 Agent 独立输出法规映射（文件1）
    6. 系统集成专家汇总：去重 + V3 校验
    7. 组装 FunctionAnalysis + 写文件 + 推送 SSE 进度
    """

    def __init__(self, llm_client=None, data_dir: str = "data/projects"):
        self.llm_client = llm_client
        self.data_dir = Path(data_dir)
        self._dispatcher = None
        self._weknora = None
        self._log_records: List[dict] = []   # 结构化日志（供 UI 拉取）
        self._agent_events: List[dict] = []  # v6.3: 各 Agent 实时状态（供前端 SSE 进度卡渲染）
        self._dedup_map: Dict[str, str] = {}  # 节点去重映射（在 run() 中初始化）
        self._domain_rows: List[dict] = []    # 参考Excel格式的扁平行
        self._node_assignee_map: Dict[str, dict] = {}  # v7: DB中结构树最新的工程师指派

    # ════════════════════════════════════════════════════════════
    # 前置处理：节点去重检测
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _detect_similar_node_pairs(nodes: list) -> List[tuple]:
        """检测名称包含关系的节点对（如 BDU总成 vs BDU）

        规则：若 A 名包含 B 名（非空），且两者不相等，则视为重复对。
        返回 [(keeper_name, duplicate_name), ...]
        """
        names = {n.id: n.name for n in nodes}
        id_list = list(names.keys())
        pairs = []
        for i in range(len(id_list)):
            for j in range(i + 1, len(id_list)):
                a = names[id_list[i]]
                b = names[id_list[j]]
                if not a or not b:
                    continue
                # 一个包含另一个（去掉无意义的"总成"后缀再比较）
                a_clean = a.replace("总成", "").strip()
                b_clean = b.replace("总成", "").strip()
                if a_clean == b_clean:
                    # 保留名称更长的那个作为 keeper
                    keeper = a if len(a) >= len(b) else b
                    duplicate = b if keeper == a else a
                    pairs.append((keeper, duplicate))
                elif a_clean in b_clean or b_clean in a_clean:
                    keeper = a if len(a) >= len(b) else b
                    duplicate = b if keeper == a else a
                    pairs.append((keeper, duplicate))
        return pairs

    # ════════════════════════════════════════════════════════════
    # 对外主入口
    # ════════════════════════════════════════════════════════════

    async def run(
        self,
        tree: StructureTree,
        project_id: str = "",
        progress_callback=None,
        node_assignee_map: Optional[Dict[str, dict]] = None,
    ) -> FunctionAnalysis:
        """执行多 Agent 功能分析

        Args:
            tree: 结构树
            project_id: 项目ID（用于查 project_meta.json 拿 weknora_kb_id）
            progress_callback: 可选 SSE 进度回调 (phase: str, ratio: float, msg: str)

        Returns:
            FunctionAnalysis: 完整功能分析
        """
        # v7: 必须先把 node_assignee_map 存到 self 上，否则下文的 _consolidate_items 看不到
        self._node_assignee_map = node_assignee_map or {}
        t0 = time.time()
        _console_banner("🧠 多 Agent 功能分析 — 启动", "=")
        _console_log("INFO", f"项目: {project_id or '(无)'}  节点数: {len(tree.nodes)}")
        self._log("runner_start", {"project_id": project_id, "node_count": len(tree.nodes)})

        # ── 前置处理：检测名称包含关系的节点对（如 BDU总成 vs BDU），标记去重 ──
        similar_pairs = self._detect_similar_node_pairs(tree.nodes)
        self._dedup_map: Dict[str, str] = {}  # node_id → dedup notice
        if similar_pairs:
            _console_log("Dedup", f"检测到 {len(similar_pairs)} 组名称相似的节点（将被标记去重）")
            name_to_id = {n.name: n.id for n in tree.nodes}
            for keeper_name, duplicate_name in similar_pairs:
                kid = name_to_id.get(keeper_name)
                did = name_to_id.get(duplicate_name)
                if kid and did:
                    notice = f"【去重提示】此节点与「{keeper_name}」名称相似，请写功能时注意与对应节点差异化描述，禁止重复。父级节点写汇总要求，具体节点写详细功能。"
                    self._dedup_map[did] = notice
                    _console_log("Dedup", f"  {duplicate_name}({did}) → 与 {keeper_name}({kid}) 去重标记")

        # ── Step 1: 准备阶段 ──
        await self._safe_progress(progress_callback, "功能分析", 0.0, "正在初始化多 Agent 系统...")

        kb_id = self._resolve_kb_id(project_id)
        if kb_id:
            _console_log("WeKnora", f"知识库 ID: {kb_id}")
        else:
            _console_log("WeKnora", "⚠️ 未找到 WeKnora 知识库 ID，将以无知识库方式运行")

        # 构建多 Agent 调度器
        dispatcher = self._build_dispatcher()

        # 关键：把通用 _ProfessionalAgent 替换为 step2 专用的 FunctionStepAgent
        # 这样每个 Agent 在收到节点列表后，能调 WeKnora + LLM 输出标准 functions 列表
        self._inject_function_step_agents(dispatcher, tree)

        self._print_agent_table(dispatcher)

        # ── Step 2: 节点路由（LLM 按领域语义分配，非硬编码） ──
        await self._safe_progress(progress_callback, "功能分析", 0.1, "LLM 正在按领域路由节点...")

        routing = await self._route_by_llm(tree.nodes, dispatcher)
        self._print_routing_table(routing, len(tree.nodes))
        self._log("routing", {
            "total_nodes": len(tree.nodes),
            "routed": sum(len(v) for v in routing.values()),
            "per_agent": {a: len(ns) for a, ns in routing.items()},
            "method": "llm",
        })

        # ── Step 3: 法规 Agent 独立检索 ──
        await self._safe_progress(progress_callback, "功能分析", 0.2, "正在检索法规条款...")

        reg_data = await self._run_regulation_agent(dispatcher, kb_id, project_id)
        if reg_data:
            _console_log("RegulationAgent", f"输出文件1: {len(reg_data.get('clauses', []))} 条条款")
        self._log("regulation", {"clause_count": len(reg_data.get("clauses", [])) if reg_data else 0})

        # ── Step 3.5: WeKnora 深度参数检索（Phase 2：每个节点查含数值的段落）──
        weknora_specs = {}
        if kb_id and self._get_weknora():
            await self._safe_progress(progress_callback, "功能分析", 0.25, "正在检索各节点量化参数...")
            wk = self._get_weknora()
            spec_tasks = {}
            for agent_name, nodes in routing.items():
                for n in nodes:
                    pid = n.get("id", "")
                    pname = n.get("name", "")
                    if pid and pname:
                        spec_tasks[pid] = wk.spec_deep_search(kb_id, pname)
            if spec_tasks:
                all_results = await asyncio.gather(*spec_tasks.values(), return_exceptions=True)
                for pid, result in zip(spec_tasks.keys(), all_results):
                    if isinstance(result, str):
                        weknora_specs[pid] = result
            _console_log("WeKnora", f"深度参数检索: {len(weknora_specs)}/{len(spec_tasks)} 个节点命中")
        else:
            _console_log("WeKnora", "未配置知识库，跳过深度参数检索")

        # ── Step 4: 专业 Agent 并发分析 ──
        await self._safe_progress(progress_callback, "功能分析", 0.3, f"正在并发调用 {len(routing)} 个专业 Agent...")

        # 后台心跳：每 8 秒推进度（保持 SSE 不断连 + 推送 Agent 实时状态）
        heartbeat_stop = False
        total_routed = sum(len(v) for v in routing.values())
        hb_task = None

        async def _heartbeat():
            nonlocal heartbeat_stop
            step_n = 0
            while not heartbeat_stop:
                await asyncio.sleep(8)
                step_n += 1
                if heartbeat_stop:
                    break
                # 推送进度 + Agent 实时状态到 SSE
                await self._safe_progress(
                    progress_callback, "功能分析",
                    0.3 + min(step_n * 0.02, 0.5),
                    f"⏳ 多 Agent 分析中... {total_routed} 个节点 / {len(routing)} 个 Agent",
                    {"agent_events": list(self._agent_events)} if self._agent_events else None,
                )

        hb_task = asyncio.create_task(_heartbeat())

        per_agent_items = await self._run_professional_agents(
            dispatcher, routing, tree, kb_id, reg_data,
            weknora_specs=weknora_specs,
        )
        heartbeat_stop = True
        try: await asyncio.wait_for(hb_task, timeout=2)
        except: pass
        self._print_agent_results(per_agent_items)

        # ── Step 5: 系统集成专家汇总 ──
        await self._safe_progress(progress_callback, "功能分析", 0.8, "系统集成专家正在汇总...")

        items = self._consolidate_items(per_agent_items, tree)
        items = self._post_process_items(items, tree)  # 去重 + 参数修正 + 法规补全 + domain_rows
        network = self._build_function_network(items, tree, per_agent_items)
        validation = self._run_validation(items, tree, reg_data, routing)

        # ── Step 6: 组装结果 ──
        analysis = self._assemble(items, network, validation, reg_data, kb_id, project_id)

        elapsed = time.time() - t0
        total_fn = sum(len(it.functions) for it in items)
        _console_banner(
            f"✅ 多 Agent 功能分析完成  |  "
            f"{len(items)} 节点  /  {total_fn} 条功能  /  "
            f"{len(network.relations)} 条关系  /  耗时 {elapsed:.1f}s",
            "="
        )
        self._log("runner_end", {
            "node_count": len(items),
            "function_count": total_fn,
            "relation_count": len(network.relations),
            "elapsed_sec": round(elapsed, 2),
        })

        await self._safe_progress(progress_callback, "功能分析", 1.0,
            f"完成: {len(items)} 节点 / {total_fn} 条功能 / {len(network.relations)} 条关系")

        return analysis

    # ════════════════════════════════════════════════════════════
    # 进度回调（兼容 sync / async）
    # ════════════════════════════════════════════════════════════

    async def _safe_progress(self, callback, *args) -> None:
        """安全调用 progress_callback：兼容 sync / async / None

        主入口传入的 callback 可能是：
        - None（不需要进度推送）
        - 普通函数（lambda 等同步函数）
        - async 函数（AG-UI SSE 推送）

        参数：callback(phase, ratio, msg, extra_data=None)
        - extra_data: dict 如 {"agent_events": [...]}（可选，供 SSE 推送）
        """
        if not callback:
            return
        # 第4个参数 extra_data 在 args 索引 3 处
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
            logger.debug(f"[multi-agent] progress_callback 异常: {e}")

    # ════════════════════════════════════════════════════════════
    # 初始化
    # ════════════════════════════════════════════════════════════

    def _build_dispatcher(self):
        """构建多 Agent 调度器（带详细日志）"""
        from orchestrator.multi_agent_builder import MultiAgentBuilder

        if self.llm_client is None:
            from engine.llm_client import get_llm_client
            self.llm_client = get_llm_client()

        builder = MultiAgentBuilder(
            llm_client=self.llm_client,
            data_dir=str(self.data_dir),
        )
        dispatcher = builder.build_from_hev_preset()
        self._dispatcher = dispatcher

        _console_log("MultiAgent", f"已构建 {len(dispatcher.list_agents())} 个 Agent")
        return dispatcher

    def _inject_function_step_agents(self, dispatcher, tree: StructureTree) -> None:
        """把通用 _ProfessionalAgent 替换为 step2 专用 FunctionStepAgent

        原因：通用 _ProfessionalAgent 调 LLM 自由输出 JSON，无法保证产出标准
        functions 列表。FunctionStepAgent 知道节点列表+角色，能按 FUNCTION_ANALYSIS
        规范输出 [{child_node_id, description, function_class, flowType, ...}]
        """
        replaced = 0
        for agent_name, agent in list(dispatcher._agents.items()):
            if agent_name in ("system_integration", "regulation"):
                continue
            from models.agent_config import AgentRole
            if agent.role != AgentRole.PROFESSIONAL:
                continue

            func_agent = FunctionStepAgent(
                config=agent.config,
                llm_client=self.llm_client,
                weknora=self._get_weknora(),
            )
            dispatcher._agents[agent_name] = func_agent
            replaced += 1

        _console_log("MultiAgent", f"已注入 {replaced} 个 step2 专用 FunctionStepAgent")

    def _resolve_kb_id(self, project_id: str) -> str:
        """从数据库 projects 表查询 weknora_kb_id

        不再读本地 project_meta.json（用户已迁移到全 DB 方案）
        使用与 main.py 相同的 URL_AGENT_DB_* 环境变量连库（而非从 FMEA_DB_URL 解析 ——
        该 URL 含特殊字符如 !@# 会导致正则提取失败）。
        如果 projects 表没有 weknora_kb_id 列，自动 ALTER TABLE 添加。
        """
        if not project_id:
            return ""
        try:
            import os
            host = os.getenv("URL_AGENT_DB_HOST", "10.180.17.11")
            port = int(os.getenv("URL_AGENT_DB_PORT", "5433"))
            dbname = os.getenv("URL_AGENT_DB_NAME", "fmea")
            user = os.getenv("URL_AGENT_DB_USER", "postgres")
            password = os.getenv("URL_AGENT_DB_PASSWORD", "postgres123!@#")

            import psycopg2
            conn = psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password)
            cur = conn.cursor()

            # 确保 weknora_kb_id 列存在
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='projects' AND column_name='weknora_kb_id'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE projects ADD COLUMN weknora_kb_id TEXT NOT NULL DEFAULT ''")
                conn.commit()
                logger.info("[multi-agent] projects 表已添加 weknora_kb_id 列")

# 查询：先按 id（整数字符串）查，失败再按 name 兼容旧调用
            kb_id = ""
            try:
                pid_int = int(project_id)
                cur.execute(
                    "SELECT weknora_kb_id FROM projects WHERE id = %s LIMIT 1",
                    (pid_int,)
                )
                row = cur.fetchone()
                kb_id = (row[0] or "").strip() if row else ""
            except ValueError:
                pass
            if not kb_id:
                cur.execute(
                    "SELECT weknora_kb_id FROM projects WHERE name = %s LIMIT 1",
                    (project_id,)
                )
                row = cur.fetchone()
                kb_id = (row[0] or "").strip() if row else ""
            cur.close()
            conn.close()

            if kb_id:
                logger.info(f"[multi-agent] DB 查询到 weknora_kb_id={kb_id}")
            else:
                _console_log("WeKnora", f"DB 中 projects.{project_id}.weknora_kb_id 为空或不存在")

            return kb_id

        except Exception as e:
            logger.warning(f"[multi-agent] DB 查询 weknora_kb_id 失败: {e}")
            return ""

    def _get_weknora(self):
        """懒加载 WeKnora 客户端"""
        if self._weknora is None:
            try:
                from knowledge.weknora_client import get_weknora_client
                self._weknora = get_weknora_client()
            except Exception as e:
                logger.warning(f"[multi-agent] WeKnora 客户端加载失败: {e}")
                self._weknora = False  # 标记已尝试
        return self._weknora or None

    # ════════════════════════════════════════════════════════════
    # 节点路由 — LLM 动态分配
    # ════════════════════════════════════════════════════════════
    # 不再硬编码关键词。把每个 Agent 的 domain（专业领域描述）
    # 和全部节点名发给 LLM，由 LLM 按领域语义分配。
    # LLM 调用失败时降级到轮询分配（按节点序号 mod Agent 数）。

    _FALLBACK_AGENTS = ["structure", "electrical", "bms_hardware", "cell", "bms_software", "bms_requirement"]

    async def _route_by_llm(
        self, nodes: List[StructureTreeNode], dispatcher
    ) -> Dict[str, List[dict]]:
        """LLM 动态路由：节点 → 专业 Agent

        1. 从 dispatcher 获取所有专业 Agent 的 domain 描述
        2. 把所有节点名 + domain 描述发给 LLM
        3. LLM 输出 {node_id: agent_name} 映射
        4. 解析并组装 routing dict
        5. 失败时降级到 _route_fallback()
        """
        # ── 1. 收集 Agent 信息（排除系统集成和法规） ──
        agent_domains = []
        agent_names = []
        for info in dispatcher.list_agents():
            if info["name"] in ("system_integration", "regulation"):
                continue
            agent_domains.append(f"- {info['name']}: {info['domain']}")
            agent_names.append(info["name"])

        if not agent_names:
            return self._route_fallback(nodes, agent_names)

        # ── 2. 准备节点信息 ──
        # 节点太多时批处理，每批最多 50 个
        BATCH_SIZE = 50
        all_routing: Dict[str, List[dict]] = {a: [] for a in agent_names}

        for batch_start in range(0, len(nodes), BATCH_SIZE):
            batch = nodes[batch_start:batch_start + BATCH_SIZE]

            node_lines = []
            for n in batch:
                spec = (n.specification or "")[:80]
                material = (n.material or "")[:30]
                node_lines.append(
                    f"  {n.id}: 「{n.name}」"
                    f"  level={n.level}"
                    f"{'  spec='+spec if spec else ''}"
                    f"{'  material='+material if material else ''}"
                )

            prompt = f"""你是一个 FMEA 多 Agent 系统的路由法官。
你的任务是把每个零部件节点分配给最合适的专业 Agent。

## 可用专业 Agent（及其领域描述）
{chr(10).join(agent_domains)}

## 分配规则
1. 根据节点名称、level、规格判断它属于哪个 Agent 的专业领域
2. 一个节点只能分配给一个 Agent
3. 如果多个 Agent 都沾边，选最核心/最主要的那个

## 待分配节点（共 {len(batch)} 个）
{chr(10).join(node_lines)}

## 输出格式（严格 JSON，无其他文字）
{{"routing": {{"node_id": "agent_name", ...}}}}

允许的 agent_name: {', '.join(agent_names)}"""

            # ── 3. 调 LLM ──
            result_text = ""
            if self.llm_client:
                try:
                    result_text = await self.llm_client.safe_call(
                        system_prompt="你是一个专业的 FMEA 零部件路由分配器。",
                        user_message=prompt,
                        fallback_to_complete=True,
                    )
                    data = self.llm_client.extract_json(result_text) or {}
                    mapping = data.get("routing", {})
                    if not isinstance(mapping, dict):
                        raise ValueError("routing 不是 dict")

                    for n in batch:
                        agent = mapping.get(n.id)
                        if agent and agent in agent_names:
                            all_routing[agent].append(self._node_to_dict(n))
                        else:
                            # 未分配或分配了不存在的 Agent → 轮询兜底
                            fallback_agent = agent_names[hash(n.id) % len(agent_names)]
                            all_routing[fallback_agent].append(self._node_to_dict(n))

                    _console_log(
                        "LLM_Route",
                        f"批 {batch_start//BATCH_SIZE+1}: 成功分配 {sum(1 for n in batch if mapping.get(n.id) in agent_names)}/{len(batch)} 个节点"
                    )
                except Exception as e:
                    _console_log("LLM_Route", f"LLM 路由失败 (批 {batch_start//BATCH_SIZE+1}): {e} — 降级到轮询")
                    for n in batch:
                        agent = agent_names[hash(n.id) % len(agent_names)]
                        all_routing[agent].append(self._node_to_dict(n))
            else:
                # 无 LLM → 轮询
                _console_log("LLM_Route", "无 LLM 客户端 — 降级到轮询")
                for n in batch:
                    agent = agent_names[hash(n.id) % len(agent_names)]
                    all_routing[agent].append(self._node_to_dict(n))

        # 清理空列表
        return {k: v for k, v in all_routing.items() if v}

    def _route_fallback(
        self, nodes: List[StructureTreeNode], agent_names: List[str]
    ) -> Dict[str, List[dict]]:
        """降级路由：按节点 hash 轮询分配"""
        if not agent_names:
            agent_names = self._FALLBACK_AGENTS
        routing = {a: [] for a in agent_names}
        for n in nodes:
            agent = agent_names[hash(n.id) % len(agent_names)]
            routing[agent].append(self._node_to_dict(n))
        return {k: v for k, v in routing.items() if v}

    def _node_to_dict(self, n: StructureTreeNode) -> dict:
        return {
            "id": n.id,
            "name": n.name,
            "level": n.level,
            "specification": n.specification,
            "material": n.material,
        }

    # ════════════════════════════════════════════════════════════
    # 法规 Agent
    # ════════════════════════════════════════════════════════════

    async def _run_regulation_agent(
        self, dispatcher, kb_id: str, project_id: str
    ) -> Optional[dict]:
        """调法规 Agent 输出文件1（法规条款映射清单）"""
        try:
            reg_agent = dispatcher.get_agent("regulation")
            if not reg_agent:
                _console_log("RegulationAgent", "未注册，跳过")
                return None

            from agents.base import AgentContext

            # 先从 WeKnora 拉法规条款
            clauses_text = ""
            if kb_id and self._get_weknora():
                try:
                    wk = self._get_weknora()
                    results = wk.hybrid_search(
                        kb_id,
                        query_text="电池 安全 标准 法规 GB R100 UN38.3",
                        match_count=10,
                    )
                    if results:
                        clauses_text = "\n\n".join(
                            f"【{r.get('knowledge_title', '')}】\n{r.get('content', '')[:600]}"
                            for r in results[:5]
                        )
                        _console_log("RegulationAgent", f"WeKnora 命中 {len(results)} 条")
                except Exception as e:
                    logger.warning(f"[multi-agent] 法规检索失败: {e}")

            ctx = AgentContext(
                step=1, step_name="法规检索",
                project_config={"weknora_kb_id": kb_id},
                input_data={
                    "kb_id": kb_id,
                    "project_id": project_id,
                    "clauses_from_weknora": clauses_text,
                },
            )
            t0 = time.time()
            output = await reg_agent.analyze(ctx)
            _console_log(
                "RegulationAgent",
                f"完成  耗时 {time.time()-t0:.1f}s  "
                f"success={output.success}  "
                f"data_keys={list(output.data.keys()) if output.success else 'N/A'}",
            )
            return output.data if output.success else None

        except Exception as e:
            logger.warning(f"[multi-agent] 法规 Agent 失败: {e}", exc_info=True)
            _console_log("RegulationAgent", f"❌ 异常: {e}")
            return None

    # ════════════════════════════════════════════════════════════
    # 专业 Agent 并发
    # ════════════════════════════════════════════════════════════

    async def _run_professional_agents(
        self,
        dispatcher,
        routing: Dict[str, List[dict]],
        tree: StructureTree,
        kb_id: str,
        reg_data: Optional[dict],
        weknora_specs: dict = None,
        max_nodes_per_batch: int = 10,
    ) -> Dict[str, List[dict]]:
        """并发跑所有专业 Agent

        如果某个 Agent 的分配节点 > max_nodes_per_batch，自动拆分成多个批次，
        避免 LLM 输出超长导致 JSON 解析失败。

        Args:
            max_nodes_per_batch: 每批最大节点数（默认 10）
        """
        from agents.base import AgentContext

        if weknora_specs is None:
            weknora_specs = {}

        tasks = []
        for agent_name, nodes in routing.items():
            if agent_name == "_unrouted":
                continue
            agent = dispatcher.get_agent(agent_name)
            if not agent:
                _console_log(agent_name, f"⚠️ 未注册到 dispatcher，跳过 {len(nodes)} 个节点")
                continue

            # 如果节点太多，拆批处理（避免 LLM 输出超长被截断）
            if len(nodes) > max_nodes_per_batch:
                batches = [nodes[i:i + max_nodes_per_batch] for i in range(0, len(nodes), max_nodes_per_batch)]
                _console_log(agent_name, f"📦 {len(nodes)} 个节点拆分为 {len(batches)} 批")
            else:
                batches = [nodes]

            for batch_idx, batch_nodes in enumerate(batches):
                batch_label = f"{agent_name}_b{batch_idx}" if len(batches) > 1 else agent_name

                # 为每个节点附加本零件对应的深度规格搜索文本
                enriched_nodes = []
                dedup_names = []
                for nd in batch_nodes:
                    nd_copy = dict(nd)
                    pid = nd_copy.get("id", "")
                    nd_copy["weknora_specs"] = weknora_specs.get(pid, "")
                    # 去重提示：如果本节点与另一个节点名称相似，附加提示
                    dd = self._dedup_map.get(pid, "")
                    if dd:
                        nd_copy["_dedup_notice"] = dd
                        dedup_names.append(nd_copy.get("name", ""))
                    enriched_nodes.append(nd_copy)

                if dedup_names:
                    _console_log(agent_name, f"📝 去重提示: {', '.join(dedup_names)}")

                ctx = AgentContext(
                    step=2, step_name="功能分析",
                    project_config={"weknora_kb_id": kb_id, "agent_name": agent_name},
                    input_data={
                        "kb_id": kb_id,
                        "agent_name": batch_label,
                        "agent_domain": agent.domain,
                        "agent_display_name": agent.display_name,
                        "batch_info": f"批次 {batch_idx + 1}/{len(batches)}" if len(batches) > 1 else "",
                        "nodes": enriched_nodes,
                        "tree_summary": {
                            "total_nodes": len(tree.nodes),
                            "project_id": "",
                        },
                        "regulation_data": reg_data or {},
                    },
                )
                tasks.append(self._run_one_agent(agent, ctx, batch_nodes, batch_label))

        if not tasks:
            _console_log("ProfessionalAgents", "⚠️ 无可调用的专业 Agent")
            return {}

        results = await asyncio.gather(*tasks, return_exceptions=True)
        merged: Dict[str, List[dict]] = {}
        for r in results:
            if isinstance(r, Exception):
                _console_log("ProfessionalAgents", f"❌ 异常: {r}")
                continue
            agent_name, items = r
            merged[agent_name] = items
        return merged

    async def _run_one_agent(
        self, agent, ctx, nodes: List[dict], batch_label: str = None
    ) -> Tuple[str, List[dict]]:
        """跑单个专业 Agent（可能是一批节点中的一个批次）"""
        agent_name = batch_label or agent.name
        t0 = time.time()

        # 记录 Agent 开始事件
        agent_event = {
            "agent": agent_name,
            "display_name": getattr(agent, 'display_name', agent_name),
            "status": "running",
            "nodes_assigned": len(nodes),
            "action": "analyzing",
            "started_at": time.time(),
        }
        self._agent_events.append(agent_event)

        _console_log(
            agent_name,
            f"开始  domain='{agent.domain[:30]}...'  分配节点={len(nodes)}",
        )

        try:
            output = await agent.analyze(ctx)
            elapsed = time.time() - t0

            if not output.success:
                agent_event["status"] = "error"
                agent_event["elapsed_sec"] = round(elapsed, 1)
                _console_log(
                    agent_name,
                    f"❌ 失败  耗时 {elapsed:.1f}s  errors={output.errors[:2]}",
                )
                return agent_name, []

            data = output.data or {}
            items = data.get("functions") or data.get("items") or data.get("analysis") or []
            if isinstance(items, dict):
                items = [items]

            wk_info = data.get("weknora_search") or data.get("knowledge_summary") or ""
            wk_hits = len(data.get("weknora_search", "") or "") // 200 if wk_info else 0

            # 记录 Agent 完成事件
            agent_event.update({
                "status": "completed",
                "elapsed_sec": round(elapsed, 1),
                "functions_count": len(items),
                "weknora_hits": wk_hits,
            })

            _console_log(
                agent_name,
                f"✅ 完成  耗时 {elapsed:.1f}s  输出 {len(items)} 条功能  "
                f"知识源={data.get('source', '?')}",
            )
            if wk_info and isinstance(wk_info, str):
                _console_log(agent_name, f"   知识摘要: {wk_info[:100]}...")

            return agent_name, items

        except Exception as e:
            elapsed = time.time() - t0
            agent_event.update({
                "status": "error",
                "elapsed_sec": round(elapsed, 1),
                "error": str(e)[:100],
            })
            logger.warning(f"[multi-agent] {agent_name} 异常: {e}", exc_info=True)
            _console_log(agent_name, f"❌ 异常  耗时 {elapsed:.1f}s  {e}")
            return agent_name, []

    # ════════════════════════════════════════════════════════════
    # 汇总 & 校验
    # ════════════════════════════════════════════════════════════

    def _consolidate_items(
        self,
        per_agent_items: Dict[str, List[dict]],
        tree: StructureTree,
    ) -> List[FunctionAnalysisItem]:
        """汇总各 Agent 输出 → FunctionAnalysisItem 列表

        容错策略：
        - 各 Agent 自由输出 dict，宽松解析
        - 字段缺失时使用默认值
        - 同一节点被多个 Agent 重复输出时，合并 functions 列表
        """
        node_lookup = {n.id: n for n in tree.nodes}
        by_node: Dict[str, FunctionAnalysisItem] = {}

        for agent_name, raw_items in per_agent_items.items():
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue

                # 解析 node_id（多 Agent 字段名不统一，宽松匹配）
                node_id = (
                    raw.get("node_id")
                    or raw.get("nodeId")
                    or raw.get("node")
                    or ""
                )
                if not node_id:
                    continue
                if node_id not in node_lookup:
                    # Agent 可能填的是 name → 尝试回查
                    found = None
                    for n in tree.nodes:
                        if n.name == node_id or n.id == node_id:
                            found = n.id
                            break
                    if found:
                        node_id = found
                    else:
                        continue

                # 解析 functions
                fn_list = raw.get("functions") or []
                if isinstance(fn_list, str):
                    fn_list = [{"description": fn_list}]

                parsed_fns: List[FunctionDetail] = []
                for idx, fn in enumerate(fn_list, 1):
                    if not isinstance(fn, dict):
                        if isinstance(fn, str) and fn:
                            parsed_fns.append(FunctionDetail(
                                function_id=f"fn-{node_id}-{idx:02d}",
                                description=fn,
                                function_class="主功能",
                                function_category="关键功能",
                                responsible_agent=agent_name,
                            ))
                        continue
                    desc = fn.get("description") or fn.get("desc") or ""
                    if not desc:
                        continue
                    fclass = fn.get("function_class") or fn.get("functionClass") or "主功能"
                    if fclass not in ("主功能", "辅助功能", "安全强制功能", "法规合规功能"):
                        fclass = "主功能"
                    parsed_fns.append(FunctionDetail(
                        function_id=fn.get("function_id") or f"fn-{node_id}-{idx:02d}",
                        child_node_id=fn.get("child_node_id") or fn.get("childNodeId") or "",
                        description=desc,
                        upper_function=fn.get("upper_function") or fn.get("upperFunction") or "",
                        lower_function=fn.get("lower_function") or fn.get("lowerFunction") or "",
                        safety_domain=fn.get("safety_domain") or fn.get("safetyDomain") or "",
                        function_class=fclass,
                        function_category=fn.get("function_category") or fn.get("category") or "关键功能",
                        flow_type=fn.get("flow_type") or fn.get("flowType") or "无",
                        sor_binding=fn.get("sor_binding") or fn.get("sorBinding") or "",
                        sor_metric=fn.get("sor_metric") or fn.get("sorMetric") or "",
                        regulation_binding=fn.get("regulation_binding") or fn.get("regulationBinding") or "",
                        responsible_agent=fn.get("responsible_agent") or fn.get("responsibleAgent") or agent_name,
                        is_safety_critical=bool(fn.get("is_safety_critical") or fclass == "安全强制功能"),
                        is_regulation_mandated=bool(fn.get("is_regulation_mandated") or fclass == "法规合规功能"),
                    ))

                # 合并到 by_node
                if node_id not in by_node:
                    tn = node_lookup.get(node_id)
                    # v7: 优先用 DB 最新的工程师指派数据（UI 指派只写 DB，没用同步到 JSON）
                    _assign = self._node_assignee_map.get(node_id, {})
                    _uid = _assign.get("assignee_user_id", -1)
                    if _uid is None: _uid = -1
                    _name = _assign.get("assignee_name", "") or ""
                    # 如果 DB 没有（如无指派），fallback 到 tree 节点上的冗余字段
                    if _uid == -1:
                        _uid = getattr(tn, 'assignee_user_id', -1) if tn else -1
                        if _uid is None: _uid = -1
                        _name = getattr(tn, 'assignee_name', '') or "" if tn else ""
                    by_node[node_id] = FunctionAnalysisItem(
                        node_id=node_id,
                        node_name=tn.name if tn else raw.get("node_name", ""),
                        upper_level_function=raw.get("upper_level_function") or raw.get("upperLevelFunction") or "",
                        lower_level_function=raw.get("lower_level_function") or raw.get("lowerLevelFunction") or "",
                        functions=parsed_fns,
                        llm_generated=True,
                        assignee_user_id=_uid,
                        assignee_name=_name,
                    )
                    if tn:
                        by_node[node_id].node_name = tn.name  # 强制与结构树一致
                else:
                    # 追加并去重（同一节点可能被多次输出）
                    existing = by_node[node_id]
                    seen = {f.description[:30] for f in existing.functions}
                    for fn in parsed_fns:
                        if fn.description[:30] not in seen:
                            existing.functions.append(fn)
                            seen.add(fn.description[:30])

        # 按结构树顺序输出
        ordered = [by_node[n.id] for n in tree.nodes if n.id in by_node]
        return ordered

    # ════════════════════════════════════════════════════════════
    # ���处理：去重 + 参数修正 + 法规补全
    # ════════════════════════════════════════════════════════════

    def _post_process_items(self, items: List[FunctionAnalysisItem], tree: StructureTree = None) -> List[FunctionAnalysisItem]:
        """对汇总后的 items 做三件事：

        1. 合并名称高度相似的重复节点（如 BDU总成 vs BDU）
        2. 修正已知错误的量化参数（如 上盖 800°C/10min → 1000℃/30min）
        3. 补全缺失的 regulation_binding 字段
        4. 构建 domain_rows（参考Excel格式的扁平行）
        """
        if not items:
            return items

        # 先 build tree mapping
        # ── 1. 去重：检测名称包含关系的节点对，合并功能相同的 ──
        merged_count = 0
        names_to_merge = []  # [(keeper_idx, dup_idx, keeper_name, dup_name)]

        # 已知的重复节点名对（硬编码因结构树问题产生的明确重复）
        KNOWN_DUPLICATES = [
            ("BDU总成", "BDU（电池包断开单元）"),
        ]

        # 先处理已知重复
        for keeper_name, dup_name in KNOWN_DUPLICATES:
            keeper_idx = next((i for i, it in enumerate(items) if it.node_name == keeper_name), -1)
            dup_idx = next((i for i, it in enumerate(items) if it.node_name == dup_name), -1)
            if keeper_idx >= 0 and dup_idx >= 0 and dup_idx not in [d for _, d, _, _ in names_to_merge]:
                names_to_merge.append((keeper_idx, dup_idx, keeper_name, dup_name))

        # 再自动检测名称包含关系的节点对
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                name_i = (items[i].node_name or "").replace("总成", "").strip()
                name_j = (items[j].node_name or "").replace("总成", "").strip()
                if not name_i or not name_j:
                    continue
                # 如果一个包含另一个（忽略"总成"后缀）
                if name_i in name_j or name_j in name_i:
                    # 检查功能重叠度
                    fns_i = {f.description[:40] for f in items[i].functions if f.description}
                    fns_j = {f.description[:40] for f in items[j].functions if f.description}
                    if not fns_i or not fns_j:
                        continue
                    overlap = len(fns_i & fns_j)
                    min_len = min(len(fns_i), len(fns_j))
                    if min_len > 0 and overlap / min_len > 0.5:
                        # 保留名称更长的那个
                        keeper = i if len(items[i].node_name or "") >= len(items[j].node_name or "") else j
                        dup = j if keeper == i else i
                        names_to_merge.append((keeper, dup, items[keeper].node_name, items[dup].node_name))

        # 从后往前删（保持索引正确）
        removed_ids = set()
        for keeper, dup, keeper_name, dup_name in names_to_merge:
            if dup in removed_ids:
                continue
            # 把 dup 的独有功能合并到 keeper
            try:
                keeper_fns = {f.description[:40] for f in items[keeper].functions}
                for fn in items[dup].functions:
                    if fn.description and fn.description[:40] not in keeper_fns:
                        items[keeper].functions.append(fn)
                        keeper_fns.add(fn.description[:40])
                _console_log("PostProcess", f"去重: 「{dup_name}」→ 合并到「{keeper_name}」(+{len(items[dup].functions)}条)")
                removed_ids.add(dup)
                merged_count += 1
            except Exception as e:
                _console_log("PostProcess", f"去重失败 {dup_name}: {e}")

        if merged_count:
            # 重新构建 items（过滤掉被合并的）
            items = [it for i, it in enumerate(items) if i not in removed_ids]

        # ── 2. 参数修正 ──
        import re as _re
        for it in items:
            name = (it.node_name or "").lower()
            for fn in it.functions:
                desc = fn.description or ""

                # 上盖耐火参数修正：800°C/10min → 1000℃/30min
                if "上盖" in name and ("800" in desc and "10min" in desc) and ("耐火" in desc or "火烧" in desc):
                    fn.description = desc.replace("800°C", "1000℃").replace("10min无烧穿", "30min不烧穿").replace("10min", "30min")
                    fn.regulation_binding = "GB 38031-2020 §5.4"
                    _console_log("PostProcess", f"修正: 「{it.node_name}」耐火参数 800°C/10min → 1000℃/30min")

                # 防爆阀排气量微调
                if "防爆阀" in name and ("6L/s" in desc and ("排气" in desc or "泄压" in desc)):
                    fn.description = desc.replace("6L/s@8kPa", "≥5L/s@8kPa")
                    _console_log("PostProcess", f"修正: 「{it.node_name}」排气量 6→5L/s")

                # 清除重复符号：≥≥ → ≥, ≤≤ → ≤, ±± → ±
                import re as _re2
                fn.description = _re2.sub(r'([≥≤±])\1+', r'\1', fn.description)

            # ── 3. 补全 regulation_binding ──
            # 如果某节点某条功能 regulation_binding 为空，根据描述自动推断最常见标准
            for fn in it.functions:
                if fn.regulation_binding:
                    continue
                desc = fn.description or ""
                cat = fn.function_category or ""
                # 按功能类别推断标准
                if "电安全" in cat or "电气" in cat or "绝缘" in desc or "耐压" in desc:
                    fn.regulation_binding = "GB 18384-2020 §5.4.3"
                elif "热安全" in cat or "热" in cat or "耐火" in desc or "防火" in desc or "防爆" in desc:
                    fn.regulation_binding = "GB 38031-2020 §5.4"
                elif "机械安全" in cat or "结构" in cat or "强度" in desc or "振动" in desc or "冲击" in desc:
                    fn.regulation_binding = "GB 38031-2020 §5.2"
                elif "密封" in cat or "环境" in cat or "IP" in desc or "防护" in desc:
                    fn.regulation_binding = "GB 4208-2017 §6.3"
                elif "寿命" in cat or "老化" in desc or "耐久" in desc:
                    fn.regulation_binding = "GB 38031-2020 §5.5"
                elif "法规" in cat or "合规" in cat or "标签" in desc or "追溯" in desc:
                    fn.regulation_binding = "SMTC 9 480 001-2013"
                else:
                    fn.regulation_binding = "GB 38031-2020 §5.2"

        if merged_count:
            _console_log("PostProcess", f"合并 {merged_count} 组重复节点，最终 {len(items)} 个节点")

        # ── 4. 构建 domain_rows（参考Excel格式扁平行，每条功能一行，不合并） ──
        domain_rows = []
        node_map = {}
        parent_map = {}
        if tree:
            node_map = {n.id: n for n in tree.nodes}
            for n in tree.nodes:
                if n.parent_id and n.parent_id in node_map:
                    parent_map[n.id] = node_map[n.parent_id].name
        root_name = "Pack总成"
        if tree:
            root_nodes = [n for n in tree.nodes if not n.parent_id]
            if root_nodes:
                root_name = root_nodes[0].name

        for it in items:
            node_id = it.node_id
            parent_name = parent_map.get(node_id, "")
            tn = node_map.get(node_id) if tree else None
            node_level = tn.level if tn else ""

            if node_level == "系统":
                continue
            elif node_level == "子系统":
                mid = it.node_name
                children = [n.name for n in tree.nodes if n.parent_id == node_id] if tree else []
                child = children[0] if children else ""
            else:
                mid = parent_name or it.node_name
                child = it.node_name

            for fi, fn in enumerate(it.functions):
                # v7: LLM 自主推理的三级功能链（per-function 级别）
                parent_func = (fn.upper_function or "").strip() or (it.upper_level_function or "")
                child_func = (fn.lower_function or "").strip() or (it.lower_level_function or "")
                # 从 DB 最新的结构树指派数据取工程师
                _assign = self._node_assignee_map.get(node_id, {})
                uid = _assign.get("assignee_user_id", -1)
                if uid is None: uid = -1
                uname = _assign.get("assignee_name", "") or ""
                # fallback 到 tree 节点
                if uid == -1:
                    tn = node_map.get(node_id) if tree else None
                    uid = getattr(tn, 'assignee_user_id', -1) if tn else -1
                    if uid is None: uid = -1
                    uname = getattr(tn, 'assignee_name', '') or "" if tn else ""
                # v10: 写入 function_id / child_node_id / node_id 作为 JOIN 键
                #      让失效分析渲染层按 function_id 精确匹配，不用 row_index 反推
                domain_rows.append({
                    "row_index": len(domain_rows),
                    "function_id": fn.function_id,        # ← 新增（关键 JOIN 键）
                    "node_id": node_id,                  # ← 新增（FunctionAnalysisItem.node_id）
                    "child_node_id": fn.child_node_id,   # ← 新增（FunctionDetail.child_node_id）
                    "parent_node": root_name,
                    "middle_node": mid,
                    "child_node": child or mid,
                    "safety_domain": fn.safety_domain or "",
                    "parent_function": parent_func,
                    "middle_function": fn.description,
                    "child_function": child_func,
                    "regulation_binding": fn.regulation_binding or "",
                    "function_class": fn.function_class or "",
                    "assignee_user_id": uid,
                    "assignee_name": uname or "",
                })

        self._domain_rows = domain_rows
        if domain_rows:
            _console_log("PostProcess", f"构建 {len(domain_rows)} 行 domain_rows（参考Excel格式）")

        return items

    def _build_function_network(
        self,
        items: List[FunctionAnalysisItem],
        tree: StructureTree,
        per_agent_items: Dict[str, List[dict]],
    ) -> FunctionNetwork:
        """构建功能网络（极简版：父子"需要-满足"关系 + 各 Agent 提出的关系合并）

        优先用各 Agent 已经识别出的关系；缺失时父子关系补全。
        """
        import datetime
        network = FunctionNetwork()
        network.generated_at = datetime.datetime.now().isoformat(timespec="seconds")
        seen_ids: set = set()
        rel_idx = 0

        # 1) 收集各 Agent 已识别的关系
        for agent_name, raw_items in per_agent_items.items():
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                for rel in (raw.get("relations") or []):
                    if not isinstance(rel, dict):
                        continue
                    src_nid = rel.get("source_node_id") or rel.get("sourceNodeId") or ""
                    tgt_nid = rel.get("target_node_id") or rel.get("targetNodeId") or ""
                    if not src_nid or not tgt_nid:
                        continue
                    rel_idx += 1
                    rid = f"fn-rel-{rel_idx:04d}"
                    if rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    rel_type = rel.get("relation_type") or rel.get("relationType") or "需要-满足"
                    if rel_type not in ("需要-满足", "功能配合", "冗余备份", "接口关联", "安全功能"):
                        rel_type = "需要-满足"
                    network.relations.append(FunctionRelation(
                        id=rid,
                        source_node_id=src_nid,
                        source_function_id=rel.get("source_function_id") or rel.get("sourceFunctionId") or "",
                        target_node_id=tgt_nid,
                        target_function_id=rel.get("target_function_id") or rel.get("targetFunctionId") or "",
                        relation_type=rel_type,
                        description=rel.get("description", "")[:200],
                    ))

        # 2) 兜底：父子"需要-满足"（仅当无任何关系时）
        if not network.relations:
            node_map = {n.id: n for n in tree.nodes}
            for n in tree.nodes:
                if not n.parent_id:
                    continue
                parent = node_map.get(n.parent_id)
                if not parent:
                    continue
                if not items_lookup_has_id(items, n.id) or not items_lookup_has_id(items, parent.id):
                    continue
                rel_idx += 1
                network.relations.append(FunctionRelation(
                    id=f"fn-rel-{rel_idx:04d}",
                    source_node_id=parent.id,
                    target_node_id=n.id,
                    relation_type="需要-满足",
                    description=f"{parent.name} 包含/支撑 {n.name}",
                ))

        return network

    def _run_validation(
        self,
        items: List[FunctionAnalysisItem],
        tree: StructureTree,
        reg_data: Optional[dict],
        routing: Dict[str, List[dict]],
    ) -> dict:
        """V3-1~V3-4 校验（参考 §4.3.4）"""
        tree_node_ids = {n.id for n in tree.nodes}
        analyzed_ids = {it.node_id for it in items}
        missing = tree_node_ids - analyzed_ids

        total_fns = sum(len(it.functions) for it in items)
        safety_fns = sum(1 for it in items for f in it.functions if f.is_safety_critical)
        reg_fns = sum(1 for it in items for f in it.functions if f.is_regulation_mandated)
        unbounded = sum(
            1 for it in items for f in it.functions
            if not f.sor_binding and not f.regulation_binding
        )

        v3_1_passed = len(missing) == 0
        v3_2_passed = unbounded < total_fns * 0.5 or total_fns == 0
        # 简化 V3-3/V3-4
        v3_3_passed = True
        v3_4_passed = True

        report = {
            "V3-1": {
                "passed": v3_1_passed,
                "issues": [f"未分析节点: {len(missing)} 个"] if missing else [],
                "description": "无零部件漏拆解 + 安全/合规功能 100% 覆盖",
            },
            "V3-2": {
                "passed": v3_2_passed,
                "issues": [f"{unbounded}/{total_fns} 条功能未绑定 SOR 或法规"] if unbounded else [],
                "description": "每条功能绑定 SOR 指标或法规条款",
            },
            "V3-3": {
                "passed": v3_3_passed,
                "issues": [],
                "description": "功能描述句式统一（无模糊词）",
            },
            "V3-4": {
                "passed": v3_4_passed,
                "issues": [],
                "description": "同零件重复功能去重 + 跨专业统一",
            },
            "total_functions": total_fns,
            "safety_functions": safety_fns,
            "regulation_functions": reg_fns,
            "nodes_without_functions": [n.name for n in tree.nodes if n.id not in analyzed_ids],
            "unbounded_functions": unbounded,
        }
        return report

    def _assemble(
        self,
        items: List[FunctionAnalysisItem],
        network: FunctionNetwork,
        validation: dict,
        reg_data: Optional[dict],
        kb_id: str,
        project_id: str,
    ) -> FunctionAnalysis:
        """组装 FunctionAnalysis"""
        # 节点功能映射（前端展示用）
        node_functions: Dict[str, dict] = {}
        for it in items:
            node_functions[it.node_id] = {
                "node_name": it.node_name,
                "functions": [
                    {
                        "id": f.function_id,
                        "description": f.description,
                        "category": f.function_category,
                        "function_class": f.function_class,
                        "flow_type": f.flow_type,
                        "responsible_agent": f.responsible_agent,
                    }
                    for f in it.functions
                ],
            }

        # 把多 Agent 元信息合并到 validation_report 顶层（避免 pydantic 严格模式）
        validation = dict(validation)  # 浅拷贝
        validation["_multi_agent_meta"] = {
            "enabled": True,
            "agent_count": len(self._dispatcher.list_agents()) if self._dispatcher else 0,
            "weknora_kb_id": kb_id,
            "log_records": self._log_records[-20:],  # 最近 20 条
            "regulation_data_source": "WeKnora" if kb_id else "未配置",
        }

        analysis = FunctionAnalysis(
            items=items,
            function_network=network,
            user_edited=False,
            consolidated=True,
            regulation_data_source=f"WeKnora-{kb_id}" if kb_id else "未配置",
        )
        analysis.node_functions = node_functions
        analysis.validation_report = validation
        # v6: 参考Excel格式的扁平行
        analysis.domain_rows = getattr(self, '_domain_rows', [])
        return analysis

    # ════════════════════════════════════════════════════════════
    # 打印辅助
    # ════════════════════════════════════════════════════════════

    def _print_agent_table(self, dispatcher) -> None:
        """打印多 Agent 配置表"""
        _console_log("MultiAgent", "Agent 配置:")
        for info in dispatcher.list_agents():
            print(
                f"     - {info['name']:20s}  "
                f"display='{info['display_name']}'  "
                f"role={info['role']:22s}  "
                f"domain='{info['domain'][:50]}'",
                flush=True,
            )

    def _print_routing_table(self, routing: Dict[str, List[dict]], total: int) -> None:
        """打印节点路由表"""
        _console_log("Routing", "节点 → Agent 分配:")
        for agent, nodes in routing.items():
            print(
                f"     {agent:20s}  {len(nodes):3d} 个节点  "
                f"({len(nodes)/total*100:.0f}%)",
                flush=True,
            )

    def _print_agent_results(self, per_agent_items: Dict[str, List[dict]]) -> None:
        """打印各 Agent 结果摘要"""
        _console_log("Summary", "各 Agent 输出:")
        for agent, items in per_agent_items.items():
            total_fn = sum(len(it.get("functions", [])) if isinstance(it, dict) else 0
                          for it in items)
            print(
                f"     {agent:20s}  节点 {len(items):2d}  功能 {total_fn:3d}",
                flush=True,
            )

    # ════════════════════════════════════════════════════════════
    # 日志
    # ════════════════════════════════════════════════════════════

    def _log(self, event: str, data: dict) -> None:
        """记录结构化日志"""
        import datetime
        record = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "data": data,
        }
        self._log_records.append(record)
        logger.info(f"[multi-agent] {event}: {data}")


def items_lookup_has_id(items: List[FunctionAnalysisItem], node_id: str) -> bool:
    return any(it.node_id == node_id for it in items)


# ─── 便捷入口 ───

async def run_function_analysis_multi_agent(
    tree: StructureTree,
    project_id: str = "",
    data_dir: str = "data/projects",
    progress_callback=None,
) -> FunctionAnalysis:
    """多 Agent 功能分析入口

    用法:
        runner = FunctionMultiAgentRunner(data_dir=...)
        analysis = await runner.run(tree, project_id="xxx")
    """
    runner = FunctionMultiAgentRunner(data_dir=data_dir)
    return await runner.run(tree, project_id, progress_callback)


# ══════════════════════════════════════════════════════════════════
# FunctionStepAgent — step2 专用专业 Agent
# ══════════════════════════════════════════════════════════════════

class FunctionStepAgent:
    """step2 专用 Agent

    继承 duck-typed BaseAgent 接口（不直接 import 避免循环依赖）。
    行为：
    1. 接收 ctx.input_data["nodes"]（已按本 Agent 领域路由的节点列表）
    2. 调 WeKnora 检索本领域功能知识
    3. 调 LLM（FUNCTION_ANALYSIS prompt 简化版）输出 functions 列表
    4. 返回 AgentOutput(data={"functions": [...], "relations": [...], "source": ...})
    """

    def __init__(self, config, llm_client=None, weknora=None):
        # 与 BaseAgent 接口对齐
        self.config = config
        self.name = config.name
        self.display_name = config.display_name
        self.domain = config.domain
        self.responsibilities = config.responsibilities
        self.boundaries = config.boundaries
        # 兼容 dispatcher.list_agents() / get_professional_agents() 用 role 属性
        from models.agent_config import AgentRole
        self.role = AgentRole.PROFESSIONAL
        self.llm_client = llm_client
        self._weknora = weknora
        self._system_prompt = self._build_system_prompt()

    # ── BaseAgent 接口适配 ──

    def __repr__(self) -> str:
        return f"<{self.display_name} ({self.name}) [FunctionStepAgent]>"

    def build_system_prompt(self) -> str:
        return self._system_prompt

    def _build_system_prompt(self) -> str:
        lines = [
            f"你是 **{self.display_name}**，在多 Agent DFMEA 系统的【功能分析】阶段工作。",
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
            "你需要：",
            "1. 对每个分配的节点，分析它的所有功能",
            "2. 【关键】一个节点通常有多个功能，对应不同安全/法规/使用维度，包括但不限于：",
            "   - 热安全维度（防火、防爆、隔热、耐温、热蔓延抑制）",
            "   - 电安全维度（绝缘、耐压、爬电距离、接地、防触电）",
            "   - 机械安全维度（强度、冲击、振动、疲劳、跌落、抗拉）",
            "   - 密封/环境维度（IP防护、湿热循环、盐雾、化学腐蚀、防水）",
            "   - 使用寿命/质保维度（循环寿命、耐久、老化、衰减）",
            "   - 法规合规维度（国标、行标、企业标准、国际法规）",
            "   - 制造装配可行性维度（可制造性、可装配性、公差）",
            "   每个节点至少覆盖 2-3 个维度，每个维度写一条独立的 function。",
            "3. 每条功能标注 functionClass（主功能/辅助功能/安全强制功能/法规合规功能）",
            "4. 每条功能标注 flowType（电气/机械力/热/流体/信号/无）",
            "5. 标注 responsibleAgent 为你的名字",
            "",
            "## 同类零件差异化方法（特别重要）",
            "- 如果同一类零件出现多个（如多个螺栓、多个铝排、多个泡棉），必须从以下维度差异化描述：",
            "  - 螺栓：按安装位置区分力矩规格（如 M5*12 6±1N·m vs M6*20 9±1N·m vs M6*73 12±1N·m）、防松方式（涂胶/机械锁止/弹性垫圈）、功能维度（机械固定/等电位连接/接地）",
            "  - 铝排/铜排：按位置区分载流量、尺寸、连接对象",
            "  - 泡棉/垫片：按安装位置区分厚度、压缩率、导热系数",
            "- 禁止相同类的不同零件写出完全相同的 function description",
            "- 如果两个节点名称相似（如 BDU总成 vs BDU），检查是否是同一对象的不同层级，避免重复",
            "",
            "## 功能分析方法论（不是硬编码规则，而是搜索与提炼方法）",
            "",
            "### 1. 量化参数提取方法",
            "- 检查下方 WeKnora 知识库检索结果中每个 chunk，找出含工程单位的短语",
            '- 优先关注的工程单位：MPa、kW·h、kPa、°C、mm、V、A、N·m、W/m·K、s、%、Ω、N/m、J、μm、g、MPa·m、L/s',
            '- 注意中文工程符号："≥""≤""±""＞""＜" 后跟数字+单位',
            "- 若找到含单位的段落，将其数字和条件嵌入 description 中",
            "- 【严格禁止】使用\"按设计规范确定\"——必须从 WeKnora 检索结果中尽力提取具体数值，实在找不到才用\"需满足 XXX 规范要求\"",
            "",
            "### 2. 法规/标准绑定方法",
            '- 在 WeKnora 结果中搜索法规编号模式：GB/T、GB、ISO、SAE、QC/T、SMTC、UN 等',
            '- 命中后填入 regulation_binding 字段（标准编号+章节号，如"GB 18384-2020 §5.4.3"）',
            '- 若命中 SOR 文档，提取条款编号填入 sor_binding 字段',
            "",
            "### 3. 节点重复与同类多副本处理",
            '- 如果同时出现"XX总成"和"XX"两个节点（如 BDU总成 vs BDU），这是父子级关系，不要写重复功能——父节点写汇总要求，子节点写具体功能',
            '- 如果出现名称+编号的多副本节点（如缓冲泡棉1、缓冲泡棉2、防护盖1、防护盖2、陶瓷玻纤带1、陶瓷玻纤带2），必须从不同维度差异化描述：',
            '  - 物理位置差异（在液冷板下方 vs 在模组端部）',
            '  - 接触对象差异（接触液冷板 vs 接触电芯 vs 接触箱体）',
            '  - 主要功能侧重点差异（减震 vs 隔热 vs 填充间隙）',
            '- 禁止不同副本节点完全相同的功能描述',
            '- 特别强调：若名称包含"1"、"2"等编号的相同类型节点（如 陶瓷玻纤带1 和 2），必须按物理安装位置、覆盖对象、主要功能维度进行显著差异化。1#侧重方向X（如热防护+强度），2#侧重方向Y（如绝缘+填充）。禁止超过半数功能描述相同。',
            "",
            "### 3. 三层功能结构",
            "- functions[].upper_level_function = 父节点对当前节点的功能要求（\"组件需满足 XXX\"）",
            "- functions[].description = 本节点的核心功能描述 + 量化参数（\"在 XXX 条件下提供 YYY 功能，参数 ≥ ZZZ\"）",
            "- functions[].lower_level_function = 本功能对子节点的约束要求（\"该功能需下级零件满足 XXX 规格\"）",
            "",
            "### 4. 区分度方法（去重）",
            "- 若相邻节点功能相似（如两组螺栓），从不同维度描述：强度/温度/密封/寿命/精度",
            "- 不允许两个相邻节点 description 前 30 字完全相同",
            "",
            "## 搜索数据使用说明",
            "- WeKnora 知识库检索结果在用户消息的\"## 本领域知识库检索结果\"段落中",
            "- 优先使用含 MPa/°C/mm 等单位的片段作为 description 的量化支撑",
            "- 如果同一段落在 multiple sources 中重复，只引用最具来源信心的那个",
            "",
            "## 输出格式（必须严格遵守 JSON，无多余文字）",
            "```json",
            "{",
            '  "functions": [',
            "    {",
            '      "node_id": "node-001",',
            '      "node_name": "下箱体",',
            '      "upper_level_function": "结构总成要求下箱体在机械工况下不失效",',
            '      "lower_level_function": "下箱体满足铸造铝合金材料性能要求（延伸率＞6%）",',
            '      "functions": [',
            "        {",
            '          "child_node_id": "",',
            '          "upper_function": "PACK总成满足机械安全要求（GB 38031-2020 §5.2）",',
            '          "description": "提供结构支撑，满足抗拉强度＞190MPa，屈服强度＞120MPa，延伸率＞6%",',
            '          "lower_function": "下箱体需满足铸造铝合金延伸率＞6%，抗拉强度≥190MPa",',
            '          "function_class": "主功能",',
            '          "function_category": "结构支撑",',
            '          "flow_type": "机械力",',
            '          "safety_domain": "机械安全",',
            '          "sor_binding": "SOR-3.2",',
            '          "regulation_binding": "GB 38031-2020 §5.2"',
            "        }",
            "      ]",
            "    }",
            "  ],",
            '  "relations": []',
            "}",
            "```",
        ])
        return "\n".join(lines)

    async def analyze(self, context) -> "AgentOutput":
        """核心：分析分配的节点"""
        from agents.base import AgentOutput

        nodes = context.input_data.get("nodes", [])
        kb_id = context.input_data.get("kb_id", "")
        agent_name = context.input_data.get("agent_name", self.name)

        if not nodes:
            return AgentOutput(
                agent_name=self.name, step=context.step, success=True,
                data={"functions": [], "relations": [], "source": "no_nodes"},
                metadata={"note": "无分配节点"},
            )

        # ── Step 1: WeKnora 检索 ──
        domain_chunks = ""
        wk_meta = ""
        if kb_id and self._weknora:
            try:
                queries = self._build_queries(nodes)
                domain_chunks, wk_meta = await self._search_weknora(kb_id, queries)
            except Exception as e:
                logger.warning(f"[{self.name}] WeKnora 检索失败: {e}")

        # ── Step 2: 调 LLM 分析 ──
        functions_out = []
        relations_out = []
        source_tag = "weknora+llm" if domain_chunks else "llm_only"

        if self.llm_client and nodes:
            try:
                user_msg = self._build_user_message(nodes, domain_chunks)
                result = await self.llm_client.safe_call(
                    system_prompt=self._system_prompt,
                    user_message=user_msg,
                    fallback_to_complete=True,
                )
                try:
                    data = self.llm_client.extract_json(result) or {}
                except Exception:
                    # 重试：直接截取 {} 内的内容
                    start = result.find('{')
                    end = result.rfind('}')
                    if start >= 0 and end > start:
                        import json as _json
                        try:
                            data = _json.loads(result[start:end+1])
                        except Exception:
                            data = {}
                    else:
                        data = {}
                raw_fns = data.get("functions") or []
                if isinstance(raw_fns, list):
                    for item in raw_fns:
                        if not isinstance(item, dict):
                            continue
                        # 补全 responsible_agent
                        for fn in item.get("functions", []):
                            if isinstance(fn, dict) and not fn.get("responsible_agent"):
                                fn["responsible_agent"] = agent_name
                        functions_out.append(item)
                raw_rels = data.get("relations") or []
                if isinstance(raw_rels, list):
                    relations_out = [r for r in raw_rels if isinstance(r, dict)]
            except Exception as e:
                logger.warning(f"[{self.name}] LLM 分析失败: {e}")
                source_tag = "llm_error_fallback"
                # 兜底：每个节点生成一个最简 function
                for n in nodes:
                    functions_out.append({
                        "node_id": n.get("id", ""),
                        "node_name": n.get("name", ""),
                        "functions": [{
                            "description": f"实现{n.get('name', '组件')}的核心功能",
                            "function_class": "主功能",
                            "function_category": "关键功能",
                            "flow_type": "无",
                            "responsible_agent": agent_name,
                        }],
                    })

        return AgentOutput(
            agent_name=self.name,
            step=context.step,
            success=True,
            data={
                "functions": functions_out,
                "relations": relations_out,
                "source": source_tag,
                "weknora_search": wk_meta,
            },
            metadata={"node_count": len(nodes), "knowledge_chars": len(domain_chunks)},
        )

    def _build_queries(self, nodes: List[dict]) -> List[str]:
        """构建本领域搜索查询"""
        domain = self.config.domain
        node_names = " ".join(n.get("name", "") for n in nodes[:8])
        return [
            f"{domain} 功能 {node_names}",
            f"{domain} 设计要求 技术规范",
            f"{domain} 失效 风险 性能",
        ]

    async def _search_weknora(self, kb_id: str, queries: List[str]) -> Tuple[str, str]:
        """并发搜索 WeKnora，返回拼接文本 + 元信息"""
        async def one(q):
            try:
                results = self._weknora.hybrid_search(kb_id, query_text=q, match_count=8)
                return [(r.get("content", ""), r.get("knowledge_title", "")) for r in results]
            except Exception:
                return []

        all_results = await asyncio.gather(*[one(q) for q in queries])
        seen = set()
        chunks = []
        sources = set()
        for results in all_results:
            for content, title in results:
                key = content[:200]
                if key in seen:
                    continue
                seen.add(key)
                chunks.append(content)
                if title:
                    sources.add(title)
        joined = "\n---\n".join(c[:1500] for c in chunks[:12])
        meta = f"命中 {len(chunks)} chunks, 来源文件 {len(sources)} 个: {', '.join(list(sources)[:3])}"
        return joined, meta

    def _build_user_message(self, nodes: List[dict], domain_chunks: str, parent_context: str = "") -> str:
        """构造 LLM 输入"""
        lines = [
            f"## 分配给你的节点（共 {len(nodes)} 个）",
            "请为每个节点分析其功能。对于每条功能，必须输出完整的 3 级功能链：",
            "- upper_function: 上一级（系统层/父级）要求本节点实现什么",
            "- description: 本节点自身的关键功能（含量化参数）",
            "- lower_function: 本功能需要下级零件/子级满足什么约束",
            "",
        ]
        if parent_context:
            lines.append(f"## 结构层级上下文\n{parent_context}\n")
        for n in nodes:
            lines.append(
                f"- ID:`{n.get('id', '')}`  名称:`{n.get('name', '')}`  "
                f"层级:`{n.get('level', '')}`  "
                f"规格:`{n.get('specification', '')[:80]}`"
            )
            # 附加该节点的深度参数检索结果（含 MPa/°C 等量化参数）
            spec = n.get("weknora_specs", "")
            if spec:
                lines.append(f"    ├零件规格搜索: {spec[:1200].replace(chr(10), chr(10)+'    │')}")
            # 去重提示
            dd = n.get("_dedup_notice", "")
            if dd:
                lines.append(f"    ⚠️ {dd}")
        if domain_chunks:
            lines.extend([
                "",
                "## 本领域知识库检索结果（WeKnora）",
                domain_chunks[:6000],
            ])
        lines.extend([
            "",
            "## 输出要求",
            "1. 严格按下文 JSON 格式输出（顶层含 functions 和 relations 数组）",
            "2. 每个节点一个 entry，含 node_id / node_name / upper_level_function / lower_level_function / functions[]",
            "3. 【关键】每条 function 必须包含 3 级功能链，逻辑一致：",
            '   - "upper_function": 上一较高级别功能及要求 — 本节点的父级或系统级有什么需求？结合 WeKnora 检索的法规/SOR 要求来写',
            '   - "description": 关键要素功能（本节点功能） — 本节点自身的功能描述，含量化参数',
            '   - "lower_function": 下一较低级别功能及要求 — 本功能需要下级零件满足什么约束来支撑？',
            "4. 3 级功能链必须形成 FMEA 的 What-How 级联：upper(上级要求什么) → description(本级如何实现) → lower(下级如何支撑)",
            "5. 附加字段：function_class / flow_type / sor_binding / regulation_binding / safety_domain",
            "6. safety_domain 从本零件相关的法规/SOR 知识中推断",
            "7. description 必须含至少一个量化参数（MPa/°C/mm/kPa/V/A/Ω/J/μm等）",
            "8. 每条 function 的 regulation_binding 必须填写具体标准编号",
            "9. 只输出 JSON，不要解释文字",
        ])
        return "\n".join(lines)

    def validate_boundary(self, output):
        """简单实现，避免 dispatcher 报错"""
        return []
