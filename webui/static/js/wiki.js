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
      loadMissing();   // 保存可能使「待补录」条目落组，同步刷新
    } catch (e) {
      toast(e.message, 'error', 5000);
    }
  }

  /* ── 待补录（已爬取但字典无分组）+ 新建分组 ───────────────── */

  let missingItems = [];
  let missingNote = '';

  async function loadMissing() {
    const box = document.getElementById('wk-alias-missing');
    try {
      const d = await api('/api/wiki/aliases/missing?kind=' + aliasKind);
      missingItems = (d && d.items) || [];
      missingNote = (d && d.note) || '';
      renderMissing();
    } catch (e) {
      missingItems = [];
      missingNote = '';
      box.innerHTML = `<div class="warn" style="margin-top:8px">${esc(e.message)}</div>`;
    }
  }

  function renderMissing() {
    const box = document.getElementById('wk-alias-missing');
    if (!missingItems.length) {
      box.innerHTML = missingNote
        ? `<p class="muted" style="margin:8px 0 0">${esc(missingNote)}</p>` : '';
      return;
    }
    const what = aliasKind === 'char' ? '角色' : '羁绊';
    box.innerHTML = `<div class="alias-group" style="margin-top:10px">
      <div class="row">
        <strong>待补录（${missingItems.length}）</strong>
        <span class="muted">已从 wiki 爬取、但字典中尚无别名分组的${what}。
          按译名一键创建分组（预填自别名 + 英文标题）；若译名用字不妥，创建后可在下方卡片中改名。</span>
      </div>
      ${missingItems.map((it, i) => `<div class="row" style="margin-top:6px">
        <span class="mono muted">${esc(it.title_en)}</span>
        <span>→ ${esc(it.canonical)}</span>
        <span class="spacer"></span>
        <button class="btn btn-sm missing-create" data-i="${i}">补录为分组</button>
      </div>`).join('')}
    </div>`;
  }

  /* 创建新分组：先以磁盘最新快照为基准刷新，避免陈旧快照全量覆盖 */
  async function createGroup(canon, hintAliases) {
    canon = String(canon || '').trim();
    if (!canon) { toast('名称不能为空', 'error'); return; }
    await loadAliases();
    if (Object.prototype.hasOwnProperty.call(aliasSnapshot, canon)) {
      toast(`「${canon}」组已存在`, 'error', 5000);
      return;
    }
    const aliases = [...new Set(
      (Array.isArray(hintAliases) && hintAliases.length ? hintAliases : [canon])
        .map(a => String(a).trim()).filter(Boolean))];
    if (!aliases.includes(canon)) aliases.unshift(canon);  // 自别名约定 canon→canon
    const next = cloneSnapshot();
    next[canon] = aliases;
    await putAliases(next, `已创建分组「${canon}」`);
  }

  document.getElementById('wk-alias-missing').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('.missing-create');
    if (!btn) return;
    const it = missingItems[Number(btn.dataset.i)];
    if (!it) return;
    await createGroup(it.canonical, it.aliases_hint);
  });

  document.getElementById('wk-alias-new').addEventListener('click', async () => {
    const canon = prompt('新分组的规范名称（角色/羁绊中文名）：');
    if (canon === null) return;
    await createGroup(canon, [canon]);
  });

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
      loadMissing();
    });
  });

  /* 搜索过滤（纯前端） */
  document.getElementById('wk-alias-q').addEventListener('input', (ev) => {
    aliasQuery = ev.target.value;
    renderAliases();
  });

  loadAliases();
  loadMissing();

  /* ── 翻译术语表编辑器 ─────────────────────────────────────── */

  let glData = {};      // {table: pairs|map|map_flag|map_int 值}
  let glShapes = {};    // {table: shape}
  let glLabels = {};    // {table: 中文标签}
  let glActive = '';

  async function loadGlossary() {
    const ed = document.getElementById('wk-gl-editor');
    ed.innerHTML = '<div class="muted">加载中…</div>';
    try {
      const d = await api('/api/wiki/glossary');
      glData = d.glossary; glShapes = d.shapes; glLabels = d.labels;
      const src = document.getElementById('wk-gl-source');
      src.textContent = d.source === 'file' ? '来源: 文件' : '来源: 内置默认';
      src.className = 'pill ' + (d.source === 'file' ? 'badge-ok' : 'badge-warn');
      if (!glActive || !(glActive in glData)) glActive = Object.keys(glData)[0] || '';
      renderGlTabs();
      renderGlEditor();
    } catch (e) {
      ed.innerHTML = `<div class="err">${esc(e.message)}</div>`;
    }
  }

  function renderGlTabs() {
    document.getElementById('wk-gl-tabs').innerHTML = Object.keys(glData).map(t => {
      const n = glShapes[t] === 'pairs' ? glData[t].length : Object.keys(glData[t]).length;
      return `<button class="tab gl-tab${t === glActive ? ' active' : ''}" data-t="${attr(t)}">${esc(glLabels[t] || t)} <span class="muted">${n}</span></button>`;
    }).join('');
  }

  function renderGlEditor() {
    const ed = document.getElementById('wk-gl-editor');
    const t = glActive;
    if (!t) { ed.innerHTML = '<div class="muted">无表</div>'; return; }
    const shape = glShapes[t], v = glData[t];
    const head = `<div class="row" style="margin-top:10px">
      <span class="muted mono">${attr(t)}</span>
      <span class="spacer"></span>
      <button class="btn btn-sm gl-add">+ 添加行</button></div>`;
    let rows = '';
    if (shape === 'pairs') {
      rows = v.map((p, i) => `<tr>
        <td class="gl-en"><input type="text" class="gl-en-in" value="${attr(p[0])}" placeholder="English term"></td>
        <td class="gl-cn"><input type="text" class="gl-cn-in" value="${attr(p[1])}" placeholder="中文"></td>
        <td class="gl-row-btns">
          <button class="btn btn-sm btn-ghost gl-up" title="上移（提高替换优先级）"${i === 0 ? ' disabled' : ''}>↑</button>
          <button class="btn btn-sm btn-ghost gl-down" title="下移"${i === v.length - 1 ? ' disabled' : ''}>↓</button>
          <button class="btn btn-sm btn-ghost gl-del" title="删除行">×</button>
        </td></tr>`).join('');
    } else {
      rows = Object.entries(v).map(([k, val]) => {
        let valCell;
        if (shape === 'map_flag') {
          valCell = `<input type="text" class="gl-cn-in" value="${attr((val || [])[0])}">
            <label style="white-space:nowrap;margin-left:6px"><input type="checkbox" class="gl-flag"${(val || [])[1] ? ' checked' : ''}> 百分比</label>`;
        } else if (shape === 'map_int') {
          valCell = `<input type="text" class="gl-int-in" value="${attr(val)}" style="width:100px">`;
        } else {
          valCell = `<input type="text" class="gl-cn-in" value="${attr(val)}">`;
        }
        return `<tr>
          <td class="gl-en"><input type="text" class="gl-key-in" value="${attr(k)}"></td>
          <td class="gl-cn">${valCell}</td>
          <td class="gl-row-btns"><button class="btn btn-sm btn-ghost gl-del" title="删除行">×</button></td>
        </tr>`;
      }).join('');
    }
    ed.innerHTML = head + `<table class="gl-table"><tbody>${rows}</tbody></table>`;
  }

  /* 把当前编辑器 DOM 的输入值同步回 glData[glActive]（重渲/保存前调用） */
  function syncGlActive() {
    const t = glActive;
    if (!t || !glShapes[t]) return;
    const shape = glShapes[t];
    const trs = [...document.querySelectorAll('#wk-gl-editor tbody tr')];
    if (!trs.length && !document.querySelector('#wk-gl-editor tbody')) return;
    if (shape === 'pairs') {
      glData[t] = trs.map(tr => [
        tr.querySelector('.gl-en-in').value,
        tr.querySelector('.gl-cn-in').value,
      ]);
    } else if (shape === 'map') {
      const o = {};
      for (const tr of trs) o[tr.querySelector('.gl-key-in').value] = tr.querySelector('.gl-cn-in').value;
      glData[t] = o;
    } else if (shape === 'map_flag') {
      const o = {};
      for (const tr of trs) o[tr.querySelector('.gl-key-in').value] =
        [tr.querySelector('.gl-cn-in').value, tr.querySelector('.gl-flag').checked];
      glData[t] = o;
    } else if (shape === 'map_int') {
      const o = {};
      for (const tr of trs) {
        const raw = tr.querySelector('.gl-int-in').value.trim();
        const n = Number(raw);
        o[tr.querySelector('.gl-key-in').value] = (raw !== '' && Number.isInteger(n)) ? n : raw;
      }
      glData[t] = o;
    }
  }

  function glValidate() {
    for (const t of Object.keys(glData)) {
      const shape = glShapes[t], v = glData[t];
      const label = glLabels[t] || t;
      if (shape === 'pairs') {
        if (!Array.isArray(v) || !v.length) return `${label}: 不能为空表`;
        for (let i = 0; i < v.length; i++) {
          if (!String(v[i][0]).trim() || !String(v[i][1]).trim()) {
            return `${label} 第 ${i + 1} 行: 英文与中文均不能为空`;
          }
        }
      } else {
        const keys = Object.keys(v || {});
        if (!keys.length) return `${label}: 不能为空表`;
        for (const k of keys) {
          if (!k.trim()) return `${label}: 存在空键`;
          const val = v[k];
          if (shape === 'map' && !String(val).trim()) return `${label}[${k}]: 值不能为空`;
          if (shape === 'map_flag' && (!Array.isArray(val) || !String(val[0] || '').trim())) {
            return `${label}[${k}]: 中文不能为空`;
          }
          if (shape === 'map_int' && !Number.isInteger(val)) return `${label}[${k}]: 必须是整数`;
        }
      }
    }
    return null;
  }

  document.getElementById('wk-gl-tabs').addEventListener('click', ev => {
    const btn = ev.target.closest('.gl-tab');
    if (!btn || btn.dataset.t === glActive) return;
    syncGlActive();
    glActive = btn.dataset.t;
    renderGlTabs();
    renderGlEditor();
  });

  document.getElementById('wk-gl-editor').addEventListener('click', ev => {
    const t = glActive;
    if (!t) return;
    const shape = glShapes[t];

    if (ev.target.closest('.gl-add')) {
      syncGlActive();
      if (shape === 'pairs') glData[t].push(['', '']);
      else if (shape === 'map_flag') glData[t][''] = ['', false];
      else if (shape === 'map_int') glData[t][''] = 0;
      else glData[t][''] = '';
      renderGlEditor();
      const last = document.querySelector('#wk-gl-editor tbody tr:last-child input');
      if (last) last.focus();
      return;
    }

    const tr = ev.target.closest('tr');
    if (!tr || !tr.parentElement) return;
    const idx = [...tr.parentElement.children].indexOf(tr);

    if (ev.target.closest('.gl-del')) {
      syncGlActive();
      if (shape === 'pairs') {
        glData[t].splice(idx, 1);
      } else {
        const keys = Object.keys(glData[t]);
        keys.splice(idx, 1);
        const o = {};
        for (const k of keys) o[k] = glData[t][k];
        glData[t] = o;
      }
      renderGlEditor();
      return;
    }

    const up = ev.target.closest('.gl-up');
    const down = ev.target.closest('.gl-down');
    if ((up || down) && shape === 'pairs') {
      syncGlActive();
      const j = up ? idx - 1 : idx + 1;
      if (j < 0 || j >= glData[t].length) return;
      const arr = glData[t];
      [arr[idx], arr[j]] = [arr[j], arr[idx]];
      renderGlEditor();
    }
  });

  document.getElementById('wk-gl-save').addEventListener('click', async () => {
    syncGlActive();
    const err = glValidate();
    if (err) { toast(err, 'error', 6000); return; }
    try {
      const r = await api('/api/wiki/glossary', { method: 'PUT', body: { tables: glData } });
      toast(`术语表已保存（${r.tables} 张）— bot 热读即时生效`, 'success', 5000);
      await loadGlossary();
    } catch (e) {
      toast(e.message, 'error', 6000);
    }
  });

  document.getElementById('wk-gl-restore').addEventListener('click', async () => {
    if (!confirm('用内置默认覆盖术语表文件？当前文件中的全部修改将丢失（旧文件保留 .bak 备份）。')) return;
    try {
      const d = await api('/api/wiki/glossary/default');
      const r = await api('/api/wiki/glossary', { method: 'PUT', body: { tables: d.glossary } });
      toast(`已恢复内置默认（${r.tables} 张）`, 'success', 5000);
      await loadGlossary();
    } catch (e) {
      toast(e.message, 'error', 6000);
    }
  });

  loadGlossary();

  /* ── 详情编辑（人工修正 / 整条 LLM 重翻译）─────────────────── */

  let dtKind = 'char';
  let dtItems = [];
  let dtQuery = '';
  let dtSelected = '';
  let dtEntry = null;
  let dtPollTimer = null;

  async function loadDetails() {
    const list = document.getElementById('wk-dt-list');
    list.innerHTML = '<div class="muted">加载中…</div>';
    try {
      const d = await api(`/api/wiki/details?kind=${dtKind}&q=${encodeURIComponent(dtQuery)}`);
      dtItems = (d && d.items) || [];
      if (d && d.note && !dtItems.length) {
        list.innerHTML = `<div class="muted">${esc(d.note)}</div>`;
        return;
      }
      renderDtList(d && d.total);
    } catch (e) {
      list.innerHTML = `<div class="err">${esc(e.message)}</div>`;
    }
  }

  function renderDtList(total) {
    const list = document.getElementById('wk-dt-list');
    document.getElementById('wk-dt-status').textContent =
      `共 ${total != null ? total : dtItems.length} 条` +
      (dtQuery ? `，过滤「${dtQuery}」后 ${dtItems.length} 条` : '');
    if (!dtItems.length) {
      list.innerHTML = '<div class="muted">无匹配条目</div>';
      return;
    }
    list.innerHTML = dtItems.map(it => `<div class="dt-item${it.title === dtSelected ? ' active' : ''}" data-title="${attr(it.title)}">
      <div><strong>${esc(it.name_cn || '(未翻译)')}</strong>${it.manual ? ' <span class="pill manual-pill" title="含人工修正字段">人工</span>' : ''}${it.has_cn ? '' : ' <span class="pill badge-warn" title="中文字段仍是英文原文">英</span>'}</div>
      <div class="t2 mono muted">${esc(it.title)}${it.id ? ' · ' + esc(it.id) : ''}${it.stars ? ' · ' + '★'.repeat(it.stars) : ''}</div>
    </div>`).join('');
  }

  function manualSet() { return new Set((dtEntry && dtEntry._manual_fields) || []); }

  function dtField(path, label, value, rows) {
    const man = manualSet().has(path) ? ' <span class="pill manual-pill">人工</span>' : '';
    const inp = rows
      ? `<textarea rows="${rows}" data-path="${attr(path)}">${esc(value || '')}</textarea>`
      : `<input type="text" data-path="${attr(path)}" value="${attr(value || '')}">`;
    return `<div class="dt-field"><label>${esc(label)}${man}</label>${inp}</div>`;
  }

  function renderDtEditor() {
    const ed = document.getElementById('wk-dt-editor');
    const e = dtEntry;
    let html = `<div class="row">
      <strong>${esc(e.name_cn || e.title || '')}</strong>
      <span class="mono muted">${esc(e.title || '')}${e.id ? ' · ' + esc(e.id) : ''}</span>
      <span class="spacer"></span>
      <button class="btn btn-sm btn-primary" id="wk-dt-save">保存人工修正</button>
      <button class="btn btn-sm" id="wk-dt-retrans" title="重置中文字段后整条重走翻译管线（术语表 + 名字解析链生效）">整条 LLM 重翻译</button>
    </div>`;
    if (dtKind === 'char') {
      html += dtField('name_cn', '中文名 (name_cn)', e.name_cn);
      html += dtField('desc', '简介 (desc)', e.desc, 3);
      const discs = Array.isArray(e.discs) ? e.discs : [];
      html += `<div class="dt-field"><label>天赋 discs（每条一个文本框，自上而下）${manualSet().has('discs') ? ' <span class="pill manual-pill">人工</span>' : ''}</label>`;
      html += (discs.length ? discs : ['']).map((d, i) =>
        `<textarea rows="2" data-path="discs" data-idx="${i}" style="margin-bottom:4px">${esc(d)}</textarea>`).join('');
      html += `<button class="btn btn-sm btn-ghost" id="wk-dt-disc-add" style="margin-top:2px">+ 添加天赋</button></div>`;
      (e.skills || []).forEach((sk, i) => {
        html += `<div class="dt-skill">
          <div class="row" style="margin-bottom:6px"><strong>技能 ${i + 1}</strong>
            <span class="muted">${esc(sk.type || '')}${sk.soul ? ' · 星尘 ' + esc(String(sk.soul)) : ''}${sk.cd ? ' · 冷却 ' + esc(String(sk.cd)) : ''}</span></div>`;
        html += dtField(`skills.${i}.name`, '名称', sk.name);
        html += dtField(`skills.${i}.des`, '描述 (des)', sk.des, 4);
        html += dtField(`skills.${i}.des2`, '强化前 (des2)', sk.des2, 3);
        html += dtField(`skills.${i}.burst`, 'Burst', sk.burst, 2);
        html += `</div>`;
      });
    } else {
      html += dtField('name_cn', '中文名 (name_cn)', e.name_cn);
      html += dtField('desc', '简介 (desc)', e.desc, 3);
      html += dtField('effect', '羁绊技能 (effect)', e.effect, 4);
      html += dtField('notes', '备注 (notes)', e.notes, 2);
      html += dtField('obtain', '获取方式 (obtain)', e.obtain, 2);
    }
    ed.innerHTML = html;
  }

  async function selectEntry(title) {
    dtSelected = title;
    renderDtList();
    const ed = document.getElementById('wk-dt-editor');
    ed.innerHTML = '<div class="muted">加载中…</div>';
    try {
      const d = await api(`/api/wiki/details/entry?kind=${dtKind}&title=${encodeURIComponent(title)}`);
      dtEntry = d.entry;
      renderDtEditor();
    } catch (e) {
      dtEntry = null;
      ed.innerHTML = `<div class="err">${esc(e.message)}</div>`;
    }
  }

  function getByPath(obj, path) {
    return path.split('.').reduce((o, k) => (o == null ? o : o[k]), obj);
  }

  /* 与 dtEntry 原值逐字段对比，仅收集发生变化的白名单路径 */
  function collectChanged() {
    const fields = {};
    const ed = document.getElementById('wk-dt-editor');
    ed.querySelectorAll('input[data-path], textarea[data-path]').forEach(inp => {
      if (inp.dataset.path === 'discs') return;
      const orig = getByPath(dtEntry, inp.dataset.path);
      if (inp.value !== (orig == null ? '' : String(orig))) fields[inp.dataset.path] = inp.value;
    });
    const tas = [...ed.querySelectorAll('textarea[data-path="discs"]')];
    if (tas.length) {
      const arr = tas.map(t => t.value);
      const orig = Array.isArray(dtEntry.discs) ? dtEntry.discs : [];
      if (arr.length !== orig.length
          || arr.some((v, i) => v !== (orig[i] == null ? '' : String(orig[i])))) {
        fields['discs'] = arr;
      }
    }
    return fields;
  }

  async function saveManual() {
    const fields = collectChanged();
    if (!Object.keys(fields).length) { toast('没有检测到修改', 'info'); return; }
    try {
      const r = await api('/api/wiki/details/entry', {
        method: 'PUT',
        body: { kind: dtKind, title: dtSelected, fields },
      });
      toast(`已保存 ${r.applied} 个字段 — bot 侧 mtime 热读即时生效`, 'success', 5000);
      await selectEntry(dtSelected);   // 刷新「人工」徽标
      await loadDetails();
    } catch (e) {
      toast(e.message, 'error', 6000);
    }
  }

  async function startRetrans() {
    const man = (dtEntry && dtEntry._manual_fields) || [];
    const warn = man.length
      ? `\n\n注意：该条目含 ${man.length} 个人工修正字段（${man.join('、')}），重翻译会将其全部覆盖为 LLM 译文！`
      : '';
    if (!confirm(`对「${(dtEntry && dtEntry.name_cn) || dtSelected}」整条重新 LLM 翻译？\n将重置全部中文字段并重走翻译管线（术语表 + 名字解析链 + LLM）。${warn}`)) return;
    try {
      await api('/api/wiki/details/retranslate', {
        method: 'POST', body: { kind: dtKind, title: dtSelected },
      });
      pollRetrans();
    } catch (e) {
      toast(e.message, 'error', 6000);
    }
  }

  function pollRetrans() {
    const st = document.getElementById('wk-dt-status');
    const kind = dtKind, title = dtSelected;
    if (dtPollTimer) clearInterval(dtPollTimer);
    const t0 = Date.now();
    const setBtn = dis => {
      const b = document.getElementById('wk-dt-retrans');
      if (b) b.disabled = dis;
    };
    setBtn(true);
    dtPollTimer = setInterval(async () => {
      let s;
      try {
        s = await api(`/api/wiki/details/retranslate/status?kind=${kind}&title=${encodeURIComponent(title)}`);
      } catch (e) { return; }
      if (s.state === 'running') {
        st.textContent = `重翻译运行中…（已 ${Math.round((Date.now() - t0) / 1000)}s）`;
        return;
      }
      clearInterval(dtPollTimer);
      dtPollTimer = null;
      st.textContent = s.state === 'done'
        ? `重翻译完成（耗时 ${s.elapsed}s）` : '';
      setBtn(false);
      if (s.state === 'done') {
        toast(s.message || '重翻译完成', 'success', 5000);
        if (dtKind === kind && dtSelected === title) await selectEntry(title);
        await loadDetails();
      } else if (s.state === 'error') {
        toast('重翻译失败: ' + (s.message || ''), 'error', 8000);
      }
    }, 2000);
  }

  document.getElementById('wk-dt-list').addEventListener('click', ev => {
    const item = ev.target.closest('.dt-item');
    if (item) selectEntry(item.dataset.title);
  });

  document.getElementById('wk-dt-editor').addEventListener('click', async ev => {
    if (ev.target.closest('#wk-dt-disc-add')) {
      const box = ev.target.closest('.dt-field');
      const ta = document.createElement('textarea');
      ta.rows = 2;
      ta.dataset.path = 'discs';
      ta.style.marginBottom = '4px';
      box.insertBefore(ta, ev.target.closest('button'));
      return;
    }
    if (ev.target.closest('#wk-dt-save')) { await saveManual(); return; }
    if (ev.target.closest('#wk-dt-retrans')) { await startRetrans(); }
  });

  let dtDebounce = null;
  document.getElementById('wk-dt-q').addEventListener('input', ev => {
    clearTimeout(dtDebounce);
    dtDebounce = setTimeout(() => {
      dtQuery = ev.target.value.trim();
      loadDetails();
    }, 300);
  });

  document.querySelectorAll('.wk-dt-kind').forEach(btn => {
    btn.addEventListener('click', () => {
      if (dtKind === btn.dataset.kind) return;
      dtKind = btn.dataset.kind;
      document.querySelectorAll('.wk-dt-kind').forEach(b =>
        b.classList.toggle('active', b === btn));
      dtSelected = '';
      dtEntry = null;
      if (dtPollTimer) { clearInterval(dtPollTimer); dtPollTimer = null; }
      document.getElementById('wk-dt-status').textContent = '';
      document.getElementById('wk-dt-editor').innerHTML =
        '<span class="muted">← 选择左侧条目进行编辑</span>';
      loadDetails();
    });
  });

  loadDetails();
})();
