"""
QQ official bot entry — stage 4 of the QQ-official worktree plan.

Thin mirror of plugins/agent_router.py: one on_message matcher with a
platform-specific type guard, event → agent.inbound view conversion
(adapters/qq_official.py::build_view), per-turn sender binding through
the _turn_sender contextvar, and flush() of the buffering official
sender once the turn completes.

Official-route specifics vs the OneBot entry (plan §7.5/§7.6):
  - no continuous-session router: group messages only ever arrive
    @-gated (c2c_group_at_messages intent) and c2c has no @ concept,
    so "no-@ window" mode has nothing to act on;
  - no poke router (no official API equivalent); changelog/announcement
    broadcast and the merged group-list export live in agent_router
    (they serve both routes), while this plugin owns the official group
    registry (GROUP_AT bookkeeping, §4.5), the daily mail pass, and the
    WebUI mail-admin queue worker (§16.2);
  - the handler is annotated with QQ event types — NoneBot dispatches by
    annotation, so this matcher never sees OneBot events and vice versa
    (both entries safely coexist at priority=1, block=False);
  - identity is opaque openids: each inbound message logs the author
    openid at INFO so admins can collect it into
    QQ_OFFICIAL_SUPERUSERS / QQ_OFFICIAL_VIP_USERS (plan §7.7).

WebUI observability: the panel is a separate process with no bot handle,
so this plugin mirrors the changelog file-queue IPC pattern — it writes
data/official_status.json (atomic tmp+replace) on startup / bot connect /
bot disconnect plus a 60s heartbeat while the process lives, and the
panel's webui/official_status.py reads it with mtime staleness rules.
The periodic heartbeat is essential: message-driven writes alone would
let a quiet evening degrade the panel card to "stale" falsely.
"""
import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from nonebot import get_bots, get_driver, logger, on_message, on_notice
from nonebot.adapters.qq import Bot as QQBot, MessageSegment
from nonebot.adapters.qq.event import (
    C2CMsgReceiveEvent, C2CMsgRejectEvent, QQMessageEvent,
)

from adapters.qq_official import (
    HANDLED_EVENTS,
    QQOfficialSender,
    build_view,
    kb_snapshot,
    send_active_c2c,
    send_active_c2c_markdown,
    send_active_c2c_media,
)
from adapters.qq_official_buttons import click_snapshot
from adapters.qq_official_stream import stream_snapshot
from agent import bond_content, greeting
from agent.asset_memory import AssetCard, make_asset_id, prompt_hash_of
from agent.character_registry import get_character_registry
from agent.context import _turn_sender
from agent.delegation import _LLM_ERROR_PREFIXES
from agent.group_registry import GroupRegistry
from agent.mailbox import escape_md_text, make_preview, new_letter_id
from agent.rate_limit import TokenBucket
from agent.turn import (
    _asset_memory, _bond_manager, _mailbox_manager, _model_router,
    _perm_manager, _special_sessions, run_agent_turn,
)
from lib import media_gen
from lib.button_spec import Btn
from lib.button_spec import fit_label as _fit_label
from lib.button_spec import max_per_row as _buttons_max_per_row
from lib.button_spec import validate as _validate_buttons
from lib.status_heartbeat import read_status, write_status
from tools.social_tools import build_image_prompt, enforce_asset_quota

official_router = on_message(priority=1, block=False)
official_notice_router = on_notice(priority=1, block=False)


# ── Status heartbeat file (panel reads, never writes) ─────────────

_STATUS_FILE = Path(__file__).resolve().parent.parent / "data" / "official_status.json"
_HEARTBEAT_INTERVAL = 60      # seconds between heartbeat refreshes
_MSG_WRITE_THROTTLE = 5.0     # min seconds between message-counter writes

_connected = False
_msg_total = 0
_last_msg_at: float | None = None
_last_msg_write = 0.0


def _read_status() -> dict:
    return read_status(_STATUS_FILE)


def _write_status(**patch) -> None:
    """Merge patch into the status file (atomic; lib/status_heartbeat).

    Best-effort: observability must never break message handling.
    """
    try:
        write_status(_STATUS_FILE, **patch)
    except OSError as e:
        logger.warning(f"[qq_official] status write failed: {e}")


def _note_message() -> None:
    """Bump in-memory message counters; flush to disk at most every 5s."""
    global _msg_total, _last_msg_at, _last_msg_write
    _msg_total += 1
    _last_msg_at = time.time()
    if _last_msg_at - _last_msg_write >= _MSG_WRITE_THROTTLE:
        _last_msg_write = _last_msg_at
        _write_status(last_message_at=_last_msg_at, messages_total=_msg_total)


@get_driver().on_startup
async def _official_status_startup():
    global _msg_total, _last_msg_at
    # Counters are lifetime-cumulative: seed from any pre-restart file.
    cur = _read_status()
    _msg_total = int(cur.get("messages_total") or 0)
    _last_msg_at = cur.get("last_message_at")
    _write_status(state="starting", appid=None, connected_at=None,
                  messages_total=_msg_total, last_message_at=_last_msg_at)

    async def _heartbeat():
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            # Unconditional: proves process liveness even while the
            # official websocket is down (state stays "disconnected"
            # and fresh, instead of degrading to "stale").
            # buttons= (§10.1): keyboard breaker state + click telemetry,
            # so the panel/ops can see degradation without tailing logs.
            # stream= (§6.10.11.2): streaming breaker state + env, same
            # observability rationale for the c2c stream route.
            _write_status(
                messages_total=_msg_total, last_message_at=_last_msg_at,
                buttons={**kb_snapshot(), **click_snapshot()},
                stream=stream_snapshot())

    asyncio.create_task(_heartbeat())


@get_driver().on_bot_connect
async def _official_bot_connect(bot):
    global _connected
    if not isinstance(bot, QQBot):
        return  # OneBot (NapCat) bot connecting — not ours
    _connected = True
    _write_status(state="connected", appid=str(bot.self_id),
                  connected_at=time.time())
    logger.info(f"[qq_official] status: connected appid={bot.self_id}")


@get_driver().on_bot_disconnect
async def _official_bot_disconnect(bot):
    global _connected
    if not isinstance(bot, QQBot):
        return
    _connected = False
    _write_status(state="disconnected", connected_at=None)
    logger.warning(f"[qq_official] status: disconnected appid={bot.self_id}")


