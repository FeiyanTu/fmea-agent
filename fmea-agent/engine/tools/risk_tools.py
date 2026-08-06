"""
风险评估专用工具 — 注册为 Agent tools（v12 重构版）

核心设计（混合架构）：
┌──────────────────────────────────────────────────┐
│ ① LLM 提取层（一次调用，不限格式）                   │
│   用户上传任意格式（管道表/HTML/图片/PDF）            │
│   → LLM 提取 → 存为结构化评分标准（DB: fmea_risk_criteria）│
├──────────────────────────────────────────────────┤
│ ② 确定性计算层（零 LLM，每次结果一致）                │
│   读取评分标准 → 纯 Python 匹配打分                 │
│   RPN = S×O×D，AP = 查表 → 保存 04-risk + DB       │
└──────────────────────────────────────────────────┘

v12 重构要点（对照 AI_DFMEA_需求规格说明书 4.5 阶段5）：
1. 【DB 优先】失效数据从 DB 读（get_baseline(db_pid, "failure")），不再读本地 03-failure.json
2. 【DB 优先】评分文件从 project_risk_files 表读；评分标准存/读 fmea_risk_criteria 表
3. 【法规高亮】从 function.domain_rows 按 function_id JOIN regulation_binding，
   risk item 标记 regulation_binding，满足需求 4.5.2-4「违反强制法规条款失效项高亮」
4. 【打分策略明确】S 优先标准匹配，未命中才用 LLM severity（记录 source）；
   O/D 标准匹配 + 措施修正因子；未命中标记 uncertain 而非随意默认分
5. 【AP 严格化】AP 查表，未命中返回 uncertain（ap=""），禁止 fallback 到 RPN 阈值
   （需求 2.3 禁止简化评分规则）
6. 【控制措施】PC/DC 由 LLM 批量生成（贴合"现有设计预防/探测措施"语义），
   失败 fallback 模板；LLM 结果缓存到本地 risk_controls_cache.json

文件命名约定（不区分中英文，自动匹配）：
- SOD 标准文件: 文件名包含 "S" / "O" / "D" 或 "sod" 或 "打分规则"
- AP 表文件:    文件名包含 "AP" 或 "ap" 或 "优先级" 或 "措施优先级"
"""
import json
import logging
import re
import os
import time
import hashlib
from pathlib import Path
from typing import List, Optional, Dict, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 数据结构（纯 Python，零依赖）
# ═══════════════════════════════════════════════════════════════

class SodCriterion:
    """单条 SOD 评分标准"""
    score: int
    description: str
    keywords: list[str]
    example: str = ""

    def __init__(self, score: int, description: str, keywords: Optional[list[str]] = None, example: str = ""):
        self.score = score
        self.description = description
        self.keywords = keywords or []
        self.example = example

    def match(self, text: str) -> bool:
        """判断给定文本是否匹配本条标准（支持中文模糊匹配）"""
        text_lower = text.lower()
        for kw in self.keywords:
            if len(kw) >= 2 and kw.lower() in text_lower:
                return True

        # 描述模糊匹配
        desc_words = [w for w in re.split(r'[,;/，；、\s]+', self.description) if len(w) >= 2]
        if not desc_words:
            return False
        match_count = sum(1 for w in desc_words if w.lower() in text_lower)
        if match_count >= 2:
            return True
        if match_count >= 1 and any(len(w) >= 4 for w in desc_words):
            return True

        # 中文 CJK 逐字匹配兜底
        if len(text) > 100:
            return False

        def _cjk_ratio(s: str) -> float:
            cjk = sum(1 for c in s if '\u4e00' <= c <= '\u9fff')
            return cjk / len(s) if s else 0

        if _cjk_ratio(self.description) > 0.4 and _cjk_ratio(text) > 0.4:
            stop = '的了一是在不了有和人这中大为上个会主'
            desc_chars = set(c for c in self.description if '\u4e00' <= c <= '\u9fff' and c not in stop)
            text_chars = set(c for c in text if '\u4e00' <= c <= '\u9fff')
            if desc_chars and text_chars:
                overlap = len(desc_chars & text_chars)
                return (overlap / len(desc_chars)) >= 0.4

        return False

    def to_dict(self) -> dict:
        return {"score": self.score, "description": self.description,
                "keywords": self.keywords, "example": self.example}

    @classmethod
    def from_dict(cls, d: dict) -> "SodCriterion":
        return cls(score=d["score"], description=d.get("description", ""),
                   keywords=d.get("keywords", []), example=d.get("example", ""))


