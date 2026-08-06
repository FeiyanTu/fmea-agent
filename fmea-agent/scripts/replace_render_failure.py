#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Replace renderFailure function with AIAG-VDA 12-column format"""
import re

path = r'D:\agent-fmea\fmea-agent\chat.html'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

start_marker = 'function renderFailure(body, data) {'
end_marker = 'function renderRiskFileManagerOnly(body) {'

start_idx = content.find(start_marker)
end_idx = content.find(end_marker)

if start_idx == -1 or end_idx == -1:
    print('ERROR: markers not found')
    exit(1)

# Build the new function
new_function = '''function renderFailure(body, data) {
  const items = data.items || [];
  const highSev = items.filter(i => (i.severity || 0) >= 7).length;

  // 保存失效数据供树的失效链按钮使用
  _failureData = data;
  _treeContext = 'failure';

  // 从 state 获取结构树节点
  const structData = state.stepData && state.stepData['structure'];
  const nodes = (structData && structData.nodes) || [];
  const nodeMap = {};
  nodes.forEach(n => { if (n && n.id) nodeMap[n.id] = n; });

  // 功能分析数据（用于功能列）
  const funcData = state.stepData && state.stepData['function'];
  const funcMap = {}; // node_id → [function_descriptions]
  if (funcData && funcData.items) {
    funcData.items.forEach(item => {
      if (item.node_id && item.functions) {
        funcMap[item.node_id] = item.functions.map(f => f.description || '');
      }
    });
  }
  // function_id → description 快速查询
  const funcIdMap = {};
  if (funcData && funcData.items) {
    funcData.items.forEach(item => {
      (item.functions || []).forEach(f => {
        if (f.function_id) funcIdMap[f.function_id] = f.description || '';
      });
    });
  }

  // 按节点分组统计失效数量
  const nodeFailCount = {};
  items.forEach(item => {
    const nid = item.node_id || '_unknown';
    nodeFailCount[nid] = (nodeFailCount[nid] || 0) + 1;
  });
  const failedNodeCount = Object.keys(nodeFailCount).length;

  const showTree = _failureViewMode === 'tree';
  const showTable = _failureViewMode === 'table';

  // AIAG-VDA 12列布局：编号 + 结构分析(4列) + 功能分析(3列) + 失效分析(4列)
  const table = buildPaginatedTable('failure', items,
    ['parent_node_name','node_name','child_node_name','dre',
     'parent_function_desc','focus_function_desc','child_function_desc',
     'failure_effect','failure_mode','failure_cause'],
    (item, idx) => {
      const sevClass = (item.severity||0)>=7 ? 'rpn-h' : (item.severity||0)>=4 ? 'rpn-m' : 'rpn-l';

      // 结构层级解析
      const focusNode = nodeMap[item.node_id] || {};
      const parentNode = focusNode.parent_id ? nodeMap[focusNode.parent_id] : null;
      const childNode = item.child_node_id ? nodeMap[item.child_node_id] : null;

      const parentName = parentNode ? parentNode.name + (parentNode.level ? ' ['+parentNode.level+']' : '') : (item.next_higher_level || '—');
      const focusName = focusNode.name || item.node_name || item.component_name || '—';
      const childName = childNode ? childNode.name + (childNode.level ? ' ['+childNode.level+']' : '') : (item.next_lower_level || '—');
      const dre = (focusNode.assignee_name || childNode && childNode.assignee_name || '未指派');

      // 功能分析 - 从function_id反查功能描述
      const fnId = item.function_id || item.functionId || '';
      const focusFuncDesc = funcIdMap[fnId] || item.function_description || item.functionDescription || '';

      // 上级和下级节点的功能描述（取该节点的第一个功能描述）
      const parentFuncs = parentNode ? funcMap[parentNode.id] : null;
      const childFuncs = childNode ? funcMap[childNode.id] : null;
      const parentFuncDesc = (parentFuncs && parentFuncs[0]) ? parentFuncs[0] : '—';
      const childFuncDesc = (childFuncs && childFuncs[0]) ? childFuncs[0] : '—';

      const rowNum = (pg && pg.pageSize && pg.currentPage)
        ? ((pg.currentPage - 1) * pg.pageSize + idx + 1)
        : (idx + 1);

      return `
      <tr>
        <td class="rownum">${rowNum}</td>
        <td>${esc(parentName)}</td>
        <td style="background:#fef3c7"><strong>${esc(focusName)}</strong></td>
        <td>${esc(childName)}</td>
        <td class="dre-cell">${esc(dre)}</td>
        <td>${esc(parentFuncDesc)}</td>
        <td>${esc(focusFuncDesc)}</td>
        <td>${esc(childFuncDesc)}</td>
        <td contenteditable="true" data-idx="${idx}" data-field="failure_effect">${esc(item.failure_effect||'')}</td>
        <td contenteditable="true" data-idx="${idx}" data-field="severity" class="${sevClass} sev-cell">${item.severity||4}</td>
        <td contenteditable="true" data-idx="${idx}" data-field="failure_mode"><strong>${esc(item.failure_mode||'')}</strong></td>
        <td contenteditable="true" data-idx="${idx}" data-field="failure_cause">${esc(item.failure_cause||'')}</td>
      </tr>`;
    },
    { headerLabels: [
      '#', '上一较高级别', '关键要素', '下一较低级别或特性类型', 'DRE',
      '上一较高级别功能及要求', '关键要素功能及要求', '下一较低级别功能及要求或特性',
      '失效影响(FE)', 'S', '失效模式(FM)', '失效原因(FC)'
    ] }
  );

  // 获取分页器用于行号计算
  const pg = getPaginator('failure');

  body.innerHTML = \`
    <div class="stats-banner">
      <div class="stat-card"><div class="stat-icon" style="background:#fee2e2">⚠️</div><div><div class="stat-value">\${items.length}</div><div class="stat-label">失效链总数</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:\${highSev>0?'#fecaca':'#dcfce7'}">🔥</div><div><div class="stat-value" style="color:\${highSev>0?'#dc2626':'#16a34a'}">\${highSev}</div><div class="stat-label">高严重度 (S≥7)</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:#e0e7ff">🌳</div><div><div class="stat-value">\${failedNodeCount}</div><div class="stat-label">涉及节点</div></div></div>
      <div class="view-toggle" style="margin-left:auto">
        <button class="vt-btn \${showTable?'active':''}" onclick="setFailureViewMode('table')" \${showTable?'disabled':''}>📋 表格展示</button>
        <button class="vt-btn \${showTree?'active':''}" onclick="setFailureViewMode('tree')" \${showTree?'disabled':''}>🌳 结构树</button>
      </div>
      \${buildPushButtonHtml('failure')}
    </div>
    \${table.searchHtml}
    <div class="data-card" style="margin-top:12px">
      <div class="data-card-header">📋 失效分析 - DFMEA 步骤 3</div>
      <div class="table-wrap" style="overflow-x:auto;max-width:100%">
        <table class="fmea-table" id="failureTable" style="min-width:1400px;white-space:nowrap">
          <thead>
            <tr>
              <th rowspan="2" style="min-width:40px;width:40px">#</th>
              <th colspan="4" style="text-align:center;background:#e0f2fe;border-right:2px solid #94a3b8">结构分析（步骤2）</th>
              <th colspan="3" style="text-align:center;background:#dbeafe;border-right:2px solid #94a3b8">功能分析（步骤3）</th>
              <th colspan="4" style="text-align:center;background:#fef3c7">失效分析（步骤4）</th>
            </tr>
            <tr>
              \${table.headersHtml}
            </tr>
          </thead>
          <tbody>\${table.bodyRows}</tbody>
        </table>
      </div>
      \${table.paginationHtml}
      <div style="margin-top:8px;padding:8px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;font-size:12px;color:#166534">
        💡 失效分析相关字段（FE/S/FM/FC）支持点击编辑并保存。结构层级和功能列自动从已有数据关联。
      </div>
    </div>
    \${nodes.length > 0 ? \`
    <div class="data-card view-container \${showTree ? '' : 'hidden'}" style="margin-top:12px">
      <div class="data-card-header">🌳 失效分布结构树（点击 ⚠️ 查看该节点失效链）</div>
      <div class="tree-canvas-wrapper" id="treeCanvas" style="min-height:300px">
      </div>
    </div>
    \` : ''}
  \`;
}
'''

new_section = new_function + '\n\nfunction renderRiskFileManagerOnly(body) {'
content = content[:start_idx] + new_section + content[end_idx + len(end_marker):]

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print('Done - renderFailure replaced with 12-column layout')
