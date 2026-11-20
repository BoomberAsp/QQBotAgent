from __future__ import annotations

import asyncio
import base64
import fcntl
import hmac
import json
import os
import re
import sys
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from .account_card import render_account_card
from .long_tasks import ACTIVITY_SWEEP_QUERY, CONFIRMED_OPERATIONS, ILLUSION_SWEEP_ORDER, STAGE_QUERY
from .long_tasks import preview as long_preview
from .filters import NAME as FILTER_NAME, parse_scheme
from .manual import MANUAL_INDEX, MANUAL_PATH, render_manual
from .menu import PAGES, THEMES, shop_arguments
from .proposals import ALLOWED_OPERATIONS
from .service import APPROVALS, Bridge, SHOP_TARGETS, TASK_NAMES, TICKET_TTL, Vault, Worker


FILES = Path(__file__).resolve().parent
COOKIE = "__Host-lucima"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
CODE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
PIN = re.compile(r"[0-9]{8}\Z")
OPERATION_ID = re.compile(r"[A-Za-z0-9_-]{16}\Z")
SHOP_PICK = re.compile(r"([A-Za-z0-9_-]{16}) ([1-6])\Z")
READ_TASKS = {"long_info", "account_view", "stages", "equipment"}
SHOP_ACTIONS = {"refresh_secret", "refresh_star_source"}
ACCOUNT_VIEWS = {"summary", "resources", "heroes", "items", "teams", "guild", "weekly", "guild_shop",
                 "abyss_sweep", "arena", "vote", "equip_event", "guild_aid", "guild_captain", "star_source"}
ACCOUNT_PAGE = re.compile(r"([a-z_]+)(?: ([1-9][0-9]{0,2})(?: ([^\x00-\x1f]{1,32}))?)?\Z")
GUILD_ORDER = re.compile(r"([A-Za-z0-9_]{1,32}) ([1-9]|10) (29|30) ([1-9][0-9]{0,6})\Z")
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


def load_secret_shop_quote(root: Path) -> dict | None:
    try:
        policy = json.loads((root / "assets" / "secret_shop_diamond_quote.json").read_bytes())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(policy, dict):
        return None
    quote = {key: policy.get(key) for key in ("gameVersion", "catalog", "currency", "unitPrice")}
    if (quote["currency"] != "2" or type(quote["unitPrice"]) is not int or quote["unitPrice"] < 1
            or not isinstance(quote["gameVersion"], str) or not isinstance(quote["catalog"], str)):
        return None
    return quote


def load_star_source_quote(root: Path) -> dict | None:
    try:
        quote = json.loads((root / "assets" / "star_source_shop_quote.json").read_bytes())
    except (OSError, ValueError, RecursionError):
        return None
    if (isinstance(quote, dict) and set(quote) == {"gameVersion", "catalog", "refresh", "equipment"}
            and all(isinstance(quote[key], str) and quote[key] for key in ("gameVersion", "catalog"))
            and quote["refresh"] == {"currency": "", "amount": 0}
            and quote["equipment"] == {"currency": "109", "amount": 5}
            and all(type(quote[key]["amount"]) is int for key in ("refresh", "equipment"))):
        return quote
    return None


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
        app.state.asset_root = root
        origin = public_origin()
        vault = Vault(data_dir / "binding.sqlite3", os.environ["LUCIMA_VAULT_KEY"])
        try:
            worker = Worker(root, str(python))
            app.state.bridge = Bridge(vault, worker, origin, secret_shop_quote=load_secret_shop_quote(root),
                                      star_source_quote=load_star_source_quote(root))
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


@app.get(MANUAL_PATH, response_class=HTMLResponse)
async def manual_page():
    return render_manual()


