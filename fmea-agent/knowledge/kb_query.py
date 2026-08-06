# ═══════════════════════════════════════════════════════════════
# [已注释] 知识库查询引擎 — 基于 LLM 的智能查询
# 原因: kb_query 存在 MiniMax 模型输出退化循环导致的必现 JSON 解析错误
# 相关代码已在 main.py / agents / tools 中同步注释
# ═══════════════════════════════════════════════════════════════
# 原始内容如下:
# ═══════════════════════════════════════════════════════════════
# """
# 知识库查询引擎 — 基于 LLM 的智能查询

# 移植自 LLMWiKiDemo 的 WikiQueryService 核心逻辑

# 支持:
# 1. 页面选择（LLM tool_call）
# 2. 内容加载与清理
# 3. 流式回答生成
# 4. React 范式迭代查询（多轮推理，不断完善结果）
# """
# import json
# import re
# import asyncio
# import logging
# from pathlib import Path
# from typing import Optional, Dict, List, Callable, AsyncGenerator

# from knowledge.kb_service import get_kb_service

# logger = logging.getLogger(__name__)


# class KnowledgeQueryEngine:
#     """知识库查询引擎"""

#     # 每次查询加载的最大页面数
#     MAX_PAGES = 8
#     # 内容最大字符数（单次输入给 LLM 的上下文）
#     MAX_CONTENT_CHARS = 12000
#     # React 迭代最大轮次
#     MAX_REACT_ROUNDS = 5

#     def __init__(self, llm_client=None):
#         """
#         Args:
#             llm_client: LLMClient 实例（可选，可从 engine.llm_client 导入）
#         """
#         self.llm_client = llm_client
#         self.kb_service = get_kb_service()

#     def _get_llm(self):
#         if self.llm_client:
#             return self.llm_client
#         from engine.llm_client import get_llm_client
#         return get_llm_client()

#     # ═══════════════════════════════════════════════════════════
#     # 简单查询（单轮）
#     # ═══════════════════════════════════════════════════════════

#     async def query(self, project_id: str, question: str) -> Dict:
#         """
#         查询知识库（同步版，单轮）

#         Args:
#             project_id: 项目ID
#             question: 查询问题

#         Returns:
#             {"answer": 回答, "selected_pages": 选中的页面列表, "sources": 源文件列表}
#         """
#         kb = get_kb_service()

#         # 1. 检查知识库状态
#         status = kb.get_status(project_id)
#         if not status["exists"] or status["total_pages"] == 0:
#             return {"answer": "知识库为空，请先初始化项目知识库。", "selected_pages": [], "sources": []}

#         # 2. 读取索引
#         index_content = kb.read_index(project_id)
#         if not index_content:
#             index_content = self._generate_on_the_fly_index(project_id)

#         # 3. 选择相关页面
#         selected_pages = await self._select_pages(question, index_content, project_id)

#         if not selected_pages:
#             # 关键词回退
#             selected_pages = self._keyword_search(project_id, question)

#         if not selected_pages:
#             return {"answer": "未找到相关内容。", "selected_pages": [], "sources": []}

#         # 4. 加载页面内容
#         content, sources = self._load_pages(project_id, selected_pages)

#         if not content:
#             return {"answer": "未能加载页面内容。", "selected_pages": selected_pages, "sources": []}

#         # 5. 生成回答
#         answer = await self._generate_answer(question, content, sources)

#         return {
#             "answer": answer,
#             "selected_pages": selected_pages,
#             "sources": sources,
#         }

#     # ═══════════════════════════════════════════════════════════
#     # React 范式查询（多轮推理迭代）
#     # ═══════════════════════════════════════════════════════════

#     async def query_react(
#         self,
#         project_id: str,
#         task: str,
#         progress_callback: Optional[Callable] = None,
#     ) -> Dict:
#         """
#         使用 React 范式进行多轮迭代查询

#         React = Reasoning + Acting:
#         1. Thought: 思考当前已知信息，判断还需要什么
#         2. Action: 执行查询操作
#         3. Observation: 观察查询结果
#         4. 重复 1-3 直到满意
#         5. Final Answer: 输出最终结果

