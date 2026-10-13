"""
FMEA LLM Client — 三层 fallback 安全调用机制
等价于 Java FmeaPipelineService.safeLLMCall()

层级1: Tool Calling (结构化输出)
层级2: Complete + JSON 强约束  
层级3: 抛出异常交由 Agent 处理
"""
import json
import re
import logging
from typing import Optional, Any, Dict

logger = logging.getLogger(__name__)


def _noop(text: str) -> str:
    return text


def _repair_truncated_json(text: str) -> str:
    """尝试修复被 max_tokens 截断的 JSON"""
    # 统计未闭合的括号
    braces = 0
    brackets = 0
    in_string = False
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
        if in_string:
            continue
        if ch == '{':
            braces += 1
        elif ch == '}':
            braces -= 1
        elif ch == '[':
            brackets += 1
        elif ch == ']':
            brackets -= 1

    # 补全未闭合的括号
    suffix = ""
    # 如果在字符串内未闭合，先闭合字符串
    if in_string:
        suffix += '"'

    # 如果在数组内，先闭合数组
    suffix += "]" * max(brackets, 0)

    # 如果在对象内，补全内容然后闭合
    if braces > 0:
        # 如果最后是逗号或冒号，说明值被截断了，补一个 null
        stripped = text.rstrip()
        if stripped.endswith((',', ':')):
            suffix += 'null'
        suffix += "}" * braces

    return text + suffix if suffix else text


def _char_cost(ch: str) -> float:
    """单个字符的 token 成本估算（保守取大）"""
    o = ord(ch)
    if o < 128:
        return 0.3          # 英文/数字/常见符号
    if 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF:
        return 1.0          # 中日韩汉字
    return 0.6              # 其他非 ASCII


def estimate_tokens(text: str) -> int:
    """估算文本 token 数

    ⚠️ 为什么必须有这个函数：按**字符数**限制输入对中文完全不成立。
    同样是 40000 字符，ASCII（如二进制乱码）约 1.2 万 token，
    真实中文却约 4 万 token，差 3 倍——后者足以把 32k 上下文的模型打爆。
    """
    if not text:
        return 0
    return int(sum(_char_cost(ch) for ch in text))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """按 token 预算截断文本（不做切词，逐字符累加，安全侧保守）"""
    if not text:
        return text
    used = 0.0
    for i, ch in enumerate(text):
        used += _char_cost(ch)
        if used > max_tokens:
            return text[:i]
    return text


