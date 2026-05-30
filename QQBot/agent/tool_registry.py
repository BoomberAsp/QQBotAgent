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
        self._tools[name] = {
            "function": func,
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
        try:
            if inspect.iscoroutinefunction(func):
                result = await func(**arguments)
            else:
                result = func(**arguments)
            return str(result) if result is not None else "(tool executed successfully, no output)"
        except TypeError as e:
            return f"[Error] Invalid arguments for tool '{name}': {e}"
        except Exception as e:
            return f"[Error] Tool '{name}' execution failed: {e}"

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
