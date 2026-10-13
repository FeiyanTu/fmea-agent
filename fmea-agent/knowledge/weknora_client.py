"""
WeKnora 知识库 API 客户端

封装 WeKnora REST API，提供知识库查询、文件检索、内容预览等能力。
"""
import logging
import re
import httpx
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

BASE_URL = "http://10.180.17.11:81/api/v1"
API_KEY = "sk-ehE_QzpBjZOaXJqjIf2JP8Xmf3R4DKE1IR3HPVmRZLSpvGHJ"

# ── 文件类型分派（WeKnora 的 file_type 形如 "docx"/"xlsx"，不带点）──

# 可本地解析为文本的容器格式
_PARSED_EXTENSIONS = {"pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt"}
# 纯文本格式，直接按编码解码
_PLAIN_TEXT_EXTENSIONS = {"txt", "md", "csv", "json", "xml", "yaml", "yml",
                          "html", "htm", "log", "ini", "conf"}
# 无法转文本的类型（图片/音视频/压缩包/CAD），调用方应跳过，不要灌进 LLM
_SKIP_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff",
                    "svg", "ico", "mp3", "wav", "mp4", "avi", "mov", "zip",
                    "rar", "7z", "dwg", "dxf", "step", "stp", "igs"}

_REPLACEMENT_CHAR = "\ufffd"

# NUL / C0（除 \n \t）/ C1 控制字符：PDF 与 DOCX 偶尔会带出来
# 虽然 JSON 能转义，但部分推理服务遇到这些字符会直接返回 400 或拒答
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _sanitize_text(text: str) -> str:
    """清掉异常控制字符（保留 \n \t \r）"""
    if not text:
        return text
    return _CTRL_RE.sub(" ", text)


def _normalize_ext(file_type: str, title: str) -> str:
    """归一化扩展名：优先用 file_type，其次取文件名后缀（均为小写、无点）"""
    ft = (file_type or "").strip().lower().lstrip(".")
    if ft:
        return ft
    fn = (title or "").strip().lower()
    return fn.rsplit(".", 1)[-1] if "." in fn else ""


def _looks_like_binary_text(text: str, ratio: float = 0.02) -> bool:
    """判断一段"文本"是否其实是二进制被强行解码的产物

    判据：U+FFFD 替换符占比过高，或控制字符占比过高。
    控制字符包含 C0（除 \\n\\r\\t）与 C1（0x7F-0x9F）——后者尤其重要：
    `_read_with_fallback_encodings` 的最后兜底是 latin-1，它**永不报错**，
    会把 zip/PDF 字节里的 0x80-0x9F 解成 C1 控制字符混进正文。
    这是防止历史 bug 重演的最后一道防线——宁可丢弃，也不把乱码当正文。
    """
    if not text:
        return True
    sample = text[:20000]
    bad = sum(
        1 for ch in sample
        if ch == _REPLACEMENT_CHAR
        or (ord(ch) < 32 and ch not in "\n\r\t")
        or 0x7F <= ord(ch) <= 0x9F
    )
    return (bad / len(sample)) > ratio


