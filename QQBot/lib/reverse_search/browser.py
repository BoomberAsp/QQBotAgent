"""无头 Chrome 裸 CDP 驱动（找图反查二期，Lens 专用）。

适配自已验证的 spike（服务器 Chrome 157 + 1081 代理 3/3 零验证码）。
不依赖 playwright：asyncio subprocess + aiohttp WebSocket 直连 DevTools。
注意 lib/browser_fetch.py 的 ruyiPage 是 Firefox 指纹，Lens 反检测参数
是 Chrome 的，两者不可混用；这里只沿用其 Semaphore 懒加载模式。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from loguru import logger

_CHROME_PATHS = (
    "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome",
    "/opt/google/chrome/chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium",
)

# 进程内缓存 exe → UA（Chrome 自动更新后重启进程即重新推导）
_UA_CACHE: dict[str, str] = {}

# 记住哪组 headless/sandbox 旗标在这台机器上能起来，下次优先
_WORKING_FLAGS: tuple[str, str] | None = None

_SEM: asyncio.Semaphore | None = None


def sem() -> asyncio.Semaphore:
    """Lens 并发闸：同一时刻只跑一个浏览器（~400MB/实例）。"""
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(1)
    return _SEM


class BrowserError(RuntimeError):
    """浏览器级失败（启动/连接/CDP 调用）。"""


def find_chrome() -> str:
    """CHROME_BIN 热读覆盖 → 常见路径 → PATH。找不到返回空串。"""
    env = os.environ.get("CHROME_BIN", "").strip()
    if env:
        return env
    for p in _CHROME_PATHS:
        if os.path.isfile(p):
            return p
    for name in ("google-chrome-stable", "google-chrome", "chromium-browser", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def chrome_ua(exe: str) -> str:
    """UA 与本机 Chrome 大版本对齐（反检测关键项之一）。"""
    if exe in _UA_CACHE:
        return _UA_CACHE[exe]
    try:
        ver = subprocess.run([exe, "--version"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except Exception:
        ver = ""
    m = re.search(r"(\d+)\.", ver)
    major = m.group(1) if m else "141"
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
    _UA_CACHE[exe] = ua
    return ua


def is_google_url(url: str) -> bool:
    try:
        host = (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return True
    if not host:
        return True
    return host == "google.com" or host.endswith(".google.com") or ".google." in host


_SOCIAL_HINT_RE = re.compile(
    r"^https?://(?:www\.|m\.)?(?:x|twitter|instagram|weibo|bilibili|tiktok|facebook|"
    r"threads|xiaohongshu|pixiv)\.(?:com|net)/", re.I)
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>\\]+")


def extract_external_url(html: str) -> str:
    """从谷歌中转页（200 + JS 跳转）的 HTML 里捞一个最可能的外部地址。"""
    text = str(html or "").replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    candidates: list[str] = []
    for match in _URL_IN_TEXT_RE.finditer(text):
        url = match.group(0).rstrip(".,;\"')")
        host = (urlsplit(url).hostname or "").lower()
        if not host or is_google_url(url) or "gstatic.com" in host or "googleusercontent.com" in host:
            continue
        candidates.append(url)
        if len(candidates) >= 60:
            break
    for url in candidates:
        if _SOCIAL_HINT_RE.search(url):
            return url
    return candidates[0] if candidates else ""


class CdpBrowser:
    """单个无头 Chrome 实例（一个 page target，一次只跑一个任务）。"""

    def __init__(self, profile: Path, *, exe: str, proxy: str, ua: str):
        self.profile = Path(profile)
        self.exe = exe
        self.proxy = proxy
        self.ua = ua
        self.proc = None
        self.session: aiohttp.ClientSession | None = None
        self.ws = None
        self.pump = None
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 0
        self._listeners: list = []

    # ── 生命周期 ──────────────────────────────────────────────────────

    def _extra_args(self) -> list[str]:
        return [
            "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--disable-background-networking",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=Translate,MediaRouter",
            "--lang=zh-CN", f"--user-agent={self.ua}",
        ]

    async def start(self):
        global _WORKING_FLAGS
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket",
                     "DevToolsActivePort", "lockfile"):
            try:
                (self.profile / name).unlink()
            except OSError:
                pass
        combos = [(h, s) for h in ("--headless=new", "--headless")
                  for s in ("", "--no-sandbox")]
        if _WORKING_FLAGS is not None:
            combos.sort(key=lambda c: 0 if c == _WORKING_FLAGS else 1)
        last = None
        for headless, sandbox in combos:
            args = [a for a in (headless, sandbox) if a] + self._extra_args() + [
                "--remote-debugging-port=0", f"--user-data-dir={self.profile}",
                f"--proxy-server={self.proxy}", "about:blank"]
            try:
                await self._launch(args)
                _WORKING_FLAGS = (headless, sandbox)
                logger.info(f"[Lens] chrome 启动成功 headless={headless or 'headed'} "
                            f"sandbox={sandbox or 'default'}")
                return
            except Exception as e:
                last = e
                await self.kill()
                await asyncio.sleep(0.3)
        raise BrowserError(f"chrome 起不来: {last}")

    async def _launch(self, args: list[str]):
        self.proc = await asyncio.create_subprocess_exec(
            self.exe, *args, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        port_file = self.profile / "DevToolsActivePort"
        deadline = time.monotonic() + 15
        port = None
        while time.monotonic() < deadline:
            try:
                text = port_file.read_text().strip()
                if text:
                    port = int(text.splitlines()[0])
                    break
            except (OSError, ValueError):
                pass
            if self.proc.returncode is not None:
                raise BrowserError(f"chrome 退出 code={self.proc.returncode}")
            await asyncio.sleep(0.15)
        if port is None:
            raise BrowserError("等不到 DevToolsActivePort")
        self.session = aiohttp.ClientSession(trust_env=False)
        target = None
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not target:
            try:
                async with self.session.get(
                        f"http://127.0.0.1:{port}/json/list",
                        timeout=aiohttp.ClientTimeout(total=3)) as r:
                    for t in await r.json():
                        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                            target = t["webSocketDebuggerUrl"]
                            break
            except Exception:
                pass
            if not target:
                await asyncio.sleep(0.2)
        if not target:
            raise BrowserError("无 page target")
        self.ws = await self.session.ws_connect(
            target, heartbeat=30, max_msg_size=64 * 1024 * 1024)
        self.pump = asyncio.create_task(self._pump())
        for m in ("Page.enable", "Runtime.enable", "Network.enable", "DOM.enable"):
            await self.send(m, timeout=10)

    async def _pump(self):
        try:
            async for msg in self.ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                p = json.loads(msg.data)
                mid = p.get("id")
                if mid is not None:
                    fut = self.pending.pop(mid, None)
                    if fut and not fut.done():
                        if p.get("error"):
                            fut.set_exception(BrowserError(str(p["error"].get("message"))))
                        else:
                            fut.set_result(p.get("result") or {})
                elif p.get("method"):
                    for listener in list(self._listeners):
                        try:
                            listener(p)
                        except Exception:
                            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(BrowserError("CDP 断开"))
            self.pending.clear()

    async def kill(self):
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        if self.pump:
            self.pump.cancel()
            try:
                await self.pump
            except (asyncio.CancelledError, Exception):
                pass
            self.pump = None
        if self.session:
            try:
                await self.session.close()
            except Exception:
                pass
            self.session = None
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    self.proc.kill()
                await self.proc.wait()
        self.proc = None

    # ── CDP 调用 ──────────────────────────────────────────────────────

    def add_listener(self, listener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    async def send(self, method: str, params: dict | None = None, timeout: float = 25):
        if self.ws is None or self.ws.closed:
            raise BrowserError("CDP 未连接")
        self.next_id += 1
        mid = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        try:
            await self.ws.send_str(json.dumps(
                {"id": mid, "method": method, "params": params or {}}))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            # 必须转 BrowserError：navigate/wait_for 的轮询只捕 BrowserError，
            # 裸 TimeoutError 会穿透 _search_once 被误标成「Lens 总超时」，
            # 一次慢探针（如上传后跨域导航期间 evaluate 挂起）就杀掉整次搜索
            raise BrowserError(f"CDP 调用超时: {method} ({timeout:.0f}s)") from None
        finally:
            self.pending.pop(mid, None)

    async def send_quiet(self, method: str, params: dict | None = None) -> None:
        try:
            await self.send(method, params, timeout=10)
        except Exception:
            pass

    async def evaluate(self, expr: str, timeout: float = 15, await_promise: bool = False):
        r = await self.send("Runtime.evaluate",
                            {"expression": expr, "returnByValue": True,
                             "awaitPromise": await_promise}, timeout=timeout)
        if r.get("exceptionDetails"):
            raise BrowserError(str(r["exceptionDetails"])[:200])
        return (r.get("result") or {}).get("value")

    async def evaluate_object(self, expr: str, timeout: float = 10) -> str:
        r = await self.send("Runtime.evaluate",
                            {"expression": expr, "returnByValue": False}, timeout=timeout)
        return str((r.get("result") or {}).get("objectId") or "")

    async def navigate(self, url: str, timeout: float = 30, settle: float = 1.0) -> str:
        try:
            nav = await self.send("Page.navigate", {"url": url}, timeout=timeout)
        except BrowserError:
            nav = {}
        if nav.get("errorText"):
            raise BrowserError(f"导航失败: {nav['errorText']}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if await self.evaluate('document.readyState === "complete"', timeout=3):
                    break
            except BrowserError:
                pass
            await asyncio.sleep(0.4)
        await asyncio.sleep(settle)
        return await self.url()

    async def wait_dom(self, accepts: Callable[[str], bool], timeout: float) -> str:
        """Wait for a usable HTML document on an allowed URL."""
        deadline = time.monotonic() + timeout
        last_error = None
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                state = await self.evaluate(
                    "({url:location.href, ready:document.readyState, body:!!document.body})",
                    timeout=min(.8, remaining))
                url = state.get("url", "") if isinstance(state, dict) else ""
                if url and accepts(url) and state.get("body") and state.get("ready") in {"interactive", "complete"}:
                    logger.info("[Lens] homepage_dom outcome=ready state={}", state["ready"])
                    return url
                last_error = None
            except BrowserError as exc:
                last_error = exc
            await asyncio.sleep(min(.15, max(0, deadline - time.monotonic())))
        if last_error:
            raise last_error
        logger.warning("[Lens] homepage_dom outcome=pending")
        return ""

    async def navigate_dom(self, url: str, accepts: Callable[[str], bool], timeout: float) -> str:
        """Navigate the root frame and return once the new document is usable."""
        started = time.monotonic()
        deadline = started + timeout
        commits: set[tuple[str, str]] = set()
        requests: dict[str, str] = {}

        def observe(event: dict) -> None:
            method = event.get("method")
            params = event.get("params") or {}
            if method == "Page.frameNavigated":
                frame = params.get("frame") or {}
                if not frame.get("parentId"):
                    commits.add((frame.get("id", ""), frame.get("loaderId", "")))
                return
            request_id = params.get("requestId")
            if method == "Network.requestWillBeSent" and params.get("type") == "Document" and request_id:
                target = (params.get("request") or {}).get("url", "")
                if accepts(target):
                    host = urlsplit(target).hostname or "-"
                    requests[request_id] = host
                    logger.info("[Lens] homepage_document id={} host={} outcome=request elapsed={:.2f}s",
                                request_id, host, time.monotonic() - started)
            elif request_id in requests and method == "Network.responseReceived":
                logger.info("[Lens] homepage_document id={} host={} outcome=response status={} elapsed={:.2f}s",
                            request_id, requests[request_id], (params.get("response") or {}).get("status"),
                            time.monotonic() - started)
            elif request_id in requests and method == "Network.loadingFailed":
                logger.warning("[Lens] homepage_document id={} host={} outcome=failed error={} elapsed={:.2f}s",
                               request_id, requests[request_id], params.get("errorText") or "-",
                               time.monotonic() - started)

        self.add_listener(observe)
        try:
            nav = await self.send("Page.navigate", {"url": url}, timeout=max(.01, timeout - .15))
            if nav.get("errorText"):
                raise BrowserError(f"导航失败: {nav['errorText']}")
            frame = nav.get("frameId")
            loader = nav.get("loaderId")
            while frame and loader and (remaining := deadline - time.monotonic()) > 0:
                if (frame, loader) in commits:
                    return await self.wait_dom(accepts, remaining)
                await asyncio.sleep(min(.08, remaining))
            logger.warning("[Lens] homepage_dom outcome=no_main_document elapsed={:.2f}s",
                           time.monotonic() - started)
            return ""
        finally:
            self.remove_listener(observe)

    async def wait_for(self, expr: str, timeout: float = 25, settle: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if await self.evaluate(
                        f"(() => {{ try {{ return Boolean(({expr})); }} "
                        f"catch (e) {{ return false; }} }})()", timeout=3):
                    await asyncio.sleep(settle)
                    return True
            except BrowserError:
                pass
            await asyncio.sleep(0.4)
        return False

    async def url(self) -> str:
        try:
            return str(await self.evaluate("location.href", timeout=8) or "")
        except BrowserError:
            return ""

    async def screenshot(self, path: Path) -> None:
        r = await self.send("Page.captureScreenshot", {"format": "png"}, timeout=20)
        path.write_bytes(base64.b64decode(r["data"]))

    # ── /goto 解链（CDP 兜底） ────────────────────────────────────────

    async def resolve_redirect(self, url: str, timeout: float = 12.0) -> str:
        """解出 URL 最终要去的外部地址（Lens `/goto?url=<token>` 的 200+JS 跳转形态）。

        真 302 靠 Fetch.requestPaused 拦截：出现非谷歌请求就记下并中止加载；
        拦截不到则轮询页面自身跳去了哪，最后从 HTML 里捞地址。
        绝不真的把目标页拉下来。
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def on_event(payload: dict) -> None:
            if payload.get("method") != "Fetch.requestPaused":
                return
            params = payload.get("params") or {}
            request_id = params.get("requestId")
            request_url = str((params.get("request") or {}).get("url") or "")
            if not request_id:
                return
            if not request_url or is_google_url(request_url):
                asyncio.ensure_future(self.send_quiet(
                    "Fetch.continueRequest", {"requestId": request_id}))
                return
            if not future.done():
                future.set_result(request_url)
            asyncio.ensure_future(self.send_quiet(
                "Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"}))

        try:
            await self.send("Fetch.enable",
                            {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
                            timeout=10)
        except BrowserError:
            return ""
        self.add_listener(on_event)
        try:
            try:
                # 拦截会让 navigate 迟迟不返回，这里不下等；下面的轮询接管
                await self.send("Page.navigate", {"url": url}, timeout=3.0)
            except BrowserError:
                pass
            deadline = loop.time() + timeout
            current = ""
            while loop.time() < deadline:
                if future.done():
                    return str(future.result() or "")
                current = await self.url()
                if current and re.match(r"^https?://", current, re.I) and not is_google_url(current):
                    return current
                await asyncio.sleep(0.4)
            if not current or is_google_url(current):
                try:
                    html = str(await self.evaluate(
                        "document.documentElement ? document.documentElement.outerHTML : ''",
                        timeout=6) or "")
                except BrowserError:
                    html = ""
                return extract_external_url(html)
            return ""
        finally:
            self.remove_listener(on_event)
            await self.send_quiet("Page.stopLoading")
            await self.send_quiet("Fetch.disable")