class SodCriteria:
    """完整的 SOD 评分标准集"""
    severity_rules: list[SodCriterion]
    occurrence_rules: list[SodCriterion]
    detection_rules: list[SodCriterion]

    def __init__(self):
        self.severity_rules = []
        self.occurrence_rules = []
        self.detection_rules = []

    # ── v12: 匹配方法统一返回 (score, matched) ──
    # matched=True 表示命中标准规则；False 表示未命中（需人工判定或 LLM 兜底）
    def _match_score(self, rules: list, text: str, default: int) -> Tuple[int, bool]:
        for rule in sorted(rules, key=lambda r: -r.score):
            if rule.match(text):
                return rule.score, True
        return default, False

    def calculate_severity(self, failure_effect: str, severity_reason: str = "") -> Tuple[int, bool]:
        text = f"{failure_effect} {severity_reason}"
        return self._match_score(self.severity_rules, text, default=4)

    def calculate_occurrence(self, failure_cause: str, prevention_control: str = "") -> Tuple[int, bool]:
        text = f"{failure_cause} {prevention_control}"
        has_pc = bool(prevention_control and prevention_control.strip())
        score, matched = self._match_score(self.occurrence_rules, text, default=5)
        if matched and has_pc:
            score = max(1, score - 2)  # 已有预防控制 → 降低发生度（AIAG-VDA 语义）
        return score, matched

    def calculate_detection(self, failure_mode: str, detection_control: str = "") -> Tuple[int, bool]:
        text = f"{failure_mode} {detection_control}"
        has_dc = bool(detection_control and detection_control.strip())
        score, matched = self._match_score(self.detection_rules, text, default=5)
        if matched and has_dc:
            score = max(1, score - 3)  # 已有探测控制 → 降低探测度（AIAG-VDA 语义）
        return score, matched

    def to_dict(self) -> dict:
        return {
            "severity": [r.to_dict() for r in self.severity_rules],
            "occurrence": [r.to_dict() for r in self.occurrence_rules],
            "detection": [r.to_dict() for r in self.detection_rules],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SodCriteria":
        obj = cls()
        obj.severity_rules = [SodCriterion.from_dict(r) for r in d.get("severity", [])]
        obj.occurrence_rules = [SodCriterion.from_dict(r) for r in d.get("occurrence", [])]
        obj.detection_rules = [SodCriterion.from_dict(r) for r in d.get("detection", [])]
        return obj


class ApRule:
    """单条 AP 判定规则"""
    s_min: int; s_max: int; o_min: int; o_max: int; d_min: int; d_max: int; ap: str

    def __init__(self, s_min: int, s_max: int, o_min: int, o_max: int,
                 d_min: int, d_max: int, ap: str):
        self.s_min = s_min; self.s_max = s_max
        self.o_min = o_min; self.o_max = o_max
        self.d_min = d_min; self.d_max = d_max
        self.ap = ap

    def to_dict(self) -> dict:
        return {"s_min": self.s_min, "s_max": self.s_max,
                "o_min": self.o_min, "o_max": self.o_max,
                "d_min": self.d_min, "d_max": self.d_max, "ap": self.ap}

    @classmethod
    def from_dict(cls, d: dict) -> "ApRule":
        return cls(d["s_min"], d["s_max"], d["o_min"], d["o_max"],
                   d["d_min"], d["d_max"], d["ap"])


class ApTable:
    """AP 表 - 从 LLM 提取的判定规则集"""
    rules: list[ApRule]
    source_file: str = ""

    def __init__(self, rules: Optional[list[ApRule]] = None):
        self.rules = rules or []

    def calculate_ap(self, s: int, o: int, d: int) -> Tuple[Optional[str], bool]:
        """返回 (ap, certain)。
        - 规则命中 → (ap, True)
        - 未命中 → (None, False) —— v12: 禁止 fallback 到 RPN 阈值（需求 2.3 禁止简化评分规则），
          标记 uncertain，由前端提示人工判定
        """
        for rule in self.rules:
            if rule.s_min <= s <= rule.s_max and \
               rule.o_min <= o <= rule.o_max and \
               rule.d_min <= d <= rule.d_max:
                return rule.ap, True
        return None, False

    def to_dict(self) -> dict:
        return {"rules": [r.to_dict() for r in self.rules]}

    @classmethod
    def from_dict(cls, d: dict) -> "ApTable":
        return cls(rules=[ApRule.from_dict(r) for r in d.get("rules", [])])


# ═══════════════════════════════════════════════════════════════
# LLM 提取层 — 把任意格式的用户文件转为结构化 JSON
# ═══════════════════════════════════════════════════════════════

_EXTRACTION_SYSTEM_PROMPT = """你是 DFMEA 评分标准提取专家。你的任务是从用户上传的文档/表格中提取结构化的 FMEA 评分标准。

输出要求（纯 JSON，不要多余的文字和 markdown 代码块标记）：

{
  "severity_rules": [
    {"score": 10, "description": "失效影响描述", "keywords": ["安全","法规","人身伤害"]},
    ...
  ],
  "occurrence_rules": [
    {"score": 10, "description": "发生频度描述", "keywords": ["每件","频繁","连续"]},
    ...
  ],
  "detection_rules": [
    {"score": 10, "description": "探测能力描述", "keywords": ["无法探测","不能检测"]},
    ...
  ]
}

提取规则：
1. score: 1-10 的整数，必须与原始文件中列出的分值完全一致
2. description: 从文件中找到该分值对应的核心标准描述（1-2句话），尽可能保留原文关键词
3. keywords: 从 description 和原始文件中提取 3-8 个最典型的关键词/短语，用于后续匹配
   - 选那些最具体、最能区分该分值等级的短语
   - 中英文均可
4. 如果原文件是 HTML 表格或复杂格式，先理解其结构再提取
5. 如果某个分值有多个描述，合并为一条
6. S/O/D 分别提取各自的规则列表"""

_AP_EXTRACTION_PROMPT = """你是 DFMEA 措施优先级(AP)判定表提取专家。你的任务是从用户上传的文档中提取结构化的 AP 判定规则。

输出要求（纯 JSON，不要多余的文字）：

{
  "rules": [
    {"s_min": 9, "s_max": 10, "o_min": 2, "o_max": 10, "d_min": 1, "d_max": 10, "ap": "H"},
    ...
  ]
}

提取规则：
1. s_min/s_max: 严重度 S 的范围（1-10）
2. o_min/o_max: 发生度 O 的范围（1-10）
3. d_min/d_max: 探测度 D 的范围（1-10）
4. ap: "H"(高优先) / "M"(中优先) / "L"(低优先)
5. 如果文件是矩阵格式（S行×O行×D列），请展开为扁平的规则列表
6. 确保覆盖所有 S×O×D 组合
7. 注意 S=1 或某些组合可能没有明确 AP 值，跳过即可"""

# v12: 预防/探测控制措施生成 prompt（需求 4.5.2-2：区分预防控制 PC / 探测控制 DC，贴合"现有设计"语义）
_CONTROLS_SYSTEM_PROMPT = """你是 DFMEA 风险分析专家。针对每条失效链，基于失效模式(FM)、失效原因(FC)和失效影响(FE)，
推断该产品在"当前设计阶段"应采取的：

1. 现有设计预防控制措施（prevention_control, PC）：设计上已采取/应采取的降低失效发生可能性的手段
   （如：冗余设计、降额设计、材料选型验证、仿真分析）
2. 现有设计探测控制措施（detection_control, DC）：设计上已有的验证/测试手段，在失效前或失效后能探测到
   （如：DV 测试、仿真验证、功能安全测试、台架试验）

要求：
- 必须是"设计层面"的预防/探测措施，禁止产线检验、装配工艺、来料管控等非设计措施
- 语言简洁，每项 1-2 句话，标注设计阶段（如：选型阶段/仿真阶段/DV验证阶段）
- 输出纯 JSON，不要多余文字"""

_CONTROLS_OUTPUT_SCHEMA_HINT = """
输出格式：
{
  "items": [
    {"id": "<失效链id>", "prevention_control": "...", "detection_control": "..."},
    ...
  ]
}
必须为输入中的每条失效链都输出对应条目，id 与输入一致。"""


async def _extract_sod_with_llm(file_contents: list[dict]) -> dict:
    """调用 LLM 从用户上传的 SOD 标准文件中提取结构化规则

    Args:
        file_contents: [{"filename": "S值打分规则.md", "content": "...全文..."}, ...]

    Returns:
        {"severity_rules": [...], "occurrence_rules": [...], "detection_rules": [...]}
    """
    from engine.llm_client import get_llm_client

    llm = get_llm_client()

    # 构建用户消息：把所有文件内容拼在一起
    user_parts = ["以下是从用户上传的 SOD 评分标准文件中读取的内容，请从中提取结构化的评分规则："]
    for fc in file_contents:
        content_preview = fc["content"]
        if len(content_preview) > 15000:
            content_preview = content_preview[:15000] + "\n...(截断)..."
        user_parts.append(f"\n=== 文件: {fc['filename']} ===\n{content_preview}")

    user_parts.append(
        "\n\n请严格按照你看到的表格数据提取每个分值(1-10)对应的描述，"
        "不要编造不存在的内容。输出格式见系统提示。"
    )

    user_message = "\n".join(user_parts)

    # 尝试 tool calling → 回退到 complete
    tools = [{
        "type": "function",
        "function": {
            "name": "extract_sod_rules",
            "description": "从用户上传文件中提取SOD评分规则",
            "parameters": {
                "type": "object",
                "properties": {
                    "severity_rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "score": {"type": "integer", "description": "评分 1-10"},
                                "description": {"type": "string", "description": "标准描述"},
                                "keywords": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["score", "description", "keywords"],
                        }
                    },
                    "occurrence_rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "score": {"type": "integer"},
                                "description": {"type": "string"},
                                "keywords": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["score", "description", "keywords"],
                        }
                    },
                    "detection_rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "score": {"type": "integer"},
                                "description": {"type": "string"},
                                "keywords": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["score", "description", "keywords"],
                        }
                    },
                },
                "required": ["severity_rules", "occurrence_rules", "detection_rules"],
            },
        }
    }]

    for attempt in range(2):
        try:
            result = await llm.call_tool(
                system_prompt=_EXTRACTION_SYSTEM_PROMPT,
                user_message=user_message,
                tools=tools,
                tool_choice="auto",
            )
            if result:
                data = json.loads(result) if isinstance(result, str) else result
                if data.get("severity_rules") and len(data["severity_rules"]) > 0:
                    logger.info(f"[LLM-SOD] 成功提取: {len(data['severity_rules'])}条S, "
                                f"{len(data.get('occurrence_rules', []))}条O, "
                                f"{len(data.get('detection_rules', []))}条D")
                    return data
        except Exception as e:
            logger.warning(f"[LLM-SOD] Tool calling 失败 (attempt {attempt+1}): {e}")

    # 回退到 complete 模式
    logger.info("[LLM-SOD] 回退到 complete 模式")
    try:
        result = await llm.call_complete(
            system_prompt=_EXTRACTION_SYSTEM_PROMPT,
            user_message=user_message,
            force_json=True,
        )
        if result:
            data = json.loads(result) if isinstance(result, str) else result
            if data.get("severity_rules"):
                logger.info(f"[LLM-SOD] Complete模式成功提取")
                return data
    except Exception as e:
        logger.warning(f"[LLM-SOD] Complete 也失败: {e}")

    # 全部失败
    logger.error("[LLM-SOD] LLM 提取全部失败")
    return {}


