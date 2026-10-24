#!/usr/bin/env python3
"""One-shot integration patcher for the Story KB feature.

The DSH write tool cannot rename over the ``\\\\wsl.localhost`` 9p mount, so
project files are patched in place from inside WSL. Every replacement must
match EXACTLY once or the run aborts for that patch; all patches carry an
explicit idempotence marker so re-running is safe.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.expanduser("~/QQBotAgent/QQBot")

PATCHES = []


def patch(rel_path: str, anchor: str, addition: str, mode: str, already: str):
    """mode: 'after' (anchor + addition) | 'replace' (addition)."""
    PATCHES.append({
        "path": os.path.join(ROOT, rel_path),
        "anchor": anchor,
        "addition": addition,
        "mode": mode,
        "already": already,
    })


# ── 1. agent/context.py ────────────────────────────────────────────

patch(
    "agent/context.py",
    '''_turn_sender: contextvars.ContextVar[Optional["Sender"]] = (
    contextvars.ContextVar("_turn_sender", default=None)
)''',
    '''

# ── Story knowledge base (Ark Re:Code 原作剧情) ────────────────────
# Per-turn retrieved original-story block. Built by agent/turn.py from the
# local FTS index when a character persona is about to speak, then injected by
# agent._build_messages() into the L4 volatile tail — never into the cached
# L0-L2 prefix, so retrieval churn cannot break provider prefix caching.
# Empty string = nothing relevant was found (or the persona has no mapping).
_current_story_context: contextvars.ContextVar[str] = (
    contextvars.ContextVar("_current_story_context", default="")
)

# Character id of the active persona inside the story corpus (e.g. "H076" for
# rubi/露比). Read by the story_search tool to label "you were there" and to
# boost scenes featuring that character. Empty = unmapped persona.
_current_story_char_id: contextvars.ContextVar[str] = (
    contextvars.ContextVar("_current_story_char_id", default="")
)''',
    mode="after",
    already="_current_story_context",
)

# ── 2. agent/agent.py : import + inject into the L4 tail ───────────

patch(
    "agent/agent.py",
    """            _current_bond_context,
        )""",
    """            _current_bond_context, _current_story_context,
        )""",
    mode="replace",
    already="_current_story_context,",
)

patch(
    "agent/agent.py",
    """        bond_ctx = _current_bond_context.get()
        if bond_ctx:
            volatile.append(bond_ctx)""",
    """

        # Original-story memory (Story KB): retrieved per turn by
        # agent/turn.py when a character persona is on stage — the character
        # recalls "her own" canon scenes without calling a tool. Volatile:
        # rebuilt every turn, never touches the cached prefix.
        story_ctx = _current_story_context.get()
        if story_ctx:
            volatile.append(story_ctx)""",
    mode="after",
    already="story_ctx = _current_story_context.get()",
)

# ── 3. agent/permissions.py : story_search is a public tool ────────

patch(
    "agent/permissions.py",
    '''    "recall_memory",         # Reads own LONG-tier memories only (user_id-scoped)
}''',
    '''    "recall_memory",         # Reads own LONG-tier memories only (user_id-scoped)
    "story_search",          # Original-story KB (read-only, no user data)
}''',
    mode="replace",
    already="story_search",
)

# ── 4. agent/turn.py : imports, helper+tools, per-turn call ────────

patch(
    "agent/turn.py",
    """    _pending_task_fold, _current_bond_context, _delegate_depth,
)""",
    """    _pending_task_fold, _current_bond_context, _delegate_depth,
    _current_story_context, _current_story_char_id,
)""",
    mode="replace",
    already="_current_story_char_id,",
)

STORY_HELPER = '''# ── Original-story memory (Story Knowledge Base) ───────────────────
#
# The story KB (lib/story_kb.py + data/story_kb/story.db) holds every dialogue
# line of the main story and the event stories. Two access paths:
#
#   1. _build_story_context() — automatic, character plane only. Retrieves the
#      few most relevant scenes for the user's message and hands them to
#      agent._build_messages() as an L4 volatile block, so the persona recalls
#      canon without calling a tool (露比 herself never operates tools).
#   2. story_search — explicit tool for the task plane (Roxy) when a story
#      question got routed there; returns deeper excerpts.
#
# Both are strictly best-effort: a missing/incompatible DB, a cold query or any
# exception degrades to "no story memory this turn", never to a failed reply.

# Injection budget: 2 hits / ~1800 chars ≈ 700-900 tokens of L4 context — big
# enough for one good scene plus a supporting beat, small enough to keep the
# turn cheap and leave room for the actual conversation.
_STORY_INJECT_HITS = 2
_STORY_INJECT_CHARS = 1800
# Fused RRF score floor: a single-index rank-1 match scores ~0.0164, so 0.0165
# keeps incidental word overlap out while still admitting a weaker match whose
# only hit is real.
_STORY_MIN_SCORE = 0.0165

_STORY_QUERY_STRIP_RE = re.compile(
    r"\\[(?:用户上传了|用户发送了|系统提醒|系统提示)[^\\]]*\\]"
)


def _story_query_from(text: str, limit: int = 400) -> str:
    """Reduce a turn's augmented message to the user's actual question."""
    if not text:
        return ""
    text = _STORY_QUERY_STRIP_RE.sub(" ", text)
    text = re.sub(r"用户说[:：]", " ", text)
    text = re.sub(r"\\s+", " ", text).strip()
    if not text or text.startswith("/") or len(text) < 4:
        return ""
    return text[:limit]


def _story_char_id_for(persona_key: str, pm) -> str:
    """Resolve the active persona to a story character id (cached lookup)."""
    try:
        from lib.story_kb import persona_char_id
        display = pm.get_display_name(persona_key) if pm else ""
        return persona_char_id(persona_key, display)
    except Exception:  # noqa: BLE001
        return ""


def _build_story_context(persona_key: str, query: str, pm) -> tuple:
    """Return (injection_block, char_id) for the current turn."""
    try:
        from lib.story_kb import get_story_kb

        kb = get_story_kb()
        if not kb.available:
            return "", ""
        q = _story_query_from(query)
        if not q:
            return "", ""
        char_id = _story_char_id_for(persona_key, pm)
        block = kb.format_injection(
            q,
            persona_char_id=char_id,
            persona_name=pm.get_short_name(persona_key) if pm else "",
            limit=_STORY_INJECT_HITS,
            max_chars=_STORY_INJECT_CHARS,
            min_score=_STORY_MIN_SCORE,
        )
        return block or "", char_id
    except Exception:  # noqa: BLE001 — never break a reply
        return "", ""


def _story_search(query: str = "", limit: int = 4) -> str:
    """Deep lookup in the original-story knowledge base (task-plane tool)."""
    try:
        from lib.story_kb import get_story_kb

        kb = get_story_kb()
        if not kb.available:
            return ("[story_search] 剧情知识库尚未构建（data/story_kb/story.db 不存在）。"
                    "管理员可运行 python scripts/build_story_kb.py --all 生成。")
        q = (query or "").strip()
        if not q:
            return "[story_search] 请提供要查询的关键词（角色名、事件名、章节等）。"
        try:
            limit = max(1, min(int(limit), 8))
        except (TypeError, ValueError):
            limit = 4
        return kb.format_tool_result(
            q, limit=limit, persona_char_id=_current_story_char_id.get(),
        )
    except Exception as exc:  # noqa: BLE001
        return f"[story_search] 检索失败：{type(exc).__name__}: {exc}"


_tool_registry.register(
    "story_search", _story_search,
    "《星陨计划(Ark Re:Code)》原作剧情库检索。当用户询问游戏剧情、角色过往、"
    "活动故事或世界观事件（如「第一次方舟战争」「谁是指挥官高文」「露比的过去」）时，"
    "用此工具取回台词原文（英文）与中文梗概。返回内容含场景出处、单元梗概、"
    "本段梗概与对白摘录；回复时请把英文台词译成中文转述，不要整段照抄。"
    "查不到时换用角色名/事件名的其他说法重试。",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "剧情检索关键词：角色名、事件名、章节号或情节描述",
            },
            "limit": {
                "type": "integer",
                "description": "返回片段数，1-8，默认 4",
            },
        },
        "required": ["query"],
    },
)


'''

patch(
    "agent/turn.py",
    "# ── Turn entry points (busy-guard wrappers)",
    STORY_HELPER + "# ── Turn entry points (busy-guard wrappers)",
    mode="replace",
    already="def _story_search(",
)

patch(
    "agent/turn.py",
    '''        _delegate_depth.set(0)
        _personality_transition.set(_personality_switch_marker.get(user_id, ""))''',
    '''

        # ── Original-story memory injection (Story KB) ──
        # Only where the character herself answers: the character plane (split
        # personas) or a unified character persona. The task plane runs as
        # neutral Roxy and reaches the KB through the story_search tool
        # instead, so its answers never borrow the character's first person.
        _story_block, _story_char = "", ""
        if not is_creation and not is_scene:
            if character_plane or persona_mode != "split":
                if persona_key and persona_key != "assistant":
                    _story_block, _story_char = _build_story_context(
                        persona_key, text_content or augmented_message, pm)
        _current_story_context.set(_story_block)
        _current_story_char_id.set(_story_char)''',
    mode="after",
    already="_current_story_context.set(_story_block)",
)


def main() -> int:
    failed = 0
    for p in PATCHES:
        path = p["path"]
        name = os.path.basename(path)
        if not os.path.isfile(path):
            print(f"!! missing file: {path}")
            failed += 1
            continue
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()

        if p["already"] in src:
            print(f"== skip (already applied): {name} :: {p['already'][:36]}")
            continue

        count = src.count(p["anchor"])
        if count != 1:
            print(f"!! anchor matched {count}x in {name}: {p['anchor'][:60]!r}")
            failed += 1
            continue

        new = (src.replace(p["anchor"], p["addition"], 1) if p["mode"] == "replace"
               else src.replace(p["anchor"], p["anchor"] + p["addition"], 1))
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        print(f"ok  patched {name} ({p['mode']})")

    print(f"\n{failed} failure(s)" if failed else "\nall patches applied")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
