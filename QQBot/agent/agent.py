"""
Agent Core — The main agent class implementing the Think→Act→Observe→Respond loop.

The agent reads its configuration from markdown files (SOUL.md, IDENTITY.md, etc.)
and uses a ToolRegistry + SessionManager + DeepSeekClient to process messages.
"""

import asyncio
import json
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .tool_registry import ToolRegistry
from .session import Session, SessionManager
from .memory import MemorySystem, MemoryEntry
from .profile import ProfileManager


class Agent:
    """LLM Agent with tool-calling capability.

    Configuration is loaded from markdown files in config_dir:
    - SOUL.md: Personality and behavior rules
    - IDENTITY.md: Name, version, capabilities
    - TOOLS.md: Tool definitions (documentation reference)
    - AGENTS.md: Orchestration and reasoning rules
    - BOOTSTRAP.md: Startup sequence
    - SESSION.md: Session configuration
    """

    # ── Construction ──────────────────────────────────────────────

    def __init__(
        self,
        deepseek_client,
        tool_registry: ToolRegistry,
        config_dir: str,
        session_manager: Optional[SessionManager] = None,
        memory_system: Optional[MemorySystem] = None,
        profile_manager: Optional[ProfileManager] = None,
        max_tool_iterations: int = 5,
        thinking_timeout: float = 180.0,
    ):
        self.client = deepseek_client
        self.tools = tool_registry
        self.config_dir = config_dir
        self.sessions = session_manager or SessionManager()
        self.memory = memory_system
        self.profiles = profile_manager
        self.max_tool_iterations = max_tool_iterations
        self.thinking_timeout = thinking_timeout

        # Load configs
        self._configs: Dict[str, str] = {}
        self._load_configs()

        # System prompt cache
        self._system_prompt: Optional[str] = None

    def _load_configs(self):
        """Load all markdown config files from config_dir."""
        config_files = [
            "SOUL.md",
            "IDENTITY.md",
            "TOOLS.md",
            "AGENTS.md",
            "BOOTSTRAP.md",
            "SESSION.md",
        ]
        for filename in config_files:
            filepath = os.path.join(self.config_dir, filename)
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    self._configs[filename.replace(".md", "").lower()] = f.read()

    # ── System Prompt ─────────────────────────────────────────────

    def build_system_prompt(self) -> str:
        """Construct the full system prompt from config files."""
        if self._system_prompt:
            return self._system_prompt

        parts = []

        # SOUL: personality and behavior
        if "soul" in self._configs:
            parts.append(self._configs["soul"])

        # IDENTITY: who the agent is
        if "identity" in self._configs:
            parts.append(self._configs["identity"])

        # AGENTS: orchestration rules
        if "agents" in self._configs:
            parts.append("# Orchestration Rules\n\n" + self._configs["agents"])

        # Current time context
        parts.append(f"\n## Current Context\n\nCurrent time: {time.strftime('%Y-%m-%d %H:%M:%S')}")

        self._system_prompt = "\n\n".join(parts)
        return self._system_prompt

    def reload_configs(self):
        """Reload config files and invalidate system prompt cache."""
        self._configs.clear()
        self._system_prompt = None
        self._load_configs()

    # ── Main Entry Point ──────────────────────────────────────────

    async def run(
        self,
        user_message: str,
        user_id: str,
        client=None,
        progress_callback: Optional[Callable[[str], Any]] = None,
    ) -> str:
        """Process a user message through the agent loop.

        Args:
            user_message: The text message from the user.
            user_id: Unique QQ user ID.
            client: Optional DeepSeekClient override for model routing.
                    When None, uses self.client (the default client).
            progress_callback: Optional async/sync callback to report
                               progress before each tool execution round.
                               Receives a short status string (e.g. "正在搜索...").

        Returns:
            The agent's final response string.
        """
        # Get or create session
        session = self.sessions.get_or_create(user_id)

        # Build messages: system prompt + history + current message
        messages = self._build_messages(session, user_message)

        # Track tool names for deduplication across iterations
        _last_reported_tools: Optional[frozenset] = None

        # Agent loop
        for iteration in range(self.max_tool_iterations):
            # THINK: Send to LLM (use routed client if provided)
            llm_client = client or self.client
            response = await llm_client.chat_completion_with_tools(
                messages=messages,
                tools=self.tools.get_schemas(),
                timeout=self.thinking_timeout,
            )

            # Check if the response contains tool calls
            if response.get("tool_calls"):
                # ── Report progress (with deduplication) ────────────
                if progress_callback:
                    tool_names = [tc["function"]["name"] for tc in response["tool_calls"]]
                    tool_set = frozenset(tool_names)
                    if tool_set != _last_reported_tools:
                        _last_reported_tools = tool_set
                        names = "、".join(tool_names)
                        if iteration >= 3:
                            msg = f"⏳ 第{iteration + 1}轮: 正在{names}..."
                        else:
                            msg = f"⏳ 正在{names}..."
                        try:
                            ret = progress_callback(msg)
                            if asyncio.iscoroutine(ret):
                                await ret
                        except Exception:
                            pass  # Progress failures must not break the agent

                # ACT: Execute tools
                tool_results = await self._execute_tool_calls(
                    response["tool_calls"], session
                )

                # Append assistant message (with tool_calls) to messages
                assistant_msg = {
                    "role": "assistant",
                    "content": response.get("content"),
                    "tool_calls": response["tool_calls"],
                }
                if response.get("reasoning_content"):
                    assistant_msg["reasoning_content"] = response["reasoning_content"]
                messages.append(assistant_msg)

                # OBSERVE: Append tool results
                for tr in tool_results:
                    messages.append(tr)

                continue  # Back to THINK

            else:
                # RESPOND: Final response, no tool calls
                final_content = response.get("content", "")

                # Update session
                session.add_message("user", user_message)
                session.add_message(
                    "assistant", final_content,
                    reasoning_content=response.get("reasoning_content"),
                )
                session.trim(self.sessions.max_context_messages)
                self.sessions.update(user_id, session)

                # Save substantive interactions to long-term memory
                await self._maybe_remember(user_id, user_message, final_content)

                # Fire background task: extract user facts → update profile
                self._schedule_profile_update(user_id, user_message, final_content)

                return final_content

        # Max iterations reached
        return "抱歉，Roxy 在尝试处理你的请求时似乎陷入了循环。请尝试换一种方式提问~"

    # ── Message Building ──────────────────────────────────────────

    def _build_messages(
        self, session: Session, user_message: str
    ) -> List[Dict[str, Any]]:
        """Build the full message list for the LLM.

        Structure:
        1. Global system prompt (SOUL + IDENTITY + AGENTS)
        2. User profile context (from ProfileManager, per user_id)
        3. Relevant long-term memories (keyword search from MemorySystem)
        4. Conversation history (from Session)
        5. Current user message
        """
        messages = []

        # 1. Global system prompt
        messages.append({
            "role": "system",
            "content": self.build_system_prompt(),
        })

        # 2. User profile context (injected into system role to save tokens)
        if self.profiles:
            profile = self.profiles.get(session.user_id)
            profile_context = profile.to_prompt_context()
            if profile_context:
                # Append to the same system message to keep the message count low
                system_text = messages[0]["content"] + "\n\n" + profile_context
                messages[0]["content"] = system_text

        # 3. Relevant long-term memories
        if self.memory:
            memories = self.memory.search(user_message)
            if memories:
                # Keep top 3 most relevant
                mem_lines = ["\n## Relevant Past Interactions"]
                for m in memories[:3]:
                    snippet = m.content[:200].replace("\n", " ")
                    mem_lines.append(f"- {m.description}: {snippet}")
                # Append to system message as well
                messages[0]["content"] += "\n".join(mem_lines)

        # 4. Conversation history
        messages.extend(session.context)

        # 5. Current user message
        messages.append({"role": "user", "content": user_message})

        return messages

    # ── Tool Execution ────────────────────────────────────────────

    async def _execute_tool_calls(
        self, tool_calls: List[dict], session: Session
    ) -> List[Dict[str, str]]:
        """Execute tool calls from the LLM response.

        Returns a list of tool result messages to append.
        """
        results = []
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            try:
                arguments = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                arguments = {}

            result_text = await self.tools.execute(tool_name, arguments)
            session.tool_call_count += 1

            results.append({
                "role": "tool",
                "tool_call_id": tc.get("id", f"call_{tool_name}"),
                "content": result_text,
            })

        return results

    # ── Memory ────────────────────────────────────────────────────

    async def _maybe_remember(
        self, user_id: str, user_message: str, agent_response: str
    ):
        """Conditionally save important interactions to long-term memory."""
        if not self.memory:
            return

        # Simple heuristic: save if the interaction seems substantive
        # (long messages, or containing certain patterns)
        combined_len = len(user_message) + len(agent_response)
        if combined_len > 300:
            summary = user_message[:100] + ("..." if len(user_message) > 100 else "")
            entry = MemoryEntry(
                name=f"interaction_{user_id}_{int(time.time())}",
                description=f"Conversation with {user_id}: {summary}",
                type="user",
                content=f"## User Message\n{user_message}\n\n## Agent Response\n{agent_response[:500]}",
            )
            self.memory.save(entry)

    # ── Profile Update ──────────────────────────────────────────────

    def _schedule_profile_update(
        self, user_id: str, user_message: str, agent_response: str
    ):
        """Schedule a background task to extract user facts and update profile."""
        if not self.profiles:
            return
        try:
            asyncio.create_task(
                self.profiles.extract_and_update(user_id, user_message, agent_response)
            )
        except RuntimeError:
            pass  # No running event loop (e.g., in tests)

    # ── Bootstrap ─────────────────────────────────────────────────

    async def bootstrap(self) -> Dict[str, Any]:
        """Run the bootstrap sequence defined in BOOTSTRAP.md.

        Returns a status dict with health check results.
        """
        status = {
            "agent": "initializing",
            "deepseek_api": "unknown",
            "tool_count": len(self.tools),
            "tools": self.tools.list_tools(),
            "configs_loaded": list(self._configs.keys()),
            "errors": [],
        }

        # Verify DeepSeek API
        try:
            test_response = await self.client.chat_completion(
                message="ping", timeout_set=30.0
            )
            status["deepseek_api"] = "healthy" if test_response else "degraded"
        except Exception as e:
            status["deepseek_api"] = "unreachable"
            status["errors"].append(f"DeepSeek API: {e}")

        if status["errors"]:
            status["agent"] = "degraded"
        else:
            status["agent"] = "healthy"

        return status

    # ── Utilities ─────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Get current agent status."""
        return {
            "agent": "running",
            "active_sessions": self.sessions.active_count(),
            "tools_registered": len(self.tools),
            "tool_names": self.tools.list_tools(),
            "config_dir": self.config_dir,
            "has_memory": self.memory is not None,
            "has_profile_manager": self.profiles is not None,
        }

    def clear_user_session(self, user_id: str):
        """Clear a user's conversation session."""
        self.sessions.clear_context(user_id)

    def cleanup(self):
        """Clean up expired sessions."""
        removed = self.sessions.cleanup_expired()
        return removed
