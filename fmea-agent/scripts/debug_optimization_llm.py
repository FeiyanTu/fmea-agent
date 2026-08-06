"""
调试脚本：优化措施 LLM 调用 — 查看模型到底返回了什么

背景：run_optimization_tool 一直报
  [LLM-Opt] 批次失败: Expecting value: line 1 column 1 (char 0)
  [LLM-Opt] 批次失败: Expecting value: line 3 column 1 (char 2)
即 optimization_tools._optimize_single_batch 中裸 json.loads(result) 解析失败。

本脚本用与 _optimize_single_batch 完全相同的 system prompt / tools / user message，
分三层查看：
  ① 底层 HTTP 完整响应（message 原始内容 + tool_calls）—— 模型到底返回了什么
  ② llm.call_tool 的返回值（复现线上路径）
  ③ 裸 json.loads vs llm.extract_json —— 验证修复方向

用法: python scripts/debug_optimization_llm.py [project_id] [条数]
"""
import asyncio
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from dotenv import load_dotenv
load_dotenv(str(PROJECT_DIR / ".env"))

from engine.llm_client import get_llm_client
from engine.tools.optimization_tools import _OPTIMIZATION_SYSTEM_PROMPT


def build_tools() -> list:
    """与 _optimize_single_batch 完全一致的工具定义"""
    return [{
        "type": "function",
        "function": {
            "name": "suggest_optimization",
            "description": "为高风险失效提出优化措施建议",
            "parameters": {
                "type": "object",
                "properties": {
                    "optimizationItems": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "failureId": {"type": "string"},
                                "preventiveAction": {"type": "string"},
                                "detectiveAction": {"type": "string"},
                                "category": {"type": "string", "enum": ["short_term", "next_gen"]},
                                "cost_impact": {"type": "string", "enum": ["high", "medium", "low"]},
                                "maintenance_cost": {"type": "string", "enum": ["high", "medium", "low"]},
                                "design_change_type": {"type": "string", "enum": ["hardware_revision", "software_logic", "structure_material", "cell_upgrade", "sor_correction"]},
                                "targetDate": {"type": "string"},
                                "remark": {"type": "string"},
                            },
                            "required": ["failureId", "preventiveAction", "detectiveAction"],
                        }
                    }
                },
                "required": ["optimizationItems"],
            },
        }
    }]


async def main(project_id: str = "15", limit: int = 2):
    llm = get_llm_client()
    print("=" * 70)
    print(f"模型配置: model={llm.model_id}  base_url={llm.base_url}")
    print(f"测试数据: project={project_id} 取前 {limit} 条高风险项")
    print("=" * 70)

    # ── 取风险数据，构造与线上一致的输入 ──
    risk_path = PROJECT_DIR / "data" / "projects" / project_id / "fmea" / "04-risk.json"
    with open(risk_path, "r", encoding="utf-8") as f:
        risk_data = json.load(f)
    batch = risk_data.get("items", [])[:limit]
    if not batch:
        print("无风险数据")
        return

    items_text = []
    for item in batch:
        items_text.append(
            f"失效链 #{item.get('failure_id', '')}\n"
            f"  失效模式(FM): {item.get('failure_mode', '')}\n"
            f"  严重度 S={item.get('severity', 0)}, 发生度 O={item.get('occurrence', 0)}, "
            f"探测度 D={item.get('detection', 0)}\n"
            f"  RPN={item.get('rpn', 0)}, AP={item.get('ap', '')}\n"
            f"  当前PC: {item.get('prevention_control', '')}\n"
            f"  当前DC: {item.get('detection_control', '')}\n"
        )
    user_message = "请为以下高风险失效项提出优化措施建议：\n\n" + "\n---\n".join(items_text)
    tools = build_tools()

    # ════════ ① 底层 HTTP 完整响应 ════════
    print("\n① 底层 HTTP 完整响应（模型原始输出）:")
    import httpx
    payload = {
        "model": llm.model_id,
        "messages": [
            {"role": "system", "content": _OPTIMIZATION_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": llm.max_tokens,
        "temperature": 0.1,
    }
    async with httpx.AsyncClient(timeout=600) as client:
        resp = await client.post(
            f"{llm.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {llm.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        print(f"  HTTP 状态: {resp.status_code}")
        if resp.status_code != 200:
            print(f"  错误响应: {resp.text[:500]}")
            return
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        print(f"  finish_reason: {choice.get('finish_reason')}")
        print(f"  message 顶层键: {list(msg.keys())}")

        tool_calls = msg.get("tool_calls")
        if tool_calls:
            print(f"  tool_calls 数量: {len(tool_calls)}")
            for i, tc in enumerate(tool_calls):
                print(f"    [{i}] name={tc.get('function', {}).get('name')}")
                print(f"        arguments 原始内容 (repr):")
                print(f"        {repr(tc.get('function', {}).get('arguments', ''))[:1500]}")
        else:
            print("  tool_calls: 无（模型未走工具调用）")

        content = msg.get("content") or ""
        if content:
            print(f"  content 原始内容 (repr 前 3000):")
            print("  " + repr(content[:3000]))
        else:
            print("  content: 空")

        # ════════ ② 复现 call_tool 返回值 ════════
        print("\n② llm.call_tool 返回值 (repr 前 3000):")
        try:
            result = await llm.call_tool(_OPTIMIZATION_SYSTEM_PROMPT, user_message, tools)
            print("  " + repr(result[:3000] if result else result))
        except Exception as e:
            print(f"  call_tool 抛出异常: {type(e).__name__}: {e}")

        # ════════ ③ 解析对比 ════════
        if 'result' in dir() and result:
            print("\n③ 解析对比（模型返回内容能否被解析）:")
            try:
                d = json.loads(result)
                print("  裸 json.loads: ✅ 成功 →", list(d.keys()) if isinstance(d, dict) else type(d))
            except Exception as e:
                print(f"  裸 json.loads: ❌ 失败 → {e}")
            try:
                d2 = llm.extract_json(result)
                print(f"  llm.extract_json: ✅ 成功 → keys={list(d2.keys()) if isinstance(d2, dict) else type(d2)}")
            except Exception as e:
                print(f"  llm.extract_json: ❌ 失败 → {e}")

    print("\n" + "=" * 70)
    print("结论: 若 ③ 中 extract_json 成功而 json.loads 失败，则修复方向 =")
    print("把 _optimize_single_batch 的裸 json.loads 换成 llm.extract_json（或先剥 think/代码块）。")
    print("=" * 70)


if __name__ == "__main__":
    pid = sys.argv[1] if len(sys.argv) > 1 else "15"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    asyncio.run(main(pid, n))
