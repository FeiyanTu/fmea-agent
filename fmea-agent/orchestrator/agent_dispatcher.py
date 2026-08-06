"""
Agent 调度器 — 对应需求规格 §3.4 交互规则

职责：
- 管理全部 Agent 的注册和生命周期
- 接收阶段任务，路由到对应专业 Agent
- 收集各 Agent 输出，提交至系统集成专家汇总
- 错误隔离：单个 Agent 失败不阻塞其他 Agent
"""
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Set

from agents.base import (
    BaseAgent, SystemIntegrationAgent, AgentContext, AgentOutput, BoundaryViolation
)
from models.agent_config import AgentConfig, AgentRole

logger = logging.getLogger(__name__)


def _console(msg: str) -> None:
    """控制台输出（带 [Dispatcher] 前缀），方便观察多 Agent 协作"""
    print(f"  [Dispatcher] {msg}", flush=True)


class AgentDispatcher:
    """多 Agent 协作调度器

    使用流程：
    1. 注册 Agent（register_agent）
    2. 设置系统集成专家（set_integration_expert）
    3. 执行阶段（dispatch_step）
    4. 收集并合并结果
    """

    def __init__(self):
        self._agents: Dict[str, BaseAgent] = {}
        self._integration_expert: Optional[SystemIntegrationAgent] = None
        self._violation_log: List[BoundaryViolation] = []

    # ── 注册管理 ──

    def register_agent(self, agent: BaseAgent):
        """注册一个 Agent"""
        if agent.name in self._agents:
            logger.warning(f"Agent '{agent.name}' 已存在，将被覆盖")
        self._agents[agent.name] = agent
        logger.info(f"[Dispatcher] 注册 Agent: {agent.display_name} ({agent.name})")

    def register_agents(self, agents: List[BaseAgent]):
        """批量注册 Agent"""
        for agent in agents:
            self.register_agent(agent)

    def set_integration_expert(self, expert: SystemIntegrationAgent):
        """设置系统集成专家（有且仅有一个）"""
        self._integration_expert = expert
        self._agents[expert.name] = expert  # 同时注册到 Agent 列表
        logger.info(f"[Dispatcher] 设置系统集成专家: {expert.display_name}")

    def get_agent(self, name: str) -> Optional[BaseAgent]:
        """按名称获取 Agent"""
        return self._agents.get(name)

    def get_professional_agents(self) -> List[BaseAgent]:
        """获取所有专业 Agent（不含系统集成专家）"""
        return [
            a for a in self._agents.values()
            if a.role == AgentRole.PROFESSIONAL
        ]

    def list_agents(self) -> List[Dict[str, Any]]:
        """列出所有 Agent（含完整能力描述，用于 UI 展示和路由）"""
        return [
            {
                "name": a.name,
                "display_name": a.display_name,
                "role": a.role.value,
                "domain": a.domain,
                "responsibilities": a.responsibilities,
                "boundaries": a.boundaries,
            }
            for a in self._agents.values()
        ]

    # ── 阶段调度 ──

    async def dispatch_step(
        self,
        context: AgentContext,
        target_agents: Optional[List[str]] = None,
    ) -> AgentOutput:
        """执行一个阶段的 Agent 协作

        流程：
        1. 确定需要参与的专业 Agent
        2. 并发展开各专业 Agent 的 analyze()
        3. 收集结果并执行越界检测
        4. 提交给系统集成专家汇总
        5. 返回汇总后的统一输出

        Args:
            context: 阶段上下文
            target_agents: 指定参与的 Agent 名称列表（None=全部专业 Agent）

        Returns:
            系统集成专家汇总后的统一输出
        """
        # 1. 确定参与 Agent
        professional_agents = self.get_professional_agents()
        if target_agents:
            professional_agents = [
                a for a in professional_agents if a.name in target_agents
            ]

        if not professional_agents:
            logger.warning(f"阶段 {context.step} 无可用专业 Agent")
            return AgentOutput(
                agent_name="dispatcher",
                step=context.step,
                success=False,
                errors=["无可用专业 Agent"],
            )

        _console(
            f"阶段 {context.step}「{context.step_name}」启动 "
            f"{len(professional_agents)} 个专业 Agent: "
            f"{', '.join(a.name for a in professional_agents)}"
        )
        t_start = time.time()
        logger.info(
            f"[Dispatcher] 阶段 {context.step} ({context.step_name}) "
            f"启动 {len(professional_agents)} 个专业 Agent"
        )

        # 2. 并发展开各专业分析
        results: List[AgentOutput] = await self._run_concurrent(
            professional_agents, context
        )

        # 3. 越界检测 — 严格拦截（对应需求 §3.3 + §3.4）
        #    - severity="error" 的越界：直接拒绝该 Agent 输出，不进入汇总
        #    - severity="warning" 的越界：记录 + 提交集成专家裁决
        filtered_results: List[AgentOutput] = []
        rejected_agents: List[Dict[str, str]] = []
        for agent, result in zip(professional_agents, results):
            violations = agent.validate_boundary(result)
            if not violations:
                filtered_results.append(result)
                continue
            has_error = any(v.severity == "error" for v in violations)
            self._violation_log.extend(violations)
            for v in violations:
                logger.warning(
                    f"[越界检测] {agent.name} [{v.severity}] {v.description}"
                )
            if has_error:
                # 硬越界：拒绝该 Agent 输出（对应法规专家"不参与工程失效"等硬边界）
                rejected_agents.append({
                    "agent": agent.name,
                    "violations": [v.description for v in violations if v.severity == "error"],
                })
                logger.error(
                    f"[越界拦截] {agent.name} 硬越界，输出被拒绝: "
                    f"{[v.description for v in violations if v.severity == 'error']}"
                )
            else:
                # 软越界：放行，提交集成专家裁决
                filtered_results.append(result)

        if rejected_agents:
            logger.warning(
                f"[Dispatcher] 阶段 {context.step} 拒绝 {len(rejected_agents)} 个越界 Agent: "
                f"{[r['agent'] for r in rejected_agents]}"
            )

        # 4. 提交系统集成专家汇总
        if self._integration_expert:
            context_for_review = AgentContext(
                step=context.step,
                step_name=context.step_name,
                project_config=context.project_config,
                input_data={
                    "agent_outputs": filtered_results,
                    "rejected_agents": rejected_agents,
                },
            )
            merged = await self._integration_expert.analyze(context_for_review)
            elapsed = time.time() - t_start
            _console(
                f"阶段 {context.step}「{context.step_name}」完成 "
                f"({elapsed:.1f}s): {len(filtered_results)}/{len(results)} 成功"
                f"{', 拒绝 '+str(len(rejected_agents))+' 越界' if rejected_agents else ''}"
            )
            logger.info(
                f"[Dispatcher] 阶段 {context.step} 汇总完成: "
                f"{len(filtered_results)}/{len(results)} 个专业输出"
                f"{' (拒绝 '+str(len(rejected_agents))+' 个越界)' if rejected_agents else ''}"
                f" → 1个统一结果"
            )
            return merged

        # 无集成专家时，简单合并
        return self._simple_merge(filtered_results, context)

    async def _run_concurrent(
        self,
        agents: List[BaseAgent],
        context: AgentContext,
    ) -> List[AgentOutput]:
        """并发展开多个 Agent 的分析

        使用 asyncio.gather 实现并发，单个 Agent 失败不阻塞其他。
        """
        async def _run_one(agent: BaseAgent) -> AgentOutput:
            t0 = time.time()
            _console(f"  ▶ {agent.display_name} ({agent.name}) 开始...")
            try:
                # 为每个 Agent 创建独立的上下文副本
                agent_ctx = AgentContext(
                    step=context.step,
                    step_name=context.step_name,
                    project_config=context.project_config,
                    agent_config=agent.config,
                    input_data=context.input_data,
                    baseline_doc=context.baseline_doc,
                )
                result = await agent.analyze(agent_ctx)
                elapsed = time.time() - t0
                _console(
                    f"  ✓ {agent.display_name} ({agent.name}) 完成 "
                    f"[{elapsed:.1f}s, success={result.success}, "
                    f"data_keys={list(result.data.keys()) if result.data else []}]"
                )
                logger.info(
                    f"[Agent] {agent.display_name} 完成: "
                    f"success={result.success}, data_keys={list(result.data.keys())}"
                )
                return result
            except Exception as e:
                elapsed = time.time() - t0
                _console(f"  ✗ {agent.display_name} 异常 [{elapsed:.1f}s]: {e}")
                logger.error(f"[Agent] {agent.display_name} 执行失败: {e}", exc_info=True)
                return AgentOutput(
                    agent_name=agent.name,
                    step=context.step,
                    success=False,
                    errors=[str(e)],
                )

        tasks = [_run_one(agent) for agent in agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 将异常转换为 AgentOutput
        processed = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                processed.append(AgentOutput(
                    agent_name=agents[i].name,
                    step=context.step,
                    success=False,
                    errors=[str(result)],
                ))
            else:
                processed.append(result)

        return processed

    def _simple_merge(
        self,
        outputs: List[AgentOutput],
        context: AgentContext,
    ) -> AgentOutput:
        """简单合并 Agent 输出（无系统集成专家时的兜底逻辑）"""
        all_data = {}
        all_errors = []
        for o in outputs:
            if not o.success:
                all_errors.append(f"[{o.agent_name}] {'; '.join(o.errors)}")
                continue
            all_data[o.agent_name] = o.data

        return AgentOutput(
            agent_name="dispatcher",
            step=context.step,
            success=len(all_errors) == 0,
            data={"agent_outputs": all_data},
            errors=all_errors,
        )

    # ── 状态查询 ──

    def get_violation_log(self) -> List[BoundaryViolation]:
        """获取越界检测日志"""
        return self._violation_log.copy()

    def clear_violation_log(self):
        """清空越界日志"""
        self._violation_log.clear()

    def get_status(self) -> Dict[str, Any]:
        """获取调度器状态摘要"""
        return {
            "total_agents": len(self._agents),
            "professional_agents": len(self.get_professional_agents()),
            "has_integration_expert": self._integration_expert is not None,
            "violation_count": len(self._violation_log),
            "agents": self.list_agents(),
        }
