/* 兑换码管理 — CRUD over QQBot/data/redeem_code/redeem_code.json */

let codes = [];
let dirty = false;
let onlyTimeLimited = false;
let editingIndex = -1;   // index into `codes` being edited, -1 = new entry

const $ = (id) => document.getElementById(id);

function today() {
  return new Date().toISOString().slice(0, 10);
}

function recentCutoff() {
  const d = new Date();
  d.setDate(d.getDate() - 30);
  return d.toISOString().slice(0, 10);
}

/* Same classification rule as the bot plugin (valid date, or added ≤30d) */
function classify(e) {
  if (e.valid) return '限时';
  if (e._added && e._added >= recentCutoff()) return '限时';
  return '长期';
}

function setDirty(v) {
  dirty = v;
  $('rd-dirty').style.display = v ? '' : 'none';
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function render() {
  const tbody = $('rd-tbody');
  const shown = codes
    .map((e, i) => ({ e, i }))
    .filter(({ e }) => !onlyTimeLimited || classify(e) === '限时');
  if (!shown.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">（空）</td></tr>';
  } else {
    tbody.innerHTML = shown.map(({ e, i }) => {
      const cls = classify(e);
      const expired = e.valid && e.valid < today();
      return `<tr>
        <td class="mono">${esc(e.code)}</td>
        <td>${esc(e.content) || '<span class="muted">—</span>'}</td>
        <td class="mono">${e.valid ? esc(e.valid) + ' 08:00' : '<span class="muted">长期</span>'}${expired ? ' <span class="pill badge-err">已过期</span>' : ''}</td>
        <td class="mono muted">${esc(e._added) || '—'}</td>
        <td><span class="muted">${esc(e._source) || '—'}</span></td>
        <td><span class="pill ${cls === '限时' ? 'badge-warn' : 'badge-ok'}">${cls}</span></td>
        <td>
          <button class="btn btn-sm" data-edit="${i}">编辑</button>
          <button class="btn btn-danger btn-sm" data-del="${i}">删除</button>
        </td>
      </tr>`;
    }).join('');
  }
  const tl = codes.filter((e) => classify(e) === '限时').length;
  $('rd-stat').textContent =
    `共 ${codes.length} 条（限时 ${tl} / 长期 ${codes.length - tl}），当前显示 ${shown.length} 条`;
}

async function load() {
  try {
    const d = await api('/api/redeem');
    codes = (d.codes || []).filter((e) => e && e.code);
    setDirty(false);
    const meta = [];
    if (d.scraped_at_iso) meta.push(`自动更新: ${d.scraped_at_iso.slice(0, 16).replace('T', ' ')}`);
    else meta.push('自动更新: 从未成功');
    meta.push(`${codes.length} 条`);
    $('rd-meta').textContent = meta.join(' · ');
    render();
    renderAlert(d.alert || {});
  } catch (e) {
    toast(e.message, 'error');
    $('rd-tbody').innerHTML = `<tr><td colspan="7" class="err">${esc(e.message)}</td></tr>`;
  }
}

function renderAlert(alert) {
  const el = $('rd-alert');
  if (alert && alert.reason) {
    $('rd-alert-text').textContent =
      alert.reason === 'no_valid_codes'
        ? '当前没有有效的限时兑换码，且网页爬取失败 — 请手动添加（可从官方推文粘贴解析）'
        : alert.reason;
    $('rd-alert-time').textContent = alert.triggered_at || '';
    el.style.display = '';
  } else {
    el.style.display = 'none';
  }
}

/* ── edit form ── */

function showForm(entry, index) {
  editingIndex = index;
  $('rd-edit-title').textContent = index >= 0 ? `编辑兑换码（${entry.code}）` : '添加兑换码';
  $('rd-f-code').value = entry.code || '';
  $('rd-f-content').value = entry.content || '';
  $('rd-f-valid').value = entry.valid || '';
  $('rd-f-code').readOnly = index >= 0;  // code is the identity key
  $('rd-edit').style.display = '';
  $('rd-f-code').focus();
}

function hideForm() {
  editingIndex = -1;
  $('rd-edit').style.display = 'none';
  $('rd-f-code').readOnly = false;
}

function saveFormEntry() {
  const code = $('rd-f-code').value.trim();
  const content = $('rd-f-content').value.trim();
  const valid = $('rd-f-valid').value.trim();
  if (!code) { toast('兑换码不能为空', 'error'); return; }
  if (valid && !/^\d{4}-\d{2}-\d{2}$/.test(valid)) {
    toast('有效期格式应为 YYYY-MM-DD（留空 = 长期）', 'error'); return;
  }
  const dup = codes.findIndex((e, i) => e.code === code && i !== editingIndex);
  if (dup >= 0) { toast(`兑换码已存在（第 ${dup + 1} 行）`, 'error'); return; }

  const isEdit = editingIndex >= 0;
  if (isEdit) {
    const e = codes[editingIndex];
    e.content = content;
    e.valid = valid;
    // A manual edit makes the entry panel-authoritative
    e._source = 'panel';
  } else {
    codes.unshift({
      code, content, valid,
      _added: today(),
      _source: 'panel',
    });
  }
  hideForm();
  setDirty(true);
  render();
  toast(isEdit ? '已更新（记得保存全部）' : '已加入列表（记得保存全部）', 'success');
}

/* ── events ── */

document.addEventListener('DOMContentLoaded', () => {
  load();

  $('rd-reload').addEventListener('click', () => {
    if (dirty && !confirm('有未保存的修改，确定放弃并重新加载？')) return;
    load();
  });

  $('rd-add').addEventListener('click', () => showForm({}, -1));
  $('rd-f-cancel').addEventListener('click', hideForm);
  $('rd-f-save').addEventListener('click', saveFormEntry);

  $('rd-filter').addEventListener('click', () => {
    onlyTimeLimited = !onlyTimeLimited;
    $('rd-filter').textContent = onlyTimeLimited ? '看全部' : '只看限时';
    $('rd-filter').classList.toggle('btn-primary', onlyTimeLimited);
    render();
  });

  $('rd-parse').addEventListener('click', async () => {
    const text = $('rd-tweet').value;
    if (!text.trim()) { toast('请先粘贴推文文本', 'error'); return; }
    try {
      const r = await api('/api/redeem/parse-tweet', { method: 'POST', body: { text } });
      if (!r.code && !r.valid && !r.content) {
        $('rd-parse-hint').textContent = '未解析出任何字段，请手动填写表单';
        showForm({}, -1);
        return;
      }
      showForm({ code: r.code, content: r.content, valid: r.valid }, -1);
      const parts = [];
      if (r.code) parts.push('兑换码');
      if (r.valid) parts.push('有效期');
      if (r.content) parts.push('内容');
      $('rd-parse-hint').textContent = `已解析: ${parts.join(' / ')} — 请核对后保存`;
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  $('rd-tbody').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button');
    if (!btn) return;
    const i = parseInt(btn.dataset.edit ?? btn.dataset.del ?? '-1', 10);
    if (i < 0 || i >= codes.length) return;
    if (btn.dataset.edit !== undefined) {
      showForm(codes[i], i);
    } else {
      if (!confirm(`删除兑换码 ${codes[i].code}？（需点「保存全部」才生效）`)) return;
      codes.splice(i, 1);
      if (editingIndex === i) hideForm();
      setDirty(true);
      render();
    }
  });

  $('rd-save').addEventListener('click', async () => {
    if (!dirty) { toast('没有未保存的修改'); return; }
    try {
      const r = await api('/api/redeem', { method: 'PUT', body: { codes } });
      toast(r.note || '已保存', 'success');
      await load();
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  $('rd-alert-dismiss').addEventListener('click', async () => {
    try {
      await api('/api/redeem/alert/clear', { method: 'POST' });
      renderAlert({});
    } catch (e) {
      toast(e.message, 'error');
    }
  });
});