def _decode_plain_text(content: bytes) -> str:
    """多编码回退解码纯文本文件"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "latin-1"):
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return content.decode("utf-8", errors="replace")


def _flatten_tables(tables: List[Dict]) -> str:
    """把结构化表格摊平成 `|` 分隔的行文本（供 LLM 与 BOM 行解析复用）"""
    lines: List[str] = []
    for t in tables:
        caption = t.get("caption") or ""
        if caption:
            lines.append(f"### {caption}")

        def _cell(v) -> str:
            return str(v).replace("\n", " ").replace("\r", " ").strip()

        headers = t.get("headers") or []
        if headers:
            lines.append(" | ".join(_cell(c) for c in headers))
        for row in t.get("rows") or []:
            lines.append(" | ".join(_cell(c) for c in row))
    return "\n".join(lines)


def _parse_document(content: bytes, ext: str, name: str) -> str:
    """把原始文件字节落到临时文件，再交给 file_converter 按类型解析为文本

    WeKnora 的 preview/download 返回的是**原始文件**，必须本地解析；
    直接把字节当文本读（旧实现）只会得到大量 U+FFFD 乱码。
    """
    import shutil
    import tempfile

    from knowledge.file_converter import load_document

    tmp_dir = tempfile.mkdtemp(prefix="weknora_kb_")
    stem = re.sub(r"[^\w\-]", "_", Path(name).stem)[:60] or "doc"
    tmp_path = Path(tmp_dir) / f"{stem}.{ext}"
    try:
        tmp_path.write_bytes(content)
        doc, _ = load_document(str(tmp_path))
        text = (doc.get("text") or "").strip()
        # load_document 失败时不抛异常，而是返回 "[加载失败: ...]"，这里要识别出来
        if text.startswith("[加载失败"):
            logger.warning(f"[WeKnora] 解析失败 {name}: {text[:120]}")
            return ""
        tables_text = _flatten_tables(doc.get("tables") or [])
        # Excel/CSV 的 text 视图只有前 50 行，tables 才是全量
        if ext in ("xlsx", "xls", "csv") and tables_text:
            return tables_text
        if tables_text:
            return f"{text}\n\n{tables_text}".strip()
        return text
    except Exception as e:
        logger.warning(f"[WeKnora] 解析异常 {name}: {e}")
        return ""
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

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
        # 知识点元信息缓存（仅在调用方没给 title/file_type 时用于回查）
        self._meta_cache: Dict[str, Dict[str, str]] = {}

    # ── 元信息解析（保证旧调用点只传 knowledge_id 也能正确解析）──

    def _resolve_meta(self, knowledge_id: str, title: str = "",
                      file_type: str = "") -> tuple:
        """确定 (扩展名, 文件名)

        调用方没给 file_type/title 时，回查 `/knowledge/{id}` 详情补齐——
        这样老的调用点（只传 knowledge_id）也能走对本地解析分支，
        不必依赖上游改造，是热修复场景下的自愈设计。
        """
        if file_type or title:
            ext = _normalize_ext(file_type, title)
            if ext:
                return ext, (title or knowledge_id)

        meta = self._meta_cache.get(knowledge_id)
        if meta is None:
            meta = {}
            try:
                detail = self.get_knowledge_detail(knowledge_id) or {}
                meta = {
                    "title": detail.get("title") or detail.get("file_name") or "",
                    "file_type": detail.get("file_type") or "",
                }
            except Exception as e:
                logger.warning(f"[WeKnora] 回查知识详情失败 {knowledge_id}: {e}")
            self._meta_cache[knowledge_id] = meta

        ext = _normalize_ext(meta.get("file_type", ""), meta.get("title", ""))
        return ext, (meta.get("title") or title or knowledge_id)

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

    # ── 下载原始文件 / 本地解析为文本 ──

    def download_knowledge(self, knowledge_id: str) -> bytes:
        """下载知识条目的**原始文件字节**

        ⚠️ 重要：`/knowledge/{id}/preview` 接口返回的是原始文件本身
        （docx/xlsx 是以 `PK\\x03\\x04` 开头的 zip 流，PDF/图片是二进制），
        与 `/knowledge/{id}/download` 返回的字节完全一致。
        需要文本时请用 `preview_knowledge_text()`，不要把这里的字节直接 decode。
        """
        resp = httpx.get(
            f"{self.base_url}/knowledge/{knowledge_id}/preview",
            headers={**self.headers, "Content-Type": "*/*"},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.content

    def preview_knowledge_text(self, knowledge_id: str, title: str = "",
                               file_type: str = "", max_chars: int = 0) -> Optional[str]:
        """获取知识文件的**纯文本**内容（下载原始文件 → 本地解析）

        分派规则：
          - xlsx/xls/csv → openpyxl/xlrd，表格摊平成 `|` 分隔行
          - docx → python-docx（段落 + 表格）
          - pdf  → PyMuPDF，回退 pypdf / pdfplumber
          - pptx → python-pptx
          - txt/md/json/... → 多编码回退解码
          - png/jpg/mp4/dwg 等无法转文本的类型 → 返回 None（调用方应跳过）

        任何一步失败、或解析结果疑似二进制乱码，都返回 None——绝不返回乱码。
        """
        ext, name = self._resolve_meta(knowledge_id, title, file_type)

        if ext not in (_PARSED_EXTENSIONS | _PLAIN_TEXT_EXTENSIONS):
            logger.info(f"[WeKnora] 跳过不可转文本的文件: {name} (type={ext or '未知'})")
            return None

        try:
            content = self.download_knowledge(knowledge_id)
        except Exception as e:
            logger.warning(f"[WeKnora] 下载失败 {name}: {e}")
            return None

        if not content:
            logger.warning(f"[WeKnora] 下载内容为空: {name}")
            return None

        text = (_decode_plain_text(content) if ext in _PLAIN_TEXT_EXTENSIONS
                else _parse_document(content, ext, name))

        if not text or not text.strip():
            logger.warning(f"[WeKnora] 解析结果为空: {name}")
            return None
        if _looks_like_binary_text(text):
            logger.warning(f"[WeKnora] 解析结果疑似二进制乱码，已丢弃: {name}")
            return None

        # 注意顺序：先做乱码判定（它依赖控制字符特征），再清洗控制字符
        text = _sanitize_text(text).strip()
        if max_chars and len(text) > max_chars:
            text = text[:max_chars]
        logger.info(f"[WeKnora] ✓ 解析文本 {name}: {len(text)} 字符 (type={ext})")
        return text

    # ── 预览文件内容（兼容旧接口）──

    def preview_knowledge(self, knowledge_id: str, title: str = "",
                          file_type: str = "") -> str:
        """[兼容旧接口] 获取知识文件纯文本内容，取不到时返回空串

        ⚠️ 历史 bug：旧实现是 `return resp.text`，而 preview 接口返回的是原始文件
        字节，docx/xlsx 会被按 UTF-8 强解出约 42% 的 U+FFFD 乱码，并一路流入
        结构树提取的 LLM 提示词和"零件名"解析，最终前端显示 `?V?⌂...`。
        现统一走 `preview_knowledge_text()` 本地解析，绝不返回乱码。
        """
        return self.preview_knowledge_text(
            knowledge_id, title=title, file_type=file_type) or ""

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
                fname = file_hit_names.get(kid, kid)
                text = self.preview_knowledge(kid, title=fname)
                if text:
                    truncated = text[:max_chars_per_file]
                    full_texts.append(f"=== 源文件: {fname} (命中{hit_count}次) ===\n{truncated}")
                    logger.info(f"[WeKnora] ✓ 解析 {fname}: {len(text)} 字符")
                else:
                    logger.info(f"[WeKnora] 跳过（无法转文本）{fname}")
            except Exception as e:
                logger.warning(f"[WeKnora] 解析失败 knowledge_id={kid}: {e}")

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