# ── P3b: HEARTBEAT 低频主动问候（官方 c2c 主动消息）────────────────
#
# 主动消息 = send_to_c2c 不带 msg_id，受平台每用户每日/每月小额配额 +
# 内容审核约束。簿记（候选索引 / 配额 / 冷却）全部在 agent/greeting.py
# 的纯函数里；本插件只做 bot 句柄获取、LLM 文案生成与发送编排。
# 全程 best-effort：任何失败只记日志，绝不影响入站消息处理。

_GREET_TARGETS_FILE = Path(__file__).resolve().parent.parent / "data" / "bond_greet_targets.json"
_GREET_QUOTA_FILE = Path(__file__).resolve().parent.parent / "data" / "official_active_quota.json"
_GROUP_REGISTRY_FILE = Path(__file__).resolve().parent.parent / "data" / "group_registry.json"

# 群登记表（§4.5/§7.3）：v1 只由 GROUP_AT_MESSAGE_CREATE 纯记账 upsert
# （节流在 GroupRegistry 内部），零发送风险；群发送/其余群事件归 P1。
_group_registry = GroupRegistry(str(_GROUP_REGISTRY_FILE))

# LLM 文案失败时的模板降级（{name} = 角色短名）——legacy greeting 路径用；
# 邮件信件**没有**模板兜底（§5.3：失败即丢弃，模板会再次制造时间错位）。
_GREET_FALLBACKS = (
    "{name}：「好久不见～最近过得怎么样呀？」",
    "{name}：「在忙吗？突然有点想找你聊聊天。」",
    "{name}：「今天也要好好吃饭、好好休息哦～」",
)


@get_driver().on_startup
async def _greeting_startup():
    # mail_enabled=true（默认）→ 每日 10:00 单一主动 loop（信封优先、媒体补位，
    # 双向 active_sent_today 互斥，§4.1）；false → 旧 HEARTBEAT greeting loop
    # （§12 回滚路径，保留一个版本）。启动时读取，切换需重启。
    if bool(_bond_manager._config.get("mail_enabled", True)):
        asyncio.create_task(_daily_active_loop())
    else:
        asyncio.create_task(_greeting_loop())


async def _daily_active_loop():
    """每日 mail_hour(默认10:00) 单一主动 loop（proactive-mail §4.1）。

    - 启动补发：now ≥ mail_hour 且 < 22:00 → 立即跑一次（信封内信件数减半，
      降低重启后第一波 LLM 突发）；幂等由每用户 last_digest_date 保证。
    - 信封 pass 先跑；媒体 pass 补位未收到信封的用户（双向互斥）。
    """
    # 启动补发
    now_dt = datetime.now()
    hour = _mail_hour()
    if hour <= now_dt.hour < 22:
        try:
            await _run_mail_pass(catch_up=True)
        except Exception as e:
            logger.warning(f"[mail] catch-up pass failed: {e!r}")
        try:
            await _run_media_pass()
        except Exception as e:
            logger.warning(f"[media] catch-up pass failed: {e!r}")
    while True:
        now_dt = datetime.now()
        target = now_dt.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now_dt:
            target += timedelta(days=1)
        delay = (target - now_dt).total_seconds() + random.uniform(0, 600)
        await asyncio.sleep(delay)
        try:
            await _run_mail_pass()      # 文本信封优先
        except Exception as e:
            logger.warning(f"[mail] pass failed: {e!r}")
        try:
            await _run_media_pass()     # 媒体只补位未收到信封的用户
        except Exception as e:
            logger.warning(f"[media] pass failed: {e!r}")


async def _greeting_loop():
    """Legacy periodic greeting pass (mail_enabled=false 回滚路径，§12)."""
    while True:
        cfg = _bond_manager._config
        hours = float(cfg.get("greet_interval_hours", 6.0))
        jitter = float(cfg.get("greet_jitter_minutes", 45))
        await asyncio.sleep(hours * 3600 + random.uniform(0, max(0.0, jitter) * 60))
        try:
            await _run_greeting_pass()
        except Exception as e:
            logger.warning(f"[qq_official] greeting pass failed: {e!r}")
        try:
            await _run_media_pass()
        except Exception as e:
            logger.warning(f"[qq_official] media pass failed: {e!r}")


def _get_qq_bot() -> QQBot | None:
    for b in get_bots().values():
        if isinstance(b, QQBot):
            return b
    return None


# ── 主动消息「角色信箱」mail pass（proactive-mail-design.md v0.5 §4/§5/§6）──
#
# 三阶段：A 收集候选（纯本地文件读）→ B 生成信件（用户间信号量并发、
# 用户内串行，角色面 flash client，失败即丢弃无模板兜底）→ C 串行发送
# （令牌桶 10 qps + 错峰 + pass 时间预算）。发送成功才记账：
# record_digest（幂等，重读校验 last_digest_date）→ 配额 kind="text" →
# bond 事件（信件**全文**，§9.1）。

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def _mail_hour() -> int:
    try:
        return max(0, min(23, int(_bond_manager._config.get("mail_hour", 10))))
    except (TypeError, ValueError):
        return 10


def _known_personas_from_sessions(openid: str) -> list:
    """Sessions 索引 → 「确实有过特殊会话」的人格集合（§4.2 数据源 2）。"""
    out = []
    try:
        index = _special_sessions._load_index(openid)
        for entry in index.get("sessions", []):
            if not isinstance(entry, dict):
                continue
            meta = entry.get("metadata") or {}
            if not isinstance(meta, dict):
                continue
            for key in ("persona_key", "char_id"):
                val = meta.get(key)
                if val and val not in out:
                    out.append(str(val))
    except Exception:
        pass
    return out


