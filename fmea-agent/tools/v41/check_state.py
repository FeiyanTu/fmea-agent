"""一次性检查脚本：step 4/5 状态 + LLM 配置"""
import json
from pathlib import Path

DATA = Path(r'D:/agent-fmea/fmea-agent/data/projects/12V低压电池/fmea')

for name in ['04-risk.json', '05-optimization.json']:
    p = DATA / name
    if not p.exists():
        print(f'{name}: 不存在')
        continue
    d = json.load(open(p, encoding='utf-8'))
    items = d.get('items', [])
    print(f'=== {name} ===')
    print(f'  items: {len(items)}')
    print(f'  generated_by: {d.get("generated_by", "-")}')
    print(f'  agent: {d.get("agent_version", "-")}')
    if items:
        f0 = items[0]
        print(f'  首条 fields: {list(f0.keys())}')
        print(f'  首条 child_node_id: {f0.get("child_node_id", "<无>")}')
        # 含 child_node_id 的占比
        with_child = sum(1 for i in items if i.get('child_node_id'))
        print(f'  含 child_node_id: {with_child}/{len(items)}')
    print()

# LLM 配置
print('=== .env 关键配置 ===')
env = Path(r'D:/agent-fmea/fmea-agent/.env')
if env.exists():
    for line in env.read_text(encoding='utf-8').splitlines():
        if any(k in line for k in ['API_BASE', 'MODEL', 'API_KEY', 'LLM']):
            # 隐藏 key
            if 'KEY' in line or 'TOKEN' in line:
                k, v = line.split('=', 1) if '=' in line else (line, '')
                print(f'  {k}={v[:8]}...' if v else f'  {k}=')
            else:
                print(f'  {line}')

# 找 step 4/5 agent 文件
print('\n=== agents/ 中的 step 文件 ===')
for f in sorted(Path(r'D:/agent-fmea/fmea-agent/agents').glob('step*.py')):
    print(f'  {f.name}: {f.stat().st_size} bytes')
