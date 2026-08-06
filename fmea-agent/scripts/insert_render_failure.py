#!/usr/bin/env python
# -*- coding: utf-8 -*-
import sys

with open(r'D:\agent-fmea\fmea-agent\chat.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 找到插入点
new_function = '''function renderFailure(body, data) {
  const items = data.items || [];
  const highSev = items.filter(i => (i.severity || 0) >= 7).length;

  // 保存失效数据供树的失效链按钮使用
  _failureData = data;
  _treeContext = 'failure';

  // 从 state 获取结构树节点（用于解析 parent / child 节点名称）
  const structData = state.stepData && state.stepData['structure'];
  const nodes = (structData && structData.nodes) || [];
  const nodeMap = {};
  nodes.forEach(n => { if (n && n.id) nodeMap[n.id] = n; });

  // 按节点分组统计失效数量
  const nodeFailCount = {};
  items.forEach(item => {
    const nid = item.node_id || '_unknown';
    nodeFailCount[nid] = (nodeFailCount[nid] || 0) + 1;
  });
  const failedNodeCount = Object.keys(nodeFailCount).length;

  const showTree = _failureViewMode === 'tree';
  const showTable = _failureViewMode === 'table';

  // AIAG-VDA FMEA 7列布局：上一较高级别 / 关键要素 / 下一较低级别 / FE / S / FM / FC
  const table = buildPaginatedTable('failure', items,
    ['parent_node_name','node_name','child_node_name','failure_effect','failure_mode','failure_cause'],
    (item, idx) => {
      const sevClass = (item.severity||0)>=7 ? 'rpn-h' : (item.severity||0)>=4 ? 'rpn-m' : 'rpn-l';
      // 解析 parent / child 节点名称
      const focusNode = nodeMap[item.node_id] || {};
      const parentNode = focusNode.parent_id ? nodeMap[focusNode.parent_id] : null;
      const childNode = item.child_node_id ? nodeMap[item.child_node_id] : null;
      const parentName = parentNode ? (parentNode.name + (parentNode.level ? ' ['+parentNode.level+']' : '')) : (item.next_higher_level || '—');
      const childName = childNode ? (childNode.name + (childNode.level ? ' ['+childNode.level+']' : '')) : (item.next_lower_level || '—');
      const focusName = focusNode.name || item.node_name || item.component_name || '—';
      const focusLevel = focusNode.level ? ' ['+focusNode.level+']' : '';
      return `
      <tr>
        <td style="background:#f8fafc">${esc(parentName)}</td>
        <td contenteditable="true" data-idx="${idx}" data-field="node_name" style="background:#fef3c7"><strong>${esc(focusName)}${esc(focusLevel)}</strong></td>
        <td style="background:#f8fafc">${esc(childName)}</td>
        <td contenteditable="true" data-idx="${idx}" data-field="failure_effect">${esc(item.failure_effect||'')}</td>
        <td contenteditable="true" data-idx="${idx}" data-field="severity" class="${sevClass}" style="text-align:center;font-weight:600">${item.severity||4}</td>
        <td contenteditable="true" data-idx="${idx}" data-field="failure_mode"><strong>${esc(item.failure_mode||'')}</strong></td>
        <td contenteditable="true" data-idx="${idx}" data-field="failure_cause">${esc(item.failure_cause||'')}</td>
      </tr>`;
    },
    { headerLabels: [
      '上一较高级别',
      '关键要素',
      '下一较低级别或特性类型',
      '失效影响 (FE)',
      'S',
      '失效模式 (FM)',
      '失效原因 (FC)'
    ] }
  );

  body.innerHTML = `
    <div class="stats-banner">
      <div class="stat-card"><div class="stat-icon" style="background:#fee2e2">⚠️</div><div><div class="stat-value">${items.length}</div><div class="stat-label">失效链总数</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:${highSev>0?'#fecaca':'#dcfce7'}">🔥</div><div><div class="stat-value" style="color:${highSev>0?'#dc2626':'#16a34a'}">${highSev}</div><div class="stat-label">高严重度 (S≥7)</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:#e0e7ff">🌳</div><div><div class="stat-value">${failedNodeCount}</div><div class="stat-label">涉及节点</div></div></div>
      <div class="view-toggle" style="margin-left:auto">
        <button class="vt-btn ${showTable?'active':''}" onclick="setFailureViewMode('table')" ${showTable?'disabled':''}>📋 表格展示</button>
        <button class="vt-btn ${showTree?'active':''}" onclick="setFailureViewMode('tree')" ${showTree?'disabled':''}>🌳 结构树</button>
      </div>
      ${buildPushButtonHtml('failure')}
    </div>
    ${table.searchHtml}
    <div class="data-card" style="margin-top:12px">
      <div class="data-card-header">📋 失效分析明细表（AIAG-VDA 7 步法 · 可编辑）</div>
      <div class="table-wrap">
        <table class="fmea-table" id="failureTable">
          <thead>
            <tr>
              <th colspan="3" style="text-align:center;background:#e0f2fe;border-right:2px solid #94a3b8">结构层级</th>
              <th colspan="4" style="text-align:center;background:#fef3c7">失效链分析</th>
            </tr>
            <tr>
              ${table.headersHtml}
            </tr>
          </thead>
          <tbody>${table.bodyRows}</tbody>
        </table>
      </div>
      ${table.paginationHtml}
    </div>
    ${nodes.length > 0 ? `
    <div class="data-card view-container ${showTree ? '' : 'hidden'}" style="margin-top:12px">
      <div class="data-card-header">🌳 失效分布结构树（点击 ⚠️ 查看该节点失效链）</div>
      <div class="tree-canvas-wrapper" id="treeCanvas" style="min-height:300px">
      </div>
    </div>
    ` : ''}
  `;
}

function renderRiskFileManagerOnly(body) {
  body.innerHTML = `
    <div class="empty-state" style="padding:32px">
      <div class="empty-icon">📭</div>
      <h2>暂无评估数据</h2>
      <p>该步骤尚未生成数据，请在对话框中引导 AI 完成风险评估。</p>
    </div>
  `;
}

'''

content = content.replace(
    "function renderRisk(body, data) {",
    new_function + "function renderRisk(body, data) {",
    1
)

with open(r'D:\agent-fmea\fmea-agent\chat.html', 'w', encoding='utf-8') as f:
    f.write(content)
print('Done')