def _mail_candidates(openid: str, mailbox: dict, cfg: dict) -> list:
    """过滤链（§4.2）→ [(persona, state)]，按 (level desc, last_seen asc) 排序。"""
    pm = get_character_registry()
    now = time.time()
    min_level = int(cfg.get("mail_min_level", 5))
    inactive_s = float(cfg.get("mail_inactive_hours", 24)) * 3600
    cooldown_s = float(cfg.get("mail_persona_cooldown_days", 7)) * 86400
    known = mailbox.get("known_personas") or {}
    persona_states = mailbox.get("personas") or {}
    out = []
    try:
        personas = _bond_manager.list_personas(openid)
    except Exception:
        return []
    for persona in personas:
        if persona == "assistant":
            continue                                   # unified，不算角色
        state = _bond_manager.get_state(openid, persona, create=False)
        if not isinstance(state, dict):
            continue
        try:
            if not pm._exists(persona, user_id=openid):
                continue                               # UGC 已删
            if pm.get_mode(persona) != "split":
                continue                               # 只给双平面角色发
        except Exception:
            continue
        if int(state.get("level", 1) or 1) < min_level:
            continue
        stats = state.get("stats") or {}
        if int(stats.get("msgs", 0) or 0) <= 0:
            continue
        if persona not in known:
            continue                                   # 从未有过特殊会话
        last_seen = float(stats.get("last_seen") or 0)
        if not last_seen or now - last_seen < inactive_s:
            continue                                   # 刚聊过不发
        p_state = persona_states.get(persona) or {}
        last_mail = float(p_state.get("last_mail_at", 0) or 0)
        if last_mail and now - last_mail < cooldown_s:
            continue                                   # 同角色冷却中
        out.append((persona, state, last_seen))
    out.sort(key=lambda t: (-int(t[1].get("level", 1) or 1), t[2]))
    return [(p, s) for p, s, _ls in out]


def _mail_due(openid: str, mailbox: dict, candidates: list, cfg: dict) -> bool:
    """每日发送条件（§4.3）。media_sent_today 由调用方对称检查（双向互斥）。"""
    if not candidates:
        return False
    now = time.time()
    if mailbox.get("last_digest_date") == time.strftime("%Y-%m-%d"):
        return False                                   # 同日幂等
    hour = datetime.now().hour
    if hour < _mail_hour() or hour >= 22:
        return False                                   # 10:00–22:00 窗口
    gap_s = float(cfg.get("mail_min_gap_hours", 20)) * 3600
    if now - float(mailbox.get("last_digest_at", 0) or 0) < gap_s:
        return False                                   # 防「正常+补发」双发
    if not greeting.active_allowed(
            str(_GREET_QUOTA_FILE), openid,
            daily_cap=int(cfg.get("mail_daily_cap", 1)),
            monthly_cap=10 ** 9):                      # 只查日上限，不查月配额
        return False
    return True


async def _compose_mail_letter(persona_key: str, state: dict) -> str:
    """一封信（角色面 flash client，§5.0/§5.1）。失败返回 ''（丢弃，无模板）。"""
    pm = get_character_registry()
    try:
        card = pm.load(persona_key) or ""
        if not card:
            return ""
        now = time.time()
        last_seen = float((state or {}).get("stats", {}).get("last_seen") or 0)
        gap_hours = max(0.0, (now - last_seen) / 3600.0) if last_seen else 0.0
        # 关键：不 clamp 到 ≥1 天——刚聊过必须承认「几小时」（§5.1）
        gap_desc = (
            "不到一小时" if gap_hours < 1 else
            "大约几小时" if gap_hours < 12 else
            "大约一天" if gap_hours < 48 else
            f"大约 {int(gap_hours // 24)} 天"
        )
        level = int((state or {}).get("level", 1) or 1)
        system = (
            card
            + "\n\n---\n你正在扮演上述角色。用户最近没有来找你，你想留一句话给 TA。"
              "保持角色口吻，自然、克制、不煽情；不要提系统、邮件、配额或本条指示。"
        )
        prompt = (
            f"距你们上次交谈{gap_desc}（羁绊 Lv{level}）。"
            f"写一句不超过 60 字的留言，只输出留言本身。"
        )
        raw = await _model_router.flash_client.chat_completion(
            message=prompt,
            history=[{"role": "system", "content": system}],
            timeout_set=20.0,
            purpose="mail_letter",
        )
        candidate = (raw or "").strip()
        if (
            candidate
            and not candidate.startswith(_LLM_ERROR_PREFIXES)
            and len(candidate) <= 100     # 60 字要求 + 宽松超限容忍
            and "\n" not in candidate
        ):
            return candidate
    except Exception:
        pass
    return ""


async def _build_letters(openid: str, candidates: list, cfg: dict,
                         deadline: float) -> list:
    """为一个用户串行生成信件（同 persona card 上下文更可控，§5.0）。"""
    pm = get_character_registry()
    letters = []
    for persona, state in candidates:
        if time.monotonic() > deadline:
            break
        text = await _compose_mail_letter(persona, state)
        if not text:
            continue                                   # 失败即丢弃（§5.3）
        try:
            name = pm.get_display_name(persona) or persona
        except Exception:
            name = persona
        letters.append({
            "id": new_letter_id(),                     # 发送前分配：按钮 data 需要
            "persona": persona,
            "name": name,
            "text": text,
            "preview": make_preview(text),
            "created_at": time.time(),
            "read": False,
        })
    # 本地审核默认关（§5.3）；开启时一批一审（合并一次调用），拒绝弃整批
    if letters and bool(cfg.get("mail_review_enabled", False)):
        try:
            joined = "\n".join(
                f"{i + 1}. {l['text']}" for i, l in enumerate(letters))
            ok, reason = await media_gen.review_prompt(
                joined, _model_router.flash_client)
            if not ok:
                logger.warning(
                    f"[mail] {openid[:8]}… batch rejected by review: {reason}")
                return []
        except Exception as e:
            logger.warning(f"[mail] review failed (fail-open): {e!r}")
    return letters


