/* 邮件管理 — 角色信箱（proactive-mail-design.md §16.4）
 *
 * 面板对 data/mailboxes/*.json 只读；删除信件/发送系统邮件写
 * data/mail_admin/queue/ 文件队列，bot 的 _mail_admin_worker 认领执行并
 * 回写 status/。列表脱敏 openid，完整值仅详情页展示（W4）。
 */

let usersPage = { items: [], page: 1, pages: 1, total: 0 };
let curQuery = '';
let curDetail = null;        // 当前详情页 openid
let queueTimer = null;

const $ = (id) => document.getElementById(id);

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

const TYPE_LABEL = { delete_letter: '删除信件', send_system: '系统邮件' };
const STATE_PILL = {
  pending: '<span class="pill badge-warn">待处理</span>',
  processing: '<span class="pill badge-warn">执行中</span>',
  done: '<span class="pill badge-ok">完成</span>',
  failed: '<span class="pill badge-err">失败</span>',
};

/* ── 统计卡 ── */

async function loadOverview() {
  try {
    const d = await api('/api/mail/overview');
    $('ml-users').textContent = d.users ?? '—';
    $('ml-letters').textContent = d.letters ?? '—';
    $('ml-letters-sub').textContent = `系统邮件 ${d.system_letters ?? 0}`;
    $('ml-unread').textContent = d.unread ?? '—';
    $('ml-disabled').textContent = d.disabled_users ?? '—';
    $('ml-queue').textContent = d.queue_pending ?? '—';
  } catch (e) { /* 统计失败不打扰 */ }
}

/* ── 用户表 ── */

function renderUsers() {
  const box = $('ml-user-table');
  const items = usersPage.items || [];
  if (!items.length) {
    box.innerHTML = `<p class="muted">（${curQuery ? '没有匹配的用户' : '还没有用户 — c2c 联系人随对话自动登记，信箱随每日邮件 pass 建档'}）</p>`;
  } else {
    box.innerHTML = `
      <table class="data-table">
        <thead><tr>
          <th></th><th>用户</th><th>人格</th><th>信件</th><th>未读</th>
          <th>最近信封</th><th>主动消息</th><th></th>
        </tr></thead>
        <tbody>${items.map((u) => `
          <tr>
            <td><input type="checkbox" class="ml-pick" value="${esc(u.openid)}"
                       data-masked="${esc(u.openid_masked)}"
                       data-active="${u.active_enabled ? '1' : '0'}"></td>
            <td class="mono">${esc(u.openid_masked)}${u.has_mailbox === false
              ? ' <span class="muted">(未建档)</span>' : ''}</td>
            <td>${u.personas}</td>
            <td>${u.letters}${u.system ? ` <span class="muted">(系统 ${u.system})</span>` : ''}</td>
            <td>${u.unread ? `<b>${u.unread}</b>` : 0}</td>
            <td>${u.last_digest_date ? esc(u.last_digest_date)
              : (u.last_seen ? `<span class="muted" title="最近活跃（尚无信封）">${fmtTime(u.last_seen)}</span>` : '—')}</td>
            <td>${u.active_enabled
              ? '<span class="pill badge-ok">开启</span>'
              : '<span class="pill badge-err">已拒收</span>'}</td>
            <td><button class="btn btn-sm" data-detail="${esc(u.openid)}">详情</button></td>
          </tr>`).join('')}
        </tbody>
      </table>`;
  }
  $('ml-pageinfo').textContent = usersPage.total
    ? `第 ${usersPage.page}/${usersPage.pages} 页 · 共 ${usersPage.total} 个用户` : '';
  $('ml-prev').disabled = usersPage.page <= 1;
  $('ml-next').disabled = usersPage.page >= usersPage.pages;
}

