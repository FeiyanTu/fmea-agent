#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Rewrite renderFailure: reuse function analysis table, append failure columns at end, pagination 50"""
p = r'D:\agent-fmea\fmea-agent\chat.html'
with open(p, 'r', encoding='utf-8') as f:
    c = f.read()

# Locate the function
start_marker = 'function renderFailure(body, data) {'
end_marker = '/** 失效分析 - 清除搜索 */'
end_marker2 = 'function clearFailureSearch()'

start = c.find(start_marker)
end = c.find(end_marker, start)
if end < 0:
    end = c.find(end_marker2, start)
if start < 0 or end < 0:
    print('ERROR: markers not found')
    exit(1)

new_func = '''function renderFailure(body, data) {
  const items = data.items || [];
  const highSev = items.filter(i => (i.severity || 0) >= 7).length;
  _failureData = data;
  _treeContext = 'failure';

  // 结构树
  const structData = state.stepData && state.stepData['structure'];
  const nodes = (structData && structData.nodes) || [];
  const nodeMap = {};
  nodes.forEach(n => { if (n && n.id) nodeMap[n.id] = n; });
  const nameToNode = {};
  nodes.forEach(n => { if (n && n.name) nameToNode[n.name] = n; });

  // 功能分析：domain_rows + function_id 查描述
  const funcData = state.stepData && state.stepData['function'];
  const funcById = {}; // function_id → description
  let domainRows = [];
  if (funcData) {
    if (funcData.domain_rows) domainRows = funcData.domain_rows;
    if (funcData.items) {
      funcData.items.forEach(item => {
        (item.functions || []).forEach(fn => {
          if (fn.function_id) funcById[fn.function_id] = fn.description || '';
        });
      });
    }
  }
  // focus_node_name → { parent_node, child_node, parent_function, middle_function, child_function }
  const fnRowByFocus = {};
  domainRows.forEach(r => {
    const focus = r.middle_node || '';
    if (focus && !fnRowByFocus[focus]) fnRowByFocus[focus] = r;
  });

  // 搜索过滤
  const searchKw = (_failureSearchKeyword || '').trim().toLowerCase();
  const searchField = _failureSearchField || 'failure_mode';
  const filtered = searchKw
    ? items.filter(it => ((it[searchField] || '')).toString().toLowerCase().includes(searchKw))
    : items;

  // 分页（超过150行启用，每页50行）
  const usePage = filtered.length > 150;
  const pageSize = 50;
  const maxPage = usePage ? Math.ceil(filtered.length / pageSize) : 1;
  let curPage = (usePage && _failurePage) || 1;
  if (curPage > maxPage) curPage = 1;
  const pageStart = (curPage - 1) * pageSize;
  const pageItems = usePage ? filtered.slice(pageStart, pageStart + pageSize) : filtered;

  const fullCount = items.length;
  const showCount = filtered.length;

  body.innerHTML = /*html*/`
    <div class="stats-banner">
      <div class="stat-card"><div class="stat-icon" style="background:#fee2e2">⚠️</div><div><div class="stat-value">${fullCount}</div><div class="stat-label">失效链总数</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:${highSev>0?'#fecaca':'#dcfce7'}">🔥</div><div><div class="stat-value" style="color:${highSev>0?'#dc2626':'#16a34a'}">${highSev}</div><div class="stat-label">高严重度 (S≥7)</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:#e0e7ff">🌳</div><div><div class="stat-value">${Object.keys(items.reduce((o,i)=>(o[i.node_id||'_']=1,o),{})).length}</div><div class="stat-label">涉及节点</div></div></div>
      ${buildPushButtonHtml('failure')}
    </div>

    <div style="display:flex;gap:8px;align-items:center;padding:8px 12px;background:#f8fafc;border-bottom:1px solid #e2e8f0;flex-wrap:wrap">
      <span style="font-size:13px;color:#475569;font-weight:600">?? 搜索：</span>
      <select id="failureSearchField" onchange="_failureSearchField=this.value;renderFailure(document.getElementById('contentBody'), _failureData);" style="padding:6px 10px;border:1px solid #cbd5e1;border-radius:4px;font-size:13px;background:white;min-width:140px">
        <option value="failure_mode">失效模式 (FM)</option>
        <option value="failure_effect">失效影响 (FE)</option>
        <option value="failure_cause">失效原因 (FC)</option>
        <option value="node_name">关键要素</option>
      </select>
      <input type="text" id="failureSearchInput" placeholder="输入关键词（按 Enter 或点搜索）" value="${escAttr(_failureSearchKeyword || '')}"
             onkeydown="if(event.key==='Enter'){event.preventDefault();_failureSearchKeyword=this.value;renderFailure(document.getElementById('contentBody'), _failureData);}"
             style="padding:6px 10px;border:1px solid #cbd5e1;border-radius:4px;font-size:13px;min-width:200px;flex:1;max-width:400px" />
      <button onclick="_failureSearchKeyword=document.getElementById('failureSearchInput').value;renderFailure(document.getElementById('contentBody'), _failureData);" style="padding:6px 14px;border:1px solid #3b82f6;border-radius:4px;background:#dbeafe;color:#1e40af;cursor:pointer;font-size:13px;font-weight:600">?? 搜索</button>
      <button onclick="clearFailureSearch()" style="padding:6px 12px;border:1px solid #cbd5e1;border-radius:4px;background:white;cursor:pointer;font-size:12px;color:#64748b">清除</button>
      <div style="flex:1"></div>
      <button onclick="saveFailureTableRows()" style="padding:6px 14px;border:1px solid #c4b5fd;border-radius:4px;background:#ede9fe;color:#5b21b6;cursor:pointer;font-size:13px;font-weight:600">?? 保存</button>
    </div>

    <div style="flex:1;display:flex;flex-direction:column;padding:12px;overflow:auto;background:white">
      <div style="overflow-x:auto;border-radius:8px;border:1px solid #e2e8f0">
        <table style="width:100%;border-collapse:collapse;min-width:1600px" id="failureTbl">
          <thead>
            <tr style="background:#f1f5f9;text-align:left;font-size:12px">
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:9%">① 上一较高级别</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:8%">② 关注要素</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:9%">③ 下一较低级别</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:7%">工程师</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:14%">① 上一较高级别功能和要求</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:14%">② 关注要素功能和要求</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:14%">③ 下一较低级别功能和要求或特性</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:14%">上一较高级别要素和/或最终用户的失效影响(FE)</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:50px;text-align:center">S</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:11%">关键要素的失效模式(FM)</th>
              <th style="padding:10px;border-bottom:2px solid #e2e8f0;width:14%">下一较低级别要素或特性的失效原因(FC)</th>
            </tr>
          </thead>
          <tbody>
            ${pageItems.length === 0 ? '<tr><td colspan="11" style="padding:40px;text-align:center;color:#94a3b8">暂无数据</td></tr>' : ''}
            ${pageItems.map((item) => {
              const sev = item.severity || 4;
              const sevBg = sev >= 7 ? '#fee2e2' : sev >= 4 ? '#fef3c7' : '#dcfce7';
              const sevColor = sev >= 7 ? '#dc2626' : sev >= 4 ? '#a16207' : '#16a34a';

              // 结构层级
              const focusNode = nodeMap[item.node_id] || {};
              const parentNode = focusNode.parent_id ? nodeMap[focusNode.parent_id] : null;
              const childNode = item.child_node_id ? nodeMap[item.child_node_id] : null;

              const parentName = parentNode ? parentNode.name + (parentNode.level ? ' ['+parentNode.level+']' : '') : (item.next_higher_level || '—');
              const focusName = focusNode.name || item.node_name || item.component_name || '—';
              const childName = childNode ? childNode.name + (childNode.level ? ' ['+childNode.level+']' : '') : (item.next_lower_level || '—');
              const dre = (childNode && childNode.assignee_name) || focusNode.assignee_name || '未指派';

              // 功能列：先按 focusName 从 domain_rows 查，再按 function_id 查
              let parentFunc = '—', middleFunc = '—', childFunc = '—';
              const fnRow = fnRowByFocus[focusName] || fnRowByFocus[focusName.replace(/\\s\\[.*\\]/, '')];
              if (fnRow) {
                parentFunc = fnRow.parent_function || parentFunc;
                middleFunc = fnRow.middle_function || middleFunc;
                childFunc = fnRow.child_function || childFunc;
              }
              // 如果 function_id 能在 funcById 中找到，覆盖 middleFunc
              const fnId = item.function_id || item.functionId || '';
              if (fnId && funcById[fnId]) middleFunc = funcById[fnId];
              if (item.function_description) middleFunc = middleFunc === '—' ? item.function_description : middleFunc;
              if (item.functionDescription) middleFunc = middleFunc === '—' ? item.functionDescription : middleFunc;

              return \`
              <tr style="border-bottom:1px solid #f1f5f9;font-size:12px">
                <td style="padding:6px;color:#475569">\${esc(parentName)}</td>
                <td style="padding:6px"><strong>\${esc(focusName)}</strong></td>
                <td style="padding:6px;color:#475569">\${esc(childName)}</td>
                <td style="padding:6px;color:#475569">\${esc(dre)}</td>
                <td style="padding:6px;line-height:1.5;color:#475569;font-size:11px">\${esc(parentFunc)}</td>
                <td style="padding:6px;line-height:1.5;color:#334155;font-size:11px"><strong>\${esc(middleFunc)}</strong></td>
                <td style="padding:6px;line-height:1.5;color:#475569;font-size:11px">\${esc(childFunc)}</td>
                <td style="padding:6px;line-height:1.5;color:#334155;font-size:11px">\${esc(item.failure_effect || '—')}</td>
                <td style="padding:6px;text-align:center;background:\${sevBg};color:\${sevColor};font-weight:600;font-size:13px">\${sev}</td>
                <td style="padding:6px;line-height:1.5;font-size:11px"><strong>\${esc(item.failure_mode || '—')}</strong></td>
                <td style="padding:6px;line-height:1.5;color:#334155;font-size:11px">\${esc(item.failure_cause || '—')}</td>
              </tr>\`;
            }).join('')}
          </tbody>
        </table>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 4px;font-size:12px;color:#64748b">
        <span>共 ${fullCount} 条失效链${searchKw ? '，当前显示 ' + showCount + ' 条（已过滤）' : ''}</span>
        <div style="display:flex;gap:4px;align-items:center">
          ${usePage ? (() => {
            let phtml = '';
            for (let i = 1; i <= maxPage; i++) {
              if (i === curPage) phtml += '<span style="padding:4px 10px;background:#3b82f6;color:white;border-radius:4px;font-size:13px;cursor:default">' + i + '</span>';
              else phtml += '<span style="padding:4px 10px;background:white;border:1px solid #cbd5e1;border-radius:4px;font-size:13px;cursor:pointer" onclick="_failurePage=' + i + ';renderFailure(document.getElementById(\'contentBody\'), _failureData)">' + i + '</span>';
            }
            return phtml;
          })() : ''}
        </div>
      </div>
    </div>
  \`;

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

/** 失效分析 - 保存 */
async function saveFailureTableRows() {
  if (!state.dirId) return;
  const data = _failureData || (state.stepData && state.stepData['failure']);
  if (!data) { showToast('无失效数据可保存', 'error'); return; }
  const ok = await saveStepTableData(data, '03-failure', state.dirId);
  if (ok) {
    state.stepData['failure'] = data;
    showToast('??? 失效分析保存成功', 'success');
  }
}
'''

result = c[:start] + new_func + c[end:]
with open(p, 'w', encoding='utf-8') as f:
    f.write(result)
print('Done')