def _build_envelope(letters: list, cfg: dict):
    """信封（§6.1/§6.2 + 附录 B1/B2/B5）→ (md_text, plain_text, rows|None)。

    - 单封且 ≤80 字 + mail_full_when_single → B2 全文，无按钮；
    - 多封 → B1 列表 + ≤mail_max_previews 个「展开」指令按钮（长名用 ①…
      映射）；总长超 mail_max_chars 按尾部（等级低）截断为「另有 N 封」；
    - plain_text = 档 3 纯文本降级（B5 风格），不带任何 md 记号。
    """
    now_dt = datetime.now()
    date_label = f"{now_dt.month}月{now_dt.day}日"
    max_chars = int(cfg.get("mail_max_chars", 800))
    max_previews = max(1, int(cfg.get("mail_max_previews", 5)))

    # ── B2：单封全文 ──
    if (len(letters) == 1 and bool(cfg.get("mail_full_when_single", True))
            and len(letters[0].get("text", "")) <= 80):
        only = letters[0]
        name = escape_md_text(only.get("name") or "")
        text = escape_md_text(only.get("text") or "")
        md = (
            f"## 今日角色来信 · {date_label}\n"
            f"**{name}**：{text}\n\n"
            f"> 想回看：发送 `/邮件`；想找 TA：`/切换会话` 或 `/人格切换`"
        )
        plain = (
            f"【今日角色来信 · {date_label}】\n"
            f"{only.get('name', '')}：{only.get('text', '')}\n\n"
            f"发送 /邮件 可回看（保留最近 28 天）。"
        )
        return md, plain, None

    # ── B1：多封列表 + 按钮 ──
    shown = letters[:max_previews]
    btns = []
    for i, letter in enumerate(shown):
        name = letter.get("name") or ""
        if len(name) <= 4:
            label = f"展开{name}"
        elif i < len(_CIRCLED):
            label = f"展开{_CIRCLED[i]}"
        else:
            label = f"展开{i + 1}"
        btns.append(Btn(
            label=_fit_label(label, 10),
            data=f"/邮件 {letter['id']}",
            action="cmd",
            enter=True,                    # 仅单聊：点击直接发送（8983+）
            visited="已展开",
            id=f"mail_{i}",
        ))
    rows = None
    if btns:
        per_row = max(1, _buttons_max_per_row())
        rows = [btns[i:i + per_row] for i in range(0, len(btns), per_row)]
        problems = _validate_buttons(rows)
        if problems:
            logger.warning(
                f"[mail] envelope buttons invalid ({problems}); "
                f"sending markdown-only")
            rows = None

    header = (f"## 今日角色来信 · {date_label}\n"
              f"共 {len(letters)} 封，点击下方按钮展开：\n")
    # 空行隔开列表与引用块，防止 > 行被并进最后一个列表项
    footer = "\n\n> 过往来信：发送 `/邮件` 查看（保留最近 28 天）"
    body = []
    for i, letter in enumerate(letters):
        name = escape_md_text(letter.get("name") or "")
        preview = escape_md_text(
            letter.get("preview") or make_preview(letter.get("text", "")))
        marker = ""
        if i < len(shown) and len(letter.get("name") or "") > 4 \
                and i < len(_CIRCLED):
            marker = _CIRCLED[i]           # ①=显示名 映射（§6.2 长名）
        body.append(f"- **{name}**{marker}：{preview}")
    hidden = 0
    md = header + "\n" + "\n".join(body) + footer
    while len(md) > max_chars and len(body) > 1:
        body.pop()                         # 尾部 = 等级低/沉默短（已排序）
        hidden += 1
        note = f"\n\n另有 {hidden} 封，发送 /邮件 查看"
        md = header + "\n" + "\n".join(body) + note + footer

    plain_lines = [f"【今日角色来信 · {date_label}】", f"共 {len(letters)} 封："]
    for letter in letters:
        preview = letter.get("preview") or make_preview(letter.get("text", ""))
        plain_lines.append(f"{letter.get('name', '')}：{preview}")
    plain_lines += ["", "发送 /邮件 查看全文（保留最近 28 天）。"]
    return md, "\n".join(plain_lines), rows


