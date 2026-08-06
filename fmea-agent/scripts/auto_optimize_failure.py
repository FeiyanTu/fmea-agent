"""
⚠️ 命名澄清：本脚本与「阶段6 优化措施」(run_optimization_tool / step5_optimization) 无关！

本脚本是【失效分析质量自优化循环】（15 轮），用途：
每轮跑失效分析 → 3 个电池专家 LLM 独立评分 → 评分 < 85 则自动修改
FAILURE_ANALYSIS_V4_SYSTEM prompt → 下一轮重跑，直至达标或轮次耗尽。

真正的"优化措施"实现在：
- engine/tools/optimization_tools.py (run_optimization_tool)
- agents/step5_optimization.py (OptimizationAgent)

每轮：
1. 跑失效分析 → 获取结果
2. 3 个电池专家 LLM 独立评分
3. 评分 < 85 → 聚合改进建议 → 修改 prompt → 下一轮
4. 评分 >= 85 或 15 轮后 → 输出最佳结果
"""
import asyncio, json, logging, os, re, sys, time, traceback
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("auto_optimize")

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from dotenv import load_dotenv
load_dotenv(str(PROJECT_DIR / ".env"))

import psycopg2, httpx
from openai import OpenAI

HOST = os.getenv("URL_AGENT_DB_HOST", "10.180.17.11")
PORT = int(os.getenv("URL_AGENT_DB_PORT", "5433"))
DB = os.getenv("URL_AGENT_DB_NAME", "fmea")
USER = os.getenv("URL_AGENT_DB_USER", "postgres")
PWD = os.getenv("URL_AGENT_DB_PASSWORD", "postgres123!@#")

LLM_BASE = os.getenv("MODEL_BASE_URL", "http://10.180.17.11:3000/v1")
LLM_KEY = os.getenv("MODEL_API_KEY", "ollama")
LLM_MODEL = os.getenv("MODEL_NAME", "deepseek-v4")

NUM_ROUNDS = 15
PASS_SCORE = 85
FAILURE_PROMPT_PATH = PROJECT_DIR / "engine" / "prompts" / "__init__.py"
AGENT_PROMPT_PATH = PROJECT_DIR / "orchestrator" / "failure_multi_agent_runner.py"

# ─── 数据读取 ───

def get_baseline(db_pid: int, step_name: str) -> dict:
    conn = psycopg2.connect(host=HOST, port=PORT, dbname=DB, user=USER, password=PWD)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM fmea_step_assignee_data "
                "WHERE project_id = %s AND step_name = %s AND assignee_user_id = -1",
                (db_pid, step_name),
            )
            row = cur.fetchone()
            return row[0] if row else {}
    finally:
        conn.close()


def get_db_pid(project_id: str) -> int:
    conn = psycopg2.connect(host=HOST, port=PORT, dbname=DB, user=USER, password=PWD)
    try:
        with conn.cursor() as cur:
            try:
                pid = int(project_id)
                cur.execute("SELECT id FROM projects WHERE id = %s", (pid,))
                row = cur.fetchone()
                if row: return row[0]
            except ValueError: pass
            cur.execute("SELECT id FROM projects WHERE name = %s", (project_id,))
            row = cur.fetchone()
            return row[0] if row else 0
    finally:
        conn.close()


# ─── 失效分析运行 ───

async def run_failure_analysis() -> dict:
    """返回失效分析统计"""
    from models.structure import StructureTree
    from models.function import FunctionAnalysis
    from orchestrator.failure_multi_agent_runner import FailureMultiAgentRunner

    db_pid = get_db_pid("15")
    tree_data = get_baseline(db_pid, "structure")
    func_data = get_baseline(db_pid, "function")

    tree = StructureTree(**tree_data)
    func_analysis = FunctionAnalysis(**func_data)

    runner = FailureMultiAgentRunner()
    analysis = await runner.run(tree, func_analysis, str(db_pid))
    return analysis.model_dump()


# ─── 3 个专家评分器 ───

EXPERT_PROFILES = {
    "electrical_bms": {
        "name": "BMS 与电气系统专家",
        "background": "8 年 BMS 软硬件开发经验，ISO 26262 功能安全工程师，精通 BMS 电气失效分析",
        "focus": "BMS 电气失效覆盖度、软件/硬件失效链合理性、S 评分准确性",
    },
    "cell_thermal": {
        "name": "电池系统安全专家",
        "background": "10 年动力电池 DFMEA 经验，精通电芯/模组/Pack 级失效分析",
        "focus": "热失控链完整性、高压安全覆盖度、机械安全、FC 设计根源限定",
    },
    "mechanical_structure": {
        "name": "制造工艺与结构专家",
        "background": "12 年电池包结构设计和制造经验，精通 Pack 箱体/密封/连接/工艺",
        "focus": "结构失效覆盖面、工艺相关性、功能→失效映射合理性",
    },
}


