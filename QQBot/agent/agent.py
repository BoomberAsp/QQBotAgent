"""
Agent Core — The main agent class implementing the Think→Act→Observe→Respond loop.

The agent reads its configuration from markdown files (SOUL.md, IDENTITY.md, etc.)
and uses a ToolRegistry + SessionManager + DeepSeekClient to process messages.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .tool_registry import ToolRegistry
from .session import Session, SessionManager
from .memory import MemorySystem, TieredMemory
from .profile import ProfileManager
from .hardware import HardwareDetector, HardwareProfile
from .workspace import UserWorkspaceManager
from .special_session import SpecialSessionManager, SpecialSession
from .task_record import build_auto_record, build_compact_line, append_task_log


# P1: long-term memory is now the three-tier engine (TieredMemory), driven
# entirely through ProfileManager extraction. The old _maybe_remember raw-dump
# path (MIN_REMEMBER_LEN / MAX_MEMORIES_PER_USER) is removed.

# TaskRecord auto-compression: a turn that used one of the known multi-turn task
# tools and whose final response exceeds AUTO_COMPRESS_MIN_LEN is folded into a
# degraded TaskRecord (unless finalize_subtask already produced an explicit
# record). Scoped to these tools so ordinary long answers (search, character
# lookup, …) are left intact. Full detail stays in the task log.
AUTO_COMPRESS_MIN_LEN = 600
_AUTO_COMPRESS_TOOLS = {"gacha_pull", "parse_battle_screenshots", "calculate_speed"}

# A task fold may remove at most this many trailing messages (the setup/result
# turns of one short flow). If the recorded boundary would remove more, it is
# treated as stale/abandoned and the fold is skipped — a guard against an old
# begin_task window wiping unrelated history on a later finalize_subtask.
MAX_FOLD_MESSAGES = 8


def _display_tool_name(tool_call: dict) -> str:
    """Return a human-facing tool name for the progress message.

    ``parse_battle_screenshots`` is annotated with its mode (轻量/全量) so
    the user can tell the two runs apart.
    """
    name = tool_call["function"]["name"]
    if name != "parse_battle_screenshots":
        return name
    try:
        args = json.loads(tool_call["function"].get("arguments") or "{}")
    except (json.JSONDecodeError, TypeError):
        return name
    label = "轻量" if args.get("mode") == "light" else "全量"
    return f"{name}（{label}）"


class Agent:
    """LLM Agent with tool-calling capability.

    Configuration is loaded from markdown files in config_dir:
    - SOUL.md: Personality and behavior rules
    - IDENTITY.md: Name, version, capabilities
    - TOOLS.md: Tool definitions (documentation reference)
    - AGENTS.md: Orchestration and reasoning rules
    - BOOTSTRAP.md: Startup sequence
    - SESSION.md: Session configuration
    - MEMORY.md: Three-tier long-term memory rules (injected into the prompt)
    """

    # ── Construction ──────────────────────────────────────────────

    def __init__(
        self,
        deepseek_client,
        tool_registry: ToolRegistry,
        config_dir: str,
        session_manager: Optional[SessionManager] = None,
        memory_system: Optional[MemorySystem] = None,
        tiered_memory: Optional[TieredMemory] = None,
        profile_manager: Optional[ProfileManager] = None,
        hardware_detector: Optional[HardwareDetector] = None,
        workspace_manager: Optional[UserWorkspaceManager] = None,
        special_session_manager: Optional[SpecialSessionManager] = None,
        max_tool_iterations: int = 5,
        thinking_timeout: float = 180.0,
    ):
        self.client = deepseek_client
        self.tools = tool_registry
        self.config_dir = config_dir
        self.sessions = session_manager or SessionManager()
        self.memory = memory_system
        self.tiered_memory = tiered_memory  # P1 three-tier engine (MEDIUM injection)
        self.profiles = profile_manager
        self.hardware_detector = hardware_detector
        self.hardware: Optional[HardwareProfile] = None
        self.workspaces = workspace_manager
        self.special_sessions = special_session_manager
        self.max_tool_iterations = max_tool_iterations
        self.thinking_timeout = thinking_timeout

        # Audit logging
        self._audit_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "audit",
        )

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
            "MEMORY.md",
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

        # MEMORY: three-tier long-term memory rules (P1, Decision K — wired in)
        if "memory" in self._configs:
            parts.append(self._configs["memory"])

        # NOTE (Cache-Hit-Rate-Plan.md Phase 3, B6): the "Current time" block
        # used to live here — but this prompt is cached in _system_prompt, so
        # the timestamp froze at first build (startup time) and went stale.
        # The real per-turn time now lives in the L4 volatile tail message
        # built by _build_messages(), AFTER the conversation history, where it
        # cannot invalidate the provider-side prefix cache.

        # Hardware context (dynamically detected, replaces hardcoded WORKSPACE.md §4)
        if self.hardware:
            parts.append(self.hardware.get_prompt_context())
            parts.append(self.hardware.get_task_refusal_context())

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
        session_type: str = "temporary",
        allowed_tools: Optional[set] = None,
        user_role: Optional[str] = None,
    ) -> str:
        """Process a user message through the agent loop.

        Args:
            user_message: The text message from the user.
            user_id: Unique QQ user ID.
            client: Optional DeepSeekClient override for model routing.
                    When None, uses self.client (the default client).
            progress_callback: Optional async/sync callback to report
                               progress before each tool execution round.
            session_type: "temporary", "special", or "continuous".

        Returns:
            The agent's final response string.
        """
        # Determine which session to use
        if session_type == "special" and self.special_sessions:
            special_session = self.special_sessions.get_active(user_id)
        else:
            special_session = None

        # Get or create temporary session (always — used as fallback)
        session = self.sessions.get_or_create(user_id)

        # Build messages: system prompt + history + current message
        messages = self._build_messages(session, user_message, special_session, role_hint=user_role)

        # Track tool names for deduplication across iterations
        _last_reported_tools: Optional[frozenset] = None

        # Track repeated tool failures to break infinite retry loops.
        # Key: (tool_name, hash(arguments_json)) -> failure_count
        _recent_tool_failures: dict = {}

        # Collect this turn's tool calls (name/args/success/snippet). Used by the
        # TaskRecord auto-compression fallback to build a degraded record when the
        # agent did not call finalize_subtask explicitly.
        _turn_tool_calls: list = []

        # Reset the task-fold directive; finalize_subtask may set it during this run.
        from agent.context import _pending_task_fold
        _pending_task_fold.set(None)

        # Agent loop
        for iteration in range(self.max_tool_iterations):
            llm_client = client or self.client
            schemas = (
                self.tools.get_schemas_for(allowed_tools)
                if allowed_tools
                else self.tools.get_schemas()
            )
            response = await llm_client.chat_completion_with_tools(
                messages=messages,
                tools=schemas,
                timeout=self.thinking_timeout,
                purpose="agent_loop",
            )

            if response.get("tool_calls"):
                # ── Report progress (with deduplication) ────────────
                if progress_callback:
                    tool_names = [_display_tool_name(tc) for tc in response["tool_calls"]]
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
                            pass

                tool_results = await self._execute_tool_calls(
                    response["tool_calls"], session, user_id, allowed_tools
                )

                # ── Collect this turn's tool calls (TaskRecord auto-fallback) ──
                _res_by_id = {
                    tr.get("tool_call_id"): tr.get("content", "") for tr in tool_results
                }
                for tc in response["tool_calls"]:
                    _tn = tc["function"]["name"]
                    try:
                        _args = json.loads(tc["function"].get("arguments", "{}") or "{}")
                    except Exception:
                        _args = {}
                    _res = _res_by_id.get(tc.get("id", f"call_{_tn}"), "")
                    _is_err = _res.startswith("[") and any(
                        _res.startswith(p) for p in (
                            "[Git Error]", "[Search", "[WebFetch", "[Code Error",
                            "[Shell", "[Security", "[SSRF", "[PDF Error]",
                        )
                    )
                    _turn_tool_calls.append({
                        "name": _tn,
                        "args": _args,
                        "success": not _is_err,
                        "snippet": _res[:200].replace("\n", " "),
                    })

                assistant_msg = {
                    "role": "assistant",
                    "content": response.get("content"),
                    "tool_calls": response["tool_calls"],
                }
                if response.get("reasoning_content"):
                    assistant_msg["reasoning_content"] = response["reasoning_content"]
                messages.append(assistant_msg)

                for tr in tool_results:
                    messages.append(tr)

                # ── Detect repeated tool failures (anti-infinite-loop) ──
                _error_prefixes = (
                    "[Git Error]", "[Search", "[WebFetch", "[Code Error",
                    "[Shell", "[Security", "[SSRF", "[PDF Error",
                )
                for tc in response["tool_calls"]:
                    tn = tc["function"]["name"]
                    args_str = tc["function"].get("arguments", "{}")
                    try:
                        import hashlib
                        args_hash = hashlib.md5(args_str.encode()).hexdigest()[:12]
                    except Exception:
                        args_hash = str(hash(args_str))
                    key = (tn, args_hash)

                    # Find the corresponding tool result
                    call_id = tc.get("id", f"call_{tn}")
                    result_text = ""
                    for tr in tool_results:
                        if tr.get("tool_call_id") == call_id:
                            result_text = tr.get("content", "")
                            break

                    is_error = result_text.startswith("[") and any(
                        result_text.startswith(prefix) for prefix in _error_prefixes
                    )
                    if is_error:
                        _recent_tool_failures[key] = _recent_tool_failures.get(key, 0) + 1
                        if _recent_tool_failures[key] >= 2:
                            messages.append({
                                "role": "system",
                                "content": (
                                    f"⚠️ 工具 '{tn}' 对相同参数的调用已连续失败 "
                                    f"{_recent_tool_failures[key]} 次。"
                                    f"请停止重试该操作，向用户说明失败原因并建议替代方案。"
                                ),
                            })
                    else:
                        # Success resets the counter for this tool+args
                        _recent_tool_failures.pop(key, None)

                continue

            else:
                # RESPOND: Final response
                final_content = response.get("content", "")
                reasoning = response.get("reasoning_content")

                # ── Persist to session (TaskRecord-aware) ────────
                # Three modes:
                #  1. Explicit finalize_subtask → fold the task's setup turns and
                #     persist the compact structured record instead of raw content.
                #  2. Auto-compression fallback → tool-heavy + long response with no
                #     explicit record; build a degraded record and persist its line.
                #  3. Normal → persist user message + full final response.
                fold = _pending_task_fold.get()
                if fold is not None:
                    line = fold.get("line", "")
                    boundary = fold.get("boundary")
                    if special_session:
                        # Snapshot storage: skip setup-turn removal, fold this turn only.
                        self.special_sessions.add_message(user_id, "user", user_message)
                        self.special_sessions.add_message(user_id, "assistant", line)
                    else:
                        if boundary is not None:
                            b = max(0, min(int(boundary), len(session.context)))
                            # Stale-window guard: a fold may only remove up to
                            # MAX_FOLD_MESSAGES trailing messages. If the recorded
                            # boundary would wipe more (e.g. an abandoned begin_task
                            # finalized much later), skip setup-turn removal and just
                            # append the record — better to keep redundant setup turns
                            # than to destroy unrelated history.
                            if (len(session.context) - b) <= MAX_FOLD_MESSAGES:
                                session.context = session.context[:b]
                        session.add_message("user", user_message)
                        session.add_message("assistant", line)
                        session.trim(self.sessions.max_context_messages)
                        self.sessions.update(user_id, session)
                elif (
                    any(tc.get("name") in _AUTO_COMPRESS_TOOLS for tc in _turn_tool_calls)
                    and len(final_content) > AUTO_COMPRESS_MIN_LEN
                ):
                    record = build_auto_record(user_message, final_content, _turn_tool_calls)
                    append_task_log(user_id, record)
                    line = build_compact_line(record)
                    if special_session:
                        self.special_sessions.add_message(user_id, "user", user_message)
                        self.special_sessions.add_message(user_id, "assistant", line)
                    else:
                        session.add_message("user", user_message)
                        session.add_message("assistant", line)
                        session.trim(self.sessions.max_context_messages)
                        self.sessions.update(user_id, session)
                else:
                    if special_session:
                        self.special_sessions.add_message(user_id, "user", user_message)
                        self.special_sessions.add_message(
                            user_id, "assistant", final_content, reasoning,
                        )
                    else:
                        session.add_message("user", user_message)
                        session.add_message("assistant", final_content, reasoning_content=reasoning)
                        session.trim(self.sessions.max_context_messages)
                        self.sessions.update(user_id, session)

                # Fire background task: extract user facts + judge memory → profile.
                # P1: the three-tier memory engine is driven entirely through
                # ProfileManager.observe_turn (merged extract+judge). The old
                # _maybe_remember raw-dump path is removed.
                self._schedule_profile_update(user_id, user_message, final_content)

                return final_content

        # Max iterations reached
        return f"抱歉，Roxy 在尝试处理你的请求时似乎陷入了循环或工具调用次数已经超过当前配额上限（{self.max_tool_iterations}次）。请尝试换一种方式提问~"

    # ── Message Building ──────────────────────────────────────────

    def _build_messages(
        self,
        session: Session,
        user_message: str,
        special_session: Optional[SpecialSession] = None,
        role_hint: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Build the full message list for the LLM.

        Cache-friendly four-layer layout (Cache-Hit-Rate-Plan.md Phase 3) —
        ordered by volatility so the provider-side prefix cache survives
        across turns; everything that can change per-turn sits AFTER the
        history in an L4 tail system message:

        messages[0] (system):
          L1 全局静态   SOUL + IDENTITY + AGENTS + MEMORY + 硬件（所有用户共享）
          L2 人格/群    personality 块 + 群功能限制（同人格/同群共享）
          L3 用户级稳定 工作区路径 + 权限说明（同用户跨轮稳定）
        然后:  会话历史（append-only，Phase 4 迟滞修剪/阶梯压缩）
        然后:  L4 易变尾部（system）— 真实当前时间、特殊会话标记、quota 用量、
               profile 注入、MEDIUM 记忆注入、人格过渡提示
        最后:  当前用户消息
        """
        from agent.context import (
            _current_personality, _personality_transition, _current_group_context,
        )
        from agent.personality import get_personality_manager

        user_id = special_session.user_id if special_session else session.user_id

        # ── L1: global static system prompt (identical for every user) ──
        system_content = self.build_system_prompt()

        # ── L2: personality (stable per user/group). Appended AFTER L1 —
        # used to be prepended before it, which split the ~10k-token config
        # block into per-personality prefixes (B5). ──
        persona_name = _current_personality.get()
        if persona_name:
            pm = get_personality_manager()
            persona_content = pm.load(persona_name)
            if persona_content:
                system_content += "\n\n---\n\n" + persona_content

        # ── L2: group feature restrictions (stable per group) ──
        group_ctx = _current_group_context.get()
        if group_ctx:
            system_content += group_ctx

        # ── L3: workspace path (stable per user) ──
        if self.workspaces:
            workspace_path = self.workspaces.get_workspace(user_id)
            system_content += (
                f"\n\n## 用户工作区（独立隔离，仅该用户可访问）\n"
                f"路径: {workspace_path}\n"
                f"用户可以在工作区内存放持久化文件、代码和输出。"
                f"子目录: code/（代码执行）、uploads/（上传文件）、output/（生成输出）、projects/（项目文件）。"
            )

        # ── L3: permission role context (stable per user; non-admin only) ──
        if role_hint and role_hint != "admin":
            system_content += (
                f"\n\n## 当前会话权限\n"
                f"你的工具列表已由系统根据当前用户身份自动过滤。"
                f"你只能看到和使用当前可用的工具。"
                f"如果用户的请求需要使用你无法访问的工具（如 shell 命令、网页抓取等），"
                f"请礼貌地说明当前权限不支持此操作，并建议用户联系管理员获取更高权限。"
            )

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_content},
        ]

        # ── Conversation history (append-only cached prefix) ──
        if special_session:
            # Special session: full untrimmed context with layered compression
            messages.extend(self._compress_context(special_session.context))
        else:
            # Temporary session: trimmed context
            messages.extend(session.context)

        # ── L4: volatile tail, rebuilt every turn. Lives AFTER the history
        # so changes here (time / session marker / quota / profile / memory)
        # never invalidate the cached system+history prefix (B1/B2/B6). ──
        volatile: List[str] = [
            f"## 当前轮次上下文\n\nCurrent time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]

        # Personality transition notice (switch without clearing history)
        transition_note = _personality_transition.get()
        if transition_note:
            volatile.append(transition_note)

        # Special session marker (message count changes every turn)
        if special_session:
            volatile.append(
                f"## 特殊会话模式\n"
                f"当前会话名称: {special_session.name}\n"
                f"会话消息数: {special_session.total_messages}\n"
                f"会话创建于: {time.strftime('%Y-%m-%d %H:%M', time.localtime(special_session.created_at))}\n"
                f"你处于特殊会话模式，拥有完整的对话上下文记忆。"
                f"如果任务已完成，可以建议用户使用 /结束会话 退出特殊会话模式。"
            )

        # Workspace quota usage (changes whenever the user writes files)
        if self.workspaces:
            quota_ctx = self.workspaces.get_quota_context(user_id)
            if quota_ctx:
                volatile.append(quota_ctx)

        # User profile context (changes after each extraction batch)
        if self.profiles:
            profile = self.profiles.get(user_id)
            profile_context = profile.to_prompt_context()
            if profile_context:
                volatile.append(profile_context)

        # MEDIUM-tier memory injection (P1 three-tier engine; changes as
        # memories are reinforced/updated). NOTE: legacy shared knowledge/
        # system memories are no longer injected here (out of P1 scope,
        # Decision 8 — audit prod knowledge/ + system/ before deploy).
        # LONG-index injection is P2.
        if self.tiered_memory:
            medium_block = self.tiered_memory.build_medium_injection(user_id)
            if medium_block:
                volatile.append(medium_block)

        messages.append({"role": "system", "content": "\n\n".join(volatile)})

        # ── Current user message ──
        messages.append({"role": "user", "content": user_message})

        return messages

    # ── Context Compression ────────────────────────────────────────

    # Compression boundary advances in steps of this many messages
    # (Cache-Hit-Rate-Plan.md Phase 4, B4): re-deciding the boundary every
    # turn moves the divergence point through the history each time and
    # kills the provider prefix cache. Stepping keeps the compressed head
    # byte-identical for COMPRESS_STEP consecutive turns.
    COMPRESS_STEP = 4

    @classmethod
    def _compress_context(cls, context: List[Dict], recent_full: int = 20) -> List[Dict]:
        """Compress older tool results in context to save tokens.

        Layer 1 (tail window): keep full original.
        Layer 2 (head): compress tool results to first line only.
        Layer 3: Progressive summary not yet implemented — all messages
                before Layer 1 are kept but with compressed tool results.

        Cache-friendly stepping: the number of compressed head messages is
        ``floor((len - recent_full) / COMPRESS_STEP) * COMPRESS_STEP`` — the
        boundary only advances once every COMPRESS_STEP new messages, so the
        compressed prefix stays byte-stable between steps (the tail window
        temporarily holds up to recent_full + COMPRESS_STEP - 1 full messages,
        which are billed at cache-hit price).
        """
        n_compress = (
            (len(context) - recent_full) // cls.COMPRESS_STEP * cls.COMPRESS_STEP
        )
        if n_compress <= 0:
            return list(context)

        compressed = []
        for i, msg in enumerate(context):
            if i >= n_compress:
                # Layer 1: keep as-is
                compressed.append(msg)
            elif msg.get("role") == "tool":
                # Layer 2: compress tool results
                content = msg.get("content", "")
                first_line = content.split("\n")[0][:200]
                compressed.append({
                    "role": "tool",
                    "tool_call_id": msg.get("tool_call_id", ""),
                    "content": first_line + ("..." if len(content) > 200 else ""),
                })
            else:
                compressed.append(msg)

        return compressed

    # ── Tool Execution ────────────────────────────────────────────

    async def _execute_tool_calls(
        self, tool_calls: List[dict], session: Session, user_id: str = "",
        allowed_tools: Optional[set] = None,
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

            # Hard-reject: defense-in-depth against disallowed tools
            if allowed_tools is not None and tool_name not in allowed_tools:
                result_text = (
                    f"[Permission] 工具 '{tool_name}' 超出当前用户权限范围，"
                    f"此调用已被系统拦截。如果你确实需要此功能，请联系管理员。"
                )
            else:
                result_text = await self.tools.execute(tool_name, arguments)

            session.tool_call_count += 1

            # Audit log: record every tool invocation (including blocked ones)
            self._write_audit_log(user_id, tool_name, arguments, result_text)

            results.append({
                "role": "tool",
                "tool_call_id": tc.get("id", f"call_{tool_name}"),
                "content": result_text,
            })

        return results

    # ── Audit Logging ────────────────────────────────────────────

    def _write_audit_log(
        self, user_id: str, tool_name: str,
        arguments: dict, result_text: str,
    ):
        """Write a JSONL audit log entry for a tool invocation.

        Each entry records: timestamp, user_id, tool_name, arguments,
        success/error status, and a result summary (first 200 chars).
        Logs are written to daily files under data/audit/.
        """
        try:
            os.makedirs(self._audit_dir, exist_ok=True)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            log_path = os.path.join(self._audit_dir, f"tool_calls_{today}.jsonl")

            # Determine if the tool returned an error
            is_error = result_text.startswith("[") and any(
                result_text.startswith(f"[{prefix}]") or result_text.startswith(f"[{prefix} ")
                for prefix in ["Search", "WebFetch", "Shell", "Code Error",
                               "Security", "PDF Error", "Git Error", "SSRF"]
            )

            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "user_id": user_id,
                "tool": tool_name,
                "arguments": arguments,
                "success": not is_error,
                "result_summary": result_text[:200].replace("\n", " "),
            }

            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # Audit logging must never break the main flow

    # ── Profile Update ──────────────────────────────────────────────

    def _schedule_profile_update(
        self, user_id: str, user_message: str, agent_response: str
    ):
        """Observe one turn for profile extraction (Layer 3 batching, §7.1).

        observe_turn is SYNCHRONOUS: it buffers the turn in memory and, once the
        per-user buffer reaches PROFILE_BATCH_K, schedules a single-flight
        background extract_batch (flash model). There is no per-turn LLM call
        any more (that was the old extract_and_update path). Never raises into
        the response path.
        """
        if not self.profiles:
            return
        try:
            self.profiles.observe_turn(user_id, user_message, agent_response)
        except Exception:
            pass  # Profile extraction must never break the response path

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
            "hardware": None,
            "errors": [],
        }

        # Hardware detection (before API check — doesn't need network)
        if self.hardware_detector:
            try:
                self.hardware = self.hardware_detector.load_or_detect()
                status["hardware"] = {
                    "cpu_cores": self.hardware.cpu_cores,
                    "memory_gb": self.hardware.memory_gb,
                    "disk_system_gb": self.hardware.disk_system_gb,
                    "disk_data_gb": self.hardware.disk_data_gb,
                    "has_gpu": self.hardware.has_gpu,
                    "detected_at": self.hardware.detected_at,
                }
                # Invalidate system prompt cache so hardware info is included
                self._system_prompt = None
            except Exception as e:
                status["errors"].append(f"Hardware detection: {e}")

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
            "has_tiered_memory": self.tiered_memory is not None,
            "has_profile_manager": self.profiles is not None,
        }

    def clear_user_session(self, user_id: str):
        """Clear a user's conversation session."""
        self.sessions.clear_context(user_id)

    def cleanup(self):
        """Clean up expired sessions."""
        removed = self.sessions.cleanup_expired()
        return removed
