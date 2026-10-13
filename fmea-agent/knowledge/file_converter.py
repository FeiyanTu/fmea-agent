"""
文档加载与结构化提取模块 v3.0

核心理念变更（vs v2 Markdown 转换）：
1. 不转 Markdown → 保留文本原始结构和表格
2. 不同文档类型用不同的提取策略：
   - PDF图纸 → fitz.get_text()，保留技术要求的行级结构
   - PDF文本 → fitz.get_text("blocks")，保留段落/表格块
   - Excel → 表格直接转 JSON，不转 Markdown
   - DOCX → 段落 + 表格分别提取
3. 统一输出：{"text": str, "tables": [...], "meta": {...}}

解决的核心痛点：
- 结构仿真报告的 Table 2-1 在 Markdown 中变成碎片
- 图纸技术要求列表在 Markdown 中丢失对齐
- xls BOM 编码问题导致读取失败
"""

import logging
import json
import re
import io
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".doc",
    ".xlsx", ".xls", ".pptx", ".ppt",
    ".txt", ".csv", ".html", ".htm",
    ".json", ".xml", ".yaml", ".yml",
}


def is_supported_file(filepath: str) -> bool:
    ext = Path(filepath).suffix.lower()
    return ext in SUPPORTED_EXTENSIONS


def load_document(filepath: str) -> Tuple[Dict, str]:
    """
    加载单个文档，返回结构化内容。

    Args:
        filepath: 文件绝对路径

    Returns:
        (结构化文档字典, 原始文件名)

    字典格式：
    {
        "text": "完整文本内容（保留行级结构）",
        "tables": [
            {"headers": [...], "rows": [[...], ...], "caption": ""}
        ],
        "meta": {
            "source_file": "原始文件名",
            "source_type": ".pdf",
            "page_count": 12,
            "file_size_kb": 150,
        }
    }
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {filepath}")
    if not path.is_file():
        raise ValueError(f"路径不是文件: {filepath}")

    ext = path.suffix.lower()
    original_name = path.name
    size_kb = path.stat().st_size / 1024

    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"不支持的文件格式: {ext}")

    logger.info(f"[Loader] 开始加载: {original_name} ({ext})")

    try:
        if ext == ".pdf":
            result = _load_pdf(path, original_name, size_kb)
        elif ext in (".xlsx", ".xls"):
            result = _load_xlsx(path, original_name, size_kb)
        elif ext in (".docx", ".doc"):
            # .doc 里绝大多数其实是改名后的 OOXML，先按 docx 解析；
            # 真正的老版二进制 .doc（OLE 复合文档）解析失败时退回文本读取。
            try:
                result = _load_docx(path, original_name, size_kb)
            except Exception as e:
                logger.warning(f"[Loader] {ext} 按 docx 解析失败，转文本回退: {e}")
                result = _load_text(path, original_name, size_kb)
        elif ext in (".pptx", ".ppt"):
            try:
                result = _load_pptx(path, original_name, size_kb)
            except Exception as e:
                logger.warning(f"[Loader] {ext} 按 pptx 解析失败，转文本回退: {e}")
                result = _load_text(path, original_name, size_kb)
        else:
            result = _load_text(path, original_name, size_kb)

        # 后处理：检测并标记表格
        _detect_tables_in_text(result)

        logger.info(f"[Loader] 加载完成: {original_name} → "
                     f"{len(result['text'])} 字符, {len(result['tables'])} 表格, "
                     f"{result['meta'].get('page_count', 0)} 页")

        return result, original_name

    except Exception as e:
        logger.error(f"[Loader] 加载失败: {original_name}: {e}")
        return {
            "text": f"[加载失败: {str(e)}]",
            "tables": [],
            "meta": {"source_file": original_name, "source_type": ext, "error": str(e)},
        }, original_name


def scan_directory(dir_path: str) -> List[Dict]:
    """
    扫描目录，仅记录文件元信息，不加载内容。

    用于项目初始化的第一步——记录有哪些文件、类型、大小，
    后续按需调用 load_document() 加载内容。

    Returns:
        [{"abs_path": str, "rel_path": str, "filename": str, "extension": str, "size_kb": float}]
    """
    dir_path = Path(dir_path)
    results = []

    all_files = []
    for f in dir_path.rglob("*"):
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
            all_files.append(f)

    logger.info(f"[Loader] 扫描目录: {dir_path} → {len(all_files)} 个文件（仅元信息）")

    for f in sorted(all_files):
        try:
            rel_path = str(f.relative_to(dir_path))
            results.append({
                "filename": f.name,
                "rel_path": rel_path,
                "extension": f.suffix.lower(),
                "size_kb": round(f.stat().st_size / 1024, 1),
                "abs_path": str(f.absolute()),
            })
        except Exception as e:
            logger.warning(f"[Loader] 扫描跳过 {f.name}: {e}")

    return results


def load_directory(dir_path: str) -> Dict[str, Dict]:
    """
    加载整个目录的所有文档。
    """
    dir_path = Path(dir_path)
    results = {}

    all_files = []
    for f in dir_path.rglob("*"):
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
            all_files.append(f)

    logger.info(f"[Loader] 批量加载: {dir_path} → {len(all_files)} 个文件")

    for f in sorted(all_files):
        try:
            doc, original_name = load_document(str(f))
            rel_path = str(f.relative_to(dir_path))
            results[rel_path] = doc
        except Exception as e:
            logger.warning(f"[Loader] 跳过 {f.name}: {e}")

    return results


# ═══════════════════════════════════════════════════════════════
# PDF 加载 — 使用 fitz 保留文本结构
# ═══════════════════════════════════════════════════════════════

def _load_pdf(path: Path, original_name: str, size_kb: float) -> Dict:
    """加载 PDF，优先 fitz (PyMuPDF)，回退 pypdf"""
    # 尝试 fitz（更好的文本结构保留）
    try:
        import fitz
        return _load_pdf_with_fitz(path, original_name, size_kb)
    except ImportError:
        pass

    # 回退 pypdf
    try:
        import pypdf
        return _load_pdf_with_pypdf(path, original_name, size_kb)
    except ImportError:
        pass

    # 尝试 pdfplumber
    try:
        import pdfplumber
        return _load_pdf_with_pdfplumber(path, original_name, size_kb)
    except ImportError:
        raise RuntimeError(
            "无法加载 PDF，需要安装以下任一库: pip install pymupdf 或 pip install pypdf"
        )


def _load_pdf_with_fitz(path: Path, original_name: str, size_kb: float) -> Dict:
    """使用 fitz (PyMuPDF) 加载 PDF"""
    import fitz

    doc = fitz.open(str(path))
    page_count = doc.page_count
    pages_text = []
    all_tables = []

    for i in range(min(page_count, 200)):
        page = doc[i]

        # 方法1: get_text("blocks") 保留段落/块结构
        blocks = page.get_text("blocks")
        block_lines = []

        for b in blocks:
            # b = (x0, y0, x1, y1, text, block_no, block_type)
            text = b[4].strip() if len(b) > 4 else ""
            if not text:
                continue
            block_type = b[6] if len(b) > 6 else 0
            if block_type == 0:  # 文本块
                block_lines.append(text)

        page_text = "\n".join(block_lines)

        if page_text.strip():
            pages_text.append(f"[页 {i+1}]\n{page_text}")

        # 方法2: 直接 get_text() 获取纯文本（用于提取技术要求列表）
        raw_text = page.get_text()
        if "技术要求" in raw_text or "Technical Requirement" in raw_text:
            # 这是图纸页，保留原始行结构
            pages_text[-1] = f"[页 {i+1} — 含技术要求]\n{raw_text}"

        # 检测表格数据（连续的数值/对行列）
        lines = raw_text.strip().split("\n")
        potential_table = _extract_fitz_table(lines)
        if potential_table:
            all_tables.append({
                "page": i + 1,
                "caption": f"Table from page {i+1}",
                "headers": potential_table[0],
                "rows": potential_table[1:],
            })

    doc.close()

    text = "\n\n".join(pages_text)

    return {
        "text": text,
        "tables": all_tables,
        "meta": {
            "source_file": original_name,
            "source_type": ".pdf",
            "page_count": page_count,
            "file_size_kb": round(size_kb, 1),
        },
    }


def _load_pdf_with_pypdf(path: Path, original_name: str, size_kb: float) -> Dict:
    """使用 pypdf 加载 PDF（回退方案）"""
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    page_count = len(reader.pages)
    pages = []
    for page in reader.pages[:200]:
        text = page.extract_text()
        if text:
            pages.append(text)
    text = "\n\n".join(pages) if pages else "(PDF 无文字内容)"

    return {
        "text": text,
        "tables": [],
        "meta": {
            "source_file": original_name,
            "source_type": ".pdf",
            "page_count": page_count,
            "file_size_kb": round(size_kb, 1),
        },
    }


def _load_pdf_with_pdfplumber(path: Path, original_name: str, size_kb: float) -> Dict:
    """使用 pdfplumber 加载 PDF"""
    import pdfplumber
    with pdfplumber.open(str(path)) as pdf:
        pages = []
        page_count = len(pdf.pages)
        for page in pdf.pages[:200]:
            text = page.extract_text()
            if text:
                pages.append(text)
    text = "\n\n".join(pages) if pages else "(PDF 无文字内容)"

    return {
        "text": text,
        "tables": [],
        "meta": {
            "source_file": original_name,
            "source_type": ".pdf",
            "page_count": page_count,
            "file_size_kb": round(size_kb, 1),
        },
    }


def _extract_fitz_table(lines: List[str]) -> Optional[List[List[str]]]:
    """
    从 fitz 提取的文本行中检测表格。
    判断特征：连续多行含类似数量的空格分隔内容、含数字。
    """
    if len(lines) < 3:
        return None

    # 找连续的数据行（含数字、按空格分出多个列）
    table_lines = []
    table_started = False
    column_count = 0

    for line in lines:
        parts = [p.strip() for p in re.split(r'\s{2,}', line) if p.strip()]
        if len(parts) >= 3:
            # 检查是否含数字（表格特征）
            has_numbers = any(
                re.search(r'\d', p) for p in parts
            )
            if has_numbers or len(parts) >= 4:
                if not table_started:
                    table_started = True
                    column_count = len(parts)
                elif len(parts) == column_count:
                    table_lines.append(parts)
                    continue
                elif abs(len(parts) - column_count) <= 2:
                    # 列数略有差异但仍在合理范围
                    while len(parts) < column_count:
                        parts.append("")
                    table_lines.append(parts)
                    continue
            else:
                if table_started and table_lines:
                    break
        elif table_started and not line.strip():
            # 空行结束表格
            if len(table_lines) >= 2:
                break
            table_lines = []
            table_started = False

    if len(table_lines) >= 2:
        return table_lines

    return None


# ═══════════════════════════════════════════════════════════════
# Excel 加载 — 直接解析表格 + 编码兼容
# ═══════════════════════════════════════════════════════════════

def _load_xlsx(path: Path, original_name: str, size_kb: float) -> Dict:
    """加载 Excel，处理编码问题，输出结构化表格"""
    all_text = []
    all_tables = []
    sheet_count = 0

    # 策略1: openpyxl (xlsx)
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), data_only=True)

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sheet_count += 1

            rows_data = []
            max_col = 0
            for row in ws.iter_rows(max_row=min(ws.max_row, 500), values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                max_col = max(max_col, len(cells))
                rows_data.append(cells)

            if not rows_data:
                continue

            # 填充不等长行
            for r in rows_data:
                while len(r) < max_col:
                    r.append("")

            table_caption = f"{original_name} / {sheet_name}"
            all_tables.append({
                "caption": table_caption,
                "headers": rows_data[0] if rows_data else [],
                "rows": rows_data[1:] if len(rows_data) > 1 else [],
                "source_sheet": sheet_name,
            })

            # 同时保留文本视图
            all_text.append(f"## Sheet: {sheet_name}")
            for r in rows_data[:50]:  # 前50行文本
                all_text.append(" | ".join(r))

        wb.close()

    except Exception as e1:
        logger.warning(f"[Loader] openpyxl 失败 {original_name}: {e1}")

        # 策略2: xlrd (xls)
        try:
            import xlrd
            wb = xlrd.open_workbook(str(path), encoding_override="gbk")

            for i in range(wb.nsheets):
                ws = wb.sheet_by_index(i)
                sheet_count += 1
                rows_data = []
                for r in range(min(ws.nrows, 500)):
                    cells = [
                        str(ws.cell_value(r, c)) if ws.cell_type(r, c) != 0 else ""
                        for c in range(ws.ncols)
                    ]
                    rows_data.append(cells)

                if rows_data:
                    all_tables.append({
                        "caption": f"{original_name} / {ws.name}",
                        "headers": rows_data[0],
                        "rows": rows_data[1:],
                    })

        except Exception as e2:
            logger.warning(f"[Loader] xlrd 也失败 {original_name}: {e2}")

            # 策略3: csv 回退 (用 csv 模块读原始字节)
            try:
                _text = _read_with_fallback_encodings(path)
                rows = [r.split(",") for r in _text.split("\n") if r.strip()]
                if rows:
                    all_tables.append({
                        "caption": f"{original_name} (CSV回退)",
                        "headers": rows[0] if rows else [],
                        "rows": rows[1:],
                    })
                    all_text.append(_text)
            except Exception:
                all_text.append(f"[无法读取 Excel: {original_name}]")

    text = "\n\n".join(all_text) if all_text else (
        f"[{original_name}: {len(all_tables)} 个工作表, 表格结构化数据见 tables 字段]"
    )

    return {
        "text": text,
        "tables": all_tables,
        "meta": {
            "source_file": original_name,
            "source_type": path.suffix.lower(),
            "sheet_count": sheet_count,
            "file_size_kb": round(size_kb, 1),
            "has_structured_data": len(all_tables) > 0,
        },
    }


def _read_with_fallback_encodings(path: Path) -> str:
    """多编码回退读取文本文件"""
    for enc in ["utf-8", "gbk", "gb2312", "gb18030", "latin-1"]:
        try:
            return path.read_text(encoding=enc, errors="strict")
        except (UnicodeDecodeError, UnicodeError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


# ═══════════════════════════════════════════════════════════════
# DOCX / PPTX / 文本 加载
# ═══════════════════════════════════════════════════════════════

def _load_docx(path: Path, original_name: str, size_kb: float) -> Dict:
    """加载 DOCX，段落 + 表格分别提取"""
    from docx import Document

    doc = Document(str(path))
    paragraphs = []
    tables = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            paragraphs.append(text)

    for i, table in enumerate(doc.tables):
        rows = []
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            rows.append(cells)
        if rows:
            tables.append({
                "caption": f"表格 {i+1}",
                "headers": rows[0],
                "rows": rows[1:],
            })

    return {
        "text": "\n\n".join(paragraphs),
        "tables": tables,
        "meta": {
            "source_file": original_name,
            "source_type": ".docx",
            "file_size_kb": round(size_kb, 1),
        },
    }


def _load_pptx(path: Path, original_name: str, size_kb: float) -> Dict:
    """加载 PPTX，提取文本和表格"""
    from pptx import Presentation

    prs = Presentation(str(path))
    slide_texts = []
    tables = []

    for i, slide in enumerate(prs.slides):
        slide_parts = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                slide_parts.append(shape.text.strip())
            if shape.has_table:
                table = shape.table
                rows = []
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    rows.append(cells)
                if rows:
                    tables.append({
                        "caption": f"幻灯片 {i+1} 表格",
                        "headers": rows[0],
                        "rows": rows[1:],
                    })
        if slide_parts:
            slide_texts.append(f"## 幻灯片 {i+1}\n" + "\n".join(slide_parts))

    return {
        "text": "\n\n---\n\n".join(slide_texts),
        "tables": tables,
        "meta": {
            "source_file": original_name,
            "source_type": ".pptx",
            "slide_count": len(prs.slides),
            "file_size_kb": round(size_kb, 1),
        },
    }


def _load_text(path: Path, original_name: str, size_kb: float) -> Dict:
    """加载纯文本/代码文件"""
    text = _read_with_fallback_encodings(path)
    return {
        "text": text,
        "tables": [],
        "meta": {
            "source_file": original_name,
            "source_type": path.suffix.lower(),
            "file_size_kb": round(size_kb, 1),
        },
    }


# ═══════════════════════════════════════════════════════════════
# 后处理：文本内表格检测
# ═══════════════════════════════════════════════════════════════

def _detect_tables_in_text(doc: Dict) -> None:
    """从文本中检测未被显式解析的表格，补充到 tables 列表"""
    text = doc.get("text", "")
    if not text:
        return

    # 如果已经有 tables，跳过文本级检测（来源文件自带表格结构）
    if doc.get("tables"):
        return

    # 检测连续的表格行（用 | 分隔的 Markdown 表格已在 fitz 阶段处理）
    # 检测用多个空格/TAB 分隔的列式数据
    lines = text.split("\n")
    potential = _extract_fitz_table(lines)

    if potential and len(potential) >= 3:
        doc["tables"].append({
            "caption": "从文本检测的表格",
            "headers": potential[0],
            "rows": potential[1:],
        })


# ═══════════════════════════════════════════════════════════════
# 兼容旧接口（供 kb_service 等调用）
# ═══════════════════════════════════════════════════════════════

def convert_file_to_markdown(filepath: str) -> Tuple[str, str]:
    """
    兼容旧接口：将文件转为 Markdown 文本。
    用于 kb_service 和旧版 indexer。
    如果已有结构化表格，嵌入为 Markdown 表格。
    """
    doc, original_name = load_document(filepath)

    parts = [doc["text"]]

    def _esc(v) -> str:
        # 单元格内的换行与竖线会切碎 Markdown 表格，必须转义
        return str(v).replace("\n", " ").replace("|", "\\|").strip()

    for t in doc.get("tables", []):
        caption = t.get("caption", "")
        headers = t.get("headers", [])
        rows = t.get("rows", [])

        if not headers:
            continue

        md_lines = [f"\n### {caption}\n"]
        md_lines.append("| " + " | ".join(_esc(h) for h in headers) + " |")
        md_lines.append("| " + " | ".join("---" for _ in headers) + " |")
        for row in rows:
            md_lines.append("| " + " | ".join(_esc(c) for c in row) + " |")
        parts.append("\n".join(md_lines))

    return "\n\n".join(parts), original_name


def convert_directory_to_markdown(dir_path: str, output_dir: str, progress_callback=None) -> Dict:
    """兼容旧接口"""
    results = load_directory(dir_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    converted = 0
    for rel_path, doc in results.items():
        md_text = doc["text"]
        if doc.get("tables"):
            md_text += f"\n\n---\n### 表格 ({len(doc['tables'])} 个)"
        output_name = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', rel_path) + ".txt"
        (output_dir / output_name).write_text(md_text, encoding="utf-8")
        converted += 1
        if progress_callback:
            progress_callback(converted, len(results), rel_path, "ok")

    return {"total": len(results), "converted": converted, "failed": 0, "files": []}


# ═══════════════════════════════════════════════════════════════
# 工具函数：提取文档中的关键信息块
# ═══════════════════════════════════════════════════════════════

def extract_tech_requirements(doc: Dict) -> List[str]:
    """从文档文本中提取技术要求列表"""
    text = doc.get("text", "")
    requirements = []

    # 图纸中的技术要求通常以编号开头
    in_tech_section = False
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            if in_tech_section:
                break  # 空行结束
            continue

        # 检测技术要求段头
        if "技术要求" in line or "Technical" in line:
            in_tech_section = True
            continue

        if in_tech_section:
            # 检测编号行: "1. xxx" 或 "★1." 或 "1)"
            if re.match(r'^[★\*]?\d+[\.\)、]', line):
                requirements.append(line)
            elif re.match(r'^[a-zA-Z]\.\)', line):
                requirements.append(line)
            else:
                # 可能是上一行的续行
                pass

    return requirements


def extract_numbered_items(text: str) -> List[str]:
    """从文本中提取带编号的列表项"""
    items = []
    pattern = re.compile(r'^\s*[\★\*]?\s*(\d+)[\.\)、]\s*(.+)')
    for line in text.split("\n"):
        m = pattern.match(line.strip())
        if m:
            items.append(m.group(2).strip())
    return items
