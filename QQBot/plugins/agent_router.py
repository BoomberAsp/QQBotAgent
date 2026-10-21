"""
Agent Router — OneBot V11 (NapCat) entry point for the QQBot agent.

Stage-1 of the QQ-official worktree plan: all platform-independent turn
logic, tool registration and module-level singletons now live in
agent/turn.py (moved verbatim). This plugin keeps only OneBot glue:

  - .env bootstrap (agent/turn.py repeats it idempotently)
  - driver on_startup hooks: config hot-reload watcher, changelog
    broadcast / group-list export (both need OneBot bot APIs)
  - on_message / on_notice matchers with their platform-specific guards
    (@-mention detection, continuous-mode window, poke filtering)
  - matcher-slot registration into agent.turn (bottom of this file)
"""

# ── Load .env into os.environ BEFORE any module-level reads ──────
# NoneBot2 (nb run) loads .env into its own pydantic config only, NOT
# into os.environ. Downstream module-level reads of os.environ (e.g.
# USER_DATA_ROOT, quota, session limits) would silently get defaults.
# This load_dotenv call must be the very first thing in this module.
from pathlib import Path
from dotenv import load_dotenv as _load_dotenv
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_load_dotenv(_ENV_FILE)

import asyncio
import os
import time

from nonebot import on_message, on_notice, get_driver
from nonebot import logger as nonebot_logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent

from agent import turn
from agent.turn import (
    # singletons & constants referenced by the on_startup hooks
    agent, _model_router, _profile_manager, _special_sessions,
    _CONFIG_DIR, _PUBLIC_SITE_URL, _split_text,
    # turn entry points (busy-guarded)
    run_agent_turn, run_continuous_turn, _handle_poke,
)
from lib.errors import log_exception
from tools.card_renderer import render_changelog_card


# ── Config hot-reload watcher ──────────────────────────────────────
# 每 5 秒检查 agent/config/ 下所有 *.md（含 personalities/）的 mtime，
# 有变化则调用 agent.reload_configs()。配合 WebUI 面板的配置热编辑：
# 面板保存提示词后 5 秒内生效，无需重启机器人。
# 带 5 秒冷却，防止连续写入触发抖动重载。
# 同时监听别名字典（character_dic.json / bonds_search_dic.json）的 mtime：
# WebUI 面板「别名管理」保存后（面板已同步重建拼音索引文件），这里让
# name_resolver 单例重读字典 + 索引，并失效角色/羁绊详情缓存，
# 使别名改动 5~10 秒内在 bot 侧生效、无需重启。
# 还监听 models_settings.json：面板「模型配置」保存（已先通过 API 可用性
# 验证）后，这里 reload 两个 ModelRouter 单例 + MultimodalClient 单例，
# 并重挂启动时固化的 3 处 client 引用（agent/_profile_manager/_special_sessions）。
@get_driver().on_startup
async def _start_config_watcher():
    from tools import name_resolver as _nr

    _alias_files = [_nr._CHAR_DIC_PATH, _nr._BOND_DIC_PATH]
    _models_file = _model_router._config_path

    async def _watch():
        last_mtimes: dict = {}
        last_reload = 0.0
        last_alias_mtimes: dict = {}
        last_alias_reload = 0.0
        last_models_mtime = 0.0
        last_models_reload = 0.0
        while True:
            await asyncio.sleep(5)
            try:
                mtimes = {}
                for root, _dirs, files in os.walk(_CONFIG_DIR):
                    for fn in files:
                        if fn.endswith(".md"):
                            p = os.path.join(root, fn)
                            try:
                                mtimes[p] = os.path.getmtime(p)
                            except OSError:
                                pass
                if last_mtimes and mtimes != last_mtimes \
                        and time.time() - last_reload > 5:
                    agent.reload_configs()
                    last_reload = time.time()
                    nonebot_logger.info("检测到配置文件变化，已热重载系统提示词")
                last_mtimes = mtimes
            except Exception:
                pass
            try:
                alias_mtimes = {}
                for p in _alias_files:
                    try:
                        alias_mtimes[p] = os.path.getmtime(p)
                    except OSError:
                        pass
                if last_alias_mtimes and alias_mtimes != last_alias_mtimes \
                        and time.time() - last_alias_reload > 5:
                    _nr.get_resolver().reload()
                    from tools import character_detail as _cd
                    from tools import bond_detail as _bd
                    _cd._invalidate()
                    _bd._invalidate()
                    last_alias_reload = time.time()
                    nonebot_logger.info("检测到别名字典变化，已热重载别名索引与详情缓存")
                last_alias_mtimes = alias_mtimes
            except Exception:
                pass
            try:
                try:
                    models_mtime = os.path.getmtime(_models_file)
                except OSError:
                    models_mtime = 0.0
                if last_models_mtime and models_mtime != last_models_mtime \
                        and time.time() - last_models_reload > 5:
                    _model_router.reload()
                    # 模块级单例（tools 里 lazy import 的 model_router /
                    # multimodal_client 与 _model_router 是不同实例）
                    from lib.model_router import model_router as _gmr
                    from lib.multimodal_client import multimodal_client as _gmc
                    _gmr.reload()
                    if _gmc is not None:
                        _gmc.reload()
                    # 重挂启动时固化的 client 引用
                    agent.client = _model_router.reasoning_client
                    _profile_manager.set_client(_model_router.flash_client)
                    _special_sessions.set_client(_model_router.reasoning_client)
                    last_models_reload = time.time()
                    nonebot_logger.info("检测到 models_settings.json 变化，已热重载模型配置")
                last_models_mtime = models_mtime
            except Exception:
                pass

    asyncio.create_task(_watch())


