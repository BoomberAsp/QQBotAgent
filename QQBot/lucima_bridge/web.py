from __future__ import annotations

import asyncio
import fcntl
import hmac
import json
import os
import re
import sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from .long_tasks import CONFIRMED_OPERATIONS, preview as long_preview
from .service import APPROVALS, Bridge, TASK_NAMES, TICKET_TTL, Vault, Worker


FILES = Path(__file__).resolve().parent
COOKIE = "__Host-lucima"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
CODE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
PIN = re.compile(r"[0-9]{8}\Z")
OPERATION_ID = re.compile(r"[A-Za-z0-9_-]{16}\Z")
# QQ Official message IDs can exceed the limits for member and group IDs.
EVENT_ID_MAX_LENGTH = 512


def public_origin() -> str:
    origin = os.environ["LUCIMA_PUBLIC_ORIGIN"].rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or origin != f"https://{parsed.netloc}":
        raise RuntimeError("LUCIMA_PUBLIC_ORIGIN 必须是一个 HTTPS 源，例如 https://lucima.oneweblog.cn")
    return origin


def load_catalog(root: Path) -> list[tuple[tuple[str, ...], str]]:
    path = root / "assets/encyclopedia/index.json"
    rows = json.loads(path.read_text(encoding="utf-8"))["characters"]
    return [(
        tuple(name.casefold() for name in [row["id"], row["name"], row.get("nameEn", ""), *row.get("aliases", [])]),
        f"{row['name']} · {row['star']}星 · {row['elementName']} · {row['id']}",
    ) for row in rows]


