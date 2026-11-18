(() => {
  const $ = id => document.getElementById(id);
  const jsonPost = data => ({method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
  let entries = new Map();
  async function api(path, options = {}) {
    const response = await fetch('/api/resources' + path, {...options, headers: {'X-Resource-Admin': '1', ...(options.headers || {})}});
    const data = await response.json();
    if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data));
    return data;
  }
  function option(select, value, text) {
    const item = document.createElement('option');
    item.value = value; item.textContent = text; select.append(item);
  }
  function selected() { return entries.get($('kb').value); }
  function source() { return selected()?.data.sources?.find(s => s.id === $('source').value); }
  function updateInfo(resetSql) {
    const entry = selected(), item = source();
    $('query').disabled = !entry || (entry.kind === 'story' && !item?.available);
    $('story-search').disabled = !entry || !entry.data.available || !$('story-persona').value;
    if (!entry) { $('kb-info').textContent = '暂无已注册知识库'; return; }
    if (entry.kind === 'json') {
      $('kb-info').textContent = 'JSON 资料库：' + entry.id + '\n查询表：documents';
      if (resetSql) $('sql').value = 'SELECT id, title, chapter, visibility FROM documents LIMIT 50';
      return;
    }
    const bindings = Object.entries(entry.data.bindings).map(([p, b]) => p + '（自动注入' + (b.auto_inject === false ? '关闭' : '开启') + '）').join('、') || '无';
    const lines = [entry.data.title + ' · ' + (entry.data.available ? '可用' : '不可用'), '绑定人格：' + bindings];
    if (entry.data.error) lines.push('错误：' + entry.data.error);
    if (item) {
      lines.push('来源：' + item.id + ' · ' + (item.available ? '可用' : '不可用'), '文件：' + item.path);
      if (item.error) lines.push('错误：' + item.error);
      if (item.meta?.build_id) lines.push('构建：' + item.meta.build_id);
      if (item.meta?.schema_version) lines.push('数据库版本：' + item.meta.schema_version);
      if (item.tables?.length) lines.push('可查询表：' + item.tables.join('、'));
      if (resetSql) $('sql').value = item.example_sql;
    }
    $('kb-info').textContent = lines.join('\n');
  }
  function choose(resetSql = true) {
    const entry = selected(), story = entry?.kind === 'story';
    $('source-label').hidden = !story;
    $('story-search-panel').hidden = !story;
    $('source').replaceChildren(); $('story-persona').replaceChildren();
    $('story-result').textContent = ''; $('results').replaceChildren();
    if (story) {
      for (const item of entry.data.sources) option($('source'), item.id, item.id + (item.available ? '' : '（不可用）'));
      const available = entry.data.sources.find(s => s.available);
      if (available) $('source').value = available.id;
      for (const persona of Object.keys(entry.data.bindings)) option($('story-persona'), persona, persona);
      $('story-keywords').placeholder = entry.data.game === 'hsr' ? '例如：卡芙卡 道别' : '例如：露比 雨夜';
    }
    updateInfo(resetSql);
  }
  async function refresh() {
    const previous = $('kb').value;
    const results = await Promise.allSettled([api(''), api('/stories')]);
    entries = new Map(); $('kb').replaceChildren();
    if (results[1].status === 'fulfilled') {
      for (const [id, data] of Object.entries(results[1].value.knowledge_bases || {})) {
        const key = 'story:' + id;
        entries.set(key, {kind: 'story', id, data});
        option($('kb'), key, data.title + ' [' + id + '] · ' + (data.available ? '可用' : '不可用'));
      }
    }
    if (results[0].status === 'fulfilled') {
      for (const [id, data] of Object.entries(results[0].value.knowledge_bases || {})) {
        const key = 'json:' + id;
        entries.set(key, {kind: 'json', id, data});
        option($('kb'), key, 'JSON · ' + id);
      }
    }
    if (entries.has(previous)) $('kb').value = previous;
    choose(!entries.has(previous));
    const errors = results.filter(r => r.status === 'rejected').map(r => r.reason.message);
    if (results[1].status === 'fulfilled' && results[1].value.error) errors.push(results[1].value.error);
    $('status').textContent = errors.length ? errors.join('\n') : '已加载 ' + entries.size + ' 个知识库';
  }
  function action(id, callback, target = 'status') {
    $(id).onclick = async () => {
      $(id).disabled = true;
      try { $(target).textContent = '处理中…'; await callback(); }
      catch (error) { $(target).textContent = error.message; }
      finally { $(id).disabled = false; updateInfo(false); }
    };
  }
  $('kb').onchange = () => { choose(); $('status').textContent = ''; };
  $('source').onchange = () => updateInfo(true);
  action('refresh', refresh);
  action('story-search', async () => {
    const entry = selected();
    if (!entry || entry.kind !== 'story') throw Error('请选择剧情库');
    const data = await api('/story-search', jsonPost({kb: entry.id, persona: $('story-persona').value, query: $('story-keywords').value.trim()}));
    $('story-result').textContent = '查询人格：' + data.persona + '\n\n' + data.text + (data.auto_reference ? '\n\n自动注入预览：\n' + data.auto_reference : '');
  }, 'story-result');
  action('query', async () => {
    const entry = selected();
    if (!entry) throw Error('请选择知识库');
    const data = await api(entry.kind === 'story' ? '/story-query' : '/query', jsonPost({kb: entry.id, source: $('source').value, sql: $('sql').value}));
    $('results').replaceChildren();
    for (const [index, row] of [data.columns, ...data.rows].entries()) {
      const tr = document.createElement('tr');
      for (const value of row) {
        const cell = document.createElement(index === 0 ? 'th' : 'td');
        cell.textContent = value === null ? 'NULL' : String(value); tr.append(cell);
      }
      $('results').append(tr);
    }
    $('status').textContent = `返回 ${data.rows.length} 行` + (data.truncated ? '（已截断）' : '');
  });
  action('publish', async () => {
    const file = $('docs').files[0];
    if (!file) throw Error('请选择 JSON 文件');
    if (file.size > 8 * 1024 * 1024) throw Error('文件超过 8 MiB');
    const documents = JSON.parse(await file.text());
    if (!confirm('确认发布并替换该 JSON 知识库的人格绑定？')) return;
    await api('/knowledge', jsonPost({id: $('kid').value.trim(), personas: $('personas').value.split(',').map(p => p.trim()).filter(Boolean), documents}));
    await refresh(); $('publish-status').textContent = '已发布，下轮 JSON 资料检索生效';
  }, 'publish-status');
  refresh().catch(error => { $('status').textContent = error.message; });
})();
