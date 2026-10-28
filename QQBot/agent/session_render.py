"""
Shared keyboard-page renderers (buttons-design §6.6/§9.2, P1).

Pure leaf module: every manager is injected by the caller, and nothing
here imports agent.turn at module level — turn.py's /会话列表 handler and
agent.button_actions' callback handlers both render through these
functions so the two paths can never drift apart.

Privacy (§9.2): in a GROUP scope the session page never renders session
names, persona notes or message counts — only a guidance line telling the
user to continue in c2c. Group-rendered callback buttons are additionally
restricted to the rendering user's openid at the wire layer
(to_message_keyboard → Permission type=0 specify_user_ids).

Row budget: the platform caps keyboards at 5 rows (env
QQ_OFFICIAL_BUTTONS_MAX_ROWS), so the session page shows at most 3
sessions per page (3 rows + 1 pagination row + 1 footer row).
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from lib.button_spec import Btn, Rows, stamp

# ── Session pages ──────────────────────────────────────────────────

SESSION_PAGE_SIZE = 3   # 3 session rows + nav row + footer row ≤ 5 rows


def _persona_note(meta: dict, registry, user_id: str) -> str:
    """Mirror of turn.py /会话列表's persona annotation (keep in sync)."""
    kind = (meta or {}).get("kind")
    if kind == "char_creation":
        return "人格: 固定（角色创作）"
    pkey = (meta or {}).get("persona_key") or (
        (meta or {}).get("char_id") if kind == "bond_scene" else None)
    if pkey and registry._exists(str(pkey), user_id=user_id):
        return f"人格: {registry.get_display_name(str(pkey))}"
    if pkey:
        return "人格: 已失效→跟随默认"
    return "人格: 跟随默认"


def session_list_stamp(sessions: list) -> str:
    """Version stamp over the full session-name list (all pages).

    Callback buttons carry it; a mismatch means the list changed since
    render → the action refuses to execute and refreshes instead.
    """
    return stamp([s.get("name", "") for s in sessions])


