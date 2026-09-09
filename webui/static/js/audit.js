/* Tool audit page */
(function () {
  let page = 1;

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }
  function table(el, head, rows) {
    el.innerHTML = '<thead><tr>' + head.map(h => `<th>${h}</th>`).join('') +
      '</tr></thead><tbody>' +
      (rows.length ? rows.map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('')
        : '<tr><td colspan="' + head.length + '" class="muted">暂无数据</td></tr>') +
      '</tbody>';
  }

  async function loadStats() {
    const days = document.getElementById('au-days').value;
    const s = await api(`/api/audit/tool-stats?days=${days}`);
    document.getElementById('au-total').textContent = s.total;
    table(document.getElementById('tbl-tools'),
      ['工具', '调用次数', '失败'],
      Object.entries(s.by_tool).map(([name, t]) => [
        name, t.calls, t.failed ? `<span class="err">${t.failed}</span>` : '0',
      ]));
  }

  async function loadCalls() {
    const qs = new URLSearchParams({
      date: document.getElementById('au-date').value,
      tool: document.getElementById('au-tool').value.trim(),
      user: document.getElementById('au-user').value.trim(),
      page, size: 50,
    });
    const d = await api('/api/audit/tool-calls?' + qs);
    page = d.page;  // server clamps out-of-range pages
    table(document.getElementById('tbl-calls'),
      ['时间', '用户', '工具', '结果', '参数', '摘要'],
      d.items.map(r => [
        `<span class="mono">${esc((r.timestamp || '').replace('T', ' ').slice(0, 19))}</span>`,
        esc(r.user_id),
        esc(r.tool),
        r.success ? '<span class="ok">成功</span>' : '<span class="err">失败</span>',
        `<code>${esc(JSON.stringify(r.arguments || {})).slice(0, 120)}</code>`,
        `<span class="muted">${esc(r.result_summary || '').slice(0, 120)}</span>`,
      ]));
    document.getElementById('au-pageinfo').textContent =
      `第 ${d.page}/${d.pages} 页 · 共 ${d.total} 条`;
  }

  function reload() {
    loadStats().catch(e => toast(e.message, 'error'));
    loadCalls().catch(e => toast(e.message, 'error'));
  }

  document.getElementById('au-days').addEventListener('change', loadStats);
  document.getElementById('au-search').addEventListener('click', () => { page = 1; loadCalls(); });
  document.getElementById('au-prev').addEventListener('click', () => { if (page > 1) { page--; loadCalls(); } });
  document.getElementById('au-next').addEventListener('click', () => { page++; loadCalls(); });

  // default date = today
  document.getElementById('au-date').value = new Date().toISOString().slice(0, 10);
  reload();
})();
