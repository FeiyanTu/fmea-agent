"""
LLM 数据查询工具 — 允许用户用自然语言查询项目数据库

通过 LLM 将自然语言转换为 SQL，执行只读查询，返回格式化结果。
双重安全校验：LLM prompt 约束 + 代码层正则校验。
"""

import json
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════
# 数据库 Schema（喂给 LLM，让它能准确生成 SQL）
# ════════════════════════════════════════════════════════════════

DB_SCHEMA = """
你有一个 PostgreSQL 数据库（数据库名: fmea），包含以下表：

=============================================================================
-- 1. 项目表
CREATE TABLE projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,          -- 项目名称
    description TEXT,                   -- 项目描述
    created_at TIMESTAMP,               -- 创建时间
    updated_at TIMESTAMP                -- 最后更新时间
);

-- 2. 用户表
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,       -- 用户名
    display_name TEXT,                   -- 显示名称/真实姓名
    email TEXT,                          -- 邮箱
    dept TEXT,                           -- 部门
    role_code TEXT,                      -- 角色编码
    active BOOLEAN DEFAULT TRUE          -- 是否活跃
);

-- 3. 项目-成员关联表
CREATE TABLE project_members (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),  -- 关联项目
    user_id INTEGER REFERENCES users(id),         -- 关联用户
    role_code TEXT,                                -- 角色: owner/leader/engineer
    joined_at TIMESTAMP                            -- 加入时间
);

-- 4. FMEA 步骤数据表 ⭐核心表⭐
CREATE TABLE fmea_step_assignee_data (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),   -- 关联项目
    step_name TEXT,                    -- 步骤: structure / function / failure / risk / optimization
    assignee_user_id INTEGER,          -- -1=系统基线, >0=工程师个人行
    data JSONB,                        -- JSON 格式的步骤完整数据
    version INTEGER DEFAULT 0          -- 乐观锁版本号
);
-- 唯一约束: (project_id, step_name, assignee_user_id)

-- fmea_step_assignee_data.data 字段中各步骤的 JSON 结构：
--   structure   → {"nodes": [{"id": "N001", "name": "电池包", "level": 1, "parent_id": null}, ...]}
--   function    → {"items": [{"node_id": "N001", "functions": [{"function_id":"F001","description":"供电","type":"主要功能"},...]}, ...], "function_network": {...}}
--   failure     → {"items": [{"id": "...", "failure_mode": "短路", "failure_effect": "电池失效", "failure_cause": "隔膜破损", "severity": 8, ...}, ...]}
--   risk        → {"items": [{"failure_id": "...", "severity": 8, "occurrence": 3, "detection": 5, "rpn": 120, "ap": "H", ...}, ...]}
--   optimization → {"items": [{"failure_id": "...", "preventive_action": "加强隔膜检测", "detective_action": "增加在线监测", ...}, ...]}

-- 5. FMEA 审批表
CREATE TABLE fmea_approvals (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    user_id INTEGER REFERENCES users(id),
    stage_no INTEGER,                   -- 阶段编号(1-7)
    status TEXT,                        -- pending / approved / rejected
    lead_status TEXT,                   -- 组长审批状态
    approved_at TIMESTAMP               -- 审批时间
);

-- 6. 风险评分标准表
CREATE TABLE fmea_risk_criteria (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    criteria JSONB,                     -- SOD 评分标准 JSON
    version INTEGER DEFAULT 0
);

-- 7. 步骤报告表
CREATE TABLE fmea_step_data (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    step_name TEXT,                     -- report
    data JSONB,                         -- 报告 JSON
    version INTEGER DEFAULT 0
);

=============================================================================
重要提示：
1. ⚠️ 只生成 SELECT 语句！禁止 INSERT/UPDATE/DELETE/DROP/ALTER/CREATE 等任何写操作
2. JSONB 字段操作: data->'key' 返回 JSON 对象, data->>'key' 返回文本值
3. JSONB 数组长度: jsonb_array_length(data->'items') 统计 items 数组元素个数
4. JSONB 数组展开: jsonb_array_elements(data->'items') AS item, 然后 item->>'field'
5. 查询项目进展: 检查 fmea_step_assignee_data 中某 project_id 有哪些 step_name
6. 模糊匹配项目名: projects.name LIKE '%关键词%' 或 projects.name ILIKE '%关键词%' (不区分大小写)
7. 最多返回 50 行: LIMIT 50
8. 统计类查询用 COUNT(), AVG(), MAX(), MIN() 等聚合函数
9. 回答要基于数据库实际数据，不要编造
"""

