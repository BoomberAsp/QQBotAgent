"""Google Lens 反查引擎（找图反查二期）。

无头 Chrome 上传 → DOM 抽卡片 → /goto 解链 → Candidate。
spike 已验证（2026-11-16，服务器 Chrome 157 + 1081 代理 3/3 零验证码）。
Lens 无相似度分（similarity=0），共识端对「Lens 作者主页单独命中」另开采纳条款。

热读开关：FIND_SOURCE_LENS（默认 on）、FIND_SOURCE_LENS_PROXY
（默认 http://127.0.0.1:1081，Lens 必须走代理）、CHROME_BIN、
FIND_SOURCE_LENS_COOKIE（可选，Cookie-Editor 导出的真人谷歌 cookie JSON
文件路径；注入后谷歌把会话当已验真人，对付自动化会话 ban）。
连续 3 次被拦截（显式验证码 / 静默限制）→ 内存熔断 10 分钟
（不持久化，重启/换代理即清零）。

静默限制形态（2026-11-16 实测）：上传被接受（发 vsrid）但
lens.usercontent.google.com/image?vsrid=… 被选择性挂起（垃圾 token 秒回、
真 token 超时），结果页只剩空壳 + 语言提示。EXTRACT 的 lensImgDead
标记识别该形态，分级 IMG_DEAD → GOOGLE_UNREACHABLE 并计入熔断。

阶段日志（loguru，[Lens] 前缀，随 turn.py 文件 sink 落 logs/bot_*.log）：
开始（bytes/proxy/chrome/UA）→ 出口IP → cookie 注入 ok/fail+名单（只记名
不记值）→ 首页/consent 落地 → 上传后 ready/results+URL(含 vsrid) →
verdict+直链/卡片数+lens 图死亡计数 → 非 SUCCESS 附 bodyHead → 逐卡解链
→ 完成/失败 code。诊断「cookie 伪装后仍被拦」按此链查。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from loguru import logger

from .accounts import handle_of, handles_in_text, is_post_page, platform_of
from .browser import (BrowserError, CdpBrowser, chrome_ua, find_chrome,
                      is_google_url, sem)
from .engines import Candidate, EngineError

LENS_TIMEOUT = 60.0
QUEUE_TIMEOUT = 20.0          # 排队超 20s 快速失败，别挤爆 60s 回执
CAPTCHA_STREAK_LIMIT = 3
CAPTCHA_BREAKER_S = 600.0
_DEFAULT_PROXY = "http://127.0.0.1:1081"
_MAX_CANDIDATES = 24
_RESOLVE_CARDS = 3            # 每解一条要一次请求/导航，QQ 里没人愿意等

_captcha_streak = 0
_breaker_until = 0.0          # time.monotonic 基准


def lens_enabled() -> bool:
    return os.environ.get("FIND_SOURCE_LENS", "on").strip().lower() not in (
        "off", "0", "false", "no")


def _proxy() -> str:
    return os.environ.get("FIND_SOURCE_LENS_PROXY", "").strip() or _DEFAULT_PROXY


async def _exit_ip(proxy: str) -> str:
    """尽力探测代理出口 IP（谷歌静默限制与出口信誉强相关，诊断用）。

    失败返回「未知」，绝不影响主路。
    """
    try:
        async with httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=6.0) as client:
            resp = await client.get("https://api.ipify.org")
            if resp.status_code == 200:
                return resp.text.strip()[:64]
            return f"HTTP{resp.status_code}"
    except Exception:  # noqa: BLE001 — 探测失败不阻塞搜索
        return "未知"


def user_cookies() -> list[dict]:
    """FIND_SOURCE_LENS_COOKIE 指向 Cookie-Editor 导出的 JSON 数组。

    只收 google.com 域；缺失/损坏/非数组静默回退 consent cookie。
    每次搜索热读（配额低，文件小），凭证不落日志。
    """
    path = os.environ.get("FIND_SOURCE_LENS_COOKIE", "").strip()
    if not path:
        return []
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"[Lens] cookie 文件不可读，回退 consent 打底：{path}（{e.__class__.__name__}）")
        return []
    if not isinstance(raw, list):
        logger.warning(f"[Lens] cookie 文件不是 JSON 数组，回退 consent 打底：{path}")
        return []
    out: list[dict] = []
    for ck in raw:
        if not isinstance(ck, dict):
            continue
        name = str(ck.get("name") or "")
        value = ck.get("value")
        domain = str(ck.get("domain") or "").lstrip(".")
        if not name or value is None or not domain:
            continue
        if domain != "google.com" and not domain.endswith(".google.com"):
            continue
        item = {"name": name, "value": str(value), "domain": domain,
                "path": str(ck.get("path") or "/")}
        exp = ck.get("expirationDate")
        if isinstance(exp, (int, float)) and exp > 0:
            if exp <= time.time():
                continue   # 短命刷新 token（如 __Secure-STRP）过期即弃
            item["expires"] = float(exp)
        if ck.get("secure"):
            item["secure"] = True
        if ck.get("httpOnly"):
            item["httpOnly"] = True
        out.append(item)
    return out


def _cookie_header() -> str:
    """/goto 解链 httpx 用的 Cookie 头：consent 打底 + 真人 cookie 覆盖。"""
    pairs = {ck["name"]: ck["value"] for ck in CONSENT_COOKIES}
    for ck in user_cookies():
        pairs[ck["name"]] = ck["value"]
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


# ── 页面脚本（spike 已验证；EXTRACT 增加 aiOverview 截取） ────────────

CONSENT_COOKIES = (
    {"name": "SOCS", "value": "CAI", "url": "https://www.google.com/"},
    {"name": "SOCS", "value": "CAI", "url": "https://lens.google.com/"},
    {"name": "CONSENT", "value": "YES+cb.20210328-17-p0.en+FX+000",
     "url": "https://www.google.com/"},
)

CAPTCHA_RE = re.compile(r"unusual traffic|our systems have detected|异常流量|不是自动程序", re.I)
CONSENT_RE = re.compile(r"consent\.google\.com", re.I)
SHELL_RE = re.compile(r"enablejs|启用\s*JavaScript", re.I)

PREPARE_JS = r"""
(function () {
  if (document.getElementById('fishLensForm')) return 'exists';
  var form = document.createElement('form');
  form.id = 'fishLensForm'; form.method = 'POST'; form.enctype = 'multipart/form-data';
  form.action = 'https://lens.google.com/v3/upload?hl=zh-CN&gl=US';
  form.target = '_self'; form.style.display = 'none';
  var picker = document.createElement('input');
  picker.type = 'file'; picker.name = 'encoded_image'; picker.id = 'fishLensFile';
  form.appendChild(picker);
  var marker = document.createElement('input');
  marker.type = 'hidden'; marker.name = 'image_content'; marker.value = '';
  form.appendChild(marker);
  document.body.appendChild(form);
  return 'ok';
})()
"""
SUBMIT_JS = "document.getElementById('fishLensForm').submit(); 'submitted'"
READY_JS = ('document.body && document.body.innerText.length > 200 && '
            'document.querySelectorAll("a[href]").length > 10')
RESULTS_JS = r"""
(() => {
  const isGoogle = (h) => /(^|\.)google\.[a-z.]+$/i.test(h) || /\.google$/i.test(h) ||
    /(^|\.)(googleadservices|doubleclick|googlesyndication|adtrafficquality|google-analytics|googletagmanager|withgoogle|googleapis|gstatic|googleusercontent)\.(com|net)$/i.test(h);
  let n = 0;
  for (const a of document.querySelectorAll('a[href]')) {
    try {
      const u = new URL(a.href);
      if (!/^https?:$/.test(u.protocol) || isGoogle(u.hostname)) continue;
      if ((a.textContent || '').trim().length < 6) continue;
      n += 1;
    } catch (e) {}
  }
  const cards = document.querySelectorAll('a[href*="/goto?url="]').length;
  return n >= 3 || cards >= 2;
})()
"""
ACCEPT_CONSENT_JS = r"""
(() => {
  const nodes = Array.from(document.querySelectorAll('button, [role=button], a'));
  const hit = nodes.find((n) => /(Accept all|全部接受|接受全部|同意全部|I agree|我同意|同意)/i.test((n.textContent || '').trim()));
  if (!hit) return false;
  hit.click();
  return true;
})()
"""
EXTRACT_JS = r"""
(() => {
  const isGoogle = (host) => /(^|\.)google\.[a-z.]+$/i.test(host) || /\.google$/i.test(host);
  const isGoogleOwned = (host) => isGoogle(host) ||
    /(^|\.)(googleadservices|doubleclick|googlesyndication|adtrafficquality|google-analytics|googletagmanager|withgoogle|googleapis|gstatic|googleusercontent)\.(com|net)$/i.test(host);
  const cleanTitle = (node) => {
    const heading = node.querySelector('h1,h2,h3,[role=heading]');
    return String((heading && heading.textContent) || node.getAttribute('aria-label') || node.textContent || '')
      .replace(/\s+/g, ' ').trim().slice(0, 200);
  };
  const rows = [], cards = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    if (a.closest('header, footer, nav, [role=navigation]')) continue;
    let u; try { u = new URL(a.getAttribute('href') || '', location.href); } catch (e) { continue; }
    if (u.protocol !== 'https:') continue;
    const title = cleanTitle(a);
    if (!title) continue;
    if (isGoogle(u.hostname) && u.pathname === '/goto' && u.searchParams.get('url')) {
      if (cards.length < 24) cards.push({ wrapper: u.href, title });
      continue;
    }
    if (isGoogleOwned(u.hostname) || seen.has(u.href)) continue;
    seen.add(u.href);
    rows.push({ url: u.href, title });
  }
  const bodyText = document.body ? document.body.innerText.slice(0, 4000) : '';
  const lensImgs = Array.from(document.images).filter(i => /lens\.usercontent\.google\.com\/image/.test(i.src || ''));
  const lensImgDeadCount = lensImgs.filter(i => i.naturalWidth === 0).length;
  const lensImgDead = lensImgs.length > 0 && lensImgDeadCount === lensImgs.length;
  let aiOverview = '';
  const aiStart = bodyText.search(/AI 概览|AI Overview/i);
  if (aiStart >= 0) {
    const tail = bodyText.slice(aiStart);
    let end = tail.length;
    for (const rx of [/如果你想/, /AI 的回答未必正确无误/, /AI responses may be incorrect/i]) {
      const i = tail.search(rx);
      if (i > 0 && i < end) end = i;
    }
    for (const item of cards.concat(rows)) {
      const t = item.title || '';
      if (t.length >= 8) { const i = tail.indexOf(t, 12); if (i > 0 && i < end) end = i; }
    }
    aiOverview = tail.slice(0, end).replace(/\s+/g, ' ').trim().slice(0, 500);
  }
  return JSON.stringify({
    url: location.href,
    results: rows, cards: cards,
    captcha: /unusual traffic|our systems have detected|异常流量/i.test(bodyText),
    empty: /No matches|No results|找不到相符|没有找到|未找到/i.test(bodyText),
    bodyHead: bodyText.slice(0, 600),
    aiOverview: aiOverview,
    lensImgDead: lensImgDead,
    lensImgTotal: lensImgs.length,
    lensImgDeadCount: lensImgDeadCount
  });
})()
"""

_AI_START_RE = re.compile(r"AI 概览|AI Overview", re.I)
_AI_STOP_RE = re.compile(r"如果你想|AI 的回答未必正确无误|AI responses may be incorrect", re.I)

LENS_SOCIAL_RE = re.compile(
    r"^(x|twitter|推特|instagram|ig|ins|微博|weibo|bilibili|哔哩|tiktok|抖音|facebook|fb|"
    r"threads|xiaohongshu|小红书)", re.I)
LENS_SOCIAL_HOST_RE = re.compile(
    r"^https?://(?:www\.|m\.)?(?:x|twitter|instagram|weibo|bilibili|tiktok|facebook|threads|"
    r"xiaohongshu)\.(?:com|net)/", re.I)
_TITLE_NAME_SEP_RE = re.compile(
    r"\s*[/|·•—-]\s*(?:X|Twitter|Instagram|微博|Weibo|bilibili|哔哩哔哩|TikTok|抖音|"
    r"Facebook|小红书).*$", re.I)


def lens_has_social(rows: list[dict]) -> bool:
    for row in rows or []:
        if LENS_SOCIAL_RE.search(str(row.get("title") or "")):
            return True
        if LENS_SOCIAL_HOST_RE.search(str(row.get("url") or "")):
            return True
    return False


def social_name_from_title(title: str) -> str:
    """卡片标题里的作者名（best-effort：@handle 或平台后缀前的名字）。"""
    t = (title or "").strip()
    handles = handles_in_text(t)
    if handles:
        return handles[0]
    stripped = _TITLE_NAME_SEP_RE.sub("", t).strip()
    return stripped[:60] if stripped and stripped != t else ""


def ai_overview_from_body(body: str) -> str:
    """JS 没截到时的 Python 兜底。"""
    m = _AI_START_RE.search(body or "")
    if not m:
        return ""
    tail = body[m.start():]
    stop = _AI_STOP_RE.search(tail)
    if stop and stop.start() > 0:
        tail = tail[:stop.start()]
    return re.sub(r"\s+", " ", tail).strip()[:500]


# ── URL 安全（纯语法，不做 DNS） ──────────────────────────────────────

def recordable_url(url: str) -> bool:
    """解链目标只记录、不抓取内容，故只做语法级私网拦截。

    不调 net.check_url_allowed：其阻塞 getaddrinfo 对境外社交域又慢又易误杀。
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(
            (".local", ".localhost", ".internal", ".lan", ".home.arpa")):
        return False
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def resolve_goto_http(client: httpx.AsyncClient, wrapper_url: str,
                            timeout: float = 8.0) -> str:
    """不跟重定向、读 Location 解 `/goto?url=<token>`（真 302 形态，~1s）。

    中间跳必须是 Google 域；最终目标过 recordable_url。无 Location 返回 ""
    （交给 CDP 兜底）。
    """
    current = str(wrapper_url or "")
    for _ in range(3):
        if not re.match(r"^https?://", current, re.I) or not is_google_url(current):
            return ""
        try:
            resp = await client.get(current, timeout=timeout, follow_redirects=False)
        except httpx.HTTPError:
            return ""
        location = resp.headers.get("location", "")
        if not location:
            return ""
        target = urljoin(current, location).split("#")[0]
        if is_google_url(target):
            current = target
            continue
        return target if recordable_url(target) else ""
    return ""


