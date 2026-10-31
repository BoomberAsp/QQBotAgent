"""
Tool Registry — Registration, schema generation, and execution of agent tools.

Tools are functions that the agent can call. Each tool has:
- A unique name
- A Python callable (sync or async)
- A description (for the LLM)
- A JSON Schema parameters definition
"""

import json
import inspect
from typing import Any, Callable, Dict, List, Optional, Set

from lib.errors import ToolError, classify_exception, log_exception


class ToolRegistry:
    """Registry for agent tools with OpenAI-compatible schema generation."""

    def __init__(self):
        self._tools: Dict[str, dict] = {}

    # ── Registration ──────────────────────────────────────────────

    def register(
        self,
        name: str,
        func: Callable,
        description: str,
        parameters: dict,
    ):
        """Register a tool function.

        Args:
            name: Unique tool name (used in function calling).
            func: Python callable. Can be sync or async.
            description: Natural language description for the LLM.
            parameters: JSON Schema for the tool's parameters.
        """
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            signature = None  # some builtins/C callables have no signature

        self._tools[name] = {
            "function": func,
            "signature": signature,
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            },
        }

    def unregister(self, name: str):
        """Remove a tool from the registry."""
        self._tools.pop(name, None)

    # ── Schema generation ─────────────────────────────────────────

    def get_schemas(self) -> List[dict]:
        """Return all tool schemas in OpenAI function-calling format."""
        return [t["schema"] for t in self._tools.values()]

    def get_schemas_for(self, allowed_names: Set[str]) -> List[dict]:
        """Return schemas only for the specified tool names."""
        allowed = set(allowed_names)
        return [t["schema"] for name, t in self._tools.items() if name in allowed]

    def get_schema(self, name: str) -> Optional[dict]:
        """Get a single tool's schema."""
        tool = self._tools.get(name)
        return tool["schema"] if tool else None

    def list_tools(self) -> List[str]:
        """Return list of registered tool names."""
        return list(self._tools.keys())

    # ── Execution ─────────────────────────────────────────────────

    async def execute(self, name: str, arguments: dict) -> str:
        """Execute a tool by name with the given arguments.

        Args:
            name: Tool name.
            arguments: Keyword arguments dict (parsed from LLM JSON).

        Returns:
            Tool result as a string.
        """
        tool = self._tools.get(name)
        if not tool:
            return f"[Error] Tool '{name}' is not registered. Available: {', '.join(self.list_tools())}"

        func = tool["function"]

        # Pre-bind arguments so signature mismatches surface as actionable,
        # schema-level errors. Parameter names are already public in the
        # tool's JSON schema, so this text is safe for the model to see.
        signature = tool.get("signature")
        if signature is not None:
            try:
                signature.bind(**arguments)
            except TypeError as e:
                return f"[Error] Invalid arguments for tool '{name}': {e}"

        try:
            if inspect.iscoroutinefunction(func):
                result = await func(**arguments)
            else:
                result = func(**arguments)
            return str(result) if result is not None else "(tool executed successfully, no output)"
        except Exception as e:
            # Never feed raw exception text back to the model: it can carry
            # server paths, internal URLs, or env details, and the model will
            # copy it into user-visible replies. The full traceback is logged
            # under a diagnostic code; only the safe text is returned.
            code = log_exception(f"tool '{name}' execution failed", e)
            if isinstance(e, ToolError):
                msg = e.safe_message
                return msg if msg.startswith("[Error]") else f"[Error] {msg}"
            if classify_exception(e) == "unavailable":
                return (
                    f"[Error] 工具 '{name}' 暂时不可用（超时或网络错误，诊断编号 {code}）。"
                    f"请稍后重试或改用其他方式。"
                )
            return (
                f"[Error] 工具 '{name}' 执行失败（诊断编号 {code}）。"
                f"请勿原样重试同一调用；改用其他方式或向用户说明情况。"
            )

    # ── Bulk registration ─────────────────────────────────────────

    def register_from_config(self, tools_config: List[dict]):
        """Register multiple tools from a configuration list.

        Each entry: {'name': str, 'function': callable, 'description': str, 'parameters': dict}
        """
        for tool in tools_config:
            self.register(
                name=tool["name"],
                func=tool["function"],
                description=tool["description"],
                parameters=tool["parameters"],
            )

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
