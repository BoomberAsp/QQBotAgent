/* Workspace disk usage page */
(function () {
  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function bar(pct) {
    const cls = pct >= 90 ? 'badge-err' : pct >= 70 ? 'badge-warn' : '';
    return `<div class="row" style="gap:6px">
      <div class="bar" style="flex:1"><div style="width:${Math.min(100, pct)}%"></div></div>
      <span class="${cls ? 'pill ' + cls : 'muted'}">${pct.toFixed(1)}%</span>
    </div>`;
  }

  async function load() {
    try {
      const d = await api('/api/workspace/stats');
      document.getElementById('ws-root').textContent = d.root || '';
      const card = document.getElementById('ws-card');
      const unmounted = document.getElementById('ws-unmounted');
      if (!d.mounted) {
        card.style.display = 'none';
        unmounted.style.display = '';
        return;
      }
      unmounted.style.display = 'none';
      card.style.display = '';
      document.getElementById('ws-total').textContent = fmtBytes(d.total_used);
      document.querySelector('#tbl-ws tbody').innerHTML = d.users.length
        ? d.users.map(u => `<tr>
            <td class="mono">${esc(u.user_id)}</td>
            <td>${u.role === 'admin' ? '管理员' : u.role === 'vip' ? 'VIP' : '普通'}</td>
            <td class="mono">${fmtBytes(u.used)}</td>
            <td class="mono">${fmtBytes(u.quota)}</td>
            <td style="min-width:180px">${bar(u.percent)}</td>
            <td><button class="btn btn-sm" data-tree="${esc(u.user_id)}">文件树</button></td>
          </tr>`).join('')
        : '<tr><td colspan="6" class="muted">没有用户工作区</td></tr>';
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  document.getElementById('tbl-ws').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-tree]');
    if (!btn) return;
    const uid = btn.dataset.tree;
    const card = document.getElementById('ws-tree-card');
    const box = document.getElementById('ws-tree');
    document.getElementById('ws-tree-uid').textContent = uid;
    card.style.display = '';
    box.textContent = '加载中…';
    try {
      const d = await api(`/api/workspace/${uid}`);
      document.getElementById('ws-tree-size').textContent = fmtBytes(d.size);
      box.innerHTML = d.entries.map(e =>
        e.type === 'dir'
          ? `📁 ${esc(e.path)}/`
          : `   ${esc(e.path)} <span class="muted">(${fmtBytes(e.size)})</span>`
      ).join('\n') || '（空）';
    } catch (e) {
      box.textContent = e.message;
    }
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  });

  document.getElementById('ws-refresh').addEventListener('click', load);
  load();
})();
