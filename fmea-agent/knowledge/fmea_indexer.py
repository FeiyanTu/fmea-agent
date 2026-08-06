"""
FMEA 语义索引构建器 (FMEA Semantic Indexer) v1.0

核心理念 — 在项目初始化时预构建 FMEA 要素索引，避免后续结构提取时重复遍历全部文档。

索引内容:
1. document_roles — 每个文档的 FMEA 角色分类（纯规则，不调 LLM）
2. fmea_elements  — FMEA 四大要素（结构/功能/特性/法规），从高优文档中抽取（调 LLM）
3. relevance_map  — FMEA 各步骤与文档的关联映射

用法:
    indexer = FmeaIndexer()
    index = await indexer.build_index(project_id)

    # 后续使用
    index = indexer.load_index(project_id)
    relevant_files = indexer.get_relevant_files(index, task="structure_tree")
"""

import json
import re
import asyncio
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field

from knowledge.kb_service import get_kb_service
from knowledge.fmea_roles import (
    classify_document,
    classify_document_l2,
    STRUCTURED_EXTS,
    ROLE_RULES as _SHARED_ROLE_RULES,  # noqa: F401 — 引用说明
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 注：ROLE_RULES / STRUCTURED_EXTS / classify_document 已移至
#     knowledge/fmea_roles.py 统一维护，此处不再重复定义。
# ═══════════════════════════════════════════════════════════════

# FMEA 四大要素
FMEA_ELEMENT_TYPES = ["structure", "function", "characteristic", "regulation"]


class FmeaIndexer:
    """FMEA 语义索引构建器"""

    # 要素抽取时的批量大小
    EXTRACT_BATCH_SIZE = 6  # 减小批量，提升每批抽取质量
    # 单个文档抽取时的最大字符数
    MAX_DOC_CHARS_FOR_EXTRACT = 50000  # 增大以保留更多 BOM 表格内容
    # 每个文档在 prompt 中的最大字符数
    MAX_PROMPT_DOC_CHARS = 15000  # 每文档在抽取 prompt 中的上限（从 8000 提升）
    # 高优文档优先级阈值
    HIGH_PRIORITY_THRESHOLD = 5.0
    # LLM 调用重试
    LLM_MAX_RETRIES = 2
    LLM_RETRY_DELAY = 5

    def __init__(self, llm_client=None):
        self.llm_client = llm_client
        self.kb_service = get_kb_service()

    def _get_llm(self):
        if self.llm_client:
            return self.llm_client
        from engine.llm_client import get_llm_client
        return get_llm_client()

    # ═══════════════════════════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════════════════════════

    async def build_index(
        self,
        project_id: str,
        progress_callback=None,
    ) -> Dict:
        """
        构建项目的 FMEA 语义索引。

        Step 1: 扫描文档 + 规则分类（不调 LLM）
        Step 2: 高优文档要素抽取（调 LLM，并行）
        Step 3: 构建关联映射
        Step 4: 持久化 fmea_index.json

        Args:
            project_id: 项目ID
            progress_callback: 进度回调 (phase, progress:float 0-1, message)

        Returns:
            完整的 FMEA 索引字典
        """
        t0 = time.time()
        self._notify(progress_callback, "classifying", 0.0, "Step 1/3: 文档规则分类...")

        # Step 1: 扫描文档 + 规则分类
        pages = self.kb_service.list_pages(project_id)
        if not pages:
            logger.warning(f"[Indexer] 项目 {project_id} 无文档，跳过索引构建")
            return self._empty_index(project_id)

        doc_roles = self._classify_by_rules(pages)
        logger.info(f"[Indexer] 规则分类完成: {len(doc_roles)} 个文档, "
                     f"角色分布: {self._role_summary(doc_roles)}")

        # ── L2 回退：对分类结果为 other 或低置信度文档做内容采样二次分类 ──
        await self._l2_fallback_classify(project_id, doc_roles)
        logger.info(f"[Indexer] L2 回退后角色分布: {self._role_summary(doc_roles)}")

        self._notify(progress_callback, "extracting", 0.3,
                     f"Step 2/3: 从高优文档抽取 FMEA 要素 "
                     f"({len([d for d in doc_roles.values() if d['priority'] >= self.HIGH_PRIORITY_THRESHOLD])} 个文档)...")

        # Step 2: 高优文档要素抽取
        high_priority_files = [
            filename for filename, info in doc_roles.items()
            if info["priority"] >= self.HIGH_PRIORITY_THRESHOLD
        ]
        logger.info(f"[Indexer] 高优文档: {len(high_priority_files)} 个")

        fmea_elements = await self._extract_elements(
            project_id, high_priority_files, doc_roles, progress_callback
        )

        self._notify(progress_callback, "mapping", 0.8, "Step 3/3: 构建关联映射...")

        # Step 3: 构建关联映射
        relevance_map = self._build_relevance_map(doc_roles, fmea_elements)

        # Step 4: 组装索引
        index = {
            "project_id": project_id,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "doc_count": len(pages),
            "document_roles": doc_roles,
            "fmea_elements": fmea_elements,
            "relevance_map": relevance_map,
        }

        # 持久化
        self._save_index(project_id, index)

        elapsed = time.time() - t0
        logger.info(f"[Indexer] 索引构建完成: {len(pages)} 文档, "
                     f"{len(high_priority_files)} 高优, 耗时 {elapsed:.1f}s")

        self._notify(progress_callback, "done", 1.0,
                     f"✅ FMEA 索引构建完成 ({len(pages)} 文档, {elapsed:.0f}s)")

        return index

    # ═══════════════════════════════════════════════════════════
    # Step 1: 规则分类（纯关键词，不调 LLM）
    # ═══════════════════════════════════════════════════════════

    def _classify_by_rules(self, pages: List[Dict], project_id: str = "") -> Dict[str, Dict]:
        """
        基于共享规则对文档进行多维度打分分类。

        Returns:
            {filename: {"role": str, "category": str, "priority": float,
                        "source_type": str, "is_structured": bool,
                        "confidence": str, "elements": []}}
        """
        result = {}

        for p in pages:
            filename = p.get("filename", "")
            title = p.get("title", "")
            summary = p.get("summary", "")
            source_file = p.get("source_file", "")
            source_type = p.get("source_type", "")
            size_kb = p.get("size_kb", 0)

            # ── 多维度打分分类 ──
            role_info = classify_document(
                filename=filename,
                title=title,
                summary=summary,
                source_file=source_file,
                source_type=source_type,
                size_kb=size_kb,
                # 先不做内容采样，L2 在后处理中统一进行
            )

            result[filename] = {
                "role": role_info["role"],
                "category": role_info["category"],
                "priority": role_info["priority"],
                "source_type": source_type,
                "is_structured": role_info["is_structured"],
                "title": title,
                "confidence": role_info["confidence"],
                "elements": [],  # 待 Step 2 填充
            }

        # ── 后处理：SOR 同系列附件角色推断 ──
        # 所有 SOR 附件都应有较高优先级，附件E（工程设计要求）尤其重要
        main_docs = [f for f, info in result.items() if info["role"] == "main_sor"]
        if main_docs:
            # 从主文件名中提取 SOR 基础前缀（去掉 "主文件.md" 等角色后缀）
            sor_base = ""
            for md in main_docs:
                # 匹配 SOR-xxx-主文件 / SOR-xxx-附件 等模式，取最后一个 - 之前的部分
                m = re.match(r'^(.+)-[^-]+(?:\.md)?$', md)
                if m:
                    sor_base = m.group(1) + "-"
                    break
            if not sor_base:
                sor_base = self._common_prefix(main_docs, min_len=5)
            if sor_base:
                for filename, info in result.items():
                    if filename.startswith(sor_base):
                        appendix_match = re.search(r'附件\s*([a-zA-Z])', filename)
                        if appendix_match:
                            letter = appendix_match.group(1).upper()
                            # 附件E：工程设计要求（含产品规格、系统方案、BOM）
                            if letter == 'E':
                                info["role"] = "eng_design"
                                info["category"] = "spec"
                                info["priority"] = max(info["priority"], 9.0)
                                info["confidence"] = "medium"
                            # 其他附件：至少提升为 spec 并给较高优先级
                            elif info["role"] in ("other", "unknown", "appendix"):
                                info["role"] = "eng_design"
                                info["category"] = "spec"
                                info["priority"] = max(info["priority"], 6.0)
                                info["confidence"] = "medium"

        # ── 后处理：确保 SOR 文档优先级最高 ──
        for filename, info in result.items():
            fn_lower = filename.lower()
            # SOR 主文件确保最高优先级
            if info["role"] == "main_sor":
                info["priority"] = max(info["priority"], 9.5)
            # BOM/零件信息确保高优先级
            if info["role"] == "bom" or ("零件信息" in fn_lower and "sor" in fn_lower):
                info["priority"] = max(info["priority"], 9.5)
            # 通用行业标准降权（SMTC/GB/ISO 等不是产品级文档）
            if re.match(r'^(smtc|gb|iso|din_|en_|iec|sae|cispr|mil)', fn_lower):
                info["priority"] = min(info["priority"], 6.0)

        return result

    async def _l2_fallback_classify(
        self,
        project_id: str,
        doc_roles: Dict[str, Dict],
    ) -> None:
        """
        L2 回退：对 role="other" 或置信度低的文档读取内容做二次分类。
        修改 doc_roles 就地。
        """
        candidates = [
            filename for filename, info in doc_roles.items()
            if info.get("confidence", "high") in ("low",)
               or info.get("role", "") in ("other", "unknown")
        ]
        if not candidates:
            return

        logger.info(f"[Indexer] L2 回退分类: {len(candidates)} 个候选文档")
        l2_count = 0

        for filename in candidates:
            try:
                content = self.kb_service.read_page(project_id, filename)
                if not content or len(content) < 100:
                    continue

                cleaned = self._clean_content(content)
                sample = cleaned[:5000]  # L2 只需要前 5000 字符做模式检测

                info = doc_roles[filename]
                updated = classify_document_l2(info, sample)

                if updated["role"] != info.get("role"):
                    l2_count += 1
                    info["role"] = updated["role"]
                    info["category"] = updated["category"]
                    info["priority"] = updated["priority"]
                    info["confidence"] = "l2"

            except Exception as e:
                logger.warning(f"[Indexer] L2 回退失败 {filename}: {e}")

        if l2_count > 0:
            logger.info(f"[Indexer] L2 回退完成: {l2_count}/{len(candidates)} 文档重新分类")

    # ═══════════════════════════════════════════════════════════
    # Step 2: 高优文档要素抽取（调 LLM）
    # ═══════════════════════════════════════════════════════════

    async def _extract_elements(
        self,
        project_id: str,
        high_priority_files: List[str],
        doc_roles: Dict[str, Dict],
        progress_callback=None,
    ) -> Dict:
        """
        从高优文档中批量抽取 FMEA 四大要素。

        分批读取文档内容，每批调用一次 LLM 提取要素。
        """
        if not high_priority_files:
            return self._empty_elements()

        llm = self._get_llm()
        llm.max_tokens = 16384

        # 解析项目名称（项目名称 = FMEA 核心对象 = 结构树根节点）
        project_name = project_id
        try:
            meta = self.kb_service._load_project_meta(project_id)
            project_name = meta.get("project_name", project_id)
        except Exception:
            pass

        # 排序：产品级文档（SOR/BOM/附件）优先处理，确保 product_definition 正确
        def _is_product_doc(fn: str) -> bool:
            fn_lower = fn.lower()
            if fn_lower.startswith(('smtc', 'gb', 'iso', 'din', 'en', 'sae', 'cispr')):
                return False
            return any(kw in fn_lower for kw in ('sor', 'bom', '零件信息', '更新通知'))

        product_first = sorted(high_priority_files, key=lambda f: (0 if _is_product_doc(f) else 1))

        # 分批
        batches = []
        for i in range(0, len(product_first), self.EXTRACT_BATCH_SIZE):
            batches.append(product_first[i:i + self.EXTRACT_BATCH_SIZE])

        logger.info(f"[Indexer] 要素抽取: {len(high_priority_files)} 文档, "
                     f"分 {len(batches)} 批")

        # 并行处理每批
        async def _process_batch(batch_files: List[str], batch_idx: int) -> Dict:
            # 加载文档内容
            contents = []
            for f in batch_files:
                content = self.kb_service.read_page(project_id, f)
                if content:
                    cleaned = self._clean_content(content)
                    if len(cleaned) > self.MAX_DOC_CHARS_FOR_EXTRACT:
                        cleaned = cleaned[:self.MAX_DOC_CHARS_FOR_EXTRACT]
                    contents.append({"filename": f, "content": cleaned,
                                     "role": doc_roles.get(f, {}).get("role", "unknown")})

            if not contents:
                return self._empty_elements()

            # 构建 prompt
            doc_sections = []
            for c in contents:
                # 每文档允许更多内容，避免截断 BOM 表格
                doc_content = c['content'][:self.MAX_PROMPT_DOC_CHARS]
                doc_sections.append(
                    f"### 文档: {c['filename']} [角色: {c['role']}]\n{doc_content}"
                )
            docs_text = "\n\n---\n\n".join(doc_sections)

            # 区分产品级文档 vs 通用标准文档
            product_doc_names = [c['filename'] for c in contents
                                 if c.get('role') in ('bom', 'main_sor', 'eng_design')
                                 and 'smtc' not in c['filename'].lower()[:5]
                                 and 'gb' not in c['filename'].lower()[:3]
                                 and 'iso' not in c['filename'].lower()[:4]]
            product_doc_hint = ", ".join(product_doc_names[:5]) if product_doc_names else "无"

            prompt = f"""你是 FMEA 结构分析专家。请从以下项目文档中提取 FMEA 四大要素。

## 项目名称（FMEA 核心对象）
{project_name} — 这是用户指定的分析对象，product_definition.description 和 structure_nodes 的根节点应与此名称一致。

## 重要区分
文档分为两类：
- **产品级文档**（含实际产品结构/规格）：{product_doc_hint}
- **通用标准文档**（行业规范，不含特定产品结构）：如规范编号开头的文件（GB/ISO/IEC/SAE等）

**产品结构树和product_definition必须从产品级文档提取**。通用标准文档只用于提取 functions（功能要求）、special_characteristics（特性要求）和 regulations（法规标准）。

## 项目文档
{docs_text}

## 提取任务
请提取以下四类 FMEA 要素，返回严格 JSON 格式：

{{
  "product_definition": {{
    "description": "实际产品的名称和总体描述（必须来自产品级文档，如 SOR/BOM/附件E）",
    "top_level_components": ["实际产品的顶层组成部分"],
    "source_files": ["来源文件名"]
  }},
  "structure_nodes": [
    {{
      "name": "组件/零件名称（必须是实际产品的组成部分，不是通用概念）",
      "level": "系统/子系统/组件/零件",
      "parent": "父级名称（顶层为空字符串）",
      "part_number": "零件号（如有）",
      "quantity": 1,
      "material": "材料牌号（如有）",
      "specification": "规格/型号（如有）",
      "source_file": "来源文件名"
    }}
  ],
  "functions": [
    {{
      "name": "功能描述",
      "scope": "所属系统/组件",
      "source_file": "来源文件名"
    }}
  ],
  "special_characteristics": [
    {{
      "name": "特性名称",
      "class": "SC/CC/空",
      "requirement": "要求值或描述",
      "scope": "所属组件",
      "source_file": "来源文件名"
    }}
  ],
  "regulations": [
    {{
      "name": "法规/标准名称",
      "scope": "适用范围",
      "source_file": "来源文件名"
    }}
  ]
}}

## 层级定义
- 系统(System): 完整产品/总成（如：BMS系统、电池系统、动力总成系统）
- 子系统(Subsystem): 产品的主要功能模块（如：主控制器、传感器模块、电源模块）
- 组件(Component): 可独立替换的功能单元（如：MCU芯片、电压采集芯片、通讯接口）
- 零件(Part): 最小不可拆分的单元（如：电阻、电容、连接器、PCB板）

## 提取规则
1. **产品级优先**：structure_nodes 和 product_definition 只能从产品级文档（SOR、BOM、附件）提取
2. **通用标准不提取结构**：从行业通用标准（GB/ISO/IEC/SAE等编号开头文件）中只提取 functions、special_characteristics、regulations，不提取 structure_nodes
3. **BOM 表格优先**：如果文档含 Markdown 表格，优先从表格中提取层级关系
4. **层级完整性**：确保每个节点都有正确的 parent 字段，顶层节点的 parent 为空字符串
5. **零件号提取**：从“零件号/Part No./型号/Model/PN”列提取
6. **只提取明确信息**：不要推测或编造，只提取文档中明确提到的要素
7. **source_file 必须**是上面给出的文档文件名之一
8. 如果某类要素在当前文档中不存在，返回空数组
9. 严格返回 JSON，不要加任何解释"""

            return await self._call_llm_with_retry(
                llm, prompt, f"batch_{batch_idx}"
            )

        # 并行执行所有批次
        tasks = [_process_batch(batch, i) for i, batch in enumerate(batches)]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        # 合并结果
        merged = self._empty_elements()
        for i, result in enumerate(batch_results):
            if isinstance(result, Exception):
                logger.error(f"[Indexer] 批次 {i} 要素抽取失败: {result}")
                continue
            if isinstance(result, dict):
                self._merge_elements(merged, result)

            if progress_callback:
                progress_pct = 0.3 + (i + 1) / len(batches) * 0.5
                self._notify(progress_callback, "extracting", progress_pct,
                             f"要素抽取: 批次 {i+1}/{len(batches)} 完成")

        # 去重
        self._deduplicate_elements(merged)

        # 更新 doc_roles 中的 elements 字段
        self._update_role_elements(doc_roles, merged)

        # 统计日志
        stats = {
            "structure_nodes": len(merged.get("structure_nodes", [])),
            "functions": len(merged.get("functions", [])),
            "special_characteristics": len(merged.get("special_characteristics", [])),
            "regulations": len(merged.get("regulations", [])),
        }
        logger.info(f"[Indexer] 要素抽取完成: {stats}")

        return merged

    # ═══════════════════════════════════════════════════════════
    # Step 3: 构建关联映射
    # ═══════════════════════════════════════════════════════════

    def _build_relevance_map(
        self,
        doc_roles: Dict[str, Dict],
        fmea_elements: Dict,
    ) -> Dict:
        """
        构建 FMEA 步骤 → 相关文档的映射。
        """
        relevance_map = {
            "structure_tree": [],
            "function_analysis": [],
            "failure_analysis": [],
            "risk_assessment": [],
        }

        for filename, info in doc_roles.items():
            role = info["role"]
            priority = info["priority"]

            # 结构树: BOM、主文件、工程设计、图纸、仿真报告、原理图
            if role in ("bom", "main_sor", "eng_design", "schematic",
                        "product_spec", "drawing", "simulation_report") and priority >= 4:
                relevance_map["structure_tree"].append(filename)

            # 功能分析: 主文件、工程设计、产品要求、特殊特性
            if role in ("main_sor", "eng_design", "special_chars") and priority >= 4:
                relevance_map["function_analysis"].append(filename)

            # 失效分析: 工程设计、特殊特性、测试规范
            if role in ("eng_design", "special_chars", "test_spec", "schematic") and priority >= 4:
                relevance_map["failure_analysis"].append(filename)

            # 风险评估: 特殊特性、测试规范、法规、标签规范
            if role in ("special_chars", "test_spec", "legal", "label_spec") and priority >= 3:
                relevance_map["risk_assessment"].append(filename)

        # 补充：从要素的 source_file 反向映射
        for node in fmea_elements.get("structure_nodes", []):
            sf = node.get("source_file", "")
            if sf and sf not in relevance_map["structure_tree"]:
                relevance_map["structure_tree"].append(sf)

        for func in fmea_elements.get("functions", []):
            sf = func.get("source_file", "")
            if sf and sf not in relevance_map["function_analysis"]:
                relevance_map["function_analysis"].append(sf)

        for char in fmea_elements.get("special_characteristics", []):
            sf = char.get("source_file", "")
            if sf and sf not in relevance_map["failure_analysis"]:
                relevance_map["failure_analysis"].append(sf)
            if sf and sf not in relevance_map["risk_assessment"]:
                relevance_map["risk_assessment"].append(sf)

        for reg in fmea_elements.get("regulations", []):
            sf = reg.get("source_file", "")
            if sf and sf not in relevance_map["risk_assessment"]:
                relevance_map["risk_assessment"].append(sf)

        # 日志
        for task, files in relevance_map.items():
            logger.info(f"[Indexer] relevance_map[{task}]: {len(files)} 个相关文档")

        return relevance_map

    # ═══════════════════════════════════════════════════════════
    # 索引读写
    # ═══════════════════════════════════════════════════════════

    def _save_index(self, project_id: str, index: Dict):
        """持久化索引到 fmea_index.json"""
        index_path = self.kb_service.base_dir / project_id / "fmea_index.json"
        index_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"[Indexer] 索引已保存: {index_path}")

    def load_index(self, project_id: str) -> Optional[Dict]:
        """加载已有的 FMEA 索引，不存在返回 None"""
        index_path = self.kb_service.base_dir / project_id / "fmea_index.json"
        if not index_path.exists():
            return None
        try:
            return json.loads(index_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[Indexer] 加载索引失败: {e}")
            return None

    def index_exists(self, project_id: str) -> bool:
        """检查索引是否存在"""
        index_path = self.kb_service.base_dir / project_id / "fmea_index.json"
        return index_path.exists()

    # ═══════════════════════════════════════════════════════════
    # 索引查询接口（供 structure_extractor 等模块调用）
    # ═══════════════════════════════════════════════════════════

    def get_relevant_files(self, index: Dict, task: str = "structure_tree") -> List[str]:
        """获取某个 FMEA 任务的相关文档列表"""
        relevance_map = index.get("relevance_map", {})
        return relevance_map.get(task, [])

    def get_high_priority_files(self, index: Dict, min_priority: float = 5.0) -> List[str]:
        """获取高优先级文档列表"""
        doc_roles = index.get("document_roles", {})
        return [
            filename for filename, info in doc_roles.items()
            if info.get("priority", 0) >= min_priority
        ]

    def get_doc_role(self, index: Dict, filename: str) -> Dict:
        """获取文档的角色信息"""
        doc_roles = index.get("document_roles", {})
        return doc_roles.get(filename, {"role": "unknown", "priority": 0})

    def get_seed_tree(self, index: Dict) -> List[Dict]:
        """
        从索引中提取结构树的种子节点（供 structure_extractor 使用）。
        返回 structure_nodes 列表，可直接用于初始化 tree。
        """
        return index.get("fmea_elements", {}).get("structure_nodes", [])

    def get_product_definition(self, index: Dict) -> Dict:
        """获取产品定义"""
        return index.get("fmea_elements", {}).get("product_definition", {})

    # ═══════════════════════════════════════════════════════════
    # 增量更新（添加文件时使用）
    # ═══════════════════════════════════════════════════════════

    async def incremental_update(
        self,
        project_id: str,
        new_files: List[str],
        progress_callback=None,
    ) -> Dict:
        """
        增量更新索引：只处理新增的文件。

        Args:
            project_id: 项目ID
            new_files: 新增的 MD 文件名列表
            progress_callback: 进度回调

        Returns:
            更新后的索引
        """
        index = self.load_index(project_id)
        if not index:
            # 索引不存在，全量构建
            logger.info(f"[Indexer] 索引不存在，执行全量构建")
            return await self.build_index(project_id, progress_callback)

        logger.info(f"[Indexer] 增量更新: {len(new_files)} 个新文件")

        # Step 1: 对新文件做规则分类
        new_pages = []
        all_pages = self.kb_service.list_pages(project_id)
        new_pages = [p for p in all_pages if p.get("filename") in new_files]

        new_roles = self._classify_by_rules(new_pages)

        # 合并到现有索引
        index["document_roles"].update(new_roles)

        # Step 2: 对高优新文件做要素抽取
        high_priority_new = [
            f for f, info in new_roles.items()
            if info["priority"] >= self.HIGH_PRIORITY_THRESHOLD
        ]

        if high_priority_new:
            new_elements = await self._extract_elements(
                project_id, high_priority_new, index["document_roles"], progress_callback
            )
            self._merge_elements(index["fmea_elements"], new_elements)
            self._deduplicate_elements(index["fmea_elements"])

        # Step 3: 重建关联映射
        index["relevance_map"] = self._build_relevance_map(
            index["document_roles"], index["fmea_elements"]
        )

        # 更新元数据
        index["doc_count"] = len(index["document_roles"])
        index["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

        # 持久化
        self._save_index(project_id, index)

        logger.info(f"[Indexer] 增量更新完成: +{len(new_files)} 文档")
        return index

    # ═══════════════════════════════════════════════════════════
    # LLM 调用（带重试）
    # ═══════════════════════════════════════════════════════════

    async def _call_llm_with_retry(self, llm, prompt: str, pass_name: str) -> Dict:
        """带重试的 LLM JSON 调用"""
        last_error = None
        for attempt in range(1, self.LLM_MAX_RETRIES + 2):
            try:
                result = await llm.call_complete(
                    system_prompt="你是 FMEA 结构分析专家。严格按 JSON 格式返回，不要添加 ```json``` 标记，不要额外解释。",
                    user_message=prompt,
                    force_json=False,
                )
                if not result or not result.strip():
                    logger.warning(f"[Indexer] {pass_name} 第{attempt}次: LLM返回空响应")
                    if attempt <= self.LLM_MAX_RETRIES:
                        await asyncio.sleep(self.LLM_RETRY_DELAY)
                        continue
                    return self._empty_elements()

                data = llm.extract_json(result)
                if isinstance(data, dict):
                    return data
                logger.warning(f"[Indexer] {pass_name} 返回非 dict: {type(data)}")
                return self._empty_elements()

            except json.JSONDecodeError as e:
                logger.error(f"[Indexer] {pass_name} 第{attempt}次 JSON解析失败: {e}")
                last_error = e
                if attempt <= self.LLM_MAX_RETRIES:
                    await asyncio.sleep(self.LLM_RETRY_DELAY)
                    continue

            except Exception as e:
                error_msg = str(e)
                is_server_error = any(kw in error_msg.lower() for kw in
                                      ["500", "502", "503", "504", "internal server",
                                       "connection", "timeout", "refused"])
                logger.error(f"[Indexer] {pass_name} 第{attempt}次 LLM调用失败: {e}"
                            + (" (服务端错误，将重试)" if is_server_error else ""))
                last_error = e
                if is_server_error and attempt <= self.LLM_MAX_RETRIES:
                    await asyncio.sleep(self.LLM_RETRY_DELAY)
                    continue

        logger.error(f"[Indexer] {pass_name} 所有重试耗尽: {last_error}")
        return self._empty_elements()

    # ═══════════════════════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════════════════════

    def _clean_content(self, content: str) -> str:
        """清理文档内容：移除 frontmatter、wiki 链接等"""
        if content.strip().startswith("---"):
            lines = content.split("\n")
            end_idx = -1
            for i in range(1, min(len(lines), 20)):
                if lines[i].strip() == "---":
                    end_idx = i
                    break
            if end_idx > 0:
                content = "\n".join(lines[end_idx + 1:])
        content = re.sub(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]', r'\1', content)
        content = re.sub(r'!\[.*?\]\(.*?\)', '', content)
        return content.strip()

    @staticmethod
    def _common_prefix(strings: List[str], min_len: int = 3) -> str:
        """提取字符串列表的最长公共前缀"""
        if not strings:
            return ""
        shortest = min(strings, key=len)
        for i in range(len(shortest), min_len - 1, -1):
            prefix = shortest[:i]
            if all(s.startswith(prefix) for s in strings):
                return prefix
        return ""

    @staticmethod
    def _role_summary(doc_roles: Dict) -> Dict:
        """统计角色分布"""
        counts = {}
        for info in doc_roles.values():
            r = info.get("role", "unknown")
            counts[r] = counts.get(r, 0) + 1
        return counts

    @staticmethod
    def _empty_index(project_id: str) -> Dict:
        """返回空索引"""
        return {
            "project_id": project_id,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "doc_count": 0,
            "document_roles": {},
            "fmea_elements": FmeaIndexer._empty_elements(),
            "relevance_map": {
                "structure_tree": [],
                "function_analysis": [],
                "failure_analysis": [],
                "risk_assessment": [],
            },
        }

    @staticmethod
    def _empty_elements() -> Dict:
        """返回空要素"""
        return {
            "product_definition": {"description": "", "top_level_components": [], "source_files": []},
            "structure_nodes": [],
            "functions": [],
            "special_characteristics": [],
            "regulations": [],
        }

    @staticmethod
    def _merge_elements(target: Dict, source: Dict):
        """合并要素结果到 target（就地修改）"""
        # product_definition: 取非空的
        if source.get("product_definition", {}).get("description"):
            if not target.get("product_definition", {}).get("description"):
                target["product_definition"] = source["product_definition"]
            else:
                # 合并 top_level_components
                existing = set(target["product_definition"].get("top_level_components", []))
                for comp in source["product_definition"].get("top_level_components", []):
                    if comp not in existing:
                        target["product_definition"].setdefault("top_level_components", []).append(comp)
                # 合并 source_files
                for sf in source["product_definition"].get("source_files", []):
                    if sf not in target["product_definition"].get("source_files", []):
                        target["product_definition"].setdefault("source_files", []).append(sf)

        # 列表型要素直接追加
        for key in ("structure_nodes", "functions", "special_characteristics", "regulations"):
            target.setdefault(key, []).extend(source.get(key, []))

    @staticmethod
    def _deduplicate_elements(elements: Dict):
        """去重要素"""
        # structure_nodes 按 name+parent 去重
        seen = set()
        unique = []
        for node in elements.get("structure_nodes", []):
            key = (node.get("name", ""), node.get("parent", ""))
            if key not in seen:
                seen.add(key)
                unique.append(node)
        elements["structure_nodes"] = unique

        # functions 按 name+scope 去重
        seen = set()
        unique = []
        for func in elements.get("functions", []):
            key = (func.get("name", ""), func.get("scope", ""))
            if key not in seen:
                seen.add(key)
                unique.append(func)
        elements["functions"] = unique

        # special_characteristics 按 name+scope 去重
        seen = set()
        unique = []
        for char in elements.get("special_characteristics", []):
            key = (char.get("name", ""), char.get("scope", ""))
            if key not in seen:
                seen.add(key)
                unique.append(char)
        elements["special_characteristics"] = unique

        # regulations 按 name 去重
        seen = set()
        unique = []
        for reg in elements.get("regulations", []):
            key = reg.get("name", "")
            if key not in seen:
                seen.add(key)
                unique.append(reg)
        elements["regulations"] = unique

    @staticmethod
    def _update_role_elements(doc_roles: Dict, elements: Dict):
        """更新 doc_roles 中的 elements 字段"""
        # 从要素的 source_file 反向映射到文档
        file_elements = {}  # filename -> [element_types]

        for node in elements.get("structure_nodes", []):
            sf = node.get("source_file", "")
            if sf:
                file_elements.setdefault(sf, set()).add("structure")

        for func in elements.get("functions", []):
            sf = func.get("source_file", "")
            if sf:
                file_elements.setdefault(sf, set()).add("function")

        for char in elements.get("special_characteristics", []):
            sf = char.get("source_file", "")
            if sf:
                file_elements.setdefault(sf, set()).add("characteristic")

        for reg in elements.get("regulations", []):
            sf = reg.get("source_file", "")
            if sf:
                file_elements.setdefault(sf, set()).add("regulation")

        for filename, elem_types in file_elements.items():
            if filename in doc_roles:
                doc_roles[filename]["elements"] = sorted(elem_types)

    def _notify(self, callback, phase: str, progress: float, message: str):
        """通知进度"""
        if callback:
            try:
                callback(phase, progress, message)
            except Exception:
                pass
        logger.info(f"[Indexer] [{phase}] {progress:.0%} - {message}")


# ═══════════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════════

_indexer: Optional[FmeaIndexer] = None


def get_fmea_indexer() -> FmeaIndexer:
    global _indexer
    if _indexer is None:
        _indexer = FmeaIndexer()
    return _indexer