async def score_with_expert(expert_key: str, analysis_data: dict) -> dict:
    """一个专家 Agent 对失效分析结果评分"""
    expert = EXPERT_PROFILES[expert_key]
    items = analysis_data.get("items", [])
    
    # 构建分析摘要（取前 30 条 + 统计）
    total = len(items)
    high_sev = sum(1 for i in items if (i.get("severity") or 0) >= 7)
    safety = sum(1 for i in items if i.get("failure_network_type") == "安全功能失效")
    fc_reasons = [i.get("failure_cause", "") for i in items[:5]]
    
    sample = json.dumps(items[:10], ensure_ascii=False, indent=1)[:3000]

    prompt = f"""你是{expert['name']}，{expert['background']}。

请从你的专业角度对以下 DFMEA 失效分析结果进行评分。

## 评分维度
1. 功能→失效映射合理性 (0-100): 每条功能对应的失效链是否逻辑自洽
2. 领域知识完整性 (0-100): 从{expert['focus']}角度看，遗漏了多少该行业公认的失效模式
3. 严重度 S 评分准确性 (0-100): 基于 AIAG-VDA 标准
4. 失效原因设计根源限定 (0-100): FC 是否严格限定为设计缺陷

## 统计
- 总失效链数: {total}
- 高严重度(S≥7): {high_sev}
- 安全功能失效: {safety}
- FC 样本: {json.dumps(fc_reasons, ensure_ascii=False)}

## 前 10 条失效链样本
{sample}

## 输出格式 (严格 JSON，不要多余文字)
{{
  "score_map": {{"function_failure_logic": 85, "domain_coverage": 80, "severity_accuracy": 75, "root_cause_discipline": 90}},
  "average_score": 82.5,
  "strengths": ["xxx"],
  "weaknesses": ["xxx"]
}}"""
    
    client = OpenAI(base_url=LLM_BASE, api_key=LLM_KEY)
    resp = client.chat.completions.create(
        model=LLM_MODEL, messages=[{"role": "user", "content": prompt}],
        temperature=0.3, max_tokens=2000,
    )
    text = resp.choices[0].message.content
    # 提取 JSON
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        logger.warning(f"专家评分 JSON 解析失败，尝试修复...")
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            return json.loads(text[brace_start:brace_end+1])
        return {"error": f"JSON parse failed", "raw": text[:500]}


# ─── Prompt 改进 ───

async def improve_prompt(scores: list, weaknesses: list, round_num: int):
    """用 LLM 生成 prompt 改进方案"""
    weakness_text = "\n".join(f"- {w}" for w in weaknesses[:5])
    avg_score = sum(s.get("average_score", 0) for s in scores) / len(scores)

    client = OpenAI(base_url=LLM_BASE, api_key=LLM_KEY)
    prompt_text = f"""你是一个 DFMEA 失效分析专家。当前失效分析质量评分为 {avg_score:.1f}/100。

需要改进的问题：
{weakness_text}

请给出 FAILURE_ANALYSIS_V4_SYSTEM 系统提示词的改进建议。改进应该针对具体的评分缺陷。

输出格式：
IMPROVEMENT: <具体的 prompt 段落修改>
REASON: <为什么这个修改能提升评分>
LOCATION: <在 prompt 的哪个部分修改>"""

    resp = client.chat.completions.create(
        model=LLM_MODEL, messages=[{"role": "user", "content": prompt_text}],
        temperature=0.4, max_tokens=3000,
    )
    return resp.choices[0].message.content


