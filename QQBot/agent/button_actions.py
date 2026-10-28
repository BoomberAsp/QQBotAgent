"""
Callback-button action registry (buttons-design §6.6, P1).

Every verb here is a MILLISECOND-scale local state change (delete with
modal pre-confirm, page flip, group toggle) — anything that would need
an LLM round-trip is a command button (type=2) instead and never lands
in this module.

Contract:
  - handlers receive a ButtonContext (identity already resolved from the
    EVENT by plugins/qq_official_buttons.py — never from the payload)
    and return an ActionResult;
  - dispatch() enforces scene → bot-role re-check (defense in depth on
    top of the wire-layer Permission) → a hard 2.5s budget (the platform
    ack window is ~3s; a late handler is worse than a failed one);
  - turn.py's managers are imported lazily INSIDE handlers: turn imports
    session_render (Phase 4) and this module must never create an import
    cycle at module load.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Coroutine, Optional

from nonebot import logger


@dataclass
class ButtonContext:
    user_id: str            # c2c: user_openid; group: group_member_openid
    group_id: str           # group_openid ("" in c2c) — from the EVENT only
    scene: str              # "c2c" | "group"
    verb: str               # decoded, e.g. "session.del"
    args: list              # decoded payload args (≤3, ascii-validated)
    interaction_id: str = ""


@dataclass
class ActionResult:
    code: int               # put_interaction code 0..5 (0 ok / 1 fail /
                            # 2 rate / 3 dup / 5 no-permission)
    message: str = ""       # plain-text receipt
    markdown: str = ""      # md receipt body (used when rows are present,
                            # or sent as msg_type=2 when markdown is available)
    rows: Optional[list] = None   # refreshed keyboard rows (lib.button_spec.Rows)


@dataclass
class ButtonAction:
    verb: str
    handler: Callable[[ButtonContext], Coroutine]
    role: str = "regular"                       # "regular" | "admin" minimum
    scenes: tuple = ("c2c", "group")            # allowed scenes


_ACTIONS: dict[str, ButtonAction] = {}


def _register(verb: str, *, role: str = "regular",
              scenes: tuple = ("c2c", "group")):
    def deco(fn):
        _ACTIONS[verb] = ButtonAction(verb=verb, handler=fn, role=role,
                                      scenes=scenes)
        return fn
    return deco


def known_verbs() -> tuple:
    return tuple(sorted(_ACTIONS))


async def dispatch(ctx: ButtonContext) -> ActionResult:
    """Scene check → role re-check → execute under a 2.5s budget."""
    action = _ACTIONS.get(ctx.verb)
    if action is None:
        return ActionResult(code=1, message="未知的按钮动作。")
    if ctx.scene not in action.scenes:
        return ActionResult(code=1, message="该按钮不适用于当前场景。")
    if action.role == "admin":
        from agent.permissions import UserRole
        from agent.turn import _perm_manager
        if _perm_manager.get_role(ctx.user_id) != UserRole.ADMIN:
            return ActionResult(code=5, message="无权限：仅限管理员操作。")
    try:
        return await asyncio.wait_for(action.handler(ctx), 2.5)
    except asyncio.TimeoutError:
        logger.warning(f"[buttons] verb={ctx.verb} timed out (>2.5s)")
        return ActionResult(code=1, message="操作超时，请稍后重试。")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[buttons] verb={ctx.verb} crashed")
        return ActionResult(code=1, message=f"操作失败：{e}")


# ── Session verbs (c2c only — §9.2 privacy) ────────────────────────


def _fresh_session_page(user_id: str, page: int = 1):
    from agent import session_render
    from agent.character_registry import get_character_registry
    from agent.turn import _perm_manager, _special_sessions
    return session_render.render_session_page(
        user_id, page, "c2c",
        sessions_mgr=_special_sessions, perm=_perm_manager,
        registry=get_character_registry())


@_register("session.del", scenes=("c2c",))
async def _session_del(ctx: ButtonContext) -> ActionResult:
    from agent import session_render
    from agent.turn import (
        _delete_quota_tail, _format_delete_summary, _profile_manager,
        _special_sessions,
    )
    if len(ctx.args) < 2:
        return ActionResult(code=1, message="参数不完整，请重新打开 /会话列表。")
    try:
        idx = int(ctx.args[0])
    except ValueError:
        return ActionResult(code=1, message="参数无效，请重新打开 /会话列表。")

    sessions = _special_sessions.list_sessions(ctx.user_id)
    ver = session_render.session_list_stamp(sessions)
    if ctx.args[1] != ver or not (1 <= idx <= len(sessions)):
        # List changed since render (or index stale) — refuse and refresh.
        md, rows = _fresh_session_page(ctx.user_id, 1)
        return ActionResult(code=1, message="",
                            markdown="列表已变化，已为你刷新：\n\n" + md,
                            rows=rows)

    name = sessions[idx - 1]["name"]
    try:
        msg = session_render.do_delete_session(
            ctx.user_id, name,
            sessions_mgr=_special_sessions,
            format_summary=_format_delete_summary,
            quota_tail=_delete_quota_tail,
            on_deleted=lambda turns: _profile_manager.flush_on_session_end(
                ctx.user_id, turns),
        )
    except ValueError as e:
        return ActionResult(code=1, message=str(e))
    md, rows = _fresh_session_page(ctx.user_id, 1)
    return ActionResult(code=0, markdown=msg + "\n\n" + md, rows=rows)


@_register("session.page", scenes=("c2c",))
async def _session_page(ctx: ButtonContext) -> ActionResult:
    try:
        page = int(ctx.args[0]) if ctx.args else 1
    except ValueError:
        page = 1
    md, rows = _fresh_session_page(ctx.user_id, page)
    return ActionResult(code=0, markdown=md, rows=rows)


@_register("session.end", scenes=("c2c",))
async def _session_end(ctx: ButtonContext) -> ActionResult:
    from agent.turn import _special_sessions
    active = _special_sessions.get_active(ctx.user_id)
    if not active:
        return ActionResult(code=1, message="当前没有活跃的特殊会话。")
    _special_sessions.end_active(ctx.user_id)
    return ActionResult(
        code=0,
        message=(f"已退出特殊会话「{active.name}」，回到临时会话模式。\n"
                 f"内容已保存，随时可以用 /切换会话 {active.name} 恢复。"))


# ── Character verb ─────────────────────────────────────────────────


@_register("char.del", scenes=("c2c",))
async def _char_del(ctx: ButtonContext) -> ActionResult:
    from agent import session_render
    from agent.character_registry import get_character_registry
    if len(ctx.args) < 2:
        return ActionResult(code=1, message="参数不完整，请重新打开 /删除角色。")
    try:
        idx = int(ctx.args[0])
    except ValueError:
        return ActionResult(code=1, message="参数无效，请重新打开 /删除角色。")
    registry = get_character_registry()
    metas = registry.list_ugc(ctx.user_id)
    ver = session_render.character_list_stamp(metas)
    if ctx.args[1] != ver or not (1 <= idx <= len(metas)):
        md, rows = session_render.render_character_page(
            ctx.user_id, registry=registry)
        return ActionResult(code=1, markdown="列表已变化，已为你刷新：\n\n" + md,
                            rows=rows)
    char_id = metas[idx - 1].get("char_id", "")
    try:
        msg = session_render.do_delete_character(
            ctx.user_id, char_id, registry=registry)
    except ValueError as e:
        return ActionResult(code=1, message=str(e))
    md, rows = session_render.render_character_page(
        ctx.user_id, registry=registry)
    if rows:
        return ActionResult(code=0, markdown=msg + "\n\n" + md, rows=rows)
    return ActionResult(code=0, markdown=msg + "\n\n" + md)


# ── Group admin verbs ──────────────────────────────────────────────


@_register("toggle.feature", role="admin", scenes=("group",))
async def _toggle_feature(ctx: ButtonContext) -> ActionResult:
    from agent import session_render
    from agent.group_features import FEATURE_LABELS, get_group_features
    if len(ctx.args) < 2 or ctx.args[0] not in FEATURE_LABELS:
        return ActionResult(code=1, message="未知的功能开关。")
    feature, act = ctx.args[0], ctx.args[1]
    if act not in ("on", "off"):
        return ActionResult(code=1, message="参数必须是 on 或 off。")
    gf = get_group_features()
    gf.refresh()
    enabled = act == "on"
    gf.set_feature(ctx.group_id, feature, enabled)
    label = FEATURE_LABELS[feature]
    md, rows = session_render.render_toggle_page(ctx.group_id, gf=gf)
    return ActionResult(
        code=0,
        markdown=(f"已在本群{'开启' if enabled else '关闭'}「{label}」。\n\n"
                  + md),
        rows=rows)


@_register("toggle.persona", role="admin", scenes=("group",))
async def _toggle_persona(ctx: ButtonContext) -> ActionResult:
    from agent.character_registry import get_character_registry
    if not ctx.args:
        return ActionResult(code=1, message="缺少人格参数。")
    pm = get_character_registry()
    key = ctx.args[0]
    if key == "default":
        pm.clear_group_personality(ctx.group_id)
        global_key = pm.get_default()
        return ActionResult(
            code=0,
            message=(f"已清除本群默认人格，回落到全局默认"
                     f"「{pm.get_display_name(global_key)}」。"))
    try:
        # owner_id lets the admin bind their own UGC characters, same as
        # the /toggle personality text path.
        resolved = pm.set_group_personality(
            ctx.group_id, key, owner_id=ctx.user_id)
    except ValueError as e:
        return ActionResult(code=1, message=str(e))
    return ActionResult(
        code=0,
        message=f"已设置本群默认人格为「{pm.get_display_name(resolved)}」。")


@_register("admin.probe", role="admin")
async def _admin_probe(ctx: ButtonContext) -> ActionResult:
    from adapters.qq_official import kb_snapshot
    snap = kb_snapshot()
    lines = [
        "**按钮能力探测**",
        f"- 熔断状态: {snap.get('state')}",
        f"- 当前可用: {'是' if snap.get('available') else '否'}"
        f"（env={snap.get('env')}）",
        f"- 累计拒绝: {snap.get('rejected_total')}",
        f"- 最近拒绝码: {snap.get('last_reject_code') or '无'}",
        f"- 冷却剩余: {snap.get('disabled_seconds_left', 0):.0f}s",
    ]
    return ActionResult(code=0, markdown="\n".join(lines))


@_register("panel.close", role="admin")
async def _panel_close(ctx: ButtonContext) -> ActionResult:
    # The platform cannot retract messages; the ack toast is the feedback.
    return ActionResult(code=0, message="面板已关闭。")