async function loadUsers(page) {
  try {
    const q = encodeURIComponent(curQuery || '');
    const d = await api(`/api/mail/users?query=${q}&page=${page || 1}&page_size=20`);
    usersPage = d;
    renderUsers();
  } catch (e) {
    toast(e.message, 'error');
    $('ml-user-table').innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

/* ── 用户详情 ── */

function letterName(l) {
  return l.system ? `【系统】${l.title || ''}` : (l.name || l.persona || '?');
}

function renderDetail(d) {
  $('ml-d-openid').textContent = d.openid;      // 详情页显示完整 openid（W4）
  $('ml-d-active').innerHTML = d.active_enabled
    ? '<span class="pill badge-ok">主动消息开启</span>'
    : '<span class="pill badge-err">已拒收主动消息</span>';

  const personaKeys = [...new Set([
    ...Object.keys(d.known_personas || {}), ...Object.keys(d.personas || {}),
  ])];
  const personaRows = personaKeys.map((p) => {
    const st = (d.personas || {})[p] || {};
    return `<tr><td class="mono">${esc(p)}</td>
      <td>${st.last_mail_at ? fmtTime(st.last_mail_at) : '—'}</td>
      <td>${st.mail_count ?? 0}</td>
      <td>${(d.known_personas || {})[p] ? fmtTime(d.known_personas[p]) : '—'}</td></tr>`;
  }).join('');

  const letterRows = (d.letters || []).map((l, i) => `
    <tr>
      <td class="mono">${esc(l.id || '')}</td>
      <td>${fmtTime(l.created_at)}</td>
      <td>${esc(letterName(l))}</td>
      <td>${esc(l.preview || '')}</td>
      <td>${l.read ? '已读' : '<b>未读</b>'}</td>
      <td>
        <button class="btn btn-sm" data-view-letter="${i}">展开</button>
        <button class="btn btn-danger btn-sm" data-del-letter="${esc(l.id || '')}"
                data-del-name="${esc(letterName(l))}">删除</button>
      </td>
    </tr>
    <tr data-full-letter="${i}" style="display:none">
      <td colspan="6"><pre style="white-space:pre-wrap;margin:4px 0">${esc(l.text || '')}</pre></td>
    </tr>`).join('');

  const digestRows = (d.digests || []).map((g) => `
    <tr>
      <td class="mono">${esc(g.id || '')}</td>
      <td>${esc(g.date || '')}</td>
      <td>${fmtTime(g.sent_at)}</td>
      <td>${(g.letter_states || []).map((s) => s.withdrawn
        ? `<s class="muted mono">${esc(s.id)}</s> <span class="warn">(已撤回)</span>`
        : `<span class="mono">${esc(s.id)}</span>`).join('<br>')}</td>
    </tr>`).join('');

  $('ml-detail-body').innerHTML = `
    <div class="row" style="align-items:flex-start;gap:16px;flex-wrap:wrap">
      <div style="flex:1;min-width:300px">
        <h3 style="margin:0 0 6px">信件（${(d.letters || []).length}）</h3>
        ${letterRows ? `<table class="data-table">
          <thead><tr><th>编号</th><th>时间</th><th>发信人</th><th>预览</th><th>已读</th><th></th></tr></thead>
          <tbody>${letterRows}</tbody></table>`
        : '<p class="muted">（没有信件）</p>'}
      </div>
      <div style="flex:1;min-width:300px">
        <h3 style="margin:0 0 6px">人格状态（${personaKeys.length}）</h3>
        ${personaRows ? `<table class="data-table">
          <thead><tr><th>人格</th><th>最近信件</th><th>信件数</th><th>首次登记</th></tr></thead>
          <tbody>${personaRows}</tbody></table>`
        : '<p class="muted">（没有登记过角色人格）</p>'}
        <h3 style="margin:14px 0 6px">信封历史（${(d.digests || []).length}）</h3>
        ${digestRows ? `<table class="data-table">
          <thead><tr><th>信封</th><th>日期</th><th>发送时间</th><th>信件</th></tr></thead>
          <tbody>${digestRows}</tbody></table>`
        : '<p class="muted">（没有发送过信封）</p>'}
      </div>
    </div>`;
  $('ml-detail-card').style.display = '';
}

async function loadDetail(openid) {
  curDetail = openid;
  try {
    const d = await api(`/api/mail/user?openid=${encodeURIComponent(openid)}`);
    renderDetail(d);
    $('ml-detail-card').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (e) {
    toast(e.message, 'error');
    curDetail = null;
  }
}

/* ── 管理队列 ── */

function renderQueue(d) {
  const rows = [];
  for (const op of (d.pending || [])) {
    rows.push(`<tr>
      <td class="mono">${esc(op.id)}</td>
      <td>${esc(TYPE_LABEL[op.type] || op.type || '?')}</td>
      <td class="mono">${esc(op.openid_masked || '')}</td>
      <td>${esc(op.type === 'delete_letter' ? (op.letter_id || '') : (op.title || ''))}</td>
      <td>${STATE_PILL[op.state] || esc(op.state || '')}</td>
      <td>${fmtTime(op.created_at)}</td><td></td></tr>`);
  }
  for (const st of (d.recent || [])) {
    rows.push(`<tr>
      <td class="mono">${esc(st.id)}</td><td>—</td><td>—</td><td></td>
      <td>${STATE_PILL[st.state] || esc(st.state || '')}</td>
      <td>${fmtTime(st.finished_at)}</td>
      <td class="err">${esc(st.error || '')}</td></tr>`);
  }
  $('ml-queue-table').innerHTML = rows.length ? `
    <table class="data-table">
      <thead><tr><th>操作 id</th><th>类型</th><th>目标</th><th>内容</th><th>状态</th><th>时间</th><th>失败原因</th></tr></thead>
      <tbody>${rows.join('')}</tbody></table>`
    : '<p class="muted">（空 — 没有待处理或最近完成的操作）</p>';

  // 有待处理项时持续轮询，直到全部完成
  const hasPending = (d.pending || []).length > 0;
  if (hasPending && !queueTimer) {
    queueTimer = setInterval(loadQueue, 3000);
  } else if (!hasPending && queueTimer) {
    clearInterval(queueTimer); queueTimer = null;
    loadOverview();                       // 队列清空后刷新统计（删除/发送已落账）
    if (curDetail) loadDetail(curDetail); // 详情页同步
  }
}

async function loadQueue() {
  try {
    renderQueue(await api('/api/mail/queue'));
  } catch (e) { /* 轮询错误静默 */ }
}

/* ── 系统邮件编辑卡 ── */

function pickedUsers() {
  return Array.from(document.querySelectorAll('.ml-pick:checked')).map((c) => ({
    openid: c.value, masked: c.dataset.masked, active: c.dataset.active === '1',
  }));
}

function openCompose() {
  const picked = pickedUsers();
  if (!picked.length) {
    toast('请先在用户表勾选至少一个用户', 'error');
    return;
  }
  const off = picked.filter((u) => !u.active);
  $('ml-c-targets').innerHTML = `目标：<b>${picked.length}</b> 个用户 — `
    + picked.slice(0, 8).map((u) => esc(u.masked)).join('、')
    + (picked.length > 8 ? ' …' : '')
    + (off.length ? ` <span class="warn">（其中 ${off.length} 个已拒收主动消息，将发送失败）</span>` : '');
  $('ml-compose-card').style.display = '';
  $('ml-compose-card').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  updateComposePreview();
}

function updateComposePreview() {
  const title = $('ml-c-title').value;
  const body = $('ml-c-body').value;
  $('ml-c-tcount').textContent = `${title.length}/60`;
  $('ml-c-bcount').textContent = `${body.length}/2000`;
  $('ml-c-preview').textContent = `## 系统邮件 · ${title}\n${body}\n\n> 发送 /邮件 查看信箱`;
}

async function sendSystemMail() {
  const picked = pickedUsers();
  if (!picked.length) { toast('请先在用户表勾选至少一个用户', 'error'); return; }
  const title = $('ml-c-title').value.trim();
  const body = $('ml-c-body').value;
  if (!title || !body.trim()) { toast('标题/正文不能为空', 'error'); return; }
  const format = $('ml-c-format').value;
  const off = picked.filter((u) => !u.active);
  let msg = `确认向 ${picked.length} 个用户发送系统邮件「${title}」？\n（入队后 bot 立即逐条发送，为真实主动消息，不可撤回发送动作）`;
  if (off.length) msg += `\n注意：${off.length} 个用户已拒收主动消息，将发送失败。`;
  if (!confirm(msg)) return;
  $('ml-c-send').disabled = true;
  try {
    const r = await api('/api/mail/send', {
      method: 'POST',
      body: { openids: picked.map((u) => u.openid), title, body, format },
    });
    toast(r.note || `已入队 ${r.count} 条`, 'success');
    $('ml-compose-card').style.display = 'none';
    document.querySelectorAll('.ml-pick:checked').forEach((c) => { c.checked = false; });
    loadQueue();
    loadOverview();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    $('ml-c-send').disabled = false;
  }
}

/* ── events ── */

document.addEventListener('DOMContentLoaded', () => {
  loadOverview();
  loadUsers(1);
  loadQueue();

  $('ml-search').addEventListener('click', () => {
    curQuery = $('ml-q').value.trim();
    loadUsers(1);
  });
  $('ml-q').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { curQuery = $('ml-q').value.trim(); loadUsers(1); }
  });
  $('ml-refresh').addEventListener('click', () => { loadOverview(); loadUsers(usersPage.page); loadQueue(); });
  $('ml-prev').addEventListener('click', () => loadUsers(usersPage.page - 1));
  $('ml-next').addEventListener('click', () => loadUsers(usersPage.page + 1));
  $('ml-queue-refresh').addEventListener('click', () => { loadQueue(); loadOverview(); });

  $('ml-compose').addEventListener('click', openCompose);
  $('ml-compose-close').addEventListener('click', () => { $('ml-compose-card').style.display = 'none'; });
  $('ml-c-title').addEventListener('input', updateComposePreview);
  $('ml-c-body').addEventListener('input', updateComposePreview);
  $('ml-c-send').addEventListener('click', sendSystemMail);

  $('ml-detail-close').addEventListener('click', () => {
    $('ml-detail-card').style.display = 'none';
    curDetail = null;
  });

  // 用户表：详情按钮（事件委托，重渲染不丢监听）
  $('ml-user-table').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-detail]');
    if (btn) loadDetail(btn.dataset.detail);
  });

  // 详情卡：展开全文 / 删除信件
  $('ml-detail-body').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('button');
    if (!btn) return;
    if (btn.dataset.viewLetter !== undefined) {
      const row = document.querySelector(`[data-full-letter="${btn.dataset.viewLetter}"]`);
      if (row) {
        const show = row.style.display === 'none';
        row.style.display = show ? '' : 'none';
        btn.textContent = show ? '收起' : '展开';
      }
    } else if (btn.dataset.delLetter) {
      const letterId = btn.dataset.delLetter;
      if (!curDetail) return;
      if (!confirm(`删除信件 ${letterId}（${btn.dataset.delName || ''}）？\n`
        + '用户 /邮件 查看该编号会显示「已被撤回」。此操作入队后由 bot 执行，不可撤销。')) return;
      btn.disabled = true;
      try {
        await api('/api/mail/letters/delete', {
          method: 'POST',
          body: { openid: curDetail, letter_id: letterId },
        });
        toast('删除操作已入队 — bot 约 3–5 秒内执行', 'success');
        loadQueue();
        loadOverview();
      } catch (e) {
        toast(e.message, 'error');
        btn.disabled = false;
      }
    }
  });
});
