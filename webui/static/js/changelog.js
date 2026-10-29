/* 更新日志与公告 — 双 Tab（proactive-mail-design.md §16.3）
 *
 * Tab 1 更新记录：CRUD over QQBot/data/changelog/changelog.json + 群发；
 * Tab 2 公告：CRUD over announcements.json（id 服务端生成，按 id 定位）+ 群发。
 *
 * 与 bot 解耦：面板只写文件（changelog/announcements/pending_broadcast），
 * bot 后台轮询器负责实际群发（OneBot + 官方双渠道）并回写 broadcast_status.json。
 * 当前 Tab 记忆在 URL hash（#changelog / #announcement）。
 */

let entries = [];
let changeTypes = ['新增', '修复', '优化', '调整', '移除'];
let annEntries = [];
let annFormats = ['text', 'markdown'];
let groups = [];          // [{channel,id,name,sendable,reason,…}]（两个 Tab 共用）
let dirty = false;
let annDirty = false;
let statusTimer = null;

const $ = (id) => document.getElementById(id);
const today = () => new Date().toISOString().slice(0, 10);

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} `
    + `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/* ── Tab 切换（URL hash 记忆）── */

function switchTab(name) {
  if (name !== 'announcement') name = 'changelog';
  $('tab-changelog').style.display = name === 'changelog' ? '' : 'none';
  $('tab-announcement').style.display = name === 'announcement' ? '' : 'none';
  document.querySelectorAll('#cl-tabs .tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.tab === name);
  });
  if (location.hash !== '#' + name) {
    history.replaceState(null, '', '#' + name);
  }
}

function setDirty(v) {
  dirty = v;
  $('cl-dirty').style.display = v ? '' : 'none';
}

function setAnnDirty(v) {
  annDirty = v;
  $('ann-dirty').style.display = v ? '' : 'none';
}

function entryLabel(e, i) {
  const v = e.version ? e.version : '(无版本)';
  const d = e.date ? '  ' + e.date : '';
  return `#${i + 1}  ${v}${d}`;
}

/* ── 更新记录编辑列表 ── */

function render() {
  const box = $('cl-list');
  if (!entries.length) {
    box.innerHTML = '<p class="muted">（空）点右上角「+ 新增记录」添加第一条更新记录。</p>';
  } else {
    box.innerHTML = entries.map((e, i) => {
      const changes = (e.changes || []).map((c, ci) => `
        <div class="row" style="gap:6px;align-items:center;margin:4px 0">
          <select data-eidx="${i}" data-cidx="${ci}" data-role="ctype" style="width:92px">
            ${changeTypes.map((t) => `<option value="${t}"${t === (c.type || '') ? ' selected' : ''}>${t}</option>`).join('')}
          </select>
          <input type="text" data-eidx="${i}" data-cidx="${ci}" data-role="ctext"
                 value="${esc(c.text || '')}" placeholder="变更内容" style="flex:1;min-width:220px">
          <button class="btn btn-danger btn-sm" data-del-change="${i}:${ci}">×</button>
        </div>`).join('');
      return `
      <div class="card" style="margin:0 0 10px;padding:12px">
        <div class="row" style="gap:8px;align-items:flex-end">
          <div class="field" style="margin:0">
            <span>版本号</span>
            <input type="text" data-eidx="${i}" data-role="version"
                   value="${esc(e.version || '')}" placeholder="2.14" style="width:120px">
          </div>
          <div class="field" style="margin:0">
            <span>日期</span>
            <input type="text" data-eidx="${i}" data-role="date"
                   value="${esc(e.date || '')}" placeholder="${today()}" style="width:150px">
          </div>
          <span class="spacer"></span>
          ${e.broadcast_at ? '<span class="pill badge-ok">已群发</span>' : ''}
          <button class="btn btn-danger btn-sm" data-del-entry="${i}">删除记录</button>
        </div>
        <div style="margin-top:8px">
          <div class="muted" style="margin-bottom:2px">变更条目</div>
          ${changes || '<p class="muted" style="margin:4px 0">（无）</p>'}
          <button class="btn btn-sm" data-add-change="${i}" style="margin-top:6px">+ 添加变更条目</button>
        </div>
      </div>`;
    }).join('');
  }
  $('cl-stat').textContent = `共 ${entries.length} 条更新记录`;
  $('cl-meta').textContent = `${entries.length} 条`;
}

