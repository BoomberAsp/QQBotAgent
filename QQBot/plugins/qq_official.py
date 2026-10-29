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
  - no poke router and no changelog-broadcast / group-list-export
    startup tasks: no official API equivalents;
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
import random
import re
import time
from pathlib import Path

from nonebot import get_bots, get_driver, logger, on_message
from nonebot.adapters.qq import Bot as QQBot, MessageSegment
from nonebot.adapters.qq.event import QQMessageEvent

from adapters.qq_official import (
    HANDLED_EVENTS,
    QQOfficialSender,
    build_view,
    kb_snapshot,
    send_active_c2c,
    send_active_c2c_media,
)
from adapters.qq_official_buttons import click_snapshot
from adapters.qq_official_stream import stream_snapshot
from agent import bond_content, greeting
from agent.asset_memory import AssetCard, make_asset_id, prompt_hash_of
from agent.character_registry import get_character_registry
from agent.context import _turn_sender
from agent.delegation import _LLM_ERROR_PREFIXES
from agent.turn import (
    _asset_memory, _bond_manager, _model_router, _perm_manager, run_agent_turn,
)
from lib import media_gen
from lib.status_heartbeat import read_status, write_status
from tools.social_tools import build_image_prompt, enforce_asset_quota

official_router = on_message(priority=1, block=False)


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

# LLM 文案失败时的模板降级（{name} = 角色短名）
_GREET_FALLBACKS = (
    "{name}：「好久不见～最近过得怎么样呀？」",
    "{name}：「在忙吗？突然有点想找你聊聊天。」",
    "{name}：「今天也要好好吃饭、好好休息哦～」",
)


@get_driver().on_startup
async def _greeting_startup():
    asyncio.create_task(_greeting_loop())


async def _greeting_loop():
    """Periodic greeting pass: interval + jitter, config re-read each round."""
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
