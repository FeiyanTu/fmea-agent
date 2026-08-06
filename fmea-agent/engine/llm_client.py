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


class LLMClient:
    """LLM 调用客户端，封装三层 fallback 机制"""

    def __init__(self, model_id: str = None,
                 base_url: str = None,
                 api_key: str = None,
                 max_tokens: int = 8192):
        import os
        self.model_id = model_id or os.getenv("FMEA_MODEL_NAME", "deepseek-chat")
        self.base_url = base_url or os.getenv("FMEA_MODEL_BASE_URL", "https://api.deepseek.com")
        self.api_key = api_key or os.getenv("FMEA_MODEL_API_KEY", os.getenv("DEEPSEEK_API_KEY", ""))
        self.max_tokens = max_tokens
        self.context_limit = 196608  # 模型上下文窗口

    async def call_tool(
        self,
        system_prompt: str,
        user_message: str,
        tools: list,
        tool_choice: str = "auto"
    ) -> Optional[str]:
        """层级1: 使用 tool calling 获取结构化输出"""
        import httpx

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": self.model_id,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "max_tokens": self.max_tokens,
            "temperature": 0.1,
        }

        async with httpx.AsyncClient(timeout=600) as client:
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

            # Check for tool calls
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                func = tool_calls[0].get("function", {})
                args_str = func.get("arguments", "{}")
                return args_str

            # Check for direct content (some models return JSON directly)
            content = msg.get("content", "")
            if content:
                return content

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

        payload = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": 0.1,
        }
        # 只有 force_json=True 时才启用 json_object 响应格式
        # 某些模型（如通义千问）可能不完全支持此参数，导致 400
        if force_json:
            payload["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=600) as client:
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
                logger.error(
                    f"[LLM] call_complete HTTP {resp.status_code}: {error_detail[:500]}"
                )
                raise RuntimeError(f"LLM API returned {resp.status_code}: {error_detail[:200]}")

            data = resp.json()

            if "choices" not in data:
                logger.error(f"[LLM] call_complete unexpected response: {json.dumps(data, ensure_ascii=False)[:500]}")
                raise RuntimeError("LLM response missing 'choices' field")

            return data["choices"][0]["message"]["content"]

    async def safe_call(
        self,
        system_prompt: str,
        user_message: str,
        tools: Optional[list] = None,
        probe_key: Optional[str] = None,
        fallback_to_complete: bool = True
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
        # 层级1: Tool Calling
        if tools:
            try:
                result = await self.call_tool(system_prompt, user_message, tools)
                if result and (probe_key is None or probe_key in result):
                    logger.info(f"[LLM] Tool call success, length={len(result)}")
                    return result
                logger.warning(f"[LLM] Tool call returned empty or invalid result")
            except Exception as e:
                logger.warning(f"[LLM] Tool call failed: {e}")

        # 层级2: Complete + JSON 强约束
        if fallback_to_complete:
            try:
                result = await self.call_complete(system_prompt, user_message, force_json=True)
                if result:
                    logger.info(f"[LLM] Fallback complete success, length={len(result)}")
                    return result
            except Exception as e:
                logger.warning(f"[LLM] Fallback complete failed: {e}")

        # 层级3: 抛出异常
        raise RuntimeError("LLM调用失败(toolCall+complete)，请检查LLM服务或重试")

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