#         用于从知识库中逐步构建结构树等复杂任务

#         Args:
#             project_id: 项目ID
#             task: 任务描述（如 "从知识库中提取产品结构树"）
#             progress_callback: 进度回调 (round, max_rounds, thought, action)

#         Returns:
#             {"answer": 最终回答, "rounds": 迭代轮次, "findings": 中间发现}
#         """
#         kb = get_kb_service()
#         status = kb.get_status(project_id)

#         if not status["exists"] or status["total_pages"] == 0:
#             return {"answer": "知识库为空", "rounds": 0, "findings": []}

#         llm = self._get_llm()
#         all_findings = []
#         context_pages = set()
#         final_answer = ""

#         for round_num in range(1, self.MAX_REACT_ROUNDS + 1):
#             if progress_callback:
#                 progress_callback(round_num, self.MAX_REACT_ROUNDS,
#                                   f"第 {round_num}/{self.MAX_REACT_ROUNDS} 轮推理...", "")

#             # Phase 1: Thought — 模型思考下一步
#             thought_prompt = self._build_thought_prompt(task, all_findings, round_num)

#             try:
#                 thought_result = await llm.call_complete(
#                     system_prompt="你是一个FMEA分析专家。请思考下一步该查询什么信息。\n\n重要：输出纯JSON，不要加<think>推理标签。",
#                     user_message=thought_prompt,
#                     force_json=True,
#                 )
#                 # 剥离 <think> 标签再解析
#                 cleaned = re.sub(r"<think>[\s\S]*?</think>", "", thought_result, flags=re.DOTALL)
#                 thought_data = llm.extract_json(cleaned)
#             except Exception as e:
#                 logger.warning(f"[Query] React 思考失败 (round {round_num}): {e}")
#                 # 直接用简单查询做最后一轮
#                 result = await self.query(project_id, task)
#                 final_answer = result["answer"]
#                 break

#             thought = thought_data.get("thought", "")
#             action = thought_data.get("action", "query")
#             query_text = thought_data.get("query", task)
#             is_final = thought_data.get("is_final", False)

#             if progress_callback:
#                 progress_callback(round_num, self.MAX_REACT_ROUNDS, thought, query_text)

#             # Phase 2: Action — 执行查询
#             if is_final:
#                 # 所有已加载内容汇总生成最终答案
#                 all_content, all_sources = self._load_pages(project_id, list(context_pages))
#                 if not all_content:
#                     all_content = "\n\n---\n\n".join(
#                         f.get("content", "") for f in all_findings
#                     )
#                     all_sources = list(set(
#                         src for f in all_findings for src in f.get("sources", [])
#                     ))

#                 final_answer = await self._generate_answer(task, all_content, all_sources)
#                 if progress_callback:
#                     progress_callback(round_num, self.MAX_REACT_ROUNDS,
#                                       "✅ 推理完成，生成最终答案", "")
#                 break

#             else:
#                 # 执行查询
#                 result = await self.query(project_id, query_text)

#                 finding = {
#                     "round": round_num,
#                     "thought": thought,
#                     "query": query_text,
#                     "answer": result["answer"],
#                     "selected_pages": result["selected_pages"],
#                     "sources": result["sources"],
#                 }
#                 all_findings.append(finding)

#                 # 追踪已加载的页面
#                 for page in result["selected_pages"]:
#                     context_pages.add(page)

#                 if progress_callback:
#                     progress_callback(round_num, self.MAX_REACT_ROUNDS,
#                                       f"第 {round_num} 轮完成，已找到 {len(context_pages)} 个相关页面",
#                                       query_text)

#         return {
#             "answer": final_answer or "未能在指定轮次内完成推理。",
#             "rounds": round_num,
#             "findings": all_findings,
#             "total_pages_loaded": len(context_pages),
#         }

#     # ═══════════════════════════════════════════════════════════
#     # 页面选择（LLM tool_call）
#     # ═══════════════════════════════════════════════════════════