def patch_failure_prompt(improvement: str):
    """修改 FAILURE_ANALYSIS_V4_SYSTEM prompt"""
    # 提取 IMPROVEMENT 段落
    m = re.search(r"IMPROVEMENT:\s*(.+?)(?=REASON:|$)", improvement, re.DOTALL)
    if not m:
        logger.warning("无法提取改进内容")
        return False
    new_content = m.group(1).strip()

    # 读当前 FAILURE_ANALYSIS_V4_SYSTEM
    with open(FAILURE_PROMPT_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    # 在 FAILURE_ANALYSIS_V4_SYSTEM 末尾追加一段改进
    marker = 'FAILURE_ANALYSIS_V4_SYSTEM = """'
    idx = content.find(marker)
    if idx < 0:
        logger.warning("无法找到 FAILURE_ANALYSIS_V4_SYSTEM")
        return False

    # 找到结尾
    end_marker = '"""'
    end_idx = content.find(end_marker, idx + len(marker))
    if end_idx < 0:
        return False

    # 在 prompt 内容最后插入改进
    prompt_end = content.rfind('"""', 0, end_idx)
    # 在内容末尾注入
    insert_point = content.rfind('""",', idx, content.find('""";', idx))
    if insert_point < 0:
        insert_point = end_idx

    # 在 """ 前插入改进段落
    patch = f"\n\n### 改进 {len(new_content[:50])}\n{new_content}\n"
    new_content_text = content[:end_idx] + patch + content[end_idx:]

    with open(FAILURE_PROMPT_PATH, "w", encoding="utf-8") as f:
        f.write(new_content_text)
    logger.info(f"✅ Prompt 已改进 (追加 {len(patch)} 字符)")
    return True


# ─── 主循环 ───

async def main():
    logger.info("=" * 60)
    logger.info("失效分析质量优化循环 — 启动")
    logger.info(f"轮次: {NUM_ROUNDS}  达标分: {PASS_SCORE}")
    logger.info("=" * 60)

    scores_history = []
    best_score = 0
    best_result = None

    for round_num in range(1, NUM_ROUNDS + 1):
        logger.info(f"\n{'='*50}")
        logger.info(f"第 {round_num}/{NUM_ROUNDS} 轮")
        logger.info(f"{'='*50}")

        # 1. 跑失效分析
        logger.info("运行失效分析...")
        t0 = time.time()
        try:
            analysis = await run_failure_analysis()
        except Exception as e:
            logger.error(f"分析失败: {e}")
            traceback.print_exc()
            continue
        elapsed = time.time() - t0
        items = analysis.get("items", [])
        logger.info(f"完成: {len(items)} 条失效链, 耗时 {elapsed:.0f}s")

        # 2. 3 个专家评分
        logger.info("3 个专家评分中...")
        scores = {}
        for expert_key in EXPERT_PROFILES:
            try:
                score = await score_with_expert(expert_key, analysis)
                scores[expert_key] = score
                avg = score.get("average_score", 0)
                logger.info(f"  {EXPERT_PROFILES[expert_key]['name']}: {avg:.1f}")
            except Exception as e:
                logger.warning(f"  {expert_key} 评分失败: {e}")
                scores[expert_key] = {"average_score": 0, "weaknesses": [str(e)]}

        avg_score = sum(
            s.get("average_score", 0) for s in scores.values()
        ) / len(scores)
        scores_history.append({"round": round_num, "avg_score": avg_score, "scores": scores})
        logger.info(f"  平均分: {avg_score:.1f}/100")

        if avg_score > best_score:
            best_score = avg_score
            best_result = analysis
            logger.info(f"  🏆 新最佳! 保存此版本")

        # 3. 达标检查
        if avg_score >= PASS_SCORE:
            logger.info(f"\n{'='*50}")
            logger.info(f"🎉 第 {round_num} 轮达标! 分数: {avg_score:.1f}")
            logger.info(f"{'='*50}")
            break

        # 4. 聚合改进建议
        weaknesses = []
        for s in scores.values():
            weaknesses.extend(s.get("weaknesses", []))
        weaknesses = list(set(weaknesses))[:5]

        if not weaknesses:
            logger.warning("无改进建议，随机调整 prompt")
            improvement = f"IMPROVEMENT: 加强失效原因的设计缺陷限定，在 prompt 中强调 FC 必须仅限设计层面。REASON: 当前部分 FC 混入制程因素。LOCATION: 失效原因章节"
        else:
            improvement = await improve_prompt(list(scores.values()), weaknesses, round_num)

        logger.info(f"改进建议: {improvement[:300]}...")
        patched = patch_failure_prompt(improvement)
        if patched:
            logger.info("Prompt 已更新，等待 server reload...")
            await asyncio.sleep(3)

    # 总结
    logger.info(f"\n{'='*60}")
    logger.info(f"优化完成!")
    logger.info(f"最佳分数: {best_score:.1f}/100 (第 {best_result} 轮)")
    avg_hist = [f'{s["avg_score"]:.1f}' for s in scores_history]
    logger.info(f"分数历史: {avg_hist}")
    logger.info(f"{'='*60}")

    # 保存最佳结果
    if best_result:
        out_path = PROJECT_DIR / "logs" / "best_failure_analysis.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(best_result, f, ensure_ascii=False, indent=2)
        logger.info(f"最佳版本已保存: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
