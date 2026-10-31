/* Global helpers for the Roxy WebUI */

/* JSON fetch wrapper: throws Error(message) on non-2xx, returns parsed body */
async function api(path, options = {}) {
  const opts = Object.assign({ headers: {} }, options);
  if (opts.body && typeof opts.body !== 'string') {
    opts.body = JSON.stringify(opts.body);
  }
  if (opts.body) opts.headers['Content-Type'] = 'application/json';
  const resp = await fetch(path, opts);
  if (resp.status === 401) {
    window.location.href = '/login';
    throw new Error('未登录');
  }
  let data = null;
  try { data = await resp.json(); } catch (e) { /* empty body */ }
  if (!resp.ok) {
    throw new Error((data && data.error) || `请求失败 (HTTP ${resp.status})`);
  }
  return data;
}

/* Toast notifications */
function toast(message, kind = 'info', timeout = 3500) {
  const root = document.getElementById('toast-root');
  if (!root) { console.log(`[toast:${kind}]`, message); return; }
  const el = document.createElement('div');
  el.className = 'toast' + (kind === 'error' ? ' error' : kind === 'success' ? ' success' : '');
  el.textContent = message;
  root.appendChild(el);
  setTimeout(() => el.remove(), timeout);
}

/* Human-readable token / byte counts */
function fmtNum(n) {
  if (n === null || n === undefined) return '—';
  if (n >= 1e8) return (n / 1e8).toFixed(2) + ' 亿';
  if (n >= 1e4) return (n / 1e4).toFixed(1) + ' 万';
  return String(n);
}
function fmtBytes(bytes) {
  if (bytes === null || bytes === undefined) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + ' ' + units[i];
}
function fmtUptime(seconds) {
  if (!seconds || seconds < 0) return '—';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}天${h}时`;
  if (h > 0) return `${h}时${m}分`;
  return `${m}分钟`;
}

/* Logout button wiring (present in base.html) */
document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('btn-logout');
  if (btn) {
    btn.addEventListener('click', async () => {
      try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
      window.location.href = '/login';
    });
  }
  const clock = document.getElementById('clock');
  if (clock) {
    const tick = () => { clock.textContent = new Date().toLocaleString('zh-CN', { hour12: false }); };
    tick();
    setInterval(tick, 1000);
  }
});