/* ── 公告编辑列表（§16.3.3）── */

function annSentPill(e) {
  if (!e.id) return '<span class="pill badge-warn">未保存</span>';
  if (e.sent_at) return `<span class="pill badge-ok">已发送 ${esc(fmtTime(e.sent_at))}</span>`;
  return '<span class="pill">未发送</span>';
}

function renderAnn() {
  const box = $('ann-list');
  if (!annEntries.length) {
    box.innerHTML = '<p class="muted">（空）点右上角「+ 新增公告」创建第一条公告。</p>';
  } else {
    box.innerHTML = annEntries.map((e, i) => `
      <div class="card" style="margin:0 0 10px;padding:12px">
        <div class="row" style="gap:8px;align-items:flex-end">
          <div class="field" style="margin:0;flex:1;min-width:220px">
            <span>标题 <b class="muted mono" data-ann-tcount="${i}">${(e.title || '').length}/60</b></span>
            <input type="text" data-aidx="${i}" data-role="title" maxlength="60"
                   value="${esc(e.title || '')}" placeholder="公告标题（≤60 字）" style="width:100%">
          </div>
          <div class="field" style="margin:0">
            <span>格式</span>
            <select data-aidx="${i}" data-role="format" style="width:120px">
              ${annFormats.map((f) => `<option value="${f}"${f === (e.format || 'text') ? ' selected' : ''}>${f === 'text' ? '纯文本' : 'markdown'}</option>`).join('')}
            </select>
          </div>
          <span class="spacer"></span>
          ${annSentPill(e)}
          <button class="btn btn-danger btn-sm" data-del-ann="${i}">删除公告</button>
        </div>
        <div class="field" style="margin:8px 0 0">
          <span>正文 <b class="muted mono" data-ann-bcount="${i}">${(e.body || '').length}/2000</b></span>
          <textarea data-aidx="${i}" data-role="body" rows="5" maxlength="2000"
                    placeholder="公告正文（≤2000 字）；markdown 格式仅在官方群生效，OneBot 群回退纯文本"
                    style="width:100%">${esc(e.body || '')}</textarea>
        </div>
        <details style="margin-top:6px">
          <summary class="muted" style="cursor:pointer">预览（发送效果）</summary>
          <pre class="muted" data-ann-preview="${i}" style="white-space:pre-wrap;margin:6px 0 0">${esc(annPreviewText(e))}</pre>
        </details>
      </div>`).join('');
  }
  $('ann-stat').textContent = `共 ${annEntries.length} 条公告`;
  $('ann-meta').textContent = `${annEntries.length} 条`;
}

function annPreviewText(e) {
  return `📢 公告\n${e.title || ''}\n\n${e.body || ''}`;
}

function renderAnnBroadcast() {
  const sel = $('ann-bc-entry');
  const prev = sel.value;
  sel.innerHTML = annEntries.length
    ? annEntries.map((e) => `<option value="${esc(e.id || '')}">${esc((e.title || '(无标题)') + (e.id ? '' : '  [未保存]'))}</option>`).join('')
    : '<option value="">（无已保存公告）</option>';
  if (prev && annEntries.some((e) => e.id === prev)) sel.value = prev;
}

/* ── 群选择器（两个 Tab 共用数据，按渠道分组，§16.5.1）── */

