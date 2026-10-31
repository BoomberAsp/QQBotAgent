/* 角色管理页 —— 浏览 / 编辑用户自创角色的 card.md 与 meta.json。
 *
 * 设计：docs/character-admin-ui-design.md §8。
 * 所有写操作都带 expected_sha256（乐观锁）；后端 409 时提示重新加载。
 */
(function () {
  'use strict';

  const state = {
    cid: null,
    detail: null,       // GET /api/characters/{cid} 的响应
    mode: 'sections',   // sections | raw
    dirty: false,
    version: null,
    list: [],
  };

  // ── 小工具 ─────────────────────────────────────────────────────
  function el(id) { return document.getElementById(id); }

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function fmtTime(ts) {
    return ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';
  }

  function pill(text, kind) {
    return `<span class="pill${kind ? ' badge-' + kind : ''}">${esc(text)}</span>`;
  }

  function fmtSize(n) {
    if (n == null) return '—';
    return typeof fmtBytes === 'function' ? fmtBytes(n) : n + ' B';
  }

  function setDirty(v) {
    state.dirty = !!v;
    const save = el('pm-save');
    if (save) save.textContent = v ? '保存 *' : '保存';
  }

  window.addEventListener('beforeunload', ev => {
    if (state.dirty) { ev.preventDefault(); ev.returnValue = ''; }
  });

  // ── Tab 切换 ───────────────────────────────────────────────────
  document.querySelectorAll('#char-tabs .tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#char-tabs .tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      document.querySelectorAll('.char-pane').forEach(p => { p.style.display = 'none'; });
      const pane = el('pane-' + tab.dataset.tab);
      if (pane) pane.style.display = '';
      if (tab.dataset.tab === 'versions') loadVersions();
    });
  });

  function activeTab() {
    const t = document.querySelector('#char-tabs .tab.active');
    return t ? t.dataset.tab : 'overview';
  }

  // ── 列表 ───────────────────────────────────────────────────────
  async function loadStats() {
    try {
      const d = await api('/api/characters/stats');
      const issues = d.issue_counts || {};
      const bad = (issues.meta_corrupt || 0) + (issues.meta_missing || 0) +
                  (issues.author_missing || 0);
      const parts = [
        `${d.total} 张自创 · ${d.authors} 位作者 · ${fmtSize(d.total_bytes)}`,
        `内置 ${d.builtin_total} 个`,
      ];
      if (bad) parts.push(`⚠ ${bad} 张不可见`);
      if (issues.name_drift) parts.push(`⚠ ${issues.name_drift} 张名称漂移`);
      const box = el('char-stats');
      box.innerHTML = parts.join('<br>')
        + `<br><span class="mono" style="font-size:11px">${esc(d.root)}</span>`;
      box.title = d.root;
      if (!d.mounted) {
        box.innerHTML += '<br><span class="err">⚠ 目录不存在，检查 .env 的 CHARACTERS_DIR</span>';
      }
    } catch (e) {
      el('char-stats').innerHTML = `<span class="err">统计失败: ${esc(e.message)}</span>`;
    }
  }

  async function loadAuthors() {
    try {
      const d = await api('/api/characters/authors');
      const sel = el('char-author');
      const cur = sel.value;
      sel.innerHTML = '<option value="">全部作者</option>' + (d.authors || []).map(a =>
        `<option value="${esc(a.uid)}">${esc(a.uid || '(无作者)')} · ${a.used}` +
        `${a.over_quota ? ' ⚠超配额' : ''}</option>`).join('');
      sel.value = cur;
    } catch (e) { /* 作者列表失败不影响主流程 */ }
  }

  function rowItem(row) {
    const errs = (row.issues || []).filter(i => i.level === 'error');
    const warns = (row.issues || []).filter(i => i.level === 'warn');
    const cls = errs.length ? ' char-item-err' : (warns.length ? ' char-item-warn' : '');
    const src = row.source === 'builtin'
      ? '<span class="pill" style="font-size:10px;padding:0 6px">内置</span>' : '';
    const note = errs.length ? errs[0].msg : (warns.length ? warns[0].msg : '');
    return `<div class="char-item${cls}" data-cid="${esc(row.char_id)}">
      <div class="row" style="gap:6px">
        <span class="char-item-name">${esc(row.display_name || row.char_id)}</span>${src}
        <span class="spacer"></span>
        <span class="muted" style="font-size:11px">${fmtSize(row.bytes)}</span>
      </div>
      <div class="char-item-sub">${esc(row.char_id)}${
        row.author ? ' · 作者 ' + esc(row.author) : ''}</div>
      ${note ? `<div class="char-item-mark">${errs.length ? '⚠' : '•'} ${esc(note)}</div>` : ''}
    </div>`;
  }

  async function loadList() {
    const params = new URLSearchParams({
      source: el('char-source').value,
      author: el('char-author').value,
      q: el('char-search').value.trim(),
      issue: el('char-issue').value,
      size: '200',
    });
    const box = el('char-list');
    try {
      const d = await api('/api/characters?' + params.toString());
      state.list = d.items || [];
      if (!state.list.length) {
        box.innerHTML = '<span class="muted">没有匹配的角色卡</span>';
        return;
      }
      box.innerHTML = state.list.map(rowItem).join('');
      box.querySelectorAll('.char-item').forEach(node => {
        node.addEventListener('click', () => selectChar(node.dataset.cid));
      });
      markActive(state.cid);
    } catch (e) {
      box.innerHTML = `<span class="muted">加载失败: ${esc(e.message)}</span>`;
    }
  }

  function markActive(cid) {
    document.querySelectorAll('#char-list .char-item').forEach(node => {
      node.classList.toggle('active', node.dataset.cid === cid);
    });
  }

  // ── 详情加载 ───────────────────────────────────────────────────
  async function selectChar(cid, opts) {
    opts = opts || {};
    if (state.dirty && state.cid && state.cid !== cid && !opts.force) {
      if (!confirm('当前角色有未保存的改动，确定切换？')) return;
    }
    try {
      const d = await api('/api/characters/' + encodeURIComponent(cid));
      state.cid = cid;
      state.detail = d;
      state.mode = d.readonly ? 'raw' : 'sections';
      state.version = null;
      setDirty(false);
      el('char-empty').style.display = 'none';
      el('char-detail').style.display = '';
      renderHeader();
      renderOverview();
      renderPrompt();
      renderMeta();
      renderPortrait();
      renderBond();
      renderLint();
      el('vs-list').innerHTML = '<span class="muted">切到「版本」页加载</span>';
      el('vs-diff').textContent = '选择左侧版本查看与当前内容的差异';
      markActive(cid);
      if (!opts.keepTab) {
        document.querySelectorAll('#char-tabs .tab').forEach(t =>
          t.classList.toggle('active', t.dataset.tab === 'overview'));
        document.querySelectorAll('.char-pane').forEach(p => { p.style.display = 'none'; });
        el('pane-overview').style.display = '';
        location.hash = '#' + encodeURIComponent(cid);
      }
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  // ── 头部 / 概览 ────────────────────────────────────────────────
  function renderHeader() {
    const d = state.detail;
    el('char-title').textContent = d.display_name || d.char_id;
    const badges = [];
    badges.push(pill(d.source === 'builtin' ? '内置人格' : '自创角色'));
    if (d.readonly) badges.push(pill('只读', 'warn'));
    if (d.author) badges.push(pill('作者 ' + d.author));
    const errs = (d.issues || []).filter(i => i.level === 'error').length;
    const warns = (d.issues || []).filter(i => i.level === 'warn').length;
    if (errs) badges.push(pill(errs + ' 项错误', 'err'));
    if (!errs && warns) badges.push(pill(warns + ' 项告警', 'warn'));
    el('char-badges').innerHTML = badges.join(' ');
    el('char-sub').textContent =
      `${d.char_id} · ${fmtSize(d.card.size)} · 更新 ${fmtTime(d.card.mtime)} · ${d.dir}`;
    el('char-delete').style.display = d.readonly ? 'none' : '';
  }

  function renderOverview() {
    const d = state.detail;
    const f = d.files || {};
    el('ov-files').innerHTML = `
      <tr><td>card.md</td><td class="mono">${fmtSize(d.card.size)} · ${d.card.sha256.slice(0, 12)}…</td></tr>
      <tr><td>编码</td><td>${d.card.encoding_ok ? '<span class="ok">UTF-8</span>' : '<span class="err">非 UTF-8</span>'}</td></tr>
      <tr><td>meta.json</td><td>${d.meta_state.state === 'ok'
        ? `<span class="ok">正常</span> · ${fmtSize(d.meta_state.bytes)}`
        : `<span class="err">${esc(d.meta_state.state)}</span> ${esc(d.meta_state.error || '')}`}</td></tr>
      <tr><td>assets/</td><td>${f.asset_count || 0} 个文件 · ${fmtSize(f.assets_bytes || 0)}</td></tr>
      <tr><td>参考立绘</td><td>${f.portrait ? '<span class="ok">已设置</span>'
        : '<span class="muted">未设置</span>'}</td></tr>
      <tr><td>体检体积</td><td>${fmtSize((d.lint || {}).bytes)} · 约 ${(d.lint || {}).tokens || 0} tokens</td></tr>`;

    const secs = (d.parsed && d.parsed.sections) || [];
    el('ov-sections').innerHTML = secs.length
      ? secs.map(s => `<div class="row" style="gap:8px;font-size:12.5px;padding:2px 0">
            <span class="mono">## ${esc(s.heading)}</span>
            <span class="spacer"></span>
            <span class="muted">${s.chars} 字符</span></div>`).join('')
      : '<span class="muted">没有识别到 level-2 段落</span>';

    el('ov-meta').textContent = d.meta && Object.keys(d.meta).length
      ? JSON.stringify(d.meta, null, 2) : '(无 meta.json)';
  }

  // ── 提示词 ─────────────────────────────────────────────────────
  function sectionBlock(sec) {
    return `<div class="char-sec" data-index="${sec.index}">
      <div class="row" style="margin-bottom:4px">
        <span class="mono char-sec-head">## ${esc(sec.heading)}</span>
        <span class="spacer"></span>
        <span class="muted" style="font-size:11.5px">${sec.chars} 字符</span>
        ${sec.known ? '' : '<span class="pill badge-warn" style="font-size:10px">自定义段落</span>'}
        <button class="btn btn-sm" data-act="remove-section" data-index="${sec.index}"
                title="删除该段落（先备份）">删除</button>
      </div>
      <textarea class="code-editor char-sec-body" data-index="${sec.index}"
                rows="${Math.min(18, Math.max(3, (sec.chars / 60 | 0) + 1))}"
                style="width:100%"></textarea>
    </div>`;
  }

  function renderPrompt() {
    const d = state.detail;
    const p = d.parsed || {};
    const readonly = !!d.readonly;
    const raw = d.card.content || '';

    el('pm-size').textContent = `${fmtSize(d.card.size)} · 约 ${(d.lint || {}).tokens || 0} tokens`;
    el('pm-h1-label').textContent = '# ' + (p.h1 || '(无 H1)');
    el('pm-meta-label').textContent = p.meta_comment_raw || '(无元数据注释)';
    el('pm-mode').value = (p.meta_comment || {}).mode || '';
    el('pm-task-voice').value = (p.meta_comment || {}).task_voice || '';
    el('pm-voice').value = (p.meta_comment || {}).voice_id || '';
    el('pm-preamble').value = p.preamble || '';

    const list = el('pm-section-list');
    const secs = p.sections || [];
    list.innerHTML = secs.map(sectionBlock).join('') ||
      '<span class="muted">没有识别到 level-2 段落（可用原文模式直接写）</span>';
    secs.forEach(s => {
      const ta = list.querySelector(`textarea[data-index="${s.index}"]`);
      if (ta) { ta.value = s.body || ''; ta.addEventListener('input', () => setDirty(true)); }
    });
    list.querySelectorAll('[data-act="remove-section"]').forEach(btn => {
      btn.addEventListener('click', () => removeSection(Number(btn.dataset.index)));
    });
    el('pm-preamble').oninput = () => setDirty(true);

    el('pm-raw-editor').value = raw;
    el('pm-raw-editor').oninput = () => setDirty(true);
    setMode(state.mode);

    const disabled = readonly;
    ['pm-save', 'pm-mode-sections', 'pm-mode-raw', 'pm-preamble', 'pm-mode',
     'pm-task-voice', 'pm-voice', 'pm-diff', 'pm-add-section'].forEach(id => {
      const node = el(id);
      if (node) node.disabled = disabled;
    });
    if (disabled) {
      el('pm-hint').innerHTML =
        '<span class="warn">这是内置人格卡（代码盘文件），本页只读。</span>' +
        '要改内容请到「<a href="/config">配置热管理 → 提示词</a>」页；' +
        '本文本区可直接查看/复制。';
      el('pm-mode-raw').classList.add('active');
    }
  }

  function setMode(mode) {
    state.mode = mode;
    const isRaw = mode === 'raw';
    el('pm-sections').style.display = isRaw ? 'none' : '';
    el('pm-raw').style.display = isRaw ? '' : 'none';
    el('pm-mode-sections').classList.toggle('active', !isRaw);
    el('pm-mode-raw').classList.toggle('active', isRaw);
  }

  el('pm-mode-sections').addEventListener('click', () => {
    if (state.detail && state.detail.readonly) return;
    if (state.mode === 'raw' && state.dirty) {
      if (!confirm('原文模式的改动会丢失（两种模式不互相转换），继续？')) return;
    }
    setMode('sections');
    renderPrompt();
  });
  el('pm-mode-raw').addEventListener('click', () => {
    if (state.detail && state.detail.readonly) { setMode('raw'); return; }
    if (state.dirty && state.mode === 'sections' &&
        !confirm('分区模式的改动会丢失（两种模式不互相转换），继续？')) return;
    setMode('raw');
  });

  // 分区模式 → 拼出卡片全文（仅用于本地 diff 预览）
  function buildSectionsPreview() {
    const d = state.detail;
    const p = d.parsed || {};
    const byIndex = {};
    el('pm-section-list').querySelectorAll('textarea.char-sec-body').forEach(ta => {
      byIndex[ta.dataset.index] = ta.value;
    });
    const raw = d.card.content || '';
    const secs = p.sections || [];
    if (!secs.length) return raw;
    const firstStart = raw.indexOf('## ' + secs[0].heading);
    if (firstStart < 0) return raw;
    let head = raw.slice(0, firstStart);
    // 前言：head 里 H1/注释之后的部分
    const preOld = p.preamble || '';
    const preNew = el('pm-preamble').value;
    if (preOld && head.indexOf(preOld) >= 0) {
      head = head.replace(preOld, preNew);
    }
    const parts = secs.map(s => {
      const body = byIndex[s.index] != null ? byIndex[s.index] : (s.body || '');
      return `## ${s.heading}\n\n${body.replace(/\s+$/, '')}\n\n`;
    });
    return head + parts.join('');
  }

  function diffLines(aText, bText) {
    const a = String(aText || '').split('\n');
    const b = String(bText || '').split('\n');
    const n = a.length, m = b.length;
    const L = [];
    for (let i = 0; i <= n; i++) L.push(new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1
                                : Math.max(L[i + 1][j], L[i][j + 1]);
      }
    }
    const out = [];
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) { out.push('  ' + a[i]); i++; j++; }
      else if (L[i + 1][j] >= L[i][j + 1]) { out.push('- ' + a[i]); i++; }
      else { out.push('+ ' + b[j]); j++; }
    }
    while (i < n) out.push('- ' + a[i++]);
    while (j < m) out.push('+ ' + b[j++]);
    return out;
  }

  el('pm-diff').addEventListener('click', () => {
    const d = state.detail;
    if (!d) return;
    const newText = state.mode === 'raw' ? el('pm-raw-editor').value
                                         : buildSectionsPreview();
    const lines = diffLines(d.card.content, newText);
    const changed = lines.some(l => l[0] === '+' || l[0] === '-');
    const box = el('pm-diff-box');
    box.style.display = '';
    box.innerHTML = changed
      ? lines.map(l => {
          const cls = l[0] === '+' ? 'diff-add' : (l[0] === '-' ? 'diff-del' : 'diff-ctx');
          return `<span class="${cls}">${esc(l)}</span>`;
        }).join('\n')
      : '<span class="muted">没有任何改动</span>';
  });

  el('pm-save').addEventListener('click', async () => {
    const d = state.detail;
    if (!d || d.readonly) return;
    const btn = el('pm-save');
    btn.disabled = true;
    try {
      let res;
      if (state.mode === 'raw') {
        res = await api('/api/characters/' + encodeURIComponent(state.cid) + '/card',
          { method: 'PUT', body: { content: el('pm-raw-editor').value,
                                   expected_sha256: d.card.sha256 } });
      } else {
        const sections = [];
        el('pm-section-list').querySelectorAll('textarea.char-sec-body').forEach(ta => {
          sections.push({ index: Number(ta.dataset.index), body: ta.value });
        });
        const meta = {
          mode: el('pm-mode').value,
          task_voice: el('pm-task-voice').value,
          voice_id: el('pm-voice').value.trim(),
        };
        res = await api('/api/characters/' + encodeURIComponent(state.cid) + '/sections',
          { method: 'PUT', body: { sections, preamble: el('pm-preamble').value,
                                   meta, expected_sha256: d.card.sha256 } });
      }
      toast(res.note || '已保存', 'success', 5000);
      setDirty(false);
      await selectChar(state.cid, { force: true, keepTab: true });
      await loadList();
      await loadStats();
    } catch (e) {
      toast(e.message, 'error', 8000);
    } finally {
      btn.disabled = false;
    }
  });

  async function removeSection(index) {
    const d = state.detail;
    if (!d || d.readonly) return;
    const sec = (d.parsed.sections || [])[index];
    if (!sec) return;
    if (!confirm(`确定删除段落「${sec.heading}」？（会先备份）`)) return;
    try {
      const res = await api('/api/characters/' + encodeURIComponent(state.cid) + '/repair',
        { method: 'POST', body: { action: 'remove_section', payload: { index } } });
      toast(res.note || '已删除段落', 'success');
      setDirty(false);
      await selectChar(state.cid, { force: true, keepTab: true });
    } catch (e) { toast(e.message, 'error'); }
  }

  el('pm-add-section').addEventListener('click', async () => {
    const d = state.detail;
    if (!d || d.readonly) return;
    const heading = prompt('新段落的标题（例如 Relationship with User / 专属剧情 / Bond Tiers）');
    if (!heading || !heading.trim()) return;
    try {
      const res = await api('/api/characters/' + encodeURIComponent(state.cid) + '/repair',
        { method: 'POST', body: { action: 'add_section', payload: { heading: heading.trim() } } });
      toast(res.note || '已新增段落', 'success');
      setDirty(false);
      await selectChar(state.cid, { force: true, keepTab: true });
    } catch (e) { toast(e.message, 'error', 6000); }
  });

  el('pm-preview-btn').addEventListener('click', async () => {
    const box = el('pm-preview-body');
    el('pm-preview-meta').textContent = '构建中…';
    try {
      const d = await api('/api/characters/' + encodeURIComponent(state.cid) + '/preview');
      box.style.display = '';
      box.textContent = d.content;
      el('pm-preview-meta').textContent =
        `L1+L2 共 ${d.length} 字符 · 本卡 ${fmtSize(d.card_bytes)} ≈ ${d.card_tokens} tokens`;
    } catch (e) {
      el('pm-preview-meta').textContent = '失败: ' + e.message;
    }
  });

  // ── 元数据 ─────────────────────────────────────────────────────
  function renderMeta() {
    const d = state.detail;
    const m = d.meta || {};
    const known = ['char_id', 'display_name', 'author', 'created_at', 'visibility',
                   'voice_id', 'source', 'initial_bond_level'];
    const voices = (d.voices || {}).pool || [];
    el('mt-voice-list').innerHTML = voices.map(v => `<option value="${esc(v)}"></option>`).join('');
    el('mt-name').value = m.display_name || '';
    el('mt-visibility').value = m.visibility || 'private';
    el('mt-voice').value = m.voice_id || '';
    el('mt-bond').value = m.initial_bond_level != null ? m.initial_bond_level : 1;
    el('mt-auto-voice').textContent = d.auto_voice
      ? `不指定时按 char_id 自动分配：${d.auto_voice}` : '';
    el('mt-author').textContent = `author = ${m.author || '(缺失)'} · source = ${m.source || 'ugc'}`
      + ` · created ${m.created_at ? fmtTime(m.created_at) : '—'}`;
    el('mt-author-input').value = m.author || '';

    const unknown = {};
    Object.keys(m).forEach(k => { if (known.indexOf(k) < 0) unknown[k] = m[k]; });
    el('mt-unknown').textContent = Object.keys(unknown).length
      ? JSON.stringify(unknown, null, 2) : '(无)';
    const disabled = !!d.readonly;
    ['mt-name', 'mt-visibility', 'mt-voice', 'mt-bond', 'mt-save', 'mt-author-input',
     'mt-author-btn'].forEach(id => { const n = el(id); if (n) n.disabled = disabled; });
  }

  el('mt-save').addEventListener('click', async () => {
    const d = state.detail;
    if (!d || d.readonly) return;
    const patch = {
      display_name: el('mt-name').value.trim(),
      visibility: el('mt-visibility').value,
      voice_id: el('mt-voice').value.trim(),
      initial_bond_level: Number(el('mt-bond').value),
    };
    try {
      const res = await api('/api/characters/' + encodeURIComponent(state.cid) + '/meta',
        { method: 'PUT', body: { patch, sync_h1: el('mt-sync-h1').checked,
                                 expected_sha256: d.meta_state.sha256 } });
      toast(res.note || '元数据已保存', 'success', 5000);
      await selectChar(state.cid, { force: true, keepTab: true });
      await loadList();
      await loadStats();
    } catch (e) { toast(e.message, 'error', 8000); }
  });

  el('mt-author-btn').addEventListener('click', async () => {
    const author = el('mt-author-input').value.trim();
    if (!author) { toast('请输入新的 uid', 'error'); return; }
    if (!confirm(`确定把归属改派给 ${author}？原作者的绑定会失效（羁绊数据保留）。`)) return;
    try {
      const res = await api('/api/characters/' + encodeURIComponent(state.cid) + '/repair',
        { method: 'POST', body: { action: 'set_author', payload: { author } } });
      toast(res.note || '已转移归属', 'success');
      await selectChar(state.cid, { force: true, keepTab: true });
      await loadAuthors();
      await loadList();
    } catch (e) { toast(e.message, 'error', 8000); }
  });

  // ── 形象 ───────────────────────────────────────────────────────
  function renderPortrait() {
    const d = state.detail;
    const img = el('pt-img');
    const bust = '?v=' + Date.now();
    img.src = '/api/characters/' + encodeURIComponent(d.char_id) + '/portrait' + bust;
    img.onerror = () => {
      img.removeAttribute('src');
      img.style.background = 'repeating-linear-gradient(45deg,#0a0e17,#0a0e17 10px,#111827 10px,#111827 20px)';
      el('pt-info').innerHTML = '<span class="muted">未设置参考立绘</span>';
    };
    img.onload = () => { img.style.background = '#0a0e17'; };
    if (d.files && d.files.portrait) {
      el('pt-info').innerHTML = `已设置 · ${fmtSize((d.files || {}).assets_bytes)}（含 assets/）`;
    }
    el('pt-note').innerHTML = d.readonly
      ? '<span class="warn">内置人格的形象是<b>全局设置</b>：覆盖层写在数据盘，影响所有使用该人格的用户；清除后退回代码盘出厂图。</span>'
      : '仅影响该作者的这个角色。平台管理员在 QQ 里用 <code>/设形象</code> 走的也是同一条落盘通路。';
  }

  el('pt-upload').addEventListener('click', async () => {
    const file = el('pt-file').files && el('pt-file').files[0];
    if (!file) { toast('先选择一张图片', 'error'); return; }
    if (file.size > 10 * 1024 * 1024) { toast('原图超过 10 MB', 'error'); return; }
    const btn = el('pt-upload');
    btn.disabled = true;
    try {
      const dataB64 = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(new Error('读取文件失败'));
        reader.readAsDataURL(file);
      });
      const res = await api('/api/characters/' + encodeURIComponent(state.cid) + '/portrait',
        { method: 'POST', body: { data_b64: dataB64, filename: file.name } });
      toast(`已保存 ${res.describe || ''}`, 'success', 5000);
      await selectChar(state.cid, { force: true, keepTab: true });
    } catch (e) {
      toast(e.message, 'error', 8000);
    } finally { btn.disabled = false; }
  });

  el('pt-clear').addEventListener('click', async () => {
    if (!confirm('确定清除参考立绘？内置人格会回退到出厂图。')) return;
    try {
      const res = await api('/api/characters/' + encodeURIComponent(state.cid) + '/portrait',
        { method: 'DELETE' });
      toast(res.note || '已清除', 'success');
      await selectChar(state.cid, { force: true, keepTab: true });
    } catch (e) { toast(e.message, 'error'); }
  });

  // ── 羁绊与绑定 ─────────────────────────────────────────────────
  function renderBond() {
    const d = state.detail;
    const b = d.bond;
    el('bd-bond').innerHTML = b
      ? `<table class="kv-table">
           <tr><td>用户</td><td class="mono">${esc(b.uid)}</td></tr>
           <tr><td>等级 / EXP</td><td>Lv${esc(String(b.level))} · ${esc(String(b.exp))}</td></tr>
           <tr><td>心情</td><td>${esc(String(b.mood))}</td></tr>
           <tr><td>消息 / 礼物</td><td>${esc(String(b.msgs || 0))} / ${esc(String(b.gifts || 0))}</td></tr>
           <tr><td>首次 / 最近</td><td>${fmtTime(b.first_met)} / ${fmtTime(b.last_seen)}</td></tr>
         </table>`
      : '<span class="muted">暂无羁绊记录（作者还没与该角色相处过）</span>';

    const users = (d.bindings || {}).users || [];
    el('bd-users').innerHTML = users.length
      ? users.map(u => `<span class="pill mono">${esc(u.uid)}</span>`).join(' ')
      : '<span class="muted">没有用户把它设为个人人格</span>';
    const groups = (d.bindings || {}).groups || [];
    el('bd-groups').innerHTML = groups.length
      ? groups.map(g => `<span class="pill mono">${esc(g.gid)}</span>`).join(' ')
      : '<span class="muted">没有群把它设为群默认</span>';
  }

  // ── 版本 ───────────────────────────────────────────────────────
  async function loadVersions() {
    if (!state.cid) return;
    try {
      const d = await api('/api/characters/' + encodeURIComponent(state.cid) + '/versions');
      el('vs-root').textContent = d.root || '';
      const rows = d.versions || [];
      if (!rows.length) {
        el('vs-list').innerHTML = '<span class="muted">还没有版本快照（每次保存前自动生成）</span>';
        return;
      }
      el('vs-list').innerHTML = rows.map(v => `
        <div class="char-item" data-ts="${esc(v.ts)}">
          <div class="row">
            <span class="mono">${esc(v.ts)}</span>
            <span class="spacer"></span>
            <span class="muted" style="font-size:11px">${esc(v.action || '')}</span>
          </div>
          <div class="char-item-sub">${fmtTime(v.at)} · ${fmtSize(v.card_bytes)} · ${
            esc(v.card_sha256 || '')}${v.has_meta ? '' : ' · 无 meta'}</div>
        </div>`).join('');
      el('vs-list').querySelectorAll('.char-item').forEach(node => {
        node.addEventListener('click', () => openVersion(node.dataset.ts, node));
      });
    } catch (e) {
      el('vs-list').innerHTML = `<span class="muted">加载失败: ${esc(e.message)}</span>`;
    }
  }

  async function openVersion(ts, node) {
    document.querySelectorAll('#vs-list .char-item').forEach(n => n.classList.remove('active'));
    if (node) node.classList.add('active');
    state.version = ts;
    el('vs-selected').textContent = '版本 ' + ts;
    el('vs-restore').disabled = false;
    try {
      const d = await api('/api/characters/' + encodeURIComponent(state.cid) +
                          '/versions/' + encodeURIComponent(ts));
      const lines = d.diff || [];
      el('vs-diff').innerHTML = lines.length
        ? lines.map(l => {
            const cls = l.startsWith('+') && !l.startsWith('+++') ? 'diff-add'
              : l.startsWith('-') && !l.startsWith('---') ? 'diff-del' : 'diff-ctx';
            return `<span class="${cls}">${esc(l)}</span>`;
          }).join('\n')
        : '<span class="muted">与当前内容一致</span>';
    } catch (e) {
      el('vs-diff').textContent = '加载失败: ' + e.message;
    }
  }

  el('vs-refresh').addEventListener('click', loadVersions);
  el('vs-restore').addEventListener('click', async () => {
    if (!state.version) return;
    if (!confirm(`回滚到版本 ${state.version}？当前内容会先被快照。`)) return;
    try {
      const res = await api('/api/characters/' + encodeURIComponent(state.cid) + '/restore',
        { method: 'POST', body: { ts: state.version } });
      toast(res.note || '已回滚', 'success', 5000);
      setDirty(false);
      await selectChar(state.cid, { force: true, keepTab: true });
      await loadVersions();
    } catch (e) { toast(e.message, 'error', 8000); }
  });

  // ── 体检与修复 ─────────────────────────────────────────────────
  const FIX_ACTIONS = {
    meta_corrupt: { label: '重建 meta.json', action: 'meta_rebuild', needAuthor: true },
    meta_missing: { label: '补建 meta.json', action: 'meta_rebuild', needAuthor: true },
    author_missing: { label: '重建 meta.json', action: 'meta_rebuild', needAuthor: true },
    name_drift: { label: '用 display_name 重写 H1', action: 'sync_name',
                  payload: { direction: 'meta_to_h1' } },
    char_id_mismatch: { label: '对齐 meta.char_id', action: 'fix_char_id' },
    orphan_dir: { label: '清理僵尸目录', action: 'delete_orphan' },
  };

  function renderLint() {
    const d = state.detail;
    const issues = d.issues || [];
    el('ln-issues').innerHTML = issues.length
      ? issues.map(i => `<div class="row" style="gap:8px;padding:3px 0">
            <span class="pill ${i.level === 'error' ? 'badge-err'
              : i.level === 'warn' ? 'badge-warn' : ''}">${esc(i.code)}</span>
            <span>${esc(i.msg)}</span></div>`).join('')
      : '<span class="ok">没有发现问题</span>';

    const lint = d.lint || { errors: [], warnings: [], infos: [] };
    const block = (title, list, cls) => list.length
      ? `<div style="margin-bottom:6px"><span class="${cls}">${title}</span><ul class="char-lint">` +
        list.map(i => `<li><span class="mono muted">${esc(i.id)}</span> ${esc(i.msg)}</li>`).join('') +
        '</ul></div>' : '';
    el('ln-lint').innerHTML =
      block('错误（会阻断保存）', lint.errors || [], 'err') +
      block('告警', lint.warnings || [], 'warn') +
      block('提示', lint.infos || [], 'muted') ||
      '<span class="ok">体检通过</span>';

    const btns = [];
    const seen = {};
    issues.forEach(i => {
      const fix = FIX_ACTIONS[i.code];
      if (!fix || seen[fix.action + (fix.payload ? JSON.stringify(fix.payload) : '')]) return;
      seen[fix.action + (fix.payload ? JSON.stringify(fix.payload) : '')] = true;
      btns.push(`<button class="btn btn-sm" data-fix="${fix.action}"
        data-payload='${esc(JSON.stringify(fix.payload || {}))}'>${esc(fix.label)}</button>`);
    });
    if (d.meta && d.meta.display_name && d.parsed &&
        d.parsed.h1 && d.meta.display_name !== d.parsed.h1) {
      btns.push(`<button class="btn btn-sm" data-fix="sync_name"
        data-payload='{"direction":"h1_to_meta"}'>用 H1 重写 display_name</button>`);
    }
    if (d.meta && d.meta.voice_id) {
      btns.push('<button class="btn btn-sm" data-fix="set_voice_auto" data-payload="{}">' +
                '音色恢复自动分配</button>');
    }
    el('ln-fixes').innerHTML = btns.join(' ') ||
      '<span class="muted">当前没有针对性修复动作</span>';
    el('ln-fixes').querySelectorAll('[data-fix]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const action = btn.dataset.fix;
        let payload = {};
        try { payload = JSON.parse(btn.dataset.payload || '{}'); } catch (e) { payload = {}; }
        if (action === 'meta_rebuild') {
          const author = prompt('重建 meta.json 需要指定 author（决定谁能看到该角色）',
                                (state.detail.meta || {}).author || '');
          if (!author) return;
          payload.author = author;
        }
        try {
          const res = await api('/api/characters/' + encodeURIComponent(state.cid) + '/repair',
            { method: 'POST', body: { action, payload } });
          toast(res.note || '已修复', 'success', 5000);
          await selectChar(state.cid, { force: true, keepTab: true });
          await loadList();
          await loadStats();
        } catch (e) { toast(e.message, 'error', 8000); }
      });
    });
  }

  el('ln-review').addEventListener('click', async () => {
    const out = el('ln-review-result');
    out.textContent = '审查中…';
    try {
      const d = await api('/api/characters/' + encodeURIComponent(state.cid) + '/review',
        { method: 'POST', body: {} });
      out.innerHTML = d.approved
        ? '<span class="ok">通过（APPROVE）</span>'
        : `<span class="err">未通过：${esc(d.reason)}</span>`;
    } catch (e) {
      out.innerHTML = `<span class="err">失败: ${esc(e.message)}</span>`;
    }
  });

  // ── 删除 / 重新加载 ────────────────────────────────────────────
  el('char-refresh').addEventListener('click', () => {
    if (state.dirty && !confirm('放弃未保存的改动，重新从磁盘加载？')) return;
    setDirty(false);
    selectChar(state.cid, { force: true, keepTab: true });
  });

  el('char-delete').addEventListener('click', async () => {
    const d = state.detail;
    if (!d || d.readonly) return;
    const name = prompt(`危险操作：删除「${d.display_name}」\n` +
      '会先整树备份到面板备份目录，然后清掉用户/群绑定引用（羁绊数据保留）。\n' +
      `请输入角色显示名以确认：`);
    if (name == null) return;
    try {
      const res = await api('/api/characters/' + encodeURIComponent(state.cid),
        { method: 'DELETE', body: { confirm_name: name.trim() } });
      toast(res.note || '已删除', 'success', 6000);
      state.cid = null; state.detail = null;
      el('char-detail').style.display = 'none';
      el('char-empty').style.display = '';
      setDirty(false);
      await loadList();
      await loadStats();
    } catch (e) { toast(e.message, 'error', 8000); }
  });

  // ── 新建（管理员代办）──────────────────────────────────────────
  function showNewMask(show) {
    el('new-mask').style.display = show ? 'flex' : 'none';
  }
  el('char-new').addEventListener('click', () => showNewMask(true));
  el('new-cancel').addEventListener('click', () => showNewMask(false));
  el('new-mask').addEventListener('click', ev => {
    if (ev.target === el('new-mask')) showNewMask(false);
  });

  el('new-submit').addEventListener('click', async () => {
    const fields = {
      name: el('new-name').value.trim(),
      gender: el('new-gender').value.trim(),
      personality: el('new-personality').value.trim(),
      speech_style: el('new-speech').value.trim(),
      appearance: el('new-appearance').value.trim(),
      worldview: el('new-worldview').value.trim(),
      relationship: el('new-relationship').value.trim(),
      greeting: el('new-greeting').value.trim(),
      initial_bond_level: Number(el('new-bond').value) || 1,
    };
    const gifts = el('new-gift').value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
    if (gifts.length) fields.gift_loved = gifts;
    const author = el('new-author').value.trim();
    if (!author || !fields.name || !fields.personality || !fields.speech_style ||
        !fields.appearance) {
      el('new-status').textContent = '作者、角色名、性格、说话风格、外观锚点都是必填';
      return;
    }
    const btn = el('new-submit');
    btn.disabled = true;
    el('new-status').textContent = el('new-skip-review').checked ? '创建中…' : '创建中（含内容审查，约 10–30 秒）…';
    try {
      const res = await api('/api/characters', {
        method: 'POST',
        body: {
          author, fields,
          allow_quota_bypass: el('new-bypass').checked,
          skip_review: el('new-skip-review').checked,
        },
      });
      toast(res.note || '已创建', 'success', 6000);
      showNewMask(false);
      el('new-status').textContent = '';
      await loadAuthors();
      await loadList();
      await loadStats();
      if (res.char_id) selectChar(res.char_id, { force: true });
    } catch (e) {
      el('new-status').textContent = e.message;
      toast(e.message, 'error', 8000);
    } finally { btn.disabled = false; }
  });

  // ── 初始化 ─────────────────────────────────────────────────────
  ['char-author', 'char-source', 'char-issue'].forEach(id =>
    el(id).addEventListener('change', loadList));
  let searchTimer = null;
  el('char-search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadList, 250);
  });

  (async function init() {
    await loadStats();
    await loadAuthors();
    await loadList();
    const hash = decodeURIComponent((location.hash || '').replace(/^#/, ''));
    if (hash && hash !== '') selectChar(hash);
  })();
})();
