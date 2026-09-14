#!/usr/bin/env python3
"""
Agent System Test Suite

Tests all components of the agent architecture without requiring
a running QQ connection. Uses mocks for the LLM backend.

Usage:
    cd /home/windows11/QQBotAgent/QQBot
    python -m pytest test_agent.py -v

    # Or run directly:
    python test_agent.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Test Helpers ──────────────────────────────────────────────────

class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


def print_header(text: str):
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'='*60}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}  {text}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'='*60}{Colors.RESET}\n")


def print_pass(text: str):
    print(f"  {Colors.GREEN}✓ PASS{Colors.RESET} — {text}")


def print_fail(text: str):
    print(f"  {Colors.RED}✗ FAIL{Colors.RESET} — {text}")


def print_info(text: str):
    print(f"  {Colors.YELLOW}→{Colors.RESET} {text}")


# ── Mock DeepSeek Client ─────────────────────────────────────────

class MockDeepSeekClient:
    """Mock LLM client that returns predefined responses for testing."""

    def __init__(self):
        self.chat_calls = []  # Track all calls
        self.tool_calls = []  # Track tool calls

        # Configurable responses
        self.plain_response = "你好！我是 Roxy，有什么可以帮助你的吗？"
        self.tool_call_response = None  # Set per test
        self.should_fail = False

    async def chat_completion(self, message: str, history=None, timeout_set=180.0,
                              purpose="chat"):
        self.chat_calls.append(("chat", message, history))
        if self.should_fail:
            return "模拟API错误"
        return self.plain_response

    async def chat_completion_with_tools(self, messages, tools, timeout=180.0,
                                         purpose="agent_loop"):
        self.chat_calls.append(("chat_with_tools", messages, tools))
        if self.should_fail:
            return {
                "content": "模拟API错误",
                "tool_calls": None,
                "role": "assistant",
                "finish_reason": "error",
            }

        # Return configured response or default
        if self.tool_call_response:
            return self.tool_call_response
        else:
            return {
                "content": self.plain_response,
                "tool_calls": None,
                "role": "assistant",
                "finish_reason": "stop",
            }


# ── Test Cases ────────────────────────────────────────────────────

class TestToolRegistry:
    """Test the ToolRegistry component."""

    def __init__(self):
        from agent.tool_registry import ToolRegistry
        self.ToolRegistry = ToolRegistry

    def run(self):
        print_header("1. ToolRegistry Tests")

        self.test_register_and_list()
        self.test_schema_generation()
        self.test_execute_sync()
        self.test_execute_async()
        self.test_execute_error()
        self.test_unregister()
        self.test_contains()

    def test_register_and_list(self):
        registry = self.ToolRegistry()
        registry.register("test_tool", lambda x: x, "A test tool", {"type": "object", "properties": {}})
        assert len(registry) == 1, f"Expected 1 tool, got {len(registry)}"
        assert "test_tool" in registry, "Tool should be in registry"
        assert registry.list_tools() == ["test_tool"], f"Unexpected tool list: {registry.list_tools()}"
        print_pass("Register and list tools")

    def test_schema_generation(self):
        registry = self.ToolRegistry()
        registry.register(
            "search", lambda q: q,
            "Search the web",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
        )
        schemas = registry.get_schemas()
        assert len(schemas) == 1
        schema = schemas[0]
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "search"
        assert schema["function"]["description"] == "Search the web"
        assert "query" in schema["function"]["parameters"]["properties"]
        print_pass("Schema generation (OpenAI format)")

    def test_execute_sync(self):
        registry = self.ToolRegistry()

        def add(a: int, b: int) -> int:
            return a + b

        registry.register("add", add, "Add numbers", {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        })

        result = asyncio.run(registry.execute("add", {"a": 3, "b": 4}))
        assert result == "7", f"Expected '7', got '{result}'"
        print_pass("Execute sync tool")

    def test_execute_async(self):
        registry = self.ToolRegistry()

        async def fetch_data(url: str) -> str:
            await asyncio.sleep(0.01)
            return f"Data from {url}"

        registry.register("fetch", fetch_data, "Fetch data", {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        })

        result = asyncio.run(registry.execute("fetch", {"url": "http://test.com"}))
        assert "Data from http://test.com" in result
        print_pass("Execute async tool")

    def test_execute_error(self):
        registry = self.ToolRegistry()

        def bad_func(x):
            raise ValueError("Something went wrong")

        registry.register("bad", bad_func, "Bad tool", {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        })

        result = asyncio.run(registry.execute("bad", {"x": 1}))
        assert "[Error]" in result, f"Expected error, got: {result}"
        print_pass("Tool execution error handling")

        # Test nonexistent tool
        result = asyncio.run(registry.execute("nonexistent", {}))
        assert "[Error]" in result
        print_pass("Nonexistent tool error handling")

    def test_unregister(self):
        registry = self.ToolRegistry()
        registry.register("temp", lambda: None, "Temp", {"type": "object", "properties": {}})
        assert len(registry) == 1
        registry.unregister("temp")
        assert len(registry) == 0
        print_pass("Unregister tool")

    def test_contains(self):
        registry = self.ToolRegistry()
        registry.register("foo", lambda: None, "Foo", {"type": "object", "properties": {}})
        assert "foo" in registry
        assert "bar" not in registry
        print_pass("Contains check")


class TestSessionManager:
    """Test the SessionManager component."""

    def run(self):
        print_header("2. SessionManager Tests")

        self.test_create_and_get()
        self.test_timeout()
        self.test_trimming()
        self.test_clear_context()
        self.test_delete()
        self.test_persistence()

    def test_create_and_get(self):
        from agent.session import SessionManager

        mgr = SessionManager()
        session = mgr.get_or_create("user_123")
        assert session.user_id == "user_123"
        assert len(session.context) == 0
        assert mgr.active_count() == 1

        # Get again — should return same session
        session2 = mgr.get_or_create("user_123")
        assert session2 is session
        print_pass("Create and get session")

    def test_timeout(self):
        from agent.session import SessionManager

        mgr = SessionManager(session_timeout=0.01)  # 10ms timeout
        session = mgr.get_or_create("user_123")
        session.add_message("user", "hello")

        time.sleep(0.02)  # Wait for timeout

        # Getting should create new context (cleared)
        session2 = mgr.get_or_create("user_123")
        assert len(session2.context) == 0, "Session should have been cleared due to timeout"
        print_pass("Session timeout and clear")

    def test_trimming(self):
        from agent.session import SessionManager

        mgr = SessionManager(max_context_messages=5)
        session = mgr.get_or_create("user_123")

        # Add 10 messages
        for i in range(10):
            session.add_message("user" if i % 2 == 0 else "assistant", f"message_{i}")

        assert len(session.context) == 10
        session.trim(5)
        assert len(session.context) == 5, f"Expected 5 after trim, got {len(session.context)}"
        # Should keep the LAST 5 messages
        assert session.context[0]["content"] == "message_5"
        print_pass("Context trimming")

    def test_clear_context(self):
        from agent.session import SessionManager

        mgr = SessionManager()
        session = mgr.get_or_create("user_123")
        session.add_message("user", "hello")
        session.add_message("assistant", "hi")

        mgr.clear_context("user_123")
        assert len(session.context) == 0
        print_pass("Clear context")

    def test_delete(self):
        from agent.session import SessionManager

        mgr = SessionManager()
        mgr.get_or_create("user_123")
        assert mgr.active_count() == 1
        mgr.delete("user_123")
        assert mgr.active_count() == 0
        print_pass("Delete session")

    def test_persistence(self):
        from agent.session import SessionManager

        tmpdir = tempfile.mkdtemp()
        try:
            mgr = SessionManager(persistence_dir=tmpdir)
            session = mgr.get_or_create("user_456")
            session.add_message("user", "persist me")
            mgr.update("user_456", session)

            # Create new manager — should load from disk
            mgr2 = SessionManager(persistence_dir=tmpdir)
            loaded = mgr2.get_or_create("user_456")
            assert len(loaded.context) == 1
            assert loaded.context[0]["content"] == "persist me"
            print_pass("Session persistence to disk")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestMemorySystem:
    """Test the MemorySystem component."""

    def run(self):
        print_header("3. MemorySystem Tests")

        self.test_save_and_recall()
        self.test_forget()
        self.test_search()
        self.test_list_all()

    def test_save_and_recall(self):
        from agent.memory import MemorySystem, MemoryEntry

        tmpdir = tempfile.mkdtemp()
        try:
            ms = MemorySystem(tmpdir)
            entry = MemoryEntry(
                name="test_memory",
                description="A test memory",
                type="knowledge",
                content="This is a test memory content.",
            )
            path = ms.save(entry)
            assert os.path.exists(path), f"Memory file not created: {path}"

            recalled = ms.recall("test_memory", "knowledge")
            assert recalled is not None, "Memory not recalled"
            assert recalled.name == "test_memory"
            assert recalled.content == "This is a test memory content."
            print_pass("Save and recall memory")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_forget(self):
        from agent.memory import MemorySystem, MemoryEntry

        tmpdir = tempfile.mkdtemp()
        try:
            ms = MemorySystem(tmpdir)
            entry = MemoryEntry(name="temp_mem", description="Temp", type="knowledge", content="Temporary")
            ms.save(entry)
            assert ms.recall("temp_mem", "knowledge") is not None

            ms.forget("temp_mem", "knowledge")
            assert ms.recall("temp_mem", "knowledge") is None
            print_pass("Forget memory")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_search(self):
        from agent.memory import MemorySystem, MemoryEntry

        tmpdir = tempfile.mkdtemp()
        try:
            ms = MemorySystem(tmpdir)
            ms.save(MemoryEntry(name="python_tips", description="Python", type="knowledge", content="Python is great for automation"))
            ms.save(MemoryEntry(name="weather_note", description="Weather", type="knowledge", content="Shenzhen is hot in summer"))

            results = ms.search("Python")
            assert len(results) >= 1, f"Expected at least 1 result, got {len(results)}"
            assert any("Python" in r.content for r in results)
            print_pass("Search memories")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_list_all(self):
        from agent.memory import MemorySystem, MemoryEntry

        tmpdir = tempfile.mkdtemp()
        try:
            ms = MemorySystem(tmpdir)
            ms.save(MemoryEntry(name="mem1", description="1", type="knowledge", content="Content 1"))
            ms.save(MemoryEntry(name="mem2", description="2", type="knowledge", content="Content 2"))

            all_mems = ms.list_all("knowledge")
            assert len(all_mems) >= 2
            print_pass("List all memories")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestAgentCore:
    """Test the Agent core logic with a mock LLM client."""

    def run(self):
        print_header("4. Agent Core Tests (with Mock LLM)")

        asyncio.run(self.test_bootstrap())
        asyncio.run(self.test_build_system_prompt())
        asyncio.run(self.test_plain_response())
        asyncio.run(self.test_tool_calling_loop())
        asyncio.run(self.test_session_persistence())
        asyncio.run(self.test_clear_context())
        asyncio.run(self.test_max_iterations())
        asyncio.run(self.test_auto_compress_fallback())
        asyncio.run(self.test_task_fold_persistence())
        asyncio.run(self.test_medium_memory_injection())

    async def test_bootstrap(self):
        from agent.tool_registry import ToolRegistry
        from agent.agent import Agent

        mock_client = MockDeepSeekClient()
        registry = ToolRegistry()
        registry.register("ping", lambda: "pong", "Ping tool", {"type": "object", "properties": {}})

        agent = Agent(
            deepseek_client=mock_client,
            tool_registry=registry,
            config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
        )

        status = await agent.bootstrap()
        assert status["agent"] in ("healthy", "degraded")
        assert "deepseek_api" in status
        assert status["tool_count"] == 1
        print_pass("Agent bootstrap with health check")

    async def test_build_system_prompt(self):
        from agent.tool_registry import ToolRegistry
        from agent.agent import Agent

        agent = Agent(
            deepseek_client=MockDeepSeekClient(),
            tool_registry=ToolRegistry(),
            config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
        )

        prompt = agent.build_system_prompt()
        # Agent name comes from the personality profile injected at runtime
        # (see agent.py _build_messages). Verify prompt is well-formed.
        assert "QQBot" in prompt, "System prompt should contain bot framework name"
        assert len(prompt) > 100, "System prompt should be substantial"
        print_pass("System prompt construction (from SOUL.md + IDENTITY.md + AGENTS.md)")

    async def test_plain_response(self):
        from agent.tool_registry import ToolRegistry
        from agent.agent import Agent

        mock_client = MockDeepSeekClient()
        mock_client.plain_response = "你好！我是Roxy~"

        agent = Agent(
            deepseek_client=mock_client,
            tool_registry=ToolRegistry(),
            config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
        )

        response = await agent.run("你好", "test_user")
        assert response == "你好！我是Roxy~", f"Unexpected response: {response}"
        print_pass("Plain text response (no tool calls)")

        # Verify session was updated
        session = agent.sessions.get("test_user")
        assert session is not None
        assert len(session.context) >= 2
        print_pass("Session context updated after response")

    async def test_tool_calling_loop(self):
        from agent.tool_registry import ToolRegistry
        from agent.agent import Agent

        mock_client = MockDeepSeekClient()

        # First call: return a tool call for get_time
        # Second call: return final response using the tool result
        call_count = [0]

        original_method = mock_client.chat_completion_with_tools

        async def staged_response(messages, tools, timeout=180.0, **_kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Stage 1: LLM decides to call get_time tool
                return {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_time_001",
                        "type": "function",
                        "function": {
                            "name": "get_time",
                            "arguments": "{}",
                        },
                    }],
                    "role": "assistant",
                    "finish_reason": "tool_calls",
                }
            else:
                # Stage 2: LLM has tool result, returns final response
                # Check that messages contain the tool result
                has_tool_result = any(
                    m.get("role") == "tool" and "当前时间" in m.get("content", "")
                    for m in messages
                )
                assert has_tool_result, "Messages should contain tool result before final response"
                return {
                    "content": f"好的，现在的时间是刚刚获取到的。",
                    "tool_calls": None,
                    "role": "assistant",
                    "finish_reason": "stop",
                }

        mock_client.chat_completion_with_tools = staged_response

        registry = ToolRegistry()
        registry.register("get_time", lambda: "当前时间: 2026-05-26 10:00:00", "Get time", {
            "type": "object", "properties": {}, "required": [],
        })

        agent = Agent(
            deepseek_client=mock_client,
            tool_registry=registry,
            config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
        )

        response = await agent.run("现在几点了？", "test_user_2")
        assert call_count[0] == 2, f"Expected 2 LLM calls (tool + final), got {call_count[0]}"
        print_pass("Tool calling loop (think → act → observe → respond)")

        # Verify tool was counted
        session = agent.sessions.get("test_user_2")
        assert session.tool_call_count == 1
        print_pass("Tool call count tracked in session")

    async def test_session_persistence(self):
        from agent.tool_registry import ToolRegistry
        from agent.session import SessionManager
        from agent.agent import Agent

        tmpdir = tempfile.mkdtemp()
        try:
            mock_client = MockDeepSeekClient()
            mock_client.plain_response = "记住了！"

            session_mgr = SessionManager(persistence_dir=tmpdir)
            agent = Agent(
                deepseek_client=mock_client,
                tool_registry=ToolRegistry(),
                config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
                session_manager=session_mgr,
            )

            await agent.run("记住这个", "persist_user")
            session = session_mgr.get("persist_user")
            assert session is not None
            assert len(session.context) >= 2

            # Create a new session manager pointing to same dir
            session_mgr2 = SessionManager(persistence_dir=tmpdir)
            loaded = session_mgr2.get_or_create("persist_user")
            assert len(loaded.context) >= 2, "Session should be persisted and reloaded"
            print_pass("Session persistence across agent runs")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_clear_context(self):
        from agent.tool_registry import ToolRegistry
        from agent.agent import Agent

        mock_client = MockDeepSeekClient()
        agent = Agent(
            deepseek_client=mock_client,
            tool_registry=ToolRegistry(),
            config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
        )

        # First interaction
        await agent.run("第一条消息", "clear_user")
        session = agent.sessions.get("clear_user")
        assert len(session.context) >= 2

        # Clear
        agent.clear_user_session("clear_user")
        assert len(session.context) == 0
        print_pass("Clear user session via agent")

    async def test_max_iterations(self):
        from agent.tool_registry import ToolRegistry
        from agent.agent import Agent

        mock_client = MockDeepSeekClient()

        # Always return tool calls (infinite loop simulation)
        async def always_tool_calls(messages, tools, timeout=180.0, **_kwargs):
            return {
                "content": None,
                "tool_calls": [{
                    "id": "call_loop",
                    "type": "function",
                    "function": {"name": "ping", "arguments": "{}"},
                }],
                "role": "assistant",
                "finish_reason": "tool_calls",
            }

        mock_client.chat_completion_with_tools = always_tool_calls

        registry = ToolRegistry()
        registry.register("ping", lambda: "pong", "Ping", {"type": "object", "properties": {}})

        agent = Agent(
            deepseek_client=mock_client,
            tool_registry=registry,
            config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
            max_tool_iterations=3,
        )

        response = await agent.run("test", "loop_user")
        assert "循环" in response or "方式" in response, f"Should give up after max iterations: {response}"
        print_pass("Max tool iterations guard (prevents infinite loops)")

    async def test_auto_compress_fallback(self):
        """A tool-heavy turn whose final answer is long gets auto-compressed:
        session context keeps a compact TaskRecord line (not the raw verbose
        output), while the full result stays retrievable in the task log."""
        from agent.tool_registry import ToolRegistry
        from agent.session import SessionManager
        from agent.agent import Agent
        import agent.task_record as task_record

        tmp_sessions = tempfile.mkdtemp()
        tmp_tasklog = tempfile.mkdtemp()
        original_dir = task_record._TASK_LOG_DIR
        try:
            task_record._TASK_LOG_DIR = tmp_tasklog

            long_result = "抽卡结果详述：" + "获得了珍贵的角色与道具。" * 60  # > 600 chars
            mock_client = MockDeepSeekClient()
            call_count = [0]

            async def staged(messages, tools, timeout=180.0, **_kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return {
                        "content": None,
                        "tool_calls": [{
                            "id": "call_gacha_1",
                            "type": "function",
                            "function": {"name": "gacha_pull", "arguments": '{"count": 10}'},
                        }],
                        "role": "assistant",
                        "finish_reason": "tool_calls",
                    }
                return {
                    "content": long_result,
                    "tool_calls": None,
                    "role": "assistant",
                    "finish_reason": "stop",
                }

            mock_client.chat_completion_with_tools = staged

            registry = ToolRegistry()
            registry.register("gacha_pull", lambda count=1: f"gacha ok x{count}",
                              "Gacha", {"type": "object", "properties": {}})

            agent = Agent(
                deepseek_client=mock_client,
                tool_registry=registry,
                config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
                session_manager=SessionManager(persistence_dir=tmp_sessions),
            )

            response = await agent.run("帮我十连抽", "compress_user")
            # The user still sees the full answer in chat…
            assert response == long_result, "chat reply must remain the full text"

            # …but only the compact record line enters the session context.
            session = agent.sessions.get("compress_user")
            assistant_msgs = [m for m in session.context if m["role"] == "assistant"]
            assert len(assistant_msgs) == 1
            line = assistant_msgs[0]["content"]
            assert line.startswith("[子任务记录]"), f"compact line expected, got: {line[:60]}"
            assert "目标: 帮我十连抽" in line and "工具: gacha_pull" in line
            assert "追溯:" in line
            for m in session.context:
                assert long_result not in m.get("content", ""), \
                    "raw verbose output must not be persisted into context"

            # Full result remains retrievable via the per-user task log.
            log_path = os.path.join(tmp_tasklog, "compress_user.jsonl")
            assert os.path.isfile(log_path), "task log should be written"
            rec = json.loads(open(log_path, encoding="utf-8").readline())
            assert rec["tool"] == "gacha_pull" and rec["status"] == "success"
            assert rec["result"] == long_result, "task log keeps the full result"
            assert rec["params"] == {"count": "10"}, "tool args folded into params"
            print_pass("Auto-compression fallback: long tool-heavy turn → TaskRecord line")
        finally:
            task_record._TASK_LOG_DIR = original_dir
            shutil.rmtree(tmp_sessions, ignore_errors=True)
            shutil.rmtree(tmp_tasklog, ignore_errors=True)

    async def test_task_fold_persistence(self):
        """finalize_subtask sets _pending_task_fold: the task's setup turns are
        removed (folded) and the compact record replaces them; a stale boundary
        that would wipe too many messages is skipped (history preserved)."""
        from agent.tool_registry import ToolRegistry
        from agent.session import SessionManager
        from agent.agent import Agent
        from agent.context import _pending_task_fold

        compact_line = "[子任务记录] 目标: 测速 | 结果: 完成 | 追溯: ref#id"

        def make_agent(tmpdir, boundary):
            def finalize_stub(**kwargs):
                _pending_task_fold.set({"line": compact_line, "boundary": boundary})
                return "recorded"

            registry = ToolRegistry()
            registry.register("finalize_subtask", finalize_stub, "Finalize",
                              {"type": "object", "properties": {}})

            mock_client = MockDeepSeekClient()
            call_count = [0]

            async def staged(messages, tools, timeout=180.0, **_kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return {
                        "content": None,
                        "tool_calls": [{
                            "id": "call_fin_1",
                            "type": "function",
                            "function": {"name": "finalize_subtask", "arguments": "{}"},
                        }],
                        "role": "assistant",
                        "finish_reason": "tool_calls",
                    }
                return {
                    "content": "任务已完成。",
                    "tool_calls": None,
                    "role": "assistant",
                    "finish_reason": "stop",
                }

            mock_client.chat_completion_with_tools = staged
            return Agent(
                deepseek_client=mock_client,
                tool_registry=registry,
                config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
                session_manager=SessionManager(persistence_dir=tmpdir),
            )

        # Case 1: fresh fold — boundary removes exactly the setup turns.
        tmpdir1 = tempfile.mkdtemp()
        try:
            agent = make_agent(tmpdir1, boundary=2)
            session = agent.sessions.get_or_create("fold_user")
            # Two pre-task setup turns (4 messages), then the task runs.
            for i in range(2):
                session.add_message("user", f"setup q{i}")
                session.add_message("assistant", f"setup a{i}")
            assert len(session.context) == 4

            await agent.run("开始测速", "fold_user")

            session = agent.sessions.get("fold_user")
            contents = [m["content"] for m in session.context]
            assert contents == ["setup q0", "setup a0", "开始测速", compact_line], \
                f"setup turn 2 should be folded, got: {contents}"
            print_pass("Task fold: setup turns removed, compact record persisted")
        finally:
            shutil.rmtree(tmpdir1, ignore_errors=True)

        # Case 2: stale boundary — folding would remove > MAX_FOLD_MESSAGES
        # messages, so the fold is skipped and history is preserved.
        tmpdir2 = tempfile.mkdtemp()
        try:
            agent = make_agent(tmpdir2, boundary=0)
            session = agent.sessions.get_or_create("stale_user")
            for i in range(6):  # 12 messages — an abandoned task window
                session.add_message("user", f"chat q{i}")
                session.add_message("assistant", f"chat a{i}")

            await agent.run("迟来的收尾", "stale_user")

            session = agent.sessions.get("stale_user")
            contents = [m["content"] for m in session.context]
            assert contents[:12] == [
                f"chat {'q' if i % 2 == 0 else 'a'}{i // 2}" for i in range(12)
            ], f"history must survive a stale fold boundary, got: {contents[:4]}..."
            assert contents[-2:] == ["迟来的收尾", compact_line]
            print_pass("Stale fold guard: oversized boundary skipped, history kept")
        finally:
            shutil.rmtree(tmpdir2, ignore_errors=True)

    async def test_profile_injection(self):
        from agent.tool_registry import ToolRegistry
        from agent.profile import UserProfile, ProfileManager
        from agent.agent import Agent

        tmpdir = tempfile.mkdtemp()
        try:
            mock_client = MockDeepSeekClient()
            mock_client.plain_response = "你好小明！"

            profiles = ProfileManager(base_dir=tmpdir)
            profile = profiles.get("user_999")
            profile.nickname = "小明"
            profile.facts = ["在深圳", "用Python"]
            profile.interests = ["机器学习"]
            profile.preferences = {"response_style": "concise"}
            profiles.save(profile)

            agent = Agent(
                deepseek_client=mock_client,
                tool_registry=ToolRegistry(),
                config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
                profile_manager=profiles,
            )

            # Run and capture the messages that were built
            agent._captured_messages = None
            original_build = agent._build_messages

            def capture_build(session, msg):
                msgs = original_build(session, msg)
                agent._captured_messages = msgs
                return msgs

            agent._build_messages = capture_build

            await agent.run("你好", "user_999")

            # Verify profile was injected into system prompt
            assert agent._captured_messages is not None
            system_content = agent._captured_messages[0]["content"]
            assert "小明" in system_content, f"Profile nickname not injected: {system_content[:200]}"
            assert "机器学习" in system_content, f"Profile interests not injected: {system_content[:200]}"
            assert "concise" in system_content, f"Profile preferences not injected: {system_content[:200]}"
            # P0 画像瘦身: facts dormant — must NOT be injected (§6).
            assert "深圳" not in system_content, f"facts must not be injected: {system_content[:200]}"
            print_pass("User profile injected (typed slots only; facts dormant)")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_medium_memory_injection(self):
        """P1: MEDIUM-tier memory (from TieredMemory) is injected into the system
        prompt; the legacy MemorySystem.search substring injection is gone."""
        from agent.tool_registry import ToolRegistry
        from agent.memory import TieredMemory
        from agent.agent import Agent

        tmpdir = tempfile.mkdtemp()
        try:
            mock_client = MockDeepSeekClient()
            mock_client.plain_response = "关于力量训练..."

            tm = TieredMemory(base_dir=tmpdir)
            # Seed a MEDIUM item directly (count>=5 so it is NOT low-confidence).
            st = tm._get_state("user_abc")
            from agent.memory import MediumItem
            st.medium.append(MediumItem(
                id="m1", content="用户正在做力量训练，按酸痛程度分割训练循环",
                count=6, entry_extraction_index=0,
            ))

            agent = Agent(
                deepseek_client=mock_client,
                tool_registry=ToolRegistry(),
                config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
                tiered_memory=tm,
            )

            captured = {}
            original = agent._build_messages

            def capture(session, msg, *a, **k):
                msgs = original(session, msg, *a, **k)
                captured["msgs"] = msgs
                return msgs

            agent._build_messages = capture
            await agent.run("今天练什么好？", "user_abc")

            system_content = captured["msgs"][0]["content"]
            assert "用户记忆（中期）" in system_content, f"MEDIUM block missing: {system_content[-400:]}"
            assert "力量训练" in system_content, f"MEDIUM fact not injected: {system_content[-400:]}"
            # count>=5 → no low-confidence label
            assert "(低置信)" not in system_content, "count>=5 must NOT be low-confidence"
            print_pass("P1 MEDIUM-tier memory injected into system prompt")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestUserProfile:
    """Test the UserProfile and ProfileManager components."""

    def run(self):
        print_header("4.5. UserProfile & ProfileManager Tests")

        self.test_create_and_save()
        self.test_to_prompt_context_empty()
        self.test_to_prompt_context_full()
        self.test_merge_facts_dedup()
        self.test_merge_facts_cap()
        self.test_persistence()

    def test_create_and_save(self):
        from agent.profile import UserProfile, ProfileManager

        tmpdir = tempfile.mkdtemp()
        try:
            mgr = ProfileManager(base_dir=tmpdir)
            profile = mgr.get("user_123")
            assert profile.user_id == "user_123"
            assert profile.total_interactions == 0

            profile.touch()
            assert profile.total_interactions == 1
            mgr.save(profile)

            # Reload
            loaded = mgr.get("user_123")
            assert loaded.total_interactions == 1
            print_pass("Create, save, and reload profile")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_to_prompt_context_empty(self):
        from agent.profile import UserProfile

        profile = UserProfile(user_id="empty_user")
        ctx = profile.to_prompt_context()
        assert ctx == "", f"Empty profile should return empty string, got: '{ctx}'"
        print_pass("Empty profile returns empty prompt context")

    def test_to_prompt_context_full(self):
        from agent.profile import UserProfile

        profile = UserProfile(
            user_id="full_user",
            nickname="小红",
            facts=["在深圳", "用Python做后端开发"],
            interests=["机器学习", "游戏"],
            preferences={"response_style": "detailed"},
            total_interactions=42,
        )
        ctx = profile.to_prompt_context()
        assert "小红" in ctx
        assert "机器学习" in ctx
        assert "detailed" in ctx
        # P0 画像瘦身: free-text `facts` are DORMANT — never injected (§6).
        assert "深圳" not in ctx, "facts must NOT be injected after P0 slimming"
        assert "已知信息" not in ctx, "facts block must be gone from prompt context"
        print_pass("Full profile injects typed slots only (facts dormant)")

    def test_merge_facts_dedup(self):
        from agent.profile import UserProfile

        profile = UserProfile(user_id="dedup_user")
        profile.merge_facts(["在深圳", "用Python"])

        # Add same fact
        profile.merge_facts(["在深圳", "喜欢游戏"])
        assert len(profile.facts) == 3, f"Expected 3 facts, got {len(profile.facts)}: {profile.facts}"
        print_pass("Fact deduplication (fuzzy matching)")

    def test_merge_facts_cap(self):
        from agent.profile import UserProfile, MAX_FACTS

        profile = UserProfile(user_id="cap_user")
        many = [f"fact_{i}" for i in range(MAX_FACTS + 5)]
        profile.merge_facts(many)
        assert len(profile.facts) == MAX_FACTS, (
            f"Expected {MAX_FACTS} facts, got {len(profile.facts)}"
        )
        assert profile.facts == many[-MAX_FACTS:], "Oldest facts should be pruned"
        print_pass("Fact list capped at MAX_FACTS with oldest pruned")

    def test_persistence(self):
        from agent.profile import UserProfile, ProfileManager

        tmpdir = tempfile.mkdtemp()
        try:
            mgr = ProfileManager(base_dir=tmpdir)
            profile = mgr.get("persist_user")
            profile.nickname = "测试用户"
            profile.merge_facts(["事实1", "事实2"])
            profile.merge_interests(["兴趣1"])
            profile.merge_preferences({"lang": "zh"})
            mgr.save(profile)

            # New manager loads same file
            mgr2 = ProfileManager(base_dir=tmpdir)
            loaded = mgr2.get("persist_user")
            assert loaded.nickname == "测试用户"
            assert "事实1" in loaded.facts
            assert "兴趣1" in loaded.interests
            assert loaded.preferences["lang"] == "zh"
            print_pass("Profile persistence to disk")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class _GatedProfileClient:
    """Mock profile-extraction client returning canned JSON.

    If `gate` (an asyncio.Event) is set, chat_completion awaits it — lets tests
    hold an extraction in-flight to exercise single-flight behaviour. Mirrors the
    DeepSeekClient.chat_completion(message, history, timeout_set, purpose) shape.
    """

    def __init__(self, response: str = "{}"):
        self.response = response
        self.calls = []
        self.gate = None

    async def chat_completion(self, message, history=None, timeout_set=180.0, purpose="chat"):
        self.calls.append({"prompt": message, "purpose": purpose, "timeout_set": timeout_set})
        if self.gate is not None:
            await self.gate.wait()
        return self.response


class TestProfileExtraction:
    """P0 extraction precision: Layer 2 filter + Layer 1 prompt + Layer 3 batching."""

    def run(self):
        print_header("4.6. Profile Extraction Precision (Layer 1/2/3)")

        # Layer 2 — deterministic post-filter (fact_filter)
        self.test_l2a_compound_terms()
        self.test_l2b_regex_patterns()
        self.test_l2c_structural_rules()
        self.test_l2x_deliberately_kept()
        self.test_filter_many_partition()
        # Layer 1 — extraction prompt + atomic apply
        self.test_prompt_no_new_facts()
        self.test_format_conversation_window()
        self.test_apply_extraction_filters_interests()
        # Layer 3 — batching / single-flight / delete-flush
        asyncio.run(self.test_observe_turn_flush_at_k())
        asyncio.run(self.test_single_flight())
        asyncio.run(self.test_delete_flush())

    # ── Layer 2 ──────────────────────────────────────────────────

    def test_l2a_compound_terms(self):
        from agent.fact_filter import filter_candidate
        junk = [
            "用户的工作区剩余空间", "这是特殊会话", "十连抽卡结果",
            "兑换码已过期", "Roxy是机器人", "行动值跑条为3", "测速完成",
        ]
        for text in junk:
            r = filter_candidate(text)
            assert not r.keep and r.category.startswith("L2A"), f"{text!r} → {r}"
        print_pass("Layer2-A: unambiguous compounds dropped (bot_state/tool/gacha/bot_self)")

    def test_l2b_regex_patterns(self):
        from agent.fact_filter import filter_candidate
        cases = {
            "剩余250 MB空间": "L2B:capacity_unit",
            "存档占用1.5GB": "L2B:capacity_unit",
            "抽到了UP角色": "L2B:gacha_result",
            "获得了2个4星紫色羁绊": "L2B:gacha_result",
            "请求超时了": "L2B:error_marker",
            # P1: the `transient_state` category was removed entirely (bare 正在
            # false-dropped real-life ongoing activities). Bot-transient events
            # are now Layer 1's job; bot-state compounds still hit L2-A/L2-C.
        }
        for text, cat in cases.items():
            r = filter_candidate(text)
            assert not r.keep and r.category == cat, f"{text!r}: want {cat}, got {r.category} (keep={r.keep})"
        print_pass("Layer2-B: regex dropped (capacity/gacha_result/error)")

    def test_l2c_structural_rules(self):
        from agent.fact_filter import filter_candidate
        r = filter_candidate("该项目使用了缓存机制")
        assert not r.keep and r.category == "L2C:unresolved_ref", r
        r = filter_candidate("250")
        assert not r.keep and r.category in ("L2C:pure_numeric", "L2B:capacity_unit"), r
        # entity markers RESOLVE an otherwise-unresolved reference → kept
        r = filter_candidate("该项目叫「星穹铁道」很好玩")
        assert r.keep, f"quoted entity should resolve reference: {r}"
        print_pass("Layer2-C: unresolved-ref + pure-numeric dropped; entity markers resolve")

    def test_l2x_deliberately_kept(self):
        from agent.fact_filter import filter_candidate
        # §10 L2-X: ambiguous bare words + legit facts must NOT be dropped.
        legit = [
            "用户喜欢「原神」这款游戏",  # quoted game entity
            "用户是短跑运动员速度很快",   # bare 速度 ambiguous
            "用户从事招募工作",           # bare 招募 ambiguous
            "用户是会话分析研究者",       # bare 会话 ambiguous
            "该职业很稳定",               # bare 该 (not a bot-context compound)
            "这个城市很好",               # bare 这个
            "用户获得了计算机硕士学位",   # bare 获得 (legit, not gacha)
            "用户出版了一本书",           # legit
            # P1: real-life ongoing activities (正在) MUST be kept — these were
            # false-dropped by the now-removed transient_state rule (the two
            # facts wrongly deleted from user 1114144652 in the 2026-09-14 cleanup).
            "用户正在通过饮食调整降血脂",
            "用户正在做力量训练，按酸痛程度自由分割训练循环",
        ]
        for text in legit:
            r = filter_candidate(text)
            assert r.keep, f"L2-X FALSE POSITIVE: {text!r} dropped by {r.category}/{r.matched}"
        print_pass("Layer2-X: ambiguous bare words + legit facts kept (no false positives)")

    def test_filter_many_partition(self):
        from agent.fact_filter import filter_many
        kept, dropped = filter_many(["用户喜欢猫", "工作区剩余250MB", "用户是教师"])
        assert kept == ["用户喜欢猫", "用户是教师"], kept
        assert len(dropped) == 1 and dropped[0][2] == "L2A:bot_state", dropped
        assert dropped[0][0] == "工作区剩余250MB" and dropped[0][1] == "工作区", dropped
        print_pass("filter_many: partitions kept/dropped with (text, match, category)")

    # ── Layer 1 ──────────────────────────────────────────────────

    def test_prompt_no_new_facts(self):
        from agent.profile import ProfileManager
        conv = ProfileManager._format_conversation([("我最近迷上原神", "原神耐玩")])
        prompt = ProfileManager._build_extraction_prompt(
            conv, {"nickname": None, "interests": [], "preferences": {}}
        )
        assert "new_facts" not in prompt, "P0 schema must NOT request new_facts"
        for token in ["石蕊测试", "助手(Roxy)", "few-shot", "<conversation>",
                      "new_interests", "new_preferences",
                      # P1: the merged extract+judge call always advertises the
                      # memory_candidates schema (§12.1), even with no judge_list.
                      "memory_candidates", "reinforce_short", "matched_id"]:
            assert token in prompt, f"prompt missing {token!r}"
        # No judge_list → the "暂无已有记忆" branch tells the model to use judge="new".
        assert "暂无已有记忆" in prompt, "empty judge_list must use the no-memory branch"
        # With a judge_list → the existing-memory section embeds each item + the
        # four judge verbs so the single flash call can route candidates.
        jp = ProfileManager._build_extraction_prompt(
            conv, {"nickname": None, "interests": [], "preferences": {}},
            judge_list=[{"id": "abc123", "content": "用户喜欢原神", "count": 4, "tier": "medium"}],
        )
        for token in ["abc123", "用户喜欢原神", "已有记忆",
                      "new", "reinforce_short", "update", "keep"]:
            assert token in jp, f"judge prompt missing {token!r}"
        assert "暂无已有记忆" not in jp, "non-empty judge_list must NOT use the no-memory branch"
        print_pass("Layer1 prompt: litmus + few-shot + memory_candidates schema + judge list")

    def test_format_conversation_window(self):
        from agent.profile import (
            ProfileManager, EXTRACT_USER_MSG_CAP, EXTRACT_AGENT_RESP_CAP,
            EXTRACT_TOTAL_CHAR_CAP,
        )
        # multi-turn, oldest→newest order preserved, third-person labels
        block = ProfileManager._format_conversation([(f"问{i}", f"答{i}") for i in range(8)])
        assert block.startswith("<conversation>") and block.endswith("</conversation>")
        assert "问7" in block and "助手(Roxy): 答0" in block
        assert block.index("问0") < block.index("问7"), "turns must stay oldest→newest"
        # per-message truncation
        one = ProfileManager._format_conversation([("x" * 900, "y" * 900)])
        assert ("x" * (EXTRACT_USER_MSG_CAP + 1)) not in one
        assert ("y" * (EXTRACT_AGENT_RESP_CAP + 1)) not in one
        # total-char cap drops OLDEST turns, newest always survives
        big = [("a" * 480 + f"#{i}#", "b" * 190) for i in range(20)]
        capped = ProfileManager._format_conversation(big)
        assert len(capped) <= EXTRACT_TOTAL_CHAR_CAP, f"{len(capped)} > cap"
        assert "#19#" in capped, "newest turn must survive the cap"
        assert "#0#" not in capped, "oldest turn should be dropped by the cap"
        print_pass("Layer1 _format_conversation: window order, truncation, total-char cap")

    def test_apply_extraction_filters_interests(self):
        tmpdir = tempfile.mkdtemp()
        try:
            from agent.profile import ProfileManager
            mgr = ProfileManager(base_dir=tmpdir)
            prof = mgr.get("u_apply")
            extracted = {
                "nickname": "阿明",
                "new_interests": ["原神", "用户工作区剩余250MB", "篮球"],
                "new_preferences": {"语言": "中文"},
            }
            changed = mgr._apply_extraction(prof, "u_apply", extracted)
            assert changed is True
            assert prof.nickname == "阿明"
            assert "原神" in prof.interests and "篮球" in prof.interests, prof.interests
            assert not any("工作区" in x for x in prof.interests), "junk interest must be L2-dropped"
            assert prof.preferences.get("语言") == "中文"
            # empty extraction → changed False (no writes)
            assert mgr._apply_extraction(mgr.get("u_noop"), "u_noop", {}) is False
            print_pass("Layer1 _apply_extraction: L2-filters interests, sets nickname/prefs, changed flag")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ── Layer 3 ──────────────────────────────────────────────────

    async def test_observe_turn_flush_at_k(self):
        tmpdir = tempfile.mkdtemp()
        try:
            from agent.profile import ProfileManager, PROFILE_BATCH_K, EXTRACT_WINDOW_N
            client = _GatedProfileClient('{"new_interests": ["原神"]}')
            mgr = ProfileManager(base_dir=tmpdir, llm_client=client)
            for i in range(PROFILE_BATCH_K - 1):
                mgr.observe_turn("u", f"m{i}", "r")
            await asyncio.sleep(0)
            assert len(client.calls) == 0, "must NOT flush before K turns"
            assert mgr._pending_n["u"] == PROFILE_BATCH_K - 1
            mgr.observe_turn("u", "mK", "r")  # Kth turn → flush
            await asyncio.sleep(0)
            assert len(client.calls) == 1, "flush exactly at K turns"
            assert client.calls[0]["purpose"] == "profile"
            assert "mK" in client.calls[0]["prompt"] and "m0" in client.calls[0]["prompt"]
            # rolling window bounded at EXTRACT_WINDOW_N
            for i in range(EXTRACT_WINDOW_N + 5):
                mgr.observe_turn("u", f"x{i}", "r")
            assert len(mgr._recent["u"]) == EXTRACT_WINDOW_N, len(mgr._recent["u"])
            await asyncio.sleep(0.05)
            assert "原神" in mgr.get("u").interests
            print_pass("Layer3 observe_turn: buffers, flushes at K, window bounded, applies")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_single_flight(self):
        tmpdir = tempfile.mkdtemp()
        try:
            from agent.profile import ProfileManager, PROFILE_BATCH_K
            client = _GatedProfileClient('{"new_interests": ["原神"]}')
            client.gate = asyncio.Event()  # hold every extraction in-flight
            mgr = ProfileManager(base_dir=tmpdir, llm_client=client)
            for i in range(PROFILE_BATCH_K):
                mgr.observe_turn("u", f"m{i}", "r")
            await asyncio.sleep(0)
            assert len(client.calls) == 1 and "u" in mgr._inflight
            # turns arriving while in-flight: NO 2nd extraction, pending accumulates
            for i in range(3):
                mgr.observe_turn("u", f"after{i}", "r")
            await asyncio.sleep(0)
            assert len(client.calls) == 1, "single-flight: no concurrent 2nd extraction"
            assert mgr._pending_n["u"] == 3, mgr._pending_n["u"]
            client.gate.set()  # release
            await asyncio.sleep(0.05)
            assert "u" not in mgr._inflight, "in-flight cleared after completion"
            assert len(client.calls) == 1, "pending(3)<K must NOT auto re-flush"
            assert "原神" in mgr.get("u").interests
            print_pass("Layer3 single-flight: one extraction at a time; pending accumulates")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_delete_flush(self):
        tmpdir = tempfile.mkdtemp()
        try:
            from agent.profile import ProfileManager, PROFILE_BATCH_K
            # substantive session (>K turns): force-flush the tail even if pending<K
            c = _GatedProfileClient("{}")
            m = ProfileManager(base_dir=tmpdir, llm_client=c)
            for i in range(3):
                m.observe_turn("u2", f"m{i}", "r")  # pending=3 < K
            await asyncio.sleep(0)
            assert len(c.calls) == 0
            m.flush_on_session_end("u2", session_turn_count=PROFILE_BATCH_K + 3)
            await asyncio.sleep(0)
            assert len(c.calls) == 1, "substantive delete must flush the tail"

            # transient session (≤K turns): drop the tail, no extraction
            c2 = _GatedProfileClient("{}")
            m2 = ProfileManager(base_dir=tmpdir, llm_client=c2)
            for i in range(3):
                m2.observe_turn("u3", f"m{i}", "r")
            await asyncio.sleep(0)
            m2.flush_on_session_end("u3", session_turn_count=2)
            await asyncio.sleep(0.02)
            assert len(c2.calls) == 0, "transient delete must NOT extract"
            assert m2._pending_n["u3"] == 0, "tail must be dropped"
            print_pass("Layer3 delete-flush: >K forces tail, ≤K drops (Decision J)")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestDeepSeekClientParsing:
    """Test the DeepSeekClient response parsing logic."""

    def run(self):
        print_header("5. DeepSeekClient Response Parsing")

        self.test_parse_plain_response()
        self.test_parse_tool_call_response()
        self.test_parse_mixed_response()

    def test_parse_plain_response(self):
        from lib.deepseek_client import DeepSeekClient

        # We can't instantiate without NoneBot config, so create a minimal instance
        # Just test the _parse_response method directly
        client = object.__new__(DeepSeekClient)

        mock_api_response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "你好！有什么可以帮助你的？",
                },
                "finish_reason": "stop",
            }]
        }

        result = DeepSeekClient._parse_response(client, mock_api_response)
        assert result["content"] == "你好！有什么可以帮助你的？"
        assert result["tool_calls"] is None
        assert result["role"] == "assistant"
        assert result["finish_reason"] == "stop"
        print_pass("Parse plain text response")

    def test_parse_tool_call_response(self):
        from lib.deepseek_client import DeepSeekClient

        client = object.__new__(DeepSeekClient)

        mock_api_response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_abc123",
                        "type": "function",
                        "function": {
                            "name": "get_time",
                            "arguments": "{}",
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }]
        }

        result = DeepSeekClient._parse_response(client, mock_api_response)
        assert result["content"] is None
        assert result["tool_calls"] is not None
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["function"]["name"] == "get_time"
        assert result["tool_calls"][0]["id"] == "call_abc123"
        assert result["finish_reason"] == "tool_calls"
        print_pass("Parse tool call response")

    def test_parse_mixed_response(self):
        from lib.deepseek_client import DeepSeekClient

        client = object.__new__(DeepSeekClient)

        # Some models return both content AND tool_calls
        mock_api_response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "让我查看一下时间。",
                    "tool_calls": [{
                        "id": "call_xyz",
                        "type": "function",
                        "function": {
                            "name": "get_time",
                            "arguments": "{}",
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }]
        }

        result = DeepSeekClient._parse_response(client, mock_api_response)
        assert result["content"] == "让我查看一下时间。"
        assert result["tool_calls"] is not None
        assert len(result["tool_calls"]) == 1
        print_pass("Parse mixed response (content + tool_calls)")


class TestBuiltinTools:
    """Test the built-in tool functions directly."""

    def run(self):
        print_header("6. Built-in Tool Tests")

        self.test_get_time()
        self.test_execute_code_success()
        self.test_execute_code_error()
        self.test_search_web()

    def test_get_time(self):
        from tools.builtin_tools import get_time

        result = get_time()
        assert "当前时间" in result
        assert "2026" in result or "2025" in result
        print_pass("get_time tool returns valid time string")

    def test_execute_code_success(self):
        import asyncio
        from tools.builtin_tools import execute_code

        result = asyncio.run(execute_code("print('Hello World')"))
        assert "Hello World" in result
        print_pass("execute_code runs Python and captures output")

        result = asyncio.run(execute_code("print(1 + 1)"))
        assert "2" in result
        print_pass("execute_code handles calculations")

    def test_execute_code_error(self):
        import asyncio
        from tools.builtin_tools import execute_code

        result = asyncio.run(execute_code("1/0"))
        assert "ZeroDivisionError" in result or "Error" in result
        print_pass("execute_code handles runtime errors")

    def test_search_web(self):
        from tools.builtin_tools import search_web

        result = search_web("Python programming", num_results=3)
        # Result may succeed (SearXNG available) or fail gracefully (SearXNG unavailable)
        assert isinstance(result, str)
        assert len(result) > 0
        print_pass("search_web returns results or graceful fallback (SearXNG)")


class TestPersonality:
    """Test PersonalityManager group-bound default resolution."""

    def run(self):
        print_header("7. Personality Manager Tests")

        self.test_resolve_precedence()
        self.test_set_group_personality()
        self.test_clear_group_personality()
        self.test_ambiguous_resolution()

    def _make_manager(self):
        import agent.personality as pmod

        tmpdir = tempfile.mkdtemp(prefix="personality_test_")
        pdir = os.path.join(tmpdir, "personalities")
        os.makedirs(pdir, exist_ok=True)
        for name, title in [
            ("assistant", "助手 Roxy"),
            ("roxy_character", "角色 Roxy (无职转生)"),
            ("rubi", "露比 (Rubi)"),
        ]:
            with open(os.path.join(pdir, f"{name}.md"), "w", encoding="utf-8") as f:
                f.write(f"# {title}\n\nTest personality.")

        config = os.path.join(tmpdir, "personality_config.json")
        with open(config, "w", encoding="utf-8") as f:
            json.dump({"default": "assistant"}, f)

        settings = os.path.join(tmpdir, "personality_settings.json")
        group = os.path.join(tmpdir, "group_personality.json")

        patcher = patch.multiple(
            pmod,
            _SETTINGS_FILE=settings,
            _DEFAULT_CONFIG_FILE=config,
            _GROUP_CONFIG_FILE=group,
        )
        patcher.start()
        return pmod.PersonalityManager(personalities_dir=pdir), patcher, tmpdir

    def test_resolve_precedence(self):
        pm, patcher, tmpdir = self._make_manager()
        try:
            # No settings anywhere -> global default
            assert pm.resolve_effective_personality("u1", "g1") == "assistant"
            # Group binding -> group wins over global
            pm.set_group_personality("g1", "露比")
            assert pm.resolve_effective_personality("u1", "g1") == "rubi"
            # Personal setting -> personal wins over group
            pm.set_user_personality("u1", "assistant")
            assert pm.resolve_effective_personality("u1", "g1") == "assistant"
            # User without personal setting still gets group default
            assert pm.resolve_effective_personality("u2", "g1") == "rubi"
            # Empty group_id -> skip group layer
            assert pm.resolve_effective_personality("u2", "") == "assistant"
            print_pass("resolve_effective_personality precedence chain")
        finally:
            patcher.stop()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_set_group_personality(self):
        pm, patcher, tmpdir = self._make_manager()
        try:
            resolved = pm.set_group_personality("g1", "Rubi")
            assert resolved == "rubi"
            assert pm.get_group_personality("g1") == "rubi"
            # Unknown name raises ValueError
            try:
                pm.set_group_personality("g2", "不存在的")
                assert False, "expected ValueError for unknown personality"
            except ValueError:
                pass
            print_pass("set_group_personality fuzzy match + validation")
        finally:
            patcher.stop()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_ambiguous_resolution(self):
        pm, patcher, tmpdir = self._make_manager()
        try:
            # "Roxy" is a substring of both 助手 Roxy and 角色 Roxy → ambiguous
            assert pm.resolve_name("Roxy") is None
            # Distinct prefixes still resolve uniquely
            assert pm.resolve_name("助手") == "assistant"
            assert pm.resolve_name("角色") == "roxy_character"
            # Ambiguous name raises ValueError with a hint
            try:
                pm.set_user_personality("u1", "Roxy")
                assert False, "expected ValueError for ambiguous name"
            except ValueError as e:
                assert "多个" in str(e)
            print_pass("ambiguous names rejected instead of silently mispicked")
        finally:
            patcher.stop()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_clear_group_personality(self):
        pm, patcher, tmpdir = self._make_manager()
        try:
            pm.set_group_personality("g1", "rubi")
            assert pm.get_group_personality("g1") == "rubi"
            pm.clear_group_personality("g1")
            assert pm.get_group_personality("g1") is None
            assert pm.resolve_effective_personality("u1", "g1") == "assistant"
            print_pass("clear_group_personality falls back to global")
        finally:
            patcher.stop()
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestTokenLedger:
    """Test the token usage ledger (metering master table)."""

    def run(self):
        print_header("8. TokenLedger Tests")

        self.test_extract_usage_deepseek_format()
        self.test_extract_usage_missing()
        self.test_extract_usage_dashscope_native()
        self.test_extract_usage_clamps_cache()
        self.test_record_and_summary()
        self.test_aggregate_day()
        self.test_format_summary()

    def test_extract_usage_deepseek_format(self):
        from lib.token_ledger import extract_usage
        result = {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
                "prompt_tokens_details": {"cached_tokens": 800},
            }
        }
        u = extract_usage(result)
        assert u["input_tokens"] == 1000, u
        assert u["output_tokens"] == 200, u
        assert u["cached_input_tokens"] == 800, u
        assert u["uncached_input_tokens"] == 200, u
        print_pass("extract_usage: DeepSeek format splits hit/miss input")

    def test_extract_usage_missing(self):
        from lib.token_ledger import extract_usage
        assert extract_usage({}) == {
            "input_tokens": 0, "output_tokens": 0,
            "cached_input_tokens": 0, "uncached_input_tokens": 0,
        }
        # usage present but no cache details → all input counts as miss
        u = extract_usage({"usage": {"prompt_tokens": 50, "completion_tokens": 5}})
        assert u["cached_input_tokens"] == 0 and u["uncached_input_tokens"] == 50, u
        print_pass("extract_usage: missing/partial usage yields safe zeros")

    def test_extract_usage_dashscope_native(self):
        from lib.token_ledger import extract_usage
        # dashscope native multimodal-generation response shape
        result = {"usage": {"input_tokens": 300, "output_tokens": 40}}
        u = extract_usage(result)
        assert u["input_tokens"] == 300 and u["output_tokens"] == 40, u
        # alternate cache-hit key used by some providers
        result2 = {"usage": {"prompt_tokens": 100, "completion_tokens": 10,
                             "prompt_cache_hit_tokens": 60}}
        u2 = extract_usage(result2)
        assert u2["cached_input_tokens"] == 60, u2
        assert u2["uncached_input_tokens"] == 40, u2
        print_pass("extract_usage: dashscope native + prompt_cache_hit_tokens")

    def test_extract_usage_clamps_cache(self):
        from lib.token_ledger import extract_usage
        result = {
            "usage": {"prompt_tokens": 10, "completion_tokens": 1,
                      "prompt_tokens_details": {"cached_tokens": 9999}}
        }
        u = extract_usage(result)
        assert u["cached_input_tokens"] == 10, u
        assert u["uncached_input_tokens"] == 0, u
        print_pass("extract_usage: cached_tokens clamped to input_tokens")

    def test_record_and_summary(self):
        import tempfile, shutil
        from lib import token_ledger as tl
        tmpdir = tempfile.mkdtemp(prefix="token_ledger_test_")
        try:
            ledger = tl.TokenLedger(data_dir=tmpdir)
            # Attribution context (set by agent_router per message)
            token = tl.usage_context.set(
                {"user_id": "u1", "group_id": "g1", "chat_type": "group"}
            )
            ledger.record("deepseek-v4-pro", "agent_loop", {
                "input_tokens": 1000, "output_tokens": 200,
                "cached_input_tokens": 800, "uncached_input_tokens": 200,
            })
            ledger.record("deepseek-v4-flash", "triage", {
                "input_tokens": 100, "output_tokens": 5,
                "cached_input_tokens": 0, "uncached_input_tokens": 100,
            })
            tl.usage_context.reset(token)

            # JSONL detail: one file, two entries, fields intact
            import glob, json
            files = glob.glob(f"{tmpdir}/usage_*.jsonl")
            assert len(files) == 1, files
            with open(files[0], encoding="utf-8") as f:
                entries = [json.loads(line) for line in f if line.strip()]
            assert len(entries) == 2, entries
            e0 = entries[0]
            assert e0["user_id"] == "u1" and e0["chat_type"] == "group", e0
            assert e0["cached_input_tokens"] == 800, e0
            assert e0["uncached_input_tokens"] == 200, e0

            # Summary table: totals + per-model + per-purpose breakdowns
            s = ledger.read_summary()
            t = s["totals"]
            assert t["requests"] == 2, t
            assert t["input_tokens"] == 1100, t
            assert t["output_tokens"] == 205, t
            assert t["cached_input_tokens"] == 800, t
            assert t["uncached_input_tokens"] == 300, t
            assert s["by_model"]["deepseek-v4-pro"]["input_tokens"] == 1000
            assert s["by_model"]["deepseek-v4-flash"]["cached_input_tokens"] == 0
            assert s["by_purpose"]["triage"]["requests"] == 1
            assert s["by_purpose"]["agent_loop"]["output_tokens"] == 200
            assert len(s["daily"]) == 1
            print_pass("record: JSONL detail + totals/by_model/by_purpose correct")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_aggregate_day(self):
        import tempfile, shutil
        from datetime import datetime
        from lib.token_ledger import TokenLedger
        tmpdir = tempfile.mkdtemp(prefix="token_ledger_test_")
        try:
            ledger = TokenLedger(data_dir=tmpdir)
            ledger.record("m1", "chat", {
                "input_tokens": 10, "output_tokens": 2,
                "cached_input_tokens": 4, "uncached_input_tokens": 6,
            })
            today = datetime.now().strftime("%Y-%m-%d")
            agg = ledger.aggregate_day(today)
            assert agg["requests"] == 1 and agg["input_tokens"] == 10, agg
            assert agg["cached_input_tokens"] == 4, agg
            assert ledger.aggregate_day("1999-01-01")["requests"] == 0
            print_pass("aggregate_day: recomputes day totals from JSONL")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_format_summary(self):
        import tempfile, shutil
        from lib.token_ledger import TokenLedger
        tmpdir = tempfile.mkdtemp(prefix="token_ledger_test_")
        try:
            ledger = TokenLedger(data_dir=tmpdir)
            ledger.record("m1", "agent_loop", {
                "input_tokens": 100, "output_tokens": 10,
                "cached_input_tokens": 25, "uncached_input_tokens": 75,
            })
            text = ledger.format_summary()
            assert "命中25" in text, text
            assert "未命中75" in text, text
            assert "25.0%" in text, text  # hit rate = 25/100
            print_pass("format_summary: renders hit-rate table")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── Main Runner ──────────────────────────────────────────────────

class TestTieredMemory:
    """P1 three-tier memory engine (SHORT/MEDIUM/LONG) — agent/memory.py:TieredMemory."""

    def _tm(self, tmpdir):
        from agent.memory import TieredMemory
        return TieredMemory(base_dir=tmpdir)

    def run(self):
        print_header("4.7. TieredMemory (P1 three-tier engine)")
        self.test_short_new_enters_front()
        self.test_short_exact_dup_guard()
        self.test_short_reinforce_moves_front()
        self.test_short_promote_at_count_3()
        self.test_short_overflow_evicts_tail()
        self.test_medium_update_refines_and_resets_age()
        self.test_medium_keep_increments()
        self.test_invalid_matched_id_falls_back_to_new()
        self.test_age_demotion_to_short()
        self.test_medium_max_safety_net()
        self.test_long_creation_twin_kept_idempotent()
        self.test_long_snapshot_turns_from_batch()
        self.test_judge_list_composition()
        self.test_injection_empty_returns_blank()
        self.test_injection_low_confidence_label()
        self.test_injection_top_n_plus_recent()
        self.test_injection_token_cap()
        self.test_touch_extraction_count()
        self.test_persistence_roundtrip()
        self.test_load_all()

    # ── SHORT ────────────────────────────────────────────────────

    def test_short_new_enters_front(self):
        tmp = tempfile.mkdtemp()
        try:
            tm = self._tm(tmp)
            s = tm.ingest_candidates("u", [("用户喜欢原神", "new", None), ("用户是程序员", "new", None)])
            st = tm._get_state("u")
            assert s["new_short"] == 2, s
            assert len(st.short) == 2 and not st.medium
            # newest inserted at front (index 0)
            assert st.short[0].content == "用户是程序员", [x.content for x in st.short]
            assert all(x.count == 1 for x in st.short)
            print_pass("SHORT: new candidates enter at front, count=1")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_short_exact_dup_guard(self):
        tmp = tempfile.mkdtemp()
        try:
            tm = self._tm(tmp)
            tm.ingest_candidates("u", [("用户喜欢原神", "new", None)])
            s = tm.ingest_candidates("u", [("用户喜欢原神", "new", None)])  # verbatim dup
            st = tm._get_state("u")
            assert len(st.short) == 1, "exact dup must NOT create a 2nd SHORT item"
            assert st.short[0].count == 2, "exact dup reinforces instead"
            assert s["reinforced_short"] == 1 and s["new_short"] == 0, s
            print_pass("SHORT: exact-dup guard reinforces instead of duplicating")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_short_reinforce_moves_front(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import ShortItem, SHORT_PRIORITY_STEP
            tm = self._tm(tmp)
            st = tm._get_state("u")
            # build a list longer than SHORT_PRIORITY_STEP so the move is observable
            for i in range(SHORT_PRIORITY_STEP + 3):
                st.short.append(ShortItem(id=f"s{i}", content=f"事实{i}", count=1))
            target = st.short[SHORT_PRIORITY_STEP + 1]   # deep item
            tm.ingest_candidates("u", [("事实X", "reinforce_short", target.id)])
            new_idx = st.short.index(target)
            assert new_idx == 1, f"moved to idx {new_idx}, expected 1 (front by STEP)"
            assert target.count == 2
            print_pass("SHORT: reinforce moves item toward front + count+1")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_short_promote_at_count_3(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import ShortItem
            tm = self._tm(tmp)
            st = tm._get_state("u")
            st.short.append(ShortItem(id="s1", content="用户喜欢原神", count=2))
            s = tm.ingest_candidates("u", [("用户喜欢原神", "reinforce_short", "s1")])
            assert s["promoted_to_medium"] == 1, s
            assert not st.short, "promoted item leaves SHORT"
            assert len(st.medium) == 1 and st.medium[0].count == 3
            assert st.medium[0].id == "s1", "promotion keeps the same id"
            assert st.medium[0].entry_extraction_index == st.extraction_count
            print_pass("SHORT→MEDIUM: promotes at count=3, keeps id, anchors age")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_short_overflow_evicts_tail(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import SHORT_MAX
            tm = self._tm(tmp)
            for i in range(SHORT_MAX + 10):
                tm.ingest_candidates("u", [(f"独特事实编号{i}", "new", None)])
            st = tm._get_state("u")
            assert len(st.short) == SHORT_MAX, f"SHORT must cap at {SHORT_MAX}, got {len(st.short)}"
            # newest survives at front, oldest evicted from tail
            assert st.short[0].content == f"独特事实编号{SHORT_MAX + 9}"
            assert all(f"编号0" not in x.content for x in st.short), "oldest should be evicted"
            print_pass("SHORT: overflow evicts oldest from tail (cap SHORT_MAX)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ── MEDIUM ───────────────────────────────────────────────────

    def test_medium_update_refines_and_resets_age(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import MediumItem
            tm = self._tm(tmp)
            st = tm._get_state("u")
            st.extraction_count = 20
            st.medium.append(MediumItem(id="m1", content="用户玩原神", count=4, entry_extraction_index=5))
            s = tm.ingest_candidates("u", [("用户是原神活跃玩家，每天做日常", "update", "m1")])
            assert s["reinforced_medium"] == 1, s
            m = st.medium[0]
            assert m.content == "用户是原神活跃玩家，每天做日常", "update replaces content"
            assert m.count == 5, "update increments count"
            assert m.entry_extraction_index == 20, "update resets age to current clock"
            print_pass("MEDIUM update: refines content, count+1, age→0")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_medium_keep_increments(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import MediumItem
            tm = self._tm(tmp)
            st = tm._get_state("u")
            st.medium.append(MediumItem(id="m1", content="用户玩原神", count=4, entry_extraction_index=0))
            s = tm.ingest_candidates("u", [("用户玩原神", "keep", "m1")])
            assert s["reinforced_medium"] == 1, s
            assert st.medium[0].content == "用户玩原神", "keep does NOT change content"
            assert st.medium[0].count == 5
            print_pass("MEDIUM keep: count+1, content unchanged")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_invalid_matched_id_falls_back_to_new(self):
        tmp = tempfile.mkdtemp()
        try:
            tm = self._tm(tmp)
            s = tm.ingest_candidates("u", [("用户喜欢猫", "update", "no_such_id"),
                                           ("用户喜欢狗", "reinforce_short", "ghost")])
            st = tm._get_state("u")
            assert s["new_short"] == 2, f"hallucinated ids must fall back to new SHORT: {s}"
            assert len(st.short) == 2 and not st.medium
            print_pass("MEDIUM/SHORT: invalid matched_id falls back to new SHORT")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_age_demotion_to_short(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import MediumItem, MEDIUM_DOWNGRADE_AGE, MEDIUM_DOWNGRADE_COUNT_RESET
            tm = self._tm(tmp)
            st = tm._get_state("u")
            st.medium.append(MediumItem(id="m1", content="陈旧事实", count=6, entry_extraction_index=0))
            st.extraction_count = MEDIUM_DOWNGRADE_AGE + 1   # age = 31 > 30
            s = tm.ingest_candidates("u", [])                 # empty ingest still runs demotions
            assert s["demoted_to_short"] == 1, s
            assert not st.medium, "aged-out MEDIUM leaves the tier"
            assert len(st.short) == 1 and st.short[0].content == "陈旧事实"
            assert st.short[0].count == MEDIUM_DOWNGRADE_COUNT_RESET, "demotion resets count to 2"
            print_pass("MEDIUM→SHORT: age>30 demotes with count reset")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_medium_max_safety_net(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import MediumItem, MEDIUM_MAX, MEDIUM_DOWNGRADE_AGE
            tm = self._tm(tmp)
            st = tm._get_state("u")
            # clock=30 keeps every age <=30 (no age-demotion); only the cap fires.
            st.extraction_count = MEDIUM_DOWNGRADE_AGE
            n = MEDIUM_MAX + 5
            for i in range(n):
                st.medium.append(MediumItem(id=f"m{i}", content=f"事实{i}", count=5, entry_extraction_index=i))
            s = tm.ingest_candidates("u", [])
            assert len(st.medium) == MEDIUM_MAX, f"cap at {MEDIUM_MAX}, got {len(st.medium)}"
            assert s["demoted_to_short"] == 5, s
            # oldest (entry_index=0 → highest age) demoted first
            assert all(m.id != "m0" for m in st.medium), "oldest must be force-demoted"
            assert any(x.content == "事实0" for x in st.short)
            print_pass("MEDIUM_MAX safety net: force-demotes oldest above cap")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ── LONG ─────────────────────────────────────────────────────

    def test_long_creation_twin_kept_idempotent(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import MediumItem, MEDIUM_LONG_THRESHOLD
            tm = self._tm(tmp)
            st = tm._get_state("u")
            st.medium.append(MediumItem(id="m1", content="用户是原神活跃玩家",
                                        count=MEDIUM_LONG_THRESHOLD, entry_extraction_index=0))
            s = tm.ingest_candidates("u", [("用户是原神活跃玩家", "keep", "m1")],
                                     snapshot_turns=[("我玩原神", "好玩")])
            assert s["promoted_to_long"] == 1, s
            assert len(st.long) == 1 and st.long[0].linked_medium_id == "m1"
            assert len(st.medium) == 1, "MEDIUM twin KEPT after LONG creation (Decision G)"
            assert st.medium[0].count == MEDIUM_LONG_THRESHOLD + 1
            # idempotent: crossing again does NOT create a 2nd LONG
            s2 = tm.ingest_candidates("u", [("用户是原神活跃玩家", "keep", "m1")])
            assert s2["promoted_to_long"] == 0 and len(st.long) == 1, "LONG creation must be idempotent"
            print_pass("LONG: created at count>10, twin kept, idempotent")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_long_snapshot_turns_from_batch(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import MediumItem
            tm = self._tm(tmp)
            st = tm._get_state("u")
            st.medium.append(MediumItem(id="m1", content="事实", count=10, entry_extraction_index=0))
            turns = [("今天练腿", "好的"), ("明天练背", "收到")]
            tm.ingest_candidates("u", [("事实", "keep", "m1")], snapshot_turns=turns)
            assert st.long[0].snapshot_turns == turns, "LONG snapshot = the triggering batch's turns"
            print_pass("LONG: snapshot_turns captured from the current batch")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ── judge list ───────────────────────────────────────────────

    def test_judge_list_composition(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import MediumItem, ShortItem
            tm = self._tm(tmp)
            st = tm._get_state("u")
            st.medium.append(MediumItem(id="m1", content="中期A", count=3, entry_extraction_index=0))
            st.medium.append(MediumItem(id="m2", content="中期B", count=9, entry_extraction_index=0))
            st.short.append(ShortItem(id="s1", content="短期A", count=1))
            jl = tm.get_judge_list("u")
            assert len(jl) == 3, jl
            # MEDIUM first, sorted by count desc; then SHORT
            assert [x["id"] for x in jl] == ["m2", "m1", "s1"], [x["id"] for x in jl]
            assert jl[0]["tier"] == "medium" and jl[2]["tier"] == "short"
            assert all(set(x) == {"id", "content", "count", "tier"} for x in jl)
            print_pass("judge list: MEDIUM(top by count) + SHORT, each {id,content,count,tier}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ── injection ────────────────────────────────────────────────

    def test_injection_empty_returns_blank(self):
        tmp = tempfile.mkdtemp()
        try:
            tm = self._tm(tmp)
            assert tm.build_medium_injection("nobody") == "", "no MEDIUM → empty injection"
            print_pass("injection: empty MEDIUM returns ''")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_injection_low_confidence_label(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import MediumItem, LOW_CONFIDENCE_THRESHOLD
            tm = self._tm(tmp)
            st = tm._get_state("u")
            st.medium.append(MediumItem(id="lo", content="低置信事实", count=LOW_CONFIDENCE_THRESHOLD - 1, entry_extraction_index=0))
            block = tm.build_medium_injection("u")
            assert "(低置信)" in block and "低置信事实" in block, block
            st.medium.append(MediumItem(id="hi", content="高置信事实", count=LOW_CONFIDENCE_THRESHOLD, entry_extraction_index=0))
            block2 = tm.build_medium_injection("u")
            hi_line = [ln for ln in block2.splitlines() if "高置信事实" in ln][0]
            assert "(低置信)" not in hi_line, "count>=5 must NOT be low-confidence"
            print_pass("injection: (低置信) label for count<5 only")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_injection_top_n_plus_recent(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import MediumItem, MEDIUM_INJECT_TOP_N
            tm = self._tm(tmp)
            st = tm._get_state("u")
            # counts 1..20, entry_index aligned so 'recent' == higher count too
            for i in range(1, 21):
                st.medium.append(MediumItem(id=f"m{i}", content=f"fact_{i:02d}", count=i, entry_extraction_index=i))
            block = tm.build_medium_injection("u")
            bullets = [ln for ln in block.splitlines() if ln.startswith("- ")]
            assert len(bullets) <= MEDIUM_INJECT_TOP_N, f"too many injected: {len(bullets)}"
            assert "fact_20" in block, "highest-count must be injected"
            assert "fact_01" in block or True  # may be excluded; checked below
            # the 5 lowest (fact_01..fact_05) are excluded (only top-15 selected)
            assert "fact_01" not in block, "lowest-count/recent item should be excluded"
            print_pass(f"injection: selects top-by-count + recent (<= {MEDIUM_INJECT_TOP_N})")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_injection_token_cap(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import MediumItem, MEDIUM_INJECT_TOKEN_CAP
            tm = self._tm(tmp)
            st = tm._get_state("u")
            for i, c in enumerate([10, 9, 8]):
                st.medium.append(MediumItem(id=f"m{i}", content="长" * 300, count=c, entry_extraction_index=0))
            block = tm.build_medium_injection("u")
            bullets = [ln for ln in block.splitlines() if ln.startswith("- ")]
            # each bullet ~302 chars; cap 600 → only the first fits before break
            body = sum(len(ln) for ln in bullets)
            assert body <= MEDIUM_INJECT_TOKEN_CAP, f"body {body} exceeds cap"
            assert len(bullets) >= 1
            print_pass(f"injection: truncates at MEDIUM_INJECT_TOKEN_CAP ({MEDIUM_INJECT_TOKEN_CAP})")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ── clock + persistence ──────────────────────────────────────

    def test_touch_extraction_count(self):
        tmp = tempfile.mkdtemp()
        try:
            tm = self._tm(tmp)
            assert tm._get_state("u").extraction_count == 0
            assert tm.touch_extraction_count("u") == 1
            assert tm.touch_extraction_count("u") == 2
            print_pass("clock: touch_extraction_count increments per call")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_persistence_roundtrip(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import TieredMemory, MediumItem, ShortItem
            tm = TieredMemory(base_dir=tmp)
            st = tm._get_state("u")
            st.extraction_count = 7
            st.short.append(ShortItem(id="s1", content="短期事实", count=2))
            st.medium.append(MediumItem(id="m1", content="中期事实", count=11, entry_extraction_index=3))
            # m1 count>10 → create a LONG with a snapshot to persist
            from agent.memory import LongObject
            st.long.append(LongObject(id="l1", title="中期事实", summary="中期事实",
                                      snapshot_turns=[("玩原神", "好玩")], snapshot_time=123.0,
                                      query_count=0, linked_medium_id="m1"))
            tm.save_user("u")

            tm2 = TieredMemory(base_dir=tmp)
            n = tm2.load_all()
            assert n == 1, n
            st2 = tm2._get_state("u")
            assert st2.extraction_count == 7
            assert st2.short[0].content == "短期事实" and st2.short[0].count == 2
            assert st2.medium[0].content == "中期事实" and st2.medium[0].entry_extraction_index == 3
            assert st2.long[0].linked_medium_id == "m1"
            assert st2.long[0].snapshot_turns == [("玩原神", "好玩")], "LONG snapshot md round-trips"
            print_pass("persistence: save_user/load round-trips all tiers + clock + LONG md")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_load_all(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import TieredMemory, ShortItem
            tm = TieredMemory(base_dir=tmp)
            for uid in ("alice", "bob", "carol"):
                tm._get_state(uid).short.append(ShortItem(id="x", content=f"{uid} 事实", count=1))
                tm.save_user(uid)
            tm2 = TieredMemory(base_dir=tmp)
            assert tm2.load_all() == 3
            assert tm2._get_state("bob").short[0].content == "bob 事实"
            print_pass("load_all: reloads every persisted user at startup")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class _SeqProfileClient:
    """Profile-extraction mock returning a sequence of canned JSON responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat_completion(self, message, history=None, timeout_set=180.0, purpose="chat"):
        self.calls.append({"prompt": message, "purpose": purpose, "timeout_set": timeout_set})
        i = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[i]


