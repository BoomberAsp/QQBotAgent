/* Roxy 公开站点交互：命令 chip 复制、演示图降级、更新记录渲染。
   更新记录数据源 ./changelog.json 由 WebUI 保存时自动导出（脱敏）。 */

(function () {
  'use strict';

  // 与 QQ 卡片 (card_renderer._CHANGELOG_TYPE_COLORS) 一致的类型色
  var TYPE_COLORS = {
    '新增': '#4caf50',
    '修复': '#e8503a',
    '优化': '#3a8ce8',
    '调整': '#e8b53a',
    '移除': '#6e748c'
  };
  var TYPE_DEFAULT = '#6e748c';

  // 版本 pill 循环色（与卡片 _HELP_SECTION_COLORS 前几色呼应）
  var VERSION_COLORS = ['#4caf50', '#3a8ce8', '#7a5ce8', '#e8b53a', '#3ab4c8', '#ff9e4a'];

  // 演示素材清单：把截图/GIF 放进 assets/ 后在此登记即可展示；
  // 文件缺失时自动降级为占位说明，不会报错。
  var DEMOS = [
    { src: './assets/demo-chat.png', caption: '日常问答：直接 @ 它说话' },
    { src: './assets/demo-redeem.png', caption: '查兑换码：/兑换码 逐条单发方便复制' },
    { src: './assets/demo-character.png', caption: '角色详情：附卡片图' }
  ];

  // ── 命令 chip 点击复制 ─────────────────────────────────────
  function showCopyTip() {
    var tip = document.getElementById('copy-tip');
    if (!tip) return;
    tip.hidden = false;
    clearTimeout(showCopyTip._t);
    showCopyTip._t = setTimeout(function () { tip.hidden = true; }, 1500);
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    // 旧浏览器 / 非 HTTPS 兜底
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand('copy');
        resolve();
      } catch (e) {
        reject(e);
      } finally {
        document.body.removeChild(ta);
      }
    });
  }

  document.querySelectorAll('.chip[data-copy]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var text = btn.getAttribute('data-copy') || '';
      copyText(text).then(showCopyTip).catch(function () {
        // 复制失败时至少让用户看到文本可手动选择
        window.prompt('复制该命令：', text);
      });
    });
  });

  // ── 演示画廊（缺失降级）───────────────────────────────────
  function buildDemos() {
    var box = document.getElementById('demo-gallery');
    if (!box) return;
    box.innerHTML = '';
    if (!DEMOS.length) {
      box.innerHTML = '<div class="demo-placeholder">演示截图即将补充，敬请期待。</div>';
      return;
    }
    var loaded = 0;
    DEMOS.forEach(function (d) {
      var fig = document.createElement('figure');
      var img = document.createElement('img');
      img.alt = d.caption;
      img.loading = 'lazy';
      img.addEventListener('load', function () { loaded++; });
      img.addEventListener('error', function () {
        // 单张缺失：整块降级为占位
        fig.outerHTML = '<div class="demo-placeholder">「' + d.caption + '」演示图待补充</div>';
        maybeAllMissing();
      });
      img.src = d.src;
      var cap = document.createElement('figcaption');
      cap.textContent = d.caption;
      fig.appendChild(img);
      fig.appendChild(cap);
      box.appendChild(fig);
    });

    function maybeAllMissing() {
      // 若所有图都加载失败，画廊里只剩占位块，无需额外处理
    }
  }

  // ── 更新记录渲染 ──────────────────────────────────────────
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function renderChangelog(data) {
    var box = document.getElementById('changelog-box');
    if (!box) return;
    var entries = (data && Array.isArray(data.entries)) ? data.entries : [];
    if (!entries.length) {
      box.innerHTML = '<p class="muted">暂无更新记录。</p>';
      return;
    }
    var html = '';
    entries.forEach(function (e, i) {
      var color = VERSION_COLORS[i % VERSION_COLORS.length];
      var head = '<div class="cl-head">' +
        '<span class="cl-version" style="background:' + color + '">' + esc(e.version || '更新') + '</span>' +
        (e.date ? '<span class="cl-date">' + esc(e.date) + '</span>' : '') +
        '</div>';
      var rows = '';
      (e.changes || []).forEach(function (ch) {
        var t = ch.type || '';
        var c = TYPE_COLORS[t] || TYPE_DEFAULT;
        rows += '<div class="cl-change">' +
          (t ? '<span class="cl-type" style="background:' + c + '">' + esc(t) + '</span>' : '') +
          '<span class="cl-text">' + esc(ch.text) + '</span>' +
          '</div>';
      });
      html += '<div class="cl-entry">' + head + rows + '</div>';
    });
    box.innerHTML = html;
  }

  function loadChangelog() {
    var box = document.getElementById('changelog-box');
    fetch('./changelog.json', { cache: 'no-cache' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(renderChangelog)
      .catch(function () {
        if (box) box.innerHTML = '<p class="muted">暂无更新记录。</p>';
      });
  }

  buildDemos();
  loadChangelog();
})();
