"""
NapCat 登录状态 / 二维码探测。

NapCat 需要扫码登录时会把二维码 PNG 写到 cache/qrcode.png（见 config.
NAPCAT_QRCODE_PATH），并自带一个 WebUI（默认 127.0.0.1:6099）暴露登录状态。

NapCat WebUI 的鉴权不是裸 token，而是「签名凭证」：
  1. hash = sha256(token + ".napcat")           # token 来自 webui.json
  2. POST /api/auth/login {hash}                 # → data.Credential (base64)
  3. 后续请求带 Authorization: Bearer <Credential>
凭证有效期 1 小时，这里缓存 ~50 分钟并在失效时自动重登，避免触发登录限速。

所有网络/文件操作都不抛异常，失败时返回 None / 保守状态，让前端降级显示。
"""

import hashlib
import json
import time

import httpx

from . import config, process_manager

# 凭证缓存（进程内）。NapCat 每次重启 secretKey 变化会使旧凭证失效，
# 那时 CheckLoginStatus 返回未授权 → 清空缓存重登。
_cred_cache: dict = {"credential": None, "ts": 0.0}
_CRED_TTL = 50 * 60          # 略小于服务端 1 小时有效期
_HTTP_TIMEOUT = 3.0


def _read_token() -> str | None:
    """从 NapCat webui.json 读取 token；缺失/损坏返回 None。"""
    try:
        raw = config.NAPCAT_WEBUI_CONFIG.read_text(encoding="utf-8")
        return json.loads(raw).get("token") or None
    except (OSError, ValueError):
        return None


def _password_hash(token: str) -> str:
    """NapCat AuthHelper.generatePasswordHash: sha256(token + '.napcat')."""
    return hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()


async def _login(client: httpx.AsyncClient) -> str | None:
    """用 token 换取签名凭证；失败返回 None。成功后写入缓存。"""
    token = _read_token()
    if not token:
        return None
    url = f"{config.NAPCAT_HTTP_BASE}/api/auth/login"
    try:
        r = await client.post(url, json={"hash": _password_hash(token)})
        r.raise_for_status()
        cred = (r.json().get("data") or {}).get("Credential")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    if cred:
        _cred_cache["credential"] = cred
        _cred_cache["ts"] = time.time()
    return cred


async def _credential(client: httpx.AsyncClient, *, force: bool = False) -> str | None:
    """返回未过期的缓存凭证，否则重新登录获取。"""
    cred = _cred_cache["credential"]
    if not force and cred and (time.time() - _cred_cache["ts"]) < _CRED_TTL:
        return cred
    return await _login(client)


async def check_login() -> dict | None:
    """调用 NapCat /QQLogin/CheckLoginStatus。

    返回 {isLogin, isOffline, qrcodeurl, loginError}；API 不可达返回 None。
    """
    url = f"{config.NAPCAT_HTTP_BASE}/api/QQLogin/CheckLoginStatus"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        for attempt in (0, 1):
            cred = await _credential(client, force=(attempt == 1))
            if not cred:
                return None
            try:
                r = await client.post(
                    url, headers={"Authorization": f"Bearer {cred}"})
            except httpx.HTTPError:
                return None
            if r.status_code in (401, 403) and attempt == 0:
                # 凭证失效（多半是 NapCat 重启换了 secretKey）→ 重登一次。
                continue
            try:
                data = r.json().get("data")
            except (ValueError, AttributeError):
                return None
            return data if isinstance(data, dict) else None
    return None


def qrcode_stat() -> dict:
    """二维码文件的磁盘状态。"""
    p = config.NAPCAT_QRCODE_PATH
    try:
        st = p.stat()
        return {"exists": True, "mtime": int(st.st_mtime), "size": st.st_size}
    except OSError:
        return {"exists": False, "mtime": None, "size": None}


async def status() -> dict:
    """综合进程态 + 登录 API + 文件状态，供前端轮询。

    login 取值：
      logged_in — NapCat 报告已登录且在线
      waiting   — 运行中，等待扫码（或已登录但离线）
      unknown   — 进程在跑但 API 不可达，无法判定（前端显示二维码不加遮罩）
    """
    procs = await _safe_status_all()
    nap = procs.get("napcat", {}) if procs else {}
    proc_state = nap.get("state", "unavailable")

    login_state = "unknown"
    login_error = ""
    api = await check_login()
    if api is not None:
        if api.get("isLogin"):
            login_state = "logged_in"
        else:
            login_state = "waiting"
            login_error = api.get("loginError") or ""
    elif proc_state != "running":
        # 进程都没起，API 自然不可达 —— 明确为未运行而非未知。
        login_state = "stopped"

    return {
        "process": proc_state,
        "login": login_state,
        "login_error": login_error,
        "qrcode": qrcode_stat(),
        "qrcode_path": str(config.NAPCAT_QRCODE_PATH),
    }


async def _safe_status_all() -> dict:
    """process_manager.status_all 是同步阻塞调用，放到线程里跑。"""
    import asyncio
    try:
        return await asyncio.to_thread(process_manager.status_all)
    except Exception:
        return {}
