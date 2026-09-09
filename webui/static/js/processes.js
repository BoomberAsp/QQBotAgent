/* Process management page */
(function () {
  const body = document.getElementById('proc-body');
  let watchdog = { enabled: false };

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
})();