function renderGroupPicker(boxId, metaId, cls) {
  const box = $(boxId);
  if (!groups.length) {
    box.innerHTML = '<span class="muted">（暂无群列表 — 确认 bot 已连接，或点「刷新群列表」；也可勾选「全部群」由 bot 发送时解析）</span>';
    $(metaId).textContent = '';
    return;
  }
  const onebot = groups.filter((g) => g.channel !== 'official');
  const official = groups.filter((g) => g.channel === 'official');
  const one = (g) => {
    const dis = g.sendable === false;
    const meta = g.channel === 'official'
      ? `<span class="muted mono">${esc(g.id.slice(0, 10))}…</span>`
      : `<span class="muted mono">(${esc(g.id)})</span>`;
    return `
      <label class="row" style="gap:4px;align-items:center;margin:0;${dis ? 'opacity:.45' : ''}" title="${esc(g.reason || '')}">
        <input type="checkbox" class="${cls}" data-channel="${esc(g.channel)}" value="${esc(g.id)}"${dis ? ' disabled' : ''}>
        <span>${esc(g.name) || esc(g.id)} ${meta}${dis && g.reason ? ` <span class="warn">${esc(g.reason)}</span>` : ''}</span>
      </label>`;
  };
  let html = '';
  if (onebot.length) {
    html += `<div class="muted" style="width:100%">OneBot 群（${onebot.length}）</div>` + onebot.map(one).join('');
  }
  if (official.length) {
    html += `<div class="muted" style="width:100%;margin-top:6px">官方群（${official.length}）— 来自群内 @ 消息登记</div>` + official.map(one).join('');
  }
  box.innerHTML = html;
  $(metaId).textContent = `OneBot ${onebot.length} · 官方 ${official.length}`;
}

function renderBroadcast() {
  const sel = $('cl-bc-entry');
  const prev = sel.value;
  sel.innerHTML = entries.length
    ? entries.map((e, i) => `<option value="${i}">${esc(entryLabel(e, i))}</option>`).join('')
    : '<option value="">（无已保存记录）</option>';
  if (prev !== '' && prev !== null && +prev < entries.length) sel.value = prev;
  renderGroupPicker('cl-groups', 'cl-groups-meta', 'cl-grp');
  renderAnnBroadcast();
  renderGroupPicker('ann-groups', 'ann-groups-meta', 'ann-grp');
}

function pickedTargets(cls) {
  return Array.from(document.querySelectorAll(`.${cls}:checked`)).map((c) => (
    c.dataset.channel === 'official'
      ? { channel: 'official', id: c.value }
      : { channel: 'onebot', id: c.value }));
}

/* ── 数据加载 ── */

