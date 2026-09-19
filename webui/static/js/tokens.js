/* Token consumption dashboard (ECharts) */
(function () {
  const hasCharts = typeof echarts !== 'undefined';
  let dailyChart = null, modelChart = null;

  function pct(r) { return r === null || r === undefined ? '—' : (r * 100).toFixed(1) + '%'; }

  function table(el, head, rows) {
    el.innerHTML = '<thead><tr>' + head.map(h => `<th>${h}</th>`).join('') +
      '</tr></thead><tbody>' +
      (rows.length ? rows.map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('')
        : '<tr><td colspan="' + head.length + '" class="muted">暂无数据</td></tr>') +
      '</tbody>';
  }

  function renderDaily(s) {
    document.getElementById('tk-requests').textContent = fmtNum(s.totals.requests);
    document.getElementById('tk-input').textContent = fmtNum(s.totals.input_tokens);
    document.getElementById('tk-input-sub').textContent =
      `命中 ${fmtNum(s.totals.cached_input_tokens)} / 未命中 ${fmtNum(s.totals.uncached_input_tokens)}`;
    document.getElementById('tk-output').textContent = fmtNum(s.totals.output_tokens);
    document.getElementById('tk-hitrate').textContent = pct(s.hit_rate);

    // purpose table（Phase 1.1：补未命中列；agent_loop 命中率是优化核心视图）
    const rows = Object.entries(s.by_purpose).map(([name, b]) => [
      name, fmtNum(b.requests), fmtNum(b.input_tokens),
      fmtNum(b.cached_input_tokens),
      fmtNum(b.uncached_input_tokens !== undefined ? b.uncached_input_tokens
        : (b.input_tokens || 0) - (b.cached_input_tokens || 0)),
      fmtNum(b.output_tokens),
      pct(b.input_tokens ? b.cached_input_tokens / b.input_tokens : null),
    ]).sort((a, b) => b[2].length - a[2].length);
    table(document.getElementById('tbl-purpose'),
      ['用途', '请求', '输入', '命中', '未命中', '输出', '命中率'], rows);

    if (!hasCharts || !Object.keys(s.daily).length) return;
    const dates = Object.keys(s.daily);
    const get = (k) => dates.map(d => s.daily[d][k] || 0);
    // Phase 1.3：每日命中率折线（右轴 %）
    const hitRate = dates.map(d => {
      const b = s.daily[d] || {};
      return b.input_tokens ? +(100 * (b.cached_input_tokens || 0) / b.input_tokens).toFixed(1) : null;
    });
    dailyChart = dailyChart || echarts.init(document.getElementById('chart-daily'));
    dailyChart.setOption({
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis' },
      legend: { textStyle: { color: '#8b98b8' } },
      grid: { left: 60, right: 55, top: 40, bottom: 30 },
      xAxis: { type: 'category', data: dates, axisLabel: { color: '#8b98b8' } },
      yAxis: [
        { type: 'value', axisLabel: { color: '#8b98b8' } },
        { type: 'value', min: 0, max: 100, axisLabel: { color: '#8b98b8', formatter: '{value}%' }, splitLine: { show: false } },
      ],
      series: [
        { name: '命中输入', type: 'bar', stack: 'in', data: get('cached_input_tokens'), itemStyle: { color: '#3ecf8e' } },
        { name: '未命中输入', type: 'bar', stack: 'in', data: get('uncached_input_tokens'), itemStyle: { color: '#5b8cff' } },
        { name: '输出', type: 'line', data: get('output_tokens'), itemStyle: { color: '#f0b429' } },
        { name: '命中率', type: 'line', yAxisIndex: 1, data: hitRate, symbol: 'circle', symbolSize: 5,
          itemStyle: { color: '#e86bd0' }, lineStyle: { type: 'dashed' },
          tooltip: { valueFormatter: v => v === null ? '—' : v + '%' } },
      ],
    }, true);
  }

  function renderModel(s) {
    // Phase 1.2：按模型命中率表（含提供商标注，缓存按提供商隔离不可混算）
    const models = s.models || [];
    table(document.getElementById('tbl-model'),
      ['模型', '提供商', '请求', '输入', '命中', '未命中', '输出', '命中率'],
      models.map(m => [
        m.model, m.provider, fmtNum(m.requests), fmtNum(m.input_tokens),
        fmtNum(m.cached_input_tokens), fmtNum(m.uncached_input_tokens),
        fmtNum(m.output_tokens), pct(m.hit_rate),
      ]));
    if (!hasCharts) return;
    const entries = Object.entries(s.by_model);
    if (!entries.length) return;
    modelChart = modelChart || echarts.init(document.getElementById('chart-model'));
    modelChart.setOption({
      backgroundColor: 'transparent',
      tooltip: { trigger: 'item', formatter: p => `${p.name}<br/>总 token: ${fmtNum(p.value)}` },
      series: [{
        type: 'pie', radius: ['38%', '68%'], center: ['50%', '52%'],
        label: { color: '#8b98b8' },
        data: entries.map(([name, b]) => ({
          name, value: (b.input_tokens || 0) + (b.output_tokens || 0),
        })),
      }],
    }, true);
  }

  function renderUsers(list) {
    table(document.getElementById('tbl-users'),
      ['用户', '请求数', '输入', '其中命中', '输出', '合计'],
      list.map(u => [
        u.user_id, fmtNum(u.requests), fmtNum(u.input_tokens),
        fmtNum(u.cached_input_tokens), fmtNum(u.output_tokens),
        fmtNum(u.input_tokens + u.output_tokens),
      ]));
  }

  async function load() {
    const days = document.getElementById('tok-days').value;
    try {
      const [s, users] = await Promise.all([
        api(`/api/tokens/summary?days=${days}`),
        api(`/api/tokens/users?days=${days}&limit=20`),
      ]);
      renderDaily(s);
      renderModel(s);
      renderUsers(users);
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  document.getElementById('tok-days').addEventListener('change', load);
  window.addEventListener('resize', () => {
    dailyChart && dailyChart.resize();
    modelChart && modelChart.resize();
  });
  load();
})();
