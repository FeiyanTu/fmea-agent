#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Rewrite renderFailure using the same UI pattern as renderFunctionTableView"""
p = r'D:\agent-fmea\fmea-agent\chat.html'
with open(p, 'r', encoding='utf-8') as f:
    c = f.read()

# Find renderFailure
start_marker = 'function renderFailure(body, data) {'
end_marker = '\nfunction renderRiskFileManagerOnly(body) {'
start = c.find(start_marker)
end = c.find(end_marker, start)
if start < 0 or end < 0:
    print('ERROR: markers not found')
    exit(1)

new_function = '''function renderFailure(body, data) {
  const items = data.items || [];
  const highSev = items.filter(i => (i.severity || 0) >= 7).length;
  _failureData = data;
  _treeContext = 'failure';

  // 结构树 & 功能分析数据
  const structData = state.stepData && state.stepData['structure'];
  const nodes = (structData && structData.nodes) || [];
  const nodeMap = {};
  nodes.forEach(n => { if (n && n.id) nodeMap[n.id] = n; });

  const funcData = state.stepData && state.stepData['function'];
  const funcMap = {}; // node_id → [function_descriptions]
  const funcIdMap = {}; // function_id → description
  if (funcData && funcData.items) {
    funcData.items.forEach(item => {
      if (item.node_id && item.functions) {
        funcMap[item.node_id] = item.functions.map(f => f.description || '');
      }
      (item.functions || []).forEach(f => {
        if (f.function_id) funcIdMap[f.function_id] = f.description || '';
      });
    });
  }

  // 按节点分组统计
  const nodeFailCount = {};
  items.forEach(item => {
    const nid = item.node_id || '_unknown';
    nodeFailCount[nid] = (nodeFailCount[nid] || 0) + 1;
  });
  const failedNodeCount = Object.keys(nodeFailCount).length;

  // 搜索 & 过滤
  const searchKw = (_failureSearchKeyword || '').trim().toLowerCase();
  const searchField = _failureSearchField || 'failure_mode';
  const filtered = searchKw
    ? items.filter(item => {
        const v = (item[searchField] || '').toString().toLowerCase();
        return v.includes(searchKw);
      })
    : items;

  body.innerHTML = `
    <div class="stats-banner">
      <div class="stat-card"><div class="stat-icon" style="background:#fee2e2">⚠️</div><div><div class="stat-value">${items.length}</div><div class="stat-label">失效链总数</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:#fecaca"><div class="stat-value" style="color:#dc2626">${highSev}</div></div><div class="stat-label">高严重度 (S≥7)</div></div>
      <div class="stat-card"><div class="stat-icon" style="background:#e0e7ff">🌳</div><div><div class="stat-value">${failedNodeCount}</div><div class="stat-label">涉及节点</div></div></div>
      ${buildPushButtonHtml('failure')}
    </div>

    <div style="display:flex;gap:8px;align-items:center;padding:8px 12px;background:#f8fafc;border-bottom:1px solid #e2e8f0;flex-wrap:wrap">
      <span style="font-size:13px;color:#475569;font-weight:600">🔍 搜索：</span>
      <select id="failureSearchField" onchange="_failureSearchField=this.value;renderFailure(document.getElementById('contentBody'), _failureData);" style="padding:6px 10px;border:1px solid #cbd5e1;border-radius:4px;font-size:13px;background:white;min-width:140px">
        <option value="parent_node_name">上一较高级别</option>
        <option value="node_name">关键要素</option>
        <option value="child_node_name">下一较低级别</option>
        <option value="failure_mode">失效模式 (FM)</option>
        <option value="failure_effect">失效影响 (FE)</option>
        <option value="failure_cause">失效原因 (FC)</option>
      </select>
      <input type="text" id="failureSearchInput" placeholder="输入关键词（按 Enter 或点搜索）" value="${escAttr(_failureSearchKeyword || '')}"
             onkeydown="if(event.key==='Enter'){event.preventDefault();_failureSearchKeyword=this.value;renderFailure(document.getElementById('contentBody'), _failureData);}"
             style="padding:6px 10px;border:1px solid #cbd5e1;border-radius:4px;font-size:13px;min-width:200px;flex:1;max-width:400px" />
      <button onclick="_failureSearchKeyword=document.getElementById('failureSearchInput').value;renderFailure(document.getElementById('contentBody'), _failureData);" style="padding:6px 14px;border:1px solid #3b82f6;border-radius:4px;background:#dbeafe;color:#1e40af;cursor:pointer;font-size:13px;font-weight:600">🔍 搜索</button>
      <button onclick="clearFailureSearch()" style="padding:6px 12px;border:1px solid #cbd5e1;border-radius:4px;background:white;cursor:pointer;font-size:12px;color:#64748b">清除</button>
      <div style="flex:1"></div>
      <button onclick="saveFailureTableRows()" style="padding:6px 14px;border:1px solid #c4b5fd;border-radius:4px;background:#ede9fe;color:#5b21b6;cursor:pointer;font-size:13px;font-weight:600">💾 保存</button>
    </div>

    <div style="flex:1;display:flex;flex-direction:column;padding:12px;overflow:auto;background:white">
      <div style="overflow-x:auto;border-radius:8px;border:1px solid #e2e8f0">
        <table style="width:100%;border-collapse:collapse;min-width:1500px" id="failureTbl">
          <thead>
            <tr style="background:#f1f5f9;text-align:left;font-size:12px">
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:50px;text-align:center">#</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:9%">① 上一较高级别</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:8%">② 关键要素</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:9%">③ 下一较低级别或特性类型</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:7%">DRE</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:14%">① 上一较高级别功能及要求<br><span style="font-weight:normal;color:#94a3b8">(请从)</span></th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:14%">② 关键要素功能及要求</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:14%">③ 下一较低级别功能及要求或特性</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:14%">① 上一较高级别要素和/或最终用户的失效影响(FE)</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:50px;text-align:center">S</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:11%">② 关键要素的失效模式(FM)</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:14%">③ 下一较低级别要素或特性的失效原因(FC)</th>
            </tr>
          </thead>
          <tbody>
            ${filtered.length === 0 ? '<tr><td colspan="12" style="padding:40px;text-align:center;color:#94a3b8">暂无数据</td></tr>' : ''}
            ${filtered.map((item, idx) => {
              const sev = item.severity || 4;
              const sevBg = sev >= 7 ? '#fee2e2' : sev >= 4 ? '#fef3c7' : '#dcfce7';
              const sevColor = sev >= 7 ? '#dc2626' : sev >= 4 ? '#a16207' : '#16a34a';
              const focusNode = nodeMap[item.node_id] || {};
              const parentNode = focusNode.parent_id ? nodeMap[focusNode.parent_id] : null;
              const childNode = item.child_node_id ? nodeMap[item.child_node_id] : null;
              const parentName = parentNode ? parentNode.name + (parentNode.level ? ' ['+parentNode.level+']' : '') : (item.next_higher_level || '—');
              const focusName = focusNode.name || item.node_name || item.component_name || '—';
              const childName = childNode ? childNode.name + (childNode.level ? ' ['+childNode.level+']' : '') : (item.next_lower_level || '—');
              const dre = focusNode.assignee_name || (childNode && childNode.assignee_name) || '未指派';
              const focusFuncDesc = funcIdMap[item.function_id || item.functionId || ''] || item.function_description || item.functionDescription || '—';
              const parentFuncs = parentNode ? funcMap[parentNode.id] : null;
              const childFuncs = childNode ? funcMap[childNode.id] : null;
              const parentFuncDesc = (parentFuncs && parentFuncs[0]) ? parentFuncs[0] : '—';
              const childFuncDesc = (childFuncs && childFuncs[0]) ? childFuncs[0] : '—';
              return `
              <tr style="border-bottom:1px solid #f1f5f9;font-size:12px" data-rowidx="${idx}">
                <td style="padding:6px;text-align:center;color:#94a3b8">${idx + 1}</td>
                <td style="padding:6px;color:#475569">${esc(parentName)}</td>
                <td style="padding:6px"><strong>${esc(focusName)}</strong></td>
                <td style="padding:6px;color:#475569">${esc(childName)}</td>
                <td style="padding:6px;color:#475569">${esc(dre)}</td>
                <td style="padding:6px;line-height:1.5;color:#475569">${esc(parentFuncDesc)}</td>
                <td style="padding:6px;line-height:1.5;color:#334155"><strong>${esc(focusFuncDesc)}</strong></td>
                <td style="padding:6px;line-height:1.5;color:#475569">${esc(childFuncDesc)}</td>
                <td style="padding:6px;line-height:1.5;color:#334155">${esc(item.failure_effect || '—')}</td>
                <td style="padding:6px;text-align:center;background:${sevBg};color:${sevColor};font-weight:600">${sev}</td>
                <td style="padding:6px;line-height:1.5"><strong>${esc(item.failure_mode || '—')}</strong></td>
                <td style="padding:6px;line-height:1.5;color:#334155">${esc(item.failure_cause || '—')}</td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>
      <div style="margin-top:8px;font-size:12px;color:#64748b">
        共 ${items.length} 条失效链，当前显示 ${filtered.length} 条${searchKw ? '（已过滤）' : ''}
      </div>
    </div>
  `;

  // 恢复搜索字段选择
  const sel = document.getElementById('failureSearchField');
  if (sel) sel.value = _failureSearchField || 'failure_mode';
}

/** 失效分析 - 清除搜索 */
function clearFailureSearch() {
  _failureSearchKeyword = '';
  const inp = document.getElementById('failureSearchInput');
  if (inp) inp.value = '';
  renderFailure(document.getElementById('contentBody'), _failureData);
}

/** 失效分析 - 保存到 DB */
async function saveFailureTableRows() {
  if (!state.dirId) return;
  const data = _failureData || (state.stepData && state.stepData['failure']);
  if (!data) { showToast('无失效数据可保存', 'error'); return; }
  const ok = await saveStepTableData(data, '03-failure', state.dirId);
  if (ok) {
    state.stepData['failure'] = data;
    showToast('✅ 失效分析保存成功', 'success');
  }
}
'''

result = c[:start] + new_function + c[end:]
with open(p, 'w', encoding='utf-8') as f:
    f.write(result)
print('Done - renderFailure rewritten with function-analysis style')

# Also add the search state variables if missing
if '_failureSearchKeyword' not in c:
    add_marker = "let _funcSearchKeyword = '';"
    add_at = c.find(add_marker)
    if add_at >= 0:
        add_str = '''let _failureSearchKeyword = '';
let _failureSearchField = 'failure_mode';
'''
        result2 = c[:add_at] + add_str + c[add_at:]
        # re-write without recursion
        with open(p, 'r', encoding='utf-8') as f:
            check = f.read()
        if '_failureSearchKeyword' not in check:
            with open(p, 'w', encoding='utf-8') as f:
                f.write(result2)
            print('Added failure search state vars')