async def _extract_ap_with_llm(file_contents: list[dict]) -> dict:
    """调用 LLM 从用户上传的 AP 表文件中提取结构化规则

    Args:
        file_contents: [{"filename": "AP表.md", "content": "...全文..."}, ...]

    Returns:
        {"rules": [{"s_min":.., "s_max":.., "o_min":.., "o_max":.., "d_min":.., "d_max":.., "ap":"H"}, ...]}
    """
    from engine.llm_client import get_llm_client

    llm = get_llm_client()

    user_parts = ["以下是从用户上传的 AP 判定表文件中读取的内容，请提取结构化的判定规则："]
    for fc in file_contents:
        content_preview = fc["content"]
        if len(content_preview) > 15000:
            content_preview = content_preview[:15000] + "\n...(截断)..."
        user_parts.append(f"\n=== 文件: {fc['filename']} ===\n{content_preview}")

    user_parts.append(
        "\n\n如果原始文件是二维矩阵格式（S行×O行×D列），请展开为扁平规则列表。"
        "每个 (S范围, O范围, D范围) 组合对应一条规则。"
    )
    user_message = "\n".join(user_parts)

    tools = [{
        "type": "function",
        "function": {
            "name": "extract_ap_rules",
            "description": "从用户上传文件中提取AP判定规则",
            "parameters": {
                "type": "object",
                "properties": {
                    "rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "s_min": {"type": "integer"}, "s_max": {"type": "integer"},
                                "o_min": {"type": "integer"}, "o_max": {"type": "integer"},
                                "d_min": {"type": "integer"}, "d_max": {"type": "integer"},
                                "ap": {"type": "string", "enum": ["H", "M", "L"]},
                            },
                            "required": ["s_min", "s_max", "o_min", "o_max", "d_min", "d_max", "ap"],
                        }
                    },
                },
                "required": ["rules"],
            },
        }
    }]

    for attempt in range(2):
        try:
            result = await llm.call_tool(
                system_prompt=_AP_EXTRACTION_PROMPT,
                user_message=user_message,
                tools=tools,
                tool_choice="auto",
            )
            if result:
                data = json.loads(result) if isinstance(result, str) else result
                if data.get("rules") and len(data["rules"]) > 0:
                    logger.info(f"[LLM-AP] 成功提取: {len(data['rules'])} 条规则")
                    return data
        except Exception as e:
            logger.warning(f"[LLM-AP] Tool calling 失败 (attempt {attempt+1}): {e}")

    logger.info("[LLM-AP] 回退到 complete 模式")
    try:
        result = await llm.call_complete(
            system_prompt=_AP_EXTRACTION_PROMPT,
            user_message=user_message,
            force_json=True,
        )
        if result:
            data = json.loads(result) if isinstance(result, str) else result
            if data.get("rules"):
                logger.info(f"[LLM-AP] Complete模式成功提取")
                return data
    except Exception as e:
        logger.warning(f"[LLM-AP] Complete 也失败: {e}")

    logger.error("[LLM-AP] LLM 提取全部失败")
    return {}


async def _generate_controls_with_llm(failure_items: list[dict]) -> Dict[str, dict]:
    """v12: LLM 批量生成预防/探测控制措施

    v22 修复: 原单次最多 200 条 → LLM 输出 JSON 过长被截断 (Unterminated string)。
    v23: 改为 BATCH=40 分片 + asyncio 并发（CONCURRENCY=4 并发批），
    既避免单批输出超长截断，又不会 11 批串行太慢。

    Args:
        failure_items: 失效链 items（需含 id / failure_mode / failure_cause / failure_effect）

    Returns:
        {failure_id: {"prevention_control": "...", "detection_control": "..."}}
        失败返回 {}
    """
    if not failure_items:
        return {}
    from engine.llm_client import get_llm_client
    llm = get_llm_client()

    # 只给 LLM 必要字段，控制 token
    compact_all = []
    for fm in failure_items:
        compact_all.append({
            "id": fm.get("id", ""),
            "failure_mode": (fm.get("failure_mode") or "")[:200],
            "failure_cause": (fm.get("failure_cause") or "")[:200],
            "failure_effect": (fm.get("failure_effect") or "")[:150],
        })

    BATCH = 40          # v22: 每批 40 条, 输出 JSON 约 4k token 内, 防截断
    CONCURRENCY = 4     # v23: 并发批数（419 条 → 11 批 → 3 轮并发跑完）

    async def _gen_batch(batch: list[dict]) -> Dict[str, dict]:
        if not batch:
            return {}
        user_message = (
            "以下为待评估的失效链列表，请为每条生成现有设计预防控制措施(PC)和探测控制措施(DC)：\n"
            + json.dumps(batch, ensure_ascii=False)
            + "\n" + _CONTROLS_OUTPUT_SCHEMA_HINT
        )

        tools = [{
            "type": "function",
            "function": {
                "name": "generate_risk_controls",
                "description": "为每条失效链生成预防/探测控制措施",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "prevention_control": {"type": "string"},
                                    "detection_control": {"type": "string"},
                                },
                                "required": ["id", "prevention_control", "detection_control"],
                            }
                        }
                    },
                    "required": ["items"],
                },
            }
        }]

        out: Dict[str, dict] = {}
        for attempt in range(2):
            try:
                result = await llm.call_tool(
                    system_prompt=_CONTROLS_SYSTEM_PROMPT,
                    user_message=user_message,
                    tools=tools,
                    tool_choice="auto",
                )
                if result:
                    data = json.loads(result) if isinstance(result, str) else result
                    items = data.get("items", [])
                    if items:
                        for it in items:
                            fid = it.get("id", "")
                            if fid:
                                out[fid] = {
                                    "prevention_control": (it.get("prevention_control") or "").strip(),
                                    "detection_control": (it.get("detection_control") or "").strip(),
                                }
                        logger.info(f"[LLM-Controls] 批(40条)成功生成 {len(out)}/{len(batch)} 条")
                        return out
            except Exception as e:
                logger.warning(f"[LLM-Controls] Tool calling 失败 (attempt {attempt+1}): {e}")

        # tool 模式全失败 → Complete 兜底一次
        try:
            result = await llm.call_complete(
                system_prompt=_CONTROLS_SYSTEM_PROMPT,
                user_message=user_message,
                force_json=True,
            )
            if result:
                data = json.loads(result) if isinstance(result, str) else result
                items = data.get("items", [])
                for it in items:
                    fid = it.get("id", "")
                    if fid:
                        out[fid] = {
                            "prevention_control": (it.get("prevention_control") or "").strip(),
                            "detection_control": (it.get("detection_control") or "").strip(),
                        }
                if out:
                    logger.info(f"[LLM-Controls] Complete模式成功生成 {len(out)} 条")
        except Exception as e:
            logger.warning(f"[LLM-Controls] Complete 也失败: {e}")
        return out

    # 分片 → 并发执行（信号量限制并发数, 避免 LLM 服务过载）
    import asyncio
    batches = [compact_all[i:i + BATCH] for i in range(0, len(compact_all), BATCH)]
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _run_with_sem(batch):
        async with sem:
            return await _gen_batch(batch)

    results = await asyncio.gather(*[_run_with_sem(b) for b in batches], return_exceptions=True)

    merged: Dict[str, dict] = {}
    for r in results:
        if isinstance(r, dict):
            merged.update(r)
        elif isinstance(r, Exception):
            logger.warning(f"[LLM-Controls] 某批生成异常: {r}")

    logger.info(f"[LLM-Controls] 累计生成 {len(merged)}/{len(compact_all)} 条（并发 {CONCURRENCY} 批 × {BATCH} 条/批）")
    if not merged:
        logger.error("[LLM-Controls] LLM 生成控制措施失败，将使用模板兜底")
    return merged