async def _run_mail_pass(catch_up: bool = False) -> None:
    """每日信封 pass（§10 草案 + §5.0 并发/预算）。best-effort，绝不影响入站。"""
    cfg = _bond_manager._config
    if not bool(cfg.get("mail_enabled", True)):
        return
    bot = _get_qq_bot()
    if bot is None:
        return
    targets = greeting.load_targets(str(_GREET_TARGETS_FILE))
    if not targets:
        return
    deadline = time.monotonic() + float(cfg.get("mail_pass_budget_minutes", 20)) * 60
    max_letters = int(cfg.get("mail_max_letters", 5))
    if catch_up:
        max_letters = max(1, max_letters // 2)   # 补发减半（§4.1）
    retention = int(cfg.get("mail_retention_days", 28))

    # ── Phase A：收集候选（纯本地读，串行足够快）──
    todo = []
    for openid in targets:
        if time.monotonic() > deadline:
            logger.warning("[mail] pass budget exhausted during collect; "
                           "rest deferred")
            break
        try:
            mailbox = _mailbox_manager.load_user(openid)
            if not mailbox.get("active_enabled", True):
                continue                         # 用户关闭了主动消息
            if greeting.active_sent_today(str(_GREET_QUOTA_FILE), openid,
                                          kind="media"):
                continue                         # 双向互斥：当天已发媒体
            # sessions 索引补齐 known_personas（一旦写入，删会话也保留）；
            # 每用户锁：与 WebUI 管理队列 worker 串行同用户信箱 RMW（§16.7）
            async with _mail_lock(openid):
                _mailbox_manager.ensure_known_personas(
                    openid, _known_personas_from_sessions(openid))
            mailbox = _mailbox_manager.load_user(openid)
            candidates = _mail_candidates(openid, mailbox, cfg)
            if not candidates:
                continue
            candidates = candidates[:max_letters]
            if not _mail_due(openid, mailbox, candidates, cfg):
                continue
            todo.append((openid, candidates))
        except Exception as e:
            logger.warning(f"[mail] collect {openid[:8]}… failed: {e!r}")

    if not todo:
        return

    # ── Phase B：生成（用户间信号量并发，用户内串行）──
    sem = asyncio.Semaphore(
        max(1, int(cfg.get("mail_gen_concurrency", 4))))

    async def _gen(openid: str, candidates: list):
        async with sem:
            try:
                return openid, await _build_letters(openid, candidates, cfg,
                                                    deadline)
            except Exception as e:
                logger.warning(f"[mail] generate {openid[:8]}… failed: {e!r}")
                return openid, []

    results = await asyncio.gather(*[_gen(o, c) for o, c in todo])

    # ── Phase C：串行发送（令牌桶 + 错峰 + 预算）──
    limiter = TokenBucket(rate=float(cfg.get("mail_rate_c2c_per_sec", 10)))
    sent = 0
    for openid, letters in results:
        if not letters:
            continue
        if time.monotonic() > deadline:
            logger.warning("[mail] pass budget exhausted during send; "
                           "rest deferred")
            break
        try:
            md, plain, rows = _build_envelope(letters, cfg)
            await limiter.acquire()
            ok = await send_active_c2c_markdown(
                bot, openid, md, rows=rows, plain_text=plain)
            if not ok:
                continue                         # 发送失败不写任何记录（§7.2）
            async with _mail_lock(openid):
                digest_id = _mailbox_manager.record_digest(
                    openid, letters, retention_days=retention)
            if digest_id is None:
                continue                         # 并发 pass 已发过（幂等拦截）
            greeting.record_active_send(str(_GREET_QUOTA_FILE), openid,
                                        kind="text")
            for letter in letters:
                # §9.1：存信件**全文**（≤60 字），不存 preview——防人格半失忆
                _bond_manager.append_event(
                    openid, letter["persona"],
                    f"你托系统给用户寄出一封信：{letter['text']}")
            sent += 1
            logger.info(f"[mail] envelope ({len(letters)} letters) → "
                        f"{openid[:8]}…")
            await asyncio.sleep(random.uniform(2.0, 6.0))   # 错峰
        except Exception as e:
            logger.warning(f"[mail] send {openid[:8]}… failed: {e!r}")
    if sent:
        logger.info(f"[mail] pass: {sent} envelope(s) sent")


# ── WebUI 邮件管理队列（proactive-mail-design.md v0.5 §16.2/§16.4）──────
#
# 面板与 bot 是两个进程，管理操作（删除信件/发送系统邮件）走文件队列：
#   WebUI 写 data/mail_admin/queue/<uuid>.json
#   → bot os.replace 认领到 processing/
#   → 执行（type 白名单 bot 端复校验，§16.6）
#   → 写 status/<id>.json（done|failed）→ 删 processing 文件
# 信箱 bot 单写者：WebUI 对 mailboxes/*.json 只读。每用户 asyncio.Lock
# 串行「10:00 pass」与本 worker 的同用户信箱 RMW（mailbox.py 是同步模块，
# 锁只能挂在插件层，§16.7）。启动时把残留 processing 标 failed: bot
# restarted，并裁剪 7 天前的 status（W5）。

_MAIL_ADMIN_DIR = Path(__file__).resolve().parent.parent / "data" / "mail_admin"
_MAIL_QUEUE_DIR = _MAIL_ADMIN_DIR / "queue"
_MAIL_PROC_DIR = _MAIL_ADMIN_DIR / "processing"
_MAIL_STATUS_DIR = _MAIL_ADMIN_DIR / "status"
_MAIL_OP_TYPES = ("delete_letter", "send_system")   # v1 白名单；edit_letter 归 v2
_MAIL_STATUS_RETENTION = 7 * 86400                  # status 保留 7 天（W5）
_SYS_TITLE_MAX = 60                                 # §16.4.2 校验
_SYS_BODY_MAX = 2000

# 每用户信箱写锁（进程内）：_run_mail_pass 与管理 worker 共用
_mail_locks: dict[str, asyncio.Lock] = {}


def _mail_lock(openid: str) -> asyncio.Lock:
    lock = _mail_locks.get(openid)
    if lock is None:
        lock = _mail_locks[openid] = asyncio.Lock()
    return lock


def _mail_op_status_write(op_id: str, state: str, error: str = "") -> None:
    """status/<id>.json 原子写（bot 端唯一写者，WebUI 只读轮询）。"""
    try:
        _MAIL_STATUS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"id": op_id, "state": state, "error": error,
                   "finished_at": time.time()}
        tmp = _MAIL_STATUS_DIR / f"{_safe_filename(op_id)}.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, _MAIL_STATUS_DIR / f"{_safe_filename(op_id)}.json")
    except OSError as e:
        logger.warning(f"[mail-admin] status write failed: {e!r}")


def _safe_filename(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "_", str(name or "op"))[:80]


