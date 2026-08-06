"""
法规库管理 — 统一从 WeKnora 知识库动态加载

提供：
- 从 WeKnora 知识库按法规编号检索条款（动态、无硬编码）
- 本地 JSON 缓存（可选）
- 条款全文存储与查询
- 支持从项目配置加载法规清单

注意：本模块不内置任何硬编码法规数据。
所有条款均通过 WeKnora 知识库动态检索获取，
确保法规内容与最新版本一致。
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class RegulationClause:
    """法规条款数据模型"""
    regulation_id: str          # 如 "GB38031-2020"
    clause_id: str              # 如 "§5.1.2"
    full_text: str              # 完整条款原文
    product_function: str = ""  # 对应产品功能
    potential_risk: str = ""    # 潜在失效风险
    responsible_module: str = ""  # 管控设计模块
    source: str = ""            # 来源文件/条款原文出处


class RegulationLibrary:
    """法规库管理器（WeKnora 驱动，无硬编码）

    用法：
        lib = RegulationLibrary("data/regulations")
        # kb_id 必传，WeKnora 是数据源
        clauses = lib.load_regulations(["GB38031-2020", "R100"], kb_id="kb-xxx")
    """

    def __init__(self, data_dir: str = "data/regulations", weknora_client=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._weknora = weknora_client
        self._index: Dict[str, RegulationClause] = {}
        self._by_regulation: Dict[str, List[str]] = {}  # reg_id → [clause_ids]

    # ════════════════════════════════════════════════════════════
    # 加载
    # ════════════════════════════════════════════════════════════

    def load_regulations(
        self,
        regulation_list: List[str],
        kb_id: str = "",
    ) -> List[RegulationClause]:
        """从项目配置的法规清单加载条款

        加载顺序（无硬编码兜底）：
        1. 优先从本地 data/regulations/ 目录加载 JSON
        2. 本地无数据且提供 kb_id 时，从 WeKnora 知识库动态检索
        3. 两者都无数据时返回空列表（不做硬编码兜底）
        """
        if not regulation_list:
            logger.warning("[法规库] regulation_list 为空")
            return []

        loaded: List[RegulationClause] = []
        for reg_id in regulation_list:
            local = self._load_single(reg_id)
            if local:
                loaded.extend(local)
                logger.info(f"[法规库] 本地加载 {reg_id}: {len(local)} 条")
            else:
                logger.info(f"[法规库] {reg_id} 无本地数据")

        # 本地无任何数据时，尝试 WeKnora 检索
        if not loaded and kb_id:
            loaded = self._load_from_weknora(regulation_list, kb_id)
            if loaded:
                logger.info(f"[法规库] WeKnora 检索共 {len(loaded)} 条")

        if not loaded:
            logger.warning(
                f"[法规库] 法规清单 {regulation_list} 无任何数据，"
                f"请检查本地 JSON 或 WeKnora 知识库 (kb_id={kb_id})"
            )

        return loaded

    def _load_from_weknora(
        self, regulation_list: List[str], kb_id: str
    ) -> List[RegulationClause]:
        """从 WeKnora 知识库动态检索法规条款

        对每个法规标准用其编号作为查询词进行 hybrid_search，
        提取命中 chunk 的 content 作为条款原文。
        """
        wk = self._weknora
        if wk is None:
            try:
                from knowledge.weknora_client import get_weknora_client
                wk = get_weknora_client()
                self._weknora = wk
            except Exception as e:
                logger.error(f"[法规库] 无法获取 WeKnora 客户端: {e}")
                return []

        seen_chunks: set = set()
        all_clauses: List[RegulationClause] = []

        for reg_id in regulation_list:
            try:
                results = wk.hybrid_search(
                    kb_id, query_text=reg_id, match_count=30
                )
            except Exception as e:
                logger.warning(f"[法规库] WeKnora 检索 {reg_id} 失败: {e}")
                continue

            for r in results:
                chunk_id = r.get("id", "")
                if not chunk_id or chunk_id in seen_chunks:
                    continue
                seen_chunks.add(chunk_id)

                content = r.get("content", "")
                if not content:
                    continue

                clause = RegulationClause(
                    regulation_id=reg_id,
                    clause_id="",  # 检索结果无 clause_id，由 LLM 后续解读提取
                    full_text=content[:3000],
                    product_function="",
                    potential_risk="",
                    responsible_module="",
                    source=r.get("knowledge_title", r.get("knowledge_filename", "")),
                )
                self._index_clause(clause)
                all_clauses.append(clause)

            logger.info(f"[法规库] WeKnora 检索 {reg_id}: {len(results)} chunks")

        return all_clauses

    def _load_single(self, reg_id: str) -> List[RegulationClause]:
        """从本地 JSON 加载单个标准的条款"""
        filepath = self.data_dir / f"{reg_id}.json"
        if not filepath.exists():
            return []

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            clauses = []
            for item in data.get("clauses", []):
                clause = RegulationClause(
                    regulation_id=reg_id,
                    clause_id=item.get("clause_id", ""),
                    full_text=item.get("full_text", ""),
                    product_function=item.get("product_function", ""),
                    potential_risk=item.get("potential_risk", ""),
                    responsible_module=item.get("responsible_module", ""),
                )
                self._index_clause(clause)
                clauses.append(clause)

            return clauses
        except Exception as e:
            logger.error(f"[法规库] 加载 {reg_id} 失败: {e}")
            return []

    def _index_clause(self, clause: RegulationClause):
        """将条款加入内存索引"""
        key = f"{clause.regulation_id}::{clause.clause_id}"
        self._index[key] = clause

        if clause.regulation_id not in self._by_regulation:
            self._by_regulation[clause.regulation_id] = []
        self._by_regulation[clause.regulation_id].append(clause.clause_id)

    # ════════════════════════════════════════════════════════════
    # 检索
    # ════════════════════════════════════════════════════════════

    def search(self, keyword: str) -> List[RegulationClause]:
        """关键词搜索条款"""
        results = []
        kw_lower = keyword.lower()
        for clause in self._index.values():
            text = f"{clause.regulation_id} {clause.clause_id} {clause.full_text} "
            text += f"{clause.product_function} {clause.potential_risk}".lower()
            if kw_lower in text:
                results.append(clause)
        return results

    def search_by_module(self, module: str) -> List[RegulationClause]:
        """按管控模块搜索"""
        return [
            c for c in self._index.values()
            if module in c.responsible_module
        ]

    def get_clause(self, reg_id: str, clause_id: str) -> Optional[RegulationClause]:
        """精确获取条款"""
        key = f"{reg_id}::{clause_id}"
        return self._index.get(key)

    def get_all_by_regulation(self, reg_id: str) -> List[RegulationClause]:
        """获取某个标准的所有条款"""
        clause_ids = self._by_regulation.get(reg_id, [])
        return [self._index[f"{reg_id}::{cid}"] for cid in clause_ids
                if f"{reg_id}::{cid}" in self._index]

    # ════════════════════════════════════════════════════════════
    # 数据管理
    # ════════════════════════════════════════════════════════════

    def save_regulation(self, reg_id: str, clauses: List[Dict[str, str]]):
        """保存标准条款到本地 JSON"""
        filepath = self.data_dir / f"{reg_id}.json"
        data = {
            "regulation_id": reg_id,
            "clauses": clauses,
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"[法规库] 已保存 {reg_id} ({len(clauses)} 条) → {filepath}")

    def get_statistics(self) -> Dict[str, Any]:
        """获取法规库统计"""
        return {
            "total_clauses": len(self._index),
            "total_regulations": len(self._by_regulation),
            "regulations": list(self._by_regulation.keys()),
        }

    # ════════════════════════════════════════════════════════════
    # 注意：本模块不再提供任何硬编码法规数据
    # 所有条款必须通过 WeKnora 知识库动态检索获取
    # ════════════════════════════════════════════════════════════