# ═══════════════════════════════════════════════════════════════
# 路径 / 缓存管理
# ═══════════════════════════════════════════════════════════════

_CRITERIA_CACHE_FILENAME = "risk_criteria.json"
_CONTROLS_CACHE_FILENAME = "risk_controls_cache.json"
_project_registry: Dict[str, Tuple[SodCriteria, ApTable]] = {}


def _get_project_data_dir(project_id: str) -> Path:
    base = Path(__file__).resolve().parent.parent.parent
    data_dir = Path(os.getenv("FMEA_DATA_DIR", str(base / "data" / "projects")))
    return data_dir / project_id


def _get_criteria_cache_path(project_id: str) -> Path:
    return _get_project_data_dir(project_id) / "fmea" / _CRITERIA_CACHE_FILENAME


def _get_controls_cache_path(project_id: str) -> Path:
    return _get_project_data_dir(project_id) / "fmea" / _CONTROLS_CACHE_FILENAME


def _resolve_db_pid(project_id: str) -> Optional[int]:
    """把字符串 project_id 解析为 DB 整数 ID（兼容直接传 id 或名称）"""
    try:
        from engine.tools.fmea_helpers import resolve_project_id_to_db_pid
        if str(project_id).isdigit():
            return int(project_id)
        return resolve_project_id_to_db_pid(project_id)
    except Exception as e:
        logger.warning(f"[risk_tools] _resolve_db_pid({project_id}) 失败: {e}")
        return None


def _run_async_safe(coro):
    """事件循环安全的异步执行（同 main.py 884-897 模式）

    场景：工具可能是同步回调（agent 框架线程 / 主线程 / 无 loop），
    统一兼容：已有 loop + 主线程 / 已有 loop + 非主线程 / 无 loop。
    """
    import asyncio
    import threading
    try:
        loop = asyncio.get_running_loop()
        if threading.current_thread() is threading.main_thread():
            return loop.run_until_complete(coro)
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            return new_loop.run_until_complete(coro)
        finally:
            new_loop.close()
    except RuntimeError:
        return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════
# Agent Tools（公开接口）
# ═══════════════════════════════════════════════════════════════