@get_driver().on_startup
async def _start_changelog_tasks():
    """更新日志后台任务。

    WebUI 是独立进程，无法访问 bot 对象，因此群发通过 **文件队列** 解耦：
    面板写 pending_broadcast.json，本任务认领后用 bot.send_group_msg 发送，
    并把结果回写 broadcast_status.json 供面板轮询显示。同时定期导出群列表
    （群号+群名）到 known_groups.json，供面板的群发勾选框使用。
    """
    from tools import changelog as _cl

    def _get_bot():
        import nonebot
        from nonebot.adapters.onebot.v11 import Bot as _Bot
        bots = nonebot.get_bots()
        return next((b for b in bots.values() if isinstance(b, _Bot)), None)

    async def _fetch_group_list(bot):
        try:
            return await bot.get_group_list()
        except Exception as e:
            nonebot_logger.warning(f"[changelog] get_group_list 失败: {e}")
            return None

    async def _group_exporter():
        # 启动后稍等 bot 上线，再首次导出；之后每 10 分钟刷新
        await asyncio.sleep(15)
        while True:
            try:
                bot = _get_bot()
                if bot is not None:
                    gl = await _fetch_group_list(bot)
                    if gl is not None:
                        groups = {}
                        for g in gl:
                            gid = str(g.get("group_id"))
                            groups[gid] = {
                                "name": g.get("group_name") or "",
                                "member_count": g.get("member_count", 0),
                            }
                        _cl.write_known_groups(groups)
            except Exception as e:
                nonebot_logger.warning(f"[changelog] 群列表导出异常: {e}")
            await asyncio.sleep(600)

    async def _do_broadcast(payload):
        bot = _get_bot()
        if bot is None:
            _cl.write_status({"state": "error", "error": "bot 未连接",
                              "sent": 0, "failed": 0})
            return

        entries = _cl.load_entries()  # 文件顺序（面板新→旧），entry_index 与之对齐
        idx = payload.get("entry_index", 0)
        if not isinstance(idx, int) or idx < 0 or idx >= len(entries):
            _cl.write_status({"state": "error", "error": "entry_index 越界",
                              "sent": 0, "failed": 0})
            return
        entry = entries[idx]
        text = "\n".join(_cl.format_announcement(entry, site_url=_PUBLIC_SITE_URL))

        targets = payload.get("targets") or []
        if not isinstance(targets, list) or not targets:
            _cl.write_status({"state": "error", "error": "未选择目标群",
                              "sent": 0, "failed": 0})
            return
        if "all" in targets:
            gl = await _fetch_group_list(bot)
            if gl is None:
                _cl.write_status({"state": "error", "error": "获取群列表失败",
                                  "sent": 0, "failed": 0})
                return
            group_ids = [str(g.get("group_id")) for g in gl]
        else:
            group_ids = [str(t) for t in targets]

        if not group_ids:
            _cl.write_status({"state": "error", "error": "目标群为空",
                              "sent": 0, "failed": 0})
            return

        _cl.write_status({"state": "sending", "total": len(group_ids),
                          "sent": 0, "failed": 0})
        chunks = _split_text(text, 300) or [text]

        # 卡片在循环外渲染一次（best-effort）；渲染失败则仅发文本
        from nonebot.adapters.onebot.v11 import MessageSegment
        card_path = None
        try:
            card_path = await asyncio.to_thread(
                render_changelog_card, [entry], title="更新公告",
                site_url=_PUBLIC_SITE_URL)
        except Exception:
            card_path = None

        sent = failed = 0
        for gid in group_ids:
            # 先发卡片图（best-effort；失败降级为仅文本，不计入 failed）
            if card_path:
                try:
                    await bot.send_group_msg(
                        group_id=int(gid),
                        message=MessageSegment.image(Path(card_path)))
                    await asyncio.sleep(1.0)
                except Exception as e:
                    nonebot_logger.warning(
                        f"[changelog] 群发卡片到 {gid} 失败（降级文本）: {e}")
            ok = True
            for j, chunk in enumerate(chunks):
                try:
                    await bot.send_group_msg(group_id=int(gid), message=chunk)
                except Exception as e:
                    ok = False
                    nonebot_logger.warning(f"[changelog] 群发到 {gid} 失败: {e}")
                    break
                if j < len(chunks) - 1:
                    await asyncio.sleep(1.0)  # 段间限速
            if ok:
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(1.0)  # 群间限速

        # 仅当至少成功发出一个群时才回写 broadcast_at，避免"全部失败仍标记已群发"
        all_failed = sent == 0
        if not all_failed:
            _cl.mark_broadcast(entry.get("created_at"))
        status = {
            "state": "error" if all_failed else "done",
            "total": len(group_ids),
            "sent": sent, "failed": failed,
            "entry_version": entry.get("version", ""),
            "entry_date": entry.get("date", ""),
            "finished_at": time.time(),
        }
        if all_failed:
            status["error"] = "所有目标群发送失败"
        _cl.write_status(status)

    async def _broadcast_poller():
        # 启动恢复：清理上次进程崩溃残留的认领文件，并把可能卡住的 "sending"
        # 状态改写为中断，避免面板无限轮询"发送中…"。
        if _cl.recover_stale_claim():
            st = _cl.read_status()
            if isinstance(st, dict) and st.get("state") == "sending":
                _cl.write_status({
                    "state": "error",
                    "error": "上次群发在发送途中被中断（bot 重启），请重新发起",
                    "sent": st.get("sent", 0), "failed": st.get("failed", 0),
                })
            nonebot_logger.warning("[changelog] 已清理残留的群发认领文件")
        while True:
            await asyncio.sleep(5)
            try:
                payload = _cl.claim_pending()
                if payload is None:
                    continue
                try:
                    await _do_broadcast(payload)
                finally:
                    _cl.finish_pending_claim()
            except Exception as e:
                nonebot_logger.warning(f"[changelog] 群发轮询异常: {e}")

    asyncio.create_task(_group_exporter())
    asyncio.create_task(_broadcast_poller())


