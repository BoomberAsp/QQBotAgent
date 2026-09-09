/* Session management: temp session list + per-user detail / special sessions */
(function () {
  const tbody = document.querySelector('#tbl-sessions tbody');
  const detail = document.getElementById('ss-detail');

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }
  function fmtTs(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return isNaN(d) ? esc(ts) : d.toLocaleString('zh-CN', { hour12: false });
  }

  async function loadList() {
    try {
      const list = await api('/api/sessions');
      tbody.innerHTML = list.length ? list.map(s => `<tr>
        <td class="mono">${esc(s.user_id)}</td>
        <td>${s.messages}</td>
        <td>${s.tool_call_count || 0}</td>
        <td>${fmtTs(s.last_active)}</td>
        <td>${fmtBytes(s.size_bytes)}</td>
        <td style="text-align:right">
          <button class="btn btn-sm" data-view="${esc(s.user_id)}">查看</button>
          <button class="btn btn-sm btn-danger" data-clear="${esc(s.user_id)}">清除</button>
        </td>
      </tr>`).join('') : '<tr><td colspan="6" class="muted">没有临时会话</td></tr>';
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  function uid() { return document.getElementById('ss-uid').value.trim(); }

  async function clearTemp(target) {
    const id = target || uid();
    if (!id) return toast('请先输入用户 ID', 'error');
    if (!confirm(`清除 ${id} 的临时会话？（会先备份到面板备份目录）`)) return;
    try {
      await api(`/api/sessions/${id}/clear-temp`, { method: 'POST' });
      toast('已清除', 'success');
      detail.textContent = '已清除 ' + id + ' 的临时会话';
      loadList();
    } catch (e) { toast(e.message, 'error'); }
  }

  tbody.addEventListener('click', (ev) => {
    const v = ev.target.closest('[data-view]');
    const c = ev.target.closest('[data-clear]');
    if (v) {
      document.getElementById('ss-uid').value = v.dataset.view;
      showTemp(v.dataset.view);
    } else if (c) {
      clearTemp(c.dataset.clear);
    }
  });

  async function showTemp(target) {
    const id = target || uid();
    if (!id) return toast('请先输入用户 ID', 'error');
    detail.textContent = '加载中…';
    try {
      const s = await api(`/api/sessions/${id}/temp`);
      const lines = (s.context || []).map(m => `[${m.role}] ${m.content}`);
      detail.textContent =
        `用户 ${s.user_id} · 创建 ${fmtTs(s.created_at)} · 最后活跃 ${fmtTs(s.last_active)}\n` +
        `消息 ${lines.length} 条 · 工具调用 ${s.tool_call_count || 0} 次\n` +
        '──────────────────────────────\n' +
        (lines.join('\n\n') || '（空）');
      detail.scrollTop = detail.scrollHeight;
    } catch (e) {
      detail.textContent = e.message;
    }
  }

  async function showSpecial() {
    const id = uid();
    if (!id) return toast('请先输入用户 ID', 'error');
    detail.innerHTML = '加载中…';
    try {
      const d = await api(`/api/sessions/${id}/special`);
      if (!d.sessions.length) {
        detail.textContent = d.error || '该用户没有特殊会话';
        return;
      }
      detail.innerHTML = d.sessions.map(s => `<div class="fb-item">
        <div class="row">
          <strong>${esc(s.name)}</strong>
          ${d.active === s.name ? '<span class="pill badge-ok">当前激活</span>' : ''}
          <span class="muted">${s.total_messages || 0} 条消息</span>
          <span class="muted">最后活跃 ${s.last_active ? fmtTs(s.last_active) : '—'}</span>
          <span class="spacer"></span>
          <button class="btn btn-sm btn-danger" data-del="${esc(s.name)}">删除</button>
        </div>
      </div>`).join('');
    } catch (e) {
      detail.textContent = e.message;
    }
  }

  detail.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-del]');
    if (!btn) return;
    const id = uid(), name = btn.dataset.del;
    if (!confirm(`删除特殊会话「${name}」？其会话级文件将一并删除（仓库保留）。`)) return;
    try {
      const r = await api(`/api/sessions/${id}/special/${encodeURIComponent(name)}`,
                          { method: 'DELETE' });
      toast(`已删除，释放 ${fmtBytes(r.freed_bytes || 0)}`, 'success');
      showSpecial();
    } catch (e) { toast(e.message, 'error'); }
  });

  document.getElementById('ss-temp').addEventListener('click', () => showTemp());
  document.getElementById('ss-special').addEventListener('click', showSpecial);
  document.getElementById('ss-clear').addEventListener('click', () => clearTemp());

  loadList();
})();