async def register_risk_criteria_tool(project_id: str) -> str:
    """【必须首先调用】注册 SOD 评分标准和 AP 表。

    工作流程：
    1. 从 DB project_risk_files 表读取项目评分文件（v12: 不再扫本地 risk-files/ 目录）
    2. 按文件名分类：SOD 文件（含 S/O/D/���分规则）和 AP 文件（含 AP/优先级）
    3. 调用 LLM 从原始文件中提取结构化的评分规则
    4. 将提取结果存到 DB fmea_risk_criteria 表（v12: DB 优先）
    5. 后续 run_risk_analysis_tool() 只读 DB 标准，不再调用 LLM

    参数:
        project_id: 项目ID

    返回:
        JSON 格式的注册结果，包含提取的规则数量和文件列表
    """
    db_pid = _resolve_db_pid(project_id)
    if db_pid is None:
        return json.dumps({"success": False, "error": f"无法解析项目 {project_id} 的 DB ID"}, ensure_ascii=False)

    # v12: 从 DB project_risk_files 表读文件（前端上传的评分文件存在这里）
    from engine.tools.fmea_helpers import list_risk_files as db_list_files, get_risk_file_content as db_get_file

    file_meta = db_list_files(db_pid)
    if not file_meta:
        # v21: 暂时不支持项目上传评分文件——直接使用全局 AIAG-VDA 标准（DB 全局行）
        from engine.tools.fmea_helpers import get_risk_criteria as _grc
        global_criteria = _grc(0) if False else None  # 不传 project_id, 强制全局
        # 直接 SELECT 全局行 (绕开 get_risk_criteria 的 project_id 优先级)
        try:
            with get_user_db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""SELECT criteria FROM fmea_risk_criteria
                                   WHERE project_id IS NULL ORDER BY version DESC LIMIT 1""")
                    row = cur.fetchone()
                    if row:
                        raw = row[0]
                        global_criteria = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            logger.warning(f"[RiskCriteria] 读全局标准失败: {e}")
        if global_criteria:
            try:
                sod_criteria = SodCriteria.from_dict(global_criteria.get("sod_criteria", {}))
                ap_table = ApTable(rules=[ApRule.from_dict(r) for r in global_criteria.get("ap_table", {}).get("rules", [])])
                _project_registry[project_id] = (sod_criteria, ap_table)
                results.update({
                    "sod_source": "global (AIAG-VDA 2021 默认)",
                    "ap_source": "global (AIAG-VDA 2021 默认)",
                    "extracted": True,
                    "db_saved": False,
                })
                logger.info(f"[RiskCriteria] 项目 {db_pid} 无评分文件, 已使用全局 AIAG-VDA 标准")
                return json.dumps({
                    "success": True,
                    "message": (
                        f"✅ 已使用全局 AIAG-VDA 2021 评分标准（项目级评分文件上传功能暂未启用）\n"
                        f"  - SOD 来源: 全局默认 ({len(sod_criteria.severity_rules)}条S, "
                        f"{len(sod_criteria.occurrence_rules)}条O, "
                        f"{len(sod_criteria.detection_rules)}条D)\n"
                        f"  - AP 来源: 全局默认 ({len(ap_table.rules)}条规则)\n"
                        f"  ⏭️ 下一步: 调用 run_risk_analysis_tool() 执行风险评估"
                    ),
                    "results": results,
                }, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"[RiskCriteria] 全局标准解析失败: {e}")
        # 全局行也没/解析失败 — 用代码内置默认 + 显式提示
        return json.dumps({
            "success": False,
            "error": "全局 AIAG-VDA 评分标准未配置（请检查 fmea_risk_criteria 表 project_id IS NULL 行）",
        }, ensure_ascii=False)

    # 读取文件内容（按文件名分类）
    sod_files, ap_files = [], []
    for meta in file_meta:
        fname = meta.get("filename", "")
        name_lower = fname.lower()
        content = db_get_file(db_pid, fname) or ""
        if any(kw in name_lower for kw in ['s值', 'o值', 'd值', 'sod', '打分规则',
                                           '严重度', '发生度', '探测度',
                                           'severity', 'occurrence', 'detection']):
            sod_files.append({"filename": fname, "content": content})
        if any(kw in name_lower for kw in ['ap', '优先级', '措施优先级', 'action priority']):
            ap_files.append({"filename": fname, "content": content})

    # 兜底：没有分类出来时全部交给 LLM 判断
    if not sod_files and not ap_files:
        sod_files = [{"filename": m.get("filename", ""), "content": db_get_file(db_pid, m.get("filename", "")) or ""}
                     for m in file_meta]
        logger.info("[RiskCriteria] 未通过文件名分类，将全部文件交由 LLM 判断")

    results = {
        "project_id": project_id,
        "db_pid": db_pid,
        "sod_files": [f["filename"] for f in sod_files],
        "ap_files": [f["filename"] for f in ap_files],
        "extracted": False,
    }

    # ── LLM 提取 SOD 标准 ──
    sod_data = None
    if sod_files:
        logger.info(f"[RiskCriteria] 调用 LLM 提取 SOD 标准 ({len(sod_files)} 个文件)")
        sod_data = await _extract_sod_with_llm(sod_files)
    if not sod_data or not sod_data.get("severity_rules"):
        logger.warning("[RiskCriteria] LLM 提取 SOD 失败或为空，回退到 AIAG/VDA 默认")
        sod_criteria = _build_default_sod()
        results["sod_source"] = "default (AIAG/VDA fallback)"
    else:
        sod_criteria = SodCriteria()
        sod_criteria.severity_rules = [SodCriterion.from_dict(r) for r in sod_data.get("severity_rules", [])]
        sod_criteria.occurrence_rules = [SodCriterion.from_dict(r) for r in sod_data.get("occurrence_rules", [])]
        sod_criteria.detection_rules = [SodCriterion.from_dict(r) for r in sod_data.get("detection_rules", [])]
        results["sod_source"] = f"LLM extracted from {len(sod_files)} file(s)"

    # ── LLM 提取 AP 表 ──
    ap_data = None
    if ap_files:
        logger.info(f"[RiskCriteria] 调用 LLM 提取 AP 表 ({len(ap_files)} 个文件)")
        ap_data = await _extract_ap_with_llm(ap_files)
    if not ap_data or not ap_data.get("rules"):
        logger.warning("[RiskCriteria] LLM 提取 AP 表失败或为空，回退到 AIAG/VDA 默认")
        ap_table = ApTable(rules=_build_default_ap_rules())
        results["ap_source"] = "default (AIAG/VDA fallback)"
    else:
        ap_table = ApTable(rules=[ApRule.from_dict(r) for r in ap_data.get("rules", [])])
        results["ap_source"] = f"LLM extracted from {len(ap_files)} file(s)"

    # ── v12: 存 DB（fmea_risk_criteria 表）；v18: 不再写本地缓存 ──
    from engine.tools.fmea_helpers import save_risk_criteria
    criteria_dict = {"sod_criteria": sod_criteria.to_dict(), "ap_table": ap_table.to_dict(),
                     "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    db_ok = save_risk_criteria(db_pid, criteria_dict)
    results["db_saved"] = db_ok
    if not db_ok:
        logger.warning("[RiskCriteria] DB 写入失败，仅保留进程内缓存（v18: 不写本地文件）")

    # 写入进程缓存
    _project_registry[project_id] = (sod_criteria, ap_table)
    results["extracted"] = True

    return json.dumps({
        "success": True,
        "message": (
            f"✅ 风险评分标准已注册\n"
            f"  - SOD 来源: {results['sod_source']} "
            f"({len(sod_criteria.severity_rules)}条S, "
            f"{len(sod_criteria.occurrence_rules)}条O, "
            f"{len(sod_criteria.detection_rules)}条D)\n"
            f"  - AP 来源: {results['ap_source']} "
            f"({len(ap_table.rules)}条规则)\n"
            f"  - 存储: {'DB (fmea_risk_criteria)' if db_ok else '本地缓存(DB写失败兜底)'}\n"
            f"  ⏭️ 下一步: 调用 run_risk_analysis_tool() 执行风险评估"
        ),
        "results": results,
    }, ensure_ascii=False)


def run_risk_analysis_tool(project_id: str = "", use_llm_controls: bool = True) -> str:
    """运行风险评估（Step 4 / 阶段5）。基于失效分析结果(DB)和评分标准(DB)，
    逐条计算风险指标(S/O/D/RPN/AP)，结果保存 DB 04-risk。

    v12 变化：
    - 失效数据从 DB 读（get_baseline(db_pid, "failure")），不再读本地 03-failure.json
    - 评分标准从 DB 读（fmea_risk_criteria），兜底本地 risk_criteria.json → AIAG/VDA 默认
    - 每条 risk item 从 function.domain_rows 按 function_id JOIN regulation_binding（法规高亮依据）
    - S 未命中标准时用 LLM severity 兜底（记录 severity_source）；O/D 未命中标记 uncertain
    - AP 查表未命中返回 ap=""（uncertain），禁止 fallback RPN
    - PC/DC 用 LLM 批量生成（use_llm_controls=True），失败 fallback 模板；结果缓存本地

    参数:
        project_id: 项目ID（可选，不提供则自动检测）
        use_llm_controls: 是否用 LLM 生成预防/探测控制措施（默认 True）

    返回:
        JSON 格式的风险评估结果
    """
    base = Path(__file__).resolve().parent.parent.parent
    data_dir = Path(os.getenv("FMEA_DATA_DIR", str(base / "data" / "projects")))

    # 自动检测项目
    if not project_id and data_dir.exists():
        candidates = sorted(data_dir.iterdir(), reverse=True)
        for d in candidates:
            if d.is_dir() and (d / "fmea" / "03-failure.json").exists():
                project_id = d.name
                break

    if not project_id:
        return "未找到已完成失效分析的项目，请先完成失效分析（Step 3）"

    db_pid = _resolve_db_pid(project_id)

    # ── v12: 读取失效数据（DB 优先）──
    failure_data = None
    if db_pid:
        from engine.tools.fmea_helpers import get_baseline
        failure_data = get_baseline(db_pid, "failure")

    # 兜底：DB 无数据时从本地 JSON 读
    if not failure_data or not failure_data.get("items"):
        failure_path = data_dir / project_id / "fmea" / "03-failure.json"
        if not failure_path.exists():
            return f"失效分析数据不存在（DB 无 baseline 且本地文件缺失）: {failure_path}"
        try:
            with open(failure_path, "r", encoding="utf-8-sig") as f:
                failure_data = json.load(f)
            logger.info(f"[RiskAnalysis] DB 无失效数据，已从本地 JSON 兜底: {failure_path}")
        except Exception as e:
            return f"读取失效数据失败: {e}"

    failures = failure_data.get("items", [])
    if not failures:
        return "失效分析数据为空"

    # v23.4: 进度回调——实时通知前端
    try:
        from engine.tools.project_init_tools import _set_tool_progress
        def _rp(phase, message, progress, **extra):
            _set_tool_progress(project_id, {
                "tool": "run_risk_analysis", "phase": phase, "message": message,
                "progress": progress, "items_count": len(failures), "updated_at": __import__("time").time(), **extra,
            })
    except Exception:
        def _rp(*_a, **_k): pass
    _rp("loading_data", f"已加载 {len(failures)} 条失效数据", 0.1)

    # ── v12: 从 function.domain_rows JOIN regulation_binding / 结构信息 ──
    func_rows_by_fn_id = {}
    func_rows_by_node_id = {}
    if db_pid:
        from engine.tools.fmea_helpers import get_baseline as _gb
        func_data = _gb(db_pid, "function")
        if func_data:
            for dr in (func_data.get("domain_rows", []) or []):
                if dr.get("function_id"):
                    func_rows_by_fn_id[dr["function_id"]] = dr
                if dr.get("node_id"):
                    func_rows_by_node_id.setdefault(dr["node_id"], []).append(dr)

    # ── 加载评分标准（v12: DB 优先 → 本地缓存 → AIAG/VDA 默认）──
    sod_criteria, ap_table = _project_registry.get(project_id, (None, None))
    if sod_criteria is None and db_pid:
        from engine.tools.fmea_helpers import get_risk_criteria
        criteria_db = get_risk_criteria(db_pid)
        if criteria_db:
            try:
                sod_criteria = SodCriteria.from_dict(criteria_db.get("sod_criteria", {}))
                ap_table = ApTable.from_dict(criteria_db.get("ap_table", {}))
                _project_registry[project_id] = (sod_criteria, ap_table)
                logger.info(f"[RiskAnalysis] 从 DB 加载评分标准: pid={db_pid}")
            except Exception as e:
                logger.warning(f"[RiskAnalysis] DB 标准解析失败: {e}��回退本地")
    if sod_criteria is None:
        cached = _load_criteria_from_cache(project_id)
        if cached:
            sod_criteria, ap_table = cached
            _project_registry[project_id] = (sod_criteria, ap_table)
            logger.info(f"[RiskAnalysis] 从本地缓存加载评分标准: {_get_criteria_cache_path(project_id)}")
        else:
            logger.info("[RiskAnalysis] 无缓存，使用默认 AIAG/VDA 标准")
            sod_criteria = _build_default_sod()
            ap_table = ApTable(rules=_build_default_ap_rules())
            _project_registry[project_id] = (sod_criteria, ap_table)

    # ── v12: 生成/加载控制措施 ──
    _rp("loading_criteria", "加载评分标准中...", 0.15)
    controls_cache: Dict[str, dict] = {}
    controls_cache_path = _get_controls_cache_path(project_id)
    if controls_cache_path.exists():
        try:
            with open(controls_cache_path, "r", encoding="utf-8") as f:
                controls_cache = json.load(f)
        except Exception:
            controls_cache = {}

    llm_controls: Dict[str, dict] = {}
    if use_llm_controls:
        # 找出缓存中缺失的条目（按 id + failure_cause 摘要判断是否过期）
        missing = []
        for fm in failures:
            fid = fm.get("id", "")
            if not fid:
                continue
            sig = hashlib.md5((fm.get("failure_cause") or "").encode()).hexdigest()[:8]
            cached = controls_cache.get(fid)
            if not cached or cached.get("_sig") != sig:
                missing.append(fm)
        if missing:
            _rp("generating_controls", f"LLM 并发生成 {len(missing)} 条控制措施 (分批 40 条 × 4 并发)...", 0.2)
            try:
                llm_controls = _run_async_safe(_generate_controls_with_llm(missing))
                # 更新缓存（v18: 只更新内存缓存，不写本地文件）
                for fid, ctl in llm_controls.items():
                    fm = next((x for x in failures if x.get("id") == fid), None)
                    controls_cache[fid] = {
                        **ctl,
                        "_sig": hashlib.md5((fm.get("failure_cause") or "").encode()).hexdigest()[:8] if fm else "",
                    }
            except Exception as e:
                logger.warning(f"[RiskAnalysis] LLM 生成控制措施异常: {e}")
            _rp("controls_done", f"控制措施已就绪（{len(llm_controls)}/{len(missing)}）", 0.7)

    # ── 逐条计算 ──
    _rp("calculating", f"开始逐条计算 S/O/D/RPN/AP...", 0.75)
    risk_items = []
    uncertain_count = 0
    for fm in failures:
        failure_id = fm.get("id", "")
        failure_effect = fm.get("failure_effect", "")
        failure_mode = fm.get("failure_mode", "")
        failure_cause = fm.get("failure_cause", "")
        severity_reason = fm.get("severity_reason", "")
        severity_input = fm.get("severity", 4)

        # 关联 function domain_row（法规绑定 + 结构信息）
        fn_row = func_rows_by_fn_id.get(fm.get("function_id", "")) or {}
        regulation_binding = fn_row.get("regulation_binding", "") or ""

        # ── 控制措施：LLM 生成 > 缓存 > 模板 ──
        ctl = llm_controls.get(failure_id) or controls_cache.get(failure_id) or {}
        prevention_control = ctl.get("prevention_control") or _suggest_prevention_control(failure_cause, failure_mode)
        detection_control = ctl.get("detection_control") or _suggest_detection_control(failure_mode, failure_cause)
        control_inferred = not (ctl.get("prevention_control") and ctl.get("detection_control"))

        # ── S：标准匹配为主，未命中用 LLM severity 兜底（v12）──
        s, s_matched = sod_criteria.calculate_severity(failure_effect, severity_reason)
        severity_source = "criteria"
        if not s_matched:
            # 标准未命中任何规则 → 用失效分析 LLM 的严重度评估（补充依据），并标记
            s = int(severity_input) if isinstance(severity_input, (int, float)) and 1 <= int(severity_input) <= 10 else 5
            severity_source = "llm_fallback"

        # ── O/D：标准匹配 + 措施修正 ──
        o, o_matched = sod_criteria.calculate_occurrence(failure_cause, prevention_control)
        d, d_matched = sod_criteria.calculate_detection(failure_mode, detection_control)

        rpn = s * o * d
        ap, ap_certain = ap_table.calculate_ap(s, o, d)
        ap_display = ap if ap_certain else ""

        # v12: uncertain = AP 表未命中 或 O/D 标准未命中（无兜底值，需人工确认）
        # S 未命中时已用 LLM severity 兜底（有依据），不计入 uncertain
        if not ap_certain or not (o_matched and d_matched):
            uncertain_count += 1

        risk_items.append({
            # v12.1: id 必须存在 — 后端 save_assignee_data 对 risk 步骤合并用 key='id'，
            #   缺 id 会导致 merge_assignee_data 的 item_key 为 None → baseline items 被清空（保存丢数据）
            "id": failure_id,
            "failure_id": failure_id,
            "failure_mode": failure_mode,
            "severity": s,
            "severity_source": severity_source,          # criteria / llm_fallback
            "severity_reason": severity_reason,
            "prevention_control": prevention_control,
            "occurrence": o,
            "occurrence_source": "criteria" if o_matched else "uncertain",
            "detection_control": detection_control,
            "detection": d,
            "detection_source": "criteria" if d_matched else "uncertain",
            "rpn": rpn,
            "ap": ap_display,                             # "" = 需人工判定
            "ap_uncertain": not ap_certain,
            "special_characteristics": "",
            # v12: 法规绑定（需求 4.5.2-4 法规高亮依据）
            "regulation_binding": regulation_binding,
            "regulation_violation": bool(regulation_binding),  # 涉及强制法规条款（无论 AP 等级均需关注）
            # v12: 控制措施来源标记
            "control_inferred": control_inferred,          # True=模板推断，False=LLM/缓存生成
            # 链式方法论：透传 (focus, child) 关联
            "node_id": fm.get("node_id", ""),
            "node_name": fm.get("node_name", ""),
            "child_node_id": fm.get("child_node_id", ""),
            "function_id": fm.get("function_id", ""),
            "parent_node": fn_row.get("parent_node", "") or fm.get("higher_level_name", ""),
            "middle_node": fn_row.get("middle_node", "") or fm.get("node_name", ""),
            "child_node": fn_row.get("child_node", "") or fm.get("child_node_name", ""),
            # v12.1: 关注要素功能和要求（middle_function）— 前端"关注要素功能和要求"列
            "middle_function": fn_row.get("middle_function", "") or fm.get("function_description", ""),
            "llm_generated": False,
            "user_modified": False,
            # v9: 透传失效分析中的 assignee（用于 save_assignee_data 按工程师拆分）
            "assignee_user_id": fm.get("assignee_user_id", -1),
            "assignee_name": fm.get("assignee_name", ""),
        })

    # ── 统计 ──
    ap_counts = {"H": 0, "M": 0, "L": 0, "": 0}
    for r in risk_items:
        ap_counts[r["ap"]] = ap_counts.get(r["ap"], 0) + 1
    rpn_values = [r["rpn"] for r in risk_items]
    avg_rpn = sum(rpn_values) / len(rpn_values) if rpn_values else 0
    high_risk = [r for r in risk_items if r["ap"] == "H" or r["regulation_violation"]]
    regulation_count = sum(1 for r in risk_items if r["regulation_violation"])

    # ── 保存（v18: 只写 DB，不写本地 JSON）──
    risk_data = {
        "items": risk_items,
        "llm_raw_output": "",
        "user_edited": False,
        "meta": {
            "version": "v12",
            "controls_source": "llm" if (use_llm_controls and llm_controls) else "template",
            "uncertain_count": uncertain_count,
            "regulation_count": regulation_count,
        },
    }

    db_ok = False
    if db_pid:
        try:
            from engine.tools.fmea_helpers import upsert_baseline, save_assignee_data
            db_ok = upsert_baseline(db_pid, "risk", risk_data)
            save_assignee_data(db_pid, "risk", -1, risk_data, filter_own=False, force_split=True)
            logger.info(f"[RiskAnalysis] DB 写入成功: project={db_pid} step=risk")
            _rp("done", f"✅ 风险评估完成 ({len(risk_items)} 条, AP H/M/L={ap_counts.get('H',0)}/{ap_counts.get('M',0)}/{ap_counts.get('L',0)})", 1.0,
                total=len(risk_items), high_risk_count=len(high_risk),
                ap_distribution=ap_counts)
        except Exception as e:
            logger.warning(f"[RiskAnalysis] DB 写入失败: {e}")

    logger.info(f"[RiskAnalysis] ✅ 风险评估完成: {len(risk_items)} 条, "
                f"AP(H={ap_counts.get('H',0)}, M={ap_counts.get('M',0)}, "
                f"L={ap_counts.get('L',0)}, 待判定={ap_counts.get('',0)}), "
                f"法规相关={regulation_count}, 平均RPN={avg_rpn:.1f}")

    # v17.12: 阶段完成邮件（给项目负责人）
    try:
        from engine.tools.notify_tools import send_stage_complete_email
        send_stage_complete_email(4, project_id)
    except Exception:
        pass

    return json.dumps({
        "success": True,
        "status": "completed",
        "project_id": project_id,
        "summary": {
            "total": len(risk_items),
            "high_risk_count": len(high_risk),
            "regulation_count": regulation_count,
            "ap_distribution": ap_counts,
            "avg_rpn": round(avg_rpn, 1),
            "max_rpn": max(rpn_values) if rpn_values else 0,
            "uncertain_count": uncertain_count,
            "db_saved": db_ok,
        },
        "saved_to": f"db://04-risk (project={project_id})",
        "message": (
            f"✅ 风险评估完成 ({len(risk_items)} 条失效链)\n"
            f"   - 🔴 高风险(AP=H): {ap_counts.get('H',0)} 条\n"
            f"   - 🟡 中风险(AP=M): {ap_counts.get('M',0)} 条\n"
            f"   - 🟢 低风险(AP=L): {ap_counts.get('L',0)} 条\n"
            f"   - ⚖️ 涉及强制法规条款: {regulation_count} 条\n"
            f"   - ⚠️ 需人工判定(AP/SOD未命中): {uncertain_count} 条\n"
            f"   - 平均 RPN: {avg_rpn:.1f}"
        ),
    }, ensure_ascii=False)


def get_risk_status_tool(project_id: str) -> str:
    """获取项目风险评估状态。"""
    base = Path(__file__).resolve().parent.parent.parent
    data_dir = Path(os.getenv("FMEA_DATA_DIR", str(base / "data" / "projects")))

    risk_path = data_dir / project_id / "fmea" / "04-risk.json"
    cache_path = _get_criteria_cache_path(project_id)

    db_pid = _resolve_db_pid(project_id)
    db_has_criteria = False
    db_has_risk = False
    risk_files = []
    if db_pid:
        from engine.tools.fmea_helpers import get_risk_criteria as _grc, list_risk_files as _lrf
        db_has_criteria = _grc(db_pid) is not None
        risk_files = [m.get("filename", "") for m in _lrf(db_pid)]
        try:
            from engine.tools.fmea_helpers import get_baseline as _gb
            db_has_risk = _gb(db_pid, "risk") is not None
        except Exception:
            pass

    info = {"project_id": project_id, "db_pid": db_pid}
    info["criteria_cached"] = db_has_criteria or cache_path.exists()
    info["criteria_source"] = "DB" if db_has_criteria else ("local_cache" if cache_path.exists() else "none")
    info["risk_analysis_exists"] = db_has_risk or risk_path.exists()
    info["risk_files"] = risk_files

    risk_data = None
    if db_has_risk and db_pid:
        from engine.tools.fmea_helpers import get_baseline as _gb
        risk_data = _gb(db_pid, "risk")
    elif risk_path.exists():
        try:
            with open(risk_path, "r", encoding="utf-8-sig") as f:
                risk_data = json.load(f)
        except Exception:
            pass

    if risk_data:
        items = risk_data.get("items", [])
        info["total_items"] = len(items)
        info["ap_distribution"] = {
            "H": sum(1 for r in items if r.get("ap") == "H"),
            "M": sum(1 for r in items if r.get("ap") == "M"),
            "L": sum(1 for r in items if r.get("ap") == "L"),
            "": sum(1 for r in items if not r.get("ap")),
        }
        info["regulation_count"] = sum(1 for r in items if r.get("regulation_violation"))

    return json.dumps(info, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
# 默认 AIAG/VDA 标准（LLM 或缓存均失败时的回退）
# ═══════════════════════════════════════════════════════════════

def _build_default_sod() -> SodCriteria:
    sc = SodCriteria()
    sc.severity_rules = [
        SodCriterion(10, "安全/法规风险（无预警）", ["安全", "法规", "人身伤害", "死亡", "着火", "爆炸", "热失控"]),
        SodCriterion(9, "安全/法规风险（有预警）", ["安全预警", "法规预警", "潜在安全"]),
        SodCriterion(8, "主要功能丧失", ["功能丧失", "无法工作", "不能工作", "完全丧失", "中断", "失灵"]),
        SodCriterion(7, "主要功能降级", ["功能降级", "性能下降", "功能退化", "降级"]),
        SodCriterion(6, "次要功能丧失", ["次要功能", "辅助功能", "舒适性"]),
        SodCriterion(5, "外观/噪音（客户明显不满意）", ["外观", "噪音", "异响", "投诉", "不满意"]),
        SodCriterion(4, "外观/噪音（多数客户注意到）", ["多数客户", "表面缺陷", "外观缺陷"]),
        SodCriterion(3, "外观/噪音（部分客户注意到）", ["部分客户", "轻微"]),
        SodCriterion(2, "几乎无影响", ["几乎无影响", "轻微影响", "极少"]),
        SodCriterion(1, "无影响", ["无影响", "不影响", "没有影响"]),
    ]
    sc.occurrence_rules = [
        SodCriterion(10, "每件/频繁", ["每件", "频繁", "连续", "总是", "频发"]),
        SodCriterion(9, "每批/经常", ["每批", "经常", "高频"]),
        SodCriterion(8, "每小时/高频", ["每小时"]),
        SodCriterion(7, "每天/每日", ["每天", "每日"]),
        SodCriterion(6, "每月/每周", ["每月", "每周"]),
        SodCriterion(5, "每季度/偶尔", ["每季度", "偶尔"]),
        SodCriterion(4, "每年/较少", ["每年", "较少"]),
        SodCriterion(3, "每3年/很少", ["每3年", "每三年", "很少", "罕见"]),
        SodCriterion(2, "每10年/极少", ["每10年", "每十年", "极少"]),
        SodCriterion(1, "从未发生/极不可能", ["从未", "极不可能", "不会发生"]),
    ]
    sc.detection_rules = [
        SodCriterion(10, "无法探测", ["无法探测", "不能探测", "无探测手段", "无法检测"]),
        SodCriterion(9, "随机人工检查", ["随机", "人工检查", "目视", "目检"]),
        SodCriterion(8, "人工检查", ["人工", "手动检查", "离线检查"]),
        SodCriterion(7, "人工计量", ["人工计量", "人工测量"]),
        SodCriterion(6, "自动检测（手动记录）", ["自动检测手动", "半自动"]),
        SodCriterion(5, "自动检测（自动记录）", ["自动检测自动记录", "自动记录"]),
        SodCriterion(4, "自动检测（在线）", ["在线检测", "在线监控", "实时监控"]),
        SodCriterion(3, "防错（过程后）", ["防错", "Poka-yoke", "过程后"]),
        SodCriterion(2, "防错（过程中）", ["防错过程中", "过程中防错"]),
        SodCriterion(1, "防错（设计）", ["防错设计", "设计防错", "固有设计"]),
    ]
    return sc


def _build_default_ap_rules() -> list[ApRule]:
    """AIAG-VDA 2021 标准 AP 表（H/M/L）— 完整覆盖全部 S×O×D 组合

    来源：AIAG & VDA FMEA Handbook 2021 AP 表（D 分档：1 / 2-4 / 5-6 / 7-10）
    需求 V5-3 + 2.3：AP 必须查标准表，禁止 fallback 到 RPN 阈值。
    """
    # (s_min,s_max), (o_min,o_max), [(d_min,d_max,ap), ...]
    _AP = [
        ((9, 10), (8, 10), [(7, 10, "H"), (5, 6, "H"), (2, 4, "H"), (1, 1, "H")]),
        ((9, 10), (6, 7),  [(7, 10, "H"), (5, 6, "H"), (2, 4, "H"), (1, 1, "H")]),
        ((9, 10), (4, 5),  [(7, 10, "H"), (5, 6, "H"), (2, 4, "H"), (1, 1, "M")]),
        ((9, 10), (2, 3),  [(7, 10, "H"), (5, 6, "M"), (2, 4, "L"), (1, 1, "L")]),
        ((9, 10), (1, 1),  [(1, 10, "L")]),
        ((7, 8), (8, 10),  [(7, 10, "H"), (5, 6, "H"), (2, 4, "H"), (1, 1, "H")]),
        ((7, 8), (6, 7),   [(7, 10, "H"), (5, 6, "H"), (2, 4, "H"), (1, 1, "M")]),
        ((7, 8), (4, 5),   [(7, 10, "H"), (5, 6, "M"), (2, 4, "M"), (1, 1, "M")]),
        ((7, 8), (2, 3),   [(7, 10, "M"), (5, 6, "M"), (2, 4, "L"), (1, 1, "L")]),
        ((7, 8), (1, 1),   [(1, 10, "L")]),
        ((4, 6), (8, 10),  [(7, 10, "H"), (5, 6, "H"), (2, 4, "M"), (1, 1, "M")]),
        ((4, 6), (6, 7),   [(7, 10, "M"), (5, 6, "M"), (2, 4, "M"), (1, 1, "L")]),
        ((4, 6), (4, 5),   [(7, 10, "M"), (5, 6, "L"), (2, 4, "L"), (1, 1, "L")]),
        ((4, 6), (2, 3),   [(7, 10, "L"), (5, 6, "L"), (2, 4, "L"), (1, 1, "L")]),
        ((4, 6), (1, 1),   [(1, 10, "L")]),
        ((2, 3), (8, 10),  [(7, 10, "M"), (5, 6, "M"), (2, 4, "L"), (1, 1, "L")]),
        ((2, 3), (6, 7),   [(7, 10, "L"), (5, 6, "L"), (2, 4, "L"), (1, 1, "L")]),
        ((2, 3), (4, 5),   [(7, 10, "L"), (5, 6, "L"), (2, 4, "L"), (1, 1, "L")]),
        ((2, 3), (2, 3),   [(7, 10, "L"), (5, 6, "L"), (2, 4, "L"), (1, 1, "L")]),
        ((2, 3), (1, 1),   [(1, 10, "L")]),
        ((1, 1), (1, 10),  [(1, 10, "L")]),
    ]
    rules = []
    for (s_lo, s_hi), (o_lo, o_hi), d_rules in _AP:
        for (d_lo, d_hi, ap) in d_rules:
            rules.append(ApRule(s_lo, s_hi, o_lo, o_hi, d_lo, d_hi, ap))
    return rules


# ═══════════════════════════════════════════════════════════════
# 缓存读取辅助（仅读旧缓存文件兼容，v18: 不再写缓存文件）
# ═══════════════════════════════════════════════════════════════

def _load_criteria_from_cache(project_id: str) -> Optional[Tuple[SodCriteria, ApTable]]:
    """从缓存文件读取结构化规则（不依赖 LLM）"""
    cache_path = _get_criteria_cache_path(project_id)
    if not cache_path.exists():
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        sod = SodCriteria.from_dict(data.get("sod_criteria", {}))
        ap = ApTable.from_dict(data.get("ap_table", {}))
        return sod, ap
    except Exception as e:
        logger.warning(f"[Cache] 读取缓存失败: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# 辅助函数：预防/探测措施模板兜底（LLM 生成失败时）
# ═══════════════════════════════════════════════════════════════

_PREVENTION_TEMPLATES = {
    "材料": "设计选型：材料认证与规格验证；来料特性确认(PC)",
    "焊接": "设计评审：焊接工艺可行性评审；焊缝设计标准确认(PC)",
    "密封": "设计评审：密封结构设计评审；密封材料选型验证(PC)",
    "连接": "设计评审：连接可靠性分析；紧固扭矩规格设计验证(PC)",
    "装配": "设计评审：装配可行性分析；防错结构设计(PC)",
    "电气": "设计评审：原理图评审；电气负载降额设计分析(PC)",
    "软件": "设计评审：软件架构评审；控制逻辑仿真验证(PC)",
    "疲劳": "设计分析：疲劳耐久性分析；材料疲劳强度验证(PC)",
    "腐蚀": "设计分析：腐蚀防护设计；材料耐蚀性选型验证(PC)",
    "振动": "设计分析：振动模态分析；紧固件防松设计(PC)",
    "过热": "设计分析：热仿真分析；散热结构设计验证(PC)",
    "短路": "设计评审：电路保护设计；绝缘间距规格验证(PC)",
}

_DETECTION_TEMPLATES = {
    "材料": "DV验证：材料性能测试；进货材质报告审核(DC)",
    "焊接": "DV验证：焊缝无损检测；焊接强度测试(DC)",
    "密封": "DV验证：气密性测试；泄漏试验(DC)",
    "连接": "DV验证：拉拔力测试；扭矩复检试验(DC)",
    "装配": "DV验证：装配功能测试；极限公差装配试验(DC)",
    "电气": "DV验证：电气性能测试；EOL 测试(DC)",
    "软件": "DV验证：集成测试；系统级功能测试(DC)",
    "疲劳": "DV验证：耐久测试；加速寿命试验(DC)",
    "腐蚀": "DV验证：盐雾测试；环境可靠性试验(DC)",
    "振动": "DV验证：振动测试；冲击试验(DC)",
    "过热": "DV验证：温升测试；热成像检测(DC)",
    "短路": "DV验证：绝缘测试；耐压测试(DC)",
}


def _suggest_prevention_control(failure_cause: str, failure_mode: str) -> str:
    text = f"{failure_cause} {failure_mode}"
    for kw, tpl in _PREVENTION_TEMPLATES.items():
        if kw in text:
            return tpl
    return "设计评审：设计评审与仿真分析(PC)"


def _suggest_detection_control(failure_mode: str, failure_cause: str) -> str:
    text = f"{failure_mode} {failure_cause}"
    for kw, tpl in _DETECTION_TEMPLATES.items():
        if kw in text:
            return tpl
    return "DV验证：DV 测试与功能验证(DC)"