async function load() {
  try {
    const d = await api('/api/changelog');
    entries = d.entries || [];
    changeTypes = d.change_types || changeTypes;
    setDirty(false);
    render();
    renderBroadcast();
  } catch (e) {
    toast(e.message, 'error');
    $('cl-list').innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

async function loadAnn() {
  try {
    const d = await api('/api/changelog/announcements');
    annEntries = d.entries || [];
    annFormats = d.formats || annFormats;
    setAnnDirty(false);
    renderAnn();
    renderBroadcast();
  } catch (e) {
    toast(e.message, 'error');
    $('ann-list').innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

async function loadGroups() {
  try {
    const d = await api('/api/changelog/groups');
    groups = d.groups || [];
    renderBroadcast();
  } catch (e) {
    toast(e.message, 'error');
  }
}

/* ── 群发状态轮询（两个 Tab 各挂一个显示位；kind/ref/skipped/by_channel，§16.5.3.5）── */

function statusHtml(s) {
  if (!s || !s.state) return '';
  const bc = s.by_channel || {};
  const chan = ['onebot', 'official'].filter((c) => bc[c] && (bc[c].sent || bc[c].failed || bc[c].skipped))
    .map((c) => `${c === 'official' ? '官方' : 'OneBot'} ${bc[c].sent || 0}✓/${bc[c].failed || 0}✗`).join(' · ');
  if (s.state === 'sending') {
    return `<span class="warn">● 发送中… ${s.sent || 0}/${s.total || 0}</span>`;
  }
  if (s.state === 'done') {
    const what = s.kind === 'announcement'
      ? `公告「${esc(s.announcement_title || s.ref?.announcement_id || '')}」`
      : `更新记录${s.entry_version ? `（${esc(s.entry_version)}）` : ''}`;
    return `<span class="pill badge-ok">完成</span> ${what} 成功 ${s.sent || 0}，失败 ${s.failed || 0}`
      + (s.skipped ? `，跳过 ${s.skipped}` : '')
      + (chan ? ` <span class="muted">[${esc(chan)}]</span>` : '');
  }
  return `<span class="pill badge-err">失败</span> ${esc(s.error || '')}`;
}

function pollStatus() {
  if (statusTimer) clearInterval(statusTimer);
  const tick = async () => {
    let s;
    try {
      s = await api('/api/changelog/broadcast/status');
    } catch (e) { return; /* 忽略轮询错误 */ }
    const html = statusHtml(s);
    $('cl-bc-status').innerHTML = html;
    $('ann-bc-status').innerHTML = html;
    if (s && (s.state === 'done' || s.state === 'error')) {
      clearInterval(statusTimer); statusTimer = null;
      load();       // 刷新「已群发」标记
      loadAnn();    // 刷新公告 sent_at/发送状态
    }
  };
  tick();
  statusTimer = setInterval(tick, 2000);
}

/* ── events ── */

document.addEventListener('DOMContentLoaded', () => {
  // Tab：初始按 URL hash，点击切换并记忆
  document.querySelectorAll('#cl-tabs .tab').forEach((t) => {
    t.addEventListener('click', () => switchTab(t.dataset.tab));
  });
  switchTab((location.hash || '').replace('#', ''));

  load();
  loadAnn();
  loadGroups();

  $('cl-reload').addEventListener('click', () => {
    if (dirty && !confirm('有未保存的修改，确定放弃并重新加载？')) return;
    load();
  });

  $('cl-add').addEventListener('click', () => {
    entries.unshift({
      version: '', date: today(),
      changes: [{ type: changeTypes[0] || '新增', text: '' }],
      created_at: Date.now() / 1000,
    });
    setDirty(true);
    render();
    renderBroadcast();
  });

  $('cl-save').addEventListener('click', async () => {
    if (!dirty) { toast('没有未保存的修改'); return; }
    try {
      const r = await api('/api/changelog', { method: 'PUT', body: { entries } });
      toast(r.note || '已保存', 'success');
      await load();
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  // 结构化编辑：文本输入原地更新模型（不重渲染，避免丢焦点）
  const list = $('cl-list');
  list.addEventListener('input', (ev) => {
    const t = ev.target;
    if (t.dataset.eidx === undefined) return;
    const i = +t.dataset.eidx, role = t.dataset.role;
    if (role === 'version') { entries[i].version = t.value; setDirty(true); }
    else if (role === 'date') { entries[i].date = t.value; setDirty(true); }
    else if (role === 'ctext') { entries[i].changes[+t.dataset.cidx].text = t.value; setDirty(true); }
  });
  list.addEventListener('change', (ev) => {
    const t = ev.target;
    if (t.dataset.role === 'ctype') {
      entries[+t.dataset.eidx].changes[+t.dataset.cidx].type = t.value;
      setDirty(true);
    }
  });
  // 结构性变更：重渲染
  list.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button');
    if (!btn) return;
    if (btn.dataset.addChange !== undefined) {
      const i = +btn.dataset.addChange;
      entries[i].changes = entries[i].changes || [];
      entries[i].changes.push({ type: changeTypes[0] || '新增', text: '' });
      setDirty(true); render();
    } else if (btn.dataset.delChange !== undefined) {
      const [i, ci] = btn.dataset.delChange.split(':').map(Number);
      entries[i].changes.splice(ci, 1);
      setDirty(true); render();
    } else if (btn.dataset.delEntry !== undefined) {
      const i = +btn.dataset.delEntry;
      if (!confirm('删除这条更新记录？（需点「保存全部」才生效）')) return;
      entries.splice(i, 1);
      setDirty(true); render(); renderBroadcast();
    }
  });

  // ── 公告编辑 ──
  $('ann-reload').addEventListener('click', () => {
    if (annDirty && !confirm('有未保存的修改，确定放弃并重新加载？')) return;
    loadAnn();
  });
  $('ann-add').addEventListener('click', () => {
    annEntries.unshift({
      id: '', title: '', body: '', format: 'text',
      created_at: Date.now() / 1000,
    });
    setAnnDirty(true);
    renderAnn();
    renderAnnBroadcast();
  });
  $('ann-save').addEventListener('click', async () => {
    if (!annDirty) { toast('没有未保存的修改'); return; }
    if (annEntries.some((e) => !(e.title || '').trim() || !(e.body || '').trim())) {
      toast('每条公告都需要标题和正文', 'error'); return;
    }
    try {
      const r = await api('/api/changelog/announcements',
        { method: 'PUT', body: { entries: annEntries } });
      toast(r.note || '已保存', 'success');
      annEntries = r.entries || annEntries;   // 服务端生成 id 后的规范结果
      setAnnDirty(false);
      renderAnn();
      renderAnnBroadcast();
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  const annList = $('ann-list');
  annList.addEventListener('input', (ev) => {
    const t = ev.target;
    if (t.dataset.aidx === undefined) return;
    const i = +t.dataset.aidx, role = t.dataset.role;
    if (role === 'title') {
      annEntries[i].title = t.value;
      const c = document.querySelector(`[data-ann-tcount="${i}"]`);
      if (c) c.textContent = `${t.value.length}/60`;
    } else if (role === 'body') {
      annEntries[i].body = t.value;
      const c = document.querySelector(`[data-ann-bcount="${i}"]`);
      if (c) c.textContent = `${t.value.length}/2000`;
      const p = document.querySelector(`[data-ann-preview="${i}"]`);
      if (p) p.textContent = annPreviewText(annEntries[i]);
    }
    setAnnDirty(true);
  });
  annList.addEventListener('change', (ev) => {
    const t = ev.target;
    if (t.dataset.role === 'format') {
      annEntries[+t.dataset.aidx].format = t.value;
      setAnnDirty(true);
    }
  });
  annList.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button');
    if (!btn || btn.dataset.delAnn === undefined) return;
    const i = +btn.dataset.delAnn;
    if (!confirm('删除这条公告？（需点「保存全部」才生效）')) return;
    annEntries.splice(i, 1);
    setAnnDirty(true); renderAnn(); renderAnnBroadcast();
  });

  // ── 群发（两个 Tab 各自的卡片，共用群数据与状态轮询）──
  $('cl-bc-refresh-groups').addEventListener('click', loadGroups);
  $('ann-bc-refresh-groups').addEventListener('click', loadGroups);
  const wireAll = (allId, cls) => {
    $(allId).addEventListener('change', (ev) => {
      document.querySelectorAll(`.${cls}`).forEach((c) => {
        c.disabled = ev.target.checked;
        if (ev.target.checked) c.checked = false;
      });
    });
  };
  wireAll('cl-bc-all', 'cl-grp');
  wireAll('ann-bc-all', 'ann-grp');

  $('cl-bc-send').addEventListener('click', async () => {
    if (dirty) { toast('请先「保存全部」再群发（群发针对已保存的记录）', 'error'); return; }
    const idx = $('cl-bc-entry').value;
    if (idx === '' || idx === null) { toast('没有可群发的记录', 'error'); return; }
    let targets;
    if ($('cl-bc-all').checked) targets = ['all'];
    else targets = pickedTargets('cl-grp');
    if (!targets.length) { toast('请勾选「全部群」或至少一个群', 'error'); return; }
    const label = entries[+idx] ? entryLabel(entries[+idx], +idx) : ('#' + (+idx + 1));
    const tgt = targets[0] === 'all' ? '全部群' : `${targets.length} 个群`;
    if (!confirm(`确认把 ${label} 群发到 ${tgt}？`)) return;
    try {
      const r = await api('/api/changelog/broadcast',
        { method: 'POST', body: { entry_index: +idx, targets } });
      toast(r.note || '已入队', 'success');
      pollStatus();
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  $('ann-bc-send').addEventListener('click', async () => {
    if (annDirty) { toast('请先「保存全部」再群发（群发针对已保存的公告）', 'error'); return; }
    const aid = $('ann-bc-entry').value;
    if (!aid) { toast('没有可群发的公告（新建后请先保存）', 'error'); return; }
    let targets;
    if ($('ann-bc-all').checked) targets = ['all'];
    else targets = pickedTargets('ann-grp');
    if (!targets.length) { toast('请勾选「全部群」或至少一个群', 'error'); return; }
    const ann = annEntries.find((e) => e.id === aid);
    const tgt = targets[0] === 'all' ? '全部群' : `${targets.length} 个群`;
    if (!confirm(`确认把公告「${(ann && ann.title) || aid}」群发到 ${tgt}？`)) return;
    try {
      const r = await api('/api/changelog/announcements/broadcast',
        { method: 'POST', body: { announcement_id: aid, targets } });
      toast(r.note || '已入队', 'success');
      pollStatus();
    } catch (e) {
      toast(e.message, 'error');
    }
  });
});
