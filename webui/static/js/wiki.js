/* Wiki cache status page */
(function () {
  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function fmtTs(t) {
    if (!t) return '—';
    return new Date(t * 1000).toLocaleString('zh-CN', { hour12: false });
  }

  function renderCard(el, s, title) {
    if (!s || !s.exists) {
      el.innerHTML = `<h2 class="card-title">${title}</h2>
        <p class="muted">目录不存在或无法读取</p>`;
      return;
    }
    el.innerHTML = `<h2 class="card-title">${title}</h2>
      <table class="kv-table">
        <tr><td>文件数</td><td class="mono">${s.files}</td></tr>
        <tr><td>总大小</td><td class="mono">${fmtBytes(s.size)}</td></tr>
        <tr><td>JSON 条目</td><td class="mono">${s.json_entries}</td></tr>
        <tr><td>最近更新</td><td class="mono">${fmtTs(s.newest_mtime)}</td></tr>
      </table>`;
  }

  async function load() {
    const wiki = document.getElementById('wk-wiki');
    const redeem = document.getElementById('wk-redeem');
    wiki.innerHTML = '<div class="muted">加载中…</div>';
    redeem.innerHTML = '<div class="muted">加载中…</div>';
    try {
      const d = await api('/api/wiki/cache-status');
      document.getElementById('wk-root').textContent = d.root || '';
      renderCard(wiki, d.wiki_cache, 'wiki_cache（角色/羁绊资料）');
      renderCard(redeem, d.redeem_code, 'redeem_code（兑换码缓存）');
    } catch (e) {
      wiki.innerHTML = `<div class="warn">${esc(e.message)}</div>`;
      redeem.innerHTML = '';
    }
  }

  document.getElementById('wk-refresh').addEventListener('click', load);
  load();
})();
