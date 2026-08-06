"""
多 Agent 系统工厂 — 一键构建完整的 Agent 协作系统

用法：
    builder = MultiAgentBuilder(llm_client)
    dispatcher = builder.build_from_hev_preset()  # HEV 电池包预设
"""
import logging
from typing import Optional

from agents.base import BaseAgent
from agents.system_integration import SystemIntegrationExpert
from agents.regulation import RegulationAgent
from models.agent_config import (
    AgentConfig, AgentRole, ProjectAgentConfig, HEV_PROFESSIONAL_AGENTS
)
from orchestrator.agent_dispatcher import AgentDispatcher

logger = logging.getLogger(__name__)


def _console(msg: str) -> None:
    print(f"  [MultiAgentBuilder] {msg}", flush=True)


class MultiAgentBuilder:
    """多 Agent 系统构建器"""

    def __init__(self, llm_client=None, data_dir: str = "data/projects"):
        self.llm_client = llm_client
        self.data_dir = data_dir

    def build_from_hev_preset(self) -> AgentDispatcher:
        """使用 HEV 电池包预设配置构建完整的多 Agent 系统"""
        config = ProjectAgentConfig(professional_agents=HEV_PROFESSIONAL_AGENTS)
        return self.build_from_config(config)

    def build_from_config(self, config: ProjectAgentConfig) -> AgentDispatcher:
        """根据项目配置构建多 Agent 系统"""
        dispatcher = AgentDispatcher()

        # 系统集成专家
        integration_expert = SystemIntegrationExpert(llm_client=self.llm_client)
        dispatcher.set_integration_expert(integration_expert)

        # 专业 Agent
        for agent_config in config.professional_agents:
            agent = self._create_professional_agent(agent_config)
            if agent:
                dispatcher.register_agent(agent)

        prof = dispatcher.get_professional_agents()
        _console(
            f"构建完成: 共 {len(dispatcher.list_agents())} Agent "
            f"({len(prof)} 专业 + 1 集成专家)"
        )
        _console(f"专业 Agent 列表: {', '.join(a.name for a in prof)}")
        logger.info(
            f"[MultiAgentBuilder] 构建完成: "
            f"{len(dispatcher.list_agents())} Agent "
            f"({len(prof)} 个专业)")
        return dispatcher

    def _create_professional_agent(self, config: AgentConfig) -> Optional[BaseAgent]:
        """根据配置创建专业 Agent"""
        if config.name == "regulation":
            return RegulationAgent(llm_client=self.llm_client)

        # 通用专业 Agent：通过 WeKnora 检索本领域知识，LLM 分析
        return _ProfessionalAgent(config, self.llm_client)


class _ProfessionalAgent(BaseAgent):
    """专业 Agent — WeKnora 知识库驱动

    身份由 AgentConfig 定义，分析时从 WeKnora 检索本领域相关知识，
    然后由 LLM 在角色约束下执行分析。
    """

    def __init__(self, config, llm_client=None):
        super().__init__(config, llm_client)
        self._weknora = None

    def _get_weknora(self):
        if self._weknora is None:
            from knowledge.weknora_client import get_weknora_client
            self._weknora = get_weknora_client()
        return self._weknora

    async def analyze(self, context):
        from agents.base import AgentOutput

        kb_id = context.input_data.get("kb_id", context.project_config.get("kb_id", ""))
        domain = self.config.domain

        # 从 WeKnora 检索本领域知识
        domain_knowledge = ""
        if kb_id and self._get_weknora():
            try:
                queries = self._build_domain_queries(context)
                domain_knowledge = await self._search_knowledge(kb_id, queries)
            except Exception as e:
                logger.warning(f"[{self.display_name}] WeKnora检索失败: {e}")

        # 用 LLM 在角色约束下分析
        if self.llm_client and domain_knowledge:
            try:
                prompt = self.build_system_prompt()
                prompt += f"""

## 本领域知识库检索结果
{domain_knowledge[:8000]}

## 当前阶段: {context.step_name}
## 输入数据摘要
{str(context.input_data)[:3000]}

请基于你的专业领域知识，完成本阶段的分析任务。输出 JSON 格式结果。
"""
                result = await self._call_llm(prompt, f"阶段{context.step} {context.step_name}")
                data = self._extract_json(result)
                return AgentOutput(
                    agent_name=self.name, step=context.step, success=True,
                    data=data if isinstance(data, dict) else {"analysis": data},
                    metadata={"source": "weknora+llm", "kb_id": kb_id},
                )
            except Exception as e:
                logger.warning(f"[{self.display_name}] LLM分析失败: {e}")

        # 回退：返回身份标识
        return AgentOutput(
            agent_name=self.name, step=context.step, success=True,
            data={"domain": self.config.domain, "agent_identity": self.config.model_dump()},
            metadata={"source": "identity_only", "kb_id": kb_id},
        )

    def _build_domain_queries(self, context) -> list:
        """构建本领域搜索查询"""
        domain = self.config.domain
        step = context.step
        queries = [f"{domain} 设计要求 技术规范"]

        if step in (2, 3):  # 结构/功能阶段
            queries.append(f"{domain} 零部件结构 组成 BOM")
        if step in (3, 4):  # 功能/失效阶段
            queries.append(f"{domain} 功能需求 性能指标 故障模式")
        if step in (4, 5):  # 失效/风险阶段
            queries.append(f"{domain} 失效模式 失效原因 设计缺陷")
        if step in (5, 6):  # 风险/优化阶段
            queries.append(f"{domain} 设计优化 改进措施 降风险方案")

        return queries

    async def _search_knowledge(self, kb_id: str, queries: list) -> str:
        """并发搜索 WeKnora"""
        import asyncio

        async def search_one(query):
            try:
                results = self._weknora.hybrid_search(kb_id, query_text=query, match_count=10)
                return [(r.get("content", ""), r.get("score", 0)) for r in results]
            except Exception:
                return []

        tasks = [search_one(q) for q in queries]
        all_results = await asyncio.gather(*tasks)

        seen = set()
        chunks = []
        for results in all_results:
            for content, score in results:
                if content not in seen:
                    seen.add(content)
                    chunks.append((content, score))

        chunks.sort(key=lambda x: -x[1])
        return "\n---\n".join(c[:1500] for c, _ in chunks[:15])
