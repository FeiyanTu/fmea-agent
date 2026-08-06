import json
from collections import defaultdict
import openpyxl

# Load structure tree
with open(r'D:\agent-fmea\fmea-agent\data\projects\12V电池-weknora\fmea\01-structure-tree.json', 'r', encoding='utf-8') as f:
    tree = json.load(f)
nodes = tree['nodes']

name_to_node = {}
name_to_node_rev = {}
for n in nodes:
    name_to_node[n['name']] = n['id']
    name_to_node_rev[n['id']] = n['name']

# Load Excel
wb = openpyxl.load_workbook(r'C:\Users\wzjgha\Downloads\DFMEA（新版格式）-12V低压电池-2025-01-14.xlsx', data_only=True)
ws = wb['DFMEA（新版格式）修订版']

# Verified column layout from row 10 headers:
# Col 3 (C): 1. Next Higher Level 上一较高级别
# Col 4 (D): 2. Focus Element 关注要素
# Col 5 (E): 3. Next Lower Level 下一较低级别
# Col 6 (F): 1. Next Higher Level Function and Requirement 上一较高级别功能和要求
# Col 7 (G): 2. Focus Element Function and Requirement 关注要素功能和要求
# Col 8 (H): 3. Next Lower Level Function and Requirement 下一较低级别功能和要求

# Track all 6 columns' last non-empty values to handle merged cells
rows = []
last_higher = ''   # col 3
last_focus = ''    # col 4
last_lower = ''    # col 5
last_upper_func = ''   # col 6
last_focus_func = ''   # col 7

for row_idx in range(11, 349):
    c3 = ws.cell(row_idx, 3).value   # 上一较高级别
    c4 = ws.cell(row_idx, 4).value   # 关注要素
    c5 = ws.cell(row_idx, 5).value   # 下一较低级别
    c6 = ws.cell(row_idx, 6).value   # 上一较高级别功能和要求
    c7 = ws.cell(row_idx, 7).value   # 关注要素功能和要求
    c8 = ws.cell(row_idx, 8).value   # 下一较低级别功能和要求

    # Update last non-empty values for merged cells
    if c3 is not None: last_higher = str(c3).strip()
    if c4 is not None: last_focus = str(c4).strip()
    if c5 is not None: last_lower = str(c5).strip()
    if c6 is not None: last_upper_func = str(c6).strip()
    if c7 is not None: last_focus_func = str(c7).strip()

    cur_lower_func = str(c8).strip() if c8 is not None else ''

    # Only include rows that have a non-empty lower function (col 8)
    if cur_lower_func:
        rows.append({
            'higher': last_higher,
            'focus': last_focus,
            'lower': last_lower,
            'upper_func': last_upper_func,
            'focus_func': last_focus_func,
            'lower_func': cur_lower_func
        })


def classify_function(text):
    if any(k in text for k in ['尺寸', '厚度', '宽度', '高度', '重量', '能量密度', '功率']):
        return ('物理特性', '机械力')
    if any(k in text for k in ['装配', '换修', '拆卸', '密封', '紧固螺栓', '铜排', '接线柱']):
        return ('装配要求', '机械力')
    if any(k in text for k in ['耐久', '寿命', '循环', '老化', '存储', '盐雾', '腐蚀', '耐腐蚀']):
        return ('耐久要求', '无')
    if any(k in text for k in ['温度', '工作温度', '存储温度', '耐压', '湿热', '针刺']):
        return ('环境要求', '热')
    if any(k in text for k in ['电压采样', '电流采样', '温度采样', '电压监控', '电流监控',
                                  '温度监控', '采样诊断', '采样同步', '采样功能']):
        return ('采样监控', '信号')
    if any(k in text for k in ['SOC', 'SOP', 'SOH', '状态估计']):
        return ('状态估计', '信号')
    if any(k in text for k in ['诊断', 'UDS', '故障码', '漏报', '故障存储']):
        return ('诊断功能', '信号')
    if any(k in text for k in ['充电', '放电', '过流', '限流', '充电电流', '充电电压']):
        return ('充放电控制', '电气')
    if any(k in text for k in ['均衡', '被动均衡']):
        return ('均衡功能', '电气')
    if any(k in text for k in ['MOS', '开闭', '驱动', '通断']):
        return ('开关控制', '电气')
    if any(k in text for k in ['短路保护', '反接保护', '过流保护', '限流保护', '安全保护']):
        return ('安全保护', '电气')
    if any(k in text for k in ['热失控', '热安全']):
        return ('热安全', '热')
    if any(k in text for k in ['模式', '休眠', '唤醒', '低功耗', '就绪', '补电', '锁电']):
        return ('模式管理', '信号')
    if any(k in text for k in ['CAN', '网络管理', '通讯', 'OTA', 'FOTA', '刷新', '标定']):
        return ('网络通信', '信号')
    if any(k in text for k in ['信息安全', '数据存储', '故障存储']):
        return ('数据安全', '信号')
    if any(k in text for k in ['防护', 'IP', 'EMC', 'EMI']):
        return ('环境防护', '电气')
    if any(k in text for k in ['功能安全', 'ASIL']):
        return ('功能安全', '信号')
    if any(k in text for k in ['供电', '低压供电', '智驾', '冷启动', 'OTA供电']):
        return ('供电性能', '电气')
    if any(k in text for k in ['容量', '能量', '倍率', '截止', 'EIS', '压差']):
        return ('电芯性能', '电气')
    if any(k in text for k in ['法规', 'REACH', '回收', '禁限用']):
        return ('法规合规', '无')
    if any(k in text for k in ['碰撞', '结构安全']):
        return ('结构安全', '机械力')
    if any(k in text for k in ['出货', '工厂模式', '保护套']):
        return ('出货状态', '无')
    if any(k in text for k in ['外观', '表面']):
        return ('外观要求', '无')
    return ('其他功能', '无')