# ── Message Handlers ─────────────────────────────────────────────

# Catch ALL messages. For group messages, we manually check for @mentions
# instead of relying on to_me() rule, which depends on NapCat setting
# the "to_me" field in the raw event data (which may not always happen).
agent_router = on_message(priority=1, block=False)


@agent_router.handle()
async def handle_agent_message(bot: Bot, event: MessageEvent):
    """Route incoming QQ messages through the Agent."""
    user_id = str(event.user_id)

    # Manual @mention check for group messages (more reliable than to_me())
    if isinstance(event, GroupMessageEvent):
        bot_qq = str(event.self_id)
        is_at_bot = any(
            seg.type == "at" and seg.data.get("qq") == bot_qq
            for seg in event.message
        )
        if not is_at_bot and not event.is_tome():
            return  # Not directed at bot, skip silently

    await run_agent_turn(bot, event, user_id)


# ── Continuous Mode Handler ───────────────────────────────────────

# Catch messages from users in continuous mode (no @mention needed)
continuous_router = on_message(priority=2, block=False)


@continuous_router.handle()
async def handle_continuous_message(bot: Bot, event: MessageEvent):
    """Route messages from group users in continuous mode to the Agent."""
    # Only applies to group chats
    if not isinstance(event, GroupMessageEvent):
        return

    user_id = str(event.user_id)
    group_id = str(event.group_id)

    # Skip messages that @ the bot — agent_router (priority=1) already handled them
    if event.is_tome():
        return
    bot_qq = str(event.self_id)
    if any(seg.type == "at" and seg.data.get("qq") == bot_qq for seg in event.message):
        return  # agent_router will handle this (manual @mention detection)

    await run_continuous_turn(bot, event, user_id, group_id)


# ── P2: 戳一戳互动（Bond-System-Design.md §4.4 / §12）─────────────────
poke_router = on_notice(priority=5, block=False)


@poke_router.handle()
async def handle_poke(bot: Bot, event):
    """QQ「戳一戳」→ split 人格结算好感 + 角色口吻摸头反应。

    仅处理戳bot本体的 poke 通知；增益功能，全程静默容错，绝不抛出中断。
    """
    try:
        from nonebot.adapters.onebot.v11 import PokeNotifyEvent
    except Exception:
        return
    if not isinstance(event, PokeNotifyEvent):
        return
    if getattr(event, "sub_type", "") != "poke":
        return
    # 只响应「戳了bot自己」；戳别人不触发
    if event.target_id != event.self_id:
        return
    try:
        user_id = str(event.user_id)
        group_id = str(event.group_id) if getattr(event, "group_id", None) else ""
        await _handle_poke(user_id, group_id, private=not group_id)
    except Exception as e:
        log_exception("poke handling failed", e)


# ── Matcher-slot registration (stage-1 seam) ──────────────────────
# agent/turn.py sends outbound messages through these matchers
# (_safe_send default + the continuous/poke paths). Assigning them
# here keeps turn.py free of plugin imports — no circular dependency.
turn.default_matcher = agent_router
turn.continuous_matcher = continuous_router
turn.poke_matcher = poke_router
