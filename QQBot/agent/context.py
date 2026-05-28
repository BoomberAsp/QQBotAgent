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
