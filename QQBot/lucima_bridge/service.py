from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet, InvalidToken

from .accounts_ui import AccountSlots
from .filters import Filters
from .menu import PAGES, THEME_NAMES, menu_data

from .long_tasks import (
    LONG_NAMES, LONG_QUEUE_TTL, SCHEDULE_TASKS, SHOP_HIT_TTL, SHOP_REFRESHES, TASK_TIMEOUT,
    LongTasks, schedule_arguments,
)


TICKET_TTL = 300
LOGIN_TIMEOUT = 110
LOGIN_NOTICE_WAIT = 20
LOGIN_NOTICE_LIMIT = 256
GROUP_BROWSER_LIMIT = 8
SESSION_TTL = 15 * 60
REMEMBER_TTL = 30 * 86400
QUEUE_TTL = 5 * 60
QUEUE_SIZE = 5
SHOP_TARGETS = {"recruit": "招募契约", "mystery": "神秘契约", "chip": "下级强化芯片"}
SHOP_CURRENCIES = {"secret": ("1", "金币"), "star": ("109", "彩虹星源")}
STAR_SOURCE_QUOTE_UNAVAILABLE = (
    "当前缺少可核实的星源商店刷新报价，本次未提交刷新。"
    "可用 /璐茜玛 查看星源商店 查询刷新计数。"
)
TASK_NAMES = {
    "signin": "签到", "mail": "领邮件", "base": "领基地", "redeem": "兑换",
    "dispatch": "派遣领取", "achievement": "成就奖励", "guild": "佣兵团签到",
    "guild_weekly": "佣兵团周奖励", "donate": "佣兵团捐赠", "recruit": "免费招募",
    "stages": "扫荡关卡查询", "equipment": "装备筛选",
    "long_info": "长任务查询", "refresh_secret": "刷新秘密商店", "refresh_star_source": "刷新星源商店",
    "account_view": "基本信息", "friend_support": "助战好友", "send_meal": "每日体力",
    "reactor": "动力转换", "potion": "成长药剂", "starforce": "升星水晶",
    "filter_preview": "装备方案预览",
    "tesseract": "技能模块", "sweep_illusion": "虚拟幻境扫荡", "member_purchase": "团员商店购买",
    **LONG_NAMES,
}
APPROVALS = {
    ("donate", "gold"): "佣兵团捐赠 1 次，最多消耗 5000 金币。",
    ("donate", "courage"): "佣兵团捐赠 1 次，最多消耗 1 个英勇证明。",
    ("recruit", ""): "使用当日免费招募 1 次。执行前会核对免费额度，额度不明或已用时停止。",
    ("refresh_secret", ""): "使用免费额度刷新秘密商店 1 次。冷却未结束时跳过，自动购买 0 件。",
}
OUTCOMES = {
    "ok": "完成。",
    "skipped": "本次没有可执行项目。",
    "deferred": "受游戏维护、日切或账号状态限制，任务未全部执行。",
    "unconfirmed": "任务结果未能完整确认，请先在游戏内核对。",
    "worker_error": "任务结果未知，请先在游戏内核对。",
    "timeout": "任务超时，请先在游戏内核对结果。",
    "auth_expired": "登录授权已失效，请重新扫码。",
    "auth_unavailable": "门户暂时无法续期，本次操作尚未开始。现有授权与托管计划已保留。",
    "not_started": "本次游戏操作尚未开始。",
    "game_login_failed": "未能建立游戏会话，本次操作尚未开始。请稍后重试。",
    "game_login_unavailable": "连接游戏服务器失败，本次操作尚未开始。请稍后重试。",
    "game_login_rejected": "游戏服务器拒绝建立会话，本次操作尚未开始。",
    "quote_unavailable": "当前无法核实刷新价格，本次未提交刷新。",
    "shop_hit": "筛选命中，刷新已停止。",
    "invalid_code": "兑换码无效或已过期。",
    "unexpected_cost": "游戏返回了意外消耗，请核对账户。",
    "duplicate_login": "检测到重复登录，已停止。请退出游戏客户端后再手动发起。",
    "partial": "部分操作已经游戏确认，详见回执。",
    "cancelled": "已停止，游戏已确认的操作见回执。",
}


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def read_auth(result: dict) -> dict | None:
    auth = result.get("auth")
    if not isinstance(auth, dict):
        return None
    limits = {"user_id": 256, "access_token": 4096, "refresh_token": 4096, "device_id": 128}
    if any(not isinstance(auth.get(field), str) or len(auth[field]) > limit for field, limit in limits.items()):
        return None
    if not all(auth[field] for field in ("user_id", "access_token", "device_id")):
        return None
    return {field: auth[field] for field in limits}


