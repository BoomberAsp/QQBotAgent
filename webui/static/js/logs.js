/* Log viewer page: 4-source tabs, search, pagination, live tail over WS */
(function () {
  const box = document.getElementById('log-box');
  const search = document.getElementById('log-search');
  const linesSel = document.getElementById('log-lines');
  const tailBtn = document.getElementById('btn-tail');
  const tailState = document.getElementById('tail-state');

  let source = 'nonebot';
  let tailOn = true;
  let ws = null;
  let loadedCount = 0;      // lines currently shown (for "load older")
  let filtering = false;    // search active → pause live tail merging

  // ── display helpers ────────────────────────────────────────────
  function nearBottom() {
    return box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  }
  function appendLines(lines) {
    if (!lines.length) return;
    const stick = nearBottom();
    const MAX = 3000;
    let current = box.textContent.split('\n');
    if (current.length + lines.length > MAX) {
      current = current.slice(current.length + lines.length - MAX);
    }
    box.textContent = current.concat(lines).join('\n');
    if (stick) box.scrollTop = box.scrollHeight;
  }
  function replaceLines(lines) {
    box.textContent = lines.join('\n') || '（空）';
    loadedCount = lines.length;
    box.scrollTop = box.scrollHeight;
  }

  // ── fetch / pagination ────────────────────────────────────────
  async function refresh(params = {}) {
    const qs = new URLSearchParams({
      lines: params.lines || linesSel.value,
      before: params.before || 0,
      q: params.q || '',
    });
    try {
      const data = await api(`/api/logs/${source}?` + qs);
      if (params.prepend) {
        const stick = false;
        box.textContent = data.lines.concat(box.textContent.split('\n')).join('\n');
        loadedCount += data.lines.length;
      } else {
        replaceLines(data.lines);
      }
      filtering = !!qs.get('q');
      return data;
    } catch (e) {
      box.textContent = '加载失败: ' + e.message;
      return null;
    }
  }

  // ── websocket tail ────────────────────────────────────────────
  function setTailState(text, cls) {
    if (!text) { tailState.style.display = 'none'; return; }
    tailState.style.display = '';
    tailState.textContent = text;
    tailState.className = 'pill ' + (cls || '');
  }
  function connect() {
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    if (!tailOn || filtering) {
      setTailState(filtering ? '过滤中，自动跟踪暂停' : '', 'badge-warn');
      return;
    }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws/logs/${source}`);
    ws.onopen = () => setTailState('实时跟踪中', 'badge-ok');
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        appendLines(msg.lines || []);
      } catch (e) { /* ignore malformed */ }
    };
    ws.onclose = () => {
      setTailState('连接断开', 'badge-err');
      if (tailOn && !filtering) setTimeout(connect, 3000);  // auto-reconnect
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  // ── wiring ────────────────────────────────────────────────────
  document.getElementById('source-tabs').addEventListener('click', (ev) => {
    const tab = ev.target.closest('.tab');
    if (!tab) return;
    document.querySelectorAll('#source-tabs .tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    source = tab.dataset.source;
    box.textContent = '加载中…';
    refresh();
    connect();
  });

  document.getElementById('btn-refresh').addEventListener('click', () => refresh());

  document.getElementById('btn-older').addEventListener('click', () =>
    refresh({ before: loadedCount, prepend: true }));

  let searchTimer = null;
  search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { refresh(); connect(); }, 350);
  });

  tailBtn.addEventListener('click', () => {
    tailOn = !tailOn;
    tailBtn.textContent = '自动跟踪：' + (tailOn ? '开' : '关');
    if (!tailOn) setTailState('', '');
    connect();
  });

  document.getElementById('btn-clear').addEventListener('click', async () => {
    if (!confirm(`确定清空 ${source} 日志？`)) return;
    try {
      await api(`/api/logs/${source}/clear`, { method: 'POST' });
      toast('日志已清空', 'success');
      refresh();
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  refresh();
  connect();
})();
