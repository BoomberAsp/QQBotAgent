/* Config hot-management page: prompts / permissions / models / group features / personality */
(function () {
  let currentPrompt = null;
  let promptMtime = null;

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  // ── Tab switching ──────────────────────────────────────────────
  document.querySelectorAll('#cfg-tabs .tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#cfg-tabs .tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const name = tab.dataset.tab;
      document.querySelectorAll('.cfg-pane').forEach(p => { p.style.display = 'none'; });
      const pane = document.getElementById('pane-' + name);
      if (pane) pane.style.display = '';
    });
  });

  // ── Prompts ────────────────────────────────────────────────────
  async function loadPromptList() {
    const box = document.getElementById('prompt-list');
    try {
      const list = await api('/api/config/prompts');
      if (!list.length) { box.innerHTML = '<span class="muted">无 .md 配置文件</span>'; return; }
      box.innerHTML = list.map(f =>
        `<div class="prompt-item" data-name="${esc(f.name)}"
              style="padding:5px 8px;border-radius:6px;cursor:pointer;font-size:13px">
           <span class="mono">${esc(f.name)}</span>
           <span class="muted" style="float:right;font-size:11px">${fmtBytes(f.size)}</span>
         </div>`).join('');
      box.querySelectorAll('.prompt-item').forEach(el => {
        el.addEventListener('click', () => openPrompt(el.dataset.name));
      });
    } catch (e) {
      box.innerHTML = `<span class="muted">加载失败: ${esc(e.message)}</span>`;
    }
  }

  function markActive(name) {
    document.querySelectorAll('#prompt-list .prompt-item').forEach(el => {
      el.style.background = el.dataset.name === name ? 'rgba(120,160,255,.15)' : '';
    });
  }

  async function openPrompt(name) {
    const editor = document.getElementById('prompt-editor');
    const saveBtn = document.getElementById('prompt-save');
    try {
      const d = await api('/api/config/prompts/' + encodeURIComponent(name).replace(/%2F/g, '/'));
      currentPrompt = name;
      promptMtime = d.mtime;
      document.getElementById('prompt-name').textContent = name;
      document.getElementById('prompt-meta').textContent =
        new Date(d.mtime * 1000).toLocaleString('zh-CN', { hour12: false });
      editor.value = d.content;
      editor.disabled = false;
      saveBtn.disabled = false;
      markActive(name);
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  document.getElementById('prompt-save').addEventListener('click', async () => {
    if (!currentPrompt) return;
    const url = '/api/config/prompts/' + encodeURIComponent(currentPrompt).replace(/%2F/g, '/');
    try {
      const d = await api(url, {
        method: 'PUT',
        body: { content: document.getElementById('prompt-editor').value },
      });
      toast(d.note || '已保存', 'success', 5000);
      openPrompt(currentPrompt);   // refresh mtime meta
      loadPromptList();            // refresh sizes
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  // Warn if leaving with unsaved changes
  window.addEventListener('beforeunload', (ev) => {
    const editor = document.getElementById('prompt-editor');
    if (!editor.disabled && editor.value !== undefined && editor.dataset.dirty === '1') {
      ev.preventDefault();
      ev.returnValue = '';
    }
  });
  document.getElementById('prompt-editor').addEventListener('input', function () {
    this.dataset.dirty = '1';
  });
  document.getElementById('prompt-save').addEventListener('click', () => {
    document.getElementById('prompt-editor').dataset.dirty = '';
  });

  // ── Permissions ────────────────────────────────────────────────
  async function loadPermissions() {
    try {
      const d = await api('/api/config/permissions');
      document.getElementById('perm-super').value = d.SUPERUSERS || '';
      document.getElementById('perm-vip').value = d.VIP_USERS || '';
      document.getElementById('perm-note').textContent = d.note || '';
    } catch (e) {
      document.getElementById('perm-note').textContent = '加载失败: ' + e.message;
    }
  }

  document.getElementById('perm-save').addEventListener('click', async () => {
    try {
      const d = await api('/api/config/permissions', {
        method: 'PUT',
        body: {
          superusers: document.getElementById('perm-super').value,
          vip_users: document.getElementById('perm-vip').value,
        },
      });
      toast(d.note || '已保存', 'success');
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  // ── Models ─────────────────────────────────────────────────────
  async function loadModels() {
    const note = document.getElementById('models-note');
    try {
      const d = await api('/api/config/models');
      document.getElementById('models-editor').value = d.content;
      note.textContent = d.note || '';
    } catch (e) {
      note.textContent = '加载失败: ' + e.message;
    }
  }

  document.getElementById('models-save').addEventListener('click', async () => {
    const note = document.getElementById('models-note');
    if (!confirm('保存模型配置后需要重启 NoneBot 才生效，确认保存？')) return;
    try {
      const d = await api('/api/config/models', {
        method: 'PUT',
        body: { content: document.getElementById('models-editor').value },
      });
      toast(d.note || '已保存', 'success', 5000);
      note.textContent = d.note || '';
      loadModels();   // re-read merged content so masks are correct
    } catch (e) {
      toast(e.message, 'error');
      note.textContent = e.message;
    }
  });

  // ── Group features ─────────────────────────────────────────────
  async function loadGroup() {
    const note = document.getElementById('group-note');
    try {
      const d = await api('/api/config/group-features');
      document.getElementById('group-editor').value = d.content;
      note.textContent = d.exists === false
        ? '文件尚不存在（bot 运行后会生成），保存即创建。' + (d.note || '')
        : (d.note || '');
    } catch (e) {
      note.textContent = '加载失败: ' + e.message;
    }
  }

  document.getElementById('group-save').addEventListener('click', async () => {
    const note = document.getElementById('group-note');
    try {
      await api('/api/config/group-features', {
        method: 'PUT',
        body: { content: document.getElementById('group-editor').value },
      });
      toast('已保存，下条消息即生效', 'success');
      note.textContent = '已保存';
    } catch (e) {
      toast(e.message, 'error');
      note.textContent = e.message;
    }
  });

  // ── Personality ────────────────────────────────────────────────
  async function loadPersonality() {
    try {
      const d = await api('/api/config/personality');
      document.getElementById('pers-available').textContent =
        (d.available || []).join(' · ') || '—';
      if (!d.personality.error) {
        document.getElementById('pers-editor').value = d.personality.content;
      }
      if (!d.group_personality.error) {
        document.getElementById('pers-group-editor').value = d.group_personality.content;
      }
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  document.getElementById('pers-save').addEventListener('click', async () => {
    try {
      const d = await api('/api/config/personality', {
        method: 'PUT',
        body: {
          personality: document.getElementById('pers-editor').value,
          group_personality: document.getElementById('pers-group-editor').value,
        },
      });
      toast(d.note || '已保存', 'success');
      loadPersonality();
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  // ── Init ───────────────────────────────────────────────────────
  loadPromptList();
  loadPermissions();
  loadModels();
  loadGroup();
  loadPersonality();
})();
