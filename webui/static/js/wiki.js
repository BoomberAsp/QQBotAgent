/* Wiki cache status page + alias manager */
(function () {
  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  /* esc() doesn't escape double quotes — needed for attribute values */
  function attr(s) { return esc(s).replace(/"/g, '&quot;'); }

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

  /* ── Alias manager ─────────────────────────────────────────── */

  let aliasKind = 'char';        // 'char' | 'bond'
  let aliasSnapshot = {};        // {canonical: [alias...]} 全量快照
  let aliasQuery = '';

  function cloneSnapshot() { return JSON.parse(JSON.stringify(aliasSnapshot)); }

  async function loadAliases() {
    const list = document.getElementById('wk-alias-list');
    list.innerHTML = '<div class="muted">加载中…</div>';
    try {
      aliasSnapshot = (await api('/api/wiki/aliases?kind=' + aliasKind)) || {};
      renderAliases();
    } catch (e) {
      list.innerHTML = `<div class="err">${esc(e.message)}</div>`;
    }
  }

  function renderAliases() {
    const list = document.getElementById('wk-alias-list');
    const canons = Object.keys(aliasSnapshot);
    const q = aliasQuery.trim().toLowerCase();
    const filtered = canons.filter(c => !q
      || c.toLowerCase().includes(q)
      || (aliasSnapshot[c] || []).some(a => a.toLowerCase().includes(q)));
    document.getElementById('wk-alias-stat').textContent =
      `共 ${canons.length} 组，显示 ${filtered.length} 组 · 修改名称输入框后点「保存本组」即重命名`;
    if (!filtered.length) {
      list.innerHTML = '<div class="muted">无匹配分组</div>';
      return;
    }
    list.innerHTML = filtered.map(c => {
      const aliases = aliasSnapshot[c] || [];
      const chips = aliases.map(a =>
        `<span class="pill alias-chip">${esc(a)}<span class="x" data-canon="${attr(c)}" data-alias="${attr(a)}" title="删除别名">×</span></span>`
      ).join('');
      return `<div class="alias-group" data-canon="${attr(c)}">
        <div class="row">
          <input class="alias-canon" value="${attr(c)}" title="修改后点保存即重命名该组">
          <span class="muted">${aliases.length} 条别名</span>
          <span class="spacer"></span>
          <input class="alias-add" placeholder="+ 新别名，回车添加">
          <button class="btn btn-sm alias-save">保存本组</button>
          <button class="btn btn-sm btn-danger alias-del-group">删除组</button>
        </div>
        <div class="alias-chips">${chips || '<span class="muted">（无别名）</span>'}</div>
      </div>`;
    }).join('');
  }

  async function putAliases(nextSnapshot, okMsg) {
    try {
      const r = await api('/api/wiki/aliases', {
        method: 'PUT',
        body: { kind: aliasKind, aliases: nextSnapshot },
      });
      if (r && r.warning) toast(r.warning, 'info', 6000);
      else toast(okMsg || '已保存', 'success');
      await loadAliases();
    } catch (e) {
      toast(e.message, 'error', 5000);
    }
  }

  /* chip × → 删除单条别名（confirm 后立即保存） */
  document.getElementById('wk-alias-list').addEventListener('click', async (ev) => {
    const x = ev.target.closest('.x');
    if (x) {
      const canon = x.dataset.canon, alias = x.dataset.alias;
      if (!confirm(`删除「${canon}」的别名「${alias}」？`)) return;
      const next = cloneSnapshot();
      next[canon] = (next[canon] || []).filter(a => a !== alias);
      await putAliases(next, '别名已删除');
      return;
    }
    const group = ev.target.closest('.alias-group');
    if (!group) return;
    const canon = group.dataset.canon;

    if (ev.target.closest('.alias-save')) {
      const newCanon = group.querySelector('.alias-canon').value.trim();
      if (!newCanon) { toast('名称不能为空', 'error'); return; }
      let next;
      if (newCanon === canon) {
        next = cloneSnapshot();
      } else {
        if (Object.prototype.hasOwnProperty.call(aliasSnapshot, newCanon)) {
          toast(`「${newCanon}」组已存在，请先合并`, 'error', 5000);
          return;
        }
        next = {};
        for (const [k, v] of Object.entries(aliasSnapshot)) {
          if (k === canon) next[newCanon] = v;
          else next[k] = v;
        }
      }
      await putAliases(next, '已保存');
      return;
    }

    if (ev.target.closest('.alias-del-group')) {
      const n = (aliasSnapshot[canon] || []).length;
      if (!confirm(`删除整组「${canon}」及其 ${n} 条别名？此操作不可撤销。`)) return;
      const next = cloneSnapshot();
      delete next[canon];
      await putAliases(next, '分组已删除');
    }
  });

  /* 新别名输入框回车 → 添加并保存 */
  document.getElementById('wk-alias-list').addEventListener('keydown', async (ev) => {
    if (ev.key !== 'Enter' || !ev.target.classList.contains('alias-add')) return;
    const group = ev.target.closest('.alias-group');
    if (!group) return;
    const canon = group.dataset.canon;
    const alias = ev.target.value.trim();
    if (!alias) return;
    const next = cloneSnapshot();
    if ((next[canon] || []).includes(alias)) {
      toast('该别名已存在于此组', 'error');
      return;
    }
    (next[canon] = next[canon] || []).push(alias);
    await putAliases(next, '别名已添加');
  });

  /* 角色/羁绊切换 */
  document.querySelectorAll('.wk-kind').forEach(btn => {
    btn.addEventListener('click', () => {
      if (aliasKind === btn.dataset.kind) return;
      aliasKind = btn.dataset.kind;
      document.querySelectorAll('.wk-kind').forEach(b =>
        b.classList.toggle('active', b === btn));
      loadAliases();
    });
  });

  /* 搜索过滤（纯前端） */
  document.getElementById('wk-alias-q').addEventListener('input', (ev) => {
    aliasQuery = ev.target.value;
    renderAliases();
  });

  loadAliases();
})();
