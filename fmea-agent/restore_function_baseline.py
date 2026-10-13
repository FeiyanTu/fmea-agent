"""
从 功能分析备份.sql 恢复 baseline 到 project_id=15, step_name='function'
删除污染的工程师个人行（uid=7, 8）和 mnode-XXX 占位行
"""
import re
import json
import os
import psycopg2

BACKUP_SQL = r'C:\Users\wzjgha\Desktop\功能分析备份.sql'

conn = psycopg2.connect(
    host=os.getenv('URL_AGENT_DB_HOST', '122.51.85.245'),
    port=int(os.getenv('URL_AGENT_DB_PORT', '5432')),
    dbname=os.getenv('URL_AGENT_DB_NAME', 'fmea'),
    user=os.getenv('URL_AGENT_DB_USER', 'postgres'),
    password=os.getenv('URL_AGENT_DB_PASSWORD', 'postgres123!@#')
)
cur = conn.cursor()

# 1. 从备份 SQL 提取完整 JSON data
with open(BACKUP_SQL, 'r', encoding='utf-8') as f:
    content = f.read()

m = re.search(r"VALUES\(\d+, \d+, 'function', (-?\d+), '({.*?})'\s*::jsonb, (\d+),", content, re.DOTALL)
if not m:
    print('❌ 无法解析备份 SQL')
    exit(1)

backup_uid = int(m.group(1))
backup_data = json.loads(m.group(2))
backup_version = int(m.group(3))
print(f'备份数据: uid={backup_uid}, version={backup_version}')
print(f'  items: {len(backup_data.get("items", []))}')
print(f'  domain_rows: {len(backup_data.get("domain_rows", []))}')

if backup_uid != -1:
    print('❌ 备份不是 baseline 行')
    exit(1)

# 2. 删除工程师个人行（uid=7, 8）和污染的 mnode 行
cur.execute("""
    DELETE FROM fmea_step_assignee_data
    WHERE project_id = 15 AND step_name = 'function' AND assignee_user_id > 0
""")
print(f'\n删除工程师个人行: {cur.rowcount} 行')

# 3. UPSERT baseline（用备份的完整 data）
cur.execute("""
    INSERT INTO fmea_step_assignee_data (project_id, step_name, assignee_user_id, data, version, updated_at)
    VALUES (%s, %s, %s, %s, %s, NOW())
    ON CONFLICT (project_id, step_name, assignee_user_id)
    DO UPDATE SET data = EXCLUDED.data, version = fmea_step_assignee_data.version + 1, updated_at = NOW()
""", (15, 'function', -1, json.dumps(backup_data, ensure_ascii=False), backup_version))
print(f'\nUPSERT baseline: {cur.rowcount} 行已恢复')

conn.commit()
cur.close()
conn.close()
print('\n✅ 恢复完成')