def _strip_think(text: Optional[str]) -> Optional[str]:
    """去掉推理模型（DeepSeek-R1 / Qwen3 等）的 <think>...</think> 段

    这些模型被 max_tokens 截断时，content 可能**只有思考内容**，
    导致上层按关键字探测结构化结果时判定为"无效返回"。
    """
    if not text:
        return text
    cleaned = re.sub(r"<think[^>]*>[\s\S]*?</think\s*>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<think[^>]*>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


class LLMClient:
    """LLM 调用客户端，封装三层 fallback 机制"""

    def __init__(self, model_id: str = None,
                 base_url: str = None,
                 api_key: str = None,
                 max_tokens: int = None):
        import os
        self.model_id = model_id or os.getenv("FMEA_MODEL_NAME", "deepseek-chat")
        self.base_url = base_url or os.getenv("FMEA_MODEL_BASE_URL", "https://api.deepseek.com")
        self.api_key = api_key or os.getenv("FMEA_MODEL_API_KEY", os.getenv("DEEPSEEK_API_KEY", ""))
        # 输出预算（max_tokens）。
        # ⚠️ 实测教训：不要盲目顶到上下文上限。推理模型的"思考"会随预算膨胀，
        #    max_tokens=524288 时它思考 30 分钟都不收敛（两次调用各撞 1800s 超时，白等 1 小时）。
        #    32768 实测 150s 内稳定完成；再配合下面的"关闭思考"就更快。
        self.max_tokens = max_tokens or int(os.getenv("FMEA_LLM_MAX_TOKENS", "32768"))
        # 是否关闭推理模型的思考链（默认**保留**思考）。
        #   关思考能把单次调用从 150s 压到 62s，但实测会让这个模型**不再按 tool schema 输出**：
        #   第 1 层返回 8709 字符正文却没有 new_nodes，第 2 层 JSON 里 new_nodes=0，等于白跑。
        #   所以只在"只求速度、不在意结构化"的场景下用 FMEA_LLM_THINKING=off 打开。
        self.thinking_disabled = os.getenv("FMEA_LLM_THINKING", "on").strip().lower() in (
            "off", "false", "0", "none", "disabled", "no")
        # 上下文窗口（用于计算输出预算余量）
        self.context_limit = int(os.getenv("FMEA_LLM_CONTEXT_LIMIT", "524288"))
        # 单次 HTTP 超时。
        # ⚠️ 别设太大：一次失败的批次会连撞"工具调用 + fallback"两次超时。
        #    之前设 1800s 时，单批失败就白等 1 小时（日志 18:24 发请求 / 18:54 超时 / 19:24 再超时）。
        #    现在输出预算卡在 32768，单次调用 150s 左右；900s 是失败场景的上界。
        #    另外 safe_call 在层级1 超时后会跳过 fallback，最坏只等一个超时。
        self.timeout = int(os.getenv("FMEA_LLM_TIMEOUT", "900"))
        # 推理模型思考预算耗尽标记（由 call_tool/call_complete 写入，safe_call 据此重试）
        self._reasoning_starved = False
        # 思考吃光预算时的重试上限
        self.reasoning_retry_max_tokens = int(os.getenv("FMEA_LLM_RETRY_MAX_TOKENS", "65536"))

    def _thinking_params(self) -> dict:
        """关闭思考链需要的请求参数（服务端不认时会是 400，见 400 处的提示）"""
        if not self.thinking_disabled:
            return {}
        return {"chat_template_kwargs": {"enable_thinking": False}}

    def _effective_max_tokens(self, input_tokens: int) -> int:
        """本次请求实际可用的输出预算

        `输入 tokens + max_tokens` 不能超过上下文窗口，否则服务端可能直接返回 400。
        只在真的会溢出时才下调（并打日志），平时返回配置值本身。
        """
        room = self.context_limit - int(input_tokens) - 1024
        if self.max_tokens > room:
            eff = max(room, 1024)
            msg = (f"[LLM] max_tokens={self.max_tokens} 会超上下文余量"
                   f"（context={self.context_limit}, 输入≈{input_tokens} tokens），"
                   f"本次下调为 {eff}")
            # 几百 token 的贴边上调不算异常（否则每次调用都刷 warning，掩盖真正的问题）
            if eff < self.max_tokens * 0.5:
                logger.warning(msg)
            else:
                logger.debug(msg)
            return eff
        return self.max_tokens

    async def call_tool(
        self,
        system_prompt: str,
        user_message: str,
        tools: list,
        tool_choice: str = "auto"
    ) -> Optional[str]:
        """层级1: 使用 tool calling 获取结构化输出"""
        import httpx

        self._reasoning_starved = False
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})

        input_tokens = estimate_tokens(system_prompt) + estimate_tokens(user_message)
        payload = {
            "model": self.model_id,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "max_tokens": self._effective_max_tokens(input_tokens),
            "temperature": 0.1,
        }
        payload.update(self._thinking_params())

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            # 先检查 HTTP 状态码，再解析 JSON
            if resp.status_code != 200:
                try:
                    error_data = resp.json()
                    error_detail = error_data.get("error", {}).get("message", str(error_data)[:300])
                except Exception:
                    error_detail = (await resp.aread())[:300].decode("utf-8", errors="replace") or f"HTTP {resp.status_code}"
                if self.thinking_disabled and resp.status_code == 400:
                    error_detail += ("  ← 若报错与 chat_template_kwargs / enable_thinking 有关，"
                                     "请在 .env 加 FMEA_LLM_THINKING=on 后重试")
                logger.error(
                    f"[LLM] call_tool HTTP {resp.status_code}: {error_detail[:500]}"
                )
                raise RuntimeError(f"LLM API returned {resp.status_code}: {error_detail[:200]}")

            data = resp.json()

            if "choices" not in data:
                logger.error(f"[LLM] call_tool unexpected response: {json.dumps(data, ensure_ascii=False)[:500]}")
                raise RuntimeError("LLM response missing 'choices' field")

            choice = data["choices"][0]
            msg = choice.get("message", {})
            finish = choice.get("finish_reason", "")
            usage = data.get("usage") or {}

            # Check for tool calls
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                func = tool_calls[0].get("function", {})
                args_str = func.get("arguments", "{}")
                logger.info(f"[LLM] tool_call 命中, arguments={len(args_str)} 字符, finish={finish!r}")
                return args_str

            # Check for direct content (some models return JSON directly)
            raw_content = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or ""
            content = _strip_think(raw_content)
            if content:
                logger.info(f"[LLM] tool_call 无 tool_calls，改用 content（{len(content)} 字符, finish={finish!r}）")
                return content

            # 空返回：把可诊断信息全部打出来，避免上层只看到一句"调用失败"
            self._reasoning_starved = bool(reasoning) and not raw_content
            hint = ""
            if finish == "length":
                hint = " ← 输出被 max_tokens 截断"
            if self._reasoning_starved:
                hint += " ← content 为空但存在 reasoning_content（思考内容吃光了输出预算，可调大 max_tokens）"
            logger.error(
                f"[LLM] tool_call 返回空内容: finish_reason={finish!r}, "
                f"content={len(raw_content)}字符, reasoning={len(reasoning)}字符, "
                f"max_tokens={self.max_tokens}, "
                f"usage={json.dumps(usage, ensure_ascii=False)}{hint}"
            )
            return None

    async def call_complete(
        self,
        system_prompt: str,
        user_message: str,
        force_json: bool = True
    ) -> Optional[str]:
        """层级2: 使用 complete 模式 + JSON 强约束"""
        import httpx

        full_system = system_prompt
        if force_json:
            full_system += "\n\n重要：你必须以纯JSON格式输出，不要加任何markdown代码块标记（不要```json```）。"

        messages = [
            {"role": "system", "content": full_system},
            {"role": "user", "content": user_message},
        ]

        input_tokens = estimate_tokens(system_prompt) + estimate_tokens(user_message)
        payload = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": self._effective_max_tokens(input_tokens),
            "temperature": 0.1,
        }
        payload.update(self._thinking_params())
        # 只有 force_json=True 时才启用 json_object 响应格式
        # 某些模型（如通义千问）可能不完全支持此参数，导致 400
        if force_json:
            payload["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            # 先检查 HTTP 状态码，再解析 JSON
            if resp.status_code != 200:
                try:
                    error_data = resp.json()
                    error_detail = error_data.get("error", {}).get("message", str(error_data)[:300])
                except Exception:
                    error_detail = (await resp.aread())[:300].decode("utf-8", errors="replace") or f"HTTP {resp.status_code}"
                if self.thinking_disabled and resp.status_code == 400:
                    error_detail += ("  ← 若报错与 chat_template_kwargs / enable_thinking 有关，"
                                     "请在 .env 加 FMEA_LLM_THINKING=on 后重试")
                logger.error(
                    f"[LLM] call_complete HTTP {resp.status_code}: {error_detail[:500]}"
                )
                raise RuntimeError(f"LLM API returned {resp.status_code}: {error_detail[:200]}")

            data = resp.json()

            if "choices" not in data:
                logger.error(f"[LLM] call_complete unexpected response: {json.dumps(data, ensure_ascii=False)[:500]}")
                raise RuntimeError("LLM response missing 'choices' field")

            choice = data["choices"][0]
            msg = choice.get("message", {})
            finish = choice.get("finish_reason", "")
            usage = data.get("usage") or {}
            raw_content = msg.get("content") or ""
            content = _strip_think(raw_content)
            if not content:
                reasoning = msg.get("reasoning_content") or ""
                self._reasoning_starved = bool(reasoning) and not raw_content
                logger.error(
                    f"[LLM] call_complete 返回空内容: finish_reason={finish!r}, "
                    f"content={len(raw_content)}字符, reasoning={len(reasoning)}字符, "
                    f"max_tokens={self.max_tokens}, "
                    f"usage={json.dumps(usage, ensure_ascii=False)}"
                )
            return content

    async def safe_call(
        self,
        system_prompt: str,
        user_message: str,
        tools: Optional[list] = None,
        probe_key: Optional[str] = None,
        fallback_to_complete: bool = True,
        retry_on_reasoning_starved: bool = True
    ) -> str:
        """
        安全 LLM 调用：三层 fallback

        Args:
            system_prompt: 系统提示词
            user_message: 用户消息
            tools: tool calling 工具定义（可选）
            probe_key: 探测关键字，用于判断 tool calling 结果是否有效
            fallback_to_complete: 是否在 tool call 失败后回退到 complete

        Returns:
            LLM 返回的原始文本

        Raises:
            RuntimeError: 三层调用均失败
        """
        import httpx

        in_chars = len(system_prompt or "") + len(user_message or "")
        in_tokens = estimate_tokens(system_prompt) + estimate_tokens(user_message)
        logger.info(
            f"[LLM] safe_call 输入 {in_chars} 字符 / 约 {in_tokens} tokens "
            f"(tools={'有' if tools else '无'}, max_tokens={self.max_tokens}, "
            f"thinking={'off' if self.thinking_disabled else 'on'}, timeout={self.timeout}s, "
            f"probe_key={probe_key!r})"
        )
        last_error = "未执行任何调用"
        timed_out = False

        def _fmt(e: Exception) -> str:
            # httpx.ReadTimeout 的 str() 是空串，必须带上类型名，否则日志里只剩"failed: "看不出原因
            msg = str(e).strip()
            return f"{type(e).__name__}: {msg}" if msg else type(e).__name__

        # 层级1: Tool Calling
        if tools:
            try:
                result = await self.call_tool(system_prompt, user_message, tools)
                if result and (probe_key is None or probe_key in result):
                    logger.info(f"[LLM] Tool call success, length={len(result)}")
                    return result
                logger.warning("[LLM] Tool call returned empty or invalid result")
                last_error = f"tool_call 返回内容无效（不含 {probe_key!r}）"
            except Exception as e:
                timed_out = isinstance(e, httpx.TimeoutException)
                logger.warning(f"[LLM] Tool call failed: {_fmt(e)}")
                last_error = f"tool_call 异常: {_fmt(e)}"

        # 层级2: Complete + JSON 强约束
        # 超时说明模型这次根本跑不完，换个调用方式大概率还是超时——
        # 直接跳过，别让一次失败的批次白等两个 timeout（之前 1800s×2 就是这么来的）。
        if fallback_to_complete and timed_out:
            logger.warning("[LLM] 层级1 超时，跳过 fallback（避免再等一个完整超时）")
            last_error += "；因超时跳过 fallback"
        elif fallback_to_complete:
            try:
                result = await self.call_complete(system_prompt, user_message, force_json=True)
                if result:
                    logger.info(f"[LLM] Fallback complete success, length={len(result)}")
                    return result
                logger.warning("[LLM] Fallback complete returned empty")
                last_error = "complete 返回空内容"
            except Exception as e:
                logger.warning(f"[LLM] Fallback complete failed: {_fmt(e)}")
                last_error = f"complete 异常: {_fmt(e)}"

        # 层级2.5: 推理模型思考吃光预算 → 抬高 max_tokens 重试一次
        #   qwen3 / DeepSeek-R1 这类推理模型把 reasoning_content 和正式输出算在同一个
        #   max_tokens 里，8192 时思考自己就能烧完，content 直接为空（finish_reason=length）。
        #   这不是模型故障，纯粹是预算不够——抬高预算重试即可，但只重试一次，避免拖长。
        if retry_on_reasoning_starved and self._reasoning_starved \
                and self.max_tokens < self.reasoning_retry_max_tokens:
            old_max = self.max_tokens
            self.max_tokens = self.reasoning_retry_max_tokens
            logger.warning(
                f"[LLM] 检测到思考内容耗尽 max_tokens({old_max})，"
                f"提升至 {self.max_tokens} 重试一次"
            )
            try:
                if tools:
                    result = await self.call_tool(system_prompt, user_message, tools)
                    if result and (probe_key is None or probe_key in result):
                        logger.info(f"[LLM] 抬高预算后 Tool call 成功, length={len(result)}")
                        return result
                    last_error = "抬高预算后 tool_call 仍无有效内容"
                if fallback_to_complete:
                    result = await self.call_complete(system_prompt, user_message, force_json=True)
                    if result:
                        logger.info(f"[LLM] 抬高预算后 complete 成功, length={len(result)}")
                        return result
                    last_error = "抬高预算后 complete 仍为空"
            except Exception as e:
                logger.warning(f"[LLM] 抬高预算重试失败: {e}")
                last_error = f"抬高预算重试异常: {e}"
            finally:
                self.max_tokens = old_max

        # 层级3: 抛出异常（带上真实原因，别让排查只能靠猜）
        raise RuntimeError(
            "LLM调用失败(toolCall+complete)，请检查LLM服务或重试；"
            f"最后原因: {last_error}（输入约 {in_tokens} tokens）"
        )

    def extract_json(self, text: str) -> dict:
        """从LLM输出中提取JSON对象（增强容错：处理截断、代码块、尾逗号、think标签等）"""
        if not text:
            raise ValueError("LLM输出为空")

        # 去除 DeepSeek-R1 等模型的 <think>...</think> 推理标签
        text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.DOTALL)

        # 去除markdown代码块
        text = re.sub(r"```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试修复常见问题：末尾被截断，补全括号
        for repair_func in [_repair_truncated_json, _noop]:
            try:
                repaired = repair_func(text)
                if repaired:
                    # 正则提取最外层对象
                    obj_match = re.search(r"\{.*\}", repaired, re.DOTALL)
                    if obj_match:
                        return json.loads(obj_match.group())
                    arr_match = re.search(r"\[.*\]", repaired, re.DOTALL)
                    if arr_match:
                        return json.loads(arr_match.group())
            except json.JSONDecodeError:
                continue

        # 最后尝试：找第一个 { 和最后一个 }，丢弃中间多余内容
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

        # 输出完整文本到日志辅助排查，exception message 保持精简以防溢栈
        logger.warning(
            "[LLM extract_json] 以下为完整LLM输出（供调试）:\n%s",
            text
        )
        raise ValueError(f"无法从LLM输出中提取JSON（前200字: {text[:200]}...）")


# 全局单例
_default_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client