class Vault:
    def __init__(self, path: Path, key: str):
        self.cipher = Fernet(key.encode())
        self.index_key = hmac.digest(key.encode(), b"lucima-bridge-index-v1", "sha256")
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA secure_delete = ON")
        self.db.execute("CREATE TABLE IF NOT EXISTS vault_key (verifier TEXT NOT NULL)")
        verifier = self.index("verify-key-v1")
        stored = self.db.execute("SELECT verifier FROM vault_key").fetchone()
        if stored is None:
            with self.db:
                self.db.execute("INSERT INTO vault_key VALUES (?)", (verifier,))
        elif not hmac.compare_digest(verifier, stored[0]):
            self.db.close()
            raise RuntimeError("LUCIMA_VAULT_KEY 与现有绑定数据库不匹配")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS binding (
                owner TEXT PRIMARY KEY, game TEXT UNIQUE NOT NULL,
                credential BLOB, expires INTEGER NOT NULL DEFAULT 0
            )
        """)

    def index(self, data: str) -> str:
        return hmac.digest(self.index_key, data.encode(), "sha256").hex()

    def record(self, owner: str) -> tuple | None:
        return self.db.execute(
            "SELECT game, credential, expires FROM binding WHERE owner = ?", (owner,),
        ).fetchone()

    def bind(self, owner: str, auth: dict, remember: bool, *, account: str = "") -> bool:
        game = self.index("game:" + auth["user_id"])
        current = self.record(owner)
        if current and current[0] != game:
            return False
        expires = int(time.time()) + REMEMBER_TTL if remember else 0
        sealed = self.seal(owner, game, {**auth, "account": account}) if remember else None
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO binding VALUES (?, ?, ?, ?) ON CONFLICT(owner) "
                    "DO UPDATE SET credential = excluded.credential, expires = excluded.expires",
                    (owner, game, sealed, expires),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def seal(self, owner: str, game: str, auth: dict) -> bytes:
        return self.cipher.encrypt(json.dumps([owner, game, auth]).encode())

    def open_credential(self, owner: str, record: tuple | None) -> tuple[dict, str] | None:
        if not record:
            return None
        game, sealed, expires = record
        if not sealed or expires <= time.time():
            return None
        try:
            saved_owner, saved_game, auth = json.loads(self.cipher.decrypt(sealed))
        except (InvalidToken, ValueError):
            return None
        if (saved_owner, saved_game) != (owner, game):
            return None
        usable = read_auth({"auth": auth})
        if not usable or self.index("game:" + usable["user_id"]) != game:
            return None
        account = auth.get("account", "")
        return usable, account if isinstance(account, str) and len(account) <= 254 else ""

    def load(self, owner: str) -> dict | None:
        opened = self.open_credential(owner, self.record(owner))
        return opened[0] if opened else None

    def candidate(self, owner: str) -> dict | None:
        record = self.record(owner)
        opened = self.open_credential(owner, record)
        if opened is None or not opened[0]["refresh_token"]:
            return None
        return {"account": opened[1], "expires": record[2]}

    def update(self, owner: str, auth: dict) -> bool:
        record = self.record(owner)
        opened = self.open_credential(owner, record)
        if opened is None or auth["user_id"] != opened[0]["user_id"]:
            return False
        with self.db:
            self.db.execute("UPDATE binding SET credential = ? WHERE owner = ?", (
                self.seal(owner, record[0], {**auth, "account": opened[1]}), owner,
            ))
        return True

    def forget(self, owner: str) -> None:
        with self.db:
            self.db.execute("UPDATE binding SET credential = NULL, expires = 0 WHERE owner = ?", (owner,))

    def unbind(self, owner: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM binding WHERE owner = ?", (owner,))

    def expire(self) -> None:
        with self.db:
            self.db.execute("UPDATE binding SET credential = NULL, expires = 0 WHERE expires BETWEEN 1 AND ?", (time.time(),))


@dataclass
class Ticket:
    owner: str
    created: float
    browser: str = ""
    pin: str = ""
    confirmed: bool = False
    failures: int = 0
    saved_attempted: bool = False
    group: bool = False


@dataclass
class LoginNotice:
    event: str
    expires: float
    id: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    message: str | None = None
    receipt: dict | None = None


@dataclass
class Job:
    owner: str
    task: str
    code: str
    created: float
    id: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    game: str | None = None
    schedule: str | None = None
    progress: str = ""
    cancel: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def durable(self) -> bool:
        return self.task in LONG_NAMES or self.task in SHOP_REFRESHES or self.schedule is not None

    @property
    def label(self) -> str:
        return ("托管·" if self.schedule else "") + TASK_NAMES[self.task]


@dataclass
class Plan:
    id: str
    task: str
    code: str
    expires: float
    game: str | None
    source_event: str


class Worker:
    def __init__(self, root: Path, python: str):
        self.root = root
        self.python = python

    async def __call__(self, payload: dict, timeout: int, *, cancel=None, progress=None) -> dict:
        with TemporaryDirectory(prefix="lucima-worker-") as directory:
            env = {key: os.environ[key] for key in (
                "PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
            ) if key in os.environ}
            env.update(ARK_PLATFORM="linux", ARK_DATA_DIR=directory, PYTHONDONTWRITEBYTECODE="1")
            launch = asyncio.create_task(asyncio.create_subprocess_exec(
                self.python, "-m", "backend.bot_worker", str(os.getpid()), cwd=self.root, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            ))
            try:
                proc = await asyncio.shield(launch)
            except asyncio.CancelledError:
                proc = await launch
                await self.stop(proc)
                raise
            except OSError:
                return {"status": "worker_error"}
            if cancel is not None:
                return await self.stream(proc, {**payload, "stream": True}, timeout, cancel, progress)
            exchange = asyncio.create_task(proc.communicate(json.dumps(payload).encode() + b"\n"))
            try:
                stdout, _ = await asyncio.wait_for(asyncio.shield(exchange), timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError) as error:
                await self.stop(proc)
                await exchange
                if isinstance(error, asyncio.CancelledError):
                    raise
                return {"status": "timeout"}
            except OSError:
                await self.stop(proc)
                return {"status": "worker_error"}
            try:
                result = json.loads(stdout) if proc.returncode == 0 and len(stdout) < 16_384 else None
            except (ValueError, UnicodeError):
                result = None
            return result if isinstance(result, dict) else {"status": "worker_error"}

    async def stream(self, proc, payload, timeout, cancel, progress):
        async def exchange():
            proc.stdin.write(json.dumps(payload).encode() + b"\n")
            await proc.stdin.drain()
            proc.stdin.close()
            result = None
            while line := await proc.stdout.readline():
                if len(line) >= 16_384 or result is not None:
                    raise ValueError("invalid worker stream")
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("invalid worker response")
                if set(row) == {"progress"} and isinstance(row["progress"], str):
                    progress(row["progress"])
                else:
                    result = row
            await proc.wait()
            return result if result and proc.returncode == 0 else {"status": "worker_error"}

        async def request_stop():
            await cancel.wait()
            if proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), 12)
                except ProcessLookupError:
                    pass
                except asyncio.TimeoutError:
                    await self.stop(proc)

        stopper = asyncio.create_task(request_stop())
        try:
            return await asyncio.wait_for(exchange(), timeout)
        except asyncio.TimeoutError:
            return {"status": "timeout"}
        except (OSError, ValueError):
            return {"status": "worker_error"}
        finally:
            stopper.cancel()
            try:
                await stopper
            except asyncio.CancelledError:
                pass
            if proc.returncode is None:
                # Drain stdout concurrently with process exit so a full pipe cannot hold the slot.
                await asyncio.gather(self.stop(proc), proc.stdout.read())

    @staticmethod
    async def stop(proc) -> None:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()


@dataclass
class ShopHit:
    owner: str
    game: str
    shop: str
    expires: int
    options: dict


class Bridge:
    def __init__(self, vault: Vault, worker: Worker, public_origin: str, *, secret_shop_quote: dict | None = None,
                 star_source_quote: dict | None = None):
        self.vault = vault
        self.accounts = AccountSlots(vault)
        self.filters = Filters(vault.db)
        self.long_tasks = LongTasks(vault.db, int(time.time()))
        self.worker = worker
        self.public_origin = public_origin
        self.secret_shop_quote = secret_shop_quote
        self.star_source_quote = star_source_quote
        self.shop_hits: dict[str, ShopHit] = {}
        self.tickets: dict[str, Ticket] = {}
        self.login_notices: dict[str, LoginNotice] = {}
        self.attempts: dict[str, float] = {}
        self.sessions: dict[str, tuple[float, dict]] = {}
        self.queue: deque[Job] = deque()
        self.active: str | None = None
        self.active_job: Job | None = None
        self.last: dict[str, tuple[float, str]] = {}
        self.receipts: dict[str, tuple[str, float, dict]] = {}
        self.plans: dict[str, Plan] = {}
        self.submitted: dict[tuple[str, str], float] = {}
        self.wake = asyncio.Event()

    def prune(self) -> None:
        now = time.monotonic()
        self.tickets = {key: ticket for key, ticket in self.tickets.items() if now - ticket.created < TICKET_TTL}
        for owner, notice in list(self.login_notices.items()):
            if notice.expires <= now:
                self.dismiss_login_notice(owner)
        self.attempts = {key: expiry for key, expiry in self.attempts.items() if expiry > now}
        self.sessions = {key: session for key, session in self.sessions.items() if session[0] > now}
        self.last = {key: result for key, result in self.last.items() if result[0] > now}
        self.receipts = {key: result for key, result in self.receipts.items() if result[1] > now}
        self.plans = {key: plan for key, plan in self.plans.items() if plan.expires > now}
        current = int(time.time())
        self.shop_hits = {key: hit for key, hit in self.shop_hits.items() if hit.expires > current}
        self.submitted = {key: expiry for key, expiry in self.submitted.items() if expiry > now}
        for job in tuple(self.queue):
            if now - job.created >= (LONG_QUEUE_TTL if job.durable else QUEUE_TTL):
                self.queue.remove(job)
                self.result(job, "排队已超时，请重新提交。", "queue_expired")

    def drop_tickets(self, owner: str) -> None:
        self.tickets = {key: ticket for key, ticket in self.tickets.items() if ticket.owner != owner}

    def dismiss_login_notice(self, owner: str) -> None:
        notice = self.login_notices.pop(owner, None)
        if notice is not None:
            notice.ready.set()

    def new_ticket(self, owner: str, event: str, *, group: bool = False) -> dict:
        self.prune()
        now = time.monotonic()
        slots = self.accounts.owners(self.accounts.scope_for(owner))
        if self.active in slots and self.active_job is None:
            return {"message": "正在验证这次登录申请，请稍等。"}
        if any(ticket.owner == owner and now - ticket.created < 10 for ticket in self.tickets.values()):
            return {"message": "请使用刚发送的二维码，或稍等后重新申请。"}
        if owner not in self.login_notices and len(self.login_notices) >= LOGIN_NOTICE_LIMIT:
            return {"message": "登录申请暂时已满，请稍后重试。"}
        for slot in slots:
            self.drop_tickets(slot)
            self.dismiss_login_notice(slot)
        secret = secrets.token_urlsafe(32)
        self.tickets[fingerprint(secret)] = Ticket(owner, now, group=group)
        notice = LoginNotice(event, now + TICKET_TTL)
        self.login_notices[owner] = notice
        return {"login_url": self.public_origin + "/#" + secret, "login_notice": notice.id}

    async def wait_login_notice(self, owner: str, notice_id: str, event: str) -> dict:
        self.prune()
        notice = self.login_notices.get(owner)
        if notice is None or notice.event != event or not hmac.compare_digest(notice.id, notice_id):
            return {}
        try:
            await asyncio.wait_for(notice.ready.wait(), LOGIN_NOTICE_WAIT)
        except asyncio.TimeoutError:
            self.prune()
            return {"pending": self.login_notices.get(owner) is notice}
        if self.login_notices.get(owner) is not notice or notice.message is None:
            return {}
        self.dismiss_login_notice(owner)
        view = self.navigation(self.accounts.scope_for(owner), "home", notice.message, remember=False)
        return {**view, "menu": "lobby", "receipt": notice.receipt}

    def claim(self, secret: str) -> tuple[str, str] | None:
        self.prune()
        ticket = self.tickets.get(fingerprint(secret))
        if not ticket or ticket.browser:
            return None
        pins = set()
        if ticket.group:
            pins = {row.pin for row in self.tickets.values() if row.owner == ticket.owner and row.browser}
            if len(pins) >= GROUP_BROWSER_LIMIT:
                return None
            ticket = Ticket(ticket.owner, ticket.created, group=True)
        browser = secrets.token_urlsafe(32)
        ticket.browser = fingerprint(browser)
        ticket.pin = f"{secrets.randbelow(100_000_000):08d}"
        while ticket.pin in pins:
            ticket.pin = f"{secrets.randbelow(100_000_000):08d}"
        if ticket.group:
            # Scanning the shared QR only creates an unconfirmed browser candidate.
            self.tickets[ticket.browser] = ticket
        return browser, ticket.pin

    def browser_ticket(self, browser: str) -> Ticket | None:
        self.prune()
        if not browser:
            return None
        digest = fingerprint(browser)
        return next((ticket for ticket in self.tickets.values() if ticket.browser == digest), None)

    def confirm(self, owner: str, pin: str) -> str:
        self.prune()
        candidates = [(key, ticket) for key, ticket in self.tickets.items() if ticket.owner == owner]
        for key, ticket in candidates:
            if not ticket.browser:
                continue
            if hmac.compare_digest(ticket.pin, pin):
                ticket.confirmed = True
                for other, _ in candidates:
                    if other != key:
                        del self.tickets[other]
                return "已确认。请回到刚打开的网页填写登录信息。"
        if any(ticket.browser for _, ticket in candidates):
            _, first = candidates[0]
            first.failures += 1
            if first.failures >= 5:
                self.drop_tickets(owner)
                self.dismiss_login_notice(owner)
        return "核对码无效或已过期，请检查网页或重新申请。"

    def saved_candidate(self, ticket: Ticket) -> dict | None:
        if not ticket.confirmed or ticket.saved_attempted:
            return None
        return self.vault.candidate(ticket.owner)

    async def login(self, browser: str, account: str, password: str, remember: bool,
                    *, use_saved: bool = False) -> tuple[int, str]:
        ticket = self.browser_ticket(browser)
        if not ticket or not ticket.confirmed:
            return 403, "请在发起登录的会话完成核对，或重新获取二维码。"
        owner = ticket.owner
        if self.active or self.queue:
            return 429, "执行服务正忙，请稍等后再提交。"
        if owner in self.attempts:
            return 429, "刚尝试过登录，请稍等后再提交。"
        had_remembered = self.vault.candidate(owner) is not None
        saved_auth = self.vault.load(owner) if use_saved and not ticket.saved_attempted else None
        if use_saved and (saved_auth is None or not saved_auth["refresh_token"]):
            return 409, "已保存的授权暂不可用，请填写账号密码登录。"
        notice = self.login_notices.get(owner)
        if notice is not None:
            notice.expires = time.monotonic() + LOGIN_TIMEOUT + 10
        if use_saved:
            ticket.saved_attempted = True
            payload = {"action": "resume", "auth": saved_auth}
        else:
            self.drop_tickets(owner)
            self.attempts[owner] = time.monotonic() + 30
            device_id = self.vault.index("device:" + owner)[:40]
            payload = {"action": "login", "account": account, "password": password, "device_id": device_id}
        self.active = owner
        try:
            result = await self.worker(payload, LOGIN_TIMEOUT)
            auth = read_auth(result)
            if result.get("status") != "ok" or not auth:
                if use_saved:
                    status = result.get("status")
                    if status in ("auth_unavailable", "timeout", "worker_error"):
                        ticket.saved_attempted = False
                        self.attempts[owner] = time.monotonic() + 30
                        return 503, "门户暂时无法续期，已保存的授权仍保留。请稍后再试。"
                    if status == "auth_expired":
                        self.clear_expired_authorization(owner)
                    return 409, "无法恢复已保存授权，请填写账号密码登录。"
                return 400, "登录失败，请检查账号密码或稍后重新扫码。"
            saved = use_saved or remember and bool(auth["refresh_token"])
            if use_saved:
                if not auth["refresh_token"] or not self.vault.update(owner, auth):
                    return 409, "已保存授权已失效，请填写账号密码登录。"
                self.drop_tickets(owner)
            elif not self.vault.bind(owner, auth, saved, account=account):
                return 409, "该游戏账号无法绑定到当前会话。若已有绑定，请从原会话解绑后再登录。"
            if not saved or not had_remembered:
                self.stop_schedule(owner)
            self.accounts.set_signed_out(owner, False)
            self.accounts.activate(self.accounts.scope_for(owner), self.accounts.position_for(owner))
            self.sessions[owner] = (time.monotonic() + SESSION_TTL, auth)
            self.last.pop(owner, None)
            self.plans.pop(owner, None)
            authorization = "已加密记住登录，最长 30 天。" if saved else "本次临时授权有效 15 分钟。"
            if use_saved:
                authorization = "已恢复记住的登录，保留原授权到期时间。"
            receipt = {"id": secrets.token_urlsafe(12), "task": "登录", "kind": "login", "status": "ok",
                       "message": "登录成功 · QQ 会话已绑定", "details": [authorization],
                       "completed_at": int(time.time()), "theme": self.accounts.preferences(self.accounts.scope_for(owner))[1]}
            self.save_receipt(owner, receipt)
            if notice is not None:
                notice.message = f"登录成功。{authorization}"
                notice.receipt = receipt
                notice.expires = time.monotonic() + 60
                notice.ready.set()
            destination = "发起登录的 QQ 群" if ticket.group else "原 QQ 私聊"
            if remember and not saved:
                return 200, f"绑定成功。游戏未返回续期令牌，本次仅保留临时登录。请返回{destination}查看大厅。"
            return 200, f"绑定成功。请返回{destination}查看大厅，或发送 /璐茜玛 大厅。"
        finally:
            if notice is not None and notice.message is None and self.login_notices.get(owner) is notice:
                if use_saved:
                    notice.expires = ticket.created + TICKET_TTL
                else:
                    self.dismiss_login_notice(owner)
            self.active = None
            self.wake.set()

    def clear_expired_authorization(self, owner: str) -> None:
        self.sessions.pop(owner, None)
        self.vault.forget(owner)
        self.stop_schedule(owner)

    def auth_for(self, owner: str) -> dict | None:
        if self.accounts.is_signed_out(owner):
            return None
        session = self.sessions.get(owner)
        return session[1] if session and session[0] > time.monotonic() else self.vault.load(owner)

    def lobby(self, owner: str) -> dict:
        authorized = bool(self.auth_for(owner))
        message = "当前账号的门户授权可用。刷新信息时会连接游戏。" if authorized else "当前没有有效授权。发送 /璐茜玛 登录 重新扫码。"
        result = self.navigation(self.accounts.scope_for(owner), "home", message)
        result["menu"] = "lobby" if authorized else "login"
        return result

    def navigation(self, scope: str, page: str, summary: str = "", *, remember=True, links=(), replace=False) -> dict:
        if page not in PAGES:
            return {"message": "目录不存在。发送 /璐茜玛 大厅 返回。"}
        owner = self.accounts.active(scope)
        _, theme, _ = self.accounts.preferences(scope)
        if remember and page not in {"accounts", "theme", "logout"}:
            self.accounts.set_page(scope, page)
        account = ""
        if self.vault.record(owner):
            state = "门户已授权" if self.auth_for(owner) else "待授权"
            account = f"当前账号 {self.accounts.label(owner)} · {state}"
        if page == "accounts":
            rows, account_links = [], []
            for slot in self.accounts.owners(scope):
                if not self.vault.record(slot):
                    continue
                pos = self.accounts.position_for(slot)
                status = ("当前 · " if slot == owner else "") + ("已授权" if self.auth_for(slot) else "待授权")
                rows.append(f"{pos}  {self.accounts.label(slot)} · {status}")
                account_links.append((f"切换 {pos}", f"切换账号 {pos}"))
            summary = "\n".join(rows or ["当前会话还没有游戏账号。"])
            summary += "\n发送 /璐茜玛 账号别名 <名称> 修改当前别名。"
            links = (*account_links, ("添加账号", "添加账号"), ("重新扫码", "登录"))
        elif page == "theme":
            summary = f"当前主题 {THEME_NAMES[theme]}。用于 Lucima 生成的图片。"
            links = (*links, ("预览界面", "界面"))
        elif page.startswith("auto_"):
            rule = self.long_tasks.schedule(owner, page.removeprefix("auto_"))
            enabled = rule and self.vault.candidate(owner) and not self.accounts.is_signed_out(owner)
            status = f"托管已启用 · 每天 {rule[2]}" if enabled else "托管未启用"
            summary = f"{status}。\n{PAGES[page].body}"
        elif page == "filter":
            names = self.filters.names(owner)
            summary = "当前账号的方案\n" + (" · ".join(names) if names else "暂无方案")
            summary += ("\n发送 /璐茜玛 预览装备方案 <名称> 或 /璐茜玛 导出装备方案 <名称>。"
                        "\n新建、导入和复制可在下一级查看用法。")
            links = (*links, ("方案管理", "菜单 filter_manage"))
        elif page in {"secret", "starshop"} and not replace:
            names = self.filters.names(owner)
            command = "刷新秘密商店 1 免费 " if page == "secret" else "刷新星源商店 1 "
            if names:
                choices = list(enumerate(names[:5], 1))
                summary = (summary or PAGES[page].body) + "\n" + " · ".join(f"方案 {i} {name}" for i, name in choices)
                links = (*links, *((f"方案 {i}", command + name) for i, name in choices))
        elif page == "home" and not self.auth_for(owner):
            links = (*links, ("扫码授权", "登录"))
        return menu_data(page, account, summary, theme=theme, links=tuple(links), replace=replace)

    def add_account(self, scope: str, event: str, *, group: bool) -> dict:
        slot = self.accounts.reserve(scope)
        if slot is None:
            return {"message": f"当前会话最多可绑定 {self.accounts.LIMIT} 个游戏账号。"}
        return self.new_ticket(slot, event, group=group)

    def switch_account(self, scope: str, position: int) -> dict:
        old = self.accounts.active(scope)
        owner = self.accounts.select(scope, position)
        if not owner or not self.vault.record(owner):
            return {"message": "这个账号还没有绑定。发送 /璐茜玛 切换账号 查看列表。"}
        if owner != old:
            self.plans.pop(old, None)
            self.plans.pop(owner, None)
            self.accounts.activate(scope, position)
        result = self.navigation(scope, self.accounts.preferences(scope)[2], remember=False)
        result["message"] += f"\n已选择 {self.accounts.label(owner)}。"
        return result

    def logout(self, owner: str) -> dict:
        if not self.vault.record(owner):
            return {"message": "当前没有已绑定账号。"}
        if self.active == owner and self.active_job is None:
            return {"message": "登录正在验证，请在验证完成后退出。"}
        scope = self.accounts.scope_for(owner)
        self.accounts.set_signed_out(owner, True)
        self.sessions.pop(owner, None)
        self.cancel(owner)
        self.cancel_queued_schedule(owner)
        self.drop_tickets(owner)
        self.dismiss_login_notice(owner)
        alternatives = [slot for slot in self.accounts.owners(scope) if slot != owner and self.auth_for(slot)]
        if alternatives:
            self.accounts.activate(scope, self.accounts.position_for(alternatives[0]))
        response = self.navigation(scope, "accounts")
        response["message"] = f"已退出 {self.accounts.label(owner)}。\n" + response["message"]
        return response

    @staticmethod
    def purchase_notice(targets):
        names = "、".join(SHOP_TARGETS[target] for target in targets)
        return (f"自动按货架金币价购买命中的{names}。"
                if names else "本轮普通道具自动购买上限 0 档。")

    def prepare_secret_shop(self, owner, count, mode, event, *, scheme, scheme_name, targets):
        if mode == "diamond":
            quote = self.secret_shop_quote
            if not quote:
                return {"status": "quote_unavailable", "message": "秘密商店钻石刷新报价暂不可用，本次未提交刷新。"}
            price = quote["unitPrice"]
            budget = count * price
            task = "refresh_secret_paid"
            order = {"count": count, "quote": quote, "budget": budget, "scheme": scheme, "targets": targets}
            preview = f"秘密商店最多刷新 {count} 次，每次 {price} 颗钻石，刷新扣款上限 {budget} 颗钻石。"
        else:
            task = "refresh_secret"
            order = {"order": f"{count} free", "scheme": scheme, "targets": targets}
            preview = (f"申请 {count} 次，使用免费额度刷新 0 或 1 次，刷新扣款上限 0。"
                       "当前货架可先购买目标道具。")
        preview += self.purchase_notice(targets)
        if scheme_name:
            preview += f" 使用装备方案「{scheme_name}」，命中即停，装备单独确认购买。"
        code = json.dumps(order, ensure_ascii=False, separators=(",", ":"))
        return self.prepare(owner, task, code, preview=preview, event=event)

    def prepare_star_source(self, owner, count, event, *, scheme, scheme_name):
        quote = self.star_source_quote
        if not quote:
            return {"status": "quote_unavailable", "message": STAR_SOURCE_QUOTE_UNAVAILABLE}
        if not scheme:
            return {"message": "请指定当前账号的装备筛选方案，例如 /璐茜玛 刷新星源商店 20 方案名。"}
        order = {"count": count, "quote": quote, "budget": 0, "scheme": scheme}
        preview = (f"星源商店最多免费刷新 {count} 次。使用装备方案「{scheme_name}」。"
                   "命中即停。购买每件消耗 5 个彩虹星源，由本人按档单独确认。")
        code = json.dumps(order, ensure_ascii=False, separators=(",", ":"))
        return self.prepare(owner, "refresh_star_source", code, preview=preview, event=event)

    def prepare(self, owner: str, task: str, code: str, *, preview: str | None = None, event: str = "") -> dict:
        self.prune()
        if preview is None and not self.auth_for(owner):
            return {"message": "请先用 /璐茜玛 登录 完成扫码。"}
        if owner not in self.plans and len(self.plans) >= 256:
            return {"message": "待确认操作已满，请稍后再试。"}
        binding = self.vault.record(owner)
        if task in TASK_NAMES and (task, code) != ("long_info", "shop") and not self.auth_for(owner):
            return {"message": "请先用 /璐茜玛 登录 完成扫码。"}
        if task == "schedule_set" and not self.vault.candidate(owner):
            return {"message": "请在登录网页勾选记住 30 天，再设置托管。"}
        if task == "cancel":
            code = next((job.id for job in (*self.queue, self.active_job)
                         if job and job.owner == owner and (job is not self.active_job or job.task in LONG_NAMES)), "")
        plan = Plan(secrets.token_urlsafe(12), task, code, time.monotonic() + 300,
                    binding[0] if binding else None, event)
        self.plans[owner] = plan
        message = preview if preview is not None else APPROVALS[(task, code)]
        if (task in TASK_NAMES or task in {"logout", "unbind", "clear_logs", "filter_drop"}) and binding is not None:
            message = f"当前账号 {self.accounts.label(owner)}\n" + message
        return {"message": message, "approval": plan.id}

    def approve(self, owner: str, token: str, event: str, execute) -> dict:
        self.prune()
        plan = self.plans.get(owner)
        if not plan or plan.source_event == event or not hmac.compare_digest(plan.id, token):
            return {"message": "执行确认无效、已使用或已过期，请重新发起。"}
        binding = self.vault.record(owner)
        if plan.game != (binding[0] if binding else None):
            self.plans.pop(owner)
            return {"message": "游戏账号绑定已变更，请重新发起。"}
        if plan.task in TASK_NAMES:
            result = self.submit(owner, plan.task, plan.code, event)
            if "job" in result:
                self.plans.pop(owner, None)
            return result
        self.plans.pop(owner)
        if plan.task == "cancel":
            return {"message": self.cancel(owner, job_id=plan.code)}
        return execute(plan.task, plan.code)

    def discard(self, owner: str, token: str) -> str:
        self.prune()
        plan = self.plans.get(owner)
        if not plan or not hmac.compare_digest(plan.id, token):
            return "这份待确认操作已失效。"
        del self.plans[owner]
        return "已放弃这份待确认操作。"

    def submit(self, owner: str, task: str, code: str, message_id: str, *, scheduled=None) -> dict:
        self.prune()
        scope = self.accounts.scope_for(owner)
        anchor = (scope, message_id)
        if anchor in self.submitted:
            return {"message": "这条消息已经提交过。"}
        slots = self.accounts.owners(scope)
        if self.active in slots or any(job.owner in slots for job in self.queue):
            return {"message": "你已有待处理的操作，请用 /璐茜玛 状态 查询。"}
        if (task, code) != ("long_info", "shop") and not self.auth_for(owner):
            return {"message": "请先用 /璐茜玛 登录 完成扫码。"}
        if task in SHOP_REFRESHES:
            pending = self.long_tasks.pending_shop_hit(owner, TASK_NAMES[task], int(time.time()))
            if pending:
                return self.receipt(owner, pending)
        if len(self.queue) >= QUEUE_SIZE:
            return {"message": "游戏任务队列已满，请稍后再试。"}
        job = Job(owner, task, code, time.monotonic())
        binding = self.vault.record(owner)
        job.game = binding[0] if binding else None
        if scheduled:
            job.schedule = scheduled[1]
        if job.durable and not self.long_tasks.enqueue(job, job.label, int(time.time()), scheduled):
            return {"message": "本次托管已处理或计划已变化。"}
        self.submitted[anchor] = time.monotonic() + 600
        self.last.pop(owner, None)
        self.queue.append(job)
        self.wake.set()
        response = {"message": f"已提交{TASK_NAMES[task]}。用 /璐茜玛 状态 查看进度。", "job": job.id}
        if job.durable:
            response.update(long_job=True, message=response["message"] + f"\n回执编号 {job.id}")
        return response

    def status(self, owner: str) -> str:
        self.prune()
        if self.active == owner:
            if (job := self.active_job) and job.durable:
                phase = "正在停止" if job.cancel.is_set() else "正在执行"
                return f"{job.label}{phase}。{job.progress}\n回执编号 {job.id}"
            return "正在处理你的操作。"
        for index, job in enumerate(self.queue, 1):
            if job.owner == owner:
                return f"{job.label}正在排队，前面还有 {index - 1 + int(self.active is not None)} 项。"
        if owner in self.last:
            receipt = self.receipt(owner, "").get("receipt")
            if receipt and "shop_confirm_until" in receipt:
                return receipt["message"]
            return self.last[owner][1]
        previous = self.long_tasks.receipt(owner, "", int(time.time()))
        if previous:
            return self.display_receipt(previous)["message"]
        if self.auth_for(owner):
            return "门户已授权，当前没有任务。"
        return "当前无有效授权。请用 /璐茜玛 登录 扫码。"

    def cancel(self, owner: str, *, job_id: str | None = None) -> str:
        self.prune()
        plan = self.plans.pop(owner, None) if job_id is None else None
        for job in self.queue:
            if job.owner == owner and (job_id is None or job.id == job_id):
                self.queue.remove(job)
                self.result(job, "已取消排队任务。", "cancelled")
                return "已取消排队任务。"
        job = self.active_job
        if job and job.owner == owner and job.task in LONG_NAMES and (job_id is None or job.id == job_id):
            job.cancel.set()
            return "已请求停止。正在提交的操作以游戏结算和最终回执为准。"
        if plan:
            return "已取消待确认操作。"
        if job_id is not None:
            return "原任务已开始、完成或退出队列。"
        return "本次操作正在执行。" if self.active == owner else "没有待取消的任务。"

    def unbind(self, owner: str) -> str:
        if self.active == owner:
            return "当前操作正在执行，完成后可解除绑定。"
        self.vault.unbind(owner)
        self.cancel(owner)
        self.drop_tickets(owner)
        self.dismiss_login_notice(owner)
        self.sessions.pop(owner, None)
        self.last.pop(owner, None)
        self.receipts = {key: row for key, row in self.receipts.items() if row[0] != owner}
        self.long_tasks.clear(owner)
        self.shop_hits = {key: hit for key, hit in self.shop_hits.items() if hit.owner != owner}
        self.filters.drop(owner)
        return "已解除绑定并清除本服务的登录授权。"

    def set_schedule(self, owner: str, code: str) -> str:
        binding, args = self.vault.record(owner), schedule_arguments(code)
        if not binding or not args or not self.vault.candidate(owner):
            return "记住的登录授权不可用，请重新扫码。"
        task, hhmm = args
        self.cancel_queued_schedule(owner, task)
        self.long_tasks.replace_schedule(owner, binding[0], task, hhmm, secrets.token_urlsafe(12), int(time.time()))
        self.wake.set()
        return f"托管已启用。每天北京时间 {hhmm} 执行{SCHEDULE_TASKS[task]}。"

    def schedule_status(self, owner: str) -> str:
        records = self.long_tasks.schedules(owner)
        receipt = self.long_tasks.receipt(owner, "", int(time.time()), scheduled_only=True)
        result = f"\n最近托管回执 /璐茜玛 回执 {receipt['id']}" if receipt else ""
        if not records or not self.vault.candidate(owner):
            return "当前没有有效的托管计划。" + result
        if self.accounts.is_signed_out(owner):
            return "账号已退出，托管暂停。" + result
        rows = [f"{hhmm} {SCHEDULE_TASKS[task]} · 自 {first} 起" + (f" · 上次 {last}" if last else "")
                for _, task, hhmm, first, last, _ in records]
        return "每日托管（北京时间）\n" + "\n".join(rows) + result

    def cancel_queued_schedule(self, owner: str, task: str | None = None) -> None:
        for job in tuple(self.queue):
            if job.owner == owner and job.schedule and (task is None or job.task == task):
                self.queue.remove(job)
                self.result(job, "已取消排队中的托管任务。", "cancelled")

    def stop_schedule(self, owner: str, task: str | None = None) -> str:
        self.long_tasks.stop(owner, task)
        self.cancel_queued_schedule(owner, task)
        return "已停止托管，并取消排队中的托管任务。"

    def enqueue_due(self, now: int) -> None:
        if len(self.queue) >= QUEUE_SIZE:
            return
        for owner, task, day, revision in self.long_tasks.due(now, QUEUE_SIZE + 1):
            if self.accounts.is_signed_out(owner):
                continue
            if not self.vault.candidate(owner):
                self.stop_schedule(owner)
                continue
            self.submit(owner, task, "", f"schedule:{revision}:{day}", scheduled=(day, revision))
            if len(self.queue) >= QUEUE_SIZE:
                break

    def result(self, job: Job, message: str, status: str, details=None, *, shop_options=None) -> None:
        facts = details if isinstance(details, list) else []
        clean = [" ".join(item.split())[:120] for item in facts[:8] if isinstance(item, str)]
        payload = {"id": job.id, "task": job.label, "status": status,
                   "message": message, "details": clean, "completed_at": int(time.time()),
                   "theme": self.accounts.preferences(self.accounts.scope_for(job.owner))[1]}
        if job.task == "account_view" and status == "ok" and clean:
            part = job.code.split(maxsplit=2)
            match = re.match(r"第 (\d+)/(\d+) 页", clean[0])
            if match and part[0] in {"heroes", "items", "teams"}:
                payload["view"] = [self.accounts.position_for(job.owner), part[0], *map(int, match.groups()),
                                   part[2] if len(part) > 2 else ""]
        if status == "shop_hit" and job.task in SHOP_REFRESHES:
            expires = payload["completed_at"] + SHOP_HIT_TTL
            payload["shop_confirm_until"] = expires
            shop = "star" if job.task == "refresh_star_source" else "secret"
            options = self.valid_shop_options(shop, shop_options)
            if options and job.game:
                self.shop_hits[job.id] = ShopHit(job.owner, job.game, shop, expires, options)
                payload["shop_choices"] = self.shop_choices(shop, options)
        if job.durable:
            self.long_tasks.finish(job.id, payload)
        self.last[job.owner] = (time.monotonic() + 3600, message)
        self.save_receipt(job.owner, payload)

    def save_receipt(self, owner: str, payload: dict) -> None:
        self.receipts[payload["id"]] = (owner, time.monotonic() + 3600, payload)
        if len(self.receipts) > 512:
            self.receipts.pop(next(iter(self.receipts)))

    @staticmethod
    def valid_shop_options(shop, options):
        result = {}
        for option in options[:6] if isinstance(options, list) else ():
            if not isinstance(option, dict):
                continue
            slot, key, price = (option.get(field) for field in ("slot", "key", "price"))
            if (type(slot) is int and 1 <= slot <= (3 if shop == "star" else 6)
                    and isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key)
                    and isinstance(price, dict) and set(price) == {"currency", "amount"}
                    and price["currency"] == SHOP_CURRENCIES[shop][0]
                    and (shop == "secret" and (price["amount"] is None
                                               or type(price["amount"]) is int and 0 < price["amount"] <= 10**9)
                         or shop == "star" and type(price["amount"]) is int and price["amount"] == 5)):
                result[slot] = {"slot": slot, "key": key, "price": price}
        return result

    @staticmethod
    def shop_choices(shop, options):
        name = SHOP_CURRENCIES[shop][1]
        choices = []
        for slot, option in sorted(options.items()):
            amount = option["price"]["amount"]
            label = f"{amount} {name}" if amount is not None else "按游戏固定金币价扣款（当前金额未返回）"
            choices.append([slot, label])
        return choices

    def display_receipt(self, payload: dict) -> dict:
        if payload.get("status") != "shop_hit":
            return payload
        visible = {key: value for key, value in payload.items() if key != "shop_choices"}
        if payload.get("shop_confirm_until", 0) <= time.time():
            return {**visible, "status": "ok", "message": f"{payload['task']}：命中确认已超时，本轮已结束。"}
        if "shop_choices" in payload:
            hit = self.shop_hits.get(payload["id"])
            if hit:
                return {**visible, "shop_choices": self.shop_choices(hit.shop, hit.options)}
            visible["message"] = f"{payload['task']}：购买选项已失效，本轮已结束。"
            visible["status"] = "ok"
        return visible

    def buy_shop_hit(self, owner: str, job_id: str, slot: int, event: str) -> dict:
        self.prune()
        hit = self.shop_hits.get(job_id)
        binding = self.vault.record(owner)
        if (not hit or hit.owner != owner or not binding or binding[0] != hit.game or slot not in hit.options):
            return {"message": "购买确认无效、已使用或已过期。"}
        option = hit.options[slot]
        code = json.dumps({**option, "shop": hit.shop, "expires": hit.expires}, separators=(",", ":"))
        response = self.submit(owner, "buy_shop_equipment", code, event)
        if "job" in response:
            del hit.options[slot]
            if not hit.options:
                self.confirm_shop_hit(owner, job_id, purchased=True)
        return response

    def confirm_shop_hit(self, owner: str, job_id: str, *, purchased=False) -> dict:
        if not self.auth_for(owner):
            return {"message": "请回到原会话并选择发起刷新的账号。"}
        now = int(time.time())
        receipt = self.long_tasks.receipt(owner, job_id, now)
        if not receipt or receipt.get("status") != "shop_hit" or receipt.get("shop_confirm_until", 0) <= now:
            return {"message": "此命中确认无效、已处理或已过期。可输入回执编号查询结果。"}
        self.shop_hits.pop(job_id, None)
        finished = {key: value for key, value in receipt.items() if key not in {"shop_choices", "shop_confirm_until"}}
        summary = "购买任务已提交，请查看购买回执。" if purchased else "命中已确认，本轮已结束。"
        finished.update(status="ok", message=f"{receipt['task']}：{summary}")
        self.long_tasks.finish(job_id, finished)
        self.save_receipt(owner, finished)
        return {"message": finished["message"], "receipt": finished}

    def history(self, owner: str, page: int) -> dict:
        self.prune()
        now = int(time.time())
        cutoff = self.accounts.history_cutoff(owner)
        rows = {row["id"]: row for row in self.long_tasks.history(owner, now, after=cutoff)}
        rows.update({data["id"]: data for actor, _, data in self.receipts.values()
                     if actor == owner and data["completed_at"] > cutoff})
        ordered = sorted(rows.values(), key=lambda row: row["completed_at"], reverse=True)
        start = (page - 1) * 5
        visible = ordered[start:start + 5]
        messages = [f"任务历史 · 第 {page} 页", *[
            f"{self.display_receipt(row)['message']}\n回执 {row['id']}" for row in visible
        ]]
        buttons = [(f"回执 {i}", f"回执 {row['id']}") for i, row in enumerate(visible, 1)]
        if page > 1:
            buttons.append(("上一页", f"任务历史 {page - 1}"))
        if start + 5 < len(ordered):
            buttons.append(("下一页", f"任务历史 {page + 1}"))
        return self.navigation(self.accounts.scope_for(owner), "logs", "\n".join(messages) if visible else "本页没有记录。",
                               links=buttons, replace=True)

    def receipt(self, owner: str, job_id: str) -> dict:
        self.prune()
        if job_id:
            stored = self.receipts.get(job_id)
        else:
            stored = next((row for row in reversed(self.receipts.values()) if row[0] == owner), None)
        durable = self.long_tasks.receipt(owner, job_id, int(time.time()))
        if durable and (not stored or stored[0] != owner or durable["completed_at"] > stored[2]["completed_at"]):
            data = self.display_receipt(durable)
            return {"message": data["message"], "receipt": data}
        if stored and stored[0] == owner:
            data = self.display_receipt(stored[2])
            return {"message": data["message"], "receipt": data}
        if job_id and any(job and job.id == job_id and job.owner == owner
                          for job in (self.active_job, *self.queue)):
            return {"message": "任务正在处理。", "pending": True}
        return {"message": "没有可领取的回执，或回执已过期。"}

    async def consume(self) -> None:
        while True:
            self.prune()
            self.vault.expire()
            now = int(time.time())
            self.long_tasks.prune(now)
            self.enqueue_due(now)
            if self.active or not self.queue:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), 30)
                except asyncio.TimeoutError:
                    pass
                continue
            job = self.queue.popleft()
            self.active = job.owner
            self.active_job = job
            try:
                await self.run_job(job)
            except Exception:
                self.result(job, OUTCOMES["worker_error"], "worker_error")
            finally:
                self.active = None
                self.active_job = None

    async def run_job(self, job: Job) -> None:
        public_reference = (job.task, job.code) == ("long_info", "shop")
        auth = None if public_reference else self.auth_for(job.owner)
        binding = self.vault.record(job.owner)
        if not public_reference and (not auth or job.game != (binding[0] if binding else None)):
            self.result(job, OUTCOMES["auth_expired"], "auth_expired")
            return
        if job.schedule:
            schedule = self.long_tasks.schedule(job.owner, job.task)
            auth = self.vault.load(job.owner)
            if not schedule or schedule[-1] != job.schedule or not auth or not auth["refresh_token"]:
                self.result(job, "托管授权已失效或计划已变化，本次任务已取消。", "cancelled")
                return
        payload = {"action": "run", "task": job.task, "code": job.code}
        if auth is not None:
            payload["auth"] = auth
        if job.durable:
            def update(message):
                job.progress = " ".join(message.split())[:160]
                self.long_tasks.progress(job.id, job.progress, int(time.time()))

            update("正在准备游戏连接")
        if job.task in LONG_NAMES:
            result = await self.worker(payload, TASK_TIMEOUT, cancel=job.cancel, progress=update)
        else:
            result = await self.worker(payload, 100)
        status = result.get("status")
        if not isinstance(status, str) or status not in OUTCOMES:
            status = "worker_error"
        elif status == "shop_hit" and job.task not in SHOP_REFRESHES:
            status = "worker_error"
        refreshed = read_auth(result)
        if auth and status == "auth_expired":
            self.clear_expired_authorization(job.owner)
        elif auth and refreshed and refreshed["user_id"] == auth["user_id"]:
            session = self.sessions.get(job.owner)
            if session and session[0] > time.monotonic():
                self.sessions[job.owner] = (session[0], refreshed)
            if refreshed != auth:
                self.vault.update(job.owner, refreshed)
        details = result.get("details")
        if not details and job.progress and status in {"timeout", "worker_error", "unconfirmed"}:
            details = [f"停止前进度：{job.progress}"]
        outcome = OUTCOMES[status]
        if public_reference and status in {"worker_error", "timeout"}:
            outcome = "暂时无法读取商品参考价，请稍后重试。"
        self.result(job, f"{job.label}：{outcome}", status, details, shop_options=result.get("shopOptions"))