# ── 结果分级与映射 ────────────────────────────────────────────────────

def grade_page(page: dict, final_url: str) -> str:
    """EXTRACT payload → SUCCESS / CONSENT / CAPTCHA / EMPTY / IMG_DEAD / JS_SHELL / UNKNOWN。"""
    body = str(page.get("bodyHead") or "")
    if CONSENT_RE.search(final_url or ""):
        return "CONSENT"
    if page.get("captcha") or CAPTCHA_RE.search(body) or "/sorry/" in (final_url or ""):
        return "CAPTCHA"
    if page.get("results") or page.get("cards"):
        return "SUCCESS"
    if page.get("empty"):
        return "EMPTY"
    if page.get("lensImgDead"):
        return "IMG_DEAD"   # 上传被接受但图片服务被选择性挂起（静默限制）
    if SHELL_RE.search(body):
        return "JS_SHELL"
    return "UNKNOWN"


def _note_verdict(verdict: str) -> None:
    """熔断计数：CAPTCHA/IMG_DEAD 累计，SUCCESS/EMPTY 清零，其余不动。"""
    global _captcha_streak, _breaker_until
    if verdict in ("CAPTCHA", "IMG_DEAD"):
        _captcha_streak += 1
        logger.info(f"[Lens] 拦截计数 streak={_captcha_streak}/{CAPTCHA_STREAK_LIMIT}（{verdict}）")
        if _captcha_streak >= CAPTCHA_STREAK_LIMIT:
            _breaker_until = time.monotonic() + CAPTCHA_BREAKER_S
            logger.warning(f"[Lens] 连续 {CAPTCHA_STREAK_LIMIT} 次被拦截，熔断 {CAPTCHA_BREAKER_S:.0f}s")
    elif verdict in ("SUCCESS", "EMPTY"):
        _captcha_streak = 0


