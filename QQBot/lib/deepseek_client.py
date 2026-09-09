"""
DeepSeek API Client — LLM backend for the QQBot agent.

Supports:
- Simple chat completion (backward compatible)
- Function/tool calling (OpenAI-compatible format)
- Structured response parsing for agent loop
"""

import json
import os
from typing import Any, Dict, List, Optional

import httpx
from nonebot import get_driver

from .token_ledger import extract_usage, token_ledger


class DeepSeekClient:
    """Async HTTP client for DeepSeek Chat Completion API.

    Supports both plain chat and function/tool calling.
    API format is compatible with OpenAI's chat completions endpoint.
    """

    def __init__(self, api_key=None, api_base=None, model=None):
        # When both api_key and api_base are passed explicitly (WebUI Playground,
        # ModelRouter._create_client), skip the NoneBot driver lookup entirely —
        # the panel process never initializes NoneBot, so get_driver() would raise.
        if api_key and api_base:
            self.api_key = api_key
            self.api_base = api_base
            self.model = model or "deepseek-chat"
            return
        config = get_driver().config
        # Three-tier fallback: constructor arg → NoneBot config → env variable
        self.api_key = (
            api_key
            or getattr(config, "DEEPSEEK_API_KEY", None)
            or os.getenv("DEEPSEEK_API_KEY")
        )
        self.api_base = (
            api_base
            or getattr(config, "DEEPSEEK_API_BASE", None)
            or os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
        )
        self.model = model or "deepseek-chat"

    # ── Simple Chat Completion (backward compatible) ──────────────

    async def chat_completion(
        self,
        message: str,
        history: list = None,
        timeout_set: float = 180.0,
        purpose: str = "chat",
    ) -> str:
        """Simple chat completion — single message, no tools.

        Args:
            message: User message text.
            history: Optional conversation history.
            timeout_set: Request timeout in seconds.
            purpose: Usage-ledger purpose tag (triage / profile / chat).

        Returns:
            Response content string, or error message.
        """
        messages = []
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": message})

        data = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.api_base}/chat/completions",
                    headers=self._headers(),
                    json=data,
                    timeout=timeout_set,
                )
                response.raise_for_status()
                result = response.json()
                self._record_usage(result, purpose)
                return result["choices"][0]["message"]["content"] or ""
            except httpx.ConnectTimeout:
                return f"思考超时，最大时长{timeout_set}秒。"
            except httpx.ReadTimeout:
                return f"响应超时，最大时长{timeout_set}秒。"
            except Exception as e:
                return f"调用DeepSeek API时出错: {str(e)}"

    # ── Tool-Enabled Chat Completion ──────────────────────────────

    async def chat_completion_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[dict],
        timeout: float = 180.0,
        purpose: str = "agent_loop",
    ) -> Dict[str, Any]:
        """Chat completion with function/tool calling support.

        Args:
            messages: Full message list including system prompt and history.
            tools: List of tool schemas in OpenAI format.
            timeout: Request timeout in seconds.
            purpose: Usage-ledger purpose tag (default: agent_loop).

        Returns:
            Dict with keys:
            - 'content': str or None (null if tool_calls present)
            - 'tool_calls': list or None (list of tool call dicts)
            - 'role': 'assistant'
            - 'finish_reason': str
        """
        data = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "tools": tools,
            "tool_choice": "auto",  # Let the model decide when to use tools
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.api_base}/chat/completions",
                    headers=self._headers(),
                    json=data,
                    timeout=timeout,
                )
                response.raise_for_status()
                result = response.json()
                self._record_usage(result, purpose)
                return self._parse_response(result)
            except httpx.ConnectTimeout:
                return {
                    "content": f"思考超时，最大时长{timeout}秒。",
                    "tool_calls": None,
                    "role": "assistant",
                    "finish_reason": "error",
                }
            except httpx.ReadTimeout:
                return {
                    "content": f"响应超时，最大时长{timeout}秒。",
                    "tool_calls": None,
                    "role": "assistant",
                    "finish_reason": "error",
                }
            except httpx.HTTPStatusError as e:
                return {
                    "content": f"API HTTP错误 ({e.response.status_code}): {e.response.text[:200]}",
                    "tool_calls": None,
                    "role": "assistant",
                    "finish_reason": "error",
                }
            except Exception as e:
                return {
                    "content": f"调用DeepSeek API时出错: {str(e)}",
                    "tool_calls": None,
                    "role": "assistant",
                    "finish_reason": "error",
                }

    # ── Usage Metering ────────────────────────────────────────────

    def _record_usage(self, result: dict, purpose: str):
        """Record token usage (input/output/cached-hit/miss) to the ledger.

        Never raises — metering must not affect the request path.
        """
        if token_ledger is None:
            return
        try:
            token_ledger.record(
                model=self.model, purpose=purpose, usage=extract_usage(result),
            )
        except Exception:
            pass

    # ── Response Parsing ──────────────────────────────────────────

    def _parse_response(self, result: dict) -> Dict[str, Any]:
        """Parse the API response into a structured dict.

        Handles both plain content responses and tool_call responses.
        """
        choice = result["choices"][0]
        message = choice["message"]
        finish_reason = choice.get("finish_reason", "stop")

        parsed = {
            "role": message.get("role", "assistant"),
            "content": message.get("content"),
            "finish_reason": finish_reason,
        }

        # Preserve reasoning_content for models that require it
        # (DeepSeek thinking mode, Qwen thinking mode, etc.)
        if message.get("reasoning_content"):
            parsed["reasoning_content"] = message["reasoning_content"]

        # Handle tool calls
        if message.get("tool_calls"):
            parsed["tool_calls"] = [
                {
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                for tc in message["tool_calls"]
            ]
        else:
            parsed["tool_calls"] = None

        return parsed

    # ── Helpers ───────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }


# Global singleton
try:
    deepseek_client = DeepSeekClient()
except Exception:
    deepseek_client = None  # NoneBot not initialized yet (e.g., during testing)
