/* Task log page */
(function () {
  let page = 1;

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  async function load() {
    const qs = new URLSearchParams({
      user_id: document.getElementById('tk-user').value,
      page, size: 50,
    });
    try {
      const d = await api('/api/tasks?' + qs);
      page = d.page;
      const sel = document.getElementById('tk-user');
      const cur = sel.value;
      sel.innerHTML = ['<option value="">全部用户</option>'].concat(
        (d.users || []).map(u => `<option value="${u}"${u === cur ? ' selected' : ''}>${u}</option>`)).join('');

      const tbody = document.querySelector('#tbl-tasks tbody');
      tbody.innerHTML = d.items.length ? d.items.map(r => `<tr>
        <td class="mono">${esc(String(r.timestamp || '').replace('T', ' ').slice(0, 19))}</td>
        <td class="mono">${esc(r._user_id)}</td>
        <td>${esc(r.tool || '—')}</td>
        <td>${r.status === 'failed' ? '<span class="err">失败</span>'
          : r.status && r.status !== 'success' ? `<span class="warn">${esc(r.status)}</span>`
          : '<span class="ok">成功</span>'}</td>
        <td class="muted">${esc((r.goal || '').slice(0, 80))}</td>
        <td><button class="btn btn-sm" data-uid="${esc(r._user_id)}" data-tid="${esc(r.task_id)}">详情</button></td>
      </tr>`).join('') : '<tr><td colspan="6" class="muted">暂无任务记录</td></tr>';
      document.getElementById('tk-pageinfo').textContent =
        `第 ${d.page}/${d.pages} 页 · 共 ${d.total} 条`;
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  document.querySelector('#tbl-tasks').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-tid]');
    if (!btn) return;
    const card = document.getElementById('tk-detail-card');
    const box = document.getElementById('tk-detail');
    card.style.display = '';
    box.textContent = '加载中…';
    try {
      const r = await api(`/api/tasks/${btn.dataset.uid}/${encodeURIComponent(btn.dataset.tid)}`);
      box.textContent = JSON.stringify(r, null, 2);
    } catch (e) {
      box.textContent = e.message;
    }
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  });

  document.getElementById('tk-user').addEventListener('change', () => { page = 1; load(); });
  document.getElementById('tk-prev').addEventListener('click', () => { if (page > 1) { page--; load(); } });
  document.getElementById('tk-next').addEventListener('click', () => { page++; load(); });

  load();
})();
