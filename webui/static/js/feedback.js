/* User feedback page: timeline with filters and tagging */
(function () {
  let page = 1;
  const TYPE_LABEL = { feedback: '反馈', bug: 'Bug', suggestion: '建议' };
  const TYPE_CLS = { feedback: '', bug: 'badge-err', suggestion: 'badge-warn' };

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function renderItem(r) {
    const ctx = r.context || {};
    const ctxBits = [];
    if (ctx.role) ctxBits.push(`角色 ${ctx.role}`);
    if (ctx.workspace_usage_mb !== undefined) {
      ctxBits.push(`工作区 ${ctx.workspace_usage_mb}/${ctx.workspace_quota_mb} MB`);
    }
    if (ctx.active_special_session) ctxBits.push(`特殊会话「${ctx.active_special_session}」`);
    const tags = (r.tags || []).map(t => `<span class="pill">${esc(t)}</span>`).join(' ');
    return `<div class="fb-item">
      <div class="row">
        <span class="pill ${TYPE_CLS[r.type] || ''}">${TYPE_LABEL[r.type] || r.type}</span>
        <span class="mono muted">${esc(r.user_id)}</span>
        <span class="muted">${esc(r.timestamp)}</span>
        <span class="spacer"></span>
        ${tags}
        <input class="fb-tag-input" placeholder="+标签" style="width:90px;padding:3px 8px;font-size:12px"
               data-month="${esc(r._month)}" data-seq="${r._seq}">
      </div>
      <div style="margin-top:6px">${esc(r.content)}</div>
      ${ctxBits.length ? `<div class="muted" style="margin-top:4px;font-size:12px">${esc(ctxBits.join(' · '))}</div>` : ''}
    </div>`;
  }

  async function load() {
    const qs = new URLSearchParams({
      month: document.getElementById('fb-month').value,
      type: document.getElementById('fb-type').value,
      page, size: 30,
    });
    try {
      const d = await api('/api/feedback?' + qs);
      page = d.page;
      // month dropdown options (keep selection)
      const sel = document.getElementById('fb-month');
      const cur = sel.value;
      const opts = ['<option value="">全部月份</option>'].concat(
        (d.months || []).map(m => `<option value="${m}"${m === cur ? ' selected' : ''}>${m}</option>`));
      sel.innerHTML = opts.join('');
      const unreadEl = document.getElementById('fb-unread');
      unreadEl.textContent = d.unread ? `未读 ${d.unread}` : '无未读';
      unreadEl.className = 'pill ' + (d.unread ? 'badge-warn' : 'badge-ok');
      document.getElementById('fb-list').innerHTML = d.items.length
        ? d.items.map(renderItem).join('')
        : '<span class="muted">暂无反馈记录</span>';
      document.getElementById('fb-pageinfo').textContent =
        `第 ${d.page}/${d.pages} 页 · 共 ${d.total} 条`;
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  document.getElementById('fb-list').addEventListener('keydown', async (ev) => {
    if (ev.key !== 'Enter') return;
    const input = ev.target.closest('.fb-tag-input');
    if (!input || !input.value.trim()) return;
    try {
      await api(`/api/feedback/${input.dataset.month}/${input.dataset.seq}/tag`, {
        method: 'POST', body: { tag: input.value.trim() },
      });
      toast('标签已添加', 'success');
      load();
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  document.getElementById('fb-month').addEventListener('change', () => { page = 1; load(); });
  document.getElementById('fb-type').addEventListener('change', () => { page = 1; load(); });
  document.getElementById('fb-markread').addEventListener('click', async () => {
    try { await api('/api/feedback/mark-read', { method: 'POST' }); load(); }
    catch (e) { toast(e.message, 'error'); }
  });
  document.getElementById('fb-prev').addEventListener('click', () => { if (page > 1) { page--; load(); } });
  document.getElementById('fb-next').addEventListener('click', () => { page++; load(); });

  load();
})();