#     async def _select_pages(self, question: str, index_content: str,
#                             project_id: str) -> List[str]:
#         """
#         使用 LLM 从索引中选择相关页面

#         参考 WikiQueryService.selectPages()
#         """
#         # 截断索引（避免超出 token 限制）
#         if len(index_content) > 6000:
#             index_content = index_content[:6000] + "\n\n... (索引已截断)"

#         # 获取可用页面列表
#         kb = get_kb_service()
#         pages = kb.list_pages(project_id)
#         page_names = [p["filename"] for p in pages]

#         prompt = f"""你是一个知识库检索专家。根据用户的问题，从给定的文档索引中选择最相关的页面。

# ## 用户问题
# {question}

# ## 知识库索引
# {index_content}

# ## 可用页面列表
# {json.dumps(page_names, ensure_ascii=False)}

# 请从可用页面列表中选择与问题最相关的页面（最多{self.MAX_PAGES}个）。
# 只返回页面文件名列表，用 JSON 数组格式：["file1.md", "file2.md"]"""

#         llm = self._get_llm()
#         try:
#             result = await llm.call_complete(
#                 system_prompt="你是知识库检索专家。只返回 JSON 数组，不要其他内容。禁止输出<think>推理标签。",
#                 user_message=prompt,
#                 force_json=True,
#             )
#             # 剥离 <think> 标签再解析
#             cleaned = re.sub(r"<think>[\s\S]*?</think>", "", result, flags=re.DOTALL)
#             data = llm.extract_json(cleaned)

#             if isinstance(data, list):
#                 selected = [s for s in data if isinstance(s, str)]
#             elif isinstance(data, dict):
#                 pages_key = data.get("pages", data.get("selected_pages", []))
#                 selected = [s for s in pages_key if isinstance(s, str)]
#             else:
#                 selected = []

#             # 验证页面是否真实存在
#             valid_pages = []
#             for s in selected:
#                 clean_name = s.strip()
#                 if clean_name in page_names:
#                     valid_pages.append(clean_name)
#                 elif clean_name + ".md" in page_names:
#                     valid_pages.append(clean_name + ".md")

#             return valid_pages[:self.MAX_PAGES]

#         except Exception as e:
#             raw_output = locals().get('result', None)
#             if raw_output:
#                 logger.warning(
#                     "[Query] LLM 原始输出（调试）:\n%s", raw_output
#                 )
#             logger.warning(f"[Query] LLM 页面选择失败: {e}")
#             return []

#     def _keyword_search(self, project_id: str, question: str) -> List[str]:
#         """关键词回退搜索"""
#         kb = get_kb_service()
#         results = kb.search_pages(project_id, question)
#         return [r["filename"] for r in results[:self.MAX_PAGES]]

#     # ═══════════════════════════════════════════════════════════
#     # 内容加载
#     # ═══════════════════════════════════════════════════════════

#     def _load_pages(self, project_id: str, page_names: List[str]) -> tuple:
#         """
#         加载选中页面的内容

#         参考 WikiQueryService.loadSelectedPagesWithSources()

#         Returns:
#             (合并后的内容文本, 源文件列表)
#         """
#         kb = get_kb_service()
#         contents = []
#         all_sources = set()

#         for name in page_names:
#             content = kb.read_page(project_id, name)
#             if not content:
#                 logger.warning(f"[Query] 页面不存在: {name}")
#                 continue

#             # 清理内容（移除 frontmatter 元数据、wiki 链接等）
#             cleaned = self._clean_content(content)
#             if cleaned:
#                 contents.append(f"## {name}\n\n{cleaned}")

#             # 提取源文件
#             sources = self._extract_sources(content)
#             all_sources.update(sources)

#         # 合并内容，控制总长度
#         merged = "\n\n---\n\n".join(contents)
#         if len(merged) > self.MAX_CONTENT_CHARS:
#             merged = merged[:self.MAX_CONTENT_CHARS] + "\n\n... (内容已截断)"

