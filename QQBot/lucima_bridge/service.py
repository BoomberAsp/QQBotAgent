from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet, InvalidToken


TICKET_TTL = 300
SESSION_TTL = 15 * 60
REMEMBER_TTL = 30 * 86400
QUEUE_TTL = 5 * 60
QUEUE_SIZE = 5
TASK_NAMES = {
    "signin": "签到", "mail": "领邮件", "base": "领基地", "redeem": "兑换",
    "dispatch": "派遣领取", "achievement": "成就奖励", "guild": "佣兵团签到",
    "guild_weekly": "佣兵团周奖励", "donate": "佣兵团捐赠", "recruit": "免费招募",
    "stages": "扫荡关卡查询", "equipment": "装备筛选",
}
APPROVALS = {
    ("donate", "gold"): "佣兵团捐赠 1 次，最多消耗 5000 金币。",
    ("donate", "courage"): "佣兵团捐赠 1 次，最多消耗 1 个英勇证明。",
    ("recruit", ""): "使用当日免费招募 1 次。执行前会核对免费额度，额度不明或已用时停止。",
}
OUTCOMES = {
    "ok": "完成。",
    "skipped": "本次没有可执行项目。",
    "deferred": "受游戏维护、日切或账号状态限制，任务未全部执行。",
    "unconfirmed": "任务结果未能完整确认，请先在游戏内核对。",
    "worker_error": "任务结果未知，请先在游戏内核对。",
    "timeout": "任务超时，请先在游戏内核对结果。",
    "auth_expired": "登录授权已失效，请重新扫码。",
    "invalid_code": "兑换码无效或已过期。",
    "unexpected_cost": "游戏返回了意外消耗，请核对账户。",
    "duplicate_login": "检测到重复登录，已停止。请退出游戏客户端后再手动发起。",
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

    def bind(self, owner: str, auth: dict, remember: bool) -> bool:
        game = self.index("game:" + auth["user_id"])
        current = self.record(owner)
        if current and current[0] != game:
            return False
        expires = int(time.time()) + REMEMBER_TTL if remember else 0
        sealed = self.seal(owner, game, auth) if remember else None
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

    def load(self, owner: str) -> dict | None:
        record = self.record(owner)
        if not record:
            return None
        game, sealed, expires = record
        if not sealed or expires <= time.time():
            return None
        try:
            saved_owner, saved_game, auth = json.loads(self.cipher.decrypt(sealed))
        except (InvalidToken, ValueError):
            return None
        return read_auth({"auth": auth}) if (saved_owner, saved_game) == (owner, game) else None

    def update(self, owner: str, auth: dict) -> None:
        record = self.record(owner)
        if record and record[1] and record[2] > time.time():
            with self.db:
                self.db.execute("UPDATE binding SET credential = ? WHERE owner = ?", (
                    self.seal(owner, record[0], auth), owner,
                ))

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


@dataclass
class Job:
    owner: str
    task: str
    code: str
    created: float
    id: str = field(default_factory=lambda: secrets.token_urlsafe(12))


@dataclass
class Plan:
    id: str
    task: str
    code: str
    expires: float


class Worker:
    def __init__(self, root: Path, python: str):
        self.root = root
        self.python = python

    async def __call__(self, payload: dict, timeout: int) -> dict:
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

    @staticmethod
    async def stop(proc) -> None:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()


class Bridge:
    def __init__(self, vault: Vault, worker: Worker, public_origin: str):
        self.vault = vault
        self.worker = worker
        self.public_origin = public_origin
        self.tickets: dict[str, Ticket] = {}
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
        self.attempts = {key: expiry for key, expiry in self.attempts.items() if expiry > now}
        self.sessions = {key: session for key, session in self.sessions.items() if session[0] > now}
        self.last = {key: result for key, result in self.last.items() if result[0] > now}
        self.receipts = {key: result for key, result in self.receipts.items() if result[1] > now}
        self.plans = {key: plan for key, plan in self.plans.items() if plan.expires > now}
        self.submitted = {key: expiry for key, expiry in self.submitted.items() if expiry > now}
        while self.queue and now - self.queue[0].created >= QUEUE_TTL:
            job = self.queue.popleft()
            self.result(job, "排队已超时，请重新提交。", "queue_expired")

    def drop_tickets(self, owner: str) -> None:
        self.tickets = {key: ticket for key, ticket in self.tickets.items() if ticket.owner != owner}

    def new_ticket(self, owner: str) -> dict:
        self.prune()
        now = time.monotonic()
        if any(ticket.owner == owner and now - ticket.created < 10 for ticket in self.tickets.values()):
            return {"message": "请使用刚发送的二维码，或稍等后重新申请。"}
        if len(self.tickets) >= 256:
            return {"message": "登录申请暂时已满，请稍后重试。"}
        self.drop_tickets(owner)
        secret = secrets.token_urlsafe(32)
        self.tickets[fingerprint(secret)] = Ticket(owner, now)
        return {"login_url": self.public_origin + "/#" + secret}

    def claim(self, secret: str) -> tuple[str, str] | None:
        self.prune()
        ticket = self.tickets.get(fingerprint(secret))
        if not ticket or ticket.browser:
            return None
        browser = secrets.token_urlsafe(32)
        ticket.browser = fingerprint(browser)
        ticket.pin = f"{secrets.randbelow(100_000_000):08d}"
        return browser, ticket.pin

    def browser_ticket(self, browser: str) -> Ticket | None:
        self.prune()
        if not browser:
            return None
        digest = fingerprint(browser)
        return next((ticket for ticket in self.tickets.values() if ticket.browser == digest), None)

    def confirm(self, owner: str, pin: str) -> str:
        self.prune()
        for key, ticket in self.tickets.items():
            if ticket.owner != owner or not ticket.browser:
                continue
            if hmac.compare_digest(ticket.pin, pin):
                ticket.confirmed = True
                return "已确认。请回到刚打开的网页填写登录信息。"
            ticket.failures += 1
            if ticket.failures >= 5:
                del self.tickets[key]
            break
        return "核对码无效或已过期，请检查网页或重新申请。"

    async def login(self, browser: str, account: str, password: str, remember: bool) -> tuple[int, str]:
        ticket = self.browser_ticket(browser)
        if not ticket or not ticket.confirmed:
            return 403, "请从原私聊完成核对，或重新获取二维码。"
        owner = ticket.owner
        if self.active or self.queue:
            return 429, "执行服务正忙，请稍等后再提交。"
        if owner in self.attempts:
            return 429, "刚尝试过登录，请稍等后再提交。"
        self.drop_tickets(owner)
        self.active = owner
        self.attempts[owner] = time.monotonic() + 30
        device_id = self.vault.index("device:" + owner)[:40]
        try:
            result = await self.worker({
                "action": "login", "account": account, "password": password, "device_id": device_id,
            }, 110)
            auth = read_auth(result)
            if result.get("status") != "ok" or not auth:
                return 400, "登录失败，请检查账号密码或稍后重新扫码。"
            saved = remember and bool(auth["refresh_token"])
            if not self.vault.bind(owner, auth, saved):
                return 409, "该账号无法与此 QQ 身份绑定。若要切换已有绑定，请先在私聊解绑。"
            self.sessions[owner] = (time.monotonic() + SESSION_TTL, auth)
            self.last.pop(owner, None)
            self.plans.pop(owner, None)
            if remember and not saved:
                return 200, "绑定成功。游戏未返回续期令牌，本次仅保留临时登录。"
            return 200, "绑定成功。请返回 QQ 私聊执行任务。"
        finally:
            self.active = None
            self.wake.set()

    def auth_for(self, owner: str) -> dict | None:
        session = self.sessions.get(owner)
        return session[1] if session and session[0] > time.monotonic() else self.vault.load(owner)

    def prepare(self, owner: str, task: str, code: str) -> dict:
        self.prune()
        if not self.auth_for(owner):
            return {"message": "请先用 /璐茜玛 登录 完成扫码。"}
        plan = Plan(secrets.token_urlsafe(12), task, code, time.monotonic() + 300)
        self.plans[owner] = plan
        return {"message": APPROVALS[(task, code)], "approval": plan.id}

    def approve(self, owner: str, token: str, event: str) -> dict:
        self.prune()
        plan = self.plans.get(owner)
        if not plan or not hmac.compare_digest(plan.id, token):
            return {"message": "执行确认无效、已使用或已过期，请重新发起。"}
        result = self.submit(owner, plan.task, plan.code, event)
        if "job" in result:
            self.plans.pop(owner, None)
        return result

    def discard(self, owner: str, token: str) -> str:
        self.prune()
        plan = self.plans.get(owner)
        if not plan or not hmac.compare_digest(plan.id, token):
            return "这份待确认操作已失效。"
        del self.plans[owner]
        return "已放弃这份待确认操作。"

    def submit(self, owner: str, task: str, code: str, message_id: str) -> dict:
        self.prune()
        anchor = (owner, message_id)
        if anchor in self.submitted:
            return {"message": "这条消息已经提交过。"}
        if self.active == owner or any(job.owner == owner for job in self.queue):
            return {"message": "你已有待处理的操作，请用 /璐茜玛 状态 查询。"}
        if not self.auth_for(owner):
            return {"message": "请先用 /璐茜玛 登录 完成扫码。"}
        if len(self.queue) >= QUEUE_SIZE:
            return {"message": "游戏任务队列已满，请稍后再试。"}
        self.submitted[anchor] = time.monotonic() + 600
        self.last.pop(owner, None)
        job = Job(owner, task, code, time.monotonic())
        self.queue.append(job)
        self.wake.set()
        return {"message": f"已提交{TASK_NAMES[task]}。用 /璐茜玛 状态 查看进度。", "job": job.id}

    def status(self, owner: str) -> str:
        self.prune()
        if self.active == owner:
            return "正在处理你的操作。"
        if any(job.owner == owner for job in self.queue):
            return "你的任务正在排队。"
        if owner in self.last:
            return self.last[owner][1]
        if self.auth_for(owner):
            return "账号已授权，当前没有任务。"
        return "当前无有效授权。请用 /璐茜玛 登录 扫码。"

    def cancel(self, owner: str) -> str:
        self.prune()
        plan = self.plans.pop(owner, None)
        for job in self.queue:
            if job.owner == owner:
                self.queue.remove(job)
                self.result(job, "已取消排队任务。", "cancelled")
                return "已取消排队任务。"
        if plan:
            return "已取消待确认操作。"
        return "本次操作正在执行。" if self.active == owner else "没有待取消的任务。"

    def unbind(self, owner: str) -> str:
        if self.active == owner:
            return "当前操作正在执行，完成后可解除绑定。"
        self.vault.unbind(owner)
        self.cancel(owner)
        self.drop_tickets(owner)
        self.sessions.pop(owner, None)
        self.last.pop(owner, None)
        self.receipts = {key: row for key, row in self.receipts.items() if row[0] != owner}
        return "已解除绑定并清除本服务的登录授权。"

    def result(self, job: Job, message: str, status: str, details=None) -> None:
        expires = time.monotonic() + 3600
        facts = details if isinstance(details, list) else []
        clean = [" ".join(item.split())[:120] for item in facts[:8] if isinstance(item, str)]
        payload = {"id": job.id, "task": TASK_NAMES[job.task], "status": status,
                   "message": message, "details": clean, "completed_at": int(time.time())}
        self.last[job.owner] = (expires, message)
        self.receipts[job.id] = (job.owner, expires, payload)
        if len(self.receipts) > 512:
            self.receipts.pop(next(iter(self.receipts)))

    def receipt(self, owner: str, job_id: str) -> dict:
        self.prune()
        if job_id:
            stored = self.receipts.get(job_id)
        else:
            stored = next((row for row in reversed(self.receipts.values()) if row[0] == owner), None)
        if stored and stored[0] == owner:
            return {"message": stored[2]["message"], "receipt": stored[2]}
        if job_id and any(job and job.id == job_id and job.owner == owner
                          for job in (self.active_job, *self.queue)):
            return {"message": "任务正在处理。", "pending": True}
        return {"message": "没有可领取的回执，或回执已过期。"}

    async def consume(self) -> None:
        while True:
            self.prune()
            self.vault.expire()
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
        auth = self.auth_for(job.owner)
        if not auth:
            self.result(job, OUTCOMES["auth_expired"], "auth_expired")
            return
        result = await self.worker({"action": "run", "auth": auth, "task": job.task, "code": job.code}, 100)
        status = result.get("status")
        if not isinstance(status, str) or status not in OUTCOMES:
            status = "worker_error"
        refreshed = read_auth(result)
        if status == "auth_expired":
            self.sessions.pop(job.owner, None)
            self.vault.forget(job.owner)
        elif refreshed and refreshed["user_id"] == auth["user_id"]:
            session = self.sessions.get(job.owner)
            if session and session[0] > time.monotonic():
                self.sessions[job.owner] = (session[0], refreshed)
            if refreshed != auth:
                self.vault.update(job.owner, refreshed)
        self.result(job, f"{TASK_NAMES[job.task]}：{OUTCOMES[status]}", status, result.get("details"))
