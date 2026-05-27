"""
Agent execution context — Context variables for cross-layer communication.

Allows tools to send images/segments through the QQ chat without
modifying every intermediate function signature (agent → tool_registry → tool).
"""

import contextvars
from typing import Any, Callable, Optional

# Coroutine function that can send MessageSegment or str to QQ
_send_msg: contextvars.ContextVar[Optional[Callable[[Any], Any]]] = (
    contextvars.ContextVar("_send_msg", default=None)
)