@asynccontextmanager
async def lifespan(app: FastAPI):
    internal_token = os.environ["LUCIMA_INTERNAL_TOKEN"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", internal_token):
        raise RuntimeError("LUCIMA_INTERNAL_TOKEN 必须是 32–128 位的随机 URL 安全字符串")
    root = Path(os.environ["LUCIMA_ROOT"]).resolve(strict=True)
    if not (root / "backend/bot_worker.py").is_file():
        raise RuntimeError("LUCIMA_ROOT 必须指向含 bot_worker.py 的 Lucima 仓库")
    python = Path(os.environ.get("LUCIMA_PYTHON", sys.executable)).expanduser()
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError("LUCIMA_PYTHON 必须是可执行 Python 的绝对路径")
    data_dir = Path(os.environ["LUCIMA_BRIDGE_DATA"]).expanduser()
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if data_dir.stat().st_mode & 0o077:
        raise RuntimeError("LUCIMA_BRIDGE_DATA 需要使用仅服务用户可访问的目录（0700）")
    with (data_dir / "instance.lock").open("a") as instance:
        try:
            fcntl.flock(instance, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("该数据目录已有运行中的 Lucima 服务") from None
        app.state.internal_token = internal_token.encode()
        app.state.catalog = load_catalog(root)
        origin = public_origin()
        vault = Vault(data_dir / "binding.sqlite3", os.environ["LUCIMA_VAULT_KEY"])
        try:
            worker = Worker(root, str(python))
            app.state.bridge = Bridge(vault, worker, origin)
            queue = asyncio.create_task(app.state.bridge.consume())
            try:
                yield
            finally:
                queue.cancel()
                with suppress(asyncio.CancelledError):
                    await queue
        finally:
            vault.db.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def response_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update({
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                                   "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    })
    return response


async def json_body(request: Request) -> dict | None:
    if request.headers.get("content-type", "").partition(";")[0].lower() != "application/json":
        return None
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 8192:
            return None
        data.extend(chunk)
    try:
        body = json.loads(data)
    except (UnicodeError, ValueError, RecursionError):
        return None
    return body if isinstance(body, dict) else None


def answer(message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"message": message}, status_code=status)


def valid(value, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(
        ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value
    )


def browser_id(request: Request) -> str:
    value = request.cookies.get(COOKIE, "")
    return value if TOKEN.fullmatch(value) else ""


@app.get("/")
async def login_page():
    return FileResponse(FILES / "login.html", media_type="text/html")


@app.get("/login.js")
async def login_js():
    return FileResponse(FILES / "login.js", media_type="application/javascript")


@app.get("/login.css")
async def login_css():
    return FileResponse(FILES / "login.css", media_type="text/css")


@app.post("/api/claim")
async def claim(request: Request):
    bridge = request.app.state.bridge
    if request.headers.get("origin") != bridge.public_origin:
        return answer("请在登录站点打开此链接。", 403)
    body = await json_body(request)
    ticket = body.get("ticket") if body else None
    claimed = bridge.claim(ticket) if isinstance(ticket, str) and TOKEN.fullmatch(ticket) else None
    if not claimed:
        return answer("二维码已失效或已被打开，请回 QQ 重新申请。", 410)
    browser, pin = claimed
    selected = bridge.browser_ticket(browser)
    if selected is None:
        return answer("二维码已失效，请回 QQ 重新申请。", 410)
    response = JSONResponse({"state": "waiting", "pin": pin, "group": selected.group})
    response.set_cookie(COOKIE, browser, max_age=TICKET_TTL, secure=True, httponly=True, samesite="strict", path="/")
    return response


@app.get("/api/session")
async def session(request: Request):
    bridge = request.app.state.bridge
    ticket = bridge.browser_ticket(browser_id(request))
    if not ticket:
        return answer("请在 QQ 发送 /璐茜玛 登录 获取二维码。", 410)
    if not ticket.confirmed:
        return {"state": "waiting", "pin": ticket.pin, "group": ticket.group}
    ready = {"state": "ready"}
    if saved := bridge.saved_candidate(ticket):
        ready["saved_login"] = saved
    return ready


@app.post("/api/login")
async def login(request: Request):
    bridge = request.app.state.bridge
    if request.headers.get("origin") != bridge.public_origin:
        return answer("请在登录站点提交。", 403)
    browser = browser_id(request)
    ticket = bridge.browser_ticket(browser)
    if not ticket or not ticket.confirmed:
        return answer("请先在发起登录的 QQ 会话完成核对。", 403)
    body = await json_body(request) or {}
    account, password, remember = body.get("account"), body.get("password"), body.get("remember", False)
    use_saved = body.get("use_saved", False)
    if use_saved is True and set(body) == {"use_saved"}:
        account, password, remember = "", "", False
    elif (use_saved is not False or not valid(account, 254) or not account.strip()
          or not valid(password, 512) or type(remember) is not bool):
        return answer("请检查账号和密码。", 400)
    try:
        status, message = await bridge.login(browser, account.strip(), password, remember, use_saved=use_saved)
    except Exception:
        status, message = 503, "暂时无法登录，请稍后重新扫码。"
    response = answer(message, status)
    if status != 429 and not (use_saved and status == 409):
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict", path="/")
    return response


async def command_context(request: Request, *, group_command: bool = False):
    provided = request.headers.get("authorization", "").encode()
    if not hmac.compare_digest(provided, b"Bearer " + request.app.state.internal_token):
        return PlainTextResponse("Forbidden", status_code=403)
    body = await json_body(request) or {}
    platform, bot, user = body.get("platform"), body.get("bot"), body.get("user")
    action, event, group = body.get("action"), body.get("event"), body.get("group")
    if (platform not in ("onebot", "qq_official") or not valid(bot, 128) or not valid(user, 128)
            or not valid(event, EVENT_ID_MAX_LENGTH) or not isinstance(action, str)):
        return answer("命令无效。", 400)
    if (group_command and not valid(group, 128)) or (not group_command and "group" in body):
        return answer("命令无效。", 400)
    bridge = request.app.state.bridge
    identity = ["group", platform, bot, group, user] if group else [platform, bot, user]
    owner = bridge.vault.index(json.dumps(identity, separators=(",", ":")))
    return bridge, owner, action, body.get("arg", ""), event


def operation_preview(action: str, arg: str) -> str | None:
    if not isinstance(arg, str) or len(arg) > 255:
        return None
    if action in CONFIRMED_OPERATIONS:
        return long_preview(action, arg)
    if action == "long_info" and arg in {"activity", "abyss", "shop"}:
        return "查询长任务可用的关卡、队伍或商品报价。"
    display_arg = re.sub(r"([\\`*_\[\]()<>])", r"\\\1", arg.strip())
    if (action, arg) in APPROVALS:
        return APPROVALS[(action, arg)]
    if action == "redeem" and CODE.fullmatch(arg):
        return f"在绑定的游戏账号兑换一次指定兑换码 `{arg}`。"
    if action == "stages" and arg in {"hunt", "elf"}:
        return "查询已解锁的讨伐关卡。" if arg == "hunt" else "查询已解锁的元素探索关卡。"
    if action == "equipment" and (not arg or valid(arg, 24)):
        return "读取装备并按副属性分数排序。" + (f"套装筛选条件 {display_arg}。" if arg else "")
    if action == "encyclopedia" and valid(arg, 64) and arg.strip():
        return f"查询 Lucima 图鉴中的 {display_arg}。"
    if arg:
        return None
    labels = {
        "login": "生成私聊扫码登录链接。账号密码在登录网页填写。",
        "status": "查看你的 Lucima 授权和任务状态。",
        "receipt": "在原私聊取回最近一张 Lucima 任务回执。",
        "cancel": "取消当前排队任务，或请求停止正在执行的长任务。",
        "unbind": "解除当前游戏账号绑定并清除 Lucima 保存的登录授权。",
        "schedule_status": "查看当前托管计划和最近排队日期。",
    }
    if action in labels:
        return labels[action]
    if action in TASK_NAMES and action not in {"recruit", "donate", "redeem", "stages", "equipment"}:
        return f"为当前绑定的游戏账号执行一次「{TASK_NAMES[action]}」。"
    return None


def execute_operation(bridge, owner: str, action: str, arg: str, event: str, catalog):
    if action == "status":
        return {"message": bridge.status(owner)}
    if action == "cancel":
        return {"message": bridge.cancel(owner)}
    if action == "unbind":
        return {"message": bridge.unbind(owner)}
    if action == "receipt":
        return bridge.receipt(owner, arg)
    if action == "schedule_set":
        return {"message": bridge.set_schedule(owner, arg)}
    if action == "schedule_stop":
        return {"message": bridge.stop_schedule(owner)}
    if action == "schedule_status":
        return {"message": bridge.schedule_status(owner)}
    if action == "encyclopedia":
        query = arg.strip().casefold()
        matches = [line for names, line in catalog if query in names]
        if not matches:
            matches = [line for names, line in catalog if any(query in name for name in names)]
        return {"message": "\n".join(matches[:5]) or "图鉴暂未找到匹配的角色。"}
    return bridge.submit(owner, action, arg, event)


@app.post("/internal/propose")
async def propose(request: Request):
    context = await command_context(request)
    if isinstance(context, (JSONResponse, PlainTextResponse)):
        return context
    bridge, owner, action, arg, event = context
    preview = operation_preview(action, arg)
    if preview is None:
        return answer("操作或参数无效。", 400)
    return bridge.prepare(owner, action, arg, preview=preview, event=event)


@app.post("/internal/command")
async def command(request: Request):
    return await run_command(request, group=False)


@app.post("/internal/group-command")
async def group_command(request: Request):
    return await run_command(request, group=True)


async def run_command(request: Request, *, group: bool):
    context = await command_context(request, group_command=group)
    if isinstance(context, (JSONResponse, PlainTextResponse)):
        return context
    bridge, owner, action, arg, event = context
    if not isinstance(arg, str):
        return answer("命令无效。", 400)

    def execute(operation, argument):
        if operation == "login":
            return bridge.new_ticket(owner, event, group=group)
        return execute_operation(bridge, owner, operation, argument, event, request.app.state.catalog)

    if action == "confirm" and PIN.fullmatch(arg):
        return answer(bridge.confirm(owner, arg))
    if action == "login_notice" and OPERATION_ID.fullmatch(arg):
        return await bridge.wait_login_notice(owner, arg, event)
    if action == "lobby" and not arg:
        return bridge.lobby(owner)
    if action == "approve" and OPERATION_ID.fullmatch(arg):
        return bridge.approve(owner, arg, event, execute)
    if action == "discard" and OPERATION_ID.fullmatch(arg):
        return answer(bridge.discard(owner, arg))
    if action == "receipt" and OPERATION_ID.fullmatch(arg):
        return execute(action, arg)
    if (action, arg) in APPROVALS:
        return bridge.prepare(owner, action, arg, event=event)
    preview = operation_preview(action, arg)
    if preview is not None:
        if action in CONFIRMED_OPERATIONS:
            return bridge.prepare(owner, action, arg, preview=preview, event=event)
        return execute(action, arg)
    return answer("命令无效。", 400)
