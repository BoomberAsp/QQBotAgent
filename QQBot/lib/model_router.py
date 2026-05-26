"""
Model Router — Multi-model orchestration for the QQBot agent.

Manages multiple DeepSeekClient instances (REASONING, FLASH, MULTIMODAL)
and routes tasks based on complexity classification. Inspired by Claude Code's
model tiering: lightweight model for triage/simple tasks, powerful model for
complex reasoning, vision model for image understanding.

Config is loaded from models_settings.json (git-ignored). When config is empty
or missing, all clients fall back to the default .env DeepSeek config.
"""

import json
import os
from typing import Dict, Optional

from .deepseek_client import DeepSeekClient


class ModelRouter:
    """Manages multiple LLM clients and routes tasks based on complexity.

    Loads model configurations from models_settings.json and creates
    dedicated DeepSeekClient instances for each model tier.
    """

    def __init__(self, config_path: str = None):
        if config_path is None:
            _project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_path = os.path.join(_project_dir, "config", "models_settings.json")
        self._config_path = config_path
        self._config = {}
        self._load_config()

        # Create clients for each model tier
        self._reasoning_client = self._create_client(self._config.get("REASONING_MODEL", {}))
        self._flash_client = self._create_client(self._config.get("FLASH_MODEL", {}))
        self._multimodal_client = self._create_client(self._config.get("MULTIMODAL_MODEL", {}))

        # Task routing config
        self._task_routing = self._config.get("task_routing", {})

    # ── Config Loading ───────────────────────────────────────────────

    def _load_config(self):
        """Load model config from JSON file. Never raises."""
        try:
            if not os.path.exists(self._config_path):
                self._config = {}
                return
            with open(self._config_path, "r", encoding="utf-8") as f:
                self._config = json.load(f)
        except (json.JSONDecodeError, IOError, OSError):
            self._config = {}

    def _create_client(self, model_config: dict) -> Optional[DeepSeekClient]:
        """Create a DeepSeekClient from a model config section.

        Returns None if api_key and api_base are both empty (will fall back
        to the default .env config on access).
        """
        api_key = model_config.get("api_key", "")
        api_base = model_config.get("api_base", "")
        model = model_config.get("model", "")

        if not api_key and not api_base:
            return None  # Caller will fall back

        try:
            return DeepSeekClient(api_key=api_key, api_base=api_base, model=model)
        except Exception:
            return None

    # ── Client Access ─────────────────────────────────────────────────

    @property
    def reasoning_client(self) -> DeepSeekClient:
        """The primary reasoning model for complex tasks."""
        if self._reasoning_client is not None:
            return self._reasoning_client
        return self._default_client()

    @property
    def flash_client(self) -> DeepSeekClient:
        """The lightweight model for triage and simple tasks."""
        if self._flash_client is not None:
            return self._flash_client
        return self._default_client()

    @property
    def multimodal_client(self) -> DeepSeekClient:
        """The vision model for image understanding."""
        if self._multimodal_client is not None:
            return self._multimodal_client
        return self._default_client()

    def _default_client(self) -> DeepSeekClient:
        """Fallback client using .env config. Created once and cached."""
        if not hasattr(self, "_default"):
            try:
                self._default = DeepSeekClient()
            except Exception:
                self._default = DeepSeekClient()
        return self._default

    # ── Routing ───────────────────────────────────────────────────────

    def get_client(self, task_type: str) -> DeepSeekClient:
        """Get the appropriate client for a task type.

        Args:
            task_type: One of "triage", "simple", "complex", "multimodal".

        Returns:
            The DeepSeekClient for that task type.
        """
        routing_map = {
            "triage": self.flash_client,
            "simple": self.flash_client,
            "complex": self.reasoning_client,
            "multimodal": self.multimodal_client,
        }
        return routing_map.get(task_type, self.reasoning_client)

    # ── Complexity Classification ──────────────────────────────────────

    async def classify_complexity(self, user_message: str) -> str:
        """Classify a user message as 'simple' or 'complex'.

        Uses the FLASH_MODEL (or fallback) with a lightweight prompt to
        determine whether the message needs the full reasoning model.

        Args:
            user_message: The user's message text.

        Returns:
            "simple" or "complex". Falls back to "complex" on any error
            (safety: better to overthink than underthink).
        """
        triage_prompt = self._task_routing.get(
            "triage_prompt",
            "请判断以下用户消息的复杂度，只回复一个词'simple'或'complex'。\n\n"
            "判断标准：\n"
            "- simple: 简单问候、闲聊、已知事实询问、无需工具调用的常识问题\n"
            "- complex: 需要搜索、需要执行代码、需要读取文件、需要多步推理、涉及专业知识\n\n"
            "用户消息: {user_message}\n\n复杂度:",
        )

        prompt = triage_prompt.replace("{user_message}", user_message)

        try:
            client = self.get_client("triage")
            result = await client.chat_completion(
                message=prompt,
                timeout_set=30.0,
            )
            # Extract the classification word
            result = result.strip().lower()
            if "complex" in result:
                return "complex"
            elif "simple" in result:
                return "simple"
            else:
                # Unrecognized response — default to complex for safety
                return "complex"
        except Exception:
            # Any error → complex (safety first)
            return "complex"

    # ── Status ─────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, str]:
        """Return a status summary of all configured models."""
        status = {}
        for name, client in [
            ("REASONING_MODEL", self._reasoning_client),
            ("FLASH_MODEL", self._flash_client),
            ("MULTIMODAL_MODEL", self._multimodal_client),
        ]:
            if client is not None:
                status[name] = f"{client.model} @ {client.api_base}"
            else:
                status[name] = "using default (.env config)"
        return status


# Global singleton
try:
    model_router = ModelRouter()
except Exception:
    model_router = ModelRouter()