# ════════════════════════════════════════════════════════════════
# 通用查询示例（注入 LLM prompt 帮助泛化）
# ════════════════════════════════════════════════════════════════

QUERY_EXAMPLES = """
常见查询示例参考：

Q: "有哪些项目？"
SQL: SELECT id, name, description, created_at, updated_at FROM projects ORDER BY updated_at DESC;

Q: "XX项目的FMEA进展到哪一步了？"
SQL: SELECT DISTINCT step_name FROM fmea_step_assignee_data WHERE project_id = (SELECT id FROM projects WHERE name ILIKE '%XX%') AND assignee_user_id = -1 ORDER BY step_name;

Q: "XX项目各步骤完成情况"
SQL: SELECT step_name, jsonb_array_length(data->'items') AS item_count
FROM fmea_step_assignee_data WHERE project_id = (SELECT id FROM projects WHERE name ILIKE '%XX%') AND assignee_user_id = -1;

Q: "XX项目结构树有多少个节点？"
SQL: SELECT jsonb_array_length(data->'nodes') AS node_count
FROM fmea_step_assignee_data WHERE project_id = (SELECT id FROM projects WHERE name ILIKE '%XX%') AND step_name = 'structure' AND assignee_user_id = -1;

Q: "XX项目有多少个失效模式？"
SQL: SELECT jsonb_array_length(data->'items') AS failure_count
FROM fmea_step_assignee_data WHERE project_id = (SELECT id FROM projects WHERE name ILIKE '%XX%') AND step_name = 'failure' AND assignee_user_id = -1;

Q: "所有项目的成员分布情况"
SQL: SELECT p.name AS project, u.display_name AS member, pm.role_code AS role
FROM project_members pm JOIN projects p ON pm.project_id = p.id JOIN users u ON pm.user_id = u.id ORDER BY p.name;

Q: "XX项目RPN最高的5个失效模式"
SQL: SELECT item->>'failure_mode' AS mode, (item->>'rpn')::int AS rpn
FROM fmea_step_assignee_data, jsonb_array_elements(data->'items') AS item
WHERE project_id = (SELECT id FROM projects WHERE name ILIKE '%XX%') AND step_name = 'risk' AND assignee_user_id = -1
ORDER BY rpn DESC LIMIT 5;

Q: "AP等级为H的高风险失效模式"
SQL: SELECT item->>'failure_mode' AS mode, item->>'severity' AS s, item->>'occurrence' AS o, item->>'detection' AS d, item->>'ap' AS ap
FROM fmea_step_assignee_data, jsonb_array_elements(data->'items') AS item
WHERE step_name = 'risk' AND assignee_user_id = -1 AND item->>'ap' = 'H'
ORDER BY (item->>'rpn')::int DESC LIMIT 50;

Q: "XX项目成员的审批状态"
SQL: SELECT p.name, u.display_name, fa.stage_no, fa.status, fa.lead_status, fa.approved_at
FROM fmea_approvals fa JOIN projects p ON fa.project_id = p.id JOIN users u ON fa.user_id = u.id
WHERE p.name ILIKE '%XX%' ORDER BY fa.stage_no;
"""

# ════════════════════════════════════════════════════════════════
# SQL 生成 Prompt 模板
# ════════════════════════════════════════════════════════════════

