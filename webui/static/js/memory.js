/* Memory & profile page */
(function () {
  let cache = {};       // "type|user|name" → entry (list already carries content)

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function fmtTs(t) {
    if (!t) return '—';
    const n = Number(t);
    if (!isFinite(n) || n <= 0) return '—';
    return new Date(n * 1000).toLocaleString('zh-CN', { hour12: false });
  }

  const TYPE_CLS = { user: '', knowledge: 'badge-ok', system: 'badge-warn' };

  // ── Memory list ────────────────────────────────────────────────
  async function load() {
    const box = document.getElementById('mem-list');
    const qs = new URLSearchParams({
      mem_type: document.getElementById('mem-type').value,
      user_id: document.getElementById('mem-user').value,
      q: document.getElementById('mem-q').value,
    });
    box.innerHTML = '<span class="muted">加载中…</span>';
    try {
      const d = await api('/api/memory?' + qs);
      const entries = d.entries || [];
      cache = {};
      entries.forEach(e => { cache[`${e.type}|${e.user_id || ''}|${e.name}`] = e; });
      document.getElementById('mem-count').textContent = `共 ${entries.length} 条`;
      if (d.error) {
        box.innerHTML = `<span class="warn">加载出错: ${esc(d.error)}</span>`;
        return;
      }
      if (!entries.length) {
        box.innerHTML = '<span class="muted">暂无记忆</span>';
        return;
      }
      box.innerHTML = entries.map(e => `
        <div class="fb-item">
          <div class="row">
            <span class="pill ${TYPE_CLS[e.type] || ''}">${esc(e.type)}</span>
            <span class="mono">${esc(e.name)}</span>
            ${e.user_id ? `<span class="muted mono">@${esc(e.user_id)}</span>` : ''}
            <span class="spacer"></span>
            <span class="muted" style="font-size:12px">更新 ${fmtTs(e.updated_at)}</span>
            <button class="btn btn-sm mem-edit" data-name="${esc(e.name)}"
                    data-type="${esc(e.type)}" data-user="${esc(e.user_id || '')}">编辑</button>
            <button class="btn btn-sm btn-danger mem-del" data-name="${esc(e.name)}"
                    data-type="${esc(e.type)}" data-user="${esc(e.user_id || '')}">删除</button>
          </div>
          ${e.description ? `<div class="muted" style="margin-top:4px;font-size:13px">${esc(e.description)}</div>` : ''}
          <pre class="log-box" style="margin-top:6px;max-height:180px;white-space:pre-wrap">${esc(e.content)}</pre>
        </div>`).join('');

      box.querySelectorAll('.mem-edit').forEach(b => b.addEventListener('click', () => {
        openEdit(b.dataset.name, b.dataset.type, b.dataset.user);
      }));
      box.querySelectorAll('.mem-del').forEach(b => b.addEventListener('click', () => {
        delMemory(b.dataset.name, b.dataset.type, b.dataset.user);
      }));
    } catch (e) {
      box.innerHTML = `<span class="warn">${esc(e.message)}</span>`;
    }
  }

  // ── Form ───────────────────────────────────────────────────────
  function showForm(on) {
    document.getElementById('mem-form').style.display = on ? '' : 'none';
  }

  function openNew() {
    document.getElementById('mem-form-title').textContent = '新建记忆';
    document.getElementById('mf-name').value = '';
    document.getElementById('mf-name').disabled = false;
    document.getElementById('mf-type').value = 'knowledge';
    document.getElementById('mf-user').value = '';
    document.getElementById('mf-desc').value = '';
    document.getElementById('mf-content').value = '';
    showForm(true);
  }

  function openEdit(name, type, user) {
    const e = cache[`${type}|${user || ''}|${name}`] || {};
    document.getElementById('mem-form-title').textContent = '编辑记忆 — ' + name;
    document.getElementById('mf-name').value = name;
    document.getElementById('mf-name').disabled = true;
    document.getElementById('mf-type').value = type;
    document.getElementById('mf-user').value = user;
    document.getElementById('mf-desc').value = e.description || '';
    document.getElementById('mf-content').value = e.content || '';
    showForm(true);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function delMemory(name, type, user) {
    if (!confirm(`删除记忆「${name}」？此操作不可撤销。`)) return;
    const qs = new URLSearchParams({ name, mem_type: type, user_id: user });
    try {
      await api('/api/memory?' + qs, { method: 'DELETE' });
      toast('已删除', 'success');
      load();
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  document.getElementById('mem-new').addEventListener('click', openNew);
  document.getElementById('mf-cancel').addEventListener('click', () => showForm(false));
  document.getElementById('mem-refresh').addEventListener('click', load);
  ['mem-type', 'mem-user', 'mem-q'].forEach(id =>
    document.getElementById(id).addEventListener('change', load));
  document.getElementById('mem-q').addEventListener('keydown', ev => {
    if (ev.key === 'Enter') load();
  });

  document.getElementById('mf-save').addEventListener('click', async () => {
    const body = {
      name: document.getElementById('mf-name').value.trim(),
      type: document.getElementById('mf-type').value,
      user_id: document.getElementById('mf-user').value.trim(),
      description: document.getElementById('mf-desc').value,
      content: document.getElementById('mf-content').value,
    };
    if (!body.name) { toast('名称不能为空', 'error'); return; }
    try {
      await api('/api/memory', { method: 'PUT', body });
      toast('已保存', 'success');
      showForm(false);
      load();
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  // ── Profile ────────────────────────────────────────────────────
  let pfUid = '';
  const pfEditor = document.getElementById('pf-editor');
  const pfSave = document.getElementById('pf-save');

  document.getElementById('pf-load').addEventListener('click', async () => {
    const uid = document.getElementById('pf-uid').value.trim();
    if (!uid) { toast('请输入 QQ 号', 'error'); return; }
    pfUid = uid;
    try {
      const d = await api('/api/profiles/' + encodeURIComponent(uid));
      pfEditor.value = JSON.stringify(d, null, 2);
      pfEditor.disabled = false;
      pfSave.disabled = false;
      document.getElementById('pf-note').textContent = '已加载画像，可编辑后保存。';
    } catch (e) {
      pfEditor.value = '';
      pfEditor.disabled = true;
      pfSave.disabled = true;
      document.getElementById('pf-note').textContent = e.message;
    }
  });

  pfSave.addEventListener('click', async () => {
    let data;
    try { data = JSON.parse(pfEditor.value); }
    catch (e) { toast('JSON 语法错误: ' + e.message, 'error'); return; }
    try {
      await api('/api/profiles/' + encodeURIComponent(pfUid), { method: 'PUT', body: data });
      toast('画像已保存', 'success');
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  load();
})();