def _mail_admin_recover() -> None:
    """启动恢复（§16.4.3.6）：残留 processing → failed；status 剪 7 天。"""
    for d in (_MAIL_QUEUE_DIR, _MAIL_PROC_DIR, _MAIL_STATUS_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    for f in sorted(_MAIL_PROC_DIR.glob("*.json")):
        op_id = f.stem
        try:
            op = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(op, dict) and op.get("id"):
                op_id = str(op["id"])
        except (json.JSONDecodeError, OSError):
            pass
        _mail_op_status_write(op_id, "failed", "bot restarted（操作被中断）")
        try:
            f.unlink()
        except OSError:
            pass
    cutoff = time.time() - _MAIL_STATUS_RETENTION
    for f in _MAIL_STATUS_DIR.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


async def _mail_admin_exec(op: dict) -> tuple[str, str]:
    """执行一个已认领操作 → (state, error)。白名单外类型一律拒绝。"""
    op_type = op.get("type")
    openid = str(op.get("openid") or "").strip()
    if op_type not in _MAIL_OP_TYPES or not openid:
        return "failed", f"非法操作类型或缺少 openid（type={op_type!r}）"

    if op_type == "delete_letter":
        letter_id = str(op.get("letter_id") or "").strip()
        if not letter_id:
            return "failed", "缺少 letter_id"
        async with _mail_lock(openid):
            ok = _mailbox_manager.delete_letter(openid, letter_id)
        return ("done", "") if ok else ("failed", "信件不存在或已删除")

    # ── send_system：立即独立 c2c 主动消息（§16.4.4），不走 10:00 pass、
    #    不参与角色信件聚合、不写 bond 事件、不记每日配额（管理员显式操作）──
    title = str(op.get("title") or "").strip()
    body = str(op.get("body") or "").strip()
    fmt = str(op.get("format") or "text")
    if not title or not body:
        return "failed", "标题/正文为空"
    if len(title) > _SYS_TITLE_MAX:
        return "failed", f"标题超 {_SYS_TITLE_MAX} 字"
    if len(body) > _SYS_BODY_MAX:
        return "failed", f"正文超 {_SYS_BODY_MAX} 字"
    if fmt not in ("text", "markdown"):
        return "failed", "format 必须是 text 或 markdown"
    if not _mailbox_manager.load_user(openid).get("active_enabled", True):
        return "failed", "用户已关闭主动消息（拒收），系统邮件未发送"
    bot = _get_qq_bot()
    if bot is None:
        return "failed", "官方 bot 未连接"
    md = (f"## 系统邮件 · {escape_md_text(title)}\n"
          f"{body if fmt == 'markdown' else escape_md_text(body)}\n\n"
          f"> 发送 `/邮件` 查看信箱")
    plain = f"【系统邮件 · {title}】\n{body}\n\n发送 /邮件 查看信箱"
    ok = await send_active_c2c_markdown(bot, openid, md, plain_text=plain)
    if not ok:
        return "failed", "主动消息发送失败（配额/审核/网络，详见 bot 日志）"
    async with _mail_lock(openid):
        letter_id = _mailbox_manager.write_system_letter(
            openid, title, body,
            retention_days=int(
                _bond_manager._config.get("mail_retention_days", 28)))
    if letter_id is None:
        return "failed", "发送成功但信箱写入失败"
    return "done", ""


async def _mail_admin_worker() -> None:
    """3–5s 扫描 queue/ → 认领 → 执行 → status/（§16.2）。"""
    while True:
        await asyncio.sleep(3.0 + random.uniform(0, 2.0))
        try:
            files = sorted(_MAIL_QUEUE_DIR.glob("*.json"))
        except OSError:
            continue
        for qf in files:
            proc = _MAIL_PROC_DIR / qf.name
            try:
                os.replace(qf, proc)         # 认领；失败=已被认领/消失
            except OSError:
                continue
            try:
                op = json.loads(proc.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                _mail_op_status_write(qf.stem, "failed", f"队列文件损坏: {e!r}")
                proc.unlink(missing_ok=True)
                continue
            if not isinstance(op, dict):
                op = {}
            op_id = str(op.get("id") or qf.stem)
            try:
                state, error = await _mail_admin_exec(op)
            except Exception as e:               # 兜底：worker 永不因单操作死掉
                state, error = "failed", f"执行异常: {type(e).__name__}: {e}"
            _mail_op_status_write(op_id, state, error)
            proc.unlink(missing_ok=True)
            logger.info(f"[mail-admin] {op.get('type')} {op_id} → {state}"
                        + (f" ({error})" if error else ""))
            if op.get("type") == "send_system":
                await asyncio.sleep(1.0)   # 批量系统邮件限速，防 bot 维度频控


@get_driver().on_startup
async def _mail_admin_startup():
    # 与 mail_enabled 无关：管理队列是管理员显式操作，回滚开关只管每日 pass
    _mail_admin_recover()
    asyncio.create_task(_mail_admin_worker())


async def _run_greeting_pass() -> None:
    cfg = _bond_manager._config
    if not bool(cfg.get("greet_enabled", True)):
        return
    bot = _get_qq_bot()
    if bot is None:
        return
    entries = greeting.load_targets(str(_GREET_TARGETS_FILE))
    if not entries:
        return
    pm = get_character_registry()

    def state_for(openid: str):
        # 仅 split（养成）人格 + 已有羁绊记录的用户可被问候
        try:
            persona = pm.resolve_effective_personality(openid, None)
            if pm.get_mode(persona) != "split":
                return None
            return _bond_manager.get_state(openid, persona, create=False)
        except Exception:
            return None

    picked = greeting.pick_greet_targets(
        entries,
        state_for=state_for,
        min_level=int(cfg.get("greet_min_level", 5)),
        cooldown_days=float(cfg.get("greet_cooldown_days", 7)),
        probability=float(cfg.get("greet_probability", 0.25)),
        max_batch=int(cfg.get("greet_batch_max", 5)),
    )
    greeted = 0
    for openid, state in picked:
        if not greeting.active_allowed(
                str(_GREET_QUOTA_FILE), openid,
                daily_cap=int(cfg.get("greet_daily_cap", 1)),
                monthly_cap=int(cfg.get("greet_monthly_cap", 4))):
            continue
        persona = pm.resolve_effective_personality(openid, None)
        text = await _compose_greeting(persona, state)
        if not text:
            continue
        if await send_active_c2c(bot, openid, text):
            greeting.record_active_send(str(_GREET_QUOTA_FILE), openid)
            greeting.mark_greeted(str(_GREET_TARGETS_FILE), openid)
            greeted += 1
            logger.info(
                f"[qq_official] active greeting → {openid[:8]}… as {persona}")
        await asyncio.sleep(random.uniform(2.0, 6.0))  # 错峰，避免齐发
    if greeted:
        logger.info(f"[qq_official] greeting pass: {greeted} sent")


# ── P4: 主动发图/语音（greeting pass 之后，共享候选索引）─────────────
#
# 门控链：split 人格 + 羁绊状态 → 等级门（图 ≥ media_image_min_level，
# 语音 ≥ media_proactive_voice_min_level）→ 概率门 proactive_media_probability
# → 今天没收到过任何主动消息（防文本+媒体同日双发）→ active_allowed
# (kind="media")（日 ≤ proactive_media_daily_cap；月与文本共享平台配额）。
# 命中后生成一条媒体主动发送；任何环节失败 → 降级纯文本主动问候（不浪费
# 候选）。成功才记配额 + 写资产卡（proactive=True）+ mark_greeted 重置冷却。
# 全程 best-effort：任何异常只记日志，绝不影响入站消息处理。

_MEDIA_IMAGE_SCENES = (
    "给用户发一张日常自拍，对镜头温柔地笑",
    "穿常服在街头散步的日常画面，姿态自然放松",
    "在温馨的房间里做自己的事情，充满生活气息",
    "在户外柔和的光线下回头一笑",
)

# 台词审查被拒时的 TTS 安全兜底（_GREET_FALLBACKS 带「{name}：」前缀不适合朗读）
_MEDIA_VOICE_FALLBACKS = (
    "好久不见呀，最近过得怎么样？",
    "突然有点想你，忙也要记得休息哦。",
    "记得按时吃饭，好好睡觉哦。",
)


async def _run_media_pass() -> None:
    cfg = _bond_manager._config
    if not bool(cfg.get("media_enabled", True)):
        return
    bot = _get_qq_bot()
    if bot is None:
        return
    entries = greeting.load_targets(str(_GREET_TARGETS_FILE))
    if not entries:
        return
    pm = get_character_registry()
    img_min = int(cfg.get("media_image_min_level", 4))
    voice_min = int(cfg.get("media_proactive_voice_min_level", 7))
    prob = float(cfg.get("proactive_media_probability", 0.15))
    media_daily_cap = int(cfg.get("proactive_media_daily_cap", 1))
    monthly_cap = int(cfg.get("greet_monthly_cap", 4))
    max_batch = int(cfg.get("greet_batch_max", 5))
    ttl = float(cfg.get("media_dedupe_ttl_hours", 168))

    sent = 0
    for openid, _entry in entries.items():
        if sent >= max_batch:
            break
        try:
            persona = pm.resolve_effective_personality(openid, None)
            if pm.get_mode(persona) != "split":
                continue
            state = _bond_manager.get_state(openid, persona, create=False)
            if not isinstance(state, dict):
                continue
            level = int(state.get("level", 1))
            can_image = level >= img_min and media_gen.is_image_available()
            can_voice = level >= voice_min and media_gen.is_tts_available()
            if not (can_image or can_voice):
                continue
            if random.random() >= prob:
                continue
            # 今天已收到主动文本 → 不再叠加媒体（防同日双发打扰）
            if greeting.active_sent_today(str(_GREET_QUOTA_FILE), openid,
                                          kind="text"):
                continue
            if not greeting.active_allowed(
                    str(_GREET_QUOTA_FILE), openid,
                    daily_cap=media_daily_cap, monthly_cap=monthly_cap,
                    kind="media"):
                continue
            kind = "voice" if (can_voice and random.random() < 0.5) else (
                "image" if can_image else "voice")
            seg, card, phash = await _prepare_proactive_media(
                openid, persona, kind, state, pm, ttl)
            if seg is not None and await send_active_c2c_media(bot, openid, seg):
                greeting.record_active_send(str(_GREET_QUOTA_FILE), openid,
                                            kind="media")
                greeting.mark_greeted(str(_GREET_TARGETS_FILE), openid)
                if card is not None:
                    _asset_memory.write_card(card)
                    if phash:
                        _asset_memory.record_prompt_hash(phash, card)
                    # P5 LRU：主动生成也是资产写盘 → 超配额即淘汰低分二进制
                    try:
                        _role = _perm_manager.get_role(openid).value
                    except Exception:
                        _role = "regular"
                    enforce_asset_quota(user_id=openid, role=_role,
                                        bond_manager=_bond_manager,
                                        asset_memory=_asset_memory)
                _asset_memory.append_audit(
                    openid, persona, kind,
                    card.model if card else "", phash or "", "proactive_ok")
                sent += 1
                logger.info(
                    f"[qq_official] proactive {kind} → {openid[:8]}… "
                    f"as {persona}")
            else:
                if phash:
                    _asset_memory.append_audit(
                        openid, persona, kind, "", phash, "proactive_failed")
                # 降级：纯文本主动问候（text 配额与冷却独立结算）
                await _fallback_text_greeting(bot, openid, persona, state)
            await asyncio.sleep(random.uniform(2.0, 6.0))  # 错峰，避免齐发
        except Exception as e:
            logger.warning(
                f"[qq_official] media pass: {openid[:8]}… failed: {e!r}")
    if sent:
        logger.info(f"[qq_official] media pass: {sent} sent")


async def _prepare_proactive_media(openid: str, persona: str, kind: str,
                                   state: dict, pm, ttl: float):
    """Generate one proactive media payload.

    Returns ``(seg|None, card|None, phash)``。card=None 表示复用旧资产
    （去重命中，不建新卡）或生成失败（seg 也为 None）。图像 = Appearance
    Anchor 强制前缀 + 日常场景池（prompt_hash 去重防重复计费/重复画面）；
    语音 = flash in-character 台词 → TTS（与被动工具同一 voice_id 解析链
    = 声纹一致）。台词未过审查 → 换安全兜底句。
    """
    card_content = pm.load(persona) or ""
    display = pm.get_display_name(persona)
    if kind == "image":
        anchor = bond_content.parse_appearance_anchor(card_content)
        prompt = build_image_prompt(random.choice(_MEDIA_IMAGE_SCENES),
                                    anchor, display)
        phash = prompt_hash_of(prompt)
        hit = _asset_memory.find_by_prompt_hash(phash, ttl_hours=ttl)
        if hit is not None and hit.path:
            return MessageSegment.file_image(Path(hit.path)), None, phash
        asset_id = make_asset_id()
        out_path = _asset_memory.binary_path(openid, persona, asset_id, "png")
        try:
            reference = pm.reference_image_path(persona)
        except Exception:
            reference = None
        path = await media_gen.generate_image(
            prompt, out_path=out_path, reference_image_path=reference)
        if not path:
            return None, None, phash
        card = AssetCard(
            id=asset_id, type="image", char_id=persona, user_id=openid,
            path=path, model=media_gen.get_model_name("image"),
            prompt=prompt, tags=["generated", "proactive"],
            prompt_hash=phash, proactive=True,
        )
        return MessageSegment.file_image(Path(path)), card, phash

    # ── voice ──
    line = await _compose_greeting(persona, state)
    m = re.search(r"「(.+?)」", line or "")
    if m:  # 模板降级带「{name}：「…」」包装 → 取台词本体
        line = m.group(1)
    line = (line or "").strip()[:120] or random.choice(_MEDIA_VOICE_FALLBACKS)
    review_ok, _reason = await media_gen.review_prompt(
        line, _model_router.flash_client)
    if not review_ok:
        line = random.choice(_MEDIA_VOICE_FALLBACKS)
    phash = prompt_hash_of("proactive_voice:" + line)
    voice_id = ""
    try:
        voice_id = pm.get_voice_id(persona) or ""
    except Exception:
        voice_id = ""
    asset_id = make_asset_id()
    out_path = _asset_memory.binary_path(openid, persona, asset_id, "mp3")
    path = await media_gen.generate_speech(
        line, voice_id, out_path=out_path,
        max_chars=int(_bond_manager.get_config("media_tts_max_chars", 300)))
    if not path:
        return None, None, phash
    card = AssetCard(
        id=asset_id, type="voice", char_id=persona, user_id=openid,
        path=path, model=media_gen.get_model_name("voice"),
        transcript=line, prompt=line, tags=["generated", "proactive"],
        prompt_hash=phash, proactive=True,
    )
    return MessageSegment.file_audio(Path(path)), card, phash


async def _fallback_text_greeting(bot, openid: str, persona: str,
                                  state: dict) -> None:
    """媒体生成/发送失败 → 纯文本主动问候（text 配额独立，成功才记账）。"""
    cfg = _bond_manager._config
    if not bool(cfg.get("greet_enabled", True)):
        return
    if not greeting.active_allowed(
            str(_GREET_QUOTA_FILE), openid,
            daily_cap=int(cfg.get("greet_daily_cap", 1)),
            monthly_cap=int(cfg.get("greet_monthly_cap", 4)),
            kind="text"):
        return
    text = await _compose_greeting(persona, state)
    if text and await send_active_c2c(bot, openid, text):
        greeting.record_active_send(str(_GREET_QUOTA_FILE), openid,
                                    kind="text")
        greeting.mark_greeted(str(_GREET_TARGETS_FILE), openid)


async def _compose_greeting(persona_key: str, state: dict) -> str:
    """In-character low-frequency greeting (flash); template on failure."""
    pm = get_character_registry()
    short = pm.get_short_name(persona_key)
    try:
        card = pm.load(persona_key) or ""
        if card:
            gap = 0
            try:
                last_seen = float((state or {}).get("stats", {}).get("last_seen") or 0)
                gap = max(1, int((time.time() - last_seen) // 86400)) if last_seen else 0
            except (TypeError, ValueError):
                gap = 0
            level = int((state or {}).get("level", 1))
            system = (
                card
                + "\n\n---\n你正在扮演上述角色。你们已经有一段时间没聊天了，"
                  "你想主动发一条消息问候用户。保持角色口吻，自然、克制、不煽情，"
                  "像老朋友或亲近的人那样关心对方近况。"
                  "不要提及系统、平台、配额或本条指示本身。"
            )
            prompt = (
                f"距你们上次交谈约 {gap} 天（羁绊 Lv{level}）。"
                f"请写一句主动问候。要求：不超过50字；不使用markdown；"
                f"只输出这句问候本身。"
            )
            raw = await _model_router.flash_client.chat_completion(
                message=prompt,
                history=[{"role": "system", "content": system}],
                timeout_set=15.0,
                purpose="bond_greeting",
            )
            candidate = (raw or "").strip()
            if (
                candidate
                and not candidate.startswith(_LLM_ERROR_PREFIXES)
                and len(candidate) <= 120
                and "\n" not in candidate
            ):
                return candidate
    except Exception:
        pass
    return random.choice(_GREET_FALLBACKS).format(name=short)


@official_notice_router.handle()
async def handle_official_notice(bot: QQBot, event):
    """C2C 主动消息拒收/恢复事件 → 信箱 active_enabled 开关（§7.2）。

    C2CMsgReject/ReceiveEvent 是 NoticeEvent（不是 Message），需要独立
    matcher；bot 注解把 OneBot 事件挡在外面。GROUP_MSG_REJECT/RECEIVE
    与群发送一起归 P1，v1 不处理。
    """
    if isinstance(event, C2CMsgRejectEvent):
        openid = str(getattr(event, "openid", "") or "")
        if openid:
            _mailbox_manager.set_active_enabled(openid, False)
            logger.info(f"[mail] active messages rejected by {openid[:8]}…; "
                        f"mailbox disabled")
    elif isinstance(event, C2CMsgReceiveEvent):
        openid = str(getattr(event, "openid", "") or "")
        if openid:
            _mailbox_manager.set_active_enabled(openid, True)
            logger.info(f"[mail] active messages re-enabled by {openid[:8]}…")


@official_router.handle()
async def handle_official_message(bot: QQBot, event: QQMessageEvent):
    """One official c2c/group-@ message → one agent turn.

    The QQMessageEvent annotation keeps OneBot events out (dependency
    typing); the isinstance guard narrows to the two event types the
    intent config actually delivers.
    """
    if not isinstance(event, HANDLED_EVENTS):
        return
    _note_message()

    view = build_view(bot, event)
    scope = "group" if view.is_group else "c2c"
    if not view.is_group:
        # P3b: c2c user_openid is the only identity usable for active
        # c2c pushes → maintain the greeting candidate index from here
        # (group member_openid can NOT be messaged via send_to_c2c).
        greeting.note_c2c_contact(str(_GREET_TARGETS_FILE), view.user_id)
    else:
        # v1 群侧唯一动作：GROUP_AT_MESSAGE_CREATE 纯记账 upsert（§4.5，
        # 节流在 GroupRegistry 内部；平台无群列表 API，越早建档越好）。
        try:
            group_openid = str(getattr(event, "group_openid", "") or "")
            if group_openid:
                _group_registry.upsert_seen(group_openid)
        except Exception:
            pass  # 记账失败绝不影响消息处理
    logger.info(
        f"[qq_official] {scope} message: openid={view.user_id} "
        f"group={view.group_id or '-'} msg_id={view.message_id} "
        f"attachments={len(event.attachments or [])} "
        f"quoted={'yes' if getattr(event, 'reply', None) else 'no'} "
        f"text={view.text[:60]!r}"
    )

    sender = QQOfficialSender(bot, event, view.is_group)
    if not sender.wants_reply(view):
        return

    # Bind this event's sender for the whole turn: _safe_send resolves
    # explicit arg → _turn_sender → default_sender, so every kernel path
    # (_send_response, cards, busy notice, tools via _ToolSender) lands
    # in the buffer without turn.py knowing the platform.
    token = _turn_sender.set(sender)
    try:
        await run_agent_turn(view)
    finally:
        _turn_sender.reset(token)
        # Best-effort: buffered output goes out even if the turn raised.
        await sender.flush()