@app.get("/manual.css")
async def manual_css():
    return FileResponse(FILES / "manual.css", media_type="text/css")


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
    if status != 429 and not (use_saved and status in {409, 503}):
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
    if action == "long_info" and (arg in {"activity", "abyss", "shop"} or ACTIVITY_SWEEP_QUERY.fullmatch(arg)):
        return "查询长任务可用的关卡、队伍或商品报价。"
    if action == "account_view" and (match := ACCOUNT_PAGE.fullmatch(arg)):
        kind, page, _ = match.groups()
        if kind in ACCOUNT_VIEWS and (not page or kind in {"heroes", "items", "teams"}):
            return "读取当前游戏账号的指定快照。"
        return None
    if action == "member_purchase":
        match = GUILD_ORDER.fullmatch(arg)
        if not match:
            return None
        item, count, currency, unit = match.groups()
        return f"团员商店购买 {item} × {count}，单价 {unit}，最多扣除货币 {currency} × {int(count) * int(unit)}。"
    if action == "sweep_illusion":
        if not (match := ILLUSION_SWEEP_ORDER.fullmatch(arg)):
            return None
        count, floor = match.groups()
        if floor:
            return (f"扫荡虚拟幻境第 {floor} 层一次，消耗全部 {count} 个意识同步器，体力消耗 0。\n"
                    "最高通关楼层或持有数量变化时停止。")
        return (f"扫荡虚拟幻境已通关且符合条件的最高层，意识同步器上限 {count}，体力消耗 0。"
                "游戏会使用执行时持有的全部意识同步器。")
    if action == "tesseract" and not arg:
        return "检查技能模块并领取已完成的产物。加速次数上限 0。"
    if action in SHOP_ACTIONS:
        if not (order := shop_arguments(action, arg)):
            return None
        count, mode = order
        if mode == "free":
            return (f"使用免费额度刷新秘密商店 1 次。申请 {count} 次，本次可刷 0 或 1 次。"
                    "冷却未结束时跳过，扣款上限 0，自动购买 0 件。")
        return None
    display_arg = re.sub(r"([\\`*_\[\]()<>])", r"\\\1", arg.strip())
    if (action, arg) in APPROVALS:
        return APPROVALS[(action, arg)]
    if action == "redeem" and CODE.fullmatch(arg):
        return f"在绑定的游戏账号兑换一次指定兑换码 `{arg}`。"
    if action == "stages" and STAGE_QUERY.fullmatch(arg):
        return "查询已解锁的讨伐关卡与参考体力。" if arg.startswith("hunt") else "查询已解锁的元素探索关卡与参考体力。"
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
        "logout": "退出当前游戏账号，暂停托管并取消排队任务。保留账号配置。",
        "clear_logs": "清空当前账号的日志显示，保留任务结算记录和按编号取回回执。",
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
    if action == "logout":
        return bridge.logout(owner)
    if action == "clear_logs":
        bridge.accounts.hide_history(owner, int(time.time()))
        return bridge.navigation(bridge.accounts.scope_for(owner), "logs", "已清空当前账号的日志显示。")
    if action == "filter_drop":
        bridge.filters.drop(owner, arg)
        return {"message": f"已删除装备方案「{arg}」。"}
    if action == "receipt":
        return bridge.receipt(owner, arg)
    if action == "schedule_set":
        return {"message": bridge.set_schedule(owner, arg)}
    if action == "schedule_stop":
        return {"message": bridge.stop_schedule(owner, arg or None)}
    if action == "schedule_status":
        return {"message": bridge.schedule_status(owner)}
    if action == "encyclopedia":
        query = arg.strip().casefold()
        matches = [line for names, line in catalog if query in names]
        if not matches:
            matches = [line for names, line in catalog if any(query in name for name in names)]
        return {"message": "\n".join(matches[:5]) or "图鉴暂未找到匹配的角色。"}
    return bridge.submit(owner, action, arg, event)


def shop_plan(bridge, scope, action, arg, event, *, propose=False):
    owner = bridge.accounts.active(scope)
    scheme = None
    scheme_name = ""
    targets = list(SHOP_TARGETS)
    target_flag = ""
    words = arg.split()
    if words and words[-1].startswith("目标="):
        if action != "refresh_secret":
            return {"message": "星源商店使用装备筛选方案。"}
        target_flag = words.pop()
        target_text = target_flag.removeprefix("目标=")
        names = {**{key: key for key in SHOP_TARGETS}, **{name: key for key, name in SHOP_TARGETS.items()},
                 "强化芯片": "chip"}
        choices = re.split("[,，、]", target_text)
        if target_text in {"无", "none"}:
            targets = []
        elif all(choice in names for choice in choices):
            targets = list(dict.fromkeys(names[choice] for choice in choices))
        else:
            return {"message": "普通购买目标可选招募契约、神秘契约、下级强化芯片，用逗号分隔，也可填目标=无。"}

    order = shop_arguments(action, " ".join(words))
    if order is None and len(words) >= 2 and FILTER_NAME.fullmatch(words[-1]):
        order = shop_arguments(action, " ".join(words[:-1]))
        if order and order[1] != "choose":
            scheme_name = words[-1]
            payload = bridge.filters.get(owner, scheme_name)
            if not payload:
                return {"message": "当前账号没有这个装备方案。"}
            scheme = json.loads(payload)
        else:
            order = None
    if order is None:
        return {"message": "次数请输入 1–200 的整数。秘密商店支持 免费 或 钻石，可在末尾指定装备方案。"}
    count, mode = order
    if mode == "choose":
        if propose:
            return {"message": "请指定使用免费额度还是钻石刷新。"}
        suffix = " " + target_flag if target_flag else ""
        return bridge.navigation(scope, "secret", f"申请 {count} 次，先选择刷新方式。",
                                 links=(("选择免费", f"刷新秘密商店 {count} 免费{suffix}"),
                                        ("选择钻石", f"刷新秘密商店 {count} 钻石{suffix}")), replace=True)
    if mode == "star":
        return bridge.prepare_star_source(owner, count, event, scheme=scheme, scheme_name=scheme_name)
    return bridge.prepare_secret_shop(owner, count, mode, event, scheme=scheme, scheme_name=scheme_name, targets=targets)