#         return merged, sorted(all_sources)

#     # ═══════════════════════════════════════════════════════════
#     # 回答生成
#     # ═══════════════════════════════════════════════════════════

#     async def _generate_answer(self, question: str, content: str,
#                                sources: List[str]) -> str:
#         """使用 LLM 基于加载的内容生成回答"""
#         # 截断内容
#         if len(content) > self.MAX_CONTENT_CHARS:
#             content = content[:self.MAX_CONTENT_CHARS] + "\n\n... (内容已截断)"

#         prompt = f"""请根据以下参考内容回答用户的问题。

# ## 用户问题
# {question}

# ## 参考内容
# {content}

# ## 源文件
# {', '.join(sources) if sources else '无'}

# 请提供准确、详细的回答。如果参考内容不足以回答，请明确指出。"""

#         llm = self._get_llm()
#         try:
#             result = await llm.call_complete(
#                 system_prompt="你是一个FMEA分析专家。请基于提供的参考内容回答问题。回答应该准确、结构化，使用中文。不要输出<think>推理标签。",
#                 user_message=prompt,
#                 force_json=False,
#             )
#             # 剥离潜在 <think> 标签
#             result = re.sub(r"<think>[\s\S]*?</think>", "", result, flags=re.DOTALL).strip()
#             return result if result else "无法生成回答。"
#         except Exception as e:
#             logger.error(f"[Query] 回答生成失败: {e}")
#             return f"回答生成失败: {str(e)}"

#     # ═══════════════════════════════════════════════════════════
#     # React 思维提示构建
#     # ═══════════════════════════════════════════════════════════

#     def _build_thought_prompt(self, task: str, findings: List[Dict],
#                               round_num: int) -> str:
#         """构建 React 思考提示"""
#         prompt = f"""## 任务
# {task}

# ## 当前轮次
# 第 {round_num}/{self.MAX_REACT_ROUNDS} 轮

# ## 已知发现
# """
#         if findings:
#             for f in findings:
#                 prompt += f"""
# ### 第 {f['round']} 轮
# - 思考: {f['thought']}
# - 查询: {f['query']}
# - 结果摘要: {f['answer'][:300]}...
# """
#         else:
#             prompt += "（尚无发现，这是第一轮）\n"

#         prompt += f"""
# ## 请决定下一步

# 返回 JSON 格式：
# {{
#     "thought": "你的推理过程（中文，简短）",
#     "action": "query 或 final_answer",
#     "query": "如果是 query，写要查询的问题；如果是 final_answer，写 null",
#     "is_final": true/false
# }}

# 如果已有足够信息完成任务，设置 is_final=true；否则继续查询。
# 每轮只能查询一个明确的问题。"""

#         return prompt

#     # ═══════════════════════════════════════════════════════════
#     # 辅助方法
#     # ═══════════════════════════════════════════════════════════

#     def _clean_content(self, content: str) -> str:
#         """清理内容：移除 frontmatter、wiki 链接等"""
#         # 移除 YAML frontmatter
#         if content.strip().startswith("---"):
#             lines = content.split("\n")
#             end_idx = -1
#             for i in range(1, min(len(lines), 20)):
#                 if lines[i].strip() == "---":
#                     end_idx = i
#                     break
#             if end_idx > 0:
#                 content = "\n".join(lines[end_idx + 1:])

#         # 移除 wiki 链接 [[xxx]]
#         content = re.sub(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]', r'\1', content)
#         # 移除 markdown 图片
#         content = re.sub(r'!\[.*?\]\(.*?\)', '', content)

#         return content.strip()

#     def _extract_sources(self, content: str) -> List[str]:
#         """从内容中提取源文件信息"""
#         sources = []

#         # 从 frontmatter 提取
#         if content.strip().startswith("---"):
#             lines = content.split("\n")
#             end_idx = -1
#             for i in range(1, min(len(lines), 20)):
#                 if lines[i].strip() == "---":
#                     end_idx = i
#                     break
#             if end_idx > 0:
#                 for line in lines[1:end_idx]:
#                     if line.startswith("source_file:"):
#                         source = line.split(":", 1)[1].strip().strip('"').strip("'")
#                         if source:
#                             sources.append(source)