SQL_GENERATION_USER_TEMPLATE = """请根据以下用户问题生成一条 PostgreSQL SELECT 查询。

用户问题: {question}

{project_context}

规则：
1. 只生成 SELECT 语句，绝对不要生成任何其他类型的 SQL
2. 返回纯 SQL 文本，不要加 markdown 标记、代码块或任何解释
3. SQL 必须完整可执行，字段名和表名必须与 schema 一致
4. 查询结果不超过 50 行，添加 LIMIT
5. 如果用户问题超出数据库范围或无法回答，回复: UNCLEAR
6. 涉及 JSONB 字段时使用 PostgreSQL 标准操作符
"""


# ════════════════════════════════════════════════════════════════
# 工具主函数
# ════════════════════════════════════════════════════════════════

async def query_project_data_tool(question: str, project_id: str = "") -> str:
    """用自然语言查询 FMEA 项目数据库。支持查询项目列表、项目进展、FMEA 各步骤统计、成员信息、风险数据等。

    适用场景：
    - 查询项目信息："有哪些项目？"、"XX项目是什么时候创建的？"
    - 查询项目进展："XX项目进行到哪一步了？"、"哪些步骤完成了？"
    - 查询统计数据："XX项目有多少失效模式？"、"RPN最高的5项是什么？"
    - 查询人员信息："XX项目有哪些成员？"、"谁是项目经理？"
    - 查询风险数据："AP为H的有哪些？"、"严重度>=8的失效模式？"

    参数:
        question: 自然语言查询问题（必填）
        project_id: 可选的项目标识(项目ID或名称关键词)，用于限定查询范围

    返回:
        JSON 字符串，包含 success, question, sql, row_count, rows 等字段
    """
    import os

    # ── 获取 LLM 配置 ──
    model_name = os.getenv("FMEA_MODEL_NAME", "deepseek-chat")
    base_url = os.getenv("FMEA_MODEL_BASE_URL", "https://api.deepseek.com")
    api_key = os.getenv("FMEA_MODEL_API_KEY", os.getenv("DEEPSEEK_API_KEY", ""))

    if not api_key:
        return json.dumps({"success": False, "error": "LLM API Key 未配置，请检查 .env 文件"}, ensure_ascii=False)

    try:
        # ═══ 步骤1: 构建 prompt → LLM 生成 SQL ═══
        project_context = ""
        if project_id:
            # 尝试将 project_id 解析为数据库项目，提供准确的上下文
            resolved = _resolve_project_name(project_id)
            if resolved:
                project_context = f"当前上下文项目: id={resolved['id']}, name={resolved['name']}"
            else:
                project_context = f"用户提到的项目(数据库可能不存在): {project_id}"

        user_prompt = SQL_GENERATION_USER_TEMPLATE.format(
            question=question,
            project_context=project_context
        )

        sql = await _call_llm(model_name, base_url, api_key, DB_SCHEMA + QUERY_EXAMPLES, user_prompt)

        if not sql or sql.strip().upper() == "UNCLEAR":
            return json.dumps({
                "success": False,
                "error": "无法将问题转换为数据库查询。建议尝试更具体的问题，如：\n"
                         "- '有哪些项目？'\n"
                         "- '项目进展如何？'\n"
                         "- 'XX项目的失效模式有多少？'"
            }, ensure_ascii=False)

        # 清理 LLM 输出的 SQL
        sql = _clean_sql(sql)
        logger.info(f"[query_tool] ▶ step1 SQL生成成功: question='{question[:60]}'")
        logger.info(f"[query_tool]   SQL: {sql}")

        # ═══ 步骤2: 代码层安全检查 ═══
        is_safe, reason = _safety_check_sql(sql)
        if not is_safe:
            logger.warning(f"[query_tool] ✗ 安全检查不通过: {reason} | SQL: {sql[:200]}")
            return json.dumps({
                "success": False,
                "error": f"生成的 SQL 未通过安全检查: {reason}",
                "sql": sql
            }, ensure_ascii=False)
        logger.info(f"[query_tool] ▶ step2 安全检查通过")

        # ═══ 步骤3: 执行 SQL ═══
        from main import get_user_db_conn

        logger.info(f"[query_tool] ▶ step3 开始执行 SQL ...")
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [desc[0] for desc in cur.description] if cur.description else []
                rows = cur.fetchall()
        logger.info(f"[query_tool] ✓ step3 SQL 执行完成: columns={columns}, rows={len(rows)}")

        # ═══ 步骤4: 格式化结果 ═══
        formatted_rows = _format_rows(columns, rows)

        # 生成 markdown 表格（让 LLM 可以直接复制到回复中）
        markdown_table = _rows_to_markdown(columns, formatted_rows)

        # 生成简短摘要（让 LLM 知道这是查询结果的总结）
        if len(formatted_rows) == 0:
            summary = "查询未返回任何数据。"
        elif len(formatted_rows) == 1 and len(columns) <= 2:
            # 单条结果，直接拼出值
            row = formatted_rows[0]
            summary = " | ".join(f"{col} = {row[col]}" for col in columns)
        else:
            summary = f"共返回 {len(formatted_rows)} 行，{len(columns)} 列。"

        result = {
            "success": True,
            "question": question,
            "sql": sql,
            "row_count": len(formatted_rows),
            "columns": columns,
            "rows": formatted_rows,
            "summary": summary,
            "markdown_table": markdown_table,
        }

        logger.info(f"[query_tool] ✅ 全部完成: row_count={len(formatted_rows)}, summary='{summary[:80]}'")
        return json.dumps(result, ensure_ascii=False, default=str)

    except Exception as e:
        logger.error(f"[query_tool] 查询失败: {e}", exc_info=True)
        return json.dumps({
            "success": False,
            "error": f"查询执行失败: {str(e)}",
            "question": question,
        }, ensure_ascii=False)


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _call_llm(model_name: str, base_url: str, api_key: str,
                    system_prompt: str, user_message: str) -> Optional[str]:
    """调用 LLM API 获取 SQL 生成结果"""
    import httpx

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    payload = {
        "model": model_name,
        "messages": messages,
        "max_tokens": 2048,
        "temperature": 0.1,       # 低温度，让输出更确定
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

        if resp.status_code != 200:
            error_detail = f"HTTP {resp.status_code}"
            try:
                error_data = resp.json()
                error_detail = error_data.get("error", {}).get("message", error_detail)
            except Exception:
                pass
            raise RuntimeError(f"LLM API 调用失败: {error_detail}")

        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return content.strip() if content else None


def _clean_sql(sql: str) -> str:
    """清理 LLM 返回的 SQL 文本（去掉 markdown 包装和多余空白）"""
    # 去掉 markdown 代码块
    sql = re.sub(r'```(?:sql|SQL)?\s*\n?', '', sql)
    sql = re.sub(r'\n?```\s*$', '', sql)
    # 去掉首尾空白和末尾分号
    sql = sql.strip().rstrip(';').strip()
    return sql


def _safety_check_sql(sql: str) -> tuple:
    """SQL 安全校验，返回 (is_safe: bool, reason: str)"""
    sql_upper = sql.upper().strip()

    # ── 1. 禁止的关键词（单词边界匹配，避免误杀 SELECT 中的子串）──
    FORBIDDEN_KEYWORDS = [
        'INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE',
        'TRUNCATE', 'GRANT', 'REVOKE', 'COPY', 'EXECUTE', 'EXEC',
        'CALL', 'LOAD', 'IMPORT', 'REPLACE', 'MERGE',
        'VACUUM', 'REINDEX', 'CLUSTER', 'DISCARD', 'LISTEN', 'NOTIFY',
        'UNLISTEN', 'MOVE', 'FETCH', 'CLOSE', 'DECLARE', 'PREPARE',
        'DEALLOCATE', 'REASSIGN', 'REFRESH',
    ]
    for kw in FORBIDDEN_KEYWORDS:
        if re.search(r'\b' + kw + r'\b', sql_upper):
            return False, f"包含禁止的关键词: {kw}"

    # ── 2. 多语句检查 ──
    # 忽略字符串内的分号（简单处理：检查是否有不在引号内的分号）
    in_single = False
    in_double = False
    for i, ch in enumerate(sql):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ';' and not in_single and not in_double:
            return False, "包含多条语句（分号）"

    # ── 3. 必须以 SELECT / WITH / EXPLAIN 开头 ──
    if not re.match(r'^\s*(SELECT|WITH|EXPLAIN)\b', sql_upper):
        return False, "不是以 SELECT / WITH / EXPLAIN 开头"

    # ── 4. 禁止访问系统表 ──
    if re.search(r'\bpg_\w+', sql_upper):
        return False, "禁止访问 PostgreSQL 系统表(pg_*)"
    if re.search(r'\binformation_schema\b', sql_upper, re.IGNORECASE):
        return False, "禁止访问 information_schema"

    return True, "OK"


def _resolve_project_name(project_id: str) -> Optional[dict]:
    """尝试将用户输入的项目标识解析为数据库项目记录"""
    try:
        from main import get_user_db_conn
        with get_user_db_conn() as conn:
            with conn.cursor() as cur:
                # 先尝试精确ID匹配
                if project_id.isdigit():
                    cur.execute(
                        "SELECT id, name FROM projects WHERE id = %s LIMIT 1",
                        (int(project_id),)
                    )
                    row = cur.fetchone()
                    if row:
                        return {"id": row[0], "name": row[1]}

                # 再尝试模糊名称匹配
                cur.execute(
                    "SELECT id, name FROM projects WHERE name ILIKE %s LIMIT 1",
                    (f"%{project_id}%",)
                )
                row = cur.fetchone()
                if row:
                    return {"id": row[0], "name": row[1]}
    except Exception as e:
        logger.warning(f"[query_tool] 项目名解析失败({project_id}): {e}")
    return None


def _format_rows(columns: list, rows: list) -> list:
    """将数据库查询结果格式化为 JSON 友好的列表"""
    formatted = []
    for row in rows:
        formatted_row = {}
        for i, col in enumerate(columns):
            val = row[i]
            if val is None:
                formatted_row[col] = None
            elif isinstance(val, str) and (val.startswith('{') or val.startswith('[')):
                # 尝试解析 JSONB/JSON 字段
                try:
                    formatted_row[col] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    formatted_row[col] = val
            elif hasattr(val, 'isoformat'):
                # datetime 对象 → ISO 字符串
                formatted_row[col] = val.isoformat()
            elif isinstance(val, (bytes, bytearray)):
                formatted_row[col] = f"<binary:{len(val)} bytes>"
            else:
                formatted_row[col] = val
        formatted.append(formatted_row)
    return formatted


def _rows_to_markdown(columns: list, rows: list, max_rows: int = 50) -> str:
    """将查询结果转为 markdown 表格，方便 LLM 直接引用"""
    if not rows:
        return "_(无数据)_"

    # 截断过多行
    display_rows = rows[:max_rows]

    # 列头
    header = "| " + " | ".join(str(c) for c in columns) + " |"
    separator = "|" + "|".join("---" for _ in columns) + "|"

    # 数据行（截断过长的值）
    def _cell(v) -> str:
        if v is None:
            return "_null_"
        s = str(v)
        # 避免 | 和换行破坏表格
        s = s.replace("|", "\\|").replace("\n", " ")
        # 截断过长内容
        if len(s) > 80:
            s = s[:77] + "..."
        return s

    data_lines = []
    for row in display_rows:
        cells = [_cell(row.get(c)) for c in columns]
        data_lines.append("| " + " | ".join(cells) + " |")

    table = "\n".join([header, separator] + data_lines)

    if len(rows) > max_rows:
        table += f"\n\n_(仅显示前 {max_rows} 行，共 {len(rows)} 行)_"

    return table