def rows_to_candidates(rows: list[dict]) -> list[Candidate]:
    """解链后的行 → Candidate。author_url 只在「平台+handle+非帖子页」时设置，
    这是共识端 Lens 单独命中采纳条款的安全前提。"""
    out: list[Candidate] = []
    seen: set[str] = set()
    for row in rows:
        url = str(row.get("url") or "")
        if not url or url in seen or not recordable_url(url):
            continue
        seen.add(url)
        title = str(row.get("title") or "")[:200]
        plat = platform_of(url)
        author = social_name_from_title(title) if plat else ""
        author_url = url if (plat and handle_of(url) and not is_post_page(url)) else ""
        out.append(Candidate(
            engine="Lens", page_url=url, title=title, author=author,
            author_url=author_url, similarity=0.0,
            extra={"wrapper": str(row.get("wrapper") or "")}))
        if len(out) >= _MAX_CANDIDATES:
            break
    return out


def _classify_browser_error(exc: Exception) -> EngineError:
    msg = str(exc)
    if "net::" in msg or "ERR_" in msg:
        return EngineError("GOOGLE_UNREACHABLE", "谷歌连不上（检查代理/外网）")
    return EngineError("ENGINE_ERROR", f"Lens 浏览器失败：{msg[:120]}")


# ── 主流程 ────────────────────────────────────────────────────────────