def render_session_page(
    user_id: str,
    page: int,
    scope: str,
    *,
    sessions_mgr,
    perm,
    registry,
    page_size: int = SESSION_PAGE_SIZE,
) -> tuple[str, Rows]:
    """Render the /会话列表 keyboard page. Returns (markdown, rows).

    Archive-aware (scene-session-archive-design.md): 剧情/创作会话不占配额，
    行内带 [已完结]/[进行中]/（角色创作）标注；配额尾行与 turn.py 的
    _quota_used/_delete_quota_tail 同口径（helpers 函数级懒 import，避免
    模块级 turn 依赖成环）。页面是 list_sessions 平铺序（与 #n 别名一致），
    不做文本视图的三段分组——分组在 3 行/页的键盘版里放不下。
    """
    if scope != "c2c":
        # §9.2 — never leak private session data into a group chat.
        return (
            "会话列表涉及你的私密数据，请私聊我发送 /会话列表 查看和管理。",
            [[Btn(label="帮助", data="/帮助", action="cmd")]],
        )

    from agent import turn as _t   # function-level: turn imports this module

    role = perm.get_role(user_id)
    max_sessions = perm.get_max_special_sessions(role)
    sessions = sessions_mgr.list_sessions(user_id)
    archive_on = _t._scene_archive_enabled()
    if not sessions:
        hint = (f"使用 /新会话 [名称] 创建一个（最多 {max_sessions} 个普通会话；"
                f"剧情/创作会话不计配额）。" if archive_on else
                f"使用 /新会话 [名称] 创建一个（最多 {max_sessions} 个）。")
        return (
            f"你目前没有特殊会话。\n{hint}",
            [[
                Btn(label="新建会话", data="/新会话", action="cmd"),
                Btn(label="帮助", data="/帮助", action="cmd"),
            ]],
        )

    active_name = sessions_mgr._load_index(user_id).get("active_session")
    ver = session_list_stamp(sessions)
    total_pages = max(1, (len(sessions) + page_size - 1) // page_size)
    page = min(max(1, int(page or 1)), total_pages)
    start = (page - 1) * page_size
    chunk = sessions[start:start + page_size]

    lines = ["**你的特殊会话**"]
    rows: Rows = []
    for i, s in enumerate(chunk, start=start + 1):
        meta = s.get("metadata") or {}
        kind = meta.get("kind")
        is_cur = s["name"] == active_name
        marker = " ← 当前" if is_cur else ""
        kind_note = ""
        if archive_on and kind == _t.SCENE_KIND:
            state = "已完结" if _t._scene_completed(s) else "进行中"
            kind_note = f" [{state}] · 不计配额"
        elif archive_on and kind == _t.CREATION_KIND:
            kind_note = " [角色创作] · 不计配额"
        created = time.strftime("%m/%d %H:%M", time.localtime(s["created_at"]))
        p_note = _persona_note(meta, registry, user_id)
        lines.append(
            f"{i}. **{s['name']}**{marker}{kind_note}\n"
            f"   创建: {created} | 消息数: {s['total_messages']} | {p_note}"
        )
        rows.append([
            Btn(label=f"切换 {i}", data=f"/切换会话 #{i}", action="cmd",
                style=0 if is_cur else 1, visited="当前" if is_cur else ""),
            Btn(label=f"删 {i}", data=f"qb1:session.del:{i}:{ver}",
                action="cb", style=1, admin_only=False,
                modal=f"确认删除会话「{s['name']}」？关联文件会一并清理，此操作不可撤销。"),
        ])

    if total_pages > 1:
        nav: list = []
        if page > 1:
            nav.append(Btn(label="上一页", data=f"qb1:session.page:{page - 1}:{ver}",
                           action="cb"))
        nav.append(Btn(label=f"第{page}/{total_pages}页", data="/会话列表",
                       action="cmd", style=0))
        if page < total_pages:
            nav.append(Btn(label="下一页", data=f"qb1:session.page:{page + 1}:{ver}",
                           action="cb"))
        rows.append(nav)

    rows.append([
        Btn(label="新建", data="/新会话", action="cmd"),
        Btn(label="结束", data="/结束会话", action="cmd"),
        Btn(label="帮助", data="/帮助", action="cmd"),
    ])

    if archive_on:
        used = _t._quota_used(user_id)
        n_scene = len(_t._scene_archives(user_id))
        lines.append(f"\n普通会话 {used}/{max_sessions} · "
                     f"剧情归档 {n_scene}（不计配额）")
    else:
        lines.append(f"\n共 {len(sessions)}/{max_sessions} 个会话")
    return "\n".join(lines), rows


# ── Character pages (/删除角色 list) ───────────────────────────────

CHAR_PAGE_SIZE = 4   # 4 delete rows + …; md lists everything regardless

# Short labels for the group toggle panel — FEATURE_LABELS entries are
# 4 hanzi (display width 8), and "·开"/"·关" would overflow the 10-unit
# label cap, so the panel uses these 2-hanzi abbreviations.
FEATURE_SHORT = {"gacha": "抽卡", "image": "图片", "voice": "语音"}


def character_list_stamp(metas: list) -> str:
    return stamp([m.get("char_id", "") for m in metas])


def render_character_page(user_id: str, *, registry) -> tuple[str, Rows]:
    """Render the /删除角色 keyboard page. Returns (markdown, rows)."""
    metas = registry.list_ugc(user_id)
    if not metas:
        return (
            "你还没有自创角色。发送 /创建角色 开始创作。",
            [[Btn(label="帮助", data="/帮助", action="cmd")]],
        )
    ver = character_list_stamp(metas)
    lines = ["**你的自创角色**"]
    rows: Rows = []
    for i, m in enumerate(metas, 1):
        char_id = m.get("char_id", "")
        display = registry.get_display_name(char_id)
        lines.append(f"{i}. {display}（{char_id}）")
        if i <= CHAR_PAGE_SIZE:   # row budget: buttons for the first 4 only
            rows.append([Btn(
                label=f"删 {i}", data=f"qb1:char.del:{i}:{ver}",
                action="cb", style=1,
                modal=f"确认删除角色「{display}」？好感度记录会保留，此操作不可撤销。")])
    if len(metas) > CHAR_PAGE_SIZE:
        lines.append("（更多角色请用 /删除角色 <名称> 删除）")
    lines.append("\n好感度记录在删除后保留；重建同名角色不会自动继承。")
    return "\n".join(lines), rows


# ── Group admin toggle panel (/toggle) ─────────────────────────────


def render_toggle_page(group_id: str, *, gf, registry=None) -> tuple[str, Rows]:
    """Render the group feature-toggle admin panel. Returns (markdown, rows).

    All callback buttons are admin_only — the wire layer additionally
    pins them to the rendering admin's openid in group scope.
    """
    from agent.group_features import FEATURE_LABELS, FEATURE_ORDER

    features = gf.get_all_features(group_id)
    lines = ["**当前群聊功能状态**"]
    rows: Rows = []
    for key in FEATURE_ORDER:
        label = FEATURE_LABELS[key]
        short = FEATURE_SHORT.get(key, label[:2])
        on = features[key]
        lines.append(f"- {label}: {'开启' if on else '关闭'}")
        rows.append([
            Btn(label=f"{short}·开", data=f"qb1:toggle.feature:{key}:on",
                action="cb", admin_only=True, style=0 if on else 1),
            Btn(label=f"{short}·关", data=f"qb1:toggle.feature:{key}:off",
                action="cb", admin_only=True, style=0 if not on else 1),
        ])

    persona_line = ""
    if registry is not None:
        group_key = registry.get_group_personality(group_id)
        if group_key:
            persona_line = (f"\n默认人格: {registry.get_display_name(group_key)}"
                            f"（{group_key}）")
        else:
            persona_line = (f"\n默认人格: 未绑定（全局默认 "
                            f"{registry.get_display_name(registry.get_default())}）")
    lines.append(persona_line)
    lines.append("点按开关即时生效；人格绑定请用「人格」按钮补全名称。")

    rows.append([
        Btn(label="人格", data="/toggle personality ", action="cmd",
            enter=False, admin_only=False),
        Btn(label="清除绑定", data="qb1:toggle.persona:default",
            action="cb", admin_only=True, style=1),
        Btn(label="关闭", data="qb1:panel.close", action="cb",
            admin_only=True, style=0),
    ])
    return "\n".join(lines), rows


# ── Shared deletion semantics (§12.5) ──────────────────────────────
# Extracted from turn.py's two-step text-confirm branches so the text
# protocol and the callback buttons execute byte-identical deletions.


def do_delete_session(
    user_id: str,
    name: str,
    *,
    sessions_mgr,
    format_summary: Callable[[dict], str],
    quota_tail: Callable[[str, str], str],
    on_deleted: Optional[Callable[[int], None]] = None,
) -> str:
    """Delete a special session and return the user-facing summary text.

    Mirrors turn.py's text-confirm branch exactly (scene-session-archive
    §4.2/B6): the session's turn count AND metadata.kind are captured
    BEFORE deletion — ``on_deleted(del_turns)`` is the profile
    delete-flush hook (Decision J, must never break the deletion, so
    exceptions are swallowed), and ``quota_tail(user_id, kind)`` renders
    the archive-aware quota line (turn._delete_quota_tail).

    Raises:
        ValueError: session does not exist (from sessions_mgr.delete).
    """
    del_turns = 0
    del_kind = ""
    for s in sessions_mgr.list_sessions(user_id):
        if s.get("name") == name:
            del_turns = s.get("total_messages", 0) // 2
            del_kind = str((s.get("metadata") or {}).get("kind") or "")
            break
    result = sessions_mgr.delete(user_id, name)
    if on_deleted is not None:
        try:
            on_deleted(del_turns)
        except Exception:  # noqa: BLE001
            pass  # profile flush must never break session deletion
    msg = f"已删除特殊会话「{name}」。"
    summary = format_summary(result)
    if summary:
        msg += "\n" + summary
    msg += quota_tail(user_id, del_kind)
    return msg


def do_delete_character(user_id: str, char_id: str, *, registry) -> str:
    """Delete a UGC character (ownership-checked). Returns summary text.

    Raises:
        ValueError: char is missing / not UGC / not authored by user_id.
    """
    if not (char_id and registry.is_ugc(char_id)
            and (registry.get_meta(char_id) or {}).get("author") == user_id):
        raise ValueError(f"未找到你的自创角色（{char_id}）。")
    display = registry.get_display_name(char_id)
    registry.delete(char_id)
    purged = registry.purge_references(char_id)
    note = f"\n（已解除 {purged} 处人格设置引用）" if purged else ""
    return (
        f"已删除自创角色「{display}」。{note}\n"
        "该角色的好感度记录已保留，若日后重建同名角色不会自动继承。"
    )
