/* Dashboard page: fetch /api/dashboard + /ws/dashboard and fill the panels.
 * Process/resource/token/feedback cards are all wired (Phase 2–5 complete).
 * The renderers below still tolerate missing/partial fields defensively. */
(function () {
  const REFRESH_MS = 5000;

  function set(id, value) {
    const el = document.getElementById(id);
    if (el && value !== undefined && value !== null) el.textContent = value;
  }

  function statusPill(state) {
    // state: "running" | "stopped" | "unknown"
    const el = document.createElement('span');
    el.className = 'pill ' + (state === 'running' ? 'badge-ok'
      : state === 'stopped' ? 'badge-err' : '');
    el.textContent = state === 'running' ? '运行中'
      : state === 'stopped' ? '已停止' : '未知';
    return el.outerHTML;
  }

  function render(data) {
    if (data.panel) {
      set('pi-addr', `${data.panel.host}:${data.panel.port}`);
      set('pi-time', data.panel.time);
    }

    // Processes (Phase 2)
    if (data.processes) {
      for (const name of ['nonebot', 'napcat', 'searxng']) {
        const p = data.processes[name];
        if (!p) continue;
        set(`st-${name}`, statusPill(p.state));
        set(`st-${name}-sub`, p.sub || '');
      }
    }

    // Resources (Phase 2)
    if (data.resources) {
      const r = data.resources;
      for (const key of ['cpu', 'mem', 'disk']) {
        const v = r[key];
        if (v === undefined || v === null) continue;
        const bar = document.getElementById(`bar-${key}`);
        if (bar) bar.style.width = Math.min(100, Math.max(0, v)) + '%';
        set(`pct-${key}`, v.toFixed(1) + '%');
      }
      if (r.mem_used !== null && r.mem_used !== undefined) {
        set('res-mem', `${fmtBytes(r.mem_used)} / ${fmtBytes(r.mem_total)}`);
      }
      if (r.disk_used !== null && r.disk_used !== undefined) {
        set('res-disk', `${fmtBytes(r.disk_used)} / ${fmtBytes(r.disk_total)}`);
      }
      if (Array.isArray(r.load)) set('res-load', r.load.map(v => v.toFixed(2)).join(' / '));
      if (r.uptime) set('res-uptime', fmtUptime(r.uptime));
    }

    // Tokens (Phase 3)
    if (data.tokens) {
      set('st-tokens', fmtNum(data.tokens.today_tokens));
      if (data.tokens.hit_rate !== undefined && data.tokens.hit_rate !== null) {
        set('st-tokens-sub', `缓存命中率 ${(data.tokens.hit_rate * 100).toFixed(1)}%`);
      }
    }

    // Feedback (Phase 3)
    if (data.feedback) {
      set('st-feedback', data.feedback.unread);
      set('st-feedback-sub', `累计 ${data.feedback.total} 条`);
    }

    // Watchdog (Phase 2)
    if (data.watchdog) {
      set('pi-watchdog', data.watchdog.enabled ? '已开启' : '已关闭');
    }
  }

  async function refresh() {
    try {
      const data = await api('/api/dashboard');
      render(data);
    } catch (e) {
      /* api() already redirected on 401; other failures are transient */
    }
  }

  refresh();
  setInterval(refresh, REFRESH_MS);
})();
