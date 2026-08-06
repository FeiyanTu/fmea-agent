"""
知识库管理服务 — 以项目为单位管理知识库

移植自 LLMWiKiDemo 的 WikiIngestService + WikiFileService

核心功能:
1. 项目知识库初始化：将 SOR 文件夹所有文件转为 MD，存入 knowledge/<project_id>/
2. 索引生成：生成 index.md（所有页面索引），供查询使用
3. 知识库状态管理
4. MD 文件管理（读取、列表、搜索）
"""
import json
import logging
from pathlib import Path
from typing import Optional, Dict, List, Callable, Awaitable
from datetime import datetime

from knowledge.file_converter import scan_directory, load_document

logger = logging.getLogger(__name__)

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"


class KnowledgeService:
    """项目知识库管理服务"""

    def __init__(self, base_dir: str = None):
        """
        Args:
            base_dir: 知识库根目录，默认 D:/agent-fmea/fmea-agent/knowledge/
        """
        self.base_dir = Path(base_dir) if base_dir else KNOWLEDGE_ROOT
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # ═══════════════════════════════════════════════════════════
    # 项目初始化
    # ═══════════════════════════════════════════════════════════

    def init_project_knowledge(
        self,
        project_id: str,
        project_name: str,
        sor_folder: str,
        progress_callback: Optional[Callable] = None,
    ) -> Dict:
        """
        初始化项目知识库 — v3 轻量模式。

        Phase 1: 扫描文件（仅元信息，不加载内容）
        Phase 2: 保存文件清单到项目元数据
        Phase 3: 构建 FMEA 索引（按需加载内容）

        Args:
            project_id: 项目唯一标识
            project_name: 项目名称
            sor_folder: SOR 文件夹路径
            progress_callback: 进度回调 (phase, progress:float 0-1, message)

        Returns:
            初始化结果
        """
        sor_path = Path(sor_folder)
        if not sor_path.exists():
            raise FileNotFoundError(f"SOR 文件夹不存在: {sor_folder}")

        project_kb_dir = self.base_dir / project_id
        project_kb_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"[KB] 初始化项目知识库: {project_name} (ID: {project_id})")
        logger.info(f"[KB] SOR 文件夹: {sor_folder}")
        logger.info(f"[KB] 知识库目录: {project_kb_dir}")

        result = {
            "project_id": project_id,
            "project_name": project_name,
            "sor_folder": str(sor_path),
            "kb_dir": str(project_kb_dir),
            "phase": "init",
            "scan": None,
            "index": None,
        }

        # Phase 1: 扫描文件（仅元信息）
        if progress_callback:
            progress_callback("scanning", 0.0, "正在扫描文件（仅记录元信息）...")

        try:
            file_list = scan_directory(str(sor_path))
            result["scan"] = {
                "total_files": len(file_list),
                "files": file_list,
            }
            if progress_callback:
                progress_callback("scanning", 0.3,
                                  f"扫描完成：{len(file_list)} 个文件")
        except Exception as e:
            logger.error(f"[KB] 文件扫描失败: {e}")
            result["scan"] = {"total_files": 0, "files": [], "error": str(e)}

        # Phase 2: 保存项目元数据（含文件清单）
        self._save_project_meta(project_id, project_name, sor_folder, result)

        # Phase 3: 构建 FMEA 索引（从文件清单中按需加载高优文档）
        if progress_callback:
            progress_callback("indexing", 0.5, "正在构建 FMEA 索引...")

        try:
            index_result = self.build_index(project_id)
            result["index"] = index_result
        except Exception as e:
            logger.error(f"[KB] 索引生成失败: {e}")
            result["index"] = {"success": False, "error": str(e)}

        if progress_callback:
            progress_callback("done", 1.0,
                              f"初始化完成：{result['scan'].get('total_files', 0)} 个文件")

        logger.info(f"[KB] 项目知识库初始化完成: "
                     f"{result['scan'].get('total_files', 0)} 个文件")

        return result

    # ═══════════════════════════════════════════════════════════
    # 索引管理
    # ═══════════════════════════════════════════════════════════

    def build_index(self, project_id: str) -> Dict:
        """
        为项目知识库生成 index.md

        参考 WikiIngestService 和 CompileService 的索引生成逻辑

        index.md 格式:
        ```
        # 项目名称 知识库索引

        ## 分类1
        - [页面标题](页面文件名.md) — 摘要

        ## 分类2
        ...
        ```
        """
        project_kb_dir = self.base_dir / project_id

        if not project_kb_dir.exists():
            return {"success": False, "error": f"项目知识库目录不存在: {project_kb_dir}"}

        # 收集所有 MD 文件
        md_files = list(project_kb_dir.glob("*.md"))
        if not md_files:
            return {"success": True, "page_count": 0, "message": "知识库为空"}

        # 提取每个文件的信息
        pages = []
        for f in md_files:
            info = self._extract_page_info(f)
            pages.append(info)

        # 按 title 排序
        pages.sort(key=lambda p: p.get("title", p.get("filename", "")))

        # 生成索引内容
        meta = self._load_project_meta(project_id)
        project_name = meta.get("project_name", project_id)

        index_lines = [
            f"# {project_name} — 知识库索引",
            f"",
            f"> 生成时间: {datetime.now().isoformat()}",
            f"> 文档数量: {len(pages)}",
            f"> 项目ID: {project_id}",
            f"",
            "---",
            "",
        ]

        # 按 source_type 分类
        by_type = {}
        for p in pages:
            st = p.get("source_type", "other")
            if st not in by_type:
                by_type[st] = []
            by_type[st].append(p)

        type_names = {
            ".pdf": "PDF 文档",
            ".docx": "Word 文档",
            ".xlsx": "Excel 表格",
            ".xls": "Excel 表格",
            ".pptx": "PPT 演示",
            ".ppt": "PPT 演示",
            ".md": "Markdown 文档",
            ".txt": "纯文本文档",
            ".csv": "CSV 数据",
            ".html": "HTML 文档",
            ".htm": "HTML 文档",
            ".json": "JSON 数据",
            ".xml": "XML 数据",
            ".yaml": "YAML 配置",
            ".yml": "YAML 配置",
            "other": "其他文档",
        }

        for st in sorted(by_type.keys()):
            category = type_names.get(st, f"{st} 文档")
            index_lines.append(f"## {category}")
            index_lines.append("")

            for p in by_type[st]:
                title = p.get("title", p.get("filename", "未知"))
                filename = p.get("filename", "")
                summary = p.get("summary", "")
                size_kb = p.get("size_kb", 0)

                line = f"- **[{title}]({filename})**"
                if summary:
                    # 截断摘要
                    short_summary = summary[:80] + "..." if len(summary) > 80 else summary
                    line += f" — {short_summary}"
                line += f" _({size_kb}KB)_"
                index_lines.append(line)

            index_lines.append("")

        # 写入 index.md
        index_path = project_kb_dir / "index.md"
        index_path.write_text("\n".join(index_lines), encoding="utf-8")

        # 生成 MOC.md（Map of Content — 快速导航）
        moc_lines = [
            f"# {project_name} — 内容地图 (MOC)",
            "",
            "## 按类型浏览",
            "",
        ]
        for st in sorted(by_type.keys()):
            category = type_names.get(st, f"{st} 文档")
            count = len(by_type[st])
            moc_lines.append(f"- [{category}](#{category.replace(' ', '-').lower()}) — {count} 篇")

        moc_lines.extend(["", "## 全文搜索", "", "请使用查询功能搜索知识库内容。", ""])

        moc_path = project_kb_dir / "MOC.md"
        moc_path.write_text("\n".join(moc_lines), encoding="utf-8")

        logger.info(f"[KB] 索引已生成: {index_path} ({len(pages)} 页)")

        return {
            "success": True,
            "page_count": len(pages),
            "index_file": str(index_path),
            "moc_file": str(moc_path),
            "categories": list(by_type.keys()),
        }

    # ═══════════════════════════════════════════════════════════
    # 查询操作
    # ═══════════════════════════════════════════════════════════

    def list_pages(self, project_id: str) -> List[Dict]:
        """列出项目中的所有文件（从扫描清单获取）"""
        meta = self._load_project_meta(project_id)
        scan = meta.get("scan", {})
        files = scan.get("files", [])

        if not files:
            return []

        pages = []
        for f in files:
            pages.append({
                "filename": f.get("rel_path", f.get("filename", "")),
                "title": f.get("filename", ""),
                "summary": "",
                "source_file": f.get("filename", ""),
                "source_type": f.get("extension", ""),
                "size_kb": f.get("size_kb", 0),
                "abs_path": f.get("abs_path", ""),
            })

        return pages

    def read_page(self, project_id: str, filename: str) -> Optional[str]:
        """读取文档内容（从原始文件按需加载）"""
        # 从扫描清单中找到文件的绝对路径
        meta = self._load_project_meta(project_id)
        scan = meta.get("scan", {})
        files = scan.get("files", [])

        # 匹配文件名（rel_path 或 filename）
        abs_path = None
        for f in files:
            if f.get("rel_path") == filename or f.get("filename") == filename:
                abs_path = f.get("abs_path")
                break

        if not abs_path:
            # 旧版兼容：检查 kb_dir 下的 .md 文件
            filepath = self.base_dir / project_id / filename
            if not filepath.exists() and not filename.endswith(".md"):
                filepath = self.base_dir / project_id / (filename + ".md")
            if filepath.exists():
                try:
                    return filepath.read_text(encoding="utf-8")
                except Exception:
                    pass
            return None

        # 按需加载原始文件
        try:
            doc, _ = load_document(abs_path)
            # 组装文本内容（含表格摘要）
            parts = [doc.get("text", "")]
            for t in doc.get("tables", []):
                headers = t.get("headers", [])
                rows = t.get("rows", [])
                if headers and rows:
                    parts.append(f"\n### {t.get('caption', '表格')}")
                    parts.append("| " + " | ".join(headers) + " |")
                    parts.append("|" + "|".join(["---"] * len(headers)) + "|")
                    for row in rows[:50]:
                        parts.append("| " + " | ".join(str(c) for c in row) + " |")
            return "\n\n".join(parts)
        except Exception as e:
            logger.warning(f"[KB] 按需加载失败 {filename}: {e}")
            return None

    def read_index(self, project_id: str) -> Optional[str]:
        """读取知识库索引"""
        return self.read_page(project_id, "index.md")

    def search_pages(self, project_id: str, keyword: str) -> List[Dict]:
        """关键词搜索知识库页面"""
        project_kb_dir = self.base_dir / project_id
        if not project_kb_dir.exists():
            return []

        results = []
        keywords = keyword.lower().split()

        for f in project_kb_dir.glob("*.md"):
            if f.name in ("index.md", "MOC.md"):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                lower_content = content.lower()

                # 计算匹配度
                match_score = sum(1 for kw in keywords if kw in lower_content)
                if match_score > 0:
                    info = self._extract_page_info(f)
                    info["match_score"] = match_score
                    # 找到第一个匹配关键词的上下文
                    for kw in keywords:
                        idx = lower_content.find(kw)
                        if idx >= 0:
                            start = max(0, idx - 50)
                            end = min(len(content), idx + 100)
                            snippet = content[start:end].replace("\n", " ")
                            info["snippet"] = f"...{snippet}..."
                            break
                    results.append(info)
            except Exception as e:
                logger.warning(f"[KB] 搜索页面失败: {f}: {e}")

        # 按匹配度排序
        results.sort(key=lambda r: r.get("match_score", 0), reverse=True)
        return results[:10]  # 最多返回 10 个

    def get_status(self, project_id: str) -> Dict:
        """获取项目知识库状态"""
        project_kb_dir = self.base_dir / project_id
        if not project_kb_dir.exists():
            return {"exists": False, "project_id": project_id}

        md_files = list(project_kb_dir.glob("*.md"))
        total_size = sum(f.stat().st_size for f in md_files)

        return {
            "exists": True,
            "project_id": project_id,
            "kb_dir": str(project_kb_dir),
            "total_pages": len(md_files),
            "total_size_kb": total_size // 1024,
            "has_index": (project_kb_dir / "index.md").exists(),
            "has_moc": (project_kb_dir / "MOC.md").exists(),
        }

    # ═══════════════════════════════════════════════════════════
    # 内部辅助方法
    # ═══════════════════════════════════════════════════════════

    def _extract_page_info(self, filepath: Path) -> Dict:
        """从 MD 文件中提取页面信息（标题、摘要、元数据）"""
        info = {
            "filename": filepath.name,
            "path": str(filepath),
            "size_kb": filepath.stat().st_size // 1024,
            "title": filepath.stem,
            "summary": "",
            "source_file": "",
            "source_type": "",
            "converted_at": "",
        }

        try:
            content = filepath.read_text(encoding="utf-8")

            # 解析 frontmatter
            if content.strip().startswith("---"):
                lines = content.split("\n")
                end_idx = -1
                for i in range(1, min(len(lines), 20)):
                    if lines[i].strip() == "---":
                        end_idx = i
                        break

                if end_idx > 0:
                    for line in lines[1:end_idx]:
                        line = line.strip()
                        if ":" in line:
                            key, value = line.split(":", 1)
                            key = key.strip()
                            value = value.strip().strip('"').strip("'")
                            if key == "title":
                                info["title"] = value
                            elif key == "source_file":
                                info["source_file"] = value
                            elif key == "source_type":
                                info["source_type"] = value
                            elif key == "converted_at":
                                info["converted_at"] = value

                    # 正文前 200 字符作为摘要
                    body_start = end_idx + 1
                    body = content[body_start:].strip()
                    if body:
                        # 跳过标题行
                        body_lines = body.split("\n")
                        summary_lines = []
                        for bl in body_lines:
                            stripped = bl.strip()
                            if stripped and not stripped.startswith("#"):
                                summary_lines.append(stripped)
                            if len(" ".join(summary_lines)) > 150:
                                break
                        info["summary"] = " ".join(summary_lines)[:200]
        except Exception as e:
            logger.warning(f"[KB] 提取页面信息失败: {filepath}: {e}")

        return info

    def _save_project_meta(self, project_id: str, project_name: str,
                           sor_folder: str, init_result: Dict):
        """保存项目元数据"""
        meta = {
            "project_id": project_id,
            "project_name": project_name,
            "sor_folder": sor_folder,
            "scan": init_result.get("scan", {}),       # 文件清单
            "index": init_result.get("index", {}),     # FMEA 索引
            "initialized_at": datetime.now().isoformat(),
        }
        meta_path = self.base_dir / project_id / "project_meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_project_meta_basic(self, project_id: str, project_name: str,
                                 weknora_kb_id: str = "", subject: str = "",
                                 sor_folder: str = "") -> Dict:
        """保存项目元数据（轻量级，仅 KB ID + 主体对象，无需 SOR 文件扫描）

        适用于：用户通过前端表单创建项目（无 SOR 源文件），但需要保留 WeKnora KB 关联
        以便 ExtractTreeTool 走 WeKnora 双通道管线
        """
        meta_path = self.base_dir / project_id / "project_meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        meta.update({
            "project_id": project_id,
            "project_name": project_name,
            "weknora_kb_id": weknora_kb_id,
            "subject": subject,
            "sor_folder": sor_folder or meta.get("sor_folder", ""),
            "updated_at": datetime.now().isoformat(),
        })
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"[KB] 已保存项目元数据: {project_id} (weknora_kb_id={weknora_kb_id[:8] if weknora_kb_id else '-'})")
        return meta

    def _load_project_meta(self, project_id: str) -> Dict:
        """加载项目元数据"""
        meta_path = self.base_dir / project_id / "project_meta.json"
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"project_id": project_id, "project_name": project_id}


# ═══════════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════════

_kb_service: Optional[KnowledgeService] = None


def get_kb_service() -> KnowledgeService:
    global _kb_service
    if _kb_service is None:
        _kb_service = KnowledgeService()
    return _kb_service
