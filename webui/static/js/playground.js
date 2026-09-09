/* Agent Playground page */
(function () {
  let history = [];   // [{role:'user'|'assistant', content}]

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function bubble(role, text, meta) {
    const isUser = role === 'user';
    const cls = isUser ? 'pg-user' : 'pg-bot';
    const label = isUser ? '你' : 'Roxy';
    return `<div class="pg-msg ${cls}">
      <div class="pg-role">${label}${meta ? ` <span class="muted" style="font-weight:400">${esc(meta)}</span>` : ''}</div>
      <div class="pg-text">${esc(text)}</div>
    </div>`;
  }

  function renderLog() {
    const log = document.getElementById('pg-log');
    if (!history.length) {
      log.innerHTML = '<span class="muted">尚无对话 — 输入消息开始测试提示词/人格效果。</span>';
      return;
    }
    log.innerHTML = history.map(m => bubble(m.role, m.content, m.meta || '')).join('');
    log.scrollTop = log.scrollHeight;
  }

  function setStatus(s, kind) {
    const el = document.getElementById('pg-status');
    el.textContent = s || '';
    el.className = kind === 'error' ? 'warn' : 'muted';
  }

  // ── Options ────────────────────────────────────────────────────
  async function loadOptions() {
    try {
      const d = await api('/api/playground/options');
      const pSel = document.getElementById('pg-personality');
      const pers = (d.personalities || []);
      pSel.innerHTML = '<option value="">(默认 / 无人格覆盖)</option>' +
        pers.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
      const tSel = document.getElementById('pg-tier');
      const models = (d.models || []);
      tSel.innerHTML = models.length
        ? models.map(m => `<option value="${esc(m.tier)}">${esc(m.tier)} — ${esc(m.model)}</option>`).join('')
        : '<option value="reasoning">reasoning</option>';
    } catch (e) {
      setStatus('选项加载失败: ' + e.message, 'error');
    }
  }

  // ── Send ───────────────────────────────────────────────────────
  async function send() {
    const input = document.getElementById('pg-input');
    const msg = input.value.trim();
    if (!msg) return;
    const btn = document.getElementById('pg-send');
    btn.disabled = true;
    setStatus('思考中…');
    history.push({ role: 'user', content: msg });
    input.value = '';
    renderLog();
    const payload = {
      message: msg,
      personality: document.getElementById('pg-personality').value,
      tier: document.getElementById('pg-tier').value,
      role: document.getElementById('pg-role').value,
      // send prior turns (excluding the one we just pushed) for multi-turn context
      history: history.slice(0, -1).map(m => ({ role: m.role, content: m.content })),
    };
    try {
      const d = await api('/api/playground/run', { method: 'POST', body: payload });
      history.push({
        role: 'assistant', content: d.reply,
        meta: `${d.model} · ${d.elapsed_sec}s`,
      });
      setStatus(`完成 — ${d.model}，用时 ${d.elapsed_sec}s`);
    } catch (e) {
      history.push({ role: 'assistant', content: '[出错] ' + e.message, meta: '' });
      setStatus(e.message, 'error');
    } finally {
      btn.disabled = false;
      renderLog();
    }
  }

  // ── Preview ────────────────────────────────────────────────────
  async function preview() {
    const qs = new URLSearchParams({
      personality: document.getElementById('pg-personality').value,
      role: document.getElementById('pg-role').value,
    });
    setStatus('构建系统提示词…');
    try {
      const d = await api('/api/config/system-prompt-preview?' + qs);
      document.getElementById('pg-preview-card').style.display = '';
      document.getElementById('pg-preview-meta').textContent =
        `personality=${d.personality} role=${d.role} · ${d.length} 字符`;
      document.getElementById('pg-preview-body').textContent = d.content;
      setStatus('');
    } catch (e) {
      setStatus(e.message, 'error');
    }
  }

  document.getElementById('pg-send').addEventListener('click', send);
  document.getElementById('pg-input').addEventListener('keydown', ev => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); send(); }
  });
  document.getElementById('pg-clear').addEventListener('click', () => {
    history = []; renderLog(); setStatus('');
  });
  document.getElementById('pg-preview').addEventListener('click', preview);
  document.getElementById('pg-preview-close').addEventListener('click', () => {
    document.getElementById('pg-preview-card').style.display = 'none';
  });

  loadOptions();
  renderLog();
})();
