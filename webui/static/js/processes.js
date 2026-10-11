/* Process management page */
(function () {
  const body = document.getElementById('proc-body');
  let watchdog = { enabled: false };

  // ── NapCat 登录二维码 ──────────────────────────────────
  const qrCard = document.getElementById('napcat-qr-card');
  const qrState = document.getElementById('qr-state');
  const qrWrap = document.getElementById('qr-wrap');
  const qrImg = document.getElementById('qr-img');
  const qrHint = document.getElementById('qr-hint');
  let lastMtime = 0;

  const QR_STATES = {
    logged_in: { cls: 'pill badge-ok', text: '已登录' },
    waiting: { cls: 'pill badge-warn', text: '等待扫码' },
    unknown: { cls: 'pill badge-warn', text: '状态未知' },
    stopped: { cls: 'pill badge-err', text: '未运行' },
  };

  async function loadQr() {
    let s;
    try { s = await api('/api/napcat/qrcode'); } catch (e) { return; }

    // 二维码仅在 NapCat 运行时才有意义。
    const show = s.process === 'running';
    qrCard.hidden = !show;
    if (!show) return;

    const st = QR_STATES[s.login] || QR_STATES.unknown;
    qrState.className = st.cls;
    qrState.textContent = st.text;

    const done = s.login === 'logged_in';
    qrWrap.classList.toggle('logged-in', done);

    const qr = s.qrcode || {};
    if (qr.exists && qr.mtime !== lastMtime) {
      lastMtime = qr.mtime;
      qrImg.src = `/api/napcat/qrcode.png?t=${qr.mtime}`;
    }
    qrImg.style.display = qr.exists ? '' : 'none';

    if (done) qrHint.textContent = '该 QQ 已成功登录';
    else if (s.login_error) qrHint.textContent = `登录异常：${s.login_error}`;
    else if (!qr.exists) qrHint.textContent = '正在生成二维码…';
    else qrHint.textContent = '使用手机 QQ 扫码登录';
  }

  function statePill(info) {
    const st = info.state;
    const cls = st === 'running' ? 'badge-ok' : st === 'unavailable' ? 'badge-warn' : 'badge-err';
    const text = st === 'running' ? '运行中' : st === 'unavailable' ? '不可用' : '已停止';
    const extra = info.adopted ? '（面板外启动）' : '';
    return `<span class="pill ${cls}">${text}</span>${extra}`;
  }

  function render(data) {
    watchdog = data.watchdog || watchdog;
    const wdState = document.getElementById('wd-state');
    wdState.textContent = watchdog.enabled ? '已开启' : '已关闭';
    wdState.className = 'pill ' + (watchdog.enabled ? 'badge-ok' : '');
    document.getElementById('wd-poll').textContent = watchdog.poll_seconds || 30;
    document.getElementById('wd-rate').textContent = watchdog.rate_limit || '';
    document.getElementById('wd-limit').textContent =
      watchdog.enabled ? '巡检中' : '自动重启已停用';

    const rows = [];
    for (const [name, p] of Object.entries(data.processes || {})) {
      const crash = p.crash_count
        ? `${p.crash_count}${p.last_crash ? '（最近 ' + new Date(p.last_crash * 1000).toLocaleString('zh-CN', { hour12: false }) + '）' : ''}`
        : '0';
      const disabled = p.state === 'unavailable';
      rows.push(`<tr>
        <td>${p.label || name}${p.should_run ? ' <span class="muted" title="看门狗将保持其运行">●</span>' : ''}</td>
        <td>${statePill(p)}</td>
        <td class="mono">${p.pid ?? '—'}</td>
        <td>${p.uptime !== undefined && p.uptime !== null ? fmtUptime(p.uptime) : '—'}</td>
        <td class="${p.crash_count ? 'warn' : 'muted'}">${crash}${p.watchdog_throttled ? ' <span class="pill badge-warn">限速中</span>' : ''}</td>
        <td style="text-align:right; white-space:nowrap">
          <button class="btn btn-sm" data-act="start" data-name="${name}" ${p.state === 'running' || disabled ? 'disabled' : ''}>启动</button>
          <button class="btn btn-sm" data-act="stop" data-name="${name}" ${p.state !== 'running' || disabled ? 'disabled' : ''}>停止</button>
          <button class="btn btn-sm" data-act="restart" data-name="${name}" ${p.state !== 'running' || disabled ? 'disabled' : ''}>重启</button>
        </td>
      </tr>`);
    }
    body.innerHTML = rows.join('') || '<tr><td colspan="6" class="muted">无进程</td></tr>';
  }

  async function load() {
    try {
      render(await api('/api/processes'));
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  body.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('button[data-act]');
    if (!btn || btn.disabled) return;
    const { name, act } = btn.dataset;
    btn.disabled = true;
    try {
      const r = await api(`/api/processes/${name}/${act}`, { method: 'POST' });
      toast(`${name} ${act} ${r.note === 'already_running' ? '（已在运行）' : '成功'}`, 'success');
    } catch (e) {
      toast(e.message, 'error');
    }
    setTimeout(load, 800);  // give the process a moment to settle
    if (name === 'napcat' && (act === 'start' || act === 'restart')) {
      setTimeout(loadQr, 800);  // 触发启动/重启后尽快尝试拉二维码
    }
  });

  document.getElementById('wd-toggle').addEventListener('click', async () => {
    const next = !watchdog.enabled;
    try {
      await api('/api/processes/watchdog', { method: 'POST', body: { enabled: next } });
      toast(`看门狗已${next ? '开启' : '关闭'}`, 'success');
      load();
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  load();
  setInterval(load, 5000);

  loadQr();
  setInterval(loadQr, 3000);   // 比进程轮询更密，贴合 NapCat 二维码刷新节奏
})();