async def _search_once(cdp: CdpBrowser, img_path: Path, ua: str,
                       deadline: float) -> tuple[list[Candidate], str]:
    t0 = time.monotonic()
    await cdp.start()
    # 真人 cookie 在后，覆盖 consent 打底值（SOCS 等）
    real = user_cookies()
    ck_ok = ck_fail = 0
    for ck in (*CONSENT_COOKIES, *real):
        try:
            r = await cdp.send("Network.setCookie", dict(ck), timeout=8)
            if r.get("success", True):
                ck_ok += 1
            else:
                ck_fail += 1
        except BrowserError:
            ck_fail += 1
    logger.info(f"[Lens] cookie 注入 ok={ck_ok} fail={ck_fail} 真人={len(real)} "
                f"名单={','.join(sorted({c['name'] for c in real})) or '-'}")
    home = await cdp.navigate("https://www.google.com/?hl=zh-CN&gl=US",
                              timeout=25, settle=0.8)
    logger.info(f"[Lens] 首页落地 {home}（+{time.monotonic() - t0:.1f}s）")
    if CONSENT_RE.search(home):
        clicked = await cdp.evaluate(ACCEPT_CONSENT_JS)
        await asyncio.sleep(1.5)
        home = await cdp.url()
        logger.info(f"[Lens] consent 页点击={clicked} 重落地 {home}")
        if CONSENT_RE.search(home):
            _note_verdict("CONSENT")
            raise EngineError("GOOGLE_CONSENT", "谷歌同意页拦截，无法上传")

    prepared = await cdp.evaluate(PREPARE_JS, timeout=10)
    if prepared not in ("ok", "exists"):
        raise EngineError("ENGINE_ERROR", f"Lens 上传表单注入失败：{prepared}")
    object_id = ""
    obj_deadline = time.monotonic() + 8
    while time.monotonic() < obj_deadline and not object_id:
        try:
            object_id = await cdp.evaluate_object("document.querySelector('#fishLensFile')")
        except BrowserError:
            pass
        if not object_id:
            await asyncio.sleep(0.3)
    if not object_id:
        raise EngineError("ENGINE_ERROR", "Lens 上传表单注入失败：无 #fishLensFile")
    await cdp.send("DOM.setFileInputFiles",
                   {"files": [str(img_path)], "objectId": object_id}, timeout=15)
    await cdp.evaluate(SUBMIT_JS, timeout=10)
    ready_ok = await cdp.wait_for(READY_JS, timeout=20, settle=0.8)
    results_ok = await cdp.wait_for(RESULTS_JS, timeout=20, settle=2.0)

    final_url = await cdp.url()
    logger.info(f"[Lens] 上传后 ready={ready_ok} results={results_ok} "
                f"url={final_url}（+{time.monotonic() - t0:.1f}s）")
    raw = await cdp.evaluate(EXTRACT_JS, timeout=20)
    page = json.loads(raw) if isinstance(raw, str) else (raw or {})
    if not isinstance(page, dict):
        page = {}
    verdict = grade_page(page, final_url)
    _note_verdict(verdict)
    logger.info(
        f"[Lens] verdict={verdict} 直链={len(page.get('results') or [])} "
        f"卡片={len(page.get('cards') or [])} "
        f"lens图死={page.get('lensImgDeadCount', '?')}/{page.get('lensImgTotal', '?')} "
        f"captcha={bool(page.get('captcha'))} empty={bool(page.get('empty'))} "
        f"aiOverview={len(str(page.get('aiOverview') or ''))}字 "
        f"（+{time.monotonic() - t0:.1f}s）")
    if verdict != "SUCCESS":
        logger.warning(f"[Lens] bodyHead: {str(page.get('bodyHead') or '')[:300]!r}")
    if verdict == "CONSENT":
        raise EngineError("GOOGLE_CONSENT", "谷歌同意页拦截，无法上传")
    if verdict == "CAPTCHA":
        raise EngineError("GOOGLE_CAPTCHA", "谷歌验证码拦截")
    if verdict == "EMPTY":
        raise EngineError("GOOGLE_EMPTY", "Lens 未找到匹配")
    if verdict == "IMG_DEAD":
        raise EngineError("GOOGLE_UNREACHABLE",
                          "Lens 图片服务无响应（代理出口被谷歌静默限制，换节点或稍后再试）")
    if verdict != "SUCCESS":
        raise EngineError("ENGINE_ERROR", f"Lens 结果页异常（{verdict}）")

    overview = str(page.get("aiOverview") or "") or ai_overview_from_body(
        str(page.get("bodyHead") or ""))
    rows = [dict(r) for r in (page.get("results") or []) if r.get("url")]
    # 已有直链含社交链接时不再逐条解 /goto —— 每解一条都要一次请求/导航
    if not lens_has_social(rows):
        seen = {str(r.get("url")) for r in rows}
        cards = [c for c in (page.get("cards") or []) if c.get("wrapper")]
        social = [c for c in cards if LENS_SOCIAL_RE.search(str(c.get("title") or ""))]
        others = [c for c in cards if not LENS_SOCIAL_RE.search(str(c.get("title") or ""))]
        if cards:
            picked = (social + others)[:_RESOLVE_CARDS]
            logger.info(f"[Lens] 解链 {len(picked)} 张 /goto 卡（社交优先={bool(social)}）")
            async with httpx.AsyncClient(
                    proxy=_proxy(), trust_env=False, follow_redirects=False,
                    headers={"User-Agent": ua, "Cookie": _cookie_header()}) as client:
                for i, card in enumerate(picked):
                    remaining = deadline - time.monotonic()
                    if remaining < 4:
                        logger.info("[Lens] 解链预算耗尽，跳过剩余卡片")
                        break
                    target = await resolve_goto_http(
                        client, str(card["wrapper"]), timeout=min(8.0, remaining))
                    via = "http"
                    if not target:
                        via = "cdp"
                        try:
                            target = await cdp.resolve_redirect(
                                str(card["wrapper"]), timeout=min(8.0, remaining))
                        except BrowserError:
                            target = ""
                    logger.info(f"[Lens] goto#{i + 1} {via} → {target or '-'} "
                                f"标题={str(card.get('title') or '')[:40]!r}")
                    if not target or target in seen or not recordable_url(target):
                        continue
                    seen.add(target)
                    rows.append({"url": target, "title": str(card.get("title") or "")[:200],
                                 "wrapper": card["wrapper"]})
    else:
        logger.info("[Lens] 已有社交直链，跳过 /goto 解链")
    candidates = rows_to_candidates(rows)
    logger.info(f"[Lens] 完成 candidates={len(candidates)} overview={len(overview)}字 "
                f"总耗时={time.monotonic() - t0:.1f}s")
    return candidates, overview


