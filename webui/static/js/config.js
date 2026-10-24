/* Config hot-management page: prompts / permissions / models / group features / personality */
(function () {
  let currentPrompt = null;
  let promptMtime = null;

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  // ── Tab switching ──────────────────────────────────────────────
  document.querySelectorAll('#cfg-tabs .tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#cfg-tabs .tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const name = tab.dataset.tab;
      document.querySelectorAll('.cfg-pane').forEach(p => { p.style.display = 'none'; });
      const pane = document.getElementById('pane-' + name);
      if (pane) pane.style.display = '';
    });
  });

  // ── Prompts ────────────────────────────────────────────────────
  async function loadPromptList() {
    const box = document.getElementById('prompt-list');
    try {
      const list = await api('/api/config/prompts');
      if (!list.length) { box.innerHTML = '<span class="muted">无 .md 配置文件</span>'; return; }
      box.innerHTML = list.map(f =>
        `<div class="prompt-item" data-name="${esc(f.name)}"
              style="padding:5px 8px;border-radius:6px;cursor:pointer;font-size:13px">
           <span class="mono">${esc(f.name)}</span>
           <span class="muted" style="float:right;font-size:11px">${fmtBytes(f.size)}</span>
         </div>`).join('');
      box.querySelectorAll('.prompt-item').forEach(el => {
        el.addEventListener('click', () => openPrompt(el.dataset.name));
      });
    } catch (e) {
      box.innerHTML = `<span class="muted">加载失败: ${esc(e.message)}</span>`;
    }
  }

  function markActive(name) {
    document.querySelectorAll('#prompt-list .prompt-item').forEach(el => {
      el.style.background = el.dataset.name === name ? 'rgba(120,160,255,.15)' : '';
    });
  }

  async function openPrompt(name) {
    const editor = document.getElementById('prompt-editor');
    const saveBtn = document.getElementById('prompt-save');
    try {
      const d = await api('/api/config/prompts/' + encodeURIComponent(name).replace(/%2F/g, '/'));
      currentPrompt = name;
      promptMtime = d.mtime;
      document.getElementById('prompt-name').textContent = name;
      document.getElementById('prompt-meta').textContent =
        new Date(d.mtime * 1000).toLocaleString('zh-CN', { hour12: false });
      editor.value = d.content;
      editor.disabled = false;
      saveBtn.disabled = false;
      markActive(name);
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  document.getElementById('prompt-save').addEventListener('click', async () => {
    if (!currentPrompt) return;
    const url = '/api/config/prompts/' + encodeURIComponent(currentPrompt).replace(/%2F/g, '/');
    try {
      const d = await api(url, {
        method: 'PUT',
        body: { content: document.getElementById('prompt-editor').value },
      });
      toast(d.note || '已保存', 'success', 5000);
      openPrompt(currentPrompt);   // refresh mtime meta
      loadPromptList();            // refresh sizes
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  // Warn if leaving with unsaved changes
  window.addEventListener('beforeunload', (ev) => {
    const editor = document.getElementById('prompt-editor');
    if (!editor.disabled && editor.value !== undefined && editor.dataset.dirty === '1') {
      ev.preventDefault();
      ev.returnValue = '';
    }
  });
  document.getElementById('prompt-editor').addEventListener('input', function () {
    this.dataset.dirty = '1';
  });
  document.getElementById('prompt-save').addEventListener('click', () => {
    document.getElementById('prompt-editor').dataset.dirty = '';
  });

  // ── Permissions (OneBot QQ-number tables + QQ-official openid tables) ──
  const PERM_FIELDS = [
    { el: 'perm-super', key: 'SUPERUSERS', field: 'superusers' },
    { el: 'perm-vip', key: 'VIP_USERS', field: 'vip_users' },
    { el: 'perm-official-super', key: 'QQ_OFFICIAL_SUPERUSERS', field: 'qq_official_superusers' },
    { el: 'perm-official-vip', key: 'QQ_OFFICIAL_VIP_USERS', field: 'qq_official_vip_users' },
  ];

  async function loadPermissions() {
    try {
      const d = await api('/api/config/permissions');
      for (const f of PERM_FIELDS) {
        document.getElementById(f.el).value = d[f.key] || '';
      }
      document.getElementById('perm-note').textContent = d.note || '';
    } catch (e) {
      document.getElementById('perm-note').textContent = '加载失败: ' + e.message;
    }
  }

  document.getElementById('perm-save').addEventListener('click', async () => {
    try {
      const body = {};
      for (const f of PERM_FIELDS) {
        body[f.field] = document.getElementById(f.el).value;
      }
      const d = await api('/api/config/permissions', {
        method: 'PUT',
        body,
      });
      toast(d.note || '已保存', 'success');
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  // ── Models: structured section cards + raw JSON fallback ───────
  // extras kinds mirror webui/config_editor._SECTION_EXTRAS:
  //   number → int/float, text → str, list → str_list (comma-separated input)
  const TEXT_EXTRAS = [
    { name: 'max_tokens', label: 'max_tokens', type: 'number', min: 1, step: 'any' },
    { name: 'temperature', label: 'temperature', type: 'number', min: 0, max: 2, step: 'any' },
  ];
  const MEDIA_EXTRAS = [
    { name: 'timeout', label: 'timeout（秒）', type: 'number', min: 1, step: 'any' },
    { name: 'max_retries', label: 'max_retries', type: 'number', min: 0, step: 1 },
    { name: 'max_concurrency', label: 'max_concurrency ※', type: 'number', min: 1, step: 1 },
  ];
  const MODEL_SECTIONS = [
    { key: 'REASONING_MODEL', label: '推理模型 REASONING', desc: '复杂任务主推理', extras: TEXT_EXTRAS },
    { key: 'FLASH_MODEL', label: '轻量模型 FLASH', desc: '复杂度分类与简单任务', extras: TEXT_EXTRAS },
    { key: 'MULTIMODAL_MODEL', label: '多模态模型 MULTIMODAL', desc: '图片理解', extras: TEXT_EXTRAS },
    { key: 'AUDIO_MODEL', label: '音频模型 AUDIO', desc: '语音/音频分析', extras: TEXT_EXTRAS },
    { key: 'OCR_MODEL', label: 'OCR 模型', desc: '截图文字识别', extras: TEXT_EXTRAS },
    {
      key: 'IMAGE_GEN_MODEL', label: '图像生成 IMAGE_GEN', desc: '角色立绘/场景图（按张计费）',
      extras: MEDIA_EXTRAS, isMedia: true,
      apiBasePlaceholder: 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation',
      note: 'api_base 是完整端点 URL（DashScope 原生，不是 /chat/completions 基址）。'
          + '测试连接为空参数探测，不产生生成调用与费用。',
    },
    {
      key: 'TTS_MODEL', label: '语音合成 TTS', desc: '角色语音（按字符计费）',
      isMedia: true,
      extras: [
        ...MEDIA_EXTRAS,
        { name: 'format', label: 'format（mp3/wav/pcm）', type: 'text' },
        { name: 'sample_rate', label: 'sample_rate', type: 'number', min: 8000, step: 1 },
        { name: 'default_voice', label: 'default_voice（音色 ID）', type: 'text' },
        { name: 'voice_pool', label: 'voice_pool（逗号分隔音色 ID）', type: 'list' },
      ],
      apiBasePlaceholder: 'https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer',
      note: 'voice 音色不能跨模型混用；音色 ID 见百炼「Qwen-Audio-TTS 音色列表」。'
          + '测试连接为空参数探测，不产生合成费用。',
    },
  ];
  const MEDIA_RESTART_NOTE = '※ max_concurrency 在 bot 进程首次调用时锁定，修改后需重启 NoneBot；其余字段每次调用即时读取。';

  function attr(s) { return esc(s).replace(/"/g, '&quot;'); }

  function extraFieldHtml(f, s) {
    const v = s[f.name];
    const val = f.type === 'list'
      ? (Array.isArray(v) ? v.join(', ') : (v || ''))
      : (v != null ? v : '');
    const numAttrs = ['min', 'max', 'step']
      .filter(a => f[a] != null).map(a => `${a}="${f[a]}"`).join(' ');
    return `<label class="field" style="flex:1"><span>${esc(f.label)}</span>
      <input data-field="${f.name}" type="${f.type === 'number' ? 'number' : 'text'}"
             ${numAttrs} value="${attr(String(val))}"></label>`;
  }

  function modelCardHtml(sec, cfg) {
    const s = cfg || {};
    const maskedKey = s.api_key || '';   // already masked by backend
    const extras = sec.extras || TEXT_EXTRAS;
    // lay extras out in rows of two, like the original max_tokens/temperature row
    const rows = [];
    for (let i = 0; i < extras.length; i += 2) {
      rows.push(`<div class="row" style="gap:14px">${extras.slice(i, i + 2).map(f => extraFieldHtml(f, s)).join('')}</div>`);
    }
    const notes = [sec.note, sec.isMedia ? MEDIA_RESTART_NOTE : '']
      .filter(Boolean).map(t => `<p class="muted" style="font-size:12px;margin:6px 0 0">${esc(t)}</p>`).join('');
    return `<div class="card" data-section="${attr(sec.key)}" style="max-width:640px;margin:0">
      <div class="row" style="margin-bottom:8px">
        <h2 class="card-title" style="margin:0">${esc(sec.label)}</h2>
        <span class="spacer"></span>
        <span class="muted" style="font-size:12px">${esc(sec.desc)}</span>
      </div>
      <label class="field"><span>api_base</span>
        <input data-field="api_base" type="text" value="${attr(s.api_base || '')}"
               placeholder="${attr(sec.apiBasePlaceholder || 'https://api.deepseek.com')}"></label>
      <label class="field"><span>api_key（留空 = 维持原值）</span>
        <input data-field="api_key" type="password" value=""
               placeholder="${attr(maskedKey)}" autocomplete="new-password"></label>
      <label class="field"><span>model</span>
        <input data-field="model" type="text" value="${attr(s.model || '')}"></label>
      ${rows.join('')}
      ${notes}
      <div class="row" style="gap:10px;margin-top:8px">
        <button class="btn btn-sm btn-test">测试连接</button>
        <button class="btn btn-sm btn-primary btn-save">验证并保存本段</button>
        <span class="probe-status muted" style="font-size:12.5px"></span>
      </div>
    </div>`;
  }

  function renderModelCards(cfg) {
    document.getElementById('model-cards').innerHTML =
      MODEL_SECTIONS.map(sec => modelCardHtml(sec, cfg[sec.key])).join('');
  }

  function readCardFields(card) {
    const sec = MODEL_SECTIONS.find(x => x.key === card.dataset.section);
    const get = (name) => {
      const el = card.querySelector(`[data-field="${name}"]`);
      return el ? el.value.trim() : '';
    };
    // api_key convention: empty input → submit the masked placeholder,
    // backend _unmask_merge restores the stored original.
    const keyEl = card.querySelector('[data-field="api_key"]');
    const out = {
      api_base: get('api_base'),
      api_key: (keyEl && keyEl.value) ? keyEl.value.trim() : (keyEl ? keyEl.placeholder : ''),
      model: get('model'),
    };
    // extras: empty input = omit (backend keeps the stored value)
    for (const f of (sec && sec.extras) || TEXT_EXTRAS) {
      const raw = get(f.name);
      if (raw === '') continue;
      if (f.type === 'number') out[f.name] = Number(raw);
      else if (f.type === 'list') out[f.name] = raw.split(',').map(x => x.trim()).filter(Boolean);
      else out[f.name] = raw;
    }
    return out;
  }

  async function loadModels() {
    const note = document.getElementById('models-note');
    const cards = document.getElementById('model-cards');
    try {
      const d = await api('/api/config/models');
      document.getElementById('models-editor').value = d.content;
      note.textContent = d.note || '';
      try {
        renderModelCards(JSON.parse(d.content));
      } catch (pe) {
        cards.innerHTML = `<div class="card err" style="max-width:640px;margin:0">
          配置 JSON 解析失败，卡片不可用，请用下方「原始 JSON」修复：${esc(pe.message)}</div>`;
      }
    } catch (e) {
      note.textContent = '加载失败: ' + e.message;
      cards.innerHTML = `<p class="err">加载失败: ${esc(e.message)}</p>`;
    }
  }

  function setCardStatus(card, text, cls) {
    const el = card.querySelector('.probe-status');
    el.textContent = text;
    el.className = 'probe-status ' + cls;
  }

  document.getElementById('model-cards').addEventListener('click', async (ev) => {
    const card = ev.target.closest('.card[data-section]');
    if (!card) return;
    const section = card.dataset.section;
    const btns = card.querySelectorAll('button');

    if (ev.target.closest('.btn-test')) {
      btns.forEach(b => { b.disabled = true; });
      setCardStatus(card, '测试中…', 'muted');
      try {
        const d = await api('/api/config/models/test', {
          method: 'POST',
          body: { section, config: readCardFields(card) },
        });
        const p = d.probe || {};
        if (p.status === 'ok_with_warning') {
          setCardStatus(card, `⚠ ${p.detail} (${p.elapsed_ms}ms)`, 'warn');
        } else {
          setCardStatus(card, `✓ 连接成功 (${p.elapsed_ms}ms)`, 'ok');
        }
      } catch (e) {
        setCardStatus(card, '✗ ' + e.message, 'err');
      } finally {
        btns.forEach(b => { b.disabled = false; });
      }
      return;
    }

    if (ev.target.closest('.btn-save')) {
      btns.forEach(b => { b.disabled = true; });
      setCardStatus(card, '验证并保存中…', 'muted');
      const secDef = MODEL_SECTIONS.find(x => x.key === section);
      const hotNote = secDef && secDef.isMedia
        ? '媒体段每次调用即时生效（max_concurrency 需重启）'
        : 'bot 约 10 秒内热重载';
      try {
        const d = await api('/api/config/models/section', {
          method: 'PUT',
          body: { section, config: readCardFields(card) },
        });
        if (d.skipped) {
          setCardStatus(card, '✓ 已保存（凭据未变，跳过验证）', 'ok');
          toast('已保存，' + hotNote, 'success', 5000);
        } else {
          const p = d.probe || {};
          const mark = p.status === 'ok_with_warning' ? '⚠' : '✓';
          setCardStatus(card, `${mark} 验证通过 (${p.elapsed_ms}ms)，已保存`,
                        p.status === 'ok_with_warning' ? 'warn' : 'ok');
          toast('验证通过并已保存，' + hotNote, 'success', 5000);
        }
        setTimeout(loadModels, 1500);   // refresh masks + raw editor
      } catch (e) {
        setCardStatus(card, '✗ ' + e.message + '（未保存）', 'err');
        toast(e.message, 'error', 5000);
      } finally {
        btns.forEach(b => { b.disabled = false; });
      }
    }
  });

  document.getElementById('models-reload').addEventListener('click', loadModels);

  document.getElementById('models-save').addEventListener('click', async () => {
    const note = document.getElementById('models-note');
    try {
      const d = await api('/api/config/models', {
        method: 'PUT',
        body: { content: document.getElementById('models-editor').value },
      });
      toast((d.note || '已保存') + '（原始 JSON 未做 API 验证）', 'success', 5000);
      note.textContent = d.note || '';
      loadModels();   // re-read merged content so masks/cards are correct
    } catch (e) {
      toast(e.message, 'error');
      note.textContent = e.message;
    }
  });

  // ── Group features ─────────────────────────────────────────────
  async function loadGroup() {
    const note = document.getElementById('group-note');
    try {
      const d = await api('/api/config/group-features');
      document.getElementById('group-editor').value = d.content;
      note.textContent = d.exists === false
        ? '文件尚不存在（bot 运行后会生成），保存即创建。' + (d.note || '')
        : (d.note || '');
    } catch (e) {
      note.textContent = '加载失败: ' + e.message;
    }
  }

  document.getElementById('group-save').addEventListener('click', async () => {
    const note = document.getElementById('group-note');
    try {
      await api('/api/config/group-features', {
        method: 'PUT',
        body: { content: document.getElementById('group-editor').value },
      });
      toast('已保存，下条消息即生效', 'success');
      note.textContent = '已保存';
    } catch (e) {
      toast(e.message, 'error');
      note.textContent = e.message;
    }
  });

  // ── Personality ────────────────────────────────────────────────
  async function loadPersonality() {
    try {
      const d = await api('/api/config/personality');
      document.getElementById('pers-available').textContent =
        (d.available || []).join(' · ') || '—';
      if (!d.personality.error) {
        document.getElementById('pers-editor').value = d.personality.content;
      }
      if (!d.group_personality.error) {
        document.getElementById('pers-group-editor').value = d.group_personality.content;
      }
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  document.getElementById('pers-save').addEventListener('click', async () => {
    try {
      const d = await api('/api/config/personality', {
        method: 'PUT',
        body: {
          personality: document.getElementById('pers-editor').value,
          group_personality: document.getElementById('pers-group-editor').value,
        },
      });
      toast(d.note || '已保存', 'success');
      loadPersonality();
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  // ── Init ───────────────────────────────────────────────────────
  loadPromptList();
  loadPermissions();
  loadModels();
  loadGroup();
  loadPersonality();
})();
