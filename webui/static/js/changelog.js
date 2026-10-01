/* 更新日志管理 — CRUD over QQBot/data/changelog/changelog.json + 群发
 *
 * 与 bot 解耦：面板只写文件（changelog.json / pending_broadcast.json），
 * bot 后台轮询器负责实际群发并回写 broadcast_status.json。
 */

let entries = [];
let changeTypes = ['新增', '修复', '优化', '调整', '移除'];
let groups = [];
let dirty = false;
let statusTimer = null;

const $ = (id) => document.getElementById(id);
const today = () => new Date().toISOString().slice(0, 10);

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function setDirty(v) {
  dirty = v;
  $('cl-dirty').style.display = v ? '' : 'none';
}

function entryLabel(e, i) {
  const v = e.version ? e.version : '(无版本)';
  const d = e.date ? '  ' + e.date : '';
  return `#${i + 1}  ${v}${d}`;
}

/* ── 记录编辑列表 ── */

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

/* ── 群发卡 ── */

function renderBroadcast() {
  const sel = $('cl-bc-entry');
  const prev = sel.value;
  sel.innerHTML = entries.length
    ? entries.map((e, i) => `<option value="${i}">${esc(entryLabel(e, i))}</option>`).join('')
    : '<option value="">（无已保存记录）</option>';
  if (prev !== '' && prev !== null && +prev < entries.length) sel.value = prev;

  const box = $('cl-groups');
  if (!groups.length) {
    box.innerHTML = '<span class="muted">（暂无群列表 — 确认 bot 已连接，或点「刷新群列表」；也可勾选「全部群」由 bot 发送时解析）</span>';
  } else {
    box.innerHTML = groups.map((g) => `
      <label class="row" style="gap:4px;align-items:center;margin:0">
        <input type="checkbox" class="cl-grp" value="${esc(g.id)}">
        <span>${esc(g.name) || esc(g.id)} <span class="muted mono">(${esc(g.id)})</span></span>
      </label>`).join('');
  }
  $('cl-groups-meta').textContent = groups.length ? `共 ${groups.length} 个群` : '';
}

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

async function loadGroups() {
  try {
    const d = await api('/api/changelog/groups');
    groups = d.groups || [];
    renderBroadcast();
  } catch (e) {
    toast(e.message, 'error');
  }
}

/* ── 群发状态轮询 ── */

function pollStatus() {
  if (statusTimer) clearInterval(statusTimer);
  const el = $('cl-bc-status');
  const tick = async () => {
    try {
      const s = await api('/api/changelog/broadcast/status');
      if (!s || !s.state) { el.textContent = ''; return; }
      if (s.state === 'sending') {
        el.innerHTML = `<span class="warn">● 发送中… ${s.sent || 0}/${s.total || 0}</span>`;
      } else if (s.state === 'done') {
        el.innerHTML = `<span class="pill badge-ok">完成</span> 成功 ${s.sent || 0}，失败 ${s.failed || 0}`
          + (s.entry_version ? `（${esc(s.entry_version)}）` : '');
        clearInterval(statusTimer); statusTimer = null;
        load();  // 刷新「已群发」标记
      } else if (s.state === 'error') {
        el.innerHTML = `<span class="pill badge-err">失败</span> ${esc(s.error || '')}`;
        clearInterval(statusTimer); statusTimer = null;
      }
    } catch (e) { /* 忽略轮询错误 */ }
  };
  tick();
  statusTimer = setInterval(tick, 2000);
}

/* ── events ── */

document.addEventListener('DOMContentLoaded', () => {
  load();
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

  // 群发
  $('cl-bc-refresh-groups').addEventListener('click', loadGroups);
  $('cl-bc-all').addEventListener('change', (ev) => {
    document.querySelectorAll('.cl-grp').forEach((c) => {
      c.disabled = ev.target.checked;
      if (ev.target.checked) c.checked = false;
    });
  });
  $('cl-bc-send').addEventListener('click', async () => {
    if (dirty) { toast('请先「保存全部」再群发（群发针对已保存的记录）', 'error'); return; }
    const idx = $('cl-bc-entry').value;
    if (idx === '' || idx === null) { toast('没有可群发的记录', 'error'); return; }
    let targets;
    if ($('cl-bc-all').checked) targets = ['all'];
    else targets = Array.from(document.querySelectorAll('.cl-grp:checked')).map((c) => c.value);
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
});
