"""
Agent execution context — Context variables for cross-layer communication.

Allows tools to send images/segments through the QQ chat without
modifying every intermediate function signature (agent → tool_registry → tool).
Also carries the current user's workspace path for scoped file operations.
"""

import contextvars
from typing import Any, Callable, Optional

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
