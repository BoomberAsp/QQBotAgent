"""
Agent Router — OneBot V11 (NapCat) entry point for the QQBot agent.

Stage-1 of the QQ-official worktree plan: all platform-independent turn
logic, tool registration and module-level singletons now live in
agent/turn.py (moved verbatim). This plugin keeps only OneBot glue:

  - .env bootstrap (agent/turn.py repeats it idempotently)
  - driver on_startup hooks: config hot-reload watcher, changelog
    broadcast / group-list export (both need OneBot bot APIs)
  - on_message / on_notice matchers with their platform-specific guards
    (@-mention detection, continuous-mode window, poke filtering),
    converting events to agent.inbound views via adapters.onebot
  - sender-slot registration into agent.turn (bottom of this file)
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
from adapters.onebot import OneBotSender, build_view
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

    # 官方群登记表（§4.5/§16.5）：与 plugins/qq_official.py 指向同一文件；
    # 那边只 upsert_seen（入站记账），这边只读 + 群发时 mark_left，
    # 原子写保证不损坏，低频写入下 lost-update 风险可忽略。
    from adapters.qq_official import send_active_group
    from agent.group_registry import GroupRegistry
    _official_registry = GroupRegistry(
        str(Path(__file__).resolve().parent.parent / "data" / "group_registry.json"))

    def _get_qq_bot():
        import nonebot
        from nonebot.adapters.qq import Bot as _QQBot
        return next((b for b in nonebot.get_bots().values()
                     if isinstance(b, _QQBot)), None)

    async def _group_exporter():
        # 启动后稍等 bot 上线，再首次导出；之后每 10 分钟刷新。
        # 合并导出（§16.5.4）：OneBot 群走 get_group_list()（拉取失败时保留
        # 上次快照）；官方群读 group_registry.json —— 没有 OneBot bot 也要写。
        await asyncio.sleep(15)
        while True:
            try:
                cur = _cl.read_known_groups()
                if not isinstance(cur, dict):
                    cur = {}
                groups = cur.get("groups") if isinstance(cur.get("groups"), dict) else {}
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
                official = {}
                for g_openid, e in _official_registry.load().get("groups", {}).items():
                    if isinstance(e, dict):
                        official[g_openid] = {
                            "status": e.get("status") or "unknown",
                            "active_enabled": bool(e.get("active_enabled", True)),
                            "last_seen_at": float(e.get("last_seen_at") or 0),
                        }
                _cl.write_known_groups(groups, official_groups=official)
            except Exception as e:
                nonebot_logger.warning(f"[changelog] 群列表导出异常: {e}")
            await asyncio.sleep(600)

    async def _do_broadcast(payload):
        """执行一次群发（§16.5.3）。

        payload 新格式 ``{kind, ref, targets}``；旧格式（裸 entry_index +
        字符串 targets）按 changelog/onebot 兼容解释。
        kind=changelog：更新记录纯文本（W3：官方渠道也不上 markdown）+ 卡片图；
        kind=announcement：公告，format=markdown 时官方渠道 msg_type=2、
        OneBot 回退纯文本。官方群发送前实时校验 group_registry，
        40034101/40034105 → mark_left。
        """
        # ── kind/ref 解析 ──
        kind = payload.get("kind") or "changelog"
        ref = payload.get("ref") if isinstance(payload.get("ref"), dict) else {}
        if kind == "changelog" and "entry_index" not in ref:
            ref = {"entry_index": payload.get("entry_index", 0)}

        entry = ann = None
        text = md_text = None
        if kind == "announcement":
            ann = _cl.get_announcement(str(ref.get("announcement_id") or ""))
            if ann is None:
                _cl.write_status({"state": "error", "kind": kind, "ref": ref,
                                  "error": "公告不存在或已被删除",
                                  "sent": 0, "failed": 0})
                return
            text = _cl.format_announcement_text(ann)
            if str(ann.get("format") or "text") == "markdown":
                # 官方渠道 msg_type=2（拒绝时适配器内降级 text）；OneBot 用 text
                md_text = f"## 📢 {ann.get('title') or '公告'}\n{ann.get('body') or ''}"
        else:
            kind = "changelog"
            entries = _cl.load_entries()  # 文件顺序（面板新→旧），entry_index 对齐
            idx = ref.get("entry_index", 0)
            if not isinstance(idx, int) or idx < 0 or idx >= len(entries):
                _cl.write_status({"state": "error", "kind": kind, "ref": ref,
                                  "error": "entry_index 越界",
                                  "sent": 0, "failed": 0})
                return
            entry = entries[idx]
            text = "\n".join(
                _cl.format_announcement(entry, site_url=_PUBLIC_SITE_URL))

        # ── targets 归一化：{channel,id} 对象 / 裸字符串(=onebot) / "all" ──
        raw_targets = payload.get("targets") or []
        if not isinstance(raw_targets, list) or not raw_targets:
            _cl.write_status({"state": "error", "kind": kind, "ref": ref,
                              "error": "未选择目标群", "sent": 0, "failed": 0})
            return
        expand_all = any(t == "all" for t in raw_targets)
        onebot_ids: list = []
        official_ids: list = []
        for t in raw_targets:
            if t == "all":
                continue
            if isinstance(t, dict):
                ch = str(t.get("channel") or "onebot")
                tid = str(t.get("id") or "").strip()
            else:
                ch, tid = "onebot", str(t).strip()   # 旧格式：裸字符串=OneBot
            if not tid:
                continue
            (onebot_ids if ch == "onebot" else official_ids).append(tid)

        ob_bot = _get_bot() if (expand_all or onebot_ids) else None
        qq_bot = _get_qq_bot() if (expand_all or official_ids) else None
        if expand_all:
            gl = await _fetch_group_list(ob_bot) if ob_bot is not None else None
            if gl is not None:
                onebot_ids += [str(g.get("group_id")) for g in gl]
            elif isinstance(cur_groups := (_cl.read_known_groups() or {}).get("groups"), dict):
                onebot_ids += list(cur_groups.keys())   # 兜底：上次导出快照
            official_ids += _official_registry.list_active()
        onebot_ids = list(dict.fromkeys(onebot_ids))
        official_ids = list(dict.fromkeys(official_ids))
        total = len(onebot_ids) + len(official_ids)
        if not total:
            _cl.write_status({"state": "error", "kind": kind, "ref": ref,
                              "error": "目标群为空", "sent": 0, "failed": 0})
            return
        if onebot_ids and ob_bot is None and not official_ids:
            _cl.write_status({"state": "error", "kind": kind, "ref": ref,
                              "error": "bot 未连接", "sent": 0, "failed": 0})
            return
        if official_ids and qq_bot is None and not onebot_ids:
            _cl.write_status({"state": "error", "kind": kind, "ref": ref,
                              "error": "官方 bot 未连接", "sent": 0, "failed": 0})
            return

        by_channel = {"onebot": {"sent": 0, "failed": 0, "skipped": 0},
                      "official": {"sent": 0, "failed": 0, "skipped": 0}}
        _cl.write_status({"state": "sending", "kind": kind, "ref": ref,
                          "total": total, "sent": 0, "failed": 0, "skipped": 0,
                          "by_channel": by_channel})
        sent = failed = skipped = 0

        # ── OneBot 渠道：300 字分块 + 卡片图（仅 changelog）+ 1s 限速 ──
        if onebot_ids:
            chunks = _split_text(text, 300) or [text]
            from nonebot.adapters.onebot.v11 import MessageSegment
            card_path = None
            if entry is not None:
                # 卡片在循环外渲染一次（best-effort）；失败则仅发文本
                try:
                    card_path = await asyncio.to_thread(
                        render_changelog_card, [entry], title="更新公告",
                        site_url=_PUBLIC_SITE_URL)
                except Exception:
                    card_path = None
            for gid in onebot_ids:
                if ob_bot is None:
                    failed += 1
                    by_channel["onebot"]["failed"] += 1
                    nonebot_logger.warning(
                        f"[changelog] OneBot bot 未连接，跳过群 {gid}")
                    continue
                # 先发卡片图（best-effort；失败降级为仅文本，不计入 failed）
                if card_path:
                    try:
                        await ob_bot.send_group_msg(
                            group_id=int(gid),
                            message=MessageSegment.image(Path(card_path)))
                        await asyncio.sleep(1.0)
                    except Exception as e:
                        nonebot_logger.warning(
                            f"[changelog] 群发卡片到 {gid} 失败（降级文本）: {e}")
                ok = True
                for j, chunk in enumerate(chunks):
                    try:
                        await ob_bot.send_group_msg(group_id=int(gid),
                                                    message=chunk)
                    except Exception as e:
                        ok = False
                        nonebot_logger.warning(
                            f"[changelog] 群发到 {gid} 失败: {e}")
                        break
                    if j < len(chunks) - 1:
                        await asyncio.sleep(1.0)  # 段间限速
                if ok:
                    sent += 1
                    by_channel["onebot"]["sent"] += 1
                else:
                    failed += 1
                    by_channel["onebot"]["failed"] += 1
                await asyncio.sleep(1.0)  # 群间限速

        # ── 官方渠道：markdown 单条（md_text 时长文由面板 2000 字上限约束）；
        #    纯文本按 800 字分块；1 条/秒（Bot 维度 60 qpm 保守值，§16.5.3.2）──
        if official_ids:
            of_chunks = _split_text(text, 800) or [text]
            for g_openid in official_ids:
                if qq_bot is None:
                    failed += 1
                    by_channel["official"]["failed"] += 1
                    nonebot_logger.warning(
                        f"[changelog] 官方 bot 未连接，跳过群 {g_openid[:8]}…")
                    continue
                reg = _official_registry.get(g_openid)
                if (reg is None or reg.get("status") != "active"
                        or not reg.get("active_enabled", True)):
                    # 关闭通知/已退群/未登记的群：跳过不试发（§16.5.3.3）
                    skipped += 1
                    by_channel["official"]["skipped"] += 1
                    continue
                if md_text:
                    res = await send_active_group(qq_bot, g_openid, text,
                                                  md_text=md_text)
                else:
                    res = "ok"
                    for j, chunk in enumerate(of_chunks):
                        res = await send_active_group(qq_bot, g_openid, chunk)
                        if res != "ok":
                            break
                        if j < len(of_chunks) - 1:
                            await asyncio.sleep(1.0)
                if res == "ok":
                    sent += 1
                    by_channel["official"]["sent"] += 1
                else:
                    if res == "left":
                        # GROUP_DEL_ROBOT 可能丢失：发送侧发现即标 left（§4.5）
                        _official_registry.mark_left(g_openid)
                        nonebot_logger.warning(
                            f"[changelog] 官方群 {g_openid[:8]}… 无权限/不存在，"
                            f"已标记 left")
                    failed += 1
                    by_channel["official"]["failed"] += 1
                await asyncio.sleep(1.0)  # 群间限速

        # 仅当至少成功发出一个群时才回写已群发标记，避免"全部失败仍标记"
        all_failed = sent == 0
        if not all_failed:
            if kind == "announcement":
                _cl.mark_announcement_sent(
                    str(ann.get("id")),
                    ([{"channel": "onebot", "id": g} for g in onebot_ids]
                     + [{"channel": "official", "id": g} for g in official_ids]))
            else:
                _cl.mark_broadcast(entry.get("created_at"))
        status = {
            "state": "error" if all_failed else "done",
            "kind": kind, "ref": ref,
            "total": total,
            "sent": sent, "failed": failed, "skipped": skipped,
            "by_channel": by_channel,
            "finished_at": time.time(),
        }
        if entry is not None:
            status["entry_version"] = entry.get("version", "")
            status["entry_date"] = entry.get("date", "")
        if ann is not None:
            status["announcement_title"] = ann.get("title", "")
        if all_failed:
            status["error"] = ("所有目标群发送失败" if failed
                               else "所有目标群被跳过（关闭通知/不可用）")
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
    # Manual @mention check for group messages (more reliable than to_me()).
    # Kept on the raw event ON PURPOSE: build_view() computes the reply
    # context (with [REPLY_DIAG] stderr output), which must not run for
    # messages not directed at the bot.
    if isinstance(event, GroupMessageEvent):
        bot_qq = str(event.self_id)
        is_at_bot = any(
            seg.type == "at" and seg.data.get("qq") == bot_qq
            for seg in event.message
        )
        if not is_at_bot and not event.is_tome():
            return  # Not directed at bot, skip silently

    await run_agent_turn(build_view(bot, event))


# ── Continuous Mode Handler ───────────────────────────────────────

# Catch messages from users in continuous mode (no @mention needed)
continuous_router = on_message(priority=2, block=False)


@continuous_router.handle()
async def handle_continuous_message(bot: Bot, event: MessageEvent):
    """Route messages from group users in continuous mode to the Agent."""
    # Only applies to group chats
    if not isinstance(event, GroupMessageEvent):
        return

    # Skip messages that @ the bot — agent_router (priority=1) already handled them
    if event.is_tome():
        return
    bot_qq = str(event.self_id)
    if any(seg.type == "at" and seg.data.get("qq") == bot_qq for seg in event.message):
        return  # agent_router will handle this (manual @mention detection)

    await run_continuous_turn(build_view(bot, event))


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


# ── Sender-slot registration (stage-3 seam) ───────────────────────
# agent/turn.py sends outbound messages through agent.outbound.Sender
# implementations; OneBotSender wraps each NoneBot matcher (_safe_send
# default + the continuous/poke paths). Assigning them here keeps
# turn.py free of plugin imports — no circular dependency.
turn.default_sender = OneBotSender(agent_router)
turn.continuous_sender = OneBotSender(continuous_router)
turn.poke_sender = OneBotSender(poke_router)