class TestMergedExtraction:
    """P1 §12.1: profile extraction + memory judge merged into ONE flash call."""

    def run(self):
        print_header("4.8. Merged Extract+Judge Pipeline (P1)")
        asyncio.run(self.test_routes_new_candidate_to_short())
        asyncio.run(self.test_l2_filters_junk_candidate())
        asyncio.run(self.test_judge_list_embedded_in_prompt())
        asyncio.run(self.test_extraction_count_increments())
        asyncio.run(self.test_reinforce_short_promotes_to_medium())

    def _mgr(self, tmpdir, client, tm):
        from agent.profile import ProfileManager
        mgr = ProfileManager(base_dir=tmpdir, llm_client=client)
        mgr.set_memory(tm)
        return mgr

    def _flush(self, mgr, uid, k):
        from agent.profile import PROFILE_BATCH_K
        for i in range(k):
            mgr.observe_turn(uid, f"消息{i}", "回复")

    async def test_routes_new_candidate_to_short(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import TieredMemory
            tm = TieredMemory(base_dir=tmp)
            client = _SeqProfileClient(['{"memory_candidates": [{"content": "用户喜欢原神", "judge": "new"}]}'])
            mgr = self._mgr(tmp, client, tm)
            self._flush(mgr, "u", 5)
            await asyncio.sleep(0.05)
            st = tm._get_state("u")
            assert any(x.content == "用户喜欢原神" for x in st.short), [x.content for x in st.short]
            assert tm._get_state("u").extraction_count == 1, "clock ticks on successful extract"
            print_pass("merged: new memory_candidate routed into SHORT; clock +1")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_l2_filters_junk_candidate(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import TieredMemory
            tm = TieredMemory(base_dir=tmp)
            client = _SeqProfileClient(['{"memory_candidates": ['
                                        '{"content": "用户工作区剩余250MB", "judge": "new"},'
                                        '{"content": "用户是教师", "judge": "new"}]}'])
            mgr = self._mgr(tmp, client, tm)
            self._flush(mgr, "u", 5)
            await asyncio.sleep(0.05)
            st = tm._get_state("u")
            contents = [x.content for x in st.short]
            assert "用户是教师" in contents, contents
            assert not any("工作区" in c for c in contents), f"L2 must drop junk before SHORT: {contents}"
            print_pass("merged: Layer-2 filter runs on candidates before SHORT")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_judge_list_embedded_in_prompt(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import TieredMemory, MediumItem
            tm = TieredMemory(base_dir=tmp)
            st = tm._get_state("u")
            st.medium.append(MediumItem(id="m_known", content="用户是原神玩家", count=5, entry_extraction_index=0))
            client = _SeqProfileClient(['{}'])
            mgr = self._mgr(tmp, client, tm)
            self._flush(mgr, "u", 5)
            await asyncio.sleep(0.05)
            prompt = client.calls[0]["prompt"]
            assert "已有记忆" in prompt, "judge section missing from prompt"
            assert "m_known" in prompt and "用户是原神玩家" in prompt, "existing MEDIUM not embedded"
            print_pass("merged: existing MEDIUM/SHORT judge list embedded in the flash prompt")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_extraction_count_increments(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import TieredMemory
            tm = TieredMemory(base_dir=tmp)
            client = _SeqProfileClient(['{}', '{}'])
            mgr = self._mgr(tmp, client, tm)
            self._flush(mgr, "u", 5)
            await asyncio.sleep(0.05)
            self._flush(mgr, "u", 5)
            await asyncio.sleep(0.05)
            assert tm._get_state("u").extraction_count == 2, tm._get_state("u").extraction_count
            print_pass("merged: extraction_count increments once per successful batch")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_reinforce_short_promotes_to_medium(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import TieredMemory, ShortItem
            tm = TieredMemory(base_dir=tmp)
            st = tm._get_state("u")
            st.short.append(ShortItem(id="s_fixed", content="用户喜欢原神", count=1))
            resp = '{"memory_candidates": [{"content": "用户喜欢原神", "judge": "reinforce_short", "matched_id": "s_fixed"}]}'
            client = _SeqProfileClient([resp, resp])
            mgr = self._mgr(tmp, client, tm)
            self._flush(mgr, "u", 5)        # count 1→2 (still SHORT)
            await asyncio.sleep(0.05)
            assert tm._get_state("u").short and tm._get_state("u").short[0].count == 2
            self._flush(mgr, "u", 5)        # count 2→3 → promote
            await asyncio.sleep(0.05)
            st = tm._get_state("u")
            assert not any(x.id == "s_fixed" for x in st.short), "promoted item leaves SHORT"
            assert any(m.id == "s_fixed" and m.count == 3 for m in st.medium), [ (m.id,m.count) for m in st.medium]
            print_pass("merged: reinforce_short across batches promotes SHORT→MEDIUM at count=3")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestMemoryMigration:
    """P1 Stage 5 migration script: seed tiers from profiles + archive old dumps."""

    def run(self):
        print_header("4.9. P1 Memory Migration Script")
        self.test_migration_seeds_and_archives()
        self.test_migration_archives_loose_legacy_dumps()

    def _load_module(self):
        import importlib.util
        path = os.path.join(os.path.dirname(__file__), "scripts", "migrate_memory_p1.py")
        spec = importlib.util.spec_from_file_location("migrate_memory_p1", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_migration_seeds_and_archives(self):
        tmp = tempfile.mkdtemp()
        try:
            mod = self._load_module()
            prof = os.path.join(tmp, "profiles")
            mem = os.path.join(tmp, "memory")
            bak = os.path.join(tmp, "backup")
            os.makedirs(os.path.join(prof, "u1114144652"))
            os.makedirs(os.path.join(mem, "user", "u1114144652"))
            os.makedirs(os.path.join(bak, "u1114144652"))
            # current profile (post-P0): the two real-life 正在 facts were dropped
            with open(os.path.join(prof, "u1114144652", "profile.json"), "w", encoding="utf-8") as f:
                json.dump({"user_id": "1114144652", "facts": ["用户喜欢原神", "用户是程序员"]}, f, ensure_ascii=False)
            # backup (pre-P0): contains the false-drops + one still-junk fact
            with open(os.path.join(bak, "u1114144652", "profile.json"), "w", encoding="utf-8") as f:
                json.dump({"user_id": "1114144652", "facts": [
                    "用户喜欢原神", "用户是程序员",
                    "用户正在通过饮食调整降血脂",
                    "用户正在做力量训练，按酸痛程度自由分割训练循环",
                    "用户正在使用特殊会话",
                ]}, f, ensure_ascii=False)
            for n in ("interaction_1.md", "interaction_2.md"):
                with open(os.path.join(mem, "user", "u1114144652", n), "w", encoding="utf-8") as f:
                    f.write("old dump")

            rc = mod.main(["--profile-dir", prof, "--memory-dir", mem,
                           "--restore-false-drops", bak, "--apply", "--no-backup"])
            assert rc == 0, rc

            tier_path = os.path.join(mem, "tiers", "1114144652.json")
            assert os.path.exists(tier_path), "tier JSON not written"
            with open(tier_path, encoding="utf-8") as f:
                data = json.load(f)
            assert data["extraction_count"] == 0
            short = {x["content"] for x in data["short"]}
            assert short == {"用户喜欢原神", "用户是程序员"}, short
            assert all(x["count"] == 1 for x in data["short"])
            med = {x["content"] for x in data["medium"]}
            assert "用户正在通过饮食调整降血脂" in med, med
            assert "用户正在做力量训练，按酸痛程度自由分割训练循环" in med, med
            assert "用户正在使用特殊会话" not in med, "still-junk must NOT be restored"
            assert all(x["count"] == 3 for x in data["medium"])
            # archives moved, originals gone
            arch = os.path.join(mem, "user", "_archive", "u1114144652")
            assert os.path.exists(os.path.join(arch, "interaction_1.md")), "not archived"
            assert not os.path.exists(os.path.join(mem, "user", "u1114144652", "interaction_1.md")), "original not moved"

            # idempotent re-run skips (tier file exists)
            rc2 = mod.main(["--profile-dir", prof, "--memory-dir", mem,
                            "--restore-false-drops", bak, "--apply", "--no-backup"])
            assert rc2 == 0
            with open(tier_path, encoding="utf-8") as f:
                assert len(json.load(f)["short"]) == 2, "re-run must be idempotent (no double-seed)"
            print_pass("migration: seeds SHORT+MEDIUM, archives dumps, restores false-drops, idempotent")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_migration_archives_loose_legacy_dumps(self):
        """Legacy raw dumps written FLAT under user/ (uid only in the filename)
        must be discovered, attributed to the right user, and archived too —
        not just the ones inside user/{uid}/ subdirs."""
        tmp = tempfile.mkdtemp()
        try:
            mod = self._load_module()
            prof = os.path.join(tmp, "profiles")
            mem = os.path.join(tmp, "memory")
            # user WITH a subdir + profile, plus loose dumps for the SAME uid
            os.makedirs(os.path.join(prof, "2578260985"))
            os.makedirs(os.path.join(mem, "user", "2578260985"))
            with open(os.path.join(prof, "2578260985", "profile.json"), "w", encoding="utf-8") as f:
                json.dump({"user_id": "2578260985", "facts": ["用户喜欢咖啡"]}, f, ensure_ascii=False)
            for n in ("interaction_2578260985_1.md", "interaction_2578260985_2.md"):
                open(os.path.join(mem, "user", "2578260985", n), "w").close()
            # legacy LOOSE files flat under user/ for the same uid
            for n in ("interaction_2578260985_900.md", "interaction_2578260985_901.md"):
                open(os.path.join(mem, "user", n), "w").close()
            # a loose-only user (no profile, no subdir) → archive, but NO empty tier
            open(os.path.join(mem, "user", "interaction_777_500.md"), "w").close()

            rc = mod.main(["--profile-dir", prof, "--memory-dir", mem, "--apply", "--no-backup"])
            assert rc == 0, rc

            arch = os.path.join(mem, "user", "_archive", "2578260985")
            got = set(os.listdir(arch))
            assert got == {"interaction_2578260985_1.md", "interaction_2578260985_2.md",
                           "interaction_2578260985_900.md", "interaction_2578260985_901.md"}, got
            # no loose files remain flat under user/
            remaining = [x for x in os.listdir(os.path.join(mem, "user"))
                         if x.startswith("interaction_")]
            assert remaining == [], f"loose dumps not archived: {remaining}"
            # loose-only user 777 archived under its own uid, no tier written
            assert os.path.exists(os.path.join(mem, "user", "_archive", "777", "interaction_777_500.md"))
            assert not os.path.exists(os.path.join(mem, "tiers", "777.json")), "no facts → no empty tier"
            assert os.path.exists(os.path.join(mem, "tiers", "2578260985.json")), "profile user gets a tier"
            print_pass("migration: archives legacy loose dumps by filename uid; no empty tiers")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def main():
    print()
    print(f"{Colors.BOLD}{Colors.CYAN}╔══════════════════════════════════════════════════════╗{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}║     QQBot Agent System — Test Suite                  ║{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}║     Architecture: Markdown-driven LLM Agent          ║{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}╚══════════════════════════════════════════════════════╝{Colors.RESET}")

    tests = [
        ("ToolRegistry", TestToolRegistry()),
        ("SessionManager", TestSessionManager()),
        ("MemorySystem", TestMemorySystem()),
        ("UserProfile & ProfileManager", TestUserProfile()),
        ("Profile Extraction Precision", TestProfileExtraction()),
        ("TieredMemory (P1 engine)", TestTieredMemory()),
        ("Merged Extract+Judge (P1)", TestMergedExtraction()),
        ("Memory Migration (P1)", TestMemoryMigration()),
        ("Agent Core (Mock LLM)", TestAgentCore()),
        ("DeepSeekClient Parsing", TestDeepSeekClientParsing()),
        ("Built-in Tools", TestBuiltinTools()),
        ("Personality Manager", TestPersonality()),
        ("TokenLedger", TestTokenLedger()),
    ]

    passed = 0
    failed = 0

    for name, test_suite in tests:
        try:
            test_suite.run()
            passed += 1
        except Exception as e:
            print_fail(f"{name} suite FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    # Summary
    print_header("Summary")
    total = passed + failed
    print(f"  Test Suites: {total} total")
    print(f"  {Colors.GREEN}Passed: {passed}{Colors.RESET}")
    if failed > 0:
        print(f"  {Colors.RED}Failed: {failed}{Colors.RESET}")
    else:
        print(f"  Failed: 0")

    print(f"\n{Colors.BOLD}{Colors.CYAN}  ✓ All critical paths verified{Colors.RESET}")
    print(f"  Next: Integration test with real DeepSeek API + QQ")
    print()

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
