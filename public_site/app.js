/* Roxy 公开站点交互：导航高亮、滚动入场、命令复制、更新日志渲染。
   更新日志数据源 ./changelog.json 由 WebUI 保存时自动导出（脱敏），缺失时优雅降级。 */

(function () {
  'use strict';

  /* ── 导航：滚动毛玻璃 + 当前区块高亮 + 移动端菜单 ── */
  var nav = document.getElementById('nav');
  var navToggle = document.getElementById('nav-toggle');
  var navLinks = Array.prototype.slice.call(document.querySelectorAll('.nav-link'));

  function onScroll() {
    nav.classList.toggle('scrolled', window.scrollY > 24);
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  navToggle.addEventListener('click', function () {
    var open = nav.classList.toggle('open');
    navToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  navLinks.forEach(function (a) {
    a.addEventListener('click', function () {
      nav.classList.remove('open');
      navToggle.setAttribute('aria-expanded', 'false');
    });
  });

  var sections = navLinks
    .map(function (a) { return document.querySelector(a.getAttribute('href')); })
    .filter(Boolean);

  function highlight() {
    var pos = window.scrollY + 120;
    var current = sections[0];
    sections.forEach(function (s) {
      if (s.offsetTop <= pos) current = s;
    });
    navLinks.forEach(function (a) {
      a.classList.toggle('active', a.getAttribute('href') === '#' + current.id);
    });
  }
  window.addEventListener('scroll', highlight, { passive: true });
  highlight();

  /* ── 滚动入场动效 ── */
  var revealEls = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window) {
    var ro = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          e.target.classList.add('in');
          ro.unobserve(e.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
    revealEls.forEach(function (el) { ro.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add('in'); });
  }

  /* ── 命令 chip 点击复制 ── */
  var tip = document.getElementById('copy-tip');
  var tipTimer = null;

  function showTip() {
    if (!tip) return;
    tip.hidden = false;
    // 强制回流以重启动画
    void tip.offsetWidth;
    tip.classList.add('show');
    clearTimeout(tipTimer);
    tipTimer = setTimeout(function () {
      tip.classList.remove('show');
    }, 1400);
  }

  function copyText(text, done) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
    } else {
      fallbackCopy(text);
      done();
    }
  }

  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) { /* 忽略 */ }
    document.body.removeChild(ta);
  }

  document.addEventListener('click', function (ev) {
    if (!ev.target || !ev.target.closest) return;
    var chip = ev.target.closest('[data-copy]');
    if (!chip) return;
    copyText(chip.getAttribute('data-copy'), function () {
      chip.classList.add('copied');
      setTimeout(function () { chip.classList.remove('copied'); }, 900);
      showTip();
    });
  });

  /* ── 演示视频：同一时间只播放一个 ── */
  var videos = Array.prototype.slice.call(document.querySelectorAll('.demo-card video'));
  videos.forEach(function (v) {
    v.addEventListener('play', function () {
      videos.forEach(function (o) { if (o !== v && !o.paused) o.pause(); });
    });
  });

  /* ── 更新日志渲染 ── */
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var KNOWN_TYPES = ['新增', '修复', '优化', '调整', '移除'];

  function renderChangelog(data) {
    var box = document.getElementById('changelog-box');
    var entries = (data && data.entries) || [];
    if (!entries.length) {
      box.innerHTML = '<p class="muted">暂无更新记录。</p>';
      return;
    }
    var html = entries.map(function (e) {
      var items = (e.changes || []).map(function (ch) {
        var type = KNOWN_TYPES.indexOf(ch.type) >= 0 ? ch.type : 'other';
        var label = type === 'other' ? (esc(ch.type) || '记录') : esc(type);
        return '<div class="tl-item"><span class="tl-tag t-' + type + '">' + label +
               '</span><span>' + esc(ch.text) + '</span></div>';
      }).join('');
      return '<div class="tl-entry reveal">' +
               '<div class="tl-head"><span class="tl-version">v' + esc(e.version) +
               '</span><span class="tl-date">' + esc(e.date) + '</span></div>' +
               '<div class="tl-changes">' + items + '</div>' +
             '</div>';
    }).join('');
    box.innerHTML = html;
    // 动态插入的节点补挂入场观察
    if ('IntersectionObserver' in window) {
      var ro2 = new IntersectionObserver(function (ents) {
        ents.forEach(function (en) {
          if (en.isIntersecting) { en.target.classList.add('in'); ro2.unobserve(en.target); }
        });
      }, { threshold: 0.1 });
      box.querySelectorAll('.reveal').forEach(function (el) { ro2.observe(el); });
    } else {
      box.querySelectorAll('.reveal').forEach(function (el) { el.classList.add('in'); });
    }
  }

  fetch('./changelog.json', { cache: 'no-cache' })
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(renderChangelog)
    .catch(function () {
      var box = document.getElementById('changelog-box');
      box.innerHTML = '<p class="muted">暂无更新记录。机器人发版后会自动同步到本页。</p>';
    });
})();
