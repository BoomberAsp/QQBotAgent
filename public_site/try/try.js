(function () {
  'use strict';

  const byId = (id) => document.getElementById(id);
  const provider = byId('provider');
  const model = byId('model');
  const key = byId('api-key');
  const probe = byId('probe');
  const connectionStatus = byId('connection-status');
  const persona = byId('personality');
  const log = byId('chat-log');
  const starter = byId('starter');
  const example = byId('example');
  const message = byId('message');
  const send = byId('send');
  const clear = byId('clear-chat');
  const chatStatus = byId('chat-status');
  const marks = { assistant: 'R', roxy_character: 'R', rubi: '露', herta: '塔' };

  let verification = '';
  let expiryTimer;
  let busy = false;
  let history = [];

  function status(element, text, kind) {
    element.textContent = text;
    element.dataset.kind = kind || '';
  }

  function refreshControls() {
    const ready = !busy && Boolean(verification);
    for (const field of [provider, model, key, persona]) field.disabled = busy;
    probe.disabled = busy;
    message.disabled = !ready;
    send.disabled = !ready || !message.value.trim();
    example.disabled = !ready;
    clear.disabled = busy || !history.length;
    starter.hidden = Boolean(history.length || log.childElementCount);
    message.placeholder = verification ? '输入你想说的话' : '请先测试模型连接';
  }

  function resetConversation() {
    history = [];
    log.replaceChildren();
    message.value = '';
    status(chatStatus, '');
  }

  function clearVerification(text) {
    window.clearTimeout(expiryTimer);
    verification = '';
    status(connectionStatus, text);
  }

  function resetConfig() {
    clearVerification('配置已更改，请测试连接。');
    resetConversation();
    refreshControls();
  }

  function selectPersona() {
    byId('chat-panel').dataset.persona = persona.value;
    byId('chat-title').textContent = persona.selectedOptions[0].textContent;
    byId('persona-mark').textContent = marks[persona.value];
  }

  function config() {
    return { provider: provider.value, model: model.value.trim(), api_key: key.value.trim() };
  }

  async function post(action, payload, timeout) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch('./api/' + action, {
        method: 'POST',
        credentials: 'omit',
        headers: { 'Content-Type': 'application/json' },
        cache: 'no-store',
        signal: controller.signal,
        body: JSON.stringify(payload),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || result.ok !== true) {
        const failure = new Error(result.error || (response.status === 429
          ? '请求过于频繁，请稍后再试。' : '试聊服务暂时不可用。'));
        failure.status = result.status;
        throw failure;
      }
      return result;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function requestMessage(error) {
    if (error.name === 'AbortError') return '请求超时，请稍后重试。';
    if (error instanceof TypeError) return '无法连接到试聊服务。';
    return error.message;
  }

  function addMessage(role, text) {
    const row = document.createElement('article');
    row.className = 'try-turn try-turn--' + role;
    const label = document.createElement('span');
    label.textContent = role === 'user' ? '你' : persona.selectedOptions[0].textContent;
    const bubble = document.createElement('p');
    bubble.textContent = text;
    row.append(label, bubble);
    log.append(row);
    log.scrollTop = log.scrollHeight;
    return row;
  }

  byId('config-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;
    clearVerification('正在测试连接…');
    busy = true;
    refreshControls();
    try {
      const result = await post('probe', config(), 18500);
      verification = result.verification;
      expiryTimer = window.setTimeout(() => {
        clearVerification('连接验证已过期，请重新测试。');
        refreshControls();
      }, result.expires_in * 1000);
      status(connectionStatus, '连接成功 · ' + result.elapsed_ms + ' ms', 'ok');
    } catch (error) {
      status(connectionStatus, requestMessage(error), 'error');
    } finally {
      busy = false;
      refreshControls();
    }
    if (verification) message.focus({ preventScroll: true });
  });

  byId('chat-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = message.value.trim();
    if (!verification || busy || !text) return;
    busy = true;
    message.value = '';
    const pending = addMessage('user', text);
    status(chatStatus, '正在等待模型回复…');
    refreshControls();
    try {
      const result = await post('chat', {
        ...config(),
        verification,
        personality: persona.value,
        message: text,
        history,
      }, 39000);
      addMessage('assistant', result.reply);
      history = [...history, { role: 'user', content: text }, { role: 'assistant', content: result.reply }].slice(-8);
      status(chatStatus, '');
    } catch (error) {
      pending.remove();
      message.value = text;
      if (['auth_failed', 'verification_required'].includes(error.status)) {
        clearVerification('请检查配置并重新测试连接。');
      }
      status(chatStatus, requestMessage(error), 'error');
    } finally {
      busy = false;
      refreshControls();
    }
    if (verification) message.focus({ preventScroll: true });
  });

  provider.addEventListener('change', () => {
    model.value = provider.value === 'deepseek' ? 'deepseek-flash' : '';
    key.value = '';
    resetConfig();
  });
  for (const input of [model, key]) input.addEventListener('input', resetConfig);
  persona.addEventListener('change', () => {
    selectPersona();
    resetConversation();
    refreshControls();
  });
  message.addEventListener('input', refreshControls);
  example.addEventListener('click', () => {
    message.value = example.textContent;
    refreshControls();
    message.focus();
  });
  clear.addEventListener('click', () => {
    resetConversation();
    refreshControls();
  });

  const selected = new URLSearchParams(window.location.search).get('persona');
  if (Object.hasOwn(marks, selected)) persona.value = selected;
  model.value = provider.value === 'deepseek' ? 'deepseek-flash' : '';
  selectPersona();
  refreshControls();
})();