# Group by (child_node, focus_function) pair
# Each unique pair produces ONE FunctionDetail
# Multiple rows with the same (child, focus_function) but different lower_funcs are merged
groups = defaultdict(list)
group_order = []
for row in rows:
    comp_name = row['lower']
    node_id = name_to_node.get(comp_name, '')
    if not node_id:
        continue
    focus_func = row['focus_func']
    if not focus_func:
        continue
    key = (node_id, focus_func)
    if key not in groups:
        group_order.append(key)
    groups[key].append(row)


# Build function details — 一条记录 = 一行 Excel（每个量化指标一条）
func_details = []
func_idx = 1
for row in rows:
    comp_name = row['lower']
    node_id = name_to_node.get(comp_name, '')
    if not node_id:
        continue
    focus_func = row['focus_func']
    lower_func = row['lower_func']
    if not focus_func:
        continue
    if not lower_func:
        continue

    description = f'{focus_func}（{lower_func}）'
    category, flow = classify_function(focus_func)
    fid = f'fn-{node_id}-{func_idx:02d}'
    func_idx += 1

    func_details.append({
        'function_id': fid,
        'child_node_id': node_id,
        'description': description,
        'function_category': category,
        'flow_type': flow,
        'higher_level': row.get('higher', ''),
        'focus_element': row.get('focus', ''),
        'lower_level': comp_name,
        'upper_function': row.get('upper_func', ''),
        'focus_function': focus_func,
        'lower_function': lower_func
    })


# Aggregate upper level functions
all_upper = set()
for rows_in_group in groups.values():
    for r in rows_in_group:
        if r['upper_func']:
            all_upper.add(r['upper_func'])

unique_upper = []
seen = set()
for u in sorted(all_upper):
    s = u.split(',')[0].strip() if ',' in u else u
    if s not in seen:
        seen.add(s)
        unique_upper.append(s)
upper_summary = '; '.join(unique_upper) if unique_upper else '整车功能要求'


# Group by child node for lower_level_function summary
lower_groups = defaultdict(list)
for fd in func_details:
    cname = name_to_node_rev.get(fd['child_node_id'], fd['child_node_id'])
    lower_groups[cname].append(fd['description'])

lower_summary_parts = []
for cname in sorted(lower_groups.keys()):
    descs = lower_groups[cname]
    brief = '、'.join(descs[:3])
    if len(descs) > 3:
        brief += '等'
    lower_summary_parts.append(f'{cname}: {brief}')
lower_summary = '; '.join(lower_summary_parts)

item = {
    'node_id': 'node-0001',
    'node_name': '12V低压电池',
    'upper_level_function': upper_summary,
    'functions': func_details,
    'lower_level_function': lower_summary,
    'llm_generated': False,
    'user_modified': True,
    'component_name': '12V低压电池',
    'higher_level_function': '',
    'focus_function': ''
}


# Build node_functions
child_nodes_by_parent = defaultdict(list)
for fd in func_details:
    child_nodes_by_parent[fd['child_node_id']].append({
        'function_id': fd['function_id'],
        'description': fd['description'],
        'focus_function': fd.get('focus_function', ''),
        'lower_function': fd.get('lower_function', '')
    })

node_functions = {}
for n in nodes:
    nid = n['id']
    if nid in child_nodes_by_parent:
        node_functions[nid] = {
            'node_name': n['name'],
            'functions': child_nodes_by_parent[nid]
        }


# Build function network
relations = []
for fd in func_details:
    target_name = name_to_node_rev.get(fd['child_node_id'], fd['child_node_id'])
    relations.append({
        'id': f'rel-{fd["function_id"]}',
        'source_node_id': 'node-0001',
        'source_function_id': fd['function_id'],
        'target_node_id': fd['child_node_id'],
        'target_function_id': fd['function_id'],
        'relation_type': '需要-满足',
        'description': f'12V低压电池需要{target_name}提供{fd["description"]}',
        'source_label': '需要方',
        'target_label': '满足方'
    })

fn_network = {
    'relations': relations,
    'generated_at': '2026-07-17'
}

output = {
    'items': [item],
    'function_network': fn_network,
    'node_functions': node_functions,
    'function_network_detail': [],
    'llm_raw_output': '',
    'user_edited': True
}

out_path = r'D:\agent-fmea\fmea-agent\data\projects\12V电池-weknora\fmea\02-function.json'
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f'Saved to: {out_path}')
print(f'FunctionDetails: {len(func_details)}')
print(f'NodeFunctions: {len(node_functions)} nodes')
print(f'Relations: {len(relations)}')

# Verify first 3 functions
print('\n=== First 3 functions ===')
for fd in func_details[:3]:
    print(f'{fd["function_id"]}: {fd["description"]}')
    print(f'  child: {fd["child_node_id"]} | category: {fd["function_category"]} | flow: {fd["flow_type"]}')
    print(f'  focus: {fd["focus_function"]}')
    print(f'  lower_function: {fd["lower_function"]}')

print('\n=== Functions by component ===')
for n in sorted(nodes, key=lambda x: x['id']):
    nid = n['id']
    if nid in child_nodes_by_parent:
        fns = child_nodes_by_parent[nid]
        print(f'{n["name"]} ({n["level"]}): {len(fns)} functions')
