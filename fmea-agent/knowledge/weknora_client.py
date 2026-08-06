"""
WeKnora 知识库 API 客户端

封装 WeKnora REST API，提供知识库查询、文件检索、内容预览等能力。
"""
import logging
import httpx
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

BASE_URL = "http://10.180.17.11:81/api/v1"
API_KEY = "sk-ehE_QzpBjZOaXJqjIf2JP8Xmf3R4DKE1IR3HPVmRZLSpvGHJ"

_HEADERS = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json",
}


class WeKnoraClient:
    """WeKnora 知识库 API 客户端"""

    def __init__(self, base_url: str = BASE_URL, api_key: str = API_KEY):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        }

    # ── 知识库列表 ──

    def list_knowledge_bases(self) -> List[Dict]:
        """列出全部知识库

        Returns:
            [{id, name, description, knowledge_count, chunk_count, ...}]
        """
        resp = httpx.get(f"{self.base_url}/knowledge-bases", headers=self.headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])

    # ── 知识库下文件列表 ──

    def list_knowledge(self, kb_id: str, page: int = 1, page_size: int = 100,
                       file_type: str = "", keyword: str = "",
                       parse_status: str = "completed") -> List[Dict]:
        """列出知识库下的知识（文件）

        Args:
            kb_id: 知识库 ID
            page: 页码
            page_size: 每页条数
            file_type: 文件类型过滤，如 "pdf"、"docx"
            keyword: 关键词过滤
            parse_status: 解析状态过滤，默认 completed

        Returns:
            [{id, title, file_name, file_type, parse_status, file_size, ...}]
        """
        params = {"page": page, "page_size": page_size}
        if file_type:
            params["file_type"] = file_type
        if keyword:
            params["keyword"] = keyword
        if parse_status:
            params["parse_status"] = parse_status

        resp = httpx.get(
            f"{self.base_url}/knowledge-bases/{kb_id}/knowledge",
            headers=self.headers, params=params, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])

    def list_all_knowledge(self, kb_id: str, file_type: str = "",
                           parse_status: str = "completed") -> List[Dict]:
        """遍历所有分页，获取知识库下的全部文件"""
        all_items = []
        page = 1
        while True:
            items = self.list_knowledge(kb_id, page=page, page_size=100,
                                        file_type=file_type, parse_status=parse_status)
            if not items:
                break
            all_items.extend(items)
            if len(items) < 100:
                break
            page += 1
        return all_items

    # ── 获取知识详情 ──

    def get_knowledge_detail(self, knowledge_id: str) -> Optional[Dict]:
        """获取知识详情

        Returns:
            {id, title, file_name, file_type, parse_status, file_path, ...}
        """
        resp = httpx.get(
            f"{self.base_url}/knowledge/{knowledge_id}",
            headers=self.headers, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data")

    # ── 预览文件内容 ──

    def preview_knowledge(self, knowledge_id: str) -> str:
        """获取知识文件预��内容（纯文本）

        返回文件内容文本，PDF 等会自动转为文本。
        """
        resp = httpx.get(
            f"{self.base_url}/knowledge/{knowledge_id}/preview",
            headers={**self.headers, "Content-Type": "*/*"},
            timeout=60
        )
        resp.raise_for_status()
        return resp.text

    # ── 混合搜索 ──

    def hybrid_search(self, kb_id: str, query_text: str,
                      match_count: int = 50,
                      vector_threshold: float = 0.3,
                      keyword_threshold: float = 0.3) -> List[Dict]:
        """混合搜索（向量 + 关键词）

        Args:
            kb_id: 知识库 ID
            query_text: 查询文本
            match_count: 返回结果数量
            vector_threshold: 向量相似度阈值
            keyword_threshold: 关键词匹配阈值

        Returns:
            [{chunk_id, content, knowledge_id, knowledge_title,
              knowledge_filename, score, ...}]
        """
        if not kb_id:
            logger.debug("[WeKnora] hybrid_search called with empty kb_id, returning []")
            return []
        body = {
            "query_text": query_text,
            "vector_threshold": vector_threshold,
            "keyword_threshold": keyword_threshold,
            "match_count": match_count,
        }
        resp = httpx.post(
            f"{self.base_url}/knowledge-bases/{kb_id}/hybrid-search",
            headers=self.headers, json=body, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        # 处理 None / 非列表返回（如 {"data": null}）
        result = data.get("data") or []
        return result if isinstance(result, list) else []

    # ── 多知识库联合搜索（专业 Agent 专属 KB + 项目 KB）──

    def hybrid_search_multi(self, kb_ids: List[str], query_text: str,
                            match_count: int = 20,
                            vector_threshold: float = 0.3,
                            keyword_threshold: float = 0.3) -> List[Dict]:
        """跨多个知识库联合搜索，去重后按 score 降序返回

        Args:
            kb_ids: 知识库 ID 列表
            query_text: 查询文本
            match_count: 每个知识库返回数量

        Returns:
            合并去重后按 score 降序排列的结果列表，每条含 source_kb_id 标记
        """
        all_results = []
        seen = set()
        for kb_id in kb_ids:
            try:
                results = self.hybrid_search(
                    kb_id, query_text,
                    match_count=match_count,
                    vector_threshold=vector_threshold,
                    keyword_threshold=keyword_threshold,
                )
                for r in results:
                    cid = r.get("id", "")
                    if cid not in seen:
                        seen.add(cid)
                        r["source_kb_id"] = kb_id
                        all_results.append(r)
            except Exception as e:
                logger.warning(f"[WeKnora] hybrid_search_multi 失败 kb={kb_id}: {e}")
        all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return all_results

    # ── 知识搜索 ──

    def knowledge_search(self, query: str, kb_id: str = "") -> List[Dict]:
        """知识搜索（不在 chunk 级别，直接搜索知识条目）

        Args:
            query: 搜索关键词
            kb_id: 知识库 ID（可选）

        Returns:
            [{id, title, ...}]
        """
        body = {"query": query}
        if kb_id:
            body["knowledge_base_id"] = kb_id
        resp = httpx.post(
            f"{self.base_url}/knowledge-search",
            headers=self.headers, json=body, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])

    # ── 多策略深度零件规格搜索（Phase 2：不以硬编码方式告诉 LLM 具体值，而是提供搜索方法论）──

    async def spec_deep_search(self, kb_id: str, part_name: str) -> str:
        """多策略深度检索零件规格参数

        策略：
        1. 精确匹配零件名
        2. 零件名 + 规格/参数/性能 关联词
        3. 零件名 + 常见工程单位（MPa/kW/°C/mm/kPa）
        4. 零件名 + SOR/设计要求/试验方法
        5. 零件名 + 标准法规编号（GB/T, SAE, ISO, QC/T, SMTC）

        Args:
            kb_id: 知识库 ID
            part_name: 零件名称

        Returns:
            拼接的文本，命中含单位/数值的行优先排在前面
        """
        import asyncio

        queries = [
            part_name,
            f"{part_name} 技术参数 规格 性能要求",
            f"{part_name} MPa kW kPa 温度 电压 电流 压力 抗拉",
            f"{part_name} GB SAE ISO QC/T SMTC 标准 法规 试验",
        ]

        all_chunks = []
        seen = set()
        for q in queries:
            try:
                results = self.hybrid_search(kb_id, query_text=q, match_count=10)
                for r in results:
                    cid = r.get("id", "")
                    if cid not in seen:
                        seen.add(cid)
                        all_chunks.append(r)
            except Exception as e:
                logger.warning(f"[WeKnora] spec_search 失败 query={q[:30]}: {e}")

        # 按"是否含数值单位"加权排序
        UNITS = ["MPa","kW","kPa","°C","mm","V","A","N·m","W/m","K","s","%","Ω","MPa·m"]
        def _score_chunk(c):
            content = str(c.get("content", ""))
            has_unit = sum(1 for u in UNITS if u in content)
            score = c.get("score", 0)
            return (has_unit * 2 + score * 0.5)

        all_chunks.sort(key=_score_chunk, reverse=True)
        top = all_chunks[:10]

        parts = []
        for i, r in enumerate(top, 1):
            c = r.get("content", "")[:2000]
            title = r.get("knowledge_title", "")
            parts.append(f"【命中{i}】【来源:{title}】\n{c}")

        logger.info(f"[WeKnora] spec_deep_search({part_name}) 返回 {len(top)} 条 (含数值加权)")
        return "\n---\n".join(parts)

    # ── 获取文件内容（搜索 + 预览双通道）──

    async def collect_documents_for_subject(self, kb_id: str, subject: str,
                                             top_n_files: int = 8,
                                             max_chars_per_file: int = 25000,
                                             total_max_chars: int = 80000) -> str:
        """为 FMEA 主体对象收集文档内容

        双通道：
        1. 混合搜索：根据 subject 搜索相关 chunks
        2. 预览：对搜索命中数最多的文件获取全文

        Args:
            kb_id: 知识库 ID
            subject: FMEA 主体对象（如"12V低压电池系统"）
            top_n_files: 要预览全文的 top-N 文件数
            max_chars_per_file: 单文件截断字符数
            total_max_chars: 总输入上限

        Returns:
            拼接后的文档文本，按两个区域组织
        """
        import asyncio

        logger.info(f"[WeKnora] 开始收集文档 subject={subject} kb_id={kb_id}")

        # ── 通道 1：混合搜索 ──
        search_queries = [
            f"{subject}的结构组成、系统架构、零部件清单",
            f"{subject}的BOM、零件列表、物料清单",
            f"{subject}的功能、性能参数、技术要求",
        ]
        seen_chunks = set()
        all_chunks = []
        for q in search_queries:
            try:
                results = self.hybrid_search(kb_id, query_text=q, match_count=40)
                for r in results:
                    cid = r.get("id", "")
                    if cid not in seen_chunks:
                        seen_chunks.add(cid)
                        all_chunks.append(r)
            except Exception as e:
                logger.warning(f"[WeKnora] 搜索失败 query={q[:30]}: {e}")

        # 按 score 降序排序，取最多 60 条
        all_chunks.sort(key=lambda x: x.get("score", 0), reverse=True)
        all_chunks = all_chunks[:60]

        # 统计各文件被命中数，优先预览命中多的文件
        file_hit_count: Dict[str, int] = {}
        file_hit_names: Dict[str, str] = {}
        for c in all_chunks:
            kid = c.get("knowledge_id", "")
            if kid:
                file_hit_count[kid] = file_hit_count.get(kid, 0) + 1
                file_hit_names[kid] = c.get("knowledge_title", c.get("knowledge_filename", kid))

        # ── 通道 2：预览 top-N 命中文件 ──
        ranked_files = sorted(file_hit_count.items(), key=lambda x: -x[1])
        top_files = ranked_files[:top_n_files]

        full_texts = []
        for kid, hit_count in top_files:
            try:
                text = self.preview_knowledge(kid)
                if text:
                    truncated = text[:max_chars_per_file]
                    fname = file_hit_names.get(kid, kid)
                    full_texts.append(f"=== 源文件: {fname} (命中{hit_count}次) ===\n{truncated}")
                    logger.info(f"[WeKnora] ✓ 预览 {fname}: {len(text)} 字符")
            except Exception as e:
                logger.warning(f"[WeKnora] 预览失败 knowledge_id={kid}: {e}")

        # ── 组装最终输入 ──
        # 区域 1：搜索结果（chunk 片段）
        chunks_text = "\n\n".join(
            f"[{c.get('score', 0):.2f}] {c.get('knowledge_title', c.get('knowledge_filename', ''))}: {c.get('content', '')[:2000]}"
            for c in all_chunks[:40]
        )

        # 区域 2：全文预览
        fulls_text = "\n\n".join(full_texts)

        combined = (
            f"以下是为 FMEA 主体对象「{subject}」收集的知识库文档资料：\n\n"
            f"### 一、检索结果（相关片段）\n{chunks_text}\n\n"
            f"### 二、源文件全文\n{fulls_text}"
        )

        if len(combined) > total_max_chars:
            logger.warning(f"[WeKnora] 总输入 {len(combined)} 字符，截断至 {total_max_chars}")
            combined = combined[:total_max_chars]

        logger.info(f"[WeKnora] 文档收集完成: {len(all_chunks)} chunks, "
                     f"{len(top_files)} 全文文件, 总 {len(combined)} 字符")
        return combined


# 单例
_client: Optional[WeKnoraClient] = None


def get_weknora_client() -> WeKnoraClient:
    global _client
    if _client is None:
        _client = WeKnoraClient()
    return _client
