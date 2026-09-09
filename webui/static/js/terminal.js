/* Web terminal: xterm.js over /ws/terminal (pty) */
(function () {
  if (typeof Terminal === 'undefined') {
    document.getElementById('term').textContent =
      'xterm.js 加载失败（CDN 不可达？）。请检查网络后刷新。';
    return;
  }

  const stateEl = document.getElementById('term-state');
  const term = new Terminal({
    fontSize: 13,
    fontFamily: '"SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace',
    theme: {
      background: '#0a0e17',
      foreground: '#dbe4f5',
      cursor: '#5b8cff',
    },
    cursorBlink: true,
    scrollback: 3000,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(document.getElementById('term'));
  fit.fit();

  let ws = null;

  function setState(text, cls) {
    stateEl.textContent = text;
    stateEl.className = 'pill ' + (cls || '');
  }

  function sendResize() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }));
    }
  }

  function connect() {
    if (ws) { try { ws.close(); } catch (e) {} }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws/terminal`);
    ws.onopen = () => {
      setState('已连接', 'badge-ok');
      term.reset();
      term.focus();
      sendResize();
    };
    ws.onmessage = (ev) => term.write(ev.data);
    ws.onclose = () => {
      setState('已断开', 'badge-err');
      term.write('\r\n\x1b[90m[连接关闭 — 点击「重新连接」]\x1b[0m\r\n');
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  term.onData((data) => {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(data);
  });

  document.getElementById('btn-reconnect').addEventListener('click', connect);

  window.addEventListener('resize', () => {
    fit.fit();
    sendResize();
  });
  // sidebar collapses under 860px → refit after layout change
  setTimeout(() => { fit.fit(); sendResize(); }, 300);

  connect();
})();
