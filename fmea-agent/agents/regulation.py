"""
法规专家 Agent — WeKnora 知识库驱动版

职责：
- 从 WeKnora 知识库检索法规标准条款
- LLM 解读条款 → 产品功能 → 潜在失效风险映射
- 输出文件1：法规条款映射清单

边界：仅负责法规解读与映射，不参与工程失效分析
"""
import logging
from typing import Any, Dict, List, Optional

from agents.base import AgentContext, AgentOutput, BaseAgent
from models.agent_config import AgentConfig, AgentRole

logger = logging.getLogger(__name__)

REGULATION_AGENT_CONFIG = AgentConfig(
    name="regulation", display_name="法规专家", role=AgentRole.PROFESSIONAL,
    domain="法规标准检索与解读",
    responsibilities=["覆盖项目配置的设计维度完成法规检索", "标准条款原文解读（禁止概括、删减）",
                      "强制条款与产品风险点匹配映射", "输出标准化法规条款映射清单（文件1）"],
    boundaries=["仅负责法规解读与映射", "不参与工程失效分析", "不介入具体设计变更方案"],
)


class RegulationAgent(BaseAgent):
    """法规专家 Agent（WeKnora 知识库驱动）

    工作流程：
    1. 从 WeKnora 知识库检索法规标准
    2. LLM 解读条款 → 映射到产品功能/风险
    3. 输出文件1
    """

    def __init__(self, llm_client=None, weknora_client=None):
        super().__init__(REGULATION_AGENT_CONFIG, llm_client)
        self._weknora = weknora_client

    def _get_weknora(self):
        if self._weknora is None:
            from knowledge.weknora_client import get_weknora_client
            self._weknora = get_weknora_client()
        return self._weknora

    async def analyze(self, context: AgentContext) -> AgentOutput:
        project_kb_id = context.input_data.get("kb_id", context.project_config.get("weknora_kb_id", ""))
        regulation_list = context.project_config.get("regulation_list", [])

        # ⭐ 多知识库联合检索：项目 KB + 法规专家专属 KB
        own_kb_id = context.project_config.get("regulation_weknora_kb_id", "")
        if not own_kb_id:
            from engine.tools import get_agent_weknora_kb_id
            own_kb_id = get_agent_weknora_kb_id("法规专家")

        clauses = await self._search_regulations_from_multi_kb(
            project_kb_id=project_kb_id,
            own_kb_id=own_kb_id,
            regulation_list=regulation_list,
        )

        # LLM 解读
        if self.llm_client and clauses:
            clauses = await self._llm_interpret_clauses(clauses, context)

        # 输出文件1
        doc1 = self._build_document_1(clauses)

        return AgentOutput(
            agent_name=self.name, step=context.step, success=True,
            data={"document_1": doc1, "regulation_clauses": clauses},
            metadata={"source": "weknora", "project_kb_id": project_kb_id,
                      "own_kb_id": own_kb_id, "clause_count": len(clauses)},
        )

    async def _search_regulations_from_multi_kb(
        self, project_kb_id: str, own_kb_id: str, regulation_list: List[str]
    ) -> list:
        """从项目 KB + 法规专家专属 KB 联合检索法规条款

        冲突规则：相同内容 → 以项目 KB 为准（项目知识具有最高优先级）
        """
        wk = self._get_weknora()

        queries = [
            "法规标准 安全要求 强制条款",
            "GB38031 R100 UN38.3 电池安全 振动 冲击 热失控",
            "GB/T 4208 IP防护 防水防尘 密封",
            "GB/T 34590 ISO26262 功能安全 BMS",
        ]
        if regulation_list:
            queries.insert(0, " ".join(regulation_list))

        # ── 搜索项目 KB ──
        project_results = []
        if project_kb_id:
            seen = set()
            for query in queries:
                try:
                    results = wk.hybrid_search(project_kb_id, query_text=query, match_count=20)
                    for r in results:
                        cid = r.get("id", "")
                        if cid not in seen:
                            seen.add(cid)
                            project_results.append(r)
                except Exception as e:
                    logger.warning(f"[法规专家] 项目KB搜索失败: {e}")

        # ── 搜索专属 KB ──
        own_results = []
        if own_kb_id:
            seen = set()
            for query in queries:
                try:
                    results = wk.hybrid_search(own_kb_id, query_text=query, match_count=20)
                    for r in results:
                        cid = r.get("id", "")
                        if cid not in seen:
                            seen.add(cid)
                            own_results.append(r)
                except Exception as e:
                    logger.warning(f"[法规专家] 专属KB搜索失败: {e}")

        # ── 合并（项目 KB 优先）──
        from engine.tools import merge_multi_kb_results
        merged = merge_multi_kb_results(project_results, own_results, top_n=30)

        logger.info(
            f"[法规专家] 联合检索: 项目KB={len(project_results)}条, "
            f"专属KB={len(own_results)}条, 合并={len(merged)}条"
        )

        # 提取内容
        clauses = []
        for r in merged:
            content = r.get("content", "")
            title = r.get("knowledge_title", r.get("knowledge_filename", ""))
            if content:
                clauses.append({
                    "source": title,
                    "content": content[:3000],
                    "score": r.get("score", 0),
                    "knowledge_id": r.get("knowledge_id", ""),
                    "kb_priority": r.get("kb_priority", ""),
                })

        return clauses

    async def _llm_interpret_clauses(
        self, raw_clauses: list, context: AgentContext
    ) -> list:
        """使用 LLM 解读法规条款并映射到产品功能"""
        try:
            # 组装检索到的法规原文（最多 15 条，避免 LLM 输出过长）
            raw_clauses = raw_clauses[:15]
            raw_text = "\n\n---\n\n".join(
                f"[{c['source']}] score={c['score']:.2f}\n{c['content']}"
                for c in raw_clauses
            )

            prompt = self.build_system_prompt() + f"""

## 任务：法规条款解读与风险映射

基于以下从知识库检索到的法规标准原文，输出标准化的映射清单。

对每个条款输出 JSON 对象，字段：
- regulation_id: 法规编号（从原文中提取）
- clause_id: 条款编号
- full_text: 完整条款原文（禁止概括、删减——尽可能保留原文）
- product_function: 条款对应的产品设计功能
- potential_risk: 条款隐含的潜在失效风险
- responsible_module: 管控责任专业

### 检索到的法规原文
{raw_text[:15000]}

请以 JSON 数组格式输出映射结果。
"""
            result = await self._call_llm(prompt, "请解读以上法规条款")
            interpreted = self._extract_json(result)
            if isinstance(interpreted, list):
                return interpreted
            elif isinstance(interpreted, dict):
                for key in ("clauses", "regulation_mappings", "mappings", "items", "data"):
                    if key in interpreted:
                        val = interpreted[key]
                        if isinstance(val, list):
                            return val[:15]
                        return val
            return raw_clauses
        except Exception as e:
            logger.warning(f"[法规专家] LLM解读失败: {e}")
            return raw_clauses

    def _build_document_1(self, clauses: list) -> dict:
        """构建文件1：法规条款映射清单"""
        rows = []
        for c in clauses:
            rows.append({
                "regulation_id": c.get("regulation_id", c.get("source", "")),
                "clause_id": c.get("clause_id", ""),
                "full_text": c.get("full_text", c.get("content", "")),
                "product_function": c.get("product_function", ""),
                "potential_risk": c.get("potential_risk", ""),
                "responsible_module": c.get("responsible_module", ""),
            })

        return {
            "document_id": "文件1",
            "document_name": "法规条款映射清单",
            "total_clauses": len(rows),
            "mapping_table": rows,
            "generated_by": self.name,
            "data_source": "WeKnora知识库",
        }
