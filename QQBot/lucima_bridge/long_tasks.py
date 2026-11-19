"""Private long-job receipts and explicitly authorized daily schedules."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


LONG_NAMES = {"activity": "活动推进", "abyss": "虚构深渊", "buy_batch": "批量购买"}
SCHEDULE_TASKS = {
    "signin": "签到", "mail": "领邮件", "base": "领基地", "friend_support": "助战好友",
    "send_meal": "每日体力", "reactor": "动力转换", "potion": "成长药剂", "starforce": "升星水晶",
    "dispatch": "领派遣", "achievement": "领成就", "recruit": "免费招募", "guild": "团签到",
    "guild_weekly": "团周奖", "refresh_secret": "刷新秘密商店",
}
CONFIRMED_OPERATIONS = LONG_NAMES.keys() | {"schedule_set", "schedule_stop"}
TASK_TIMEOUT = 12 * 60
LONG_QUEUE_TTL = 30 * 60
RECEIPT_TTL = 24 * 3600
LOCAL = ZoneInfo("Asia/Shanghai")


def schedule_arguments(arg):
    parts = arg.split()
    if (len(parts) == 2 and parts[0] in SCHEDULE_TASKS
            and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", parts[1])):
        return parts
    return None


def preview(action, arg):
    parts = arg.split()
    if action == "activity" and len(parts) == 4:
        family, team, count, energy = parts
        if (re.fullmatch(r"BH[0-9]{1,5}", family) and re.fullmatch(r"[A-Za-z0-9_-]{1,24}", team)
                and re.fullmatch(r"[1-3]", count) and re.fullmatch(r"[0-9]{1,3}", energy)):
            return f"以队伍 {team} 推进活动 {family}，最多 {count} 关，体力预算 {energy}。"
    if action == "abyss" and len(parts) == 3:
        scene, *teams = parts
        if re.fullmatch(r"MysteriousRealm_[0-9]{1,2}", scene) and all(
                re.fullmatch(r"(?:[A-Za-z0-9_]{1,24}|-)(?:,(?:[A-Za-z0-9_]{1,24}|-)){0,3}", team)
                and any(role != "-" for role in team.split(",")) for team in teams):
            return f"挑战虚构深渊 {scene} 一次。\n上半 {teams[0]}\n下半 {teams[1]}\n禁止使用魂力和自动进入下一层。"
    if action == "buy_batch" and 1 <= len(parts) <= 5:
        rows, totals = {}, {}
        for part in parts:
            match = re.fullmatch(r"([A-Za-z0-9_]{1,32}):([1-9]|10):([0-9]{1,8}):([0-9]{1,8})", part)
            if not match:
                return None
            sid, count, currency, price = match.groups()
            if sid in rows:
                return None
            rows[sid] = f"{sid} × {count}，单价 {int(price)}（货币 {currency}）"
            totals[currency] = totals.get(currency, 0) + int(count) * int(price)
        budget = "、".join(f"{item} × {amount}" for item, amount in totals.items())
        return "批量购买\n" + "\n".join(rows.values()) + f"\n扣款上限 {budget}。报价或购买资格变化时停止。"
    if action == "schedule_set" and (args := schedule_arguments(arg)):
        task, hhmm = args
        return (f"每天北京时间 {hhmm} 执行一次{SCHEDULE_TASKS[task]}，并替换这个任务之前的时间。"
                "只在记住登录授权有效时执行。到点后一小时内排队，回执可在原会话查询。")
    if action == "schedule_stop" and not arg:
        return "停止当前每日托管并取消尚在队列中的托管任务。已提交给游戏的操作将在本次回执中记录。"
    if action == "schedule_stop" and arg in SCHEDULE_TASKS:
        return f"停止「{SCHEDULE_TASKS[arg]}」计划并取消该任务尚在队列中的托管执行。"
    return None


class LongTasks:
    def __init__(self, db, now: int):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS lucima_task_receipt (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, task TEXT NOT NULL,
                state TEXT NOT NULL, updated INTEGER NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS lucima_receipt_owner ON lucima_task_receipt(owner, updated);
        """)
        self._migrate_schedule()
        for job_id, owner, task, state, payload in db.execute(
                "SELECT id, owner, task, state, payload FROM lucima_task_receipt WHERE state != 'done'").fetchall():
            status = "unconfirmed" if state == "running" else "cancelled"
            message = ("服务重启，任务结果待核对。请先在游戏内查看。" if state == "running"
                       else "服务重启，排队任务已取消。")
            details = [json.loads(payload)] if state == "running" and payload else []
            receipt = {"id": job_id, "task": task, "status": status, "message": message,
                       "details": details, "completed_at": now}
            self.finish(job_id, receipt)
        self.prune(now)

    def _migrate_schedule(self):
        columns = self.db.execute("PRAGMA table_info(lucima_schedule)").fetchall()
        has_legacy_key = bool(columns) and not any(row[1] == "task" and row[5] for row in columns)
        with self.db:
            self.db.execute("BEGIN")
            if has_legacy_key:
                self.db.execute("ALTER TABLE lucima_schedule RENAME TO lucima_schedule_old")
            self.db.execute("""CREATE TABLE IF NOT EXISTS lucima_schedule (
                owner TEXT NOT NULL, game TEXT NOT NULL, task TEXT NOT NULL, hhmm TEXT NOT NULL,
                first_day TEXT NOT NULL, last_day TEXT NOT NULL DEFAULT '', revision TEXT NOT NULL,
                PRIMARY KEY (owner, task)
            )""")
            if has_legacy_key:
                self.db.execute("INSERT INTO lucima_schedule SELECT * FROM lucima_schedule_old")
                self.db.execute("DROP TABLE lucima_schedule_old")

    def enqueue(self, job, name, now, scheduled=None):
        with self.db:
            if scheduled:
                day, revision = scheduled
                changed = self.db.execute("""UPDATE lucima_schedule SET last_day = ?
                    WHERE owner = ? AND revision = ? AND last_day < ?""", (day, job.owner, revision, day))
                if not changed.rowcount:
                    return False
            self.db.execute("INSERT INTO lucima_task_receipt VALUES (?, ?, ?, 'queued', ?, '')",
                            (job.id, job.owner, name, now))
        return True

    def progress(self, job_id, message, now):
        with self.db:
            self.db.execute("UPDATE lucima_task_receipt SET state = 'running', updated = ?, payload = ? WHERE id = ?",
                            (now, json.dumps(message), job_id))

    def finish(self, job_id, receipt):
        with self.db:
            self.db.execute("UPDATE lucima_task_receipt SET state = 'done', updated = ?, payload = ? WHERE id = ?",
                            (receipt["completed_at"], json.dumps(receipt), job_id))

    def receipt(self, owner, job_id, now, *, scheduled_only=False):
        where = " AND id = ?" if job_id else ""
        if scheduled_only:
            where += " AND task LIKE '托管·%'"
        args = (owner, now - RECEIPT_TTL, job_id) if job_id else (owner, now - RECEIPT_TTL)
        row = self.db.execute("SELECT payload FROM lucima_task_receipt WHERE owner = ? AND updated > ?"
                              " AND state = 'done'" + where + " ORDER BY updated DESC, rowid DESC LIMIT 1", args).fetchone()
        return json.loads(row[0]) if row else None

    def history(self, owner, now, *, after=0):
        rows = self.db.execute("""SELECT payload FROM lucima_task_receipt
            WHERE owner = ? AND state = 'done' AND updated > ?
            ORDER BY updated DESC, rowid DESC LIMIT 100""", (owner, max(after, now - RECEIPT_TTL)))
        return [json.loads(row[0]) for row in rows]

    def prune(self, now):
        with self.db:
            self.db.execute("DELETE FROM lucima_task_receipt WHERE state = 'done' AND updated <= ?", (now - RECEIPT_TTL,))

    def schedule(self, owner, task=None):
        if task is not None:
            return self.db.execute("""SELECT game, task, hhmm, first_day, last_day, revision FROM lucima_schedule
                WHERE owner = ? AND task = ?""", (owner, task)).fetchone()
        return self.db.execute("SELECT game, task, hhmm, first_day, last_day, revision FROM lucima_schedule WHERE owner = ?",
                               (owner,)).fetchone()

    def schedules(self, owner):
        return self.db.execute("""SELECT game, task, hhmm, first_day, last_day, revision FROM lucima_schedule
            WHERE owner = ? ORDER BY hhmm, task""", (owner,)).fetchall()

    def replace_schedule(self, owner, game, task, hhmm, revision, now):
        local = datetime.fromtimestamp(now, LOCAL)
        first = local.date() + timedelta(days=int(local.strftime("%H:%M") >= hhmm))
        with self.db:
            self.db.execute("""INSERT INTO lucima_schedule VALUES (?, ?, ?, ?, ?, '', ?)
                ON CONFLICT(owner, task) DO UPDATE SET game = excluded.game,
                hhmm = excluded.hhmm, first_day = excluded.first_day, revision = excluded.revision""",
                (owner, game, task, hhmm, first.isoformat(), revision))

    def due(self, now, limit):
        local = datetime.fromtimestamp(now, LOCAL)
        start = local - timedelta(hours=1)
        windows = [(local.date().isoformat(), start.strftime("%H:%M"), local.strftime("%H:%M"))]
        if start.date() != local.date():
            windows = [(start.date().isoformat(), start.strftime("%H:%M"), "23:59"),
                       (local.date().isoformat(), "00:00", local.strftime("%H:%M"))]
        result = []
        for day, earliest, latest in windows:
            rows = self.db.execute("""SELECT s.owner, s.task, s.revision FROM lucima_schedule s
                JOIN binding b ON b.owner = s.owner AND b.game = s.game
                LEFT JOIN lucima_account_slot a ON a.owner = s.owner
                WHERE s.first_day <= ? AND s.last_day < ? AND s.hhmm BETWEEN ? AND ?
                      AND b.credential IS NOT NULL AND b.expires > ? AND COALESCE(a.signed_out, 0) = 0
                      ORDER BY s.hhmm, s.owner LIMIT ?""",
                (day, day, earliest, latest, now, limit - len(result))).fetchall()
            result.extend((owner, task, day, revision) for owner, task, revision in rows)
        return result

    def stop(self, owner, task=None):
        with self.db:
            if task is None:
                self.db.execute("DELETE FROM lucima_schedule WHERE owner = ?", (owner,))
            else:
                self.db.execute("DELETE FROM lucima_schedule WHERE owner = ? AND task = ?", (owner, task))

    def clear(self, owner):
        self.stop(owner)
        with self.db:
            self.db.execute("DELETE FROM lucima_task_receipt WHERE owner = ?", (owner,))