#         return sources

#     def _generate_on_the_fly_index(self, project_id: str) -> str:
#         """动态生成索引（当 index.md 不存在时）"""
#         kb = get_kb_service()
#         pages = kb.list_pages(project_id)

#         if not pages:
#             return "知识库为空。"

#         lines = ["# 项目知识库页面列表", ""]
#         for p in pages:
#             title = p.get("title", p.get("filename", ""))
#             fname = p.get("filename", "")
#             summary = p.get("summary", "")
#             source = p.get("source_file", "")
#             line = f"- **{title}** ({fname})"
#             if summary:
#                 line += f" — {summary[:60]}"
#             if source:
#                 line += f" _[来源: {source}]_"
#             lines.append(line)

#         return "\n".join(lines)


# # ═══════════════════════════════════════════════════════════════
# # 流式查询引擎（SSE）
# # ═══════════════════════════════════════════════════════════════

# class StreamingQueryEngine(KnowledgeQueryEngine):
#     """支持 SSE 流式输出的查询引擎"""

#     async def query_stream(
#         self,
#         project_id: str,
#         question: str,
#         progress_callback: Optional[Callable] = None,
#     ) -> AsyncGenerator[Dict, None]:
#         """
#         流式查询知识库

#         Yields:
#             {"type": "status"/"thinking"/"answer"/"sources"/"done"/"error", "data": ...}
#         """
#         kb = get_kb_service()

#         try:
#             # 状态检查
#             status = kb.get_status(project_id)
#             if not status["exists"] or status["total_pages"] == 0:
#                 yield {"type": "error", "data": "知识库为空，请先初始化项目知识库。"}
#                 return

#             # 读取索引
#             yield {"type": "status", "data": "正在检索相关页面..."}
#             index_content = kb.read_index(project_id)
#             if not index_content:
#                 index_content = self._generate_on_the_fly_index(project_id)

#             # 选择页面
#             yield {"type": "status", "data": "正在选择相关概念..."}
#             selected_pages = await self._select_pages(question, index_content, project_id)

#             if not selected_pages:
#                 selected_pages = self._keyword_search(project_id, question)

#             yield {"type": "status", "data": f"已选择 {len(selected_pages)} 个相关页面"}
#             yield {"type": "thinking", "data": f"选中页面: {', '.join(selected_pages)}"}

#             if not selected_pages:
#                 yield {"type": "error", "data": "未找到相关内容。"}
#                 return

#             # 加载内容
#             yield {"type": "status", "data": "正在加载页面内容..."}
#             content, sources = self._load_pages(project_id, selected_pages)

#             if not content:
#                 yield {"type": "error", "data": "未能加载页面内容。"}
#                 return

#             # 生成回答
#             yield {"type": "status", "data": "正在生成回答..."}

#             answer = await self._generate_answer(question, content, sources)

#             # 发送回答（分块）
#             chunk_size = 200
#             for i in range(0, len(answer), chunk_size):
#                 chunk = answer[i:i + chunk_size]
#                 yield {"type": "answer", "data": chunk}

#             # 发送源文件
#             if sources:
#                 yield {"type": "sources", "data": sources}

#             # 完成
#             yield {"type": "done", "data": {
#                 "answer": answer,
#                 "selected_pages": selected_pages,
#                 "sources": sources,
#             }}

#         except Exception as e:
#             logger.error(f"[Query] 流式查询失败: {e}")
#             yield {"type": "error", "data": f"查询失败: {str(e)}"}


# # ═══════════════════════════════════════════════════════════════
# # 全局单例
# # ═══════════════════════════════════════════════════════════════

# _query_engine: Optional[KnowledgeQueryEngine] = None


# def get_query_engine() -> KnowledgeQueryEngine:
#     global _query_engine
#     if _query_engine is None:
#         _query_engine = KnowledgeQueryEngine()
#     return _query_engine