@app.post("/internal/propose")
async def propose(request: Request):
    return await prepare_proposal(request, group=False)


@app.post("/internal/group-propose")
async def group_propose(request: Request):
    return await prepare_proposal(request, group=True)


async def prepare_proposal(request: Request, *, group: bool):
    context = await command_context(request, group_command=group)
    if isinstance(context, (JSONResponse, PlainTextResponse)):
        return context
    bridge, scope, action, arg, event = context
    if action not in ALLOWED_OPERATIONS:
        return answer("操作或参数无效。", 400)
    owner = bridge.accounts.active(scope)
    if action in SHOP_ACTIONS and isinstance(arg, str) and len(arg) <= 96:
        return shop_plan(bridge, scope, action, arg, event, propose=True)
    preview = operation_preview(action, arg)
    if preview is None:
        return answer("操作或参数无效。", 400)
    if group and action == "login":
        preview = "在原群生成扫码登录链接。账号密码在登录网页填写。"
    elif group and action == "receipt":
        preview = "在原群取回你当前账号的最近一张 Lucima 任务回执。"
    return bridge.prepare(owner, action, arg, preview=preview, event=event)


async def render_profile_receipt(response, root, *, group=False, current_account=True):
    receipt = response.get("receipt")
    if not receipt or receipt.get("kind") != "profile":
        return response
    card = receipt.get("account_profile")
    display = {key: value for key, value in receipt.items() if key not in {"account_profile", "profile_binding"}}
    if card:
        display["expand_profile"] = group and current_account and not card["full"]
        try:
            png = await asyncio.to_thread(render_account_card, card, root, receipt.get("theme"))
            if len(png) <= 512 * 1024:
                display["profile_png"] = base64.b64encode(png).decode("ascii")
        except Exception:
            # The existing text summary is also the image fallback.
            pass
    return {**response, "receipt": display}


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
    bridge, scope, action, arg, event = context
    if not isinstance(arg, str):
        return answer("命令无效。", 400)
    if action == "manual" and not arg:
        return answer(f"{MANUAL_INDEX}\n完整版\n{bridge.public_origin}{MANUAL_PATH}")
    owner = bridge.accounts.active(scope)

    def execute(operation, argument):
        if operation == "login":
            return bridge.new_ticket(owner, event, group=group)
        return execute_operation(bridge, owner, operation, argument, event, request.app.state.catalog)

    if action == "confirm" and PIN.fullmatch(arg):
        ticket_owner = next((slot for slot in bridge.accounts.owners(scope) if slot in bridge.login_notices), owner)
        return answer(bridge.confirm(ticket_owner, arg))
    if action == "login_notice" and OPERATION_ID.fullmatch(arg):
        notice_owner = next((slot for slot in bridge.accounts.owners(scope)
                             if (notice := bridge.login_notices.get(slot)) and hmac.compare_digest(notice.id, arg)), owner)
        return await bridge.wait_login_notice(notice_owner, arg, event)
    if action == "account_view" and arg == "profile":
        return bridge.submit(owner, "account_view", "profile" if group else "profile_full", event)
    if action == "expand_profile" and group and OPERATION_ID.fullmatch(arg):
        receipt = bridge.receipt(owner, arg).get("receipt", {})
        binding = bridge.vault.record(owner)
        card = receipt.get("account_profile")
        if not card or card["full"] or not binding or receipt.get("profile_binding") != binding[0]:
            return answer("这张账号卡已失效，请在原会话重新刷新。")
        return bridge.prepare(
            owner, "account_view", "profile_full", event=event,
            preview="在本群显示完整账号信息，包含 UID、详细资源和团员预览。群成员都可以看到。",
        )
    if action == "lobby" and not arg:
        return bridge.lobby(owner)
    if action == "navigate" and arg in PAGES:
        return bridge.navigation(scope, arg)
    if action == "view_card" and not arg:
        page = bridge.accounts.preferences(scope)[2]
        return {**bridge.navigation(scope, page), "picture": True}
    if action == "add_account" and not arg:
        return bridge.add_account(scope, event, group=group)
    if action == "switch_account" and re.fullmatch(r"[1-5]", arg):
        return bridge.switch_account(scope, int(arg))
    if action == "rename_account" and arg:
        if not bridge.accounts.rename(owner, arg):
            return answer("请先绑定账号，别名使用 1–12 个汉字、字母、数字或连字符。", 400)
        return bridge.navigation(scope, "accounts")
    if action == "theme" and arg in THEMES:
        bridge.accounts.set_theme(scope, THEMES[arg])
        return {**bridge.navigation(scope, bridge.accounts.preferences(scope)[2]), "picture": True}
    if action == "history" and re.fullmatch(r"[1-9][0-9]{0,2}", arg):
        return bridge.history(owner, int(arg))
    if action == "view_page":
        position, _, query = arg.partition(" ")
        if position != str(bridge.accounts.position_for(owner)):
            return {"message": "当前账号已切换。请重新打开要查看的账号页面。"}
        match = ACCOUNT_PAGE.fullmatch(query)
        if match and match.group(1) in {"heroes", "items", "teams"} and match.group(2):
            return bridge.submit(owner, "account_view", query, event)
        return answer("分页参数无效。", 400)
    if action == "timer" and not arg:
        start = time.perf_counter()
        await asyncio.sleep(1)
        elapsed = time.perf_counter() - start
        return bridge.navigation(scope, "timer", f"目标 1 秒 · 实测 {elapsed:.3f} 秒 · 偏差 {(elapsed - 1)*1000:+.1f} 毫秒。")
    if action.startswith("filter_"):
        if not bridge.vault.record(owner):
            return {"message": "请先绑定当前游戏账号。"}
        if action == "filter_save":
            scheme = parse_scheme(arg)
            if not scheme:
                return {"message": "方案格式无效。可在工具箱的方案管理页查看格式。"}
            return {"message": "已保存方案。" if bridge.filters.save(owner, *scheme) else "当前账号的方案数量已达上限。"}
        if action == "filter_copy":
            names = arg.split()
            if len(names) != 2 or not all(FILTER_NAME.fullmatch(name) for name in names):
                return {"message": "请输入原方案名和新方案名。"}
            original, copy = (bridge.filters.get(owner, name) for name in names)
            if not original or copy:
                return {"message": "原方案不存在，或新名称已使用。"}
            payload = json.loads(original)
            payload["name"] = names[1]
            saved = bridge.filters.save(owner, names[1], json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            return {"message": "已复制方案。" if saved else "当前账号的方案数量已达上限。"}
        if FILTER_NAME.fullmatch(arg) and (payload := bridge.filters.get(owner, arg)):
            if action == "filter_export":
                return {"message": payload}
            if action == "filter_preview":
                return bridge.submit(owner, "filter_preview", payload, event)
            if action == "filter_drop":
                return bridge.prepare(owner, action, arg, preview=f"删除当前账号的装备方案「{arg}」。", event=event)
        return {"message": "未找到这个装备方案。"}
    if action == "approve" and OPERATION_ID.fullmatch(arg):
        return await render_profile_receipt(bridge.approve(owner, arg, event, execute),
                                            request.app.state.asset_root, group=group)
    if action == "discard" and OPERATION_ID.fullmatch(arg):
        return answer(bridge.discard(owner, arg))
    if action == "receipt" and (not arg or OPERATION_ID.fullmatch(arg)):
        for slot in bridge.accounts.owners(scope) if arg else (owner,):
            response = bridge.receipt(slot, arg)
            if response.get("receipt") or response.get("pending"):
                if slot != owner and (receipt := response.get("receipt")):
                    response["receipt"] = {k: v for k, v in receipt.items()
                                           if k not in {"view", "shop_confirm_until", "shop_choices", "battle_choices",
                                                        "illusion_quote", "sweep_choices"}}
                return await render_profile_receipt(response, request.app.state.asset_root,
                                                    group=group, current_account=slot == owner)
        return execute(action, arg)
    if action == "confirm_shop_hit" and OPERATION_ID.fullmatch(arg):
        return bridge.confirm_shop_hit(owner, arg)
    if action == "buy_shop_hit" and (pick := SHOP_PICK.fullmatch(arg)):
        return bridge.buy_shop_hit(owner, pick.group(1), int(pick.group(2)), event)
    if action in SHOP_ACTIONS and len(arg) <= 96:
        return shop_plan(bridge, scope, action, arg, event)
    preview = operation_preview(action, arg)
    if preview is not None:
        if (action in CONFIRMED_OPERATIONS or action in {"logout", "unbind", "clear_logs"}
                or action in TASK_NAMES and action not in READ_TASKS):
            return bridge.prepare(owner, action, arg, preview=preview, event=event)
        return execute(action, arg)
    return answer("命令无效。", 400)