async def search_lens(data: bytes, timeout: float = LENS_TIMEOUT,
                      ) -> tuple[list[Candidate], str]:
    """Lens 反查主入口。返回 (候选, AI 概览文本)；失败抛 EngineError。

    每次搜索独立 launch→kill（配额低，不做常驻）；finally 清理 profile
    与临时图片，防 /tmp 泄漏。
    """
    if not lens_enabled():
        return [], ""
    if time.monotonic() < _breaker_until:
        logger.warning(f"[Lens] 熔断中，剩余 {_breaker_until - time.monotonic():.0f}s，本次跳过")
        raise EngineError("GOOGLE_CAPTCHA", "Lens 连续被谷歌拦截，熔断中，请稍后再试")
    exe = find_chrome()
    if not exe:
        raise EngineError("ENGINE_ERROR", "未找到 Chrome（可设 CHROME_BIN）")
    try:
        await asyncio.wait_for(sem().acquire(), QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"[Lens] 排队超时（>{QUEUE_TIMEOUT:.0f}s），放弃")
        raise EngineError("ENGINE_TIMEOUT", "Lens 排队超时") from None

    t_start = time.monotonic()
    proxy = _proxy()
    ua = chrome_ua(exe)
    logger.info(f"[Lens] 开始 bytes={len(data)} proxy={proxy} chrome={exe} ua={ua}")
    logger.info(f"[Lens] 出口IP={await _exit_ip(proxy)}")
    profile = Path(tempfile.mkdtemp(prefix="findlens_"))
    img_path = profile / "upload.image"
    cdp = CdpBrowser(profile, exe=exe, proxy=proxy, ua=ua)
    deadline = time.monotonic() + timeout - 3.0
    try:
        img_path.write_bytes(data)
        return await asyncio.wait_for(
            _search_once(cdp, img_path, ua, deadline), timeout)
    except asyncio.TimeoutError:
        logger.warning(f"[Lens] 总超时 {timeout:.0f}s")
        raise EngineError("ENGINE_TIMEOUT", "Lens 超时") from None
    except EngineError as e:
        logger.warning(f"[Lens] 失败 code={e.code} msg={e.message} "
                       f"耗时={time.monotonic() - t_start:.1f}s")
        raise
    except BrowserError as e:
        err = _classify_browser_error(e)
        logger.warning(f"[Lens] 浏览器失败 code={err.code} msg={err.message} "
                       f"耗时={time.monotonic() - t_start:.1f}s")
        raise err from e
    finally:
        await cdp.kill()
        shutil.rmtree(profile, ignore_errors=True)
        sem().release()