"""
Agent execution context — Context variables for cross-layer communication.

Allows tools to send images/segments through the QQ chat without
modifying every intermediate function signature (agent → tool_registry → tool).
Also carries the current user's workspace path for scoped file operations.
"""

import contextvars
from typing import Any, Callable, Optional

# Current user's QQ ID.
# Set by agent_router before each agent.run() call.
# Used by tools that need to query per-user state (sessions, workspace, etc.).
_current_user_id: contextvars.ContextVar[str] = (
    contextvars.ContextVar("_current_user_id", default="")
)

# Coroutine function that can send MessageSegment or str to QQ
_send_msg: contextvars.ContextVar[Optional[Callable[[Any], Any]]] = (
    contextvars.ContextVar("_send_msg", default=None)
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

# File-creation callback: tools invoke it after writing a file into the
# workspace, passing the absolute path. Set by agent_router before agent.run()
# to attribute the file to the active special session.
_on_file_created: contextvars.ContextVar[Optional[Callable[[str], None]]] = (
    contextvars.ContextVar("_on_file_created", default=None)
)
