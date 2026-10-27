"""
Agent execution context — Context variables for cross-layer communication.

Allows tools to send images/text through the current chat without
modifying every intermediate function signature (agent → tool_registry → tool).
Also carries the current user's workspace path for scoped file operations.
"""
from __future__ import annotations

import contextvars
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agent.outbound import Sender

# Current user's QQ ID.
# Set by agent_router before each agent.run() call.
# Used by tools that need to query per-user state (sessions, workspace, etc.).
_current_user_id: contextvars.ContextVar[str] = (
    contextvars.ContextVar("_current_user_id", default="")
)

# Platform-agnostic outbound Sender (agent/outbound.py) that tools use
# to push images/text directly to the current chat. Set by agent/turn.py
# before each agent.run() call (stage 3; replaces the old _send_msg
# coroutine callback, which was _safe_send in disguise).
_current_sender: contextvars.ContextVar[Optional["Sender"]] = (
    contextvars.ContextVar("_current_sender", default=None)
)

# Current user's isolated workspace root path.
# Set by agent_router before each agent.run() call.
# Tools (execute_code, shell_exec, read_file) read this to scope
# file operations to the user's own workspace directory.
_current_user_workspace: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("_current_user_workspace", default=None)
)

# Current user's permission role ("admin" / "vip" / "regular").
# Set by agent_router before each agent.run() call.
# Used by read_file to gate multimodal analysis, and by execute_code
# to apply tiered resource limits.
_current_user_role: contextvars.ContextVar[str] = (
    contextvars.ContextVar("_current_user_role", default="regular")
)

# Tiered resource limits for execute_code.
# Dict with keys: max_timeout (int seconds), max_output (int bytes),
# max_memory_mb (int). Set by agent_router from PermissionManager.
_current_code_limits: contextvars.ContextVar[dict] = (
    contextvars.ContextVar("_current_code_limits", default={})
)

# Per-user workspace disk quota in bytes, resolved from the current user's
# role. Set by agent_router before each agent.run() call (admin 2GB /
# vip 500MB / regular 100MB). Read by UserWorkspaceManager.check_quota()
# and get_quota_context() so quota warnings use the correct per-role limit.
_current_quota_bytes: contextvars.ContextVar[int] = (
    contextvars.ContextVar("_current_quota_bytes", default=0)
)

# Current group chat ID (only set for group messages, empty for private).
# Set by agent_router before each agent.run() call.
# Used by tools (play_gacha_animation) to check group-level feature toggles.
_current_group_id: contextvars.ContextVar[str] = (
    contextvars.ContextVar("_current_group_id", default="")
)

# Group feature restriction context — pre-built system prompt section
# set by agent_router before agent.run(). Read by agent._build_messages()
# to inject into the system prompt so the LLM knows what's disabled.
_current_group_context: contextvars.ContextVar[str] = (
    contextvars.ContextVar("_current_group_context", default="")
)

# Current personality name (e.g. "assistant", "roxy_character", "rubi").
# Set by agent_router before each agent.run() call, persisted per-user.
# Read by agent._build_messages() to inject the personality prompt.
_current_personality: contextvars.ContextVar[str] = (
    contextvars.ContextVar("_current_personality", default="assistant")
)

# One-line transition notice injected right after the personality prompt when
# the user has switched personalities without clearing history, e.g.
# "人格已从 X 切换为 Y，历史回复风格不代表当前人格。". Set by agent_router before
# agent.run(); read by agent._build_messages(). Empty = no transition notice.
_personality_transition: contextvars.ContextVar[str] = (
    contextvars.ContextVar("_personality_transition", default="")
)

# File-creation callback: tools invoke it after writing a file into the
# workspace, passing the absolute path. Set by agent_router before agent.run()
# to attribute the file to the active special session.
_on_file_created: contextvars.ContextVar[Optional[Callable[[str], None]]] = (
    contextvars.ContextVar("_on_file_created", default=None)
)

# Character-plane bond state block (Bond-System-Design.md §6): built per-turn
# by agent_router (level/title, mood, companionship stats), read by
# agent._build_messages() and injected into the L4 volatile tail — never into
# the cached L0-L2 prefix. Empty = no injection (unified personality or
# task-plane run).
_current_bond_context: contextvars.ContextVar[str] = (
    contextvars.ContextVar("_current_bond_context", default="")
)

# Per-turn render-capability declaration (buttons-design §6.10.6): built by
# agent/turn.py when the effective persona is unified AND the turn's Sender
# reports supports_markdown (QQ official route, msg_type=2). Injected by
# agent._build_messages() into the L4 volatile tail — never the cached
# prefix — and doubles as the render-layer flag: _send_response() reads it
# back to pick the markdown branch. Empty = plain-text turn (split persona,
# OneBot channel, or markdown disabled/breaker-open).
_current_render_context: contextvars.ContextVar[str] = (
    contextvars.ContextVar("_current_render_context", default="")
)

# delegate_task nesting guard (Bond-System-Design.md §3.3): 0 = not inside a
# delegation. run_delegated_task sets 1 for the inner task-plane run; a second
# delegate_task call sees non-zero and hard-rejects (single-level nesting).
_delegate_depth: contextvars.ContextVar[int] = (
    contextvars.ContextVar("_delegate_depth", default=0)
)

# Task-fold directive, set by the ``finalize_subtask`` tool mid-run and read by
# agent persistence at end of run. A dict {"line": str, "boundary": int|None}:
#   - "line"     the compact structured record to persist instead of the raw
#                final response;
#   - "boundary" session message index before the task's setup turns — messages
#                from this index onward are removed (folded) before the record
#                is appended. None = no setup turns to fold (compress only).
# None (default) = persist normally (subject to auto-compression fallback).
_pending_task_fold: contextvars.ContextVar[Optional[dict]] = (
    contextvars.ContextVar("_pending_task_fold", default=None)
)

# Platform of the current turn ("onebot" | "qq_official"). Set by
# agent.turn.run_agent_turn()/run_continuous_turn() from view.platform.
# Read by PermissionManager.get_role() to pick the identity table:
# qq_official consults QQ_OFFICIAL_SUPERUSERS/QQ_OFFICIAL_VIP_USERS
# (opaque openids), onebot keeps SUPERUSERS/VIP_USERS (QQ numbers) —
# plan §7.7. Task-scoped: each NoneBot event runs in its own task, so
# concurrent turns on the two platforms never cross-contaminate.
_current_platform: contextvars.ContextVar[str] = (
    contextvars.ContextVar("_current_platform", default="onebot")
)

# Per-turn sender override (stage 4). The QQ official entry plugin sets
# this to the QQOfficialSender bound to the current event; _safe_send
# resolves explicit ``sender`` argument → this contextvar → the
# default_sender slot. The OneBot entry never sets it (its matcher-slot
# path is unchanged).
_turn_sender: contextvars.ContextVar[Optional["Sender"]] = (
    contextvars.ContextVar("_turn_sender", default=None)
)

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
)
