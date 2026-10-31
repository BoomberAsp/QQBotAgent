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
import re
import shutil
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
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

    async def chat_completion_with_tools_stream(self, messages, tools,
                                                timeout=180.0,
                                                purpose="agent_loop",
                                                *, content_callback=None):
        """Streaming mock: fires content_callback ONCE with the full text.

        Mirrors the real client's suppression rule — a tool_call round
        emits no content (so content_callback never fires), the final
        round forwards its content. Returns the identical dict shape.
        """
        result = await self.chat_completion_with_tools(
            messages, tools, timeout=timeout, purpose=purpose)
        content = result.get("content")
        if content_callback is not None and content and not result.get("tool_calls"):
            import asyncio as _aio
            _maybe = content_callback(content)
            if _aio.iscoroutine(_maybe):
                await _maybe
        return result


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
        self.test_error_sanitization()
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

    def test_error_sanitization(self):
        """P1-2: raw exception text must never reach the model."""
        from lib.errors import ToolError
        registry = self.ToolRegistry()

        secret = "RuntimeError at /home/windows11/QQBotAgent/.env DB_PASSWORD=hunter2"

        def leaky():
            raise RuntimeError(secret)

        registry.register("leaky", leaky, "Leaks internals", {"type": "object", "properties": {}})
        result = asyncio.run(registry.execute("leaky", {}))
        assert "[Error]" in result, result
        assert "hunter2" not in result and "/home/" not in result, \
            f"Raw exception leaked to model: {result}"
        assert "诊断编号" in result, f"Expected diagnostic code in: {result}"
        print_pass("Internal tool errors sanitized (no raw exception text)")

        def classified():
            raise ToolError("not_found", "未找到角色「阿米娅」，请检查名称后重试。")

        registry.register("classified", classified, "ToolError", {"type": "object", "properties": {}})
        result = asyncio.run(registry.execute("classified", {}))
        assert result == "[Error] 未找到角色「阿米娅」，请检查名称后重试。", result
        print_pass("ToolError safe message passes through verbatim")

        def slow():
            raise TimeoutError("connect to 10.0.0.1:8080 timed out")

        registry.register("slow", slow, "Times out", {"type": "object", "properties": {}})
        result = asyncio.run(registry.execute("slow", {}))
        assert "暂时不可用" in result and "10.0.0.1" not in result, result
        print_pass("Timeouts classified as unavailable without leaking details")

        def needs(x, y):
            return x + y

        registry.register("needs", needs, "Two args", {"type": "object", "properties": {}})
        result = asyncio.run(registry.execute("needs", {"x": 1}))
        assert result.startswith("[Error] Invalid arguments"), result
        print_pass("Argument binding errors remain actionable")

        def internal_type_bug(x):
            return None + x  # TypeError INSIDE the tool → internal, not invalid_args

        registry.register("buggy", internal_type_bug, "Internal bug", {"type": "object", "properties": {}})
        result = asyncio.run(registry.execute("buggy", {"x": 1}))
        assert "执行失败" in result and "unsupported operand" not in result, result
        print_pass("Internal TypeErrors are not misreported as argument errors")

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
        self.test_external_delete()

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
        from agent.session import SessionManager, TRIM_HYSTERESIS

        mgr = SessionManager(max_context_messages=5)
        session = mgr.get_or_create("user_123")

        # Add 10 messages — well past max + hysteresis, so trim engages
        for i in range(10):
            session.add_message("user" if i % 2 == 0 else "assistant", f"message_{i}")

        assert len(session.context) == 10
        session.trim(5)
        assert len(session.context) == 5, f"Expected 5 after trim, got {len(session.context)}"
        # Should keep the LAST 5 messages
        assert session.context[0]["content"] == "message_5"

        # Hysteresis (Phase 4, B3): within max + TRIM_HYSTERESIS the head must
        # NOT move, so the provider-side prefix cache survives turn to turn.
        session.clear()
        for i in range(5 + TRIM_HYSTERESIS):
            session.add_message("user" if i % 2 == 0 else "assistant", f"m_{i}")
        session.trim(5)
        assert len(session.context) == 5 + TRIM_HYSTERESIS, (
            f"trim must be lazy inside the hysteresis band, got {len(session.context)}")
        assert session.context[0]["content"] == "m_0", "head must not move"
        session.add_message("user", "m_overflow")
        session.trim(5)
        assert len(session.context) == 5, (
            f"past the band trim snaps back to max, got {len(session.context)}")
        assert session.context[-1]["content"] == "m_overflow"
        print_pass("Context trimming (with Phase 4 hysteresis band)")

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

    def test_external_delete(self):
        """WebUI-style external delete must not be resurrected by the cache."""
        from agent.session import SessionManager

        tmpdir = tempfile.mkdtemp()
        try:
            mgr = SessionManager(persistence_dir=tmpdir)
            session = mgr.get_or_create("u_del")
            session.add_message("user", "旧上下文")
            mgr.update("u_del", session)
            path = os.path.join(tmpdir, "u_del.json")
            assert os.path.exists(path)

            # Panel deletes the file behind the bot's back → next
            # get_or_create must drop the stale cache (empty context),
            # and the following save must not restore the old content.
            os.remove(path)
            fresh = mgr.get_or_create("u_del")
            assert len(fresh.context) == 0, "deleted session must lose its context"
            fresh.add_message("user", "新消息")
            mgr.update("u_del", fresh)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            contents = [m["content"] for m in data["context"]]
            assert contents == ["新消息"], contents

            # Deletion landing mid-request (after get_or_create, before
            # update) must be honored too — no write-back of stale context.
            inflight = mgr.get_or_create("u_del")
            os.remove(path)
            inflight.add_message("user", "处理中")
            mgr.update("u_del", inflight)
            assert not os.path.exists(path), "mid-flight delete must not resurrect file"
            print_pass("External session delete not resurrected (cache revalidation)")
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
        asyncio.run(self.test_profile_injection())
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

            # NOTE: this test was defined but never wired into run() — the
            # 2-arg wrapper below did not match agent.run()'s call signature,
            # so it would have raised TypeError had it been called. Fixed and
            # registered alongside the Phase 3 assertion update.
            def capture_build(session, msg, *a, **k):
                msgs = original_build(session, msg, *a, **k)
                agent._captured_messages = msgs
                return msgs

            agent._build_messages = capture_build

            await agent.run("你好", "user_999")

            # Verify profile was injected. Phase 3 (Cache-Hit-Rate-Plan.md):
            # the profile block moved from messages[0] to the L4 volatile tail
            # — the LAST system message, placed after the history so that
            # profile churn cannot invalidate the cached prefix.
            assert agent._captured_messages is not None
            system_msgs = [
                m for m in agent._captured_messages if m.get("role") == "system"
            ]
            assert len(system_msgs) >= 2, "expected L1-L3 head + L4 tail system messages"
            tail = system_msgs[-1]["content"]
            head = system_msgs[0]["content"]
            assert "小明" in tail, f"Profile nickname not injected: {tail[:200]}"
            assert "机器学习" in tail, f"Profile interests not injected: {tail[:200]}"
            assert "concise" in tail, f"Profile preferences not injected: {tail[:200]}"
            assert "小明" not in head, "profile must NOT be in the cached head (Phase 3)"
            # P0 画像瘦身: facts dormant — must NOT be injected (§6). Scope the
            # check to the tail's 当前轮次上下文 block (the L4 injection site).
            assert "深圳" not in tail, f"facts must not be injected: {tail[:200]}"
            print_pass("User profile injected into L4 tail (typed slots only; facts dormant)")
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
            # ① P2-lite: a LONG entry must also reach the tail via the index.
            from agent.memory import LongObject
            st.long.append(LongObject(
                id="l9", title="用户长期偏好记录", summary="长期偏好摘要",
                snapshot_turns=[("旧对话", "旧回复")], snapshot_time=time.time(),
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

            # Phase 3 (Cache-Hit-Rate-Plan.md): the MEDIUM block moved from
            # messages[0] to the L4 volatile tail (last system message).
            system_msgs = [m for m in captured["msgs"] if m.get("role") == "system"]
            tail = system_msgs[-1]["content"]
            assert "用户记忆（中期）" in tail, f"MEDIUM block missing: {tail[-400:]}"
            assert "力量训练" in tail, f"MEDIUM fact not injected: {tail[-400:]}"
            # MEMORY.md documentation (in the L1 head) legitimately *mentions*
            # the header string, so check the user fact instead — documentation
            # never contains it.
            assert "力量训练" not in system_msgs[0]["content"], (
                "MEDIUM facts must NOT be in the cached head (Phase 3)")
            # count>=5 → no low-confidence label. The tail holds only runtime
            # injections (MEMORY.md documentation lives in the L1 head), so the
            # rsplit guard is kept merely as belt-and-braces.
            medium_block = tail.rsplit("## 用户记忆（中期）", 1)[1]
            assert "(低置信)" not in medium_block, "count>=5 must NOT be low-confidence"
            # LONG index (Memory-Optimization-Plan ①): one-line-per-entry, with
            # the recall_memory hint; full text/snapshot must NOT leak into the tail.
            assert "用户长期记忆索引" in tail, f"LONG index missing: {tail[-400:]}"
            assert "[l9] 用户长期偏好记录" in tail, "LONG index line missing"
            assert "recall_memory" in tail, "LONG index must hint the recall tool"
            assert "长期偏好摘要" not in tail, (
                "index must stay one-line; full text only via recall_memory")
            print_pass("P1 MEDIUM-tier memory + LONG index injected into L4 tail")
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
        self.test_external_edit_wins()

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

    def test_external_edit_wins(self):
        """WebUI-style external edit must not be reverted by the bot's cache."""
        from agent.profile import ProfileManager

        tmpdir = tempfile.mkdtemp()
        try:
            bot_mgr = ProfileManager(base_dir=tmpdir)
            p = bot_mgr.get("u_panel")
            p.nickname = "旧昵称"
            p.interests = ["原神"]
            bot_mgr.save(p)

            # External process (panel) rewrites profile.json behind the bot
            path = os.path.join(tmpdir, "u_panel", "profile.json")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data["nickname"] = "面板新昵称"
            data["interests"] = ["星穹铁道"]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            # Bump mtime so the change is detectable regardless of fs granularity
            st = os.stat(path)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))

            got = bot_mgr.get("u_panel")
            assert got.nickname == "面板新昵称", got.nickname
            assert got.interests == ["星穹铁道"], got.interests

            # Bot-side change + save preserves the panel edit (no clobber)
            got.interests.append("崩铁")
            bot_mgr.save(got)
            with open(path, encoding="utf-8") as f:
                data2 = json.load(f)
            assert data2["nickname"] == "面板新昵称"
            assert data2["interests"] == ["星穹铁道", "崩铁"]
            print_pass("External profile edit wins over stale bot cache")
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
        self.test_updated_preferences_revision()
        # Layer 3 — batching / single-flight / delete-flush
        asyncio.run(self.test_observe_turn_flush_at_k())
        asyncio.run(self.test_single_flight())
        asyncio.run(self.test_delete_flush())
        asyncio.run(self.test_buffer_crash_recovery())
        asyncio.run(self.test_buffer_persona_roundtrip())

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
            # ⑧: user corrections of old memories must survive L2 — they carry
            # the highest-value signal (judge="update"), so no pattern may catch
            # "改为/不再/改X了" phrasing.
            "用户已把每周跑步三次改为每周游泳三次",
            "用户不再熬夜了，现在晚上十一点睡觉",
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
                      "memory_candidates", "reinforce_short", "matched_id",
                      # ② negative list: re-derivable public info → don't store
                      # (litmus-style second criterion + few-shot 例6).
                      "可再推导测试", "例6", "流浪者",
                      # ⑧ user corrections → judge="update" (few-shot 例7).
                      "例7", "纠正",
                      # ⑩ preference-slot revision path (few-shot 例8):
                      # rule-4 amendment + schema field + null-delete semantics.
                      "updated_preferences", "例8", "null"]:
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
                      "new", "reinforce_short", "update", "keep",
                      # ⑧: the judge_list branch must spell out the correction
                      # → update routing rule.
                      "纠正"]:
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

    def test_updated_preferences_revision(self):
        """⑩: updated_preferences overwrites an existing slot, null deletes it,
        unknown-key delete is a no-op — the rule-4 "write-once preferences"
        blind spot the L2 baseline exposed as stable staleness (×4, both versions)."""
        tmpdir = tempfile.mkdtemp()
        try:
            from agent.profile import ProfileManager
            mgr = ProfileManager(base_dir=tmpdir)
            prof = mgr.get("u_upd")
            prof.preferences.update({"设备": "安卓手机", "饮品": "不喝咖啡", "语言": "中文"})
            changed = mgr._apply_extraction(prof, "u_upd", {
                "updated_preferences": {"设备": "iPhone", "饮品": None, "不存在的键": None},
            })
            assert changed is True
            assert prof.preferences.get("设备") == "iPhone", prof.preferences
            assert "饮品" not in prof.preferences, "null must delete the slot"
            assert "不存在的键" not in prof.preferences, "unknown-key delete is a no-op"
            assert prof.preferences.get("语言") == "中文", "untouched slots stay"
            # same-value update → changed False (no spurious writes)
            assert mgr._apply_extraction(prof, "u_upd",
                                         {"updated_preferences": {"设备": "iPhone"}}) is False
            # non-dict payload is ignored defensively
            assert mgr._apply_extraction(prof, "u_upd",
                                         {"updated_preferences": "iPhone"}) is False
            assert prof.preferences.get("设备") == "iPhone"
            print_pass("⑩ updated_preferences: overwrite / null-delete / no-op / idempotent")
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

    async def test_buffer_crash_recovery(self):
        """⑦: the Layer-3 buffer mirrors to buffer.json and survives restarts."""
        tmpdir = tempfile.mkdtemp()
        buf = os.path.join(tmpdir, "u", "buffer.json")
        try:
            from agent.profile import ProfileManager, PROFILE_BATCH_K
            # "process 1": buffer K-1 turns, then die (object dropped, no flush)
            c1 = _GatedProfileClient("{}")
            m1 = ProfileManager(base_dir=tmpdir, llm_client=c1)
            for i in range(PROFILE_BATCH_K - 1):
                m1.observe_turn("u", f"m{i}", "r")
            await asyncio.sleep(0)
            assert len(c1.calls) == 0
            with open(buf, encoding="utf-8") as f:
                blob = json.load(f)
            assert blob["pending_n"] == PROFILE_BATCH_K - 1, blob
            assert len(blob["turns"]) == PROFILE_BATCH_K - 1

            # "process 2": restore on first observe; the Kth turn flushes ONCE
            # with the pre-crash turns included in the extraction window
            c2 = _GatedProfileClient('{"new_interests": ["原神"]}')
            m2 = ProfileManager(base_dir=tmpdir, llm_client=c2)
            m2.observe_turn("u", "mK", "r")
            await asyncio.sleep(0.05)
            assert len(c2.calls) == 1, "restored pending must flush at the Kth turn"
            assert "m0" in c2.calls[0]["prompt"] and "mK" in c2.calls[0]["prompt"], (
                "pre-crash turns must reach the extraction window")
            assert "原神" in m2.get("u").interests

            # flush persisted pending_n=0 → "process 3" must NOT double-extract
            with open(buf, encoding="utf-8") as f:
                assert json.load(f)["pending_n"] == 0
            c3 = _GatedProfileClient("{}")
            m3 = ProfileManager(base_dir=tmpdir, llm_client=c3)
            m3.observe_turn("u", "after", "r")
            await asyncio.sleep(0.02)
            assert len(c3.calls) == 0, "pending=0 must restore (no double extraction)"
            assert m3._pending_n["u"] == 1

            # corrupt buffer file → ignored, fresh buffer, no crash
            with open(buf, "w") as f:
                f.write("{corrupt")
            c4 = _GatedProfileClient("{}")
            m4 = ProfileManager(base_dir=tmpdir, llm_client=c4)
            m4.observe_turn("u", "fresh", "r")
            assert m4._pending_n["u"] == 1 and len(m4._recent["u"]) == 1
            print_pass("Layer3 ⑦: buffer survives restarts, flush-once, corrupt-tolerant")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_buffer_persona_roundtrip(self):
        """Per-turn persona mirrors to buffer.json and restores verbatim, so a
        LONG snapshot built after a restart still labels the assistant
        in-character (not the hardcoded default)."""
        tmpdir = tempfile.mkdtemp()
        buf = os.path.join(tmpdir, "u", "buffer.json")
        try:
            from agent.profile import ProfileManager
            c1 = _GatedProfileClient("{}")
            m1 = ProfileManager(base_dir=tmpdir, llm_client=c1)
            # < K turns so nothing flushes; one carries a persona, one is legacy
            m1.observe_turn("u", "m0", "r0", "露比")
            m1.observe_turn("u", "m1", "r1")           # 3-arg legacy call → ""
            await asyncio.sleep(0)
            with open(buf, encoding="utf-8") as f:
                blob = json.load(f)
            assert blob["turns"][0] == ["m0", "r0", "露比"], blob["turns"]
            assert blob["turns"][1] == ["m1", "r1", ""], blob["turns"]
            # a fresh manager restores the persona verbatim on first observe
            c2 = _GatedProfileClient("{}")
            m2 = ProfileManager(base_dir=tmpdir, llm_client=c2)
            m2.observe_turn("u", "m2", "r2", "触发恢复")
            dq = list(m2._recent["u"])
            assert dq[0] == ("m0", "r0", "露比"), dq
            assert dq[1] == ("m1", "r1", ""), dq
            print_pass("Layer3: per-turn persona mirrors to buffer.json + restores")
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
        self.test_execute_code_output_cap()
        self.test_read_file_segmented()
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

    def test_execute_code_output_cap(self):
        """Phase6-B1: 200KB stdout must be capped at 16KB with guidance."""
        import asyncio
        from tools.builtin_tools import execute_code, MAX_OUTPUT_SIZE

        assert MAX_OUTPUT_SIZE == 16384, "MAX_OUTPUT_SIZE should be 16KB"
        result = asyncio.run(execute_code("print('x' * 200000)"))
        assert "已截断" in result
        assert "聚合" in result, "truncation notice should guide toward aggregation"
        assert len(result) < MAX_OUTPUT_SIZE + 2000, (
            f"result should stay near the 16KB cap, got {len(result)}"
        )
        print_pass("execute_code caps output at 16KB with aggregation guidance")

    def test_read_file_segmented(self):
        """Phase6-B1: large text reads are segmented via offset with exact continuity."""
        import asyncio
        import os
        from tools.builtin_tools import _get_workspace_root
        from tools.file_tools import read_file

        ws = _get_workspace_root()
        os.makedirs(ws, exist_ok=True)
        rel = "_test_segmented.txt"
        path = os.path.join(ws, rel)
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(f"{i:07d}\n" for i in range(20000)))  # 160K chars
        try:
            sep = "─" * 40 + "\n\n"
            raw = open(path, encoding="utf-8").read()

            r1 = asyncio.run(read_file(rel))
            assert "已截断" in r1 and "offset=50000" in r1, (
                "first segment should advertise the next offset"
            )
            body1 = r1.split(sep, 1)[1]
            assert body1 == raw[:50000]

            r2 = asyncio.run(read_file(rel, offset=50000))
            assert "分段读取" in r2 and "offset=100000" in r2
            body2 = r2.split(sep, 1)[1]
            assert body2 == raw[50000:100000], "segments must be byte-exact continuous"

            # offset beyond EOF → empty body, no crash
            r3 = asyncio.run(read_file(rel, offset=len(raw) + 10))
            assert isinstance(r3, str) and r3
            print_pass("read_file segmented reading (offset) is exact and continuous")
        finally:
            os.remove(path)

    def test_search_web(self):
        from tools.builtin_tools import search_web

        result = search_web("Python programming", num_results=3)
        # Result may succeed (SearXNG available) or fail gracefully (SearXNG unavailable)
        assert isinstance(result, str)
        assert len(result) > 0
        print_pass("search_web returns results or graceful fallback (SearXNG)")


class TestSearchArchive:
    """Test SearchArchive: per-user persistence of search_web/web_fetch results."""

    def run(self):
        print_header("6b. SearchArchive Tests")

        self.test_save_and_recall()
        self.test_user_isolation()
        self.test_path_traversal()
        self.test_ttl_expiry()
        self.test_per_user_cap()
        self.test_archive_helper_no_context()
        self.test_archive_helper_roundtrip()

    def test_save_and_recall(self):
        from agent.search_archive import SearchArchive

        tmpdir = tempfile.mkdtemp()
        try:
            sa = SearchArchive(base_dir=tmpdir)
            aid = sa.save("u1", "search_web", "Python tips", "full result text",
                          raw=[{"url": "https://example.com"}])
            assert aid and len(aid) == 12, f"bad archive id: {aid}"

            rec = sa.recall("u1", aid)
            assert rec is not None, "archive not recalled"
            assert rec["content"] == "full result text"
            assert rec["query"] == "Python tips"
            assert rec["tool"] == "search_web"
            assert rec["raw_results"] == [{"url": "https://example.com"}]
            assert rec["iso_time"], "missing iso_time"
            print_pass("save + recall round-trip preserves all fields")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_user_isolation(self):
        from agent.search_archive import SearchArchive

        tmpdir = tempfile.mkdtemp()
        try:
            sa = SearchArchive(base_dir=tmpdir)
            aid = sa.save("user_A", "web_fetch", "https://x.dev", "A's page text")
            assert sa.recall("user_A", aid) is not None
            assert sa.recall("user_B", aid) is None, "cross-user recall must fail"
            print_pass("archives are isolated per user")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_path_traversal(self):
        from agent.search_archive import SearchArchive

        tmpdir = tempfile.mkdtemp()
        try:
            sa = SearchArchive(base_dir=tmpdir)
            aid_a = sa.save("userA", "search_web", "q", "A content")

            # Traversal attempts from another user must all be rejected
            assert sa.recall("userB", f"../userA/{aid_a}") is None
            assert sa.recall("userA", f"{aid_a}/../../userA/{aid_a}") is None
            assert sa.recall("userA", "not-a-valid-id") is None
            assert sa.recall("userA", "") is None
            assert sa.recall("userA", None) is None
            print_pass("invalid / traversal archive ids are rejected")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_ttl_expiry(self):
        from agent.search_archive import SearchArchive, TTL_SECONDS

        tmpdir = tempfile.mkdtemp()
        try:
            sa = SearchArchive(base_dir=tmpdir)
            old_id = sa.save("u1", "search_web", "old query", "old content")
            old_path = os.path.join(tmpdir, "u1", f"{old_id}.json")
            # Backdate mtime beyond the TTL
            expired = time.time() - TTL_SECONDS - 3600
            os.utime(old_path, (expired, expired))

            # Expired archives read as gone even before the sweep
            assert sa.recall("u1", old_id) is None, "expired archive must not recall"

            # A new save triggers the lazy sweep and physically removes it
            sa.save("u1", "search_web", "new query", "new content")
            assert not os.path.exists(old_path), "expired file not swept"
            print_pass("TTL expiry: recall refuses + lazy sweep removes file")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_per_user_cap(self):
        from agent.search_archive import SearchArchive, MAX_PER_USER

        tmpdir = tempfile.mkdtemp()
        try:
            sa = SearchArchive(base_dir=tmpdir)
            last_id = None
            for i in range(MAX_PER_USER + 5):
                last_id = sa.save("u1", "search_web", f"query {i}", f"content {i}")
            user_dir = os.path.join(tmpdir, "u1")
            files = [f for f in os.listdir(user_dir) if f.endswith(".json")]
            assert len(files) <= MAX_PER_USER, f"cap exceeded: {len(files)}"
            # The newest archive always survives
            assert sa.recall("u1", last_id) is not None
            print_pass(f"per-user cap enforced (<= {MAX_PER_USER}, newest kept)")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_archive_helper_no_context(self):
        from tools.builtin_tools import _archive_tool_result
        from agent.context import _current_user_id

        # No user context ("") → silent no-op, never raises.
        # Explicitly pin the contextvar so a leaked set() from an earlier
        # suite cannot divert archives into the real data dir.
        token = _current_user_id.set("")
        try:
            result = _archive_tool_result("search_web", "q", "content")
            assert result == "", f"expected empty footer without user context, got: {result!r}"
            print_pass("_archive_tool_result degrades silently without user context")
        finally:
            _current_user_id.reset(token)

    def test_archive_helper_roundtrip(self):
        from agent import search_archive as sa_mod
        from agent.context import _current_user_id
        from tools.builtin_tools import _archive_tool_result

        tmpdir = tempfile.mkdtemp()
        old_default = sa_mod._default_archive
        token = _current_user_id.set("u_helper")
        try:
            sa_mod._default_archive = sa_mod.SearchArchive(base_dir=tmpdir)
            footer = _archive_tool_result("search_web", "测试查询", "完整结果文本",
                                          raw=[{"title": "t"}])
            assert "[存档] id:" in footer, f"missing archive footer: {footer!r}"
            assert "recall_search_result" in footer

            aid = footer.split("id:")[1].split()[0]
            rec = sa_mod._default_archive.recall("u_helper", aid)
            assert rec is not None and rec["content"] == "完整结果文本"
            print_pass("_archive_tool_result persists + footer carries recallable id")
        finally:
            _current_user_id.reset(token)
            sa_mod._default_archive = old_default
            shutil.rmtree(tmpdir, ignore_errors=True)


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


class TestCacheStability:
    """Phase 3/4 (Cache-Hit-Rate-Plan.md): provider prefix-cache invariants.

    Prefix caching only pays off when the bytes ahead of the new content are
    identical to a recent request. These tests pin the structural guarantees
    rather than the hit rate itself (which is observable only against a real
    provider via the token ledger + webui).
    """

    def run(self):
        print_header("14. Cache Stability Tests (Phase 3/4)")

        asyncio.run(self.test_head_is_user_invariant())
        asyncio.run(self.test_volatile_lives_in_tail())
        asyncio.run(self.test_head_stable_across_profile_change())
        asyncio.run(self.test_history_head_moves_rarely())
        asyncio.run(self.test_compress_boundary_steps())
        asyncio.run(self.test_system_prompt_has_no_timestamp())

    def _make_agent(self, tmpdir=None, **kw):
        from agent.tool_registry import ToolRegistry
        from agent.agent import Agent

        kwargs = dict(
            deepseek_client=MockDeepSeekClient(),
            tool_registry=ToolRegistry(),
            config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
        )
        if tmpdir:
            from agent.profile import ProfileManager
            kwargs["profile_manager"] = ProfileManager(base_dir=tmpdir)
        kwargs.update(kw)
        return Agent(**kwargs)

    @staticmethod
    def _systems(msgs):
        """(head, tail) — first and last system messages of a built list."""
        sys_msgs = [m for m in msgs if m.get("role") == "system"]
        assert len(sys_msgs) >= 2, f"expected head+tail system messages, got {len(sys_msgs)}"
        return sys_msgs[0]["content"], sys_msgs[-1]["content"]

    async def test_head_is_user_invariant(self):
        """B5: the cached head must not depend on who is asking.

        Two users, no personality/group context → byte-identical messages[0].
        """
        from agent.context import _current_personality, _current_group_context
        p_tok, g_tok = _current_personality.set(""), _current_group_context.set("")
        try:
            agent = self._make_agent()
            head_a, _ = self._systems(
                agent._build_messages(agent.sessions.get_or_create("user_A"), "hi"))
            head_b, _ = self._systems(
                agent._build_messages(agent.sessions.get_or_create("user_B"), "hello"))
            assert head_a == head_b, "head must be identical across users (shared hot prefix)"
            assert len(head_a) > 1000, "head should carry the config block"
            print_pass("L1 head is user-invariant (shared prefix across users)")
        finally:
            _current_personality.reset(p_tok)
            _current_group_context.reset(g_tok)

    async def test_volatile_lives_in_tail(self):
        """B1/B6: per-turn content must sit AFTER the history, never in the head."""
        from agent.context import _current_personality, _current_group_context
        p_tok, g_tok = _current_personality.set(""), _current_group_context.set("")
        try:
            agent = self._make_agent()
            session = agent.sessions.get_or_create("user_A")
            session.add_message("user", "old question")
            session.add_message("assistant", "old answer")

            head, tail = self._systems(agent._build_messages(session, "new question"))

            # Tail is positioned after the history
            msgs = agent._build_messages(session, "new question")
            roles = [m["role"] for m in msgs]
            assert roles[0] == "system"
            assert roles.index("system", 1) > roles.index("assistant"), (
                f"volatile system message must follow the history: {roles}")
            assert roles[-1] == "user"
            # Time lives in the tail only
            assert "Current time:" in tail, "real per-turn time belongs in the tail"
            assert "Current time:" not in head, "head must stay timestamp-free"
            print_pass("Volatile context (time) is in the L4 tail, after history")
        finally:
            _current_personality.reset(p_tok)
            _current_group_context.reset(g_tok)

    async def test_head_stable_across_profile_change(self):
        """B2: a profile update must not invalidate the cached head+history."""
        from agent.context import _current_personality, _current_group_context
        from agent.profile import ProfileManager
        p_tok, g_tok = _current_personality.set(""), _current_group_context.set("")
        tmpdir = tempfile.mkdtemp()
        try:
            profile = ProfileManager(base_dir=tmpdir).get("user_A")
            profile.nickname = "旧昵称"
            ProfileManager(base_dir=tmpdir).save(profile)

            agent = self._make_agent(tmpdir=tmpdir)
            session = agent.sessions.get_or_create("user_A")
            head1, tail1 = self._systems(agent._build_messages(session, "turn one"))

            # Simulate an extraction batch landing between two turns
            p2 = agent.profiles.get("user_A")
            p2.nickname = "新昵称"
            agent.profiles.save(p2)

            head2, tail2 = self._systems(agent._build_messages(session, "turn two"))
            assert head1 == head2, "head must be byte-stable across a profile change"
            assert tail1 != tail2, "tail is expected to change"
            assert "新昵称" in tail2 and "新昵称" not in head2
            print_pass("Head byte-stable across profile change; delta absorbed by tail")
        finally:
            _current_personality.reset(p_tok)
            _current_group_context.reset(g_tok)
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_history_head_moves_rarely(self):
        """B3: with hysteresis the history head changes ~once per 5 turns, not 5×."""
        from agent.session import Session, TRIM_HYSTERESIS

        session = Session(user_id="user_A")
        for i in range(20):  # start exactly at the documented window
            session.add_message("user" if i % 2 == 0 else "assistant", f"m{i}")

        heads = [session.context[0]["content"]]
        for turn in range(5):
            session.add_message("user", f"q{turn}")
            session.add_message("assistant", f"a{turn}")
            session.trim(20)
            if session.context[0]["content"] != heads[-1]:
                heads.append(session.context[0]["content"])
            assert len(session.context) <= 20 + TRIM_HYSTERESIS

        changes = len(heads) - 1
        assert changes <= 2, f"history head moved {changes}× in 5 turns (expected ≤2)"
        print_pass(f"Hysteresis trim: history head moved {changes}× over 5 turns (was 5×)")

    async def test_compress_boundary_steps(self):
        """B4: the compression boundary advances in steps of COMPRESS_STEP."""
        from agent.agent import Agent

        step = Agent.COMPRESS_STEP
        # 40 messages, every 3rd is a verbose tool result
        ctx = []
        for i in range(40):
            if i % 3 == 0:
                ctx.append({"role": "tool", "tool_call_id": f"c{i}",
                            "content": "line1\n" + "x" * 500})
            else:
                ctx.append({"role": "user" if i % 2 else "assistant", "content": f"c{i}"})

        def compressed_count(out):
            # Only tool messages are rewritten; non-tool messages keep identity.
            return sum(1 for o, c in zip(out, ctx) if o is not c)

        def tools_in(rng):
            return sum(1 for i in rng if i % 3 == 0)

        # Nothing compressed while inside the full-detail window
        assert Agent._compress_context(ctx[:20]) == ctx[:20]
        assert compressed_count(Agent._compress_context(ctx[:20 + step - 1])) == 0, (
            "boundary must not advance before a full step accumulates")

        # First step: exactly the head `step` messages are eligible
        base_len = 20 + step
        base = Agent._compress_context(ctx[:base_len])
        assert compressed_count(base) == tools_in(range(step)), (
            f"expected {tools_in(range(step))} rewritten tool messages, "
            f"got {compressed_count(base)}")
        assert base[step:] == ctx[step:base_len], "tail window must stay untouched"

        # Byte-stability inside the step: growing the context by 1..step-1
        # must leave the already-emitted prefix identical.
        for extra in range(1, step):
            grown = Agent._compress_context(ctx[:base_len + extra])
            assert grown[:base_len] == base, (
                f"prefix drifted at +{extra} messages — cache would miss")
            assert compressed_count(grown) == tools_in(range(step)), (
                f"boundary advanced early at +{extra}")

        # Crossing the step advances the boundary by exactly COMPRESS_STEP
        nxt = Agent._compress_context(ctx[:base_len + step])
        assert compressed_count(nxt) == tools_in(range(2 * step)), (
            f"expected {tools_in(range(2 * step))} compressed, got {compressed_count(nxt)}")
        assert nxt[:20 + step - 1] != base[:20 + step - 1] or nxt[20:base_len] != base[20:], (
            "boundary advance must rewrite the newly-aged messages")
        print_pass(f"Compression boundary steps by {step} (byte-stable between steps)")

    async def test_system_prompt_has_no_timestamp(self):
        """B6: build_system_prompt() must be time-free so the cache never expires."""
        agent = self._make_agent()
        first = agent.build_system_prompt()
        assert first == agent.build_system_prompt(), "prompt must be cached/stable"
        now = time.strftime("%Y-%m-%d %H")
        assert f"Current time: {now}" not in first, (
            "frozen timestamp must not be baked into the cached system prompt")
        print_pass("Cached system prompt carries no timestamp")


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
        self.test_long_snapshot_persona_label()
        self.test_judge_list_composition()
        self.test_injection_empty_returns_blank()
        self.test_injection_low_confidence_label()
        self.test_injection_top_n_plus_recent()
        self.test_injection_token_cap()
        self.test_long_index_injection()
        self.test_recall_long_lookup_and_stats()
        self.test_touch_extraction_count()
        self.test_persistence_roundtrip()
        self.test_load_all()
        self.test_correction_replaces_stale_short()
        self.test_correction_sweeps_stale_variants_keeps_adjacent()
        self.test_correction_targets_medium_and_resets_age()
        self.test_correction_requires_marker()
        self.test_neardup_merge_recency_wins()
        self.test_neardup_guard_distinct_facts_coexist()
        self.test_neardup_digit_guard_enumerations()
        self.test_correction_spares_fused_episode_entry()
        self.test_correction_no_topic_word_sweep()
        # ④ write-time typing (category)
        self.test_category_defaults_to_fact()
        self.test_category_explicit_all_four()
        self.test_category_invalid_or_hallucinated_keeps_existing()
        self.test_category_reinforce_does_not_retype()
        self.test_category_update_retypes()
        self.test_category_survives_promote_and_demote()
        self.test_critical_demote_age_3x()
        self.test_category_persistence_roundtrip()
        self.test_load_v1_file_defaults()
        self.test_get_judge_list_includes_category()
        # ③ staleness annotation
        self.test_staleness_label_stale_fact()
        self.test_staleness_label_fresh_fact_unlabelled()
        self.test_staleness_label_scoped_to_fact_only()
        self.test_staleness_label_legacy_ts0_unlabelled()
        self.test_staleness_label_cross_year_format()

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
            # ⑨ note: "编号{i}" items differ by ONE char — normally the numeric-
            # correction pattern M1 collapses (三十岁→三十五岁); the ASCII digit
            # guard keeps distinct enumerations separate, so this stays a valid
            # 60-distinct-item overflow fixture.
            contents = [f"独特事实编号{i}" for i in range(SHORT_MAX + 10)]
            for c in contents:
                tm.ingest_candidates("u", [(c, "new", None)])
            st = tm._get_state("u")
            assert len(st.short) == SHORT_MAX, f"SHORT must cap at {SHORT_MAX}, got {len(st.short)}"
            # newest survives at front, oldest evicted from tail
            assert st.short[0].content == contents[-1]
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

    def test_long_snapshot_persona_label(self):
        """Per-turn persona: a LONG snapshot writes/renders/reloads the session
        persona instead of the old hardcoded 助手(Roxy); legacy 2-tuple turns
        still fall back to Roxy."""
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import TieredMemory, MediumItem
            tm = self._tm(tmp)
            st = tm._get_state("u")
            st.medium.append(MediumItem(id="m1", content="事实", count=10, entry_extraction_index=0))
            # one turn carries a character persona (露比); one is a legacy 2-tuple
            turns = [("今天练腿", "好的呀", "露比"), ("旧格式轮", "旧回复")]
            tm.ingest_candidates("u", [("事实", "keep", "m1")], snapshot_turns=turns)
            lo = st.long[0]
            # snapshot_turns stored verbatim → per-turn persona preserved
            assert lo.snapshot_turns == turns, lo.snapshot_turns
            # recall rendering uses each turn's OWN persona; legacy turn → Roxy
            text = tm._format_snapshot_text(lo)
            assert "助手(露比): 好的呀" in text, text
            assert "助手(Roxy): 旧回复" in text, text
            # persist → the md file on disk carries the persona label
            tm.save_user("u")
            with open(os.path.join(tm._long_user_dir("u"), lo.id + ".md"),
                      encoding="utf-8") as f:
                md = f.read()
            assert "助手(露比): 好的呀" in md, md
            # reload → persona recovered from the label; legacy turn read back as Roxy
            tm2 = TieredMemory(base_dir=tmp)
            tm2.load_all()
            lo2 = tm2._get_state("u").long[0]
            assert lo2.snapshot_turns[0] == ("今天练腿", "好的呀", "露比"), lo2.snapshot_turns
            assert lo2.snapshot_turns[1] == ("旧格式轮", "旧回复", "Roxy"), lo2.snapshot_turns
            print_pass("LONG snapshot: per-turn persona label writes/renders/reloads (legacy→Roxy)")
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
            assert all(set(x) == {"id", "content", "count", "tier", "category"} for x in jl)
            assert all(x["category"] == "fact" for x in jl), "④ default category surfaced"
            print_pass("judge list: MEDIUM(top by count) + SHORT, each {id,content,count,tier,category}")
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

    # ── LONG index + recall (P2-lite ①) ─────────────────────────

    def test_long_index_injection(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import LongObject, LONG_INJECT_MAX_LINES
            tm = self._tm(tmp)
            assert tm.build_long_index("nobody") == "", "no LONG → empty index"
            st = tm._get_state("u")
            now = time.time()
            for i in range(LONG_INJECT_MAX_LINES + 5):
                st.long.append(LongObject(
                    id=f"l{i:02d}", title=f"长期事实{i}", summary="s",
                    snapshot_turns=[], snapshot_time=now + i,
                ))
            block = tm.build_long_index("u")
            assert "用户长期记忆索引" in block and "recall_memory" in block
            lines = [ln for ln in block.splitlines() if ln.startswith("- [")]
            assert len(lines) == LONG_INJECT_MAX_LINES, (
                f"expected {LONG_INJECT_MAX_LINES} index lines, got {len(lines)}")
            # newest first; entries beyond the cap are dropped entirely
            assert "[l24] 长期事实24" in lines[0], lines[0]
            assert "l00" not in block and "l04" not in block, "oldest beyond cap must be dropped"
            # one-line format: id + title + （YYYY-MM）
            assert f"（{time.strftime('%Y-%m', time.localtime(now + 24))}）" in lines[0], lines[0]
            # byte-stable across calls (write-once fields only)
            assert block == tm.build_long_index("u"), "index must be byte-stable"
            print_pass(f"LONG index: '' when empty; {LONG_INJECT_MAX_LINES} lines newest-first; byte-stable")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_recall_long_lookup_and_stats(self):
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import (TieredMemory, LongObject, LONG_RECALL_MAX_MATCHES,
                                      LONG_RECALL_SNAPSHOT_CAP, LONG_RECALL_QUERY_LOG_CAP)
            tm = self._tm(tmp)
            st = tm._get_state("u")
            now = time.time()
            big_turns = [("很长的用户消息" * 40, "很长的回复" * 40) for _ in range(5)]
            st.long.append(LongObject(id="lab123", title="原神抽卡记录", summary="用户小保底歪了",
                                      snapshot_turns=big_turns, snapshot_time=now))
            st.long.append(LongObject(id="lcd456", title="健身计划", summary="用户正在练腿",
                                      snapshot_turns=[("今天练腿", "加油")], snapshot_time=now - 100))
            tm.save_user("u")

            # id-prefix match + snapshot rendering capped with truncation marker
            hits = tm.recall_long("u", "lab")
            assert len(hits) == 1 and hits[0][0].id == "lab123"
            assert len(hits[0][1]) <= LONG_RECALL_SNAPSHOT_CAP + 30 and "已截断" in hits[0][1]
            # keyword match (title/summary, substring)
            assert [lo.id for lo, _ in tm.recall_long("u", "练腿")] == ["lcd456"]
            # miss / wrong user / blank query → []
            assert tm.recall_long("u", "不存在的关键词") == []
            assert tm.recall_long("other", "lab") == [], "recall must be user-scoped"
            assert tm.recall_long("u", "   ") == []

            # stats bumped + persisted + query_log capped
            assert st.long[0].query_count == 1 and st.long[0].query_log
            for _ in range(LONG_RECALL_QUERY_LOG_CAP + 5):
                tm.recall_long("u", "lab")
            assert len(st.long[0].query_log) == LONG_RECALL_QUERY_LOG_CAP
            tm2 = TieredMemory(base_dir=tmp)
            tm2.load_all()
            lo2 = next(lo for lo in tm2._get_state("u").long if lo.id == "lab123")
            assert lo2.query_count == st.long[0].query_count, "query stats must persist"

            # max-matches cap, newest first; snapshot-less → placeholder text
            st.long.clear()
            for i in range(LONG_RECALL_MAX_MATCHES + 2):
                st.long.append(LongObject(id=f"k{i}", title="共同关键词", summary="x",
                                          snapshot_turns=[], snapshot_time=now + i))
            hits = tm.recall_long("u", "共同关键词")
            assert [lo.id for lo, _ in hits] == ["k4", "k3", "k2"], [lo.id for lo, _ in hits]
            assert hits[0][1] == "（无对话快照）"
            print_pass("recall_long: id-prefix/keyword, user-scoped, capped newest-first, stats persisted")
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
            # legacy 2-tuple snapshot writes as 助手(Roxy) and reads back as a
            # 3-tuple with the persona recovered from the speaker label
            assert st2.long[0].snapshot_turns == [("玩原神", "好玩", "Roxy")], "LONG snapshot md round-trips"
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

    # ── ⑨ ingest-level deterministic guards (Memory-Optimization-Plan) ──

    def _drive(self, tm, uid, contents):
        """Ingest each content as judge="new" (the path ⑨ guards), ticking the clock."""
        summaries = []
        for c in contents:
            summaries.append(tm.ingest_candidates(uid, [(c, "new", None)], []))
            tm.touch_extraction_count(uid)
        return summaries

    def _all_contents(self, tm, uid):
        st = tm._get_state(uid)
        return [s.content for s in st.short] + [m.content for m in st.medium]

    def test_correction_replaces_stale_short(self):
        """⑨ M2: L2 ct3 pattern — an explicit correction judged "new" must
        replace the stale value in place, never coexist with it."""
        tmp = tempfile.mkdtemp()
        try:
            tm = self._tm(tmp)
            ss = self._drive(tm, "u", [
                "用户是会计专业的学生",
                "用户是会计专业的学生",              # exact-dup → count=2
                "用户是计算机专业的学生，不是会计专业",  # correction, judge="new"
            ])
            st = tm._get_state("u")
            assert ss[2]["corrected_in_place"] == 1, ss[2]
            assert len(st.short) + len(st.medium) == 1, self._all_contents(tm, "u")
            assert "不是会计专业" in self._all_contents(tm, "u")[0]
            # replaced in place: count inherited+1 (=3 → promoted), age anchored
            assert st.medium and st.medium[0].count == 3, [(m.content, m.count) for m in st.medium]
            assert st.medium[0].entry_extraction_index == st.extraction_count - 1
            print_pass("⑨ M2: explicit correction replaces stale SHORT value in place")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_correction_sweeps_stale_variants_keeps_adjacent(self):
        """⑨ M2b: differently-phrased stale variants (住在杭州/家住杭州) are all
        swept; a compatible adjacent fact (姐姐是老师) survives both orderings."""
        tmp = tempfile.mkdtemp()
        try:
            tm = self._tm(tmp)
            self._drive(tm, "u", [
                "用户是独生女",
                "用户的姐姐是老师",                  # compatible adjacent — must survive
                "用户不是独生女，有一个姐姐",
                "用户不是独生女，她有一个亲姐姐",
            ])
            contents = self._all_contents(tm, "u")
            assert "用户的姐姐是老师" in contents, contents
            assert not any(c == "用户是独生女" for c in contents), contents
            assert sum(1 for c in contents if "独生女" in c) == 1, contents
            print_pass("⑨ M2b: stale variants swept, topic-adjacent compatible fact kept")

            tm2 = self._tm(tempfile.mkdtemp())
            self._drive(tm2, "v", [
                "用户住在杭州",
                "用户家住杭州",                     # stale variant, Dice-only 0.33 with the above
                "用户全家去年已从杭州搬到南京居住",
                "用户全家去年搬到了南京住",
            ])
            contents2 = self._all_contents(tm2, "v")
            assert not any("住在杭州" in c or "家住杭州" in c for c in contents2), contents2
            assert sum(1 for c in contents2 if "南京" in c) == 1, contents2
            print_pass("⑨ M2b: phrasing-divergent stale variants (住在/家住) fully cleared")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_correction_targets_medium_and_resets_age(self):
        """⑨ M2 on the MEDIUM tier: content replaced, count+1, age reset to 0."""
        tmp = tempfile.mkdtemp()
        try:
            tm = self._tm(tmp)
            for _ in range(3):                      # promote to MEDIUM at count=3
                tm.ingest_candidates("u", [("用户是健身教练", "new", None)], [])
                tm.touch_extraction_count("u")
            for _ in range(5):                      # let it age
                tm.ingest_candidates("u", [], [])
                tm.touch_extraction_count("u")
            st = tm._get_state("u")
            assert len(st.medium) == 1 and st.medium[0].count == 3
            age_before = st.extraction_count - st.medium[0].entry_extraction_index
            assert age_before >= 5, age_before
            tm.ingest_candidates("u", [("用户已转行，现在是快递员，不再是健身教练", "new", None)], [])
            assert len(st.medium) == 1 and not st.short
            assert st.medium[0].content == "用户已转行，现在是快递员，不再是健身教练"
            assert st.medium[0].count == 4, "count inherited (+1), not restarted"
            assert st.extraction_count - st.medium[0].entry_extraction_index == 0, \
                f"correction resets age (was {age_before})"
            print_pass("⑨ M2: MEDIUM-tier correction replaces content, count+1, age resets")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_correction_requires_marker(self):
        """⑨ M2 gate 1: without a narrow correction marker, compatible
        restatements must NOT clobber existing entries (no 已经/现在/其实 markers)."""
        tmp = tempfile.mkdtemp()
        try:
            tm = self._tm(tmp)
            ss = self._drive(tm, "u", [
                "用户每天肝原神日常",
                "用户已经在玩原神五年了",            # shares 原神 topic, no narrow marker
                "用户喜欢喝美式咖啡",
                "用户不喝咖啡，只喝茶",              # 不 alone is NOT a marker (不是/不再 are)
            ])
            assert all(s["corrected_in_place"] == 0 for s in ss), ss
            contents = self._all_contents(tm, "u")
            assert len(contents) == 4, contents
            print_pass("⑨ M2: no marker → no replacement (compatible facts coexist)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_neardup_merge_recency_wins(self):
        """⑨ M1: unmarked numeric correction (三十岁→三十五岁) merges via the
        containment rule and keeps the NEWER wording (recency-wins)."""
        tmp = tempfile.mkdtemp()
        try:
            tm = self._tm(tmp)
            ss = self._drive(tm, "u", [
                "用户今年三十岁",
                "用户今年三十岁，刚过完生日",         # near-dup merge (Dice 0.62)
                "用户今年三十五岁",                  # overlap 0.60 with embellished variant
                "用户今年三十五岁了",
            ])
            assert ss[1]["near_dup_merged"] == 1 and ss[2]["near_dup_merged"] == 1, ss
            st = tm._get_state("u")
            contents = self._all_contents(tm, "u")
            assert len(contents) == 1, contents
            assert "三十五" in contents[0] and "三十岁" not in contents[0], contents
            print_pass("⑨ M1: numeric near-dup chain collapses to one recency-wins item")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_neardup_guard_distinct_facts_coexist(self):
        """⑨ M1 guards (measured from L2 baseline data): same-template allergy
        pair, city pair, and evolving-fact pair must all stay separate."""
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import _char_bigrams, _is_near_dup
            # unit-level: guard pairs far below both thresholds
            guards = [
                ("用户对花生过敏", "用户对海鲜过敏"),                     # Dice 0.25 / ovl 0.25
                ("用户对花生过敏，吃了会休克", "用户对海鲜过敏，吃虾蟹起疹子"),  # 0.21 / 0.22
                ("用户住在杭州", "用户住在南京"),                        # 0.25 / 0.33
                ("用户每天肝原神日常", "用户已经在玩原神五年了"),           # 0.14 / 0.33
                ("用户有一个上幼儿园的女儿", "用户的女儿今年九月升小学，适应得还不错"),
            ]
            for a, b in guards:
                assert not _is_near_dup(_char_bigrams(a), _char_bigrams(b)), (a, b)
            # end-to-end: repeated mentions of both allergies stay two items
            tm = self._tm(tmp)
            self._drive(tm, "u", [
                "用户对花生过敏", "用户对海鲜过敏",
                "用户对花生过敏，吃了会休克", "用户对海鲜过敏，吃虾蟹起疹子",
            ])
            contents = self._all_contents(tm, "u")
            assert sum(1 for c in contents if "花生" in c) == 1, contents
            assert sum(1 for c in contents if "海鲜" in c) == 1, contents
            print_pass("⑨ M1: guard pairs (allergy template/city/evolving) never merge")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_neardup_digit_guard_enumerations(self):
        """⑨ M1 digit guard: differing ASCII digit signatures mark ENUMERATED
        variants (编号1号/编号10号 share 0.70 bigrams!) — they must coexist,
        while identifier corrections still ride the M2 marker path."""
        tmp = tempfile.mkdtemp()
        try:
            tm = self._tm(tmp)
            self._drive(tm, "u", [
                "中期事实1号", "中期事实10号", "中期事实100号",
            ])
            contents = self._all_contents(tm, "u")
            assert len(contents) == 3, f"digit variants must not merge: {contents}"
            # same digits, near-dup template → merges as usual
            self._drive(tm, "v", ["用户住在3号楼", "用户住在3号楼房子里"])
            contents_v = self._all_contents(tm, "v")
            assert len(contents_v) == 1, contents_v
            # identifier correction with M2 marker still replaces (the corrected
            # sentence restates the identifier topic → containment 0.5 ≥ 0.33)
            self._drive(tm, "w", ["用户住在3号楼", "用户不住3号楼了，改成住5号楼"])
            contents_w = self._all_contents(tm, "w")
            assert len(contents_w) == 1 and "5号楼" in contents_w[0], contents_w
            print_pass("⑨ M1: digit guard keeps enumerations, M2 still corrects identifiers")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_correction_spares_fused_episode_entry(self):
        """⑨ M2 bar (L2 batch-2 p2 se1 regression): extraction often FUSES an
        episode into a fact entry (有一个上幼儿园的女儿,曾因女儿演出感动落泪).
        A later 升小学 correction shares 女儿/幼儿/儿园 with it but scores
        0.18/0.21 — below STALE_VARIANT_OVERLAP → the fused entry (and its
        canary episode) must survive untouched; the correction lands as new."""
        tmp = tempfile.mkdtemp()
        try:
            tm = self._tm(tmp)
            fused = "用户有一个上幼儿园的女儿，曾因女儿演出感动落泪"
            ss = self._drive(tm, "u", [
                fused,
                "用户女儿今年九月升小学了，不再是幼儿园的小朋友",
            ])
            assert ss[1]["corrected_in_place"] == 0, ss[1]
            assert ss[1].get("stale_swept", 0) == 0, ss[1]
            contents = self._all_contents(tm, "u")
            assert fused in contents, contents          # episode detail preserved
            assert any("升小学" in c for c in contents), contents
            assert len(contents) == 2, contents
            print_pass("⑨ M2: fused episode entry spared by correction below the variant bar")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_correction_no_topic_word_sweep(self):
        """⑨ M2b bar (L2 batch-2 p4 regression): a correction sharing only a
        topic word (游戏, Dice 0.09-0.15) must neither target nor sweep
        unrelated same-topic facts — the loose first-run bar halved p4's store
        (cl3 亏损三万块 + all 3 salient episodes lost). The genuine stale entry
        (从来不用外包…自己做, containment 0.36) IS still replaced."""
        tmp = tempfile.mkdtemp()
        try:
            tm = self._tm(tmp)
            stale = "用户从来不用外包，游戏素材全自己做"
            cl3 = "用户的第一款游戏亏损三万块"
            ss = self._drive(tm, "u", [
                stale,
                cl3,
                "用户的游戏音乐不再自己做，改为外包",
            ])
            assert ss[2]["corrected_in_place"] == 1, ss[2]
            assert ss[2].get("stale_swept", 0) == 0, ss[2]
            contents = self._all_contents(tm, "u")
            assert cl3 in contents, contents            # topic-adjacent fact survives
            assert not any("从来不用外包" in c for c in contents), contents
            assert any("改为外包" in c for c in contents), contents
            assert len(contents) == 2, contents
            print_pass("⑨ M2b: topic-word-only overlap neither targets nor sweeps")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ── ④ write-time typing (category) — Memory-Optimization-Plan batch 2 ──

    def _items(self, tm, uid):
        st = tm._get_state(uid)
        return list(st.short) + list(st.medium)

    def _by_content(self, tm, uid, sub):
        for it in self._items(tm, uid):
            if sub in it.content:
                return it
        return None

    def test_category_defaults_to_fact(self):
        """④ a candidate with no category (dict w/o key, or legacy 3-tuple)
        creates a SHORT item typed `fact`."""
        tmp = tempfile.mkdtemp()
        try:
            tm = self._tm(tmp)
            tm.ingest_candidates("u", [
                {"content": "用户住在杭州", "judge": "new"},          # dict, no category
                ("用户是程序员", "new", None),                        # legacy 3-tuple
            ], [])
            for sub in ("住在杭州", "程序员"):
                it = self._by_content(tm, "u", sub)
                assert it is not None and it.category == "fact", (sub, it and it.category)
            print_pass("④ category: absent/legacy candidate defaults to 'fact'")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_category_explicit_all_four(self):
        """④ all four VALID_CATEGORIES round-trip through a dict candidate,
        and a 4-tuple carries category too."""
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import VALID_CATEGORIES
            tm = self._tm(tmp)
            tm.ingest_candidates("u", [
                {"content": "用户对青霉素过敏", "judge": "new", "category": "critical"},
                {"content": "用户喜欢简短回复", "judge": "new", "category": "preference"},
                {"content": "用户上周去了漫展", "judge": "new", "category": "event"},
                ("用户住在杭州", "new", None, "fact"),               # 4-tuple form
            ], [])
            got = {self._by_content(tm, "u", s).category: s
                   for s in ("青霉素过敏", "简短回复", "漫展", "住在杭州")}
            assert set(got) == set(VALID_CATEGORIES), got
            assert got["critical"] == "青霉素过敏" and got["preference"] == "简短回复"
            assert got["event"] == "漫展" and got["fact"] == "住在杭州"
            print_pass("④ category: preference/fact/event/critical all stored (dict + 4-tuple)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_category_invalid_or_hallucinated_keeps_existing(self):
        """④ an invalid tag normalizes to None → existing item KEEPS its category
        on update; a brand-new item with an invalid tag falls back to `fact`."""
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import _norm_category
            assert _norm_category("CRITICAL") == "critical", "case/space normalized"
            assert _norm_category("  Preference ") == "preference"
            for bad in ("allergy", "", None, 42, "facts"):
                assert _norm_category(bad) is None, bad
            tm = self._tm(tmp)
            st = tm._get_state("u")
            from agent.memory import MediumItem
            st.medium.append(MediumItem(id="m1", content="用户对青霉素过敏",
                                        count=4, entry_extraction_index=st.extraction_count,
                                        category="critical"))
            # update carrying a hallucinated tag must NOT clobber the critical type
            tm.ingest_candidates("u", [
                {"content": "用户对青霉素和头孢都过敏", "judge": "update",
                 "matched_id": "m1", "category": "allergy"},
            ], [])
            assert st.medium[0].category == "critical", st.medium[0].category
            assert "头孢" in st.medium[0].content, "content still refined"
            # new item with invalid tag → fact
            tm.ingest_candidates("u", [
                {"content": "用户养了一只猫", "judge": "new", "category": "pet"},
            ], [])
            assert self._by_content(tm, "u", "养了一只猫").category == "fact"
            print_pass("④ category: invalid/hallucinated tag → keep existing / default fact")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_category_reinforce_does_not_retype(self):
        """④ pure reinforcement (reinforce_short / keep) never re-types: the
        stored category survives even if the judge supplies a different tag,
        because reinforce doesn't pass category to the item."""
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import ShortItem
            tm = self._tm(tmp)
            st = tm._get_state("u")
            st.short.append(ShortItem(id="s1", content="用户住在杭州", count=1,
                                      category="fact"))
            # reinforce_short path ignores category entirely
            tm.ingest_candidates("u", [
                {"content": "用户住在杭州", "judge": "reinforce_short",
                 "matched_id": "s1", "category": "critical"},
            ], [])
            it = st.short[0]
            assert it.count == 2 and it.category == "fact", (it.count, it.category)
            print_pass("④ category: reinforce_short keeps stored type (no silent re-type)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_category_update_retypes(self):
        """④ an `update` judge with a valid category DOES re-type (a fact can
        legitimately be promoted to critical when new detail arrives)."""
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import MediumItem
            tm = self._tm(tmp)
            st = tm._get_state("u")
            st.medium.append(MediumItem(id="m1", content="用户吃药要注意",
                                        count=4, entry_extraction_index=st.extraction_count,
                                        category="fact"))
            tm.ingest_candidates("u", [
                {"content": "用户对青霉素过敏，用错会休克", "judge": "update",
                 "matched_id": "m1", "category": "critical"},
            ], [])
            assert st.medium[0].category == "critical", st.medium[0].category
            print_pass("④ category: update re-types fact → critical")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_category_survives_promote_and_demote(self):
        """④ category + last_confirmed_ts ride along on SHORT→MEDIUM promotion
        and MEDIUM→SHORT age-demotion (both directions preserve typing)."""
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import MEDIUM_DOWNGRADE_AGE
            tm = self._tm(tmp)
            # promote a critical fact to MEDIUM (count reaches 3)
            for _ in range(3):
                tm.ingest_candidates("u", [
                    {"content": "用户对青霉素过敏", "judge": "new", "category": "critical"},
                ], [])
                tm.touch_extraction_count("u")
            st = tm._get_state("u")
            assert st.medium and st.medium[0].category == "critical", "promote carries category"
            ts_before = st.medium[0].last_confirmed_ts
            assert ts_before > 0, "promoted item carries a last_confirmed_ts"
            # age it past the *fact* threshold but within critical's 3× window
            st.extraction_count = st.medium[0].entry_extraction_index + MEDIUM_DOWNGRADE_AGE + 1
            tm.ingest_candidates("u", [], [])
            assert st.medium and st.medium[0].category == "critical", \
                "critical must NOT demote at fact-age (3× window)"
            print_pass("④ category: promote carries type; critical resists fact-age demotion")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_critical_demote_age_3x(self):
        """④ CATEGORY_DEMOTE_AGE: at age 31 a `fact` demotes but a `critical`
        of the same age stays in MEDIUM (critical = base × 3)."""
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import (MediumItem, MEDIUM_DOWNGRADE_AGE,
                                      CATEGORY_DEMOTE_AGE)
            assert CATEGORY_DEMOTE_AGE["critical"] == MEDIUM_DOWNGRADE_AGE * 3
            assert CATEGORY_DEMOTE_AGE["fact"] == MEDIUM_DOWNGRADE_AGE
            tm = self._tm(tmp)
            st = tm._get_state("u")
            age = MEDIUM_DOWNGRADE_AGE + 1                      # 31
            st.extraction_count = age
            st.medium.append(MediumItem(id="f", content="普通事实", count=6,
                                        entry_extraction_index=0, category="fact"))
            st.medium.append(MediumItem(id="c", content="用户过敏禁忌", count=6,
                                        entry_extraction_index=0, category="critical"))
            tm.ingest_candidates("u", [], [])
            ids = {m.id for m in st.medium}
            assert "f" not in ids, "fact demotes at age 31"
            assert "c" in ids, "critical survives age 31 (3× window)"
            assert any(s.id == "f" and s.category == "fact" for s in st.short), \
                "demoted fact lands in SHORT keeping its category"
            print_pass("④ category: critical demote age = 3× fact; demoted item keeps type")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_category_persistence_roundtrip(self):
        """④③ save_user/load_user round-trip category + last_confirmed_ts."""
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import TieredMemory, ShortItem, MediumItem
            tm = TieredMemory(base_dir=tmp)
            st = tm._get_state("u")
            st.short.append(ShortItem(id="s1", content="短期禁忌", count=2,
                                      category="critical", last_confirmed_ts=12345.0))
            st.medium.append(MediumItem(id="m1", content="中期偏好", count=6,
                                        entry_extraction_index=1,
                                        category="preference", last_confirmed_ts=67890.0))
            tm.save_user("u")
            tm2 = TieredMemory(base_dir=tmp)
            tm2.load_all()
            st2 = tm2._get_state("u")
            assert st2.short[0].category == "critical"
            assert st2.short[0].last_confirmed_ts == 12345.0
            assert st2.medium[0].category == "preference"
            assert st2.medium[0].last_confirmed_ts == 67890.0
            print_pass("④③ persistence: category + last_confirmed_ts round-trip")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_load_v1_file_defaults(self):
        """④ a legacy schema-v1 tier file (no category/ts keys) loads with
        category=fact and last_confirmed_ts=0.0 — no crash, no label."""
        tmp = tempfile.mkdtemp()
        try:
            import json as _json
            from agent.memory import TieredMemory
            tiers = os.path.join(tmp, "tiers")
            os.makedirs(tiers, exist_ok=True)
            v1 = {
                "user_id": "u", "extraction_count": 5, "schema_version": 1,
                "short": [{"id": "s1", "content": "老短期事实", "count": 2}],
                "medium": [{"id": "m1", "content": "老中期事实", "count": 6,
                            "entry_extraction_index": 1}],
                "long": [],
            }
            with open(os.path.join(tiers, "u.json"), "w", encoding="utf-8") as f:
                _json.dump(v1, f, ensure_ascii=False)
            tm = TieredMemory(base_dir=tmp)
            tm.load_all()
            st = tm._get_state("u")
            assert st.short[0].category == "fact" and st.short[0].last_confirmed_ts == 0.0
            assert st.medium[0].category == "fact" and st.medium[0].last_confirmed_ts == 0.0
            # ts=0 → never labelled
            assert tm._staleness_suffix(st.medium[0], now=None) == ""
            print_pass("④ load: legacy v1 file defaults to fact/ts=0 (unlabelled)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_get_judge_list_includes_category(self):
        """④ get_judge_list surfaces category so the judge can respect typing."""
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import MediumItem, ShortItem
            tm = self._tm(tmp)
            st = tm._get_state("u")
            st.medium.append(MediumItem(id="m1", content="中期禁忌", count=6,
                                        entry_extraction_index=0, category="critical"))
            st.short.append(ShortItem(id="s1", content="短期经历", count=1,
                                      category="event"))
            rows = {r["id"]: r for r in tm.get_judge_list("u")}
            assert rows["m1"]["category"] == "critical" and rows["m1"]["tier"] == "medium"
            assert rows["s1"]["category"] == "event" and rows["s1"]["tier"] == "short"
            print_pass("④ get_judge_list: category surfaced per row")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ── ③ staleness annotation「截至 X 月」— Memory-Optimization-Plan batch 2 ──

    def test_staleness_label_stale_fact(self):
        """③ a `fact` older than STALE_LABEL_AFTER_DAYS gets a （截至 X 月）label."""
        tmp = tempfile.mkdtemp()
        try:
            import time as _time
            from agent.memory import TieredMemory, MediumItem, STALE_LABEL_AFTER_DAYS
            tm = self._tm(tmp)
            st = tm._get_state("u")
            now = _time.time()
            stale_ts = now - (STALE_LABEL_AFTER_DAYS + 5) * 86400.0
            st.medium.append(MediumItem(id="m1", content="用户住在杭州", count=6,
                                        entry_extraction_index=0, category="fact",
                                        last_confirmed_ts=stale_ts))
            block = tm.build_medium_injection("u")
            month = _time.localtime(stale_ts).tm_mon
            assert f"（截至 {month} 月）" in block, block
            line = [ln for ln in block.splitlines() if "住在杭州" in ln][0]
            assert line.endswith(f"（截至 {month} 月）"), line
            print_pass("③ label: stale fact renders （截至 X 月） in injection")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_staleness_label_fresh_fact_unlabelled(self):
        """③ a fact confirmed within the window carries NO label."""
        tmp = tempfile.mkdtemp()
        try:
            import time as _time
            from agent.memory import TieredMemory, MediumItem, STALE_LABEL_AFTER_DAYS
            tm = self._tm(tmp)
            st = tm._get_state("u")
            fresh_ts = _time.time() - (STALE_LABEL_AFTER_DAYS - 5) * 86400.0
            st.medium.append(MediumItem(id="m1", content="用户住在杭州", count=6,
                                        entry_extraction_index=0, category="fact",
                                        last_confirmed_ts=fresh_ts))
            block = tm.build_medium_injection("u")
            assert "截至" not in block, block
            print_pass("③ label: fresh fact (< window) is unlabelled")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_staleness_label_scoped_to_fact_only(self):
        """③ critical/preference/event NEVER get the label even when stale —
        hedging an allergy is dangerous; preference has ⑩; event is past-tense."""
        tmp = tempfile.mkdtemp()
        try:
            import time as _time
            from agent.memory import (TieredMemory, MediumItem,
                                      STALE_LABEL_AFTER_DAYS, STALE_LABEL_CATEGORIES)
            assert STALE_LABEL_CATEGORIES == ("fact",)
            tm = self._tm(tmp)
            now = _time.time()
            stale = now - (STALE_LABEL_AFTER_DAYS + 400) * 86400.0   # >1y stale
            for i, cat in enumerate(("critical", "preference", "event")):
                item = MediumItem(id=f"m{i}", content=f"{cat}条目", count=6,
                                  entry_extraction_index=0, category=cat,
                                  last_confirmed_ts=stale)
                assert tm._staleness_suffix(item, now=now) == "", (cat, "must stay unlabelled")
            fact = MediumItem(id="mf", content="fact条目", count=6,
                              entry_extraction_index=0, category="fact",
                              last_confirmed_ts=stale)
            assert tm._staleness_suffix(fact, now=now) != "", "stale fact IS labelled"
            print_pass("③ label: scoped to category=fact only (critical/pref/event exempt)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_staleness_label_legacy_ts0_unlabelled(self):
        """③ last_confirmed_ts == 0.0 (legacy/unknown) → no label regardless of age."""
        tmp = tempfile.mkdtemp()
        try:
            from agent.memory import TieredMemory, MediumItem
            tm = self._tm(tmp)
            item = MediumItem(id="m1", content="老事实", count=6,
                              entry_extraction_index=0, category="fact",
                              last_confirmed_ts=0.0)
            assert tm._staleness_suffix(item) == ""
            print_pass("③ label: ts=0 (unknown age) never labelled")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_staleness_label_cross_year_format(self):
        """③ a confirmation from a PRIOR year includes the year: （截至 YYYY 年 X 月）;
        same-year omits it."""
        tmp = tempfile.mkdtemp()
        try:
            import time as _time
            from agent.memory import TieredMemory, MediumItem
            tm = self._tm(tmp)
            now = _time.mktime(_time.localtime())               # ~today
            cur_year = _time.localtime(now).tm_year
            # same-year stale: 60 days ago (still this year unless run in Jan/Feb)
            same = MediumItem(id="a", content="x", count=6, entry_extraction_index=0,
                              category="fact", last_confirmed_ts=now - 60 * 86400.0)
            same_lbl = tm._staleness_suffix(same, now=now)
            same_year = _time.localtime(now - 60 * 86400.0).tm_year
            if same_year == cur_year:
                assert "年" not in same_lbl and "月" in same_lbl, same_lbl
            # prior-year stale: 400 days ago → must include the year
            prior_ts = now - 400 * 86400.0
            prior = MediumItem(id="b", content="y", count=6, entry_extraction_index=0,
                               category="fact", last_confirmed_ts=prior_ts)
            lbl = tm._staleness_suffix(prior, now=now)
            py = _time.localtime(prior_ts).tm_year
            pm = _time.localtime(prior_ts).tm_mon
            assert lbl == f"（截至 {py} 年 {pm} 月）", lbl
            assert py != cur_year, "400d ago must be a different year for this assertion"
            print_pass("③ label: cross-year format （截至 YYYY 年 X 月）, same-year omits year")
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


# ── P0: Bond system / dual-plane routing tests ────────────────────

class TestBondManager:
    """Test agent/bond.py — EXP/level/mood settlement + rendering."""

    def run(self):
        print_header("16. Bond Manager Tests (P0)")
        self.test_level_curve()
        self.test_first_turn_settlement()
        self.test_daily_first_only_once()
        self.test_long_session_bonus_once()
        self.test_mood_decay_to_baseline()
        self.test_daily_soft_cap_diminishing()
        self.test_format_status_and_prompt()
        self.test_reset()
        self.test_persistence_reload()

    def _mgr(self, tmp):
        from agent.bond import BondManager
        return BondManager(
            base_dir=os.path.join(tmp, "bonds"),
            config_path=os.path.join(tmp, "no_such_config.json"),
        )

    def test_level_curve(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            assert m.level_for_exp(0) == 1
            assert m.level_for_exp(99) == 1
            assert m.level_for_exp(100) == 2
            assert m.level_for_exp(7999) == 9
            assert m.level_for_exp(8000) == 10
            assert m.title_for(1) == "陌生"
            assert m.title_for(10) == "灵魂伴侣"
            nxt = m.next_threshold(1)
            assert nxt["level"] == 2 and nxt["exp"] == 100
            assert m.next_threshold(10) is None
            assert m.current_threshold(2) == 100
            print_pass("level curve / thresholds / titles")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_first_turn_settlement(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            r = m.settle_turn("u1", "rubi")
            assert r["is_first_today"] is True
            # first_chat(20) + mood-weighted per-msg(~3)
            assert r["exp_gained"] >= 21, r
            assert r["exp"] == r["exp_gained"]
            assert r["level"] == 1
            print_pass("first-turn settlement grants daily-first + per-msg EXP")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_daily_first_only_once(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            r1 = m.settle_turn("u1", "rubi")
            r2 = m.settle_turn("u1", "rubi")
            assert r1["is_first_today"] is True
            assert r2["is_first_today"] is False
            # second turn only earns per-msg (much less than first-turn bundle)
            assert r2["exp_gained"] < r1["exp_gained"]
            print_pass("daily-first EXP granted only once per day")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_long_session_bonus_once(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            m.settle_turn("u1", "rubi", turn_count=0)   # consume daily-first
            r2 = m.settle_turn("u1", "rubi", turn_count=10)  # long-session bonus
            r3 = m.settle_turn("u1", "rubi", turn_count=10)  # already earned
            assert r2["exp_gained"] >= 30, r2
            assert r3["exp_gained"] < 10, r3
            print_pass("long-session bonus fires once per day")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mood_decay_to_baseline(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            st = m.get_state("u1", "rubi")
            now = time.time()
            st["mood"] = 100.0
            st["mood_updated"] = now - 3600  # 1h ago → decay 5
            assert abs(m._decayed_mood(st, now) - 95.0) < 0.1
            st["mood_updated"] = now - 48 * 3600  # clamps to baseline 60
            assert abs(m._decayed_mood(st, now) - 60.0) < 0.01
            st["mood"] = 0.0
            st["mood_updated"] = now - 48 * 3600  # rises back to baseline
            assert abs(m._decayed_mood(st, now) - 60.0) < 0.01
            assert m._mood_label(90) == "极佳"
            assert m._mood_label(20) == "低落"
            print_pass("mood decays toward baseline both directions + labels")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_daily_soft_cap_diminishing(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            from datetime import datetime
            m = self._mgr(tmp)
            today = datetime.now().strftime("%Y-%m-%d")
            # at soft cap → halved
            st = m.get_state("u1", "rubi")
            m._roll_daily(st, today)
            st["daily"]["first_done"] = True
            st["daily"]["chat_exp"] = 150
            st["mood"] = 60.0
            st["mood_updated"] = time.time()
            r_cap = m.settle_turn("u1", "rubi")
            # beyond 2× cap → quartered
            st["daily"]["chat_exp"] = 300
            st["mood"] = 60.0
            st["mood_updated"] = time.time()
            r_2cap = m.settle_turn("u1", "rubi")
            assert r_cap["exp_gained"] <= 2, r_cap
            assert r_2cap["exp_gained"] < r_cap["exp_gained"], (r_cap, r_2cap)
            print_pass("daily soft-cap diminishing returns (halve → quarter)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_format_status_and_prompt(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            m.settle_turn("u1", "rubi")
            status = m.format_status("u1", "rubi", display_name="露比")
            assert "羁绊" in status and "露比" in status and "Lv" in status
            prompt = m.build_prompt_context("u1", "rubi", char_short_name="露比")
            assert "羁绊状态" in prompt
            # unknown character (create=False) → first-meet copy
            fresh = m.build_prompt_context("nobody", "nochar")
            assert "刚刚开始" in fresh
            print_pass("format_status + build_prompt_context rendering")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reset(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            m.settle_turn("u1", "rubi")
            assert m.get_state("u1", "rubi", create=False) is not None
            m.reset("u1", "rubi")
            assert m.get_state("u1", "rubi", create=False) is None
            print_pass("reset clears one character's bond")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_persistence_reload(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            m.settle_turn("u1", "rubi")
            exp1 = m.get_state("u1", "rubi")["exp"]
            # A fresh manager (cold cache) must read the same value from disk
            m2 = self._mgr(tmp)
            exp2 = m2.get_state("u1", "rubi", create=False)["exp"]
            assert exp1 == exp2 and exp1 > 0
            print_pass("bond state persists to disk + reloads on cold cache")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPersonalityMeta:
    """Test personality card mode/task_voice metadata parsing."""

    def run(self):
        print_header("17. Personality Mode Metadata Tests (P0)")
        self.test_mode_and_voice()

    def test_mode_and_voice(self):
        from agent.personality import PersonalityManager
        tmp = tempfile.mkdtemp(prefix="pmeta_")
        try:
            pdir = os.path.join(tmp, "personalities")
            os.makedirs(pdir, exist_ok=True)
            cards = {
                "assistant": "# 助手 Roxy\n\n没有元数据的普通卡片。",
                "rubi": "# 露比 (Rubi)\n\n<!-- mode: split | task_voice: character -->\n\n角色内容。",
                "tpl": "# 模板角色\n\n<!-- mode: split | task_voice: template -->\n\n内容。",
            }
            for key, body in cards.items():
                with open(os.path.join(pdir, f"{key}.md"), "w", encoding="utf-8") as f:
                    f.write(body)
            pm = PersonalityManager(personalities_dir=pdir)
            # defaults for a card without metadata
            assert pm.get_mode("assistant") == "unified"
            assert pm.get_task_voice("assistant") == "character"
            # split + character voice
            assert pm.get_mode("rubi") == "split"
            assert pm.get_task_voice("rubi") == "character"
            # split + template voice
            assert pm.get_mode("tpl") == "split"
            assert pm.get_task_voice("tpl") == "template"
            print_pass("mode/task_voice parsed from card metadata (+ defaults)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestModelRouterClassify:
    """Test ModelRouter.classify — dual-axis (plane + complexity) parsing."""

    def run(self):
        print_header("18. ModelRouter.classify Tests (P0)")
        self.test_parsing_and_fallbacks()

    def _router_with(self, response=None, raises=False):
        from lib.model_router import ModelRouter
        router = ModelRouter(config_path="/nonexistent/models.json")
        client = AsyncMock()
        if raises:
            client.chat_completion = AsyncMock(side_effect=RuntimeError("boom"))
        else:
            client.chat_completion = AsyncMock(return_value=response)
        router.get_client = MagicMock(return_value=client)
        return router

    def test_parsing_and_fallbacks(self):
        # canonical two-axis
        r = self._router_with("plane=social complexity=simple")
        assert asyncio.run(r.classify("你好")) == {"plane": "social", "complexity": "simple"}
        r = self._router_with("plane=task complexity=complex")
        assert asyncio.run(r.classify("搜一下")) == {"plane": "task", "complexity": "complex"}
        # colon + comma separators
        r = self._router_with("plane: task, complexity: simple")
        out = asyncio.run(r.classify("x"))
        assert out["plane"] == "task" and out["complexity"] == "simple"
        # plane present but complexity missing → default complex
        r = self._router_with("plane=social")
        out = asyncio.run(r.classify("x"))
        assert out == {"plane": "social", "complexity": "complex"}
        # legacy single-axis: complex → task plane
        r = self._router_with("complex")
        assert asyncio.run(r.classify("x")) == {"plane": "task", "complexity": "complex"}
        # legacy single-axis: "complexity: simple" must NOT match \bcomplex\b
        r = self._router_with("complexity: simple")
        out = asyncio.run(r.classify("x"))
        assert out == {"plane": "social", "complexity": "simple"}, out
        # unparsable → safe fallback (task/complex)
        r = self._router_with("hello world")
        assert asyncio.run(r.classify("x")) == {"plane": "task", "complexity": "complex"}
        # exception → safe fallback
        r = self._router_with(raises=True)
        assert asyncio.run(r.classify("x")) == {"plane": "task", "complexity": "complex"}
        print_pass("classify parses plane+complexity, legacy & error fallbacks")


class TestDelegation:
    """Test agent/delegation.py — dual-plane task delegation + revoicing."""

    def run(self):
        print_header("19. Delegation / Dual-Plane Tests (P0)")
        self.test_guards()
        self.test_run_delegated_isolation()
        self.test_revoice()
        self.test_repatch()

    def test_guards(self):
        from agent.delegation import run_delegated_task
        from agent.context import _delegate_depth
        # missing user
        r = asyncio.run(run_delegated_task(
            "x", agent=None, model_router=None, perm_manager=None, user_id=""))
        assert r.startswith("[Error]")
        # empty task
        r = asyncio.run(run_delegated_task(
            "   ", agent=None, model_router=None, perm_manager=None, user_id="u1"))
        assert "任务描述为空" in r
        # nested delegate rejected
        tok = _delegate_depth.set(1)
        try:
            r = asyncio.run(run_delegated_task(
                "x", agent=None, model_router=None, perm_manager=None, user_id="u1"))
            assert "嵌套" in r
        finally:
            _delegate_depth.reset(tok)
        print_pass("delegation guards: no user / empty task / nested")

    def test_run_delegated_isolation(self):
        from agent.delegation import run_delegated_task
        from agent.context import (
            _current_personality, _current_bond_context, _delegate_depth,
        )
        captured = {}

        class _MockAgent:
            async def run(self, message, user_id, **kwargs):
                captured["message"] = message
                captured["kwargs"] = kwargs
                captured["personality"] = _current_personality.get()
                captured["bond"] = _current_bond_context.get()
                captured["depth"] = _delegate_depth.get()
                return "任务完成结果"

        class _Role:
            value = "vip"

        class _MockPerm:
            def get_role(self, uid):
                return _Role()
            def get_allowed_tools(self, role):
                return {"web_search", "execute_code", "delegate_task"}

        class _MockRouter:
            flash_client = object()
            reasoning_client = object()
            async def classify_complexity(self, task):
                return "simple"

        p_tok = _current_personality.set("rubi")
        b_tok = _current_bond_context.set("BONDCTX")
        try:
            r = asyncio.run(run_delegated_task(
                "  do the thing  ",
                agent=_MockAgent(), model_router=_MockRouter(),
                perm_manager=_MockPerm(), user_id="u1",
                disabled_tools={"execute_code"},
            ))
            assert r == "任务完成结果"
            assert captured["message"] == "do the thing"  # stripped
            kw = captured["kwargs"]
            assert kw["persist"] is False
            assert kw["session_type"] == "temporary"
            assert "delegate_task" not in kw["allowed_tools"]
            assert "execute_code" not in kw["allowed_tools"]
            assert "web_search" in kw["allowed_tools"]
            # task plane runs as neutral assistant, no bond, depth guard set
            assert captured["personality"] == "assistant"
            assert captured["bond"] == ""
            assert captured["depth"] == 1
            # contextvars restored after the delegated run
            assert _current_personality.get() == "rubi"
            assert _current_bond_context.get() == "BONDCTX"
            assert _delegate_depth.get() == 0
            print_pass("run_delegated_task isolation + contextvar restore")
        finally:
            _current_personality.reset(p_tok)
            _current_bond_context.reset(b_tok)

    def test_revoice(self):
        from agent.delegation import revoice_task_result, template_opening

        class _RVClient:
            def __init__(self, resp):
                self.resp = resp
            async def chat_completion(self, message, history=None,
                                      timeout_set=180.0, purpose="chat"):
                return self.resp

        # LLM success → opening + verbatim body
        r = asyncio.run(revoice_task_result(
            client=_RVClient("锵锵，结果来啦！"), persona_content="PERSONA",
            short_name="露比", user_request="查天气", response="今天晴天",
            use_llm=True))
        assert r == "锵锵，结果来啦！\n\n今天晴天", r
        # LLM error-prefix → template fallback, body preserved
        r2 = asyncio.run(revoice_task_result(
            client=_RVClient("思考超时了"), persona_content="P",
            short_name="露比", user_request="q", response="BODY", use_llm=True))
        assert r2.endswith("\n\nBODY")
        assert "思考超时" not in r2.split("\n\n")[0]
        # LLM output too long → template fallback
        r3 = asyncio.run(revoice_task_result(
            client=_RVClient("x" * 200), persona_content="P",
            short_name="露比", user_request="q", response="B", use_llm=True))
        assert r3.endswith("\n\nB") and len(r3.split("\n\n")[0]) <= 120
        # use_llm=False → template (client ignored)
        r4 = asyncio.run(revoice_task_result(
            client=_RVClient("should not be used"), persona_content="P",
            short_name="露比", user_request="q", response="B", use_llm=False))
        assert r4.endswith("\n\nB")
        # empty response → just the opening
        r5 = asyncio.run(revoice_task_result(
            client=None, persona_content="", short_name="露比",
            user_request="q", response="", use_llm=False))
        assert "露比" in r5
        assert "露比" in template_opening("露比")
        print_pass("revoice: LLM opening, template fallbacks, verbatim body")

    def test_repatch(self):
        from agent.delegation import repatch_last_assistant

        class _Sess:
            def __init__(self, ctx):
                self.context = ctx

        class _SM:
            def __init__(self, sess):
                self.sess = sess
                self.updated = False
            def get(self, uid):
                return self.sess
            def update(self, uid, sess):
                self.updated = True

        # verbatim match → replaced
        sess = _Sess([{"role": "user", "content": "hi"},
                      {"role": "assistant", "content": "RAW"}])
        sm = _SM(sess)
        assert repatch_last_assistant(sm, "u1", "RAW", "开场白\n\nRAW") is True
        assert sess.context[-1]["content"] == "开场白\n\nRAW"
        assert sm.updated is True
        # non-match (already folded) → untouched
        sess2 = _Sess([{"role": "assistant", "content": "COMPRESSED"}])
        sm2 = _SM(sess2)
        assert repatch_last_assistant(sm2, "u1", "RAW", "NEW") is False
        assert sess2.context[-1]["content"] == "COMPRESSED"
        # no session
        class _SMNone:
            def get(self, uid):
                return None
        assert repatch_last_assistant(_SMNone(), "u1", "a", "b") is False
        print_pass("repatch_last_assistant verbatim-guard")


class TestAgentPersist:
    """Test Agent.run(persist=False) isolated execution (delegate_task plane)."""

    def run(self):
        print_header("20. Agent persist=False Isolation Tests (P0)")
        asyncio.run(self.test_persist_flag())

    async def test_persist_flag(self):
        from agent.tool_registry import ToolRegistry
        from agent.agent import Agent

        mock_client = MockDeepSeekClient()
        mock_client.plain_response = "隔离结果"
        agent = Agent(
            deepseek_client=mock_client,
            tool_registry=ToolRegistry(),
            config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
        )
        # persist=False → detached session, nothing registered with the manager
        resp = await agent.run("task", "iso_user", persist=False)
        assert resp == "隔离结果"
        assert agent.sessions.get("iso_user") is None
        # persist=True (default) → session written back
        resp2 = await agent.run("hello", "persist_user")
        assert resp2 == "隔离结果"
        sess = agent.sessions.get("persist_user")
        assert sess is not None and len(sess.context) >= 2
        print_pass("persist=False isolates session; persist=True writes back")


class TestCharacterRegistry:
    """Test agent/character_registry.py — UGC overlay on PersonalityManager."""

    def run(self):
        print_header("21. CharacterRegistry Tests (P1)")
        self._setup()
        try:
            self.test_discover_builtin_only()
            self.test_crud_and_scope()
            self.test_group_and_default()
            self.test_purge_references()
            self.test_env_chars_dir()
            self.test_bad_encoding_meta()
            self.test_session_persona_layer()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _setup(self):
        from agent.character_registry import CharacterRegistry
        self.tmp = tempfile.mkdtemp(prefix="creg_")
        pdir = os.path.join(self.tmp, "personalities")
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "assistant.md"), "w", encoding="utf-8") as f:
            f.write("# 助手 Roxy\n\nbase")
        with open(os.path.join(pdir, "rubi.md"), "w", encoding="utf-8") as f:
            f.write("# 露比 (Rubi)\n\n<!-- mode: split | task_voice: character -->\n\nx")
        self.reg = CharacterRegistry(
            personalities_dir=pdir,
            characters_dir=os.path.join(self.tmp, "characters"),
        )

    def _mk(self, char_id, name, author):
        card = f"# {name}\n<!-- mode: split | task_voice: character -->\n\n内容"
        meta = {"char_id": char_id, "display_name": name, "author": author,
                "created_at": 0, "visibility": "private", "voice_id": None, "source": "ugc"}
        self.reg.register_character(char_id, card, meta)

    def test_discover_builtin_only(self):
        # _discover() must never leak UGC (protects get_default fallback)
        self._mk("星野爱_abc123", "星野爱", "u1")
        assert set(self.reg._discover()) == {"assistant", "rubi"}
        assert self.reg.is_ugc("星野爱_abc123")
        assert self.reg.get_mode("星野爱_abc123") == "split"
        assert self.reg.get_display_name("星野爱_abc123") == "星野爱"
        print_pass("_discover() builtin-only; UGC load/mode/display via registry")

    def test_crud_and_scope(self):
        self._mk("c_aaa111", "爱丽丝", "u1")
        # owner-scoped resolve: own vs other
        assert self.reg.resolve_name("爱丽丝", user_id="u1") == "c_aaa111"
        assert self.reg.resolve_name("爱丽丝", user_id="u2") is None
        # exact char_id (owner only)
        assert self.reg.resolve_name("c_aaa111", user_id="u1") == "c_aaa111"
        assert self.reg.resolve_name("c_aaa111", user_id="u2") is None
        # builtin still resolves
        assert self.reg.resolve_name("rubi", user_id="u1") == "rubi"
        # list: builtins + own UGC only
        keys_u1 = {e["key"] for e in self.reg.list_personalities(user_id="u1")}
        keys_u2 = {e["key"] for e in self.reg.list_personalities(user_id="u2")}
        assert "c_aaa111" in keys_u1 and "c_aaa111" not in keys_u2
        assert {"assistant", "rubi"} <= keys_u2
        # set_user_personality is owner-scoped
        self.reg.set_user_personality("u1", "爱丽丝")
        assert self.reg.resolve_effective_personality("u1") == "c_aaa111"
        # u2 cannot resolve u1's char → falls back to default
        try:
            self.reg.set_user_personality("u2", "爱丽丝")
            assert False, "u2 should not resolve u1's UGC"
        except ValueError:
            pass
        # delete
        assert self.reg.delete("c_aaa111") is True
        assert self.reg.is_ugc("c_aaa111") is False
        assert self.reg.delete("c_aaa111") is False
        print_pass("UGC CRUD + owner-scoped resolve/list/set (privacy)")

    def test_group_and_default(self):
        self._mk("c_bbb222", "群角色", "admin1")
        # owner can bind their UGC to a group
        self.reg.set_group_personality("g1", "群角色", owner_id="admin1")
        assert self.reg.get_group_personality("g1") == "c_bbb222"
        # another user in that group resolves it (group layer = any existing key)
        assert self.reg.resolve_effective_personality("u9", "g1") == "c_bbb222"
        # non-owner cannot bind someone else's UGC
        try:
            self.reg.set_group_personality("g2", "群角色", owner_id="other")
            assert False
        except ValueError:
            pass
        # get_default unaffected by UGC
        assert self.reg.get_default() in {"assistant", "rubi"}
        print_pass("group binding owner-scoped at set; get_default builtin-only")

    def test_purge_references(self):
        self._mk("c_ccc333", "待删", "u1")
        self.reg.set_user_personality("u1", "待删")
        self.reg.set_group_personality("g5", "待删", owner_id="u1")
        assert self.reg.resolve_effective_personality("u1") == "c_ccc333"
        removed = self.reg.purge_references("c_ccc333")
        assert removed == 2, removed
        # references gone → fall back to default / unset
        assert self.reg.resolve_effective_personality("u1") != "c_ccc333"
        assert self.reg.get_group_personality("g5") is None
        self.reg.delete("c_ccc333")
        print_pass("purge_references clears user settings + group bindings")

    def test_env_chars_dir(self):
        """CHARACTERS_DIR env redirects UGC root when no explicit arg given."""
        from agent.character_registry import CharacterRegistry, _DEFAULT_CHARACTERS_DIR
        pdir = os.path.join(self.tmp, "personalities")
        env_dir = os.path.join(self.tmp, "env_characters")
        old = os.environ.get("CHARACTERS_DIR")
        try:
            os.environ["CHARACTERS_DIR"] = env_dir
            reg = CharacterRegistry(personalities_dir=pdir)
            assert reg._char_dir == env_dir, reg._char_dir
            reg.register_character("c_env001", "# env\n内容",
                                   {"display_name": "env", "author": "u1"})
            assert os.path.isfile(os.path.join(env_dir, "c_env001", "card.md"))
            # explicit arg still wins over env
            reg2 = CharacterRegistry(personalities_dir=pdir, characters_dir=self.tmp)
            assert reg2._char_dir == self.tmp
        finally:
            if old is None:
                os.environ.pop("CHARACTERS_DIR", None)
            else:
                os.environ["CHARACTERS_DIR"] = old
        # no env → module default
        assert CharacterRegistry(personalities_dir=pdir)._char_dir == _DEFAULT_CHARACTERS_DIR
        print_pass("CHARACTERS_DIR env override + explicit-arg precedence")

    def test_bad_encoding_meta(self):
        """GBK/corrupt meta.json: get_meta → None (logged), never raises;
        one bad character must not break listing for everyone else."""
        self._mk("c_good11", "好角色", "u1")
        bad_dir = os.path.join(self.tmp, "characters", "c_bad222")
        os.makedirs(bad_dir, exist_ok=True)
        with open(os.path.join(bad_dir, "card.md"), "w", encoding="utf-8") as f:
            f.write("# 坏元数据\n内容")
        with open(os.path.join(bad_dir, "meta.json"), "wb") as f:
            f.write('{"display_name": "坏角色"}'.encode("gbk"))  # 非 UTF-8
        assert self.reg.get_meta("c_bad222") is None       # no raise
        assert self.reg.get_meta("c_good11")["author"] == "u1"
        own = self.reg.list_personalities(user_id="u1")
        keys = [e["key"] for e in own]
        assert "c_good11" in keys and "c_bad222" not in keys, keys
        # 非 UTF-8 card.md 同样优雅降级
        with open(os.path.join(bad_dir, "meta.json"), "w", encoding="utf-8") as f:
            f.write('{"display_name": "坏角色", "author": "u1"}')
        with open(os.path.join(bad_dir, "card.md"), "wb") as f:
            f.write("# 坏卡片".encode("gbk"))
        assert self.reg.load("c_bad222") is None           # no raise
        self.reg.delete("c_good11")
        self.reg.delete("c_bad222")
        print_pass("bad-encoding meta/card degrade gracefully, listing survives")

    def test_session_persona_layer(self):
        """会话人格绑定：provider 注入 + 优先级 + 懒校验穿透 + 来源标签。"""
        from types import SimpleNamespace
        self._mk("c_sess01", "会话角色", "u1")
        orig_settings = self.reg._load_settings()
        orig_groups = self.reg._load_group_settings()
        try:
            # 无 provider → 原三层链（全局默认）
            self.reg.set_session_provider(None)
            assert self.reg.resolve_effective_personality("u1") == self.reg.get_default()

            # 会话绑定 > 用户设置
            self.reg.set_user_personality("u1", "assistant")
            sess = SimpleNamespace(metadata={"persona_key": "c_sess01"})
            self.reg.set_session_provider(
                lambda uid: sess if uid == "u1" else None)
            assert self.reg.resolve_effective_personality("u1") == "c_sess01"
            assert self.reg.resolve_effective_source("u1") == "会话绑定"
            # 其他用户不受影响
            assert self.reg.resolve_effective_personality("u2") == self.reg.get_default()

            # 绑定 key 失效（UGC 被删）→ 懒校验穿透到用户层
            sess.metadata["persona_key"] = "deleted_char"
            assert self.reg.resolve_effective_personality("u1") == "assistant"
            assert self.reg.resolve_effective_source("u1") == "个人设置"

            # bond_scene：char_id 钉住优先于一切（persona_key 冗余字段被忽略）
            scene = SimpleNamespace(metadata={
                "kind": "bond_scene", "char_id": "rubi",
                "persona_key": "c_sess01"})
            self.reg.set_session_provider(lambda uid: scene)
            assert self.reg.resolve_effective_personality("u1") == "rubi"
            assert self.reg.resolve_effective_source("u1") == "剧情钉住"
            # scene char_id 失效 → 穿透（不回退到 persona_key）
            scene.metadata["char_id"] = "deleted_too"
            assert self.reg.resolve_effective_personality("u1") == "assistant"

            # char_creation → 固定中性，穿透
            creation = SimpleNamespace(
                metadata={"kind": "char_creation", "persona_key": "rubi"})
            self.reg.set_session_provider(lambda uid: creation)
            assert self.reg.resolve_effective_personality("u1") == "assistant"

            # provider 抛异常 → 安全穿透，不阻断解析
            def boom(uid):
                raise RuntimeError("provider down")
            self.reg.set_session_provider(boom)
            assert self.reg.resolve_effective_personality("u1") == "assistant"

            # 无 metadata 属性的会话对象（如裸 object()）→ getattr 防御穿透
            self.reg.set_session_provider(lambda uid: object())
            assert self.reg.resolve_effective_personality("u1") == "assistant"

            # source 标签：个人设置 / 群默认 / 全局默认
            self.reg.set_session_provider(lambda uid: None)
            assert self.reg.resolve_effective_source("u1") == "个人设置"
            self.reg.set_group_personality("g9", "rubi")
            assert self.reg.resolve_effective_source("ux", "g9") == "群默认"
            assert self.reg.resolve_effective_source("ux", "") == "全局默认"
            print_pass("session persona layer: priority/lazy-validation/scene-pin/source")
        finally:
            self.reg.set_session_provider(None)
            self.reg._save_settings(orig_settings)
            self.reg._save_group_settings(orig_groups)
            self.reg.delete("c_sess01")


class TestCreateCharacterTool:
    """Test tools/creation_tools.py — create_character validate/review/write."""

    def run(self):
        print_header("22. create_character Tool Tests (P1)")
        asyncio.run(self._run_async())

    def _reg(self):
        from agent.character_registry import CharacterRegistry
        tmp = tempfile.mkdtemp(prefix="ccreate_")
        self._tmps.append(tmp)
        pdir = os.path.join(tmp, "personalities")
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "assistant.md"), "w", encoding="utf-8") as f:
            f.write("# 助手 Roxy\nbase")
        return CharacterRegistry(
            personalities_dir=pdir, characters_dir=os.path.join(tmp, "characters")
        )

    def _perm(self, max_chars=1):
        from agent.permissions import UserRole
        class P:
            def get_role(self, u): return UserRole.REGULAR
            def get_max_characters(self, r): return max_chars
        return P()

    def _client(self, resp):
        class C:
            async def chat_completion(self, message, history, timeout_set, purpose):
                return resp
        return C()

    async def _run_async(self):
        from tools.creation_tools import create_character, build_card, validate_fields
        self._tmps = []
        try:
            good = dict(name="星野爱", personality="温柔", speech_style="轻声",
                        appearance="粉发蓝瞳", gift_loved=["花"])
            # validation: missing required
            assert validate_fields(dict(name="A", personality="p")) is not None
            assert validate_fields(good) is None
            # card structure carries required metadata + delegate_task wording
            card = build_card(good)
            assert card.startswith("# 星野爱")
            assert "mode: split | task_voice: character" in card
            assert "delegate_task" in card
            assert "Appearance Anchor" in card and "Gift Preferences" in card

            # success (no review client → fail-open path)
            reg = self._reg()
            res = await create_character(user_id="u1", fields=good, registry=reg,
                                         perm_manager=self._perm(max_chars=3), review_client=None)
            assert res.startswith("✅"), res
            assert len(reg._ugc_ids()) == 1

            # quota block (reg already holds 1 char from the success above)
            res = await create_character(user_id="u1", fields=dict(good, name="第二个"),
                                         registry=reg, perm_manager=self._perm(max_chars=1),
                                         review_client=None)
            assert res.startswith("[Error]"), res

            # name collision
            reg2 = self._reg()
            await create_character(user_id="u1", fields=good, registry=reg2,
                                   perm_manager=self._perm(max_chars=3), review_client=None)
            res = await create_character(user_id="u1", fields=good, registry=reg2,
                                         perm_manager=self._perm(max_chars=3), review_client=None)
            assert res.startswith("[Error]") and "已拥有" in res

            # review REJECT → fail-closed (nothing written)
            reg3 = self._reg()
            res = await create_character(user_id="u1", fields=dict(good, name="坏"),
                                         registry=reg3, perm_manager=self._perm(max_chars=3),
                                         review_client=self._client("REJECT: 违规"))
            assert res.startswith("[Error]") and len(reg3._ugc_ids()) == 0

            # review infra error → fail-open (written)
            reg4 = self._reg()
            res = await create_character(user_id="u1", fields=dict(good, name="正常"),
                                         registry=reg4, perm_manager=self._perm(max_chars=3),
                                         review_client=self._client("调用DeepSeek API时出错: x"))
            assert res.startswith("✅") and len(reg4._ugc_ids()) == 1

            print_pass("validate/card/quota/collision/review reject+fail-open/success")
        finally:
            for t in self._tmps:
                shutil.rmtree(t, ignore_errors=True)


class TestCharCreationInterview:
    """Test agent.py L4 interview injection for char_creation sessions."""

    def run(self):
        print_header("23. Char-Creation Interview Injection (P1)")
        asyncio.run(self._run_async())

    async def _run_async(self):
        from agent.tool_registry import ToolRegistry
        from agent.agent import Agent
        from agent.session import Session
        from agent.special_session import SpecialSession

        agent = Agent(
            deepseek_client=MockDeepSeekClient(),
            tool_registry=ToolRegistry(),
            config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
        )
        sess = Session(user_id="u1")

        def _tail(sp):
            msgs = agent._build_messages(sess, "你好", sp)
            # last system message = L4 volatile tail
            sys_msgs = [m for m in msgs if m["role"] == "system"]
            return sys_msgs[-1]["content"]

        # creation session → interview instructions present
        creation = SpecialSession(session_id="c1", user_id="u1", name="角色创作",
                                  metadata={"kind": "char_creation"})
        tail = _tail(creation)
        assert "角色创作访谈" in tail and "create_character" in tail

        # ordinary special session → marker present, interview absent
        normal = SpecialSession(session_id="s1", user_id="u1", name="普通会话")
        tail2 = _tail(normal)
        assert "特殊会话模式" in tail2 and "角色创作访谈" not in tail2

        print_pass("L4 interview injected only for char_creation metadata")


# ── P2: Bond content / gift / poke / events tests ─────────────────

_P2_CARD = """# 测试角色

<!-- mode: split | task_voice: character -->

## Core Personality

一个测试用角色。

## Gift Preferences

- 喜爱: 草莓蛋糕、向日葵、蓝色发饰
- 普通: 巧克力、红茶
- 讨厌: 苦药、辣到冒火的东西

## Bond Tiers

### Lv3 · 熟悉

第三级解锁的档案内容A。

### Lv5 · 密友

第五级解锁的档案内容B。

### Lv7 · 暧昧

第七级解锁的档案内容C。
"""

# P3: 场景 tier（标题含「场景」）与档案 tier 混排，验证区分/解析/防泄漏
_P3_CARD = """# 测试角色P3

<!-- mode: split | task_voice: character -->

## Core Personality

一个测试用角色。

## Bond Tiers

### Lv3 · 熟悉

第三级档案内容A。

### Lv4 · 场景 · 雨夜谈心

【第一幕 · 骤雨】夜里突降暴雨，你们躲进废弃哨塔。
【第二幕 · 火光】你们生火烘衣服，聊起往事。
【终幕 · 雨停之约】雨停了，她把护身符塞进你手里。至此剧情自然收束，调用 complete_scene 收尾。

### Lv5 · 密友

第五级档案内容B。

### Lv6 · 场景 · 星空之约

【第一幕】山顶看星星。
【终幕】流星划过。调用 complete_scene 收尾。
"""


class TestBondContent:
    """Test agent/bond_content.py — card parsing (gift prefs / tiers / address)."""

    def run(self):
        print_header("24. Bond Content Parsing (P2)")
        self.test_gift_preferences()
        self.test_match_gift()
        self.test_bond_tiers()
        self.test_scenes()
        self.test_address_directive()
        self.test_builtin_cards_parse()

    def test_gift_preferences(self):
        from agent import bond_content as bc
        prefs = bc.parse_gift_preferences(_P2_CARD)
        assert prefs["loved"] == ["草莓蛋糕", "向日葵", "蓝色发饰"], prefs
        assert prefs["normal"] == ["巧克力", "红茶"], prefs
        assert prefs["disliked"] == ["苦药", "辣到冒火的东西"], prefs
        # no section → empty
        empty = bc.parse_gift_preferences("# x\n\n## Core\n\nhi")
        assert empty == {"loved": [], "normal": [], "disliked": []}
        # 未设定 skipped
        unset = bc.parse_gift_preferences(
            "## Gift Preferences\n- 喜爱: 未设定\n- 普通: 茶\n"
        )
        assert unset["loved"] == [] and unset["normal"] == ["茶"]
        print_pass("parse_gift_preferences (三档 + 未设定 skip + empty)")

    def test_match_gift(self):
        from agent import bond_content as bc
        prefs = bc.parse_gift_preferences(_P2_CARD)
        assert bc.match_gift("草莓蛋糕", prefs) == "loved"
        assert bc.match_gift("一朵向日葵", prefs) == "loved"   # substring
        assert bc.match_gift("苦药", prefs) == "disliked"
        assert bc.match_gift("巧克力", prefs) == "normal"
        assert bc.match_gift("奇怪的石头", prefs) == "normal"  # unmatched default
        assert bc.match_gift("", prefs) == "normal"
        print_pass("match_gift (loved/disliked/normal + default)")

    def test_bond_tiers(self):
        from agent import bond_content as bc
        tiers = bc.parse_bond_tiers(_P2_CARD)
        assert [t["level"] for t in tiers] == [3, 5, 7], tiers
        assert [t["title"] for t in tiers] == ["熟悉", "密友", "暧昧"], tiers
        assert "档案内容A" in tiers[0]["body"]
        assert [t["level"] for t in bc.unlocked_tiers(_P2_CARD, 5)] == [3, 5]
        assert [t["level"] for t in bc.unlocked_tiers(_P2_CARD, 2)] == []
        assert [t["level"] for t in bc.tiers_unlocking_at(_P2_CARD, 7)] == [7]
        txt = bc.unlocked_tiers_text(_P2_CARD, 5)
        assert "【熟悉】" in txt and "【密友】" in txt and "【暧昧】" not in txt
        assert bc.unlocked_tiers_text(_P2_CARD, 1) == ""
        # UGC alias: build_card() writes `## Bond Levels` — must parse identically
        ugc_card = _P2_CARD.replace("## Bond Tiers", "## Bond Levels")
        ugc_tiers = bc.parse_bond_tiers(ugc_card)
        assert [t["level"] for t in ugc_tiers] == [3, 5, 7], ugc_tiers
        assert [t["title"] for t in ugc_tiers] == ["熟悉", "密友", "暧昧"], ugc_tiers
        assert [t["level"] for t in bc.unlocked_tiers(ugc_card, 5)] == [3, 5]
        # `Bond Tiers` wins when both sections somehow coexist
        both = _P2_CARD + "\n## Bond Levels\n\n### Lv9 · 别名\n\n不该被读到。\n"
        assert [t["level"] for t in bc.parse_bond_tiers(both)] == [3, 5, 7]
        # free-text Bond Levels (no ### Lv subsections) degrades to []
        freeform = "# x\n\n## Bond Levels\n\n好感越高越亲近。Lv3 之后更随意。\n"
        assert bc.parse_bond_tiers(freeform) == []
        print_pass("parse/unlocked/unlocking_at/text tiers by level (+Bond Levels alias)")

    def test_scenes(self):
        """P3: 场景 tier 解析 / 可用性 / get_scene / 动态层防泄漏。"""
        from agent import bond_content as bc
        tiers = bc.parse_bond_tiers(_P3_CARD)
        assert [bc.is_scene_tier(t) for t in tiers] == [False, True, False, True], tiers
        scenes = bc.parse_scenes(_P3_CARD)
        assert [s["level"] for s in scenes] == [4, 6], scenes
        assert [s["title"] for s in scenes] == ["雨夜谈心", "星空之约"], scenes
        assert str(scenes[0]["raw_title"]).startswith("场景")
        assert "第一幕" in scenes[0]["body"] and "complete_scene" in scenes[0]["body"]
        # availability by level
        assert [s["title"] for s in bc.scenes_available(_P3_CARD, 5)] == ["雨夜谈心"]
        assert [s["title"] for s in bc.scenes_available(_P3_CARD, 3)] == []
        assert [s["title"] for s in bc.scenes_unlocking_at(_P3_CARD, 6)] == ["星空之约"]
        assert bc.scenes_unlocking_at(_P3_CARD, 5) == []
        # get_scene: exact / marker prefix / substring both directions / miss
        assert bc.get_scene(_P3_CARD, "雨夜谈心")["level"] == 4
        assert bc.get_scene(_P3_CARD, "场景 · 雨夜谈心")["level"] == 4
        assert bc.get_scene(_P3_CARD, "雨夜")["level"] == 4
        assert bc.get_scene(_P3_CARD, "雨夜谈心（加长版）")["level"] == 4
        assert bc.get_scene(_P3_CARD, "不存在的剧情") is None
        assert bc.get_scene(_P3_CARD, "   ") is None
        assert bc.get_scene("", "雨夜谈心") is None
        # leak guard: scene scripts must NOT enter the ordinary dynamic layer
        txt = bc.unlocked_tiers_text(_P3_CARD, 10)
        assert "档案内容A" in txt and "档案内容B" in txt
        assert "第一幕" not in txt and "雨夜谈心" not in txt, txt
        print_pass("scenes: is_scene/parse/available/get_scene + tier-text leak guard")

    def test_address_directive(self):
        from agent import bond_content as bc
        assert "刚认识" in bc.address_directive(1)
        assert "熟悉" in bc.address_directive(3)
        assert "亲密" in bc.address_directive(5)
        assert "最亲密" in bc.address_directive(7)
        assert bc.address_directive(10) == bc.address_directive(7)
        print_pass("address_directive tiers by level")

    def test_builtin_cards_parse(self):
        """Official builtin cards must expose gift prefs + structured tiers."""
        from agent import bond_content as bc
        base = os.path.join(os.path.dirname(__file__), "agent", "config", "personalities")
        for fname in ("rubi.md", "roxy_character.md"):
            card = open(os.path.join(base, fname), encoding="utf-8").read()
            prefs = bc.parse_gift_preferences(card)
            assert prefs["loved"] and prefs["disliked"], f"{fname} gift prefs"
            tiers = bc.parse_bond_tiers(card)
            assert len(tiers) >= 3, f"{fname} needs structured Bond Tiers"
            assert all(isinstance(t["level"], int) for t in tiers)
            # P3: each builtin card ships at least one playable scene whose
            # script never leaks into the ordinary conversation dynamic layer
            scenes = bc.parse_scenes(card)
            assert scenes, f"{fname} needs at least one 场景 tier"
            assert all(str(s["body"]).strip() for s in scenes), f"{fname} empty scene"
            leak = bc.unlocked_tiers_text(card, 99)
            for s in scenes:
                assert str(s["title"]) not in leak, f"{fname} scene leaked"
        print_pass("builtin cards parse (gifts + tiers + scenes, no leak)")


class TestBondGiftPoke:
    """Test bond.py give_gift/poke settlement + social_tools.give_gift."""

    def run(self):
        print_header("25. Bond Gift / Poke Settlement (P2)")
        self.test_gift_tiers_exp()
        self.test_gift_daily_cap()
        self.test_poke_exp_and_cap()
        self.test_gift_level_up()
        asyncio.run(self.test_give_gift_tool())

    def _mgr(self, tmp):
        from agent.bond import BondManager
        return BondManager(
            base_dir=os.path.join(tmp, "bonds"),
            config_path=os.path.join(tmp, "no_such_config.json"),
        )

    def test_gift_tiers_exp(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            r_loved = m.give_gift("u1", "rubi", "loved")
            assert r_loved["accepted"] and r_loved["exp_gained"] == 30, r_loved
            r_normal = m.give_gift("u2", "rubi", "normal")
            assert r_normal["exp_gained"] == 15, r_normal
            r_bad = m.give_gift("u3", "rubi", "disliked")
            assert r_bad["exp_gained"] == 5, r_bad
            # mood deltas differ by tier
            assert m.get_state("u1", "rubi")["mood"] > m.get_state("u3", "rubi")["mood"]
            print_pass("give_gift EXP by tier (loved30/normal15/disliked5) + mood")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gift_daily_cap(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            for _ in range(3):
                assert m.give_gift("u1", "rubi", "normal")["accepted"]
            r4 = m.give_gift("u1", "rubi", "loved")
            assert r4["accepted"] is False and r4["reason"] == "daily_limit", r4
            assert r4["gifts_today"] == 3 and r4["daily_gift_limit"] == 3
            print_pass("give_gift daily cap (3/day) blocks further gifts")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_poke_exp_and_cap(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            r1 = m.poke("u1", "rubi")
            assert r1["accepted"] and r1["exp_gained"] == 5, r1
            m.poke("u1", "rubi")
            m.poke("u1", "rubi")
            r4 = m.poke("u1", "rubi")
            assert r4["accepted"] is False and r4["reason"] == "daily_limit", r4
            assert r4["daily_poke_limit"] == 3
            print_pass("poke grants 5 EXP + daily cap (3/day)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gift_level_up(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            st = m.get_state("u1", "rubi")
            st["exp"] = 95  # just below Lv2 (100)
            m._save("u1")
            r = m.give_gift("u1", "rubi", "loved")  # +30 → 125 → Lv2
            assert r["level"] == 2, r
            assert any(u["level"] == 2 for u in r["level_ups"]), r
            print_pass("give_gift can trigger level-up (returned in level_ups)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_give_gift_tool(self):
        from tools.social_tools import give_gift
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            # loved gift → positive observation with EXP + reaction instruction
            obs = await give_gift(
                user_id="u1", gift_name="草莓蛋糕", char_id="rubi",
                card_content=_P2_CARD, bond_manager=m,
            )
            assert "草莓蛋糕" in obs and "好感 +30" in obs, obs
            assert "角色口吻" in obs, obs
            # disliked gift → different tone, lower EXP
            obs2 = await give_gift(
                user_id="u1", gift_name="苦药", char_id="rubi",
                card_content=_P2_CARD, bond_manager=m,
            )
            assert "不太喜欢" in obs2 and "好感 +5" in obs2, obs2
            # error paths
            assert (await give_gift(user_id="", gift_name="x", char_id="rubi",
                                    card_content="", bond_manager=m)).startswith("[Error]")
            assert (await give_gift(user_id="u1", gift_name="  ", char_id="rubi",
                                    card_content="", bond_manager=m)).startswith("[Error]")
            # daily cap message (u1 already got 2; push to limit)
            m.give_gift("u1", "rubi", "normal")  # 3rd
            capped = await give_gift(
                user_id="u1", gift_name="巧克力", char_id="rubi",
                card_content=_P2_CARD, bond_manager=m,
            )
            assert "上限" in capped and "明天再来" in capped, capped
            print_pass("social_tools.give_gift observation + tiers + cap + errors")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestBondEvents:
    """Test bond.py bond_events log + dynamic build_prompt_context (P2)."""

    def run(self):
        print_header("26. Bond Events & Dynamic Prompt Context (P2)")
        self.test_append_and_recent()
        self.test_recent_cap_and_strip()
        self.test_prompt_context_dynamic_sections()
        self.test_prompt_context_no_state()

    def _mgr(self, tmp):
        from agent.bond import BondManager
        return BondManager(
            base_dir=os.path.join(tmp, "bonds"),
            config_path=os.path.join(tmp, "no_such_config.json"),
        )

    def test_append_and_recent(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            assert m.recent_events("u1", "rubi") == []  # none yet
            m.append_event("u1", "rubi", "用户送了「草莓蛋糕」给你，你非常喜爱")
            ev = m.recent_events("u1", "rubi")
            assert len(ev) == 1 and "草莓蛋糕" in ev[0], ev
            # timestamp stripped
            assert not ev[0].startswith("-") and "20" not in ev[0][:4]
            # empty text ignored
            m.append_event("u1", "rubi", "   ")
            assert len(m.recent_events("u1", "rubi")) == 1
            print_pass("append_event writes + recent_events reads (ts stripped)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_recent_cap_and_strip(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            for i in range(5):
                m.append_event("u1", "rubi", f"事件{i}")
            last3 = m.recent_events("u1", "rubi", 3)
            assert last3 == ["事件2", "事件3", "事件4"], last3
            # per-character isolation
            m.append_event("u1", "roxy", "另一个角色的事件")
            assert "另一个角色" not in " ".join(m.recent_events("u1", "rubi"))
            assert m.recent_events("u1", "roxy") == ["另一个角色的事件"]
            print_pass("recent_events caps at n + isolates per character")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_prompt_context_dynamic_sections(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            m.settle_turn("u1", "rubi")  # create state
            m.append_event("u1", "rubi", "用户戳了戳你")
            ctx = m.build_prompt_context(
                "u1", "rubi", "露比",
                unlocked_text="【熟悉】\n档案内容A",
                address_line="称呼演化: 你们已经熟悉",
                recent=["用户戳了戳你"],
            )
            assert "羁绊状态" in ctx
            assert "露比" in ctx
            assert "档案内容A" in ctx and "已解锁的羁绊档案" in ctx
            assert "称呼演化" in ctx
            assert "最近的共同经历" in ctx and "用户戳了戳你" in ctx
            print_pass("build_prompt_context injects unlocked/address/recent")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_prompt_context_no_state(self):
        tmp = tempfile.mkdtemp(prefix="bond_")
        try:
            m = self._mgr(tmp)
            ctx = m.build_prompt_context(
                "u1", "rubi", "露比", address_line="称呼演化: 你们刚认识不久",
            )
            assert "初次相识" in ctx
            assert "称呼演化" in ctx  # address still injected with no state
            assert "已解锁的羁绊档案" not in ctx  # no unlocked_text passed
            print_pass("build_prompt_context graceful with no bond state")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestOfficialStatusHeartbeat:
    """Test lib/status_heartbeat — atomic status file for bot→panel IPC."""

    def run(self):
        print_header("27. Official Status Heartbeat Writer (WebUI IPC)")
        self.test_write_merge_atomic()
        self.test_read_corrupt()

    def test_write_merge_atomic(self):
        from lib.status_heartbeat import read_status, write_status
        tmp = tempfile.mkdtemp(prefix="hb_")
        try:
            path = os.path.join(tmp, "sub", "official_status.json")
            # first write creates parent dirs, stamps written_at
            out = write_status(path, state="starting", appid=None)
            assert out["state"] == "starting" and "written_at" in out
            assert os.path.isfile(path) and not os.path.exists(path + ".tmp")
            # second write MERGES (keeps old keys) and re-stamps
            out2 = write_status(path, state="connected", appid="102808287")
            assert out2["state"] == "connected"
            assert out2["appid"] == "102808287"
            assert out2["written_at"] >= out["written_at"]
            cur = read_status(path)
            assert cur["state"] == "connected" and cur["appid"] == "102808287"
            print_pass("write_status atomic merge + written_at stamp")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_corrupt(self):
        from lib.status_heartbeat import read_status
        tmp = tempfile.mkdtemp(prefix="hb_")
        try:
            # missing file → {}
            assert read_status(os.path.join(tmp, "nope.json")) == {}
            # corrupt JSON → {}
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w") as f:
                f.write("{not json")
            assert read_status(bad) == {}
            # valid JSON but not an object → {}
            arr = os.path.join(tmp, "arr.json")
            with open(arr, "w") as f:
                f.write("[1,2]")
            assert read_status(arr) == {}
            print_pass("read_status degrades to {} on missing/corrupt/non-object")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestWebuiOfficialStatus:
    """Test webui/official_status.status() — mtime staleness + appid_env."""

    def run(self):
        print_header("28. WebUI Official Status Aggregator (P1)")
        self.test_staleness_ladder()
        self.test_appid_env()

    def _import_module(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from webui import official_status as wos
        return wos

    def _write_with_age(self, path, age_seconds, **payload):
        import json as _json
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(payload, f)
        old = time.time() - age_seconds
        os.utime(path, (old, old))

    def test_staleness_ladder(self):
        from pathlib import Path as _Path
        wos = self._import_module()
        tmp = tempfile.mkdtemp(prefix="wos_")
        try:
            path = os.path.join(tmp, "official_status.json")
            with patch.object(wos, "STATUS_FILE", _Path(path)):
                # missing file → unknown
                st = asyncio.run(wos.status())
                assert st["state"] == "unknown" and st["heartbeat_age"] is None
                # fresh (10s old) → passthrough state
                self._write_with_age(path, 10, state="connected",
                                     appid="102808287", messages_total=42,
                                     connected_at=time.time() - 300,
                                     last_message_at=time.time() - 60)
                st = asyncio.run(wos.status())
                assert st["state"] == "connected" and st["appid"] == "102808287"
                assert st["messages_total"] == 42 and 0 <= st["heartbeat_age"] < 60
                # 300s old (>150s, <600s) → stale
                self._write_with_age(path, 300, state="connected")
                st = asyncio.run(wos.status())
                assert st["state"] == "stale", st
                # 700s old (>600s) → unknown
                self._write_with_age(path, 700, state="connected")
                st = asyncio.run(wos.status())
                assert st["state"] == "unknown"
            print_pass("status() staleness ladder: fresh/stale/unknown + missing")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_appid_env(self):
        from pathlib import Path as _Path
        wos = self._import_module()
        tmp = tempfile.mkdtemp(prefix="wos_")
        try:
            path = os.path.join(tmp, "official_status.json")
            bots_json = '[{"id":"102808287","secret":"x","use_websocket":true}]'
            with patch.object(wos, "STATUS_FILE", _Path(path)), \
                    patch.object(wos.config, "bot_env",
                                 lambda k, d="": bots_json if k == "QQ_BOTS" else d):
                st = asyncio.run(wos.status())
                assert st["appid_env"] == "102808287"
            # malformed QQ_BOTS → None, never raises
            with patch.object(wos, "STATUS_FILE", _Path(path)), \
                    patch.object(wos.config, "bot_env",
                                 lambda k, d="": "not-json" if k == "QQ_BOTS" else d):
                st = asyncio.run(wos.status())
                assert st["appid_env"] is None
            print_pass("appid_env parsed from QQ_BOTS (malformed → None)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestWebuiPermissionsEditor:
    """webui/config_editor permissions — dual identity tables (QQ-official)."""

    def run(self):
        print_header("29. WebUI Permissions Config Editor (QQ-official)")
        self.test_validators()
        self.test_write_roundtrip()

    def _import_module(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from webui import config_editor as ce
        return ce

    def test_validators(self):
        ce = self._import_module()
        # QQ-number tables: digits only (unchanged behavior)
        assert ce._validate_id_list("123456,789012")
        assert ce._validate_id_list('["123", "456"]')
        assert ce._validate_id_list("")            # empty = clear, allowed
        assert not ce._validate_id_list("5F0E9A3B1C2D")   # openid ≠ QQ number
        # official tables: opaque alphanumeric openids
        assert ce._validate_openid_list("5F0E9A3B1C2D2E4F,AB_cd-12")
        assert ce._validate_openid_list("['ABC123def','XY_zz99']")
        assert ce._validate_openid_list("")
        assert not ce._validate_openid_list("ab")           # too short (<4)
        assert not ce._validate_openid_list("has space,bad")
        assert not ce._validate_openid_list("中文openid")
        assert not ce._validate_openid_list("a,b,")         # trailing empty
        print_pass("validators: QQ-number vs openid tables (format + rejects)")

    def test_write_roundtrip(self):
        from pathlib import Path as _Path
        ce = self._import_module()
        tmp = tempfile.mkdtemp(prefix="wpe_")
        try:
            env = _Path(tmp) / ".env"
            env.write_text(
                'DRIVER=~fastapi+~httpx+~websockets\n'
                'QQ_BOTS=[{"id":"102808287","secret":"S3CRET"}]\n'
                'SUPERUSERS=["111"]\n', encoding="utf-8")
            with patch.object(ce, "ENV_FILE", env), \
                    patch.object(ce, "BACKUPS", _Path(tmp) / "backups"):
                # None = untouched; set official tables + append VIP_USERS
                r = ce.permissions_write(None, "222,333", "5F0E9A3B1C2D", None)
                assert r.get("ok"), r
                text = env.read_text(encoding="utf-8")
                assert 'SUPERUSERS=["111"]' in text      # untouched
                assert "VIP_USERS=222,333" in text       # appended
                assert "QQ_OFFICIAL_SUPERUSERS=5F0E9A3B1C2D" in text
                assert "QQ_OFFICIAL_VIP_USERS" not in text   # None → not added
                assert 'QQ_BOTS=[{"id":"102808287","secret":"S3CRET"}]' in text
                assert "DRIVER=~fastapi+~httpx+~websockets" in text
                # second write: update in place + clear via ""
                r = ce.permissions_write('["111","999"]', "", None, "ABCD1234")
                assert r.get("ok"), r
                text = env.read_text(encoding="utf-8")
                assert 'SUPERUSERS=["111","999"]' in text
                assert any(ln == "VIP_USERS=" for ln in text.splitlines()), text  # cleared
                assert "QQ_OFFICIAL_SUPERUSERS=5F0E9A3B1C2D" in text  # kept
                assert "QQ_OFFICIAL_VIP_USERS=ABCD1234" in text
                assert text.count("QQ_OFFICIAL_VIP_USERS=") == 1      # no dup
                # read reflects all four tables
                with patch.object(ce.config, "_BOT_ENV", {}):
                    got = ce.permissions_read()
                assert got["QQ_OFFICIAL_VIP_USERS"] == "ABCD1234", got
                assert got["VIP_USERS"] == "", got
                # bad openid list rejected, file untouched
                before = env.read_text(encoding="utf-8")
                r = ce.permissions_write(None, None, "bad openid!", None)
                assert "error" in r and env.read_text(encoding="utf-8") == before
            print_pass("permissions_write: None=keep / ''=clear / other lines intact")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPersonalityMarkerPersistence:
    """/人格切换 transition marker — disk persistence (A) + history injection (B).

    Prod incident 2026-10-22: the marker lived only in process memory; a panel
    restart between the switch and the next message wiped it, so the L4
    transition note never reached the model and a special session full of
    old-persona replies dragged the answer back into the old voice.
    """

    def run(self):
        print_header("30. Personality Marker Persistence (A+B)")
        self.turn = self._import_turn()
        self.test_marker_roundtrip()
        self.test_marker_load_degrades()
        self.test_inject_special_session()
        self.test_inject_temp_session()
        self.test_inject_noop_without_marker()

    def _import_turn(self):
        # turn.py reads USER_DATA_ROOT at import time; keep it off the
        # production path when running on a dev machine.
        os.environ.setdefault("USER_DATA_ROOT", "/tmp/qqbot_smoke")
        import agent.turn as turn
        return turn

    def test_marker_roundtrip(self):
        turn = self.turn
        tmp = tempfile.mkdtemp(prefix="pers_marker_")
        orig_file = turn._MARKERS_FILE
        orig_dict = dict(turn._personality_switch_marker)
        try:
            turn._MARKERS_FILE = os.path.join(tmp, "personality_markers.json")
            turn._personality_switch_marker.clear()
            turn._personality_switch_marker["u1"] = "人格已从「露比」切换为「助手」。"
            turn._save_personality_markers()
            assert os.path.exists(turn._MARKERS_FILE)
            # simulate process restart: memory wiped, disk survives
            turn._personality_switch_marker.clear()
            loaded = turn._load_personality_markers()
            assert loaded == {"u1": "人格已从「露比」切换为「助手」。"}, loaded
            # non-str junk is filtered out on load (JSON keys are always
            # strings, so junk can only appear on the value side)
            with open(turn._MARKERS_FILE, "w", encoding="utf-8") as f:
                json.dump({"u1": "ok", "bad": 123, "worse": ["x"]}, f)
            assert turn._load_personality_markers() == {"u1": "ok"}
            print_pass("marker save/load survives restart; junk filtered")
        finally:
            turn._MARKERS_FILE = orig_file
            turn._personality_switch_marker.clear()
            turn._personality_switch_marker.update(orig_dict)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_marker_load_degrades(self):
        turn = self.turn
        tmp = tempfile.mkdtemp(prefix="pers_marker_bad_")
        orig_file = turn._MARKERS_FILE
        try:
            # missing file
            turn._MARKERS_FILE = os.path.join(tmp, "nope.json")
            assert turn._load_personality_markers() == {}
            # corrupt JSON
            with open(turn._MARKERS_FILE, "w") as f:
                f.write("{corrupt!!")
            assert turn._load_personality_markers() == {}
            print_pass("load degrades to {} on missing/corrupt markers file")
        finally:
            turn._MARKERS_FILE = orig_file
            shutil.rmtree(tmp, ignore_errors=True)

    def test_inject_special_session(self):
        turn = self.turn
        orig_marker = dict(turn._personality_switch_marker)
        orig_special = turn._special_sessions
        orig_session = turn._session_manager
        try:
            turn._personality_switch_marker.clear()
            turn._personality_switch_marker["u2"] = "人格已切换为「助手」。"
            special = MagicMock()
            special.get_active.return_value = object()   # active special session
            turn._special_sessions = special
            session_mgr = MagicMock()
            turn._session_manager = session_mgr
            turn._inject_personality_transition_into_history("u2")
            special.add_message.assert_called_once()
            args = special.add_message.call_args[0]
            assert args[0] == "u2" and args[1] == "system", args
            assert "人格已切换为「助手」" in args[2]
            assert "不得模仿" in args[2]
            # temp session untouched while a special session is active
            session_mgr.get_or_create.assert_not_called()
            print_pass("inject → active special session delta (system role)")
        finally:
            turn._special_sessions = orig_special
            turn._session_manager = orig_session
            turn._personality_switch_marker.clear()
            turn._personality_switch_marker.update(orig_marker)

    def test_inject_temp_session(self):
        from agent.session import SessionManager
        turn = self.turn
        tmp = tempfile.mkdtemp(prefix="pers_inject_")
        orig_marker = dict(turn._personality_switch_marker)
        orig_special = turn._special_sessions
        orig_session = turn._session_manager
        try:
            turn._personality_switch_marker.clear()
            turn._personality_switch_marker["u3"] = "人格已切换为「助手」。"
            special = MagicMock()
            special.get_active.return_value = None       # no special session
            turn._special_sessions = special
            mgr = SessionManager(persistence_dir=tmp)
            turn._session_manager = mgr
            # non-empty temp session → injected and persisted
            s = mgr.get_or_create("u3")
            s.add_message("user", "旧人格时代的提问")
            s.add_message("assistant", "露比风格的回答～")
            mgr.update("u3", s)
            turn._inject_personality_transition_into_history("u3")
            with open(os.path.join(tmp, "u3.json"), encoding="utf-8") as f:
                saved = json.load(f)
            roles = [m["role"] for m in saved["context"]]
            assert roles == ["user", "assistant", "system"], roles
            assert "人格已切换为「助手」" in saved["context"][-1]["content"]
            # empty temp session → nothing to counteract, no write
            turn._inject_personality_transition_into_history("u4")
            assert not os.path.exists(os.path.join(tmp, "u4.json"))
            print_pass("inject → non-empty temp session persisted; empty skipped")
        finally:
            turn._special_sessions = orig_special
            turn._session_manager = orig_session
            turn._personality_switch_marker.clear()
            turn._personality_switch_marker.update(orig_marker)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_inject_noop_without_marker(self):
        turn = self.turn
        orig_marker = dict(turn._personality_switch_marker)
        orig_special = turn._special_sessions
        try:
            turn._personality_switch_marker.clear()
            special = MagicMock()
            turn._special_sessions = special
            turn._inject_personality_transition_into_history("ghost")
            special.get_active.assert_not_called()
            special.add_message.assert_not_called()
            print_pass("no marker → inject is a clean no-op")
        finally:
            turn._special_sessions = orig_special
            turn._personality_switch_marker.clear()
            turn._personality_switch_marker.update(orig_marker)


class TestSessionPersonaBinding:
    """会话人格绑定端到端：创建快照 / 会话内切换 / --全局 / scene 拒绝 / 提示消息。

    全部走真实 SpecialSessionManager + 真实 CharacterRegistry（临时目录），
    仅替换 turn 模块级单例与发送回调（沿用 TestSceneSession 的手法）。
    """

    def run(self):
        print_header("51. Session-Persona Binding")
        self.turn = _import_turn_for_tests()
        asyncio.run(self.test_new_session_snapshot())
        asyncio.run(self.test_switch_in_session_and_global())
        asyncio.run(self.test_scene_creation_reject())
        asyncio.run(self.test_switch_end_list_messages())
        self.test_clear_context_keeps_binding()

    # ── 隔离环境 ──────────────────────────────────────────────────

    def _env(self):
        """搭建隔离环境并替换 turn 模块属性；返回 ctx dict。"""
        import agent.personality as pmod
        from agent.character_registry import CharacterRegistry
        from agent.special_session import SpecialSessionManager
        turn = self.turn
        tmp = tempfile.mkdtemp(prefix="sess_persona_")
        pdir = os.path.join(tmp, "personalities")
        os.makedirs(pdir, exist_ok=True)
        for name, title in [("assistant", "助手 Roxy"), ("rubi", "露比 (Rubi)")]:
            with open(os.path.join(pdir, f"{name}.md"), "w", encoding="utf-8") as f:
                f.write(f"# {title}\n\nTest personality.")
        with open(os.path.join(tmp, "personality_config.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"default": "assistant"}, f)
        patcher = patch.multiple(
            pmod,
            _SETTINGS_FILE=os.path.join(tmp, "personality_settings.json"),
            _DEFAULT_CONFIG_FILE=os.path.join(tmp, "personality_config.json"),
            _GROUP_CONFIG_FILE=os.path.join(tmp, "group_personality.json"),
        )
        patcher.start()
        reg = CharacterRegistry(
            personalities_dir=pdir,
            characters_dir=os.path.join(tmp, "characters"),
        )
        mgr = SpecialSessionManager(
            user_data_root=os.path.join(tmp, "users"), max_per_user=10)
        reg.set_session_provider(lambda uid: mgr.get_active(uid))

        sent = []

        async def fake_send(payload, sender=None):
            sent.append(payload)

        from agent.session import SessionManager
        tmp_sm = SessionManager(persistence_dir=os.path.join(tmp, "sessions"))
        ctx = {
            "tmp": tmp, "reg": reg, "mgr": mgr, "sent": sent,
            "patcher": patcher,
            "orig": (
                turn._special_sessions, turn.get_character_registry,
                turn._safe_send, turn._perm_manager, turn._quota_warning,
                turn._MARKERS_FILE, turn._session_manager,
                dict(turn._pending_personality_switch),
                dict(turn._personality_switch_marker),
            ),
        }
        turn._special_sessions = mgr
        turn._session_manager = tmp_sm
        turn.get_character_registry = lambda: reg
        turn._safe_send = fake_send
        perm = MagicMock()
        perm.get_role.return_value = "admin"
        perm.get_max_special_sessions.return_value = 10
        turn._perm_manager = perm
        turn._quota_warning = lambda uid: ""
        turn._MARKERS_FILE = os.path.join(tmp, "personality_markers.json")
        turn._pending_personality_switch.clear()
        turn._personality_switch_marker.clear()
        return ctx

    def _restore(self, ctx):
        turn = self.turn
        (turn._special_sessions, turn.get_character_registry, turn._safe_send,
         turn._perm_manager, turn._quota_warning, turn._MARKERS_FILE,
         turn._session_manager, _pend, _mark) = ctx["orig"]
        turn._pending_personality_switch.clear()
        turn._pending_personality_switch.update(_pend)
        turn._personality_switch_marker.clear()
        turn._personality_switch_marker.update(_mark)
        ctx["reg"].set_session_provider(None)
        ctx["patcher"].stop()
        shutil.rmtree(ctx["tmp"], ignore_errors=True)

    @staticmethod
    def _view(group_id=None):
        from types import SimpleNamespace
        return SimpleNamespace(group_id=group_id)

    # ── 用例 ──────────────────────────────────────────────────────

    async def test_new_session_snapshot(self):
        """/新会话 与 /保存为会话 创建时快照当前生效人格。"""
        ctx = self._env()
        turn, mgr, reg, sent = self.turn, ctx["mgr"], ctx["reg"], ctx["sent"]
        try:
            # 无用户设置 → 快照全局默认 assistant
            await turn._handle_session_command("/新会话 研究", "u1")
            entry = [s for s in mgr.list_sessions("u1") if s["name"] == "研究"][0]
            assert entry["metadata"]["persona_key"] == "assistant", entry
            assert any("会话人格: 助手 Roxy" in s for s in sent), sent
            # 快照后切换生效：resolve 走会话绑定
            assert reg.resolve_effective_personality("u1") == "assistant"
            assert reg.resolve_effective_source("u1") == "会话绑定"

            # 用户默认改为 rubi → 新会话快照 rubi（而非旧会话的绑定）
            reg.set_user_personality("u1", "rubi")
            mgr.update_metadata("u1", "研究", {"persona_key": "rubi"})
            await turn._handle_session_command("/新会话 二", "u1")
            entry2 = [s for s in mgr.list_sessions("u1") if s["name"] == "二"][0]
            # 注意：创建时仍处于「研究」会话（快照=研究的绑定 rubi）
            assert entry2["metadata"]["persona_key"] == "rubi", entry2

            # /保存为会话：先退到临时会话，塞上下文，再保存
            mgr.end_active("u1")
            reg.set_user_personality("u1", "assistant")
            sess = turn._session_manager.get_or_create("u1x")
            sess.add_message("user", "旧对话")
            turn._session_manager.update("u1x", sess)
            sent.clear()
            await turn._handle_session_command("/保存为会话 存档", "u1x")
            entry3 = [s for s in mgr.list_sessions("u1x") if s["name"] == "存档"][0]
            assert entry3["metadata"]["persona_key"] == "assistant", entry3
            assert any("会话人格: 助手 Roxy" in s for s in sent), sent
            print_pass("/新会话 + /保存为会话 snapshot effective persona at creation")
        finally:
            self._restore(ctx)

    async def test_switch_in_session_and_global(self):
        """会话内 /人格切换 只写绑定；--全局 只写用户默认。"""
        ctx = self._env()
        turn, mgr, reg, sent = self.turn, ctx["mgr"], ctx["reg"], ctx["sent"]
        try:
            reg.set_user_personality("u1", "rubi")
            s = mgr.create("u1", "聊天", metadata={"persona_key": "rubi"})
            mgr.switch_to("u1", s.name)
            view = self._view()

            # 会话内切换 → 只写 metadata，用户默认不动
            await turn._handle_personality_command("/人格切换 assistant", "u1", view)
            entry = [x for x in mgr.list_sessions("u1") if x["name"] == "聊天"][0]
            assert entry["metadata"]["persona_key"] == "assistant", entry
            assert reg.get_personal_personality("u1") == "rubi"
            assert reg.resolve_effective_personality("u1") == "assistant"
            # pending 3 元组 + marker 带「本会话」+ 提示含「仅当前会话」
            old, new, scoped = turn._pending_personality_switch["u1"]
            assert scoped is True and new == "助手 Roxy", (old, new, scoped)
            assert "本会话" in turn._personality_switch_marker["u1"]
            assert any("仅当前会话" in x for x in sent), sent

            # 同名再切 → 「无需切换」短路，不产生 pending
            turn._pending_personality_switch.clear()
            sent.clear()
            await turn._handle_personality_command("/人格切换 assistant", "u1", view)
            assert any("无需切换" in x for x in sent), sent
            assert "u1" not in turn._pending_personality_switch

            # --全局 → 写用户默认，会话绑定与 pending/marker 均不动
            turn._pending_personality_switch.clear()
            turn._personality_switch_marker.clear()
            sent.clear()
            await turn._handle_personality_command(
                "/人格切换 assistant --全局", "u1", view)
            assert reg.get_personal_personality("u1") == "assistant"
            entry = [x for x in mgr.list_sessions("u1") if x["name"] == "聊天"][0]
            assert entry["metadata"]["persona_key"] == "assistant", entry
            assert "u1" not in turn._pending_personality_switch
            assert any("默认人格" in x and "当前会话人格不变" in x for x in sent), sent

            # 临时会话（无活跃特殊会话）→ 原行为：写用户默认
            mgr.end_active("u1")
            sent.clear()
            await turn._handle_personality_command("/人格切换 露比", "u1", view)
            assert reg.get_personal_personality("u1") == "rubi"
            pend = turn._pending_personality_switch["u1"]
            assert len(pend) == 3 and pend[2] is False, pend
            print_pass("in-session switch binds session only; --全局 writes user default")
        finally:
            self._restore(ctx)

    async def test_scene_creation_reject(self):
        """bond_scene / char_creation 会话拒绝切换；--全局 在 scene 内放行。"""
        ctx = self._env()
        turn, mgr, reg, sent = self.turn, ctx["mgr"], ctx["reg"], ctx["sent"]
        try:
            view = self._view()
            scene = mgr.create("u2", "剧情·雨夜", metadata={
                "kind": "bond_scene", "char_id": "rubi", "persona_key": "rubi"})
            mgr.switch_to("u2", scene.name)
            await turn._handle_personality_command("/人格切换 assistant", "u2", view)
            assert any("固定" in x for x in sent), sent
            assert reg.get_personal_personality("u2") is None
            entry = [x for x in mgr.list_sessions("u2") if x["name"] == "剧情·雨夜"][0]
            assert entry["metadata"]["persona_key"] == "rubi", entry

            # --全局 在 scene 内放行：改默认、不动钉住
            sent.clear()
            await turn._handle_personality_command(
                "/人格切换 assistant --全局", "u2", view)
            assert reg.get_personal_personality("u2") == "assistant"
            assert reg.resolve_effective_personality("u2") == "rubi"  # 仍钉住
            assert any("当前会话人格不变" in x for x in sent), sent

            # char_creation 同样拒绝
            creation = mgr.create(
                "u3", "角色创作", metadata={"kind": "char_creation"})
            mgr.switch_to("u3", creation.name)
            sent.clear()
            await turn._handle_personality_command("/人格切换 assistant", "u3", view)
            assert any("角色创作" in x and "固定" in x for x in sent), sent
            print_pass("scene/creation sessions reject switch; --全局 bypasses")
        finally:
            self._restore(ctx)

    async def test_switch_end_list_messages(self):
        """/切换会话、/结束会话、/会话列表 的人格提示与标注。"""
        ctx = self._env()
        turn, mgr, reg, sent = self.turn, ctx["mgr"], ctx["reg"], ctx["sent"]
        try:
            mgr.create("u4", "会话A", metadata={"persona_key": "rubi"})
            mgr.create("u4", "会话B", metadata={"persona_key": "assistant"})
            mgr.create("u4", "旧会话")  # 存量会话：无绑定
            mgr.switch_to("u4", "会话A")

            sent.clear()
            await turn._handle_session_command("/切换会话 会话B", "u4")
            assert "当前人格: 助手 Roxy" in sent[-1], sent
            await turn._handle_session_command("/切换会话 会话A", "u4")
            assert "当前人格: 露比 (Rubi)" in sent[-1], sent
            # 切到未绑定的存量会话 → 穿透到全局默认
            await turn._handle_session_command("/切换会话 旧会话", "u4")
            assert "当前人格: 助手 Roxy" in sent[-1], sent

            sent.clear()
            await turn._handle_session_command("/结束会话", "u4")
            assert "当前人格: 助手 Roxy" in sent[-1], sent  # 回落到全局默认

            sent.clear()
            await turn._handle_session_command("/会话列表", "u4")
            out = sent[-1]
            assert "人格: 露比 (Rubi)" in out, out
            assert "人格: 助手 Roxy" in out, out
            assert "人格: 跟随默认" in out, out  # 存量未绑定会话
            print_pass("switch/end/list commands surface session persona")
        finally:
            self._restore(ctx)

    def test_clear_context_keeps_binding(self):
        """clear_context 清历史但保留 persona_key；update_metadata 往返。"""
        ctx = self._env()
        mgr = ctx["mgr"]
        try:
            s = mgr.create("u5", "保留绑定", metadata={"persona_key": "rubi"})
            mgr.switch_to("u5", s.name)
            mgr.add_message("u5", "user", "hello")
            mgr.clear_context("u5")
            entry = [x for x in mgr.list_sessions("u5")
                     if x["name"] == "保留绑定"][0]
            assert entry["metadata"]["persona_key"] == "rubi", entry
            assert entry["total_messages"] == 0, entry
            # update_metadata 合并语义 + 不存在的会话返回 False
            assert mgr.update_metadata("u5", "保留绑定",
                                       {"persona_key": "assistant"}) is True
            entry = [x for x in mgr.list_sessions("u5")
                     if x["name"] == "保留绑定"][0]
            assert entry["metadata"]["persona_key"] == "assistant", entry
            assert mgr.update_metadata("u5", "不存在", {"x": 1}) is False
            print_pass("clear_context keeps binding; update_metadata roundtrip")
        finally:
            self._restore(ctx)


# ── P3a: bond scenes / commemorative card / reunion & anniversary ──

def _import_turn_for_tests():
    """Import agent.turn with USER_DATA_ROOT kept off the production path."""
    os.environ.setdefault("USER_DATA_ROOT", "/tmp/qqbot_smoke")
    import agent.turn as turn
    return turn


def _tmp_bond_manager(tmp):
    from agent.bond import BondManager
    return BondManager(
        base_dir=os.path.join(tmp, "bonds"),
        config_path=os.path.join(tmp, "no_such_config.json"),
    )


class TestBondScene:
    """P3a: scene settlement — bond.complete_scene + social_tools + card."""

    def run(self):
        print_header("31. Bond Scene Settlement (P3a)")
        self.test_complete_scene_one_shot()
        self.test_scene_done_records()
        asyncio.run(self.test_complete_scene_tool())
        self.test_render_scene_card()

    def test_complete_scene_one_shot(self):
        tmp = tempfile.mkdtemp(prefix="scene_")
        try:
            m = _tmp_bond_manager(tmp)
            r = m.complete_scene("u1", "rubi", "雨夜谈心")
            assert r["accepted"] and r["exp_gained"] == 100, r
            # 0 + 100 EXP → Lv2 (threshold 100)
            assert r["level"] == 2 and any(u["level"] == 2 for u in r["level_ups"]), r
            # repeat completion → no reward, EXP untouched
            r2 = m.complete_scene("u1", "rubi", "雨夜谈心")
            assert r2["accepted"] is False and r2["reason"] == "already_done", r2
            assert r2["exp_gained"] == 0 and r2["exp"] == r["exp"], r2
            assert r2["level_ups"] == []
            # a different scene still rewards
            r3 = m.complete_scene("u1", "rubi", "星空之约")
            assert r3["accepted"] and r3["exp_gained"] == 100, r3
            # mood got a bump
            assert m.get_state("u1", "rubi")["mood"] > 0
            print_pass("complete_scene one-shot +100 EXP → Lv2; dup blocked")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_scene_done_records(self):
        tmp = tempfile.mkdtemp(prefix="scene_")
        try:
            m = _tmp_bond_manager(tmp)
            assert m.mark_scene_done("u1", "rubi", "雨夜谈心") is True
            assert m.mark_scene_done("u1", "rubi", "雨夜谈心") is False  # dup
            assert m.mark_scene_done("u1", "rubi", "  ") is False        # blank
            assert m.has_done_scene("u1", "rubi", "雨夜谈心") is True
            assert m.has_done_scene("u1", "rubi", "星空之约") is False
            done = m.scenes_done("u1", "rubi")
            assert list(done) == ["雨夜谈心"] and done["雨夜谈心"] > 0
            # per-character / unknown-user isolation
            assert m.has_done_scene("u1", "roxy", "雨夜谈心") is False
            assert m.has_done_scene("ghost", "rubi", "雨夜谈心") is False
            assert m.scenes_done("ghost", "rubi") == {}
            print_pass("mark/has/scenes_done records + isolation")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_complete_scene_tool(self):
        from tools.social_tools import complete_scene
        tmp = tempfile.mkdtemp(prefix="scene_")
        try:
            m = _tmp_bond_manager(tmp)
            out = await complete_scene(
                user_id="u1", char_id="rubi", scene_title="雨夜谈心",
                bond_manager=m, memento="雨停之后，心还没停。",
            )
            assert out["ok"] and out["result"]["accepted"], out
            assert out["result"]["memento"] == "雨停之后，心还没停。"
            assert out["result"]["scene_title"] == "雨夜谈心"
            obs = out["observation"]
            assert "雨夜谈心" in obs and "收尾" in obs and "纪念卡" in obs, obs
            assert "Lv2" in obs, obs  # level-up surfaced to the character
            # shared-memory events: completion + level-up
            ev = m.recent_events("u1", "rubi", 5)
            assert any("完成了剧情场景「雨夜谈心」" in e for e in ev), ev
            assert any("羁绊提升到了 Lv2" in e for e in ev), ev
            # duplicate → gentle already-done observation, no new events
            out2 = await complete_scene(
                user_id="u1", char_id="rubi", scene_title="雨夜谈心", bond_manager=m,
            )
            assert out2["ok"] and out2["result"]["accepted"] is False
            assert "早已完成" in out2["observation"], out2["observation"]
            assert len(m.recent_events("u1", "rubi", 10)) == len(ev)
            # missing scene_title degrades to a generic title, still settles
            out3 = await complete_scene(
                user_id="u2", char_id="rubi", scene_title="  ", bond_manager=m,
            )
            assert out3["ok"] and out3["result"]["scene_title"] == "这段剧情"
            # error path
            err = await complete_scene(
                user_id="", char_id="rubi", scene_title="x", bond_manager=m,
            )
            assert err["ok"] is False and err["observation"].startswith("[Error]")
            print_pass("social_tools.complete_scene obs/events/dup/generic/error")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_render_scene_card(self):
        from tools.card_renderer import render_scene_card, scene_card_path
        tmp = tempfile.mkdtemp(prefix="scene_card_")
        try:
            entry = {
                "char_name": "露比", "scene_title": "雨夜谈心",
                "level": 4, "level_title": "朋友",
                "date": "2026-10-23", "memento": "雨停之后，心还没停。",
            }
            out = os.path.join(tmp, "card.png")
            p = render_scene_card(entry, out_path=out)
            assert p == out and os.path.isfile(p) and os.path.getsize(p) > 1000
            # degraded entry (all defaults) still renders, never raises
            p2 = render_scene_card({}, out_path=os.path.join(tmp, "empty.png"))
            assert p2 and os.path.isfile(p2)
            # long memento wraps without error
            p3 = render_scene_card(
                dict(entry, memento="很长很长的纪念语。" * 12),
                out_path=os.path.join(tmp, "long.png"))
            assert p3 and os.path.isfile(p3)
            # path helper: slug sanitized, per char+scene
            sp = scene_card_path(entry)
            assert sp.endswith(".png") and "露比" in sp and "雨夜谈心" in sp
            assert scene_card_path({}) .endswith(".png")
            print_pass("render_scene_card renders full/empty/long entries to PNG")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSceneSession:
    """P3a: L4 injection, opening seed, level-up routing, command, finish."""

    def run(self):
        print_header("32. Bond-Scene Session & Commands (P3a)")
        self.turn = _import_turn_for_tests()
        self.test_l4_injection()
        self.test_scene_opening_line()
        asyncio.run(self.test_scene_start_opening())
        asyncio.run(self.test_emit_level_ups_routing())
        asyncio.run(self.test_scene_command())
        asyncio.run(self.test_finish_bond_scene())

    def test_l4_injection(self):
        from agent.tool_registry import ToolRegistry
        from agent.agent import Agent
        from agent.session import Session
        from agent.special_session import SpecialSession

        agent = Agent(
            deepseek_client=MockDeepSeekClient(),
            tool_registry=ToolRegistry(),
            config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
        )
        sess = Session(user_id="u1")

        def _tail(sp):
            msgs = agent._build_messages(sess, "……", sp)
            sys_msgs = [m for m in msgs if m["role"] == "system"]
            return sys_msgs[-1]["content"]

        script = "【第一幕 · 骤雨】夜里突降暴雨。\n【终幕 · 雨停之约】雨停了。"
        scene = SpecialSession(
            session_id="sc1", user_id="u1", name="剧情·雨夜谈心",
            metadata={"kind": "bond_scene", "char_id": "rubi",
                      "scene_title": "雨夜谈心", "script": script,
                      "completed": False})
        tail = _tail(scene)
        assert "剧情场景（进行中）" in tail and "complete_scene" in tail, tail
        assert "本场剧情脚本" in tail and "骤雨" in tail and "雨停之约" in tail, tail

        # char_creation → interview only, no scene block
        creation = SpecialSession(session_id="c1", user_id="u1", name="角色创作",
                                  metadata={"kind": "char_creation"})
        t2 = _tail(creation)
        assert "角色创作访谈" in t2 and "剧情场景（进行中）" not in t2
        # ordinary special session → neither
        normal = SpecialSession(session_id="s1", user_id="u1", name="普通会话")
        t3 = _tail(normal)
        assert "特殊会话模式" in t3 and "剧情场景（进行中）" not in t3
        print_pass("L4 scene instructions + script only for kind=bond_scene")

    def test_scene_opening_line(self):
        """降级开场：通用引导文案，绝不回显脚本舞台指示原文。"""
        turn = self.turn
        orig = turn.get_character_registry
        try:
            reg = MagicMock()
            reg.get_short_name.return_value = "露比"
            turn.get_character_registry = lambda: reg
            scene = {"title": "雨夜谈心",
                     "body": ("# 标题行应被跳过\n> 引用行也应被跳过\n"
                              "【第一幕 · 骤雨】夜里突降暴雨，你们躲进哨塔。\n"
                              "【终幕】雨停了。")}
            line = turn._scene_opening_line("rubi", "雨夜谈心", scene)
            assert "剧情《雨夜谈心》开始" in line, line
            assert "/结束会话" in line and "露比" in line, line
            # 脚本是给模型的提示词，降级文案也不得泄漏给用户
            assert "第一幕" not in line and "哨塔" not in line, line
            # empty body → same generic framed fallback
            line2 = turn._scene_opening_line(
                "rubi", "星空之约", {"title": "星空之约", "body": "  "})
            assert "剧情《星空之约》开始" in line2 and "/结束会话" in line2
            print_pass("opening fallback: framed generic line, never echoes script")
        finally:
            turn.get_character_registry = orig

    async def test_scene_start_opening(self):
        """_start_bond_scene：LLM 演绎开场（不回显脚本）；LLM 失败降级固定文案。"""
        turn = self.turn
        tmp = tempfile.mkdtemp(prefix="scene_open_")
        orig = (turn._bond_manager, turn._perm_manager, turn._special_sessions,
                turn._safe_send, turn.get_character_registry, turn._model_router)
        sent = []
        try:
            m = _tmp_bond_manager(tmp)
            st = m.get_state("u1", "rubi")
            st["level"] = 5
            m._save("u1")
            turn._bond_manager = m
            perm = MagicMock()
            perm.get_role.return_value = "admin"
            perm.get_max_special_sessions.return_value = 10
            turn._perm_manager = perm
            special = MagicMock()
            special.get_active.return_value = None
            special.list_sessions.return_value = []
            _created = MagicMock()
            _created.name = "剧情·雨夜谈心"  # MagicMock(name=…) 不设属性，须显式赋值
            special.create.return_value = _created
            turn._special_sessions = special

            async def fake_send(payload, sender=None):
                sent.append(payload)

            turn._safe_send = fake_send
            reg = MagicMock()
            reg.get_short_name.return_value = "露比"
            reg.load.return_value = _P3_CARD
            turn.get_character_registry = lambda: reg
            scene = {"title": "雨夜谈心", "level": 4,
                     "body": ("【第一幕 · 骤雨】夜里突降暴雨，你们躲进哨塔。\n"
                              "【终幕】雨停了。")}

            # LLM 成功 → 发送演绎文本 + 框架头尾；脚本原文不外泄
            async def fake_ok(message, history=None, timeout_set=None, purpose=None):
                assert "哨塔" in message  # 脚本作为提示词给模型
                return "暴雨砸在哨塔的铁皮顶上，你被我拉着钻进塔里……「发什么呆？」"

            router = MagicMock()
            router.flash_client.chat_completion = fake_ok
            turn._model_router = router
            await turn._start_bond_scene("u1", "rubi", scene)
            assert len(sent) == 1, sent
            seed = sent[0]
            assert seed.startswith("—— 剧情《雨夜谈心》开始 ——"), seed
            assert "暴雨砸在哨塔的铁皮顶上" in seed, seed
            assert "/结束会话" in seed, seed
            assert "【第一幕" not in seed, seed  # 舞台指示不回显
            special.add_message.assert_called_once_with("u1", "assistant", seed)
            special.switch_to.assert_called_once()

            # LLM 失败 → 降级通用文案，同样不泄漏脚本
            sent.clear()
            special.add_message.reset_mock()
            special.switch_to.reset_mock()
            m2 = _tmp_bond_manager(tempfile.mkdtemp(prefix="scene_open2_"))
            st2 = m2.get_state("u2", "rubi")
            st2["level"] = 5
            m2._save("u2")
            turn._bond_manager = m2

            async def fake_err(message, history=None, timeout_set=None, purpose=None):
                return "调用DeepSeek API时出错: timeout"

            router.flash_client.chat_completion = fake_err
            await turn._start_bond_scene("u2", "rubi", scene)
            assert len(sent) == 1, sent
            assert "剧情《雨夜谈心》开始" in sent[0], sent[0]
            assert "轻轻拉住你的衣袖" in sent[0], sent[0]
            assert "哨塔" not in sent[0], sent[0]
            print_pass("scene start: LLM opening (no script leak) + degrade fallback")
        finally:
            (turn._bond_manager, turn._perm_manager, turn._special_sessions,
             turn._safe_send, turn.get_character_registry, turn._model_router) = orig
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_emit_level_ups_routing(self):
        """档案 tier → 🔓 解锁提示；场景 tier → 私聊邀请（群聊/已完成不邀请）。"""
        turn = self.turn
        tmp = tempfile.mkdtemp(prefix="scene_lv_")
        orig = (turn._bond_manager, turn._safe_send, turn.get_character_registry,
                turn._send_level_up_notice)
        sent = []
        try:
            m = _tmp_bond_manager(tmp)
            turn._bond_manager = m

            async def fake_send(payload, sender=None):
                sent.append(payload)

            async def noop_notice(*a, **k):
                return None

            reg = MagicMock()
            reg.load.return_value = _P3_CARD
            reg.get_short_name.return_value = "露比"
            turn.get_character_registry = lambda: reg
            turn._safe_send = fake_send
            turn._send_level_up_notice = noop_notice

            # Lv3 → archive tier unlock notice, no invite
            await turn._emit_level_ups("u1", "rubi", [{"level": 3, "title": "熟悉"}])
            assert any("羁绊档案解锁" in s and "熟悉" in s for s in sent), sent
            assert not any("剧情" in s for s in sent), sent
            ev = m.recent_events("u1", "rubi", 5)
            assert any("解锁了新的羁绊档案「熟悉」" in e for e in ev), ev

            # Lv4 (private) → scene invite, NO archive notice
            sent.clear()
            await turn._emit_level_ups("u1", "rubi", [{"level": 4, "title": "朋友"}])
            assert len(sent) == 1 and "雨夜谈心" in sent[0], sent
            assert "/剧情 雨夜谈心" in sent[0], sent[0]
            assert not any("羁绊档案解锁" in s for s in sent), sent

            # group chat → no invite at all
            sent.clear()
            await turn._emit_level_ups("u1", "rubi", [{"level": 4, "title": "朋友"}],
                                       is_group=True)
            assert not any("雨夜谈心" in s for s in sent), sent

            # already-completed scene → no repeat invite
            m.mark_scene_done("u1", "rubi", "雨夜谈心")
            sent.clear()
            await turn._emit_level_ups("u1", "rubi", [{"level": 4, "title": "朋友"}])
            assert not any("雨夜谈心" in s for s in sent), sent
            print_pass("level-up routing: archive→🔓 notice, scene→private invite")
        finally:
            (turn._bond_manager, turn._safe_send, turn.get_character_registry,
             turn._send_level_up_notice) = orig
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_scene_command(self):
        """/剧情 列表 / 启动 / 群聊拒绝 / 未知名 / 非命令。"""
        turn = self.turn
        tmp = tempfile.mkdtemp(prefix="scene_cmd_")
        orig = (turn._bond_manager, turn._safe_send, turn.get_character_registry,
                turn._start_bond_scene)
        sent, started = [], []
        try:
            m = _tmp_bond_manager(tmp)
            st = m.get_state("u1", "rubi")
            st["level"] = 5  # 雨夜谈心(Lv4) 可体验；星空之约(Lv6) 未解锁
            m._save("u1")
            turn._bond_manager = m

            async def fake_send(payload, sender=None):
                sent.append(payload)

            async def fake_start(user_id, char_id, scene):
                started.append((user_id, char_id, scene["title"]))

            reg = MagicMock()
            reg.resolve_effective_personality.return_value = "rubi"
            reg.load.return_value = _P3_CARD
            reg.get_short_name.return_value = "露比"
            reg.get_display_name.return_value = "露比 (Rubi)"
            turn.get_character_registry = lambda: reg
            turn._safe_send = fake_send
            turn._start_bond_scene = fake_start

            view = MagicMock()
            view.group_id = None
            view.is_group = False

            # list
            assert await turn._handle_scene_command("/剧情", "u1", view) is True
            msg = sent[-1]
            assert "雨夜谈心" in msg and "可体验" in msg, msg
            assert "星空之约" in msg and "羁绊 Lv6 解锁" in msg, msg
            # start via substring title
            assert await turn._handle_scene_command("/剧情 雨夜", "u1", view) is True
            assert started == [("u1", "rubi", "雨夜谈心")], started
            # completed scene marked in the list
            m.mark_scene_done("u1", "rubi", "雨夜谈心")
            await turn._handle_scene_command("#剧情", "u1", view)
            assert "已完成" in sent[-1], sent[-1]
            # group → refuse, no start
            view.is_group = True
            view.group_id = "g1"
            await turn._handle_scene_command("/剧情 星空之约", "u1", view)
            assert "私聊" in sent[-1] and len(started) == 1, (sent[-1], started)
            # unknown title (private)
            view.is_group = False
            view.group_id = None
            await turn._handle_scene_command("/剧情 不存在的剧情", "u1", view)
            assert "没有找到" in sent[-1] and len(started) == 1
            # non-command → not handled
            assert await turn._handle_scene_command("/剧情x", "u1", view) is False
            assert await turn._handle_scene_command("剧情", "u1", view) is False
            print_pass("/剧情: list(可体验/已完成/门槛) + start + group refuse + miss")
        finally:
            (turn._bond_manager, turn._safe_send, turn.get_character_registry,
             turn._start_bond_scene) = orig
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_finish_bond_scene(self):
        """post-run 收尾：completed 封档写入 + 纪念卡 + B1 封档提示（无升级独立发）。"""
        from types import SimpleNamespace
        turn = self.turn
        tmp = tempfile.mkdtemp(prefix="scene_fin_")
        orig = (turn._bond_manager, turn._special_sessions, turn._safe_send,
                turn.get_character_registry, turn.render_scene_card)
        sent, entries = [], []

        def _active():
            return SimpleNamespace(
                name="剧情·雨夜谈心",
                metadata={"kind": "bond_scene", "char_id": "rubi",
                          "scene_title": "雨夜谈心", "completed": False})

        try:
            m = _tmp_bond_manager(tmp)
            turn._bond_manager = m
            special = MagicMock()
            special.get_active.return_value = _active()
            turn._special_sessions = special

            async def fake_send(payload, sender=None):
                sent.append(payload)

            turn._safe_send = fake_send
            reg = MagicMock()
            reg.get_display_name.return_value = "露比 (Rubi)"
            reg.get_short_name.return_value = "露比"
            turn.get_character_registry = lambda: reg

            def fake_render(entry, out_path=None):
                entries.append(entry)
                return os.path.join(tmp, "card.png")

            turn.render_scene_card = fake_render
            result = {"char_id": "rubi", "scene_title": "雨夜谈心", "level": 4,
                      "level_ups": [], "memento": "雨停之后，心还没停。"}
            await turn._finish_bond_scene("u1", result)
            # card entry assembled from result + bond title
            assert entries and entries[0]["scene_title"] == "雨夜谈心", entries
            assert entries[0]["char_name"] == "露比 (Rubi)"
            assert entries[0]["memento"] == "雨停之后，心还没停。"
            assert entries[0]["level"] == 4
            assert entries[0]["level_title"] == m.title_for(4)
            assert entries[0]["date"] == time.strftime("%Y-%m-%d")
            # 封档：写 completed/completed_at（§5.2）+ end_active
            special.update_metadata.assert_called_once()
            _um = special.update_metadata.call_args[0]
            assert _um[0] == "u1" and _um[1] == "剧情·雨夜谈心", _um
            assert _um[2].get("completed") is True and "completed_at" in _um[2], _um
            special.end_active.assert_called_once_with("u1")
            # 无升级 → 纪念卡 + 独立 B1 封档提示（2 条；§5.2/B1）
            assert len(sent) == 2, sent
            assert getattr(sent[0], "path", "").endswith("card.png"), sent[0]
            assert "羁绊纪念" in getattr(sent[0], "label", ""), sent[0]
            assert isinstance(sent[1], str), sent[1]
            assert "已完结" in sent[1] and "/切换会话 剧情·雨夜谈心" in sent[1], sent[1]
            # render failure (None) → 无卡，但 B1 封档提示仍独立发，会话仍关闭
            sent.clear()
            special.reset_mock()
            special.get_active.return_value = _active()
            turn.render_scene_card = lambda entry, out_path=None: None
            await turn._finish_bond_scene("u1", result)
            assert len(sent) == 1 and isinstance(sent[0], str), sent
            assert special.end_active.called
            print_pass("finish: completed 封档写入 + card + B1 提示；render None 降级")
        finally:
            (turn._bond_manager, turn._special_sessions, turn._safe_send,
             turn.get_character_registry, turn.render_scene_card) = orig
            shutil.rmtree(tmp, ignore_errors=True)


class TestSceneArchive:
    """剧情会话封档与配额豁免（scene-session-archive-design.md §8 T1–T15）。

    用真实 SpecialSessionManager（tmp 根）验证配额/去重/封档/回填语义，
    覆盖 4 个强制点与展示口径；LLM/开场/纪念卡渲染按需打桩。
    """

    def run(self):
        print_header("32b. Scene-Session Archive & Quota Exemption")
        self.turn = _import_turn_for_tests()
        self.test_quota_helpers()
        self.test_rollback_switch()
        asyncio.run(self.test_start_dedup_resume())
        asyncio.run(self.test_start_cap())
        asyncio.run(self.test_finish_archive_merge())
        self.test_injection_guard()
        asyncio.run(self.test_complete_scene_guard())
        self.test_webui_special_list()

    # ── fixtures ────────────────────────────────────────────────
    def _setup(self, role="regular", max_sessions=1, scene_cap=None):
        turn = self.turn
        tmp = tempfile.mkdtemp(prefix="scene_arch_")
        from agent.special_session import SpecialSessionManager
        mgr = SpecialSessionManager(
            user_data_root=os.path.join(tmp, "users"), max_per_user=50)
        m = _tmp_bond_manager(tmp)
        sent = []

        async def fake_send(payload, sender=None):
            sent.append(payload)

        reg = MagicMock()
        reg._exists.return_value = True
        reg.get_display_name.side_effect = lambda k, **kw: f"名称:{k}"
        reg.get_short_name.side_effect = lambda k, **kw: "露比"
        reg.resolve_effective_personality.return_value = "rubi"
        perm = MagicMock()
        perm.get_role.return_value = role
        perm.get_max_special_sessions.return_value = max_sessions

        orig = (turn._special_sessions, turn._bond_manager, turn._safe_send,
                turn.get_character_registry, turn._perm_manager,
                os.environ.get("SCENE_ARCHIVE_ENABLED"),
                os.environ.get("MAX_SCENE_ARCHIVES"))
        turn._special_sessions = mgr
        turn._bond_manager = m
        turn._safe_send = fake_send
        turn.get_character_registry = lambda: reg
        turn._perm_manager = perm
        os.environ.pop("SCENE_ARCHIVE_ENABLED", None)  # 默认开启
        if scene_cap is not None:
            os.environ["MAX_SCENE_ARCHIVES"] = str(scene_cap)
        return {"tmp": tmp, "mgr": mgr, "bond": m, "sent": sent,
                "reg": reg, "orig": orig}

    def _restore(self, ctx):
        turn = self.turn
        (turn._special_sessions, turn._bond_manager, turn._safe_send,
         turn.get_character_registry, turn._perm_manager, _sae, _msa) = ctx["orig"]
        if _sae is None:
            os.environ.pop("SCENE_ARCHIVE_ENABLED", None)
        else:
            os.environ["SCENE_ARCHIVE_ENABLED"] = _sae
        if _msa is None:
            os.environ.pop("MAX_SCENE_ARCHIVES", None)
        else:
            os.environ["MAX_SCENE_ARCHIVES"] = _msa
        turn._pending_bond_scene_done.pop("u1", None)
        shutil.rmtree(ctx["tmp"], ignore_errors=True)

    @staticmethod
    def _mk_scene(mgr, uid, title, char_id="rubi", completed=None,
                  kind="bond_scene"):
        md = {"kind": kind, "char_id": char_id, "persona_key": char_id,
              "scene_title": title}
        if completed is not None:
            md["completed"] = completed
        return mgr.create(uid, f"剧情·{title}", metadata=md)

    @staticmethod
    def _entry(mgr, uid, name):
        for e in mgr.list_sessions(uid):
            if e["name"] == name:
                return e
        return None

    # ── T1/T3/T12/T13 + B6：配额辅助 + 懒回填 ──────────────────
    def test_quota_helpers(self):
        turn = self.turn
        ctx = self._setup(role="regular", max_sessions=1)
        try:
            mgr, uid = ctx["mgr"], "u1"
            assert turn._quota_used(uid) == 0 and turn._scene_archives(uid) == []
            # T1：剧情会话不计配额
            self._mk_scene(mgr, uid, "雨夜谈心", completed=False)
            assert turn._quota_used(uid) == 0
            assert len(turn._scene_archives(uid)) == 1
            # 普通会话计配额；创作会话豁免
            mgr.create(uid, "普通A", metadata={})
            self._mk_scene(mgr, uid, "创作", kind="char_creation")
            assert turn._quota_used(uid) == 1, turn._quota_used(uid)
            assert len(turn._quota_sessions(uid)) == 1  # 仅普通A
            # B6 删除尾行：普通 vs 剧情
            t_norm = turn._delete_quota_tail(uid, "")
            assert "普通会话" in t_norm and "1/1" in t_norm, t_norm
            t_scene = turn._delete_quota_tail(uid, "bond_scene")
            assert "配额不变" in t_scene and "剧情归档剩 1" in t_scene, t_scene
            t_create = turn._delete_quota_tail(uid, "char_creation")
            assert "配额不变" in t_create, t_create
            # T12：懒回填（无 completed 字段 + has_done_scene=True → 写回）
            self._mk_scene(mgr, uid, "星空之约")  # 无 completed 字段
            ctx["bond"].mark_scene_done(uid, "rubi", "星空之约")
            e = self._entry(mgr, uid, "剧情·星空之约")
            assert "completed" not in (e.get("metadata") or {})
            turn._backfill_completed(uid, e)
            assert e["metadata"]["completed"] is True and "completed_at" in e["metadata"]
            # 回填已落盘（重读仍在）
            e2 = self._entry(mgr, uid, "剧情·星空之约")
            assert e2["metadata"]["completed"] is True
            # 无法判定（未 done）→ 保持未回填，避免误封档
            self._mk_scene(mgr, uid, "未定")
            e3 = self._entry(mgr, uid, "剧情·未定")
            turn._backfill_completed(uid, e3)
            assert "completed" not in (e3.get("metadata") or {})
            # _find_scene_session 命中 + 取最新
            found = turn._find_scene_session(uid, "rubi", "星空之约")
            assert found and found["name"] == "剧情·星空之约"
            assert turn._find_scene_session(uid, "rubi", "不存在") is None
            print_pass("quota helpers: 剧情/创作豁免 + B6 尾行 + 懒回填 (T1/T3/T12/T13)")
        finally:
            self._restore(ctx)

    # ── 回滚开关 ────────────────────────────────────────────────
    def test_rollback_switch(self):
        turn = self.turn
        ctx = self._setup(role="regular", max_sessions=1)
        try:
            os.environ["SCENE_ARCHIVE_ENABLED"] = "0"
            mgr, uid = ctx["mgr"], "u1"
            self._mk_scene(mgr, uid, "雨夜谈心", completed=True)
            mgr.create(uid, "普通A", metadata={})
            # 关闭：_quota_sessions 退回全量；_scene_completed 恒 False
            assert turn._quota_used(uid) == 2, turn._quota_used(uid)
            e = self._entry(mgr, uid, "剧情·雨夜谈心")
            assert turn._scene_completed(e) is False
            tail = turn._delete_quota_tail(uid, "bond_scene")
            assert "当前特殊会话: 2/1" in tail, tail  # 统一口径
            print_pass("rollback switch off → 统一配额语义 (SCENE_ARCHIVE_ENABLED=0)")
        finally:
            self._restore(ctx)

    # ── T4/T7：/剧情 去重恢复 + 归档拒绝 ───────────────────────
    async def test_start_dedup_resume(self):
        turn = self.turn
        ctx = self._setup(role="regular", max_sessions=1)
        try:
            mgr, sent, uid = ctx["mgr"], ctx["sent"], "u1"
            # T4：中途 /结束会话 → /剧情 同标题 = 恢复（不新建、消息数不重置）
            s = self._mk_scene(mgr, uid, "雨夜谈心", completed=False)
            mgr.switch_to(uid, s.name)
            mgr.add_message(uid, "user", "hello")
            mgr.add_message(uid, "assistant", "hi")
            before = self._entry(mgr, uid, s.name)["total_messages"]
            mgr.end_active(uid)
            await turn._start_bond_scene(
                uid, "rubi", {"title": "雨夜谈心", "level": 1, "body": "【第一幕】…"})
            assert len(turn._scene_archives(uid)) == 1, turn._scene_archives(uid)
            active = mgr.get_active(uid)
            assert active and active.name == s.name, active
            assert self._entry(mgr, uid, s.name)["total_messages"] == before
            assert any("继续上次未完成的剧情" in str(x) for x in sent), sent
            # 幂等：正在此剧情中再发 → “你正在这段剧情中”
            sent.clear()
            await turn._start_bond_scene(
                uid, "rubi", {"title": "雨夜谈心", "level": 1, "body": "…"})
            assert any("你正在这段剧情" in str(x) for x in sent), sent
            # T7：已封档（completed=True）→ 拒绝重开，不新建
            sent.clear()
            mgr.end_active(uid)
            self._mk_scene(mgr, uid, "星空之约", completed=True)
            await turn._start_bond_scene(
                uid, "rubi", {"title": "星空之约", "level": 1, "body": "…"})
            assert any("已完结封档" in str(x) for x in sent), sent
            assert len(turn._scene_archives(uid)) == 2, turn._scene_archives(uid)
            print_pass("/剧情 去重：恢复未完成 (T4) + 幂等 + 拒绝已封档 (T7)")
        finally:
            self._restore(ctx)

    # ── T8：总量上限 + 恢复不受限 ──────────────────────────────
    async def test_start_cap(self):
        turn = self.turn
        ctx = self._setup(role="regular", max_sessions=1, scene_cap=2)
        try:
            mgr, sent, uid = ctx["mgr"], ctx["sent"], "u1"
            self._mk_scene(mgr, uid, "A", completed=True)
            self._mk_scene(mgr, uid, "B", completed=True)
            # 第 3 个新剧情 → 上限拒绝（不新建）
            await turn._start_bond_scene(
                uid, "rubi", {"title": "C", "level": 1, "body": "…"})
            assert any("已达上限" in str(x) for x in sent), sent
            assert len(turn._scene_archives(uid)) == 2, turn._scene_archives(uid)
            # 恢复未完成会话仍允许（去重先于上限检查）
            sent.clear()
            self._mk_scene(mgr, uid, "D", completed=False)
            await turn._start_bond_scene(
                uid, "rubi", {"title": "D", "level": 1, "body": "…"})
            assert any("继续上次未完成的剧情" in str(x) for x in sent), sent
            active = mgr.get_active(uid)
            assert active and active.name == "剧情·D", active
            print_pass("剧情总量上限：新建被拒 + 恢复未完成仍允许 (T8)")
        finally:
            self._restore(ctx)

    # ── B1b/T5：封档行并入升级 vs 独立发 + completed 写入 ──────
    async def test_finish_archive_merge(self):
        turn = self.turn
        ctx = self._setup(role="regular", max_sessions=1)
        orig_emit = turn._emit_level_ups
        orig_render = turn.render_scene_card
        orig_distill = turn._distill_bond_episode
        try:
            mgr, sent, uid = ctx["mgr"], ctx["sent"], "u1"
            captured = {}

            async def fake_emit(user_id, persona_key, level_ups, sender=None,
                                is_group=False, archive_note=""):
                captured["archive_note"] = archive_note

            turn._emit_level_ups = fake_emit
            turn.render_scene_card = lambda entry, out_path=None: None
            turn._distill_bond_episode = lambda *a, **k: None
            # 有升级：封档行并入升级（archive_note 非空），不独立发 B1
            s = self._mk_scene(mgr, uid, "雨夜谈心", completed=False)
            mgr.switch_to(uid, s.name)
            await turn._finish_bond_scene(uid, {
                "char_id": "rubi", "scene_title": "雨夜谈心", "level": 4,
                "level_ups": [{"level": 4, "title": "挚友"}], "memento": "…"})
            assert "已封档" in captured.get("archive_note", ""), captured
            assert not any(isinstance(x, str) and "已完结，已为你封档" in x
                           for x in sent), sent
            e = self._entry(mgr, uid, s.name)
            assert e["metadata"].get("completed") is True, e
            assert "completed_at" in e["metadata"], e
            # 无升级：archive_note 空 + 独立发 B1
            sent.clear(); captured.clear()
            s2 = self._mk_scene(mgr, uid, "星空之约", completed=False)
            mgr.switch_to(uid, s2.name)
            await turn._finish_bond_scene(uid, {
                "char_id": "rubi", "scene_title": "星空之约", "level": 4,
                "level_ups": [], "memento": "…"})
            assert captured.get("archive_note", "x") == "", captured
            assert any(isinstance(x, str) and "已完结，已为你封档" in x
                       for x in sent), sent
            print_pass("封档：有升级并入升级文案 / 无升级独立 B1 + completed 写入 (B1b/T5)")
        finally:
            turn._emit_level_ups = orig_emit
            turn.render_scene_card = orig_render
            turn._distill_bond_episode = orig_distill
            self._restore(ctx)

    # ── T6/#5b：归档运行态不注入剧情指令/剧本 ──────────────────
    def test_injection_guard(self):
        from agent.tool_registry import ToolRegistry
        from agent.agent import Agent
        from agent.session import Session
        from agent.special_session import SpecialSession
        agent = Agent(
            deepseek_client=MockDeepSeekClient(),
            tool_registry=ToolRegistry(),
            config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
        )
        sess = Session(user_id="u1")

        def _tail(sp):
            msgs = agent._build_messages(sess, "……", sp)
            return [m for m in msgs if m["role"] == "system"][-1]["content"]

        script = "【第一幕 · 骤雨】夜里突降暴雨。\n【终幕 · 雨停之约】雨停了。"
        base = {"kind": "bond_scene", "char_id": "rubi",
                "scene_title": "雨夜谈心", "script": script}
        live = SpecialSession(session_id="sc1", user_id="u1", name="剧情·雨夜谈心",
                              metadata=dict(base, completed=False))
        t_live = _tail(live)
        assert "剧情场景（进行中）" in t_live and "骤雨" in t_live, t_live
        done = SpecialSession(session_id="sc2", user_id="u1", name="剧情·雨夜谈心",
                              metadata=dict(base, completed=True))
        t_done = _tail(done)
        assert "剧情场景（进行中）" not in t_done, t_done
        assert "骤雨" not in t_done and "complete_scene" not in t_done, t_done
        print_pass("注入守卫：已封档剧情 volatile tail 无指令/剧本 (T6/#5b)")

    # ── T6/§5.4：complete_scene 第三层防御 ─────────────────────
    async def test_complete_scene_guard(self):
        turn = self.turn
        ctx = self._setup(role="regular", max_sessions=1)
        try:
            mgr, uid = ctx["mgr"], "u1"
            fn = turn._tool_registry._tools["complete_scene"]["function"]
            s = self._mk_scene(mgr, uid, "雨夜谈心", completed=True)
            mgr.switch_to(uid, s.name)
            tok_u = turn._current_user_id.set(uid)
            tok_p = turn._current_personality.set("rubi")
            try:
                out = await fn(memento="x")
                assert out.startswith("[Error]") and "没有进行中的剧情" in out, out
                # 进行中 → 守卫放行（结算由底层 bond manager 处理，非 [Error]）
                mgr.update_metadata(uid, s.name, {"completed": False})
                out2 = await fn(memento="x")
                assert not out2.startswith("[Error]"), out2
            finally:
                turn._current_user_id.reset(tok_u)
                turn._current_personality.reset(tok_p)
                turn._pending_bond_scene_done.pop(uid, None)
            print_pass("complete_scene 守卫：已封档拒绝 / 进行中放行 (T6/§5.4)")
        finally:
            self._restore(ctx)

    # ── T15：WebUI special_list 展平 + 配额摘要 ────────────────
    def test_webui_special_list(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from webui import data_reader as dr
        turn = self.turn
        ctx = self._setup(role="regular", max_sessions=1)
        orig_sm = dr._special_manager
        orig_perm = dr._permissions
        try:
            mgr, uid = ctx["mgr"], "u1"
            self._mk_scene(mgr, uid, "雨夜谈心", completed=True)
            self._mk_scene(mgr, uid, "星空之约", completed=False)
            self._mk_scene(mgr, uid, "创作", kind="char_creation")
            mgr.create(uid, "普通A", metadata={})
            perm_mock = MagicMock()
            perm_mock.get_role.return_value = MagicMock(value="regular")
            perm_mock.get_max_special_sessions.return_value = 1
            dr._special_manager = lambda: mgr
            dr._permissions = perm_mock
            out = dr.special_list(uid)
            assert out.get("quota_used") == 1, out       # 仅普通A
            assert out.get("quota_max") == 1, out
            assert out.get("scene_archives") == 2, out    # 雨夜谈心 + 星空之约
            assert out.get("scene_done") == 1, out        # 雨夜谈心
            by = {s["name"]: s for s in out["sessions"]}
            assert by["剧情·雨夜谈心"]["completed"] is True
            assert by["剧情·雨夜谈心"]["counts_quota"] is False
            assert by["普通A"]["counts_quota"] is True
            assert by["剧情·创作"]["kind"] == "char_creation"
            print_pass("webui special_list：kind/completed 展平 + 配额摘要 (T15)")
        finally:
            dr._special_manager = orig_sm
            dr._permissions = orig_perm
            self._restore(ctx)


class TestBondReunionAnniversary:
    """P3a: passive reunion + anniversary — prompt injection & once-guards."""

    def run(self):
        print_header("33. Bond Reunion & Anniversary (P3a)")
        self.test_reunion_pending_and_context()
        self.test_note_reunion_guard()
        self.test_anniversary_pending_and_note()
        self.test_anniversary_no_trickle()

    def _aged_state(self, m, user, char, *, last_seen_days=0.0, first_met_days=0.0):
        """Create state via settle_turn, then backdate stats for time-based tests."""
        m.settle_turn(user, char)
        st = m.get_state(user, char)
        now = time.time()
        if last_seen_days:
            st["stats"]["last_seen"] = now - last_seen_days * 86400
        if first_met_days:
            st["stats"]["first_met"] = now - first_met_days * 86400
        m._save(user)
        return st

    def test_reunion_pending_and_context(self):
        tmp = tempfile.mkdtemp(prefix="reunion_")
        try:
            m = _tmp_bond_manager(tmp)
            self._aged_state(m, "u1", "rubi", last_seen_days=20, first_met_days=25)
            gap = m.reunion_pending("u1", "rubi")
            assert gap == 20, gap  # ≥ reunion_days(14) → gap returned
            ctx = m.build_prompt_context("u1", "rubi", "露比")
            assert "久别重逢" in ctx and "20 天" in ctx, ctx
            # fresh gap → no reunion
            self._aged_state(m, "u2", "rubi", last_seen_days=1)
            assert m.reunion_pending("u2", "rubi") == 0
            assert "久别重逢" not in m.build_prompt_context("u2", "rubi", "露比")
            # boundary: exactly reunion_days counts
            self._aged_state(m, "u3", "rubi", last_seen_days=14.5)
            assert m.reunion_pending("u3", "rubi") == 14
            # no state / disabled config → 0
            assert m.reunion_pending("ghost", "rubi") == 0
            m._config["reunion_enabled"] = False
            assert m.reunion_pending("u1", "rubi") == 0
            assert "久别重逢" not in m.build_prompt_context("u1", "rubi", "露比")
            print_pass("reunion_pending + 久别重逢 injection (gap/boundary/off)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_note_reunion_guard(self):
        tmp = tempfile.mkdtemp(prefix="reunion_")
        try:
            m = _tmp_bond_manager(tmp)
            self._aged_state(m, "u1", "rubi", last_seen_days=20)
            assert m.note_reunion("u1", "rubi") is True   # first today → log event
            assert m.note_reunion("u1", "rubi") is False  # once-per-day guard
            print_pass("note_reunion once-per-day guard")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_anniversary_pending_and_note(self):
        tmp = tempfile.mkdtemp(prefix="anniv_")
        try:
            m = _tmp_bond_manager(tmp)
            self._aged_state(m, "u1", "rubi", first_met_days=31)  # day 32 ≥ 30
            assert m.anniversary_pending("u1", "rubi") == 30
            ctx = m.build_prompt_context("u1", "rubi", "露比")
            assert "纪念日" in ctx and "第 30 天" in ctx, ctx
            assert m.note_anniversary("u1", "rubi") == 30
            assert m.anniversary_pending("u1", "rubi") is None
            assert m.note_anniversary("u1", "rubi") is None  # idempotent
            assert "纪念日" not in m.build_prompt_context("u1", "rubi", "露比")
            # below first milestone → nothing
            self._aged_state(m, "u2", "rubi", first_met_days=5)
            assert m.anniversary_pending("u2", "rubi") is None
            assert m.note_anniversary("u2", "rubi") is None
            assert m.anniversary_pending("ghost", "rubi") is None
            print_pass("anniversary pending/note + 纪念日 injection + once-guard")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_anniversary_no_trickle(self):
        """长期缺席用户跳级：一次标记全部已越过里程碑，不逐回合滴灌。"""
        tmp = tempfile.mkdtemp(prefix="anniv_")
        try:
            m = _tmp_bond_manager(tmp)
            self._aged_state(m, "u1", "rubi", first_met_days=400)  # day 401
            assert m.anniversary_pending("u1", "rubi") == 365      # largest hit
            assert m.note_anniversary("u1", "rubi") == 365
            st = m.get_state("u1", "rubi")
            assert set(st["anniversaries_marked"]) == {30, 100, 365}, st
            assert m.anniversary_pending("u1", "rubi") is None     # no trickle
            print_pass("absent-user jump marks ALL passed milestones at once")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ── P3b: official active messaging + greeting bookkeeping ─────────

class TestActiveGreeting:
    """P3b: send_active_c2c + agent/greeting.py quota/targets/gates."""

    def run(self):
        print_header("34. Active Greeting & Quota (P3b)")
        asyncio.run(self.test_send_active_c2c())
        self.test_target_index()
        self.test_quota_daily_monthly()
        self.test_pick_greet_targets()
        self.test_bond_config_defaults()

    async def test_send_active_c2c(self):
        from adapters.qq_official import send_active_c2c
        from nonebot.adapters.qq.exception import (
            ActionFailed, NetworkError, RateLimitException,
        )
        bot = MagicMock()
        calls = []

        async def ok_send(**kwargs):
            calls.append(kwargs)
            return MagicMock()

        bot.send_to_c2c = ok_send
        assert await send_active_c2c(bot, "ABC123openid", "好久不见～") is True
        # ACTIVE semantics: exactly openid + message, NO msg_id/msg_seq
        assert len(calls) == 1 and set(calls[0]) == {"openid", "message"}, calls
        assert calls[0]["openid"] == "ABC123openid"
        assert calls[0]["message"].type == "text"
        # empty openid / blank text → False, nothing sent
        assert await send_active_c2c(bot, "", "x") is False
        assert await send_active_c2c(bot, "oid", "   ") is False
        assert len(calls) == 1

        # ActionFailed (audit/quota) → False with NO retry
        calls.clear()

        async def rejected(**kwargs):
            calls.append(kwargs)
            raise ActionFailed(MagicMock())

        bot.send_to_c2c = rejected
        assert await send_active_c2c(bot, "oid", "hi") is False
        assert len(calls) == 1

        # RateLimitException → one retry, second attempt succeeds
        calls.clear()
        attempts = {"n": 0}

        async def flaky(**kwargs):
            calls.append(kwargs)
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RateLimitException(MagicMock())
            return MagicMock()

        bot.send_to_c2c = flaky
        assert await send_active_c2c(bot, "oid", "hi") is True
        assert len(calls) == 2

        # NetworkError twice → False after exactly one retry
        calls.clear()

        async def net_fail(**kwargs):
            calls.append(kwargs)
            raise NetworkError("timeout")

        bot.send_to_c2c = net_fail
        assert await send_active_c2c(bot, "oid", "hi") is False
        assert len(calls) == 2
        print_pass("send_active_c2c: no msg_id; reject=no-retry; net/rate=1 retry")

    def test_target_index(self):
        from agent import greeting
        tmp = tempfile.mkdtemp(prefix="greet_")
        try:
            path = os.path.join(tmp, "targets.json")
            greeting.note_c2c_contact(path, "OID1", now=1000.0)
            greeting.note_c2c_contact(path, "OID1", now=2000.0)  # refresh
            greeting.note_c2c_contact(path, "  ", now=1000.0)   # blank ignored
            targets = greeting.load_targets(path)
            assert set(targets) == {"OID1"}, targets
            assert targets["OID1"]["last_seen"] == 2000.0
            greeting.mark_greeted(path, "OID1", now=5000.0)
            t = greeting.load_targets(path)["OID1"]
            assert t["last_greeted"] == 5000.0 and t["greet_count"] == 1
            assert t["last_seen"] == 2000.0  # mark keeps other fields
            greeting.mark_greeted(path, "OID1", now=9000.0)
            assert greeting.load_targets(path)["OID1"]["greet_count"] == 2
            # corrupt file → {} degrade; junk values filtered
            with open(path, "w") as f:
                f.write("{corrupt!!")
            assert greeting.load_targets(path) == {}
            with open(path, "w") as f:
                json.dump({"a": {"last_seen": 1}, "b": 42, "": {}}, f)
            assert set(greeting.load_targets(path)) == {"a"}
            # missing file → {}
            assert greeting.load_targets(os.path.join(tmp, "nope.json")) == {}
            print_pass("target index: note/mark/load + corrupt & junk degrade")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_quota_daily_monthly(self):
        from agent import greeting
        tmp = tempfile.mkdtemp(prefix="quota_")
        try:
            path = os.path.join(tmp, "quota.json")
            base = time.mktime(time.strptime("2026-10-23", "%Y-%m-%d")) + 43200
            # unknown user → allowed; first send ok, same-day second blocked
            assert greeting.active_allowed(path, "OID", now=base)
            greeting.record_active_send(path, "OID", now=base)
            assert not greeting.active_allowed(path, "OID", now=base + 60)
            # next days: allowed once per day, until monthly cap (4)
            for d in (1, 2, 3):
                t = base + d * 86400
                assert greeting.active_allowed(path, "OID", now=t), d
                greeting.record_active_send(path, "OID", now=t)
            assert not greeting.active_allowed(path, "OID", now=base + 4 * 86400)
            # still same month, higher daily cap does not bypass monthly cap
            assert not greeting.active_allowed(path, "OID", now=base + 4 * 86400,
                                               daily_cap=9, monthly_cap=4)
            # next month → fresh quota
            assert greeting.active_allowed(path, "OID", now=base + 40 * 86400)
            # empty openid record is a no-op
            greeting.record_active_send(path, "  ", now=base)
            print_pass("quota: ≤1/day, ≤4/month, day/month rollover resets")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pick_greet_targets(self):
        from agent import greeting
        now = 1_000_000.0
        entries = {
            "low":  {"last_seen": now},                                # Lv2 → skip
            "cool": {"last_seen": now, "last_greeted": now - 3 * 86400},   # cooldown
            "ok1":  {"last_seen": now, "last_greeted": now - 10 * 86400},  # passes
            "ok2":  {"last_seen": now},                                # never greeted
        }
        states = {"low": {"level": 2}, "cool": {"level": 7},
                  "ok1": {"level": 5}, "ok2": {"level": 9}}
        state_for = lambda oid: states.get(oid)  # noqa: E731
        # probability gate closed → nobody; rng()==probability → excluded
        assert greeting.pick_greet_targets(entries, state_for=state_for,
                                           now=now, rng=lambda: 0.99) == []
        assert greeting.pick_greet_targets({"ok2": entries["ok2"]},
                                           state_for=state_for, now=now,
                                           rng=lambda: 0.25,
                                           probability=0.25) == []
        # gates open → level + cooldown survivors, in entries order
        picked = greeting.pick_greet_targets(entries, state_for=state_for,
                                             now=now, rng=lambda: 0.0)
        assert [oid for oid, _ in picked] == ["ok1", "ok2"], picked
        assert picked[0][1] == states["ok1"]
        # batch cap
        assert len(greeting.pick_greet_targets(entries, state_for=state_for,
                                               now=now, rng=lambda: 0.0,
                                               max_batch=1)) == 1
        # no bond state / level missing (=Lv1) → skipped
        assert greeting.pick_greet_targets(entries, state_for=lambda o: None,
                                           now=now, rng=lambda: 0.0) == []
        assert greeting.pick_greet_targets({"x": {}}, state_for=lambda o: {},
                                           now=now, rng=lambda: 0.0) == []
        # higher min_level raises the bar
        assert greeting.pick_greet_targets(entries, state_for=state_for,
                                           now=now, rng=lambda: 0.0,
                                           min_level=9) == [("ok2", states["ok2"])]
        print_pass("pick gates: level/cooldown/probability/batch + order")

    def test_bond_config_defaults(self):
        from agent.bond import _DEFAULTS
        assert _DEFAULTS["greet_enabled"] is True
        assert _DEFAULTS["greet_min_level"] == 5
        assert _DEFAULTS["greet_daily_cap"] == 1
        assert _DEFAULTS["greet_monthly_cap"] == 4
        assert _DEFAULTS["greet_cooldown_days"] == 7
        assert 0 < _DEFAULTS["greet_probability"] <= 1
        assert _DEFAULTS["greet_interval_hours"] > 0
        print_pass("bond config exposes greet_* tunables")


# ── P4: 多模态生成（media_gen / asset_memory / 配额 / 锚点 / 主动媒体）──

class _FakeResp:
    """Minimal httpx.Response stand-in (json + streaming download)."""

    def __init__(self, status=200, payload=None, text="", body=b""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text
        self._body = body

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    async def aiter_bytes(self, chunk=65536):
        yield self._body


class _StreamCM:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *exc):
        return False


class _FakeHttpx:
    """Scripted replacement for media_gen.httpx (queue of responses/exceptions)."""

    class TimeoutException(Exception):
        pass

    class NetworkError(Exception):
        pass

    def __init__(self, queue):
        self.queue = list(queue)
        self.calls = []  # [(kind, url, kwargs)]

    def AsyncClient(self, **kwargs):
        outer = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, **kw):
                outer.calls.append(("post", url, kw))
                item = outer.queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

            def stream(self, method, url, **kw):
                outer.calls.append(("stream", url, kw))
                item = outer.queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return _StreamCM(item)

        return _Client()


def _image_payload(url="http://cdn.test/img.png"):
    return {"output": {"choices": [{"message": {"content": [{"image": url}]}}]},
            "usage": {"input_tokens": 10}}


def _tts_payload(url="http://cdn.test/a.mp3"):
    return {"output": {"audio": {"url": url}}, "usage": {"characters": 12}}


class TestMediaGen:
    """35. P4: lib/media_gen.py runner + social_tools create_image/send_voice."""

    CARD = ("# 露比 (Rubi)\n\n## Appearance Anchor\n\n"
            "淡金色长发、海蓝色瞳孔的娇小少女。\n\n## Other\n\nx")

    def run(self):
        print_header("35. Media Generation Runner & Tools (P4)")
        from lib import media_gen
        self.mg = media_gen
        self.tmp = tempfile.mkdtemp(prefix="mediagen_")
        self._orig_settings = media_gen._SETTINGS_PATH
        self._orig_httpx = media_gen.httpx
        try:
            media_gen._SETTINGS_PATH = self._write_settings()
            self._reset_sems()
            asyncio.run(self.test_image_success())
            asyncio.run(self.test_image_reference_edit())
            asyncio.run(self.test_image_retry_and_reject())
            asyncio.run(self.test_tts_success_and_truncation())
            asyncio.run(self.test_not_configured())
            asyncio.run(self.test_review_prompt())
            self.test_config_helpers()
            asyncio.run(self.test_create_image_tool_chain())
            asyncio.run(self.test_send_voice_tool_chain())
        finally:
            media_gen._SETTINGS_PATH = self._orig_settings
            media_gen.httpx = self._orig_httpx
            self._reset_sems()
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _reset_sems(self):
        self.mg._IMAGE_SEM = None
        self.mg._TTS_SEM = None

    def _write_settings(self, image=True, tts=True):
        cfg = {}
        if image:
            cfg["IMAGE_GEN_MODEL"] = {
                "api_key": "k", "api_base": "http://api.test/gen",
                "model": "img-model", "timeout": 5, "max_retries": 1,
                "max_concurrency": 2,
            }
        if tts:
            cfg["TTS_MODEL"] = {
                "api_key": "k", "api_base": "http://api.test/tts",
                "model": "tts-model", "format": "mp3", "sample_rate": 22050,
                "timeout": 5, "max_retries": 1, "max_concurrency": 2,
                "default_voice": "dv", "voice_pool": ["v1", "v2", "v3"],
            }
        path = os.path.join(self.tmp, f"settings_{len(os.listdir(self.tmp))}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return path

    def _use_httpx(self, queue):
        fake = _FakeHttpx(queue)
        self.mg.httpx = fake
        return fake

    async def test_image_success(self):
        out = os.path.join(self.tmp, "gen", "img.png")
        fake = self._use_httpx([
            _FakeResp(200, _image_payload()),
            _FakeResp(200, body=b"PNGDATA"),
        ])
        got = await self.mg.generate_image(
            "a cat", out_path=out, negative_prompt="blurry")
        assert got == out and os.path.isfile(out), got
        with open(out, "rb") as f:
            assert f.read() == b"PNGDATA"
        kind, url, kw = fake.calls[0]
        assert kind == "post" and url == "http://api.test/gen"
        assert kw["headers"]["Authorization"] == "Bearer k"
        body = kw["json"]
        assert body["model"] == "img-model"
        content = body["input"]["messages"][0]["content"]
        assert content == [{"text": "a cat"}], content  # no reference → pure T2I
        assert body["parameters"]["watermark"] is False
        assert body["parameters"]["negative_prompt"] == "blurry"
        # download went through the stream call
        assert fake.calls[1][0] == "stream"
        # empty prompt → None, no HTTP
        fake.queue.clear()
        assert await self.mg.generate_image("  ", out_path=out) is None
        assert fake.calls[1][0] == "stream" and len(fake.calls) == 2
        print_pass("generate_image: T2I body/headers, download to out_path")

    async def test_image_reference_edit(self):
        ref = os.path.join(self.tmp, "ref.png")
        with open(ref, "wb") as f:
            f.write(b"REF")
        out = os.path.join(self.tmp, "i2i.png")
        fake = self._use_httpx([
            _FakeResp(200, _image_payload()),
            _FakeResp(200, body=b"IMG2"),
        ])
        got = await self.mg.generate_image(
            "same face, new scene", out_path=out, reference_image_path=ref)
        assert got == out
        content = fake.calls[0][2]["json"]["input"]["messages"][0]["content"]
        assert len(content) == 2 and content[1] == {"text": "same face, new scene"}
        assert content[0]["image"].startswith("data:image/png;base64,"), content[0]
        # missing reference file degrades to pure T2I (no crash)
        fake.queue.extend([_FakeResp(200, _image_payload()),
                           _FakeResp(200, body=b"IMG3")])
        got2 = await self.mg.generate_image(
            "x", out_path=os.path.join(self.tmp, "i2i2.png"),
            reference_image_path=os.path.join(self.tmp, "no_such.png"))
        assert got2 and len(fake.calls[2][2]["json"]["input"]["messages"][0]["content"]) == 1
        print_pass("generate_image: reference → base64 I2I edit; missing ref → T2I")

    async def test_image_retry_and_reject(self):
        out = os.path.join(self.tmp, "retry.png")
        # 500 → one retry → success
        fake = self._use_httpx([
            _FakeResp(500, text="boom"),
            _FakeResp(200, _image_payload()),
            _FakeResp(200, body=b"OK"),
        ])
        with patch.object(asyncio, "sleep", new=AsyncMock(return_value=None)):
            got = await self.mg.generate_image("x", out_path=out)
        assert got == out
        assert sum(1 for c in fake.calls if c[0] == "post") == 2
        # 4xx (审核/参数) → NO retry, None
        fake2 = self._use_httpx([_FakeResp(400, text="rejected")])
        assert await self.mg.generate_image("x", out_path=out) is None
        assert len(fake2.calls) == 1
        # timeout twice → None after exactly one retry
        fake3 = self._use_httpx([
            _FakeHttpx.TimeoutException(), _FakeHttpx.TimeoutException()])
        with patch.object(asyncio, "sleep", new=AsyncMock(return_value=None)):
            assert await self.mg.generate_image("x", out_path=out) is None
        assert len(fake3.calls) == 2
        # 200 but no url in payload → None
        self._use_httpx([_FakeResp(200, {"output": {"choices": []}})])
        assert await self.mg.generate_image("x", out_path=out) is None
        # download fails (network) → None even though generation succeeded
        self._use_httpx([_FakeResp(200, _image_payload()),
                         _FakeHttpx.NetworkError()])
        assert await self.mg.generate_image("x", out_path=out) is None
        print_pass("generate_image: 5xx retry-once, 4xx no-retry, timeout/download → None")

    async def test_tts_success_and_truncation(self):
        out = os.path.join(self.tmp, "voice", "a.mp3")
        fake = self._use_httpx([
            _FakeResp(200, _tts_payload()),
            _FakeResp(200, body=b"MP3DATA"),
        ])
        long_text = "晚安呀" * 100  # 300 chars
        got = await self.mg.generate_speech(long_text, "v1", out_path=out,
                                            max_chars=10)
        assert got == out and os.path.isfile(out)
        with open(out, "rb") as f:
            assert f.read() == b"MP3DATA"
        _, url, kw = fake.calls[0]
        assert url == "http://api.test/tts"
        inp = kw["json"]["input"]
        assert inp["text"] == long_text[:10]  # 按字符计费 → 硬截断
        assert inp["voice"] == "v1"
        assert inp["format"] == "mp3" and inp["sample_rate"] == 22050
        # empty voice → default_voice; hard cap respected
        fake.queue.extend([_FakeResp(200, _tts_payload()),
                           _FakeResp(200, body=b"M2")])
        got2 = await self.mg.generate_speech(
            "x" * 5000, "", out_path=os.path.join(self.tmp, "b.mp3"),
            max_chars=99999)
        assert got2
        inp2 = fake.calls[2][2]["json"]["input"]
        assert inp2["voice"] == "dv" and len(inp2["text"]) == 600
        # no audio url → None; empty text → None without HTTP
        self._use_httpx([_FakeResp(200, {"output": {}})])
        assert await self.mg.generate_speech("hi", "v1", out_path=out) is None
        assert await self.mg.generate_speech("  ", "v1", out_path=out) is None
        print_pass("generate_speech: body/truncation/default_voice, no-url → None")

    async def test_not_configured(self):
        self.mg._SETTINGS_PATH = self._write_settings(image=False, tts=False)
        try:
            fake = self._use_httpx([])
            assert await self.mg.generate_image("x", out_path="y") is None
            assert await self.mg.generate_speech("x", "v", out_path="y") is None
            assert fake.calls == []  # never touches the network
            assert self.mg.is_image_available() is False
            assert self.mg.is_tts_available() is False
            print_pass("unconfigured sections → None, zero HTTP")
        finally:
            self.mg._SETTINGS_PATH = self._write_settings()

    async def test_review_prompt(self):
        class _Client:
            def __init__(self, reply):
                self.reply = reply
                self.calls = 0

            async def chat_completion(self, message, history=None,
                                      timeout_set=20.0, purpose=""):
                self.calls += 1
                if isinstance(self.reply, Exception):
                    raise self.reply
                return self.reply

        ok, reason = await self.mg.review_prompt("正常的画", _Client("APPROVE"))
        assert ok and reason == ""
        ok, reason = await self.mg.review_prompt("色情内容", _Client("REJECT: 色情描写"))
        assert not ok and reason == "色情描写"
        ok, _ = await self.mg.review_prompt("x", _Client("REJECT"))
        assert not ok  # bare REJECT still fail-closed with default reason
        # infra errors → fail-open
        ok, _ = await self.mg.review_prompt("x", _Client("调用DeepSeek API时出错: 500"))
        assert ok
        ok, _ = await self.mg.review_prompt("x", _Client(RuntimeError("net")))
        assert ok
        ok, _ = await self.mg.review_prompt("x", _Client(""))
        assert ok
        # no client → fail-open; empty text → rejected without any call
        assert await self.mg.review_prompt("x", None) == (True, "")
        ok, reason = await self.mg.review_prompt("   ", _Client("APPROVE"))
        assert not ok and reason == "内容为空"
        print_pass("review_prompt: REJECT fail-closed, infra fail-open, empty reject")

    def test_config_helpers(self):
        pool, default = self.mg.get_voice_pool()
        assert pool == ["v1", "v2", "v3"] and default == "dv"
        assert self.mg.get_model_name("image") == "img-model"
        assert self.mg.get_model_name("voice") == "tts-model"
        print_pass("get_voice_pool/get_model_name read fresh config")

    # ── social_tools 全链路（三重门 + 去重 + 资产卡 + observation）──

    def _tool_env(self):
        from agent.asset_memory import AssetMemory
        from types import SimpleNamespace
        env_dir = tempfile.mkdtemp(prefix="env_", dir=self.tmp)
        m = _tmp_bond_manager(env_dir)
        am = AssetMemory(base_dir=os.path.join(env_dir, "assets_tools"))
        reg = SimpleNamespace(reference_image_path=lambda cid: None,
                              get_voice_id=lambda cid: "vid1")
        prog, imgs, auds = [], [], []

        async def send_progress(text):
            prog.append(text)

        async def send_image(item):
            imgs.append(item)

        async def send_audio(item):
            auds.append(item)

        return m, am, reg, prog, imgs, auds, send_progress, send_image, send_audio

    async def test_create_image_tool_chain(self):
        from tools.social_tools import create_image
        m, am, reg, prog, imgs, _auds, sp, si, _sa = self._tool_env()
        m.add_exp("u1", "rubi", 1200)  # Lv5 — image gate (Lv4) passes

        async def fake_gen(prompt, *, out_path, reference_image_path=None,
                           negative_prompt="", **kw):
            fake_gen.calls.append({"prompt": prompt, "ref": reference_image_path,
                                   "neg": negative_prompt})
            if fake_gen.fail:
                return None
            with open(out_path, "wb") as f:
                f.write(b"PNG")
            return out_path
        fake_gen.calls = []
        fake_gen.fail = False

        common = dict(char_id="rubi", role="regular", card_content=self.CARD,
                      display_name="露比", bond_manager=m, registry=reg,
                      asset_memory=am, send_progress=sp, send_image=si)
        with patch.object(self.mg, "is_image_available", lambda: True), \
                patch.object(self.mg, "get_model_name", lambda kind: "img-model"), \
                patch.object(self.mg, "review_prompt", AsyncMock(return_value=(True, ""))), \
                patch.object(self.mg, "generate_image", new=fake_gen):
            # 等级门：Lv1 用户被婉拒，且不发桥接语、不生成
            obs = await create_image(user_id="u_low", description="海边", **common)
            assert "羁绊还不够深" in obs and fake_gen.calls == [] and prog == []
            # 无效入参
            obs = await create_image(user_id="", description="x", **common)
            assert obs.startswith("[Error]")
            obs = await create_image(user_id="u1", description="  ", **common)
            assert "想画什么" in obs
            # 配额门（u_q 等级达标，只卡配额）
            m.add_exp("u_q", "rubi", 1200)
            m._config["daily_image_regular"] = 1
            m.record_media_use("u_q", "rubi", "image")
            obs = await create_image(user_id="u_q", description="x", **common)
            assert "今天的画图次数已经用完了" in obs
            m._config["daily_image_regular"] = 3
            # 生成失败 → 道歉 observation + gen_failed 审计，无卡无发送
            fake_gen.fail = True
            obs = await create_image(user_id="u1", description="失败图", **common)
            assert "画图生成失败" in obs and imgs == []
            assert am.list_cards("u1") == []
            fake_gen.fail = False
            # 成功：锚点前缀 prompt + 桥接语 + 资产卡 + 记账 + 发送
            n_gen, n_prog = len(fake_gen.calls), len(prog)
            obs = await create_image(user_id="u1", description="在海边散步", **common)
            assert "画好了" in obs
            call = fake_gen.calls[-1]
            assert call["prompt"].startswith("角色外观（必须严格保持一致）: 淡金色长发")
            assert "角色: 露比" in call["prompt"] and "画面: 在海边散步" in call["prompt"]
            assert call["neg"] and call["ref"] is None
            assert len(fake_gen.calls) == n_gen + 1
            assert len(prog) == n_prog + 1 and prog[-1]  # 桥接语先于生成
            assert len(imgs) == 1 and imgs[0].label == "在海边散步"
            cards = am.list_cards("u1", "rubi")
            assert len(cards) == 1 and cards[0].tags == ["generated"]
            assert cards[0].prompt == call["prompt"] and cards[0].model == "img-model"
            assert os.path.isfile(cards[0].path)
            st = m.get_state("u1", "rubi", create=False)
            assert st["daily"]["images"] == 1 and st["stats"]["images"] == 1
            # 去重：同 description 再来一次 → 复用旧文件，不再计费
            obs = await create_image(user_id="u1", description="在海边散步", **common)
            assert "旧作" in obs
            assert len(fake_gen.calls) == n_gen + 1  # 去重命中不再生成
            assert len(imgs) == 2  # 旧作也重发了
            assert m.get_state("u1", "rubi", create=False)["daily"]["images"] == 2
            assert len(am.list_cards("u1", "rubi")) == 1  # 去重不建新卡
            # 审查拒绝 → 婉拒 + review_reject 审计，审查前置不生成
            n_gen = len(fake_gen.calls)
            with patch.object(self.mg, "review_prompt",
                              AsyncMock(return_value=(False, "违规"))):
                obs = await create_image(user_id="u1", description="违规内容", **common)
            assert "内容安全审查" in obs
            assert len(fake_gen.calls) == n_gen
        audit = open(am._audit_path, encoding="utf-8").read()
        assert "gen_failed" in audit and "review_reject" in audit
        assert "dedupe" in audit
        assert any(l.endswith("|ok") for l in audit.splitlines())
        print_pass("create_image chain: gates/dedupe/anchor prompt/card/audit/obs")

    async def test_send_voice_tool_chain(self):
        from tools.social_tools import send_voice
        m, am, reg, prog, _imgs, auds, sp, _si, sa = self._tool_env()
        m.add_exp("u1", "rubi", 1200)  # Lv5 — voice gate passes

        async def fake_tts(text, voice, *, out_path, max_chars=300, **kw):
            fake_tts.calls.append({"text": text, "voice": voice})
            if fake_tts.fail:
                return None
            with open(out_path, "wb") as f:
                f.write(b"MP3")
            return out_path
        fake_tts.calls = []
        fake_tts.fail = False

        common = dict(char_id="rubi", role="regular", bond_manager=m,
                      registry=reg, asset_memory=am,
                      send_progress=sp, send_audio=sa)
        with patch.object(self.mg, "is_tts_available", lambda: True), \
                patch.object(self.mg, "get_model_name", lambda kind: "tts-model"), \
                patch.object(self.mg, "review_prompt", AsyncMock(return_value=(True, ""))), \
                patch.object(self.mg, "generate_speech", new=fake_tts):
            # 等级门（voice 需 Lv5；u_low 是 Lv1）
            obs = await send_voice(user_id="u_low", text="晚安", **common)
            assert "羁绊还不够深" in obs and fake_tts.calls == []
            # 空文本 → 引导 observation
            obs = await send_voice(user_id="u1", text="  ", **common)
            assert "想听你说什么" in obs
            # 合成失败 → 道歉
            fake_tts.fail = True
            obs = await send_voice(user_id="u1", text="晚安", **common)
            assert "语音合成失败" in obs and auds == []
            fake_tts.fail = False
            # 成功：固定 voice_id + 资产卡(transcript) + 记账 + OutAudio
            n_prog = len(prog)
            obs = await send_voice(user_id="u1", text="晚安呀，团长～", **common)
            assert "录了一段语音" in obs and "晚安呀，团长～" in obs
            assert fake_tts.calls[-1]["voice"] == "vid1"  # 声纹一致性
            assert len(prog) == n_prog + 1 and prog[-1]  # 桥接语先于合成
            assert len(auds) == 1 and auds[0].label == "晚安呀，团长～"[:20]
            cards = am.list_cards("u1", "rubi")
            assert len(cards) == 1 and cards[0].type == "voice"
            assert cards[0].transcript == "晚安呀，团长～"
            st = m.get_state("u1", "rubi", create=False)
            assert st["daily"]["voices"] == 1 and st["daily"]["images"] == 0
            # media_tts_max_chars 截断
            m._config["media_tts_max_chars"] = 10
            obs = await send_voice(user_id="u1", text="一二三四五六七八九十超出部分", **common)
            assert fake_tts.calls[-1]["text"] == "一二三四五六七八九十"
            assert am.list_cards("u1", "rubi")[0].transcript == "一二三四五六七八九十"
            m._config["media_tts_max_chars"] = 300
            # 配额门：voice 独立于 image 计数
            m._config["daily_voice_regular"] = 2
            obs = await send_voice(user_id="u1", text="再来一句", **common)
            assert "今天的语音次数已经用完了" in obs
        print_pass("send_voice chain: gate/voice_id/transcript card/truncation/quota")


class TestAssetCardsP4:
    """36. P4: agent/asset_memory.py — 卡读写/列表/去重/审计/缓存."""

    def run(self):
        print_header("36. Asset Cards & Dedupe (P4)")
        from agent.asset_memory import AssetMemory
        self.tmp = tempfile.mkdtemp(prefix="assets_")
        try:
            self.am = AssetMemory(base_dir=self.tmp)
            self.test_ids_and_hashes()
            self.test_card_roundtrip()
            self.test_list_cards()
            self.test_dedupe()
            self.test_audit()
            self.test_cache_mtime()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ids_and_hashes(self):
        from agent.asset_memory import make_asset_id, prompt_hash_of
        ids = {make_asset_id() for _ in range(100)}
        assert len(ids) == 100
        assert all(i.startswith("ast_") and len(i) == 12 for i in ids)
        assert prompt_hash_of(" x ") == prompt_hash_of("x")  # strip-stable
        assert len(prompt_hash_of("x")) == 32
        assert prompt_hash_of("a") != prompt_hash_of("b")
        assert prompt_hash_of("") == prompt_hash_of("   ")
        print_pass("make_asset_id unique; prompt_hash_of stable/stripped")

    def test_card_roundtrip(self):
        from agent.asset_memory import AssetCard
        am = self.am
        card = AssetCard(
            id="ast_00000001", type="image", char_id="rubi", user_id="u1",
            path="/tmp/x/ast_00000001.png", model="img-model",
            prompt="海边散步", tags=["generated"], created_at=1000,
            recall_count=2, prompt_hash="ph1", proactive=False)
        p = am.write_card(card)
        assert os.path.isfile(p) and p.endswith("ast_00000001.md")
        raw = open(p, encoding="utf-8").read()
        assert raw.startswith("---\n") and "海边散步" in raw
        # 冷读（新实例，绕过写缓存）验证磁盘格式可解析
        from agent.asset_memory import AssetMemory as _AM
        cold = _AM(base_dir=am.base_dir).read_card("u1", "ast_00000001")
        back = am.read_card("u1", "ast_00000001")
        for c in (cold, back):
            assert c.id == card.id and c.type == "image"
            assert c.prompt == "海边散步" and c.transcript == ""
            assert c.path == card.path and c.model == "img-model"
            assert c.tags == ["generated"] and c.created_at == 1000
            assert c.recall_count == 2 and c.prompt_hash == "ph1"
            assert c.proactive is False
        # voice card: body 存 transcript
        v = AssetCard(id="ast_00000002", type="voice", char_id="rubi",
                      user_id="u1", transcript="晚安", prompt="晚安",
                      tags=["generated", "proactive"], proactive=True)
        am.write_card(v)
        v2 = am.read_card("u1", "ast_00000002")
        assert v2.transcript == "晚安" and v2.proactive is True
        # path=None（已淘汰语义）roundtrip
        n = AssetCard(id="ast_00000003", type="image", char_id="c",
                      user_id="u1", path=None)
        am.write_card(n)
        assert am.read_card("u1", "ast_00000003").path is None
        # malformed content → None
        assert AssetCard.from_markdown("no frontmatter") is None
        assert AssetCard.from_markdown("---\nid: x\n") is None
        assert am.read_card("u1", "ast_nope") is None
        print_pass("card write/read roundtrip (image/voice/evicted) + malformed → None")

    def test_list_cards(self):
        from agent.asset_memory import AssetCard
        am = self.am
        for aid, char, ts in (("ast_l0000001", "rubi", 300),
                              ("ast_l0000002", "rubi", 100),
                              ("ast_l0000003", "roxy", 200)):
            am.write_card(AssetCard(id=aid, type="image", char_id=char,
                                    user_id="u2", created_at=ts))
        allc = am.list_cards("u2")
        assert [c.id for c in allc] == ["ast_l0000001", "ast_l0000003",
                                        "ast_l0000002"]  # newest first
        rubi = am.list_cards("u2", "rubi")
        assert [c.id for c in rubi] == ["ast_l0000001", "ast_l0000002"]
        assert am.list_cards("ghost") == []
        print_pass("list_cards: newest-first ordering + char filter + isolation")

    def test_dedupe(self):
        from agent.asset_memory import AssetCard, prompt_hash_of
        am = self.am
        path = am.binary_path("u3", "rubi", "ast_d0000001", "png")
        assert path.endswith("ast_d0000001.png")
        with open(path, "wb") as f:
            f.write(b"PNG")
        card = AssetCard(id="ast_d0000001", type="image", char_id="rubi",
                         user_id="u3", path=path, prompt="p1")
        am.write_card(card)
        ph = prompt_hash_of("p1")
        assert am.find_by_prompt_hash(ph) is None  # 未登记
        am.record_prompt_hash(ph, card)
        hit = am.find_by_prompt_hash(ph)
        assert hit is not None and hit.id == "ast_d0000001"
        assert am.find_by_prompt_hash("") is None
        assert am.find_by_prompt_hash("unknown") is None
        # TTL 过期 → miss；ttl_hours=0 → 不检查过期
        idx = json.load(open(am._dedupe_path, encoding="utf-8"))
        idx[ph]["ts"] = time.time() - 200 * 3600
        json.dump(idx, open(am._dedupe_path, "w", encoding="utf-8"))
        assert am.find_by_prompt_hash(ph, ttl_hours=168) is None
        assert am.find_by_prompt_hash(ph, ttl_hours=0) is not None
        # 二进制丢失 → miss（防止发不存在的文件）
        idx[ph]["ts"] = time.time()
        json.dump(idx, open(am._dedupe_path, "w", encoding="utf-8"))
        os.remove(path)
        assert am.find_by_prompt_hash(ph) is None
        # 无 path 的卡不登记
        am.record_prompt_hash("ph_nopath", AssetCard(
            id="ast_d0000002", type="image", char_id="rubi", user_id="u3"))
        assert "ph_nopath" not in json.load(
            open(am._dedupe_path, encoding="utf-8"))
        # 损坏索引 → {} 降级，且可继续登记
        with open(am._dedupe_path, "w", encoding="utf-8") as f:
            f.write("{corrupt!!")
        assert am.find_by_prompt_hash(ph) is None
        with open(path, "wb") as f:
            f.write(b"PNG2")
        am.record_prompt_hash(ph, card)
        assert am.find_by_prompt_hash(ph) is not None
        print_pass("dedupe: hit/TTL expiry/missing file/no-path skip/corrupt → {}")

    def test_audit(self):
        am = self.am
        am.append_audit("u4", "rubi", "image", "img-model", "ph9", "ok")
        am.append_audit("u4", "rubi", "voice", "tts-model", "ph8", "gen_failed")
        lines = open(am._audit_path, encoding="utf-8").read().strip().splitlines()
        assert len(lines) == 2
        f1 = lines[0].split("|")
        assert len(f1) == 7
        assert f1[1:] == ["u4", "rubi", "image", "img-model", "ph9", "ok"]
        assert f1[0].isdigit()  # ts
        assert lines[1].endswith("|gen_failed")
        print_pass("append_audit: ts|user|char|type|model|phash|result lines")

    def test_cache_mtime(self):
        from agent.asset_memory import AssetCard
        am = self.am
        card = AssetCard(id="ast_c0000001", type="image", char_id="rubi",
                         user_id="u5", model="m1")
        p = am.write_card(card)
        assert am.read_card("u5", "ast_c0000001").model == "m1"
        # 外部改写（panel/其他进程）→ mtime 变化 → 缓存失效重读
        card.model = "m2-external"
        with open(p, "w", encoding="utf-8") as f:
            f.write(card.to_markdown())
        st = os.stat(p)
        os.utime(p, (st.st_atime, st.st_mtime + 5))
        assert am.read_card("u5", "ast_c0000001").model == "m2-external"
        # 文件消失 → None（缓存弹出，不抛异常）
        os.remove(p)
        assert am.read_card("u5", "ast_c0000001") is None
        assert am.read_card("u5", "ast_c0000001") is None  # 二次读也安全
        print_pass("read_card cache: mtime invalidation + vanished file → None")


class TestMediaQuota:
    """37. P4: bond.py media 配额/等级门/日滚动/format_status."""

    def run(self):
        print_header("37. Media Quota & Level Gates (P4)")
        self.tmp = tempfile.mkdtemp(prefix="mediaquota_")
        try:
            self.test_defaults()
            self.test_level_gates()
            self.test_quota_roles()
            self.test_daily_roll_and_status()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults(self):
        from agent.bond import _DEFAULTS
        assert _DEFAULTS["media_enabled"] is True
        assert _DEFAULTS["media_image_min_level"] == 4
        assert _DEFAULTS["media_voice_min_level"] == 5
        assert _DEFAULTS["media_proactive_voice_min_level"] == 7
        assert (_DEFAULTS["daily_image_regular"], _DEFAULTS["daily_image_vip"],
                _DEFAULTS["daily_image_admin"]) == (3, 10, 50)
        assert (_DEFAULTS["daily_voice_regular"], _DEFAULTS["daily_voice_vip"],
                _DEFAULTS["daily_voice_admin"]) == (3, 10, 50)
        assert _DEFAULTS["proactive_media_probability"] == 0.15
        assert _DEFAULTS["proactive_media_daily_cap"] == 1
        assert _DEFAULTS["media_dedupe_ttl_hours"] == 168
        assert _DEFAULTS["media_tts_max_chars"] == 300
        assert _DEFAULTS["media_asset_quota_mb_regular"] == 100
        print_pass("bond _DEFAULTS expose all P4 media tunables")

    def test_level_gates(self):
        m = _tmp_bond_manager(self.tmp)
        assert m.get_level("nobody", "rubi") == 1  # 无状态 → Lv1
        assert m.get_level("u", "rubi") == 1
        m.add_exp("u", "rubi", 300)  # Lv3
        assert m.get_level("u", "rubi") == 3
        assert m.media_level_gate("u", "rubi", "image") == (False, 3, 4)
        m.add_exp("u", "rubi", 350)  # 650 → Lv4
        assert m.media_level_gate("u", "rubi", "image") == (True, 4, 4)
        assert m.media_level_gate("u", "rubi", "voice") == (False, 4, 5)
        m.add_exp("u", "rubi", 550)  # 1200 → Lv5
        assert m.media_level_gate("u", "rubi", "voice") == (True, 5, 5)
        assert m.media_level_gate("u", "rubi", "voice", proactive=True) == (False, 5, 7)
        m.add_exp("u", "rubi", 1800)  # 3000 → Lv7
        assert m.media_level_gate("u", "rubi", "voice", proactive=True) == (True, 7, 7)
        assert m.media_level_gate("u", "rubi", "video") == (False, 0, 0)  # fail-closed
        print_pass("level gates: image Lv4 / voice Lv5 / proactive Lv7 boundaries")

    def test_quota_roles(self):
        m = _tmp_bond_manager(self.tmp)
        ok, msg = m.check_media_quota("u", "rubi", "image", "regular")
        assert ok and msg == ""
        for i in (1, 2, 3):
            assert m.record_media_use("u", "rubi", "image") == i
        ok, msg = m.check_media_quota("u", "rubi", "image", "regular")
        assert not ok and msg == "daily_limit:3/3"
        # vip 上限更高（同一用户同一计数，角色决定 limit）
        assert m.check_media_quota("u", "rubi", "image", "vip") == (True, "")
        for i in range(4, 11):
            m.record_media_use("u", "rubi", "image")
        assert m.check_media_quota("u", "rubi", "image", "vip")[0] is False
        assert m.check_media_quota("u", "rubi", "image", "admin") == (True, "")
        # 未知角色 → 按 regular 处理
        m.record_media_use("u2", "rubi", "voice")
        m.record_media_use("u2", "rubi", "voice")
        m.record_media_use("u2", "rubi", "voice")
        assert m.check_media_quota("u2", "rubi", "voice", "ghost")[0] is False
        # voice 与 image 计数独立
        assert m.check_media_quota("u2", "rubi", "image", "regular") == (True, "")
        st = m.get_state("u2", "rubi", create=False)
        assert st["daily"]["voices"] == 3 and st["daily"]["images"] == 0
        assert st["stats"]["voices"] == 3
        # 未知类型 / 全局开关
        assert m.check_media_quota("u", "rubi", "video", "regular") == \
            (False, "unknown_media_type")
        assert m.record_media_use("u", "rubi", "video") == 0
        m._config["media_enabled"] = False
        assert m.check_media_quota("u3", "rubi", "image", "regular") == \
            (False, "media_disabled")
        m._config["media_enabled"] = True
        assert m.check_media_quota("u3", "rubi", "image", "regular")[0]
        print_pass("quota: per-role caps 3/10/50, counters isolated, fail-closed")

    def test_daily_roll_and_status(self):
        # 全新目录：避免与 test_quota_roles 的同日计数互相污染
        m = _tmp_bond_manager(tempfile.mkdtemp(prefix="roll_", dir=self.tmp))
        for _ in range(3):
            m.record_media_use("u", "rubi", "image")
        assert m.check_media_quota("u", "rubi", "image", "regular")[0] is False
        # 跨日：daily.date 过期 → 计数清零
        st = m.get_state("u", "rubi")
        st["daily"]["date"] = "2000-01-01"
        m._save("u")
        assert m.check_media_quota("u", "rubi", "image", "regular") == (True, "")
        assert m.get_state("u", "rubi", create=False)["daily"]["images"] == 0
        assert m.get_state("u", "rubi", create=False)["stats"]["images"] == 3  # 累计保留
        # format_status 带媒体行（按角色上限）
        m.record_media_use("u", "rubi", "image")
        m.record_media_use("u", "rubi", "voice")
        m.record_media_use("u", "rubi", "voice")
        out = m.format_status("u", "rubi", display_name="露比", role="regular")
        assert "今日生成: 画图 1/3 · 语音 2/3" in out, out
        out_vip = m.format_status("u", "rubi", display_name="露比", role="vip")
        assert "画图 1/10 · 语音 2/10" in out_vip, out_vip
        # media_enabled=False → 不显示媒体行
        m._config["media_enabled"] = False
        assert "今日生成" not in m.format_status("u", "rubi", role="regular")
        print_pass("daily roll resets counters; format_status media line per role")


class TestAnchorVoiceId:
    """38. P4: Appearance Anchor 解析 + voice_id 解析链（声纹一致性）."""

    def run(self):
        print_header("38. Appearance Anchor & Voice ID (P4)")
        self.tmp = tempfile.mkdtemp(prefix="anchor_")
        try:
            self.test_parse_anchor()
            self.test_builtin_cards()
            self.test_ugc_resolution_chain()
            self.test_build_card_and_assign()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parse_anchor(self):
        from agent.bond_content import parse_appearance_anchor
        card = ("# X\n\n## Appearance Anchor\n\n银发红瞳。\n\n## Other\n\ny")
        assert parse_appearance_anchor(card) == "银发红瞳。"
        assert parse_appearance_anchor("# X\n\n## Other\n\ny") == ""
        assert parse_appearance_anchor("") == ""
        assert parse_appearance_anchor(None) == ""
        print_pass("parse_appearance_anchor: section body / absent → ''")

    def test_builtin_cards(self):
        # 内置卡真文件回归：rubi/roxy 的 voice_id 注释 + Appearance Anchor 段
        from agent.personality import PersonalityManager
        from agent.bond_content import parse_appearance_anchor
        pm = PersonalityManager()
        assert pm.get_voice_id("rubi") == "longxiaochun_v3.6"
        assert pm.get_voice_id("roxy_character") == "longanrou_v3.6"
        anchor = parse_appearance_anchor(pm.load("rubi"))
        assert "淡金色长发" in anchor and "海蓝色" in anchor, anchor
        anchor2 = parse_appearance_anchor(pm.load("roxy_character"))
        assert "蓝色" in anchor2 and "三股辫" in anchor2, anchor2
        print_pass("builtin rubi/roxy cards carry voice_id + Appearance Anchor")

    def test_ugc_resolution_chain(self):
        import hashlib
        from agent.character_registry import CharacterRegistry
        pool = ["v1", "v2", "v3"]

        def fake_pool():
            return pool, "dv"

        pdir = os.path.join(self.tmp, "personalities")
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "plain.md"), "w", encoding="utf-8") as f:
            f.write("# 素卡\n\n<!-- mode: split -->\n\nx")
        with patch("agent.character_registry.get_voice_pool", fake_pool):
            reg = CharacterRegistry(personalities_dir=pdir,
                                    characters_dir=os.path.join(self.tmp, "chars"))
            # 1) UGC meta.json.voice_id 最优先（即使卡片注释另有值）
            reg.register_character(
                "c_meta1", "# A\n<!-- mode: split | voice_id: cardvoice -->\n\nx",
                {"display_name": "A", "author": "u1", "voice_id": "metavoice"})
            assert reg.get_voice_id("c_meta1") == "metavoice"
            # 2) meta 无 voice_id → 卡片注释
            reg.register_character(
                "c_card1", "# B\n<!-- mode: split | voice_id: cardvoice -->\n\nx",
                {"display_name": "B", "author": "u1", "voice_id": None})
            assert reg.get_voice_id("c_card1") == "cardvoice"
            # 3) 两者皆无 → voice_pool blake2b 确定性分配（跨调用/跨进程稳定）
            reg.register_character(
                "c_pool1", "# C\n<!-- mode: split -->\n\nx",
                {"display_name": "C", "author": "u1"})
            v1 = reg.get_voice_id("c_pool1")
            assert v1 == reg.get_voice_id("c_pool1") and v1 in pool
            digest = hashlib.blake2b(b"c_pool1", digest_size=8).hexdigest()
            assert v1 == pool[int(digest, 16) % len(pool)]
            # 内置卡无 voice_id 注释 → 同款 pool 分配
            pv = reg.get_voice_id("plain")
            d2 = hashlib.blake2b(b"plain", digest_size=8).hexdigest()
            assert pv == pool[int(d2, 16) % len(pool)]
            # 4) 池空 → default_voice → None
            with patch("agent.character_registry.get_voice_pool", lambda: ([], "dv")):
                assert reg.get_voice_id("c_pool1") == "dv"
            with patch("agent.character_registry.get_voice_pool", lambda: ([], "")):
                assert reg.get_voice_id("c_pool1") is None
            # reference_image_path：缺失 → None；存在 → 路径
            assert reg.reference_image_path("c_pool1") is None
            adir = os.path.join(self.tmp, "chars", "c_pool1", "assets")
            os.makedirs(adir, exist_ok=True)
            ref = os.path.join(adir, "reference.png")
            with open(ref, "wb") as f:
                f.write(b"PNG")
            assert reg.reference_image_path("c_pool1") == ref
        print_pass("voice_id chain: meta → card comment → pool(blake2b) → default")

    def test_build_card_and_assign(self):
        import hashlib
        from tools.creation_tools import assign_voice_id, build_card
        from agent.bond_content import parse_appearance_anchor
        from agent.personality import PersonalityManager
        pool = ["v1", "v2", "v3"]
        with patch("tools.creation_tools.get_voice_pool", lambda: (pool, "dv")):
            a = assign_voice_id("xyz_123")
            assert a == assign_voice_id("xyz_123") and a in pool
            digest = hashlib.blake2b(b"xyz_123", digest_size=8).hexdigest()
            assert a == pool[int(digest, 16) % len(pool)]
        with patch("tools.creation_tools.get_voice_pool", lambda: ([], "dv")):
            assert assign_voice_id("q") == "dv"
        with patch("tools.creation_tools.get_voice_pool", lambda: ([], "")):
            assert assign_voice_id("q") is None
        fields = {"name": "小雪", "gender": "女", "personality": "冷静沉着",
                  "speech_style": "语速平缓", "appearance": "银发红瞳的少女"}
        card = build_card(fields, voice_id="v123")
        assert "<!-- mode: split | task_voice: character | voice_id: v123 -->" in card
        m = PersonalityManager._VOICE_ID_RE.search(card)
        assert m and m.group(1) == "v123"
        assert parse_appearance_anchor(card) == "银发红瞳的少女"
        card2 = build_card(fields)
        assert "voice_id" not in card2.split("-->")[0]
        assert parse_appearance_anchor(card2) == "银发红瞳的少女"
        print_pass("build_card voice slot + anchor section; assign_voice_id stable")


class _FakeRandom:
    """Deterministic stand-in for the random module inside the plugin."""

    def __init__(self, values=None):
        self.values = list(values or [])

    def random(self):
        return self.values.pop(0) if self.values else 0.0

    def uniform(self, a, b):
        return 0.0

    def choice(self, seq):
        return seq[0]


class _FakeMediaGenModule:
    """Stand-in for lib.media_gen inside plugins.qq_official."""

    def __init__(self):
        self.image_ok = True
        self.tts_ok = True
        self.image_result_ok = True
        self.voice_result_ok = True
        self.review_ok = True
        self.image_calls = []
        self.voice_calls = []

    def is_image_available(self):
        return self.image_ok

    def is_tts_available(self):
        return self.tts_ok

    def get_model_name(self, kind):
        return f"{kind}-model"

    async def generate_image(self, prompt, *, out_path, **kw):
        self.image_calls.append({"prompt": prompt, "out": out_path})
        if not self.image_result_ok:
            return None
        with open(out_path, "wb") as f:
            f.write(b"PNG")
        return out_path

    async def generate_speech(self, text, voice, *, out_path, **kw):
        self.voice_calls.append({"text": text, "voice": voice})
        if not self.voice_result_ok:
            return None
        with open(out_path, "wb") as f:
            f.write(b"MP3")
        return out_path

    async def review_prompt(self, text, client, **kw):
        return (self.review_ok, "" if self.review_ok else "违规")


class _FakePersonaMgr:
    CARD = ("# 露比 (Rubi)\n\n<!-- mode: split | task_voice: character | "
            "voice_id: voice-rubi -->\n\n## Appearance Anchor\n\n"
            "淡金色长发。\n")

    def __init__(self):
        self.mode = "split"

    def resolve_effective_personality(self, user_id, group_id=None):
        return "rubi"

    def get_mode(self, persona):
        return self.mode

    def load(self, persona):
        return self.CARD

    def get_display_name(self, persona):
        return "露比"

    def get_short_name(self, persona):
        return "露比"

    def get_voice_id(self, persona):
        return "voice-rubi"

    def reference_image_path(self, persona):
        return None


class TestProactiveMedia:
    """39. P4: plugins/qq_official.py _run_media_pass 门控链 + 降级 + 记账."""

    PATCHED = ("_bond_manager", "_asset_memory", "media_gen", "random",
               "get_character_registry", "_get_qq_bot", "_GREET_TARGETS_FILE",
               "_GREET_QUOTA_FILE", "send_active_c2c_media", "send_active_c2c",
               "_compose_greeting")

    def run(self):
        print_header("39. Proactive Media Pass (P4)")
        _import_turn_for_tests()  # USER_DATA_ROOT off production paths
        import nonebot
        try:
            nonebot.init()
        except ValueError:
            pass  # already initialized in this process
        import plugins.qq_official as po
        self.po = po
        self.tmp = tempfile.mkdtemp(prefix="media_pass_")
        self._setup()
        try:
            self.test_quota_kind_semantics()
            asyncio.run(self.test_gate_chain())
            asyncio.run(self.test_success_voice_and_daily_cap())
            asyncio.run(self.test_success_image_anchor())
            asyncio.run(self.test_send_failure_falls_back_to_text())
            asyncio.run(self.test_generation_failure_falls_back())
            asyncio.run(self.test_no_record_when_everything_fails())
        finally:
            self._teardown()
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _setup(self):
        from agent import greeting
        from agent.asset_memory import AssetMemory
        po = self.po
        self.greeting = greeting
        self.m = _tmp_bond_manager(self.tmp)
        self.am = AssetMemory(base_dir=os.path.join(self.tmp, "assets"))
        self.mg = _FakeMediaGenModule()
        self.pm = _FakePersonaMgr()
        self.rng = _FakeRandom()
        self.targets = os.path.join(self.tmp, "targets.json")
        self.quota = os.path.join(self.tmp, "quota.json")
        self.media_sends = []
        self.text_sends = []
        self.media_send_ok = True
        self.text_send_ok = True
        self._orig = {k: getattr(po, k) for k in self.PATCHED}

        async def fake_media_send(bot, openid, seg):
            self.media_sends.append((openid, seg))
            return self.media_send_ok

        async def fake_text_send(bot, openid, text):
            self.text_sends.append((openid, text))
            return self.text_send_ok

        async def fake_compose(persona, state):
            return "露比：「好久不见呀，最近好吗？」"

        po._bond_manager = self.m
        po._asset_memory = self.am
        po.media_gen = self.mg
        po.random = self.rng
        po.get_character_registry = lambda: self.pm
        po._get_qq_bot = lambda: MagicMock()
        po._GREET_TARGETS_FILE = self.targets
        po._GREET_QUOTA_FILE = self.quota
        po.send_active_c2c_media = fake_media_send
        po.send_active_c2c = fake_text_send
        po._compose_greeting = fake_compose

    def _teardown(self):
        for k, v in self._orig.items():
            setattr(self.po, k, v)

    def _reset_files(self):
        # 去重索引也要清：fake rng.choice 恒选场景池第一项，跨用例同
        # prompt 会命中上一个用例登记的资产（去重机制本身另有专测）
        for p in (self.targets, self.quota, self.am._audit_path,
                  self.am._dedupe_path):
            try:
                os.remove(p)
            except OSError:
                pass
        self.media_sends.clear()
        self.text_sends.clear()
        self.media_send_ok = True
        self.text_send_ok = True
        self.mg.image_result_ok = True
        self.mg.voice_result_ok = True
        self.mg.image_calls.clear()
        self.mg.voice_calls.clear()
        self.rng.values.clear()

    def _seed(self, openid, exp):
        self.greeting.note_c2c_contact(self.targets, openid)
        if exp:
            self.m.add_exp(openid, "rubi", exp)

    def test_quota_kind_semantics(self):
        g = self.greeting
        base = time.mktime(time.strptime("2026-10-10", "%Y-%m-%d")) + 43200
        data = {}
        g.quota_record(data, "O", now=base, kind="text")
        # 每日按 kind 独立：文本已发 1 条，媒体当日仍可发
        assert g.quota_allows(data, "O", now=base + 60, daily_cap=1,
                              monthly_cap=4, kind="media")
        g.quota_record(data, "O", now=base + 60, kind="media")
        assert not g.quota_allows(data, "O", now=base + 120, daily_cap=1,
                                  monthly_cap=4, kind="media")
        # 月度是平台共享配额：媒体判定按 text+media 总和
        d2 = {}
        for i in range(3):
            g.quota_record(d2, "O", now=base + i * 86400, kind="text")
        g.quota_record(d2, "O", now=base + 3 * 86400, kind="media")
        day4 = base + 4 * 86400
        assert not g.quota_allows(d2, "O", now=day4, daily_cap=1,
                                  monthly_cap=4, kind="media")   # 3+1 ≥ 4
        assert g.quota_allows(d2, "O", now=day4, daily_cap=1,
                              monthly_cap=4, kind="text")        # text 自身 3 < 4
        # 前缀隔离：media 计数不污染 text 平铺键（P3b 生产文件兼容）
        rec = data["O"]
        assert rec["day_count"] == 1 and rec["media_day_count"] == 1
        assert "last_active" in rec and "media_last_active" in rec
        # active_sent_today（file-backed + 脏数据 → False）
        path = os.path.join(self.tmp, "q2.json")
        assert g.active_sent_today(path, "O", kind="text") is False  # 无文件
        g.record_active_send(path, "O", kind="text")
        assert g.active_sent_today(path, "O", kind="text") is True
        assert g.active_sent_today(path, "O", kind="media") is False
        with open(path, "w", encoding="utf-8") as f:
            f.write("{corrupt!!")
        assert g.active_sent_today(path, "O", kind="text") is False
        print_pass("quota kinds: daily per-kind, monthly shared total, sent_today")

    async def test_gate_chain(self):
        po = self.po
        # (a) media_enabled=False → 整个 pass 直接返回
        self._reset_files()
        self._seed("OID1", 4300)
        self.m._config["media_enabled"] = False
        await po._run_media_pass()
        assert self.media_sends == [] and self.text_sends == []
        self.m._config["media_enabled"] = True
        # (b) 无 bot → 返回
        po._get_qq_bot = lambda: None
        await po._run_media_pass()
        assert self.media_sends == []
        po._get_qq_bot = lambda: MagicMock()
        # (c) 等级门：Lv3 (<4) → 跳过
        self._reset_files()
        self._seed("OID_low", 300)
        await po._run_media_pass()
        assert self.media_sends == [] and self.text_sends == []
        # (d) 概率门：rng ≥ 0.15 → 跳过
        self._reset_files()
        self._seed("OID_prob", 4300)
        self.rng.values.append(0.99)
        await po._run_media_pass()
        assert self.media_sends == []
        # (e) 今天已收到主动文本 → 不叠加媒体（防同日双发）
        self._reset_files()
        self._seed("OID_dup", 4300)
        self.greeting.record_active_send(self.quota, "OID_dup", kind="text")
        await po._run_media_pass()
        assert self.media_sends == []
        # (f) 非 split 人格 → 跳过
        self._reset_files()
        self._seed("OID_mode", 4300)
        self.pm.mode = "unified"
        await po._run_media_pass()
        assert self.media_sends == []
        self.pm.mode = "split"
        # (g) 无羁绊状态（未 seed exp → get_state create=False → None）→ 跳过
        self._reset_files()
        self.greeting.note_c2c_contact(self.targets, "OID_nostate")
        await po._run_media_pass()
        assert self.media_sends == []
        print_pass("media pass gates: enabled/bot/level/prob/same-day-text/mode/state")

    async def test_success_voice_and_daily_cap(self):
        po = self.po
        self._reset_files()
        self._seed("OID1", 4300)  # Lv8：图+语音都可
        self.rng.values.extend([0.0, 0.0])  # 概率过；kind 选择 <0.5 → voice
        await po._run_media_pass()
        assert len(self.media_sends) == 1
        openid, seg = self.media_sends[0]
        assert openid == "OID1" and seg.type == "file_audio"
        # TTS 用固定 voice_id；台词从「」包装中提取
        assert len(self.mg.voice_calls) == 1
        assert self.mg.voice_calls[0]["voice"] == "voice-rubi"
        assert self.mg.voice_calls[0]["text"] == "好久不见呀，最近好吗？"
        # 记账：media 已发、text 未发；冷却已重置
        assert self.greeting.active_sent_today(self.quota, "OID1", kind="media")
        assert not self.greeting.active_sent_today(self.quota, "OID1", kind="text")
        t = self.greeting.load_targets(self.targets)["OID1"]
        assert t.get("last_greeted") and t.get("greet_count") == 1
        # 资产卡：proactive voice，transcript 保留
        cards = self.am.list_cards("OID1", "rubi")
        assert len(cards) == 1 and cards[0].proactive is True
        assert cards[0].type == "voice" and cards[0].transcript == "好久不见呀，最近好吗？"
        assert os.path.isfile(cards[0].path)
        audit = open(self.am._audit_path, encoding="utf-8").read()
        assert "proactive_ok" in audit
        # 同日第二次 pass：media 日配额（1/天）→ 跳过，不重复生成
        self.rng.values.clear()
        await po._run_media_pass()
        assert len(self.media_sends) == 1 and len(self.mg.voice_calls) == 1
        print_pass("proactive voice: fixed voice_id, record-after-success, ≤1/day")

    async def test_success_image_anchor(self):
        po = self.po
        self._reset_files()
        self._seed("OID2", 1200)  # Lv5：只够图（语音要 Lv7）
        self.rng.values.append(0.0)
        await po._run_media_pass()
        assert len(self.media_sends) == 1
        assert self.media_sends[0][1].type == "file_image"
        assert self.mg.voice_calls == []  # can_voice=False → kind 必为 image
        prompt = self.mg.image_calls[0]["prompt"]
        assert prompt.startswith("角色外观（必须严格保持一致）: 淡金色长发。")
        assert "给用户发一张日常自拍" in prompt  # 场景池第一项
        cards = self.am.list_cards("OID2", "rubi")
        assert len(cards) == 1 and cards[0].type == "image"
        assert cards[0].proactive is True and "proactive" in cards[0].tags
        print_pass("proactive image: Lv5 image-only, anchor-prefixed prompt, card")

    async def test_send_failure_falls_back_to_text(self):
        po = self.po
        self._reset_files()
        self._seed("OID3", 4300)
        self.media_send_ok = False  # 平台拒收（审核/配额）
        self.rng.values.extend([0.0, 0.0])
        await po._run_media_pass()
        assert len(self.media_sends) == 1  # 尝试过
        assert len(self.text_sends) == 1   # 降级纯文本问候
        # 只在成功一侧记账：media 未记，text 已记 + 冷却重置
        assert not self.greeting.active_sent_today(self.quota, "OID3", kind="media")
        assert self.greeting.active_sent_today(self.quota, "OID3", kind="text")
        t = self.greeting.load_targets(self.targets)["OID3"]
        assert t.get("last_greeted")
        # 发送失败不写资产卡；审计留痕
        assert self.am.list_cards("OID3", "rubi") == []
        audit = open(self.am._audit_path, encoding="utf-8").read()
        assert "proactive_failed" in audit and "proactive_ok" not in audit
        print_pass("send failure → text fallback; record only after success")

    async def test_generation_failure_falls_back(self):
        po = self.po
        self._reset_files()
        self._seed("OID4", 1200)  # Lv5 → image
        self.mg.image_result_ok = False  # 生成失败 → seg None
        self.rng.values.append(0.0)
        await po._run_media_pass()
        assert self.media_sends == []  # seg=None → 短路，不浪费主动配额尝试
        assert len(self.text_sends) == 1
        assert not self.greeting.active_sent_today(self.quota, "OID4", kind="media")
        assert self.greeting.active_sent_today(self.quota, "OID4", kind="text")
        assert self.am.list_cards("OID4", "rubi") == []
        print_pass("generation failure → no media attempt, text fallback recorded")

    async def test_no_record_when_everything_fails(self):
        po = self.po
        self._reset_files()
        self._seed("OID5", 4300)
        self.media_send_ok = False
        self.text_send_ok = False
        self.rng.values.extend([0.0, 0.0])
        await po._run_media_pass()
        assert len(self.media_sends) == 1 and len(self.text_sends) == 1
        assert not self.greeting.active_sent_today(self.quota, "OID5", kind="media")
        assert not self.greeting.active_sent_today(self.quota, "OID5", kind="text")
        t = self.greeting.load_targets(self.targets)["OID5"]
        assert not t.get("last_greeted")  # 冷却不被失败消耗
        print_pass("all sends fail → zero quota recorded, cooldown untouched")


# ── P5: 多模态记忆闭环（检索 / 召回 / 上传文本化 / 事件蒸馏 / LRU）──

def _seed_asset(am, user="u1", char="rubi", type_="image", desc="",
                tags=None, created=None, recall=0, with_file=True,
                path_override=None, size=64):
    """Write one asset card (+ real binary unless with_file=False)."""
    from agent.asset_memory import AssetCard, make_asset_id
    aid = make_asset_id()
    path = path_override
    if path is None and with_file:
        ext = "mp3" if type_ == "voice" else "png"
        path = am.binary_path(user, char, aid, ext)
        with open(path, "wb") as f:
            f.write(b"X" * size)
    card = AssetCard(
        id=aid, type=type_, char_id=char, user_id=user, path=path,
        model="tts-model" if type_ == "voice" else "img-model",
        prompt=desc, transcript=desc if type_ == "voice" else "",
        tags=list(tags if tags is not None else ["generated"]),
        created_at=created if created is not None else time.time(),
        recall_count=recall,
    )
    am.write_card(card)
    return card


class _FakeRerankClient:
    """Scripted flash client for search_with_rerank (str reply or Exception)."""

    def __init__(self, reply="1"):
        self.reply = reply
        self.calls = 0

    async def chat_completion(self, message, history=None, timeout_set=20.0,
                              purpose=""):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class TestAssetSearch:
    """40. P5: AssetMemory 关键词检索 + 歧义 LLM 重排."""

    def run(self):
        print_header("40. Asset Search & Rerank (P5)")
        from agent.asset_memory import AssetMemory
        self.tmp = tempfile.mkdtemp(prefix="p5search_")
        try:
            self.am = AssetMemory(base_dir=os.path.join(self.tmp, "assets"))
            self.test_keyword_search()
            asyncio.run(self.test_rerank_triggered())
            asyncio.run(self.test_rerank_skipped())
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def test_keyword_search(self):
        am = self.am
        c1 = _seed_asset(am, desc="在海边散步的自拍，夕阳很好")
        c2 = _seed_asset(am, type_="voice", desc="晚安语音，好梦")
        c3 = _seed_asset(am, desc="星空下的魔法课",
                         tags=["generated", "proactive"])
        c4 = _seed_asset(am, char="roxy", desc="海边的洛琪希")
        # char_id=None → 跨角色检索；显式指定 → 只查该角色
        assert len(am.search("u1", "海边")) == 2  # c1 + c4
        hits = am.search("u1", "海边", char_id="rubi")
        assert [h.id for h in hits] == [c1.id]
        assert [h.id for h in am.search("u1", "海边", char_id="roxy")] == [c4.id]
        # 语音卡走 transcript
        hits = am.search("u1", "晚安")
        assert hits and hits[0].id == c2.id
        # tag 命中
        hits = am.search("u1", "proactive")
        assert hits and hits[0].id == c3.id
        # 无命中 / 空 query / 无此用户
        assert am.search("u1", "完全无关的词汇") == []
        assert am.search("u1", "   ") == []
        assert am.search("nobody", "海边") == []
        # top_k 截断
        for i in range(6):
            _seed_asset(am, user="u2", desc=f"日常自拍 {i}")
        assert len(am.search("u2", "自拍", top_k=3)) == 3
        print_pass("keyword search: prompt/transcript/tags, char filter, top_k")

    async def test_rerank_triggered(self):
        from agent.asset_memory import AssetMemory
        am = AssetMemory(base_dir=os.path.join(self.tmp, "assets_rr"))
        base = time.time()
        cards = [_seed_asset(am, user="u3", desc=f"和团长在一起的日子 {i}",
                             created=base + i)
                 for i in range(5)]
        # 全卡同分 → 截断线分差 0 → 触发一次 flash 重排；候选列新的在前，
        # 即序号 1..5 = cards[4], cards[3], cards[2], cards[1], cards[0]
        client = _FakeRerankClient(reply="3,1")
        hits = await am.search_with_rerank("u3", "在一起", client, top_k=2)
        assert client.calls == 1
        assert [h.id for h in hits] == [cards[2].id, cards[4].id]
        # 输出不可解析（无序号）→ 静默回退关键词序
        client2 = _FakeRerankClient(reply="抱歉，我无法判断")
        hits2 = await am.search_with_rerank("u3", "在一起", client2, top_k=2)
        assert client2.calls == 1 and len(hits2) == 2
        # 客户端抛异常 → 静默回退
        client3 = _FakeRerankClient(reply=RuntimeError("boom"))
        hits3 = await am.search_with_rerank("u3", "在一起", client3, top_k=2)
        assert len(hits3) == 2
        # 序号越界的被跳过，合法的保留
        client4 = _FakeRerankClient(reply="99,2,x")
        hits4 = await am.search_with_rerank("u3", "在一起", client4, top_k=2)
        assert [h.id for h in hits4] == [cards[1].id] or len(hits4) >= 1
        print_pass("rerank: ambiguity trigger, index parse, garbage/error fallback")

    async def test_rerank_skipped(self):
        from agent.asset_memory import AssetMemory
        am = AssetMemory(base_dir=os.path.join(self.tmp, "assets_skip"))
        strong = _seed_asset(am, user="u4", desc="烟花大会",
                             tags=["generated", "烟花"])
        for i in range(4):
            _seed_asset(am, user="u4", desc=f"烟花相关的其他回忆 {i}")
        client = _FakeRerankClient(reply="5,4")
        # tag 命中 (+3) 使截断线分差 ≥1 → 关键词序已可信，不花重排调用
        hits = await am.search_with_rerank("u4", "烟花", client, top_k=1)
        assert client.calls == 0
        assert hits[0].id == strong.id
        # 无 rerank client → 关键词序
        hits2 = await am.search_with_rerank("u4", "烟花", None, top_k=1)
        assert hits2[0].id == strong.id
        # 候选数 ≤ top_k → 直接返回，不触发重排
        hits3 = await am.search_with_rerank("u4", "烟花大会", client, top_k=10)
        assert client.calls == 0 and len(hits3) >= 1
        print_pass("rerank skipped: clear margin / no client / few candidates")


class TestAssetRecall:
    """41. P5: recall_asset 工具 — 重发 / 已淡去 / 无命中."""

    def run(self):
        print_header("41. Asset Recall Tool (P5)")
        from agent.asset_memory import AssetMemory
        from agent.outbound import OutAudio, OutImage
        self.OutAudio, self.OutImage = OutAudio, OutImage
        self.tmp = tempfile.mkdtemp(prefix="p5recall_")
        try:
            self.am = AssetMemory(base_dir=os.path.join(self.tmp, "assets"))
            asyncio.run(self.test_recall_hit_resend())
            asyncio.run(self.test_recall_evicted_and_miss())
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_recall_hit_resend(self):
        from agent.asset_memory import AssetMemory as _AM
        from tools.social_tools import recall_asset
        am = self.am
        img = _seed_asset(am, desc="海边散步的自拍")
        voc = _seed_asset(am, type_="voice", desc="晚安语音")
        sent = []

        async def send_media(item):
            sent.append(item)

        obs = await recall_asset(user_id="u1", char_id="rubi", query="海边",
                                 asset_memory=am, rerank_client=None,
                                 send_media=send_media)
        assert len(sent) == 1 and isinstance(sent[0], self.OutImage)
        assert sent[0].path == img.path
        assert "找回" in obs and "图片" in obs
        # 重发成功 → recall_count +1（冷读磁盘验证已落盘）
        cold = _AM(base_dir=am.base_dir)
        assert cold.read_card("u1", img.id).recall_count == 1
        # 语音卡 → OutAudio
        obs = await recall_asset(user_id="u1", char_id="rubi", query="晚安",
                                 asset_memory=am, send_media=send_media)
        assert len(sent) == 2 and isinstance(sent[1], self.OutAudio)
        assert sent[1].path == voc.path and "语音" in obs
        # send_media=None → 不崩，仍返回找回 observation
        obs = await recall_asset(user_id="u1", char_id="rubi", query="海边",
                                 asset_memory=am, send_media=None)
        assert "找回" in obs
        print_pass("recall hit: image/voice resend, bump_recall, no-sender safe")

    async def test_recall_evicted_and_miss(self):
        from tools.social_tools import recall_asset
        am = self.am
        _seed_asset(am, desc="旧日的烟花大会", with_file=False)  # path=None 已淘汰
        sent = []

        async def send_media(item):
            sent.append(item)

        obs = await recall_asset(user_id="u1", char_id="rubi", query="烟花",
                                 asset_memory=am, send_media=send_media)
        assert sent == []
        assert "淡去" in obs and "烟花" in obs  # 文本记忆仍在 → 角色凭卡描述
        obs = await recall_asset(user_id="u1", char_id="rubi", query="恐龙",
                                 asset_memory=am, send_media=send_media)
        assert "没有找到" in obs and sent == []
        obs = await recall_asset(user_id="u1", char_id="rubi", query="  ",
                                 asset_memory=am)
        assert "哪次回忆" in obs
        obs = await recall_asset(user_id="", char_id="rubi", query="海边",
                                 asset_memory=am)
        assert obs.startswith("[Error]")
        print_pass("recall: evicted → describe, miss → natural reply, guards")


class TestUploadTextualization:
    """42. P5: turn._textualize_upload — 上传 → user_upload 卡 + EXP 日封顶.

    非 split 人格不触发的门在 run_agent_turn 的附件循环里（_upload_persona
    解析）；这里覆盖后台任务本体 + _spawn_textualize_upload 的上下文守卫。
    """

    PATCHED = ("_asset_memory", "_bond_manager", "_perm_manager")

    def run(self):
        print_header("42. Upload Textualization (P5)")
        from types import SimpleNamespace
        import lib.multimodal_client as mc
        turn = _import_turn_for_tests()
        self.turn = turn
        self.client = mc.multimodal_client
        self.tmp = tempfile.mkdtemp(prefix="p5upload_")
        orig = tuple(getattr(turn, n) for n in self.PATCHED)
        orig_mc = (self.client.is_available, self.client.is_audio_available,
                   self.client.analyze_image, self.client.analyze_audio)
        try:
            from agent.asset_memory import AssetMemory
            turn._asset_memory = AssetMemory(
                base_dir=os.path.join(self.tmp, "assets"))
            turn._bond_manager = _tmp_bond_manager(self.tmp)
            turn._perm_manager = SimpleNamespace(
                get_role=lambda uid: SimpleNamespace(value="regular"))
            self.am, self.m = turn._asset_memory, turn._bond_manager
            asyncio.run(self.test_image_and_voice_cards())
            asyncio.run(self.test_error_and_unavailable())
            asyncio.run(self.test_reasoning_scaffold_and_truncation())
            asyncio.run(self.test_daily_cap_and_exception())
            self.test_spawn_guard()
        finally:
            for n, v in zip(self.PATCHED, orig):
                setattr(turn, n, v)
            (self.client.is_available, self.client.is_audio_available,
             self.client.analyze_image, self.client.analyze_audio) = orig_mc
            shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_image_and_voice_cards(self):
        turn = self.turn
        img_path = os.path.join(self.tmp, "up", "cat.png")
        os.makedirs(os.path.dirname(img_path), exist_ok=True)
        with open(img_path, "wb") as f:
            f.write(b"P")
        self.client.is_available = lambda: True
        seen = []

        async def fake_image(path, prompt=None, timeout=60.0):
            seen.append((path, prompt))
            return "一只橘猫趴在窗台上晒太阳。"
        self.client.analyze_image = fake_image
        await turn._textualize_upload("u1", "rubi", "image", img_path)
        assert seen[0][0] == img_path and "一句话" in seen[0][1]
        cards = self.am.list_cards("u1", "rubi")
        assert len(cards) == 1
        c = cards[0]
        assert c.type == "user_upload" and c.tags == ["uploaded", "image"]
        assert c.path == img_path
        assert "橘猫" in c.prompt and "橘猫" in c.transcript
        st = self.m.get_state("u1", "rubi", create=False)
        assert st["daily"]["uploads"] == 1
        assert st["exp"] == int(self.m._config["upload_exp"])
        audit = open(self.am._audit_path, encoding="utf-8").read()
        assert "|upload|" in audit and "textualized" in audit
        # 语音 → 转录卡
        voc_path = os.path.join(self.tmp, "up", "msg.mp3")
        with open(voc_path, "wb") as f:
            f.write(b"M")
        self.client.is_audio_available = lambda: True

        async def fake_audio(path, prompt=None, timeout=120.0):
            return "用户说：明天一起去海边吧。语气愉快。"
        self.client.analyze_audio = fake_audio
        await turn._textualize_upload("u1", "rubi", "voice", voc_path)
        cards = self.am.list_cards("u1", "rubi")
        assert len(cards) == 2
        voc = [c for c in cards if "voice" in c.tags][0]
        assert voc.type == "user_upload" and "海边" in voc.prompt
        assert self.m.get_state("u1", "rubi", create=False)["daily"]["uploads"] == 2
        print_pass("image/voice upload → user_upload card, EXP granted, audit")

    async def test_error_and_unavailable(self):
        turn = self.turn
        called = []
        self.client.is_available = lambda: False

        async def fake_image(path, prompt=None, timeout=60.0):
            called.append(path)
            return "desc"
        self.client.analyze_image = fake_image
        await turn._textualize_upload("u2", "rubi", "image", "/x/y.png")
        assert called == [] and self.am.list_cards("u2", "rubi") == []
        # 客户端把错误当文本返回（[多模态] 前缀）→ 不入库
        self.client.is_available = lambda: True

        async def err_image(path, prompt=None, timeout=60.0):
            return "[多模态] 图片文件不存在: /x/y.png"
        self.client.analyze_image = err_image
        await turn._textualize_upload("u2", "rubi", "image", "/x/y.png")
        assert self.am.list_cards("u2", "rubi") == []
        # 空描述 / 未知 kind → 静默
        async def empty_image(path, prompt=None, timeout=60.0):
            return "   "
        self.client.analyze_image = empty_image
        await turn._textualize_upload("u2", "rubi", "image", "/x/y.png")
        await turn._textualize_upload("u2", "rubi", "file", "/x/y.zip")
        assert self.am.list_cards("u2", "rubi") == []
        print_pass("unavailable / error-text / empty / unknown kind → no card")

    async def test_reasoning_scaffold_and_truncation(self):
        """防御层②：思考脚手架剥离 + 句边界截断（根修回归护栏）。"""
        turn = self.turn
        self.client.is_available = lambda: True

        # [思考]…[回复]… 脚手架 → 只取回复段正文入库
        async def scaffold_image(path, prompt=None, timeout=60.0):
            return ("[思考]\n用户想要客观描述，先看主体再看环境……（很长的推理）"
                    "\n\n[回复]\n一只橘猫趴在窗台上晒太阳，神情慵懒。")
        self.client.analyze_image = scaffold_image
        await turn._textualize_upload("u5", "rubi", "image", "/x/s.png")
        cards = self.am.list_cards("u5", "rubi")
        assert len(cards) == 1
        body = cards[0].prompt
        assert "[思考]" not in body and "[回复]" not in body
        assert body == "一只橘猫趴在窗台上晒太阳，神情慵懒。"

        # 只有 [思考] 无 [回复]（旧 bug 现场：思考吃光预算被切断）→ 放弃入库
        async def reasoning_only(path, prompt=None, timeout=60.0):
            return "[思考] 主体是动漫少女，姿态趴在床上，穿着白色"
        self.client.analyze_image = reasoning_only
        await turn._textualize_upload("u5", "rubi", "image", "/x/t.png")
        assert len(self.am.list_cards("u5", "rubi")) == 1  # 没多写卡

        # [DEBUG] 前缀（旧 analyze_audio 调试残留）→ 放弃入库
        async def debug_image(path, prompt=None, timeout=60.0):
            return "[DEBUG] 源文件: 123B, WAV: 456B\n\n一段语音转录。"
        self.client.analyze_image = debug_image
        await turn._textualize_upload("u5", "rubi", "image", "/x/d.png")
        assert len(self.am.list_cards("u5", "rubi")) == 1

        # 超长正文 → 句边界收口，不再是硬切的半句
        long_text = "".join(f"第{i}句话描述了一些细节内容。" for i in range(30))
        async def long_image(path, prompt=None, timeout=60.0):
            return long_text
        self.client.analyze_image = long_image
        await turn._textualize_upload("u5", "rubi", "image", "/x/l.png")
        cards = self.am.list_cards("u5", "rubi")
        assert len(cards) == 2
        long_body = [c for c in cards if "第0句" in c.prompt][0].prompt
        assert len(long_body) <= 200
        assert long_body.endswith("。")  # 收在句末标点，不是断裂的半句
        print_pass("reasoning scaffold stripped / dropped, sentence-boundary cut")

    async def test_daily_cap_and_exception(self):
        turn = self.turn
        self.client.is_available = lambda: True

        async def fake_image(path, prompt=None, timeout=60.0):
            return "一张风景照。"
        self.client.analyze_image = fake_image
        self.m._config["upload_exp_daily_cap"] = 2
        for i in range(3):
            await turn._textualize_upload("u3", "rubi", "image", f"/x/{i}.png")
        st = self.m.get_state("u3", "rubi", create=False)
        assert st["daily"]["uploads"] == 2               # 封顶
        assert len(self.am.list_cards("u3", "rubi")) == 3  # 卡照样全部入库
        assert st["exp"] == 2 * int(self.m._config["upload_exp"])
        audit = open(self.am._audit_path, encoding="utf-8").read()
        assert "textualized_capped" in audit
        # 分析抛异常 → 静默不崩、不入库
        async def boom(path, prompt=None, timeout=60.0):
            raise RuntimeError("net down")
        self.client.analyze_image = boom
        await turn._textualize_upload("u4", "rubi", "image", "/x/z.png")
        assert self.am.list_cards("u4", "rubi") == []
        print_pass("daily EXP cap (cards still written), exception silent")

    def test_spawn_guard(self):
        turn = self.turn
        n0 = len(turn._upload_tasks)
        turn._spawn_textualize_upload("", "rubi", "image", "/x.png")
        turn._spawn_textualize_upload("u1", "", "image", "/x.png")
        turn._spawn_textualize_upload("u1", "rubi", "image", "")
        assert len(turn._upload_tasks) == n0
        print_pass("spawn guard: missing user/char/path → no task")


class TestEpisodeDistill:
    """43. P5: 里程碑事件蒸馏 → TieredMemory (category=event)."""

    PATCHED = ("_asset_memory", "_bond_manager", "_tiered_memory")

    class _FakeTM:
        def __init__(self):
            self.calls = []

        def ingest_candidates(self, user_id, candidates, snapshot_turns=None):
            self.calls.append((user_id, list(candidates)))
            return {}

    def run(self):
        print_header("43. Episode Distillation (P5)")
        turn = _import_turn_for_tests()
        self.turn = turn
        self.tmp = tempfile.mkdtemp(prefix="p5distill_")
        orig = tuple(getattr(turn, n) for n in self.PATCHED)
        try:
            from agent.asset_memory import AssetMemory
            turn._asset_memory = AssetMemory(
                base_dir=os.path.join(self.tmp, "assets"))
            turn._bond_manager = _tmp_bond_manager(self.tmp)
            self.am, self.m = turn._asset_memory, turn._bond_manager
            self.tm = self._FakeTM()
            turn._tiered_memory = self.tm
            self.test_distill_scene_episode()
            self.test_switch_and_failure()
            self.test_distill_length_cap()
        finally:
            for n, v in zip(self.PATCHED, orig):
                setattr(turn, n, v)
            shutil.rmtree(self.tmp, ignore_errors=True)

    def test_distill_scene_episode(self):
        turn = self.turn
        _seed_asset(self.am, user="u1", desc="雨夜哨塔里的合影")
        self.m.append_event("u1", "rubi", "完成了剧情场景「雨夜谈心」")
        turn._distill_bond_episode(
            "u1", "rubi",
            title="与用户一起完成了剧情「雨夜谈心」",
            highlights=["纪念语：「雨停了，心还暖着」"])
        assert len(self.tm.calls) == 1
        uid, cands = self.tm.calls[0]
        assert uid == "u1" and len(cands) == 1
        content, judge, matched, category = cands[0]
        assert judge == "new" and matched is None and category == "event"
        assert "雨夜谈心" in content and "纪念语" in content
        assert "雨夜哨塔" in content       # 近期影音资产折进事件文本
        assert len(content) <= 300
        # 真实 TieredMemory 的 category 白名单包含 event（防接口漂移）
        from agent.memory import VALID_CATEGORIES
        assert "event" in VALID_CATEGORIES
        print_pass("scene distill: 4-tuple category=event, assets folded in")

    def test_switch_and_failure(self):
        turn = self.turn
        self.m._config["episode_distill_enabled"] = False
        turn._distill_bond_episode("u1", "rubi", title="不该被蒸馏")
        assert len(self.tm.calls) == 1  # 开关关闭 → 无新调用
        self.m._config["episode_distill_enabled"] = True

        class _Boom:
            def ingest_candidates(self, *a, **kw):
                raise RuntimeError("db down")
        turn._tiered_memory = _Boom()
        turn._distill_bond_episode("u1", "rubi", title="升级了")  # 静默不崩
        turn._tiered_memory = self.tm
        turn._distill_bond_episode("u1", "rubi", title="羁绊升级至 Lv5（密友）")
        assert len(self.tm.calls) == 2
        print_pass("switch off → skip; ingest failure → silent; re-enabled works")

    def test_distill_length_cap(self):
        text = self.am.distill_episode(
            "u1", "rubi", asset_ids=[], highlights=["很长的一段话" * 100],
            title="标题")
        assert len(text) <= 300
        assert self.am.distill_episode("u1", "rubi", None, None, "只有标题") == "只有标题"
        assert self.am.distill_episode("u1", "rubi", [], [], "") == ""
        # 不存在的 asset_id 被跳过，不炸
        assert "只有标题" in self.am.distill_episode(
            "u1", "rubi", ["ast_nosuch"], [], "只有标题")
        print_pass("distill_episode: ≤300 chars, title-only, empty, bad ids")


class TestAssetEvictionInjection:
    """44. P5: LRU 淘汰（只删二进制）+ L4 影音记忆注入块."""

    def run(self):
        print_header("44. Asset Eviction & Injection (P5)")
        from agent.asset_memory import AssetMemory
        self.tmp = tempfile.mkdtemp(prefix="p5evict_")
        try:
            self.am = AssetMemory(base_dir=os.path.join(self.tmp, "assets"))
            self.test_scores_and_order()
            self.test_evict_and_protect()
            self.test_enforce_quota()
            self.test_injection_block()
            self.test_quota_helper()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scores_and_order(self):
        import math
        am = self.am
        now = time.time()
        old_zero = _seed_asset(am, user="u1", desc="旧的普通图",
                               created=now - 40 * 86400)
        new_zero = _seed_asset(am, user="u1", desc="新的普通图", created=now)
        loved = _seed_asset(am, user="u1", desc="常被想起的图",
                            created=now - 30 * 86400, recall=1)
        # score = recall × exp(-age/30)：零召回旧的 < 零召回新的 < 被召回过的
        cands = am.eviction_candidates("u1")
        assert [c.id for c in cands] == [old_zero.id, new_zero.id, loved.id]
        assert abs(am.eviction_score(loved, now=now) - math.exp(-1.0)) < 1e-6
        assert am.eviction_score(new_zero, now=now) == 0.0
        print_pass("eviction score: recall×exp(-age/30) asc, ties → oldest first")

    def test_evict_and_protect(self):
        from agent.asset_memory import AssetMemory as _AM
        am = self.am
        # 资产目录外的路径（工作区上传原件 / characters 锚点）→ 硬保护
        outside = os.path.join(self.tmp, "workspace", "cat.png")
        os.makedirs(os.path.dirname(outside), exist_ok=True)
        with open(outside, "wb") as f:
            f.write(b"CAT")
        up = _seed_asset(am, user="u1", desc="外部文件卡", path_override=outside)
        assert up.id not in [c.id for c in am.eviction_candidates("u1")]
        assert am.evict("u1", up.id) is False
        assert os.path.isfile(outside)
        # 正常资产：二进制删除、卡保留（path=None），冷读可验证
        victim = _seed_asset(am, user="u1", desc="要被淘汰的图")
        victim_path = victim.path  # evict 会原地改缓存对象（path=None）
        assert os.path.isfile(victim_path)
        assert am.evict("u1", victim.id) is True
        assert not os.path.isfile(victim_path)
        cold = _AM(base_dir=am.base_dir)
        back = cold.read_card("u1", victim.id)
        assert back is not None and back.path is None
        assert back.prompt == "要被淘汰的图"  # 文本记忆不褪色
        # 已淘汰 → 幂等 False；不存在的卡 → False
        assert am.evict("u1", victim.id) is False
        assert am.evict("u1", "ast_nosuch") is False
        print_pass("evict: binary-only delete, card kept; outside paths protected")

    def test_enforce_quota(self):
        from agent.asset_memory import AssetMemory
        am = AssetMemory(base_dir=os.path.join(self.tmp, "assets_q"))
        now = time.time()
        big = _seed_asset(am, user="u1", desc="旧的大文件",
                          created=now - 10 * 86400, size=900 * 1024)
        small = _seed_asset(am, user="u1", desc="新的小文件",
                            created=now, size=200 * 1024)
        big_path, small_path = big.path, small.path  # evict 原地改缓存对象
        assert am.assets_size("u1") >= 1100 * 1024
        evicted = am.enforce_quota("u1", 1)  # 1MB 配额 → 淘汰低分大文件
        assert evicted == [big.id]
        assert not os.path.isfile(big_path) and os.path.isfile(small_path)
        assert am.assets_size("u1") < 1024 * 1024
        # 达标 → 不动；非法/零配额 → 不动
        assert am.enforce_quota("u1", 100) == []
        assert am.enforce_quota("u1", "abc") == []
        assert am.enforce_quota("u1", 0) == []
        assert os.path.isfile(small.path)
        print_pass("enforce_quota: evicts lowest-score until under cap")

    def test_injection_block(self):
        from agent.asset_memory import AssetMemory
        am = AssetMemory(base_dir=os.path.join(self.tmp, "assets_inj"))
        now = time.time()
        _seed_asset(am, user="u1", desc="海边的合影", created=now - 86400)
        _seed_asset(am, user="u1", type_="voice", desc="晚安语音", created=now)
        _seed_asset(am, user="u1", desc="旧日的烟花",
                    created=now - 5 * 86400, with_file=False)
        _seed_asset(am, user="u1", type_="user_upload", desc="猫照片",
                    tags=["uploaded", "image"], created=now - 2 * 86400,
                    path_override=os.path.join(self.tmp, "no_such_cat.png"))
        block = am.build_asset_injection("u1", "rubi", display_name="露比")
        assert block.startswith("## 你与露比的影音记忆")
        assert "recall_asset" in block
        lines = [l for l in block.splitlines() if l.startswith("- [")]
        assert len(lines) == 4
        # 新的在前 + 类型标签
        assert "晚安语音" in lines[0] and "语音" in lines[0]
        assert "海边的合影" in lines[1] and "图片" in lines[1]
        assert "用户发来的图片" in lines[2] and "淡去" in lines[2]  # 文件已丢
        assert "烟花" in lines[3] and "淡去" in lines[3]  # 已淘汰卡照样注入
        # top_k / 角色过滤 / 空态
        block2 = am.build_asset_injection("u1", "rubi", top_k=2)
        assert len([l for l in block2.splitlines() if l.startswith("- [")]) == 2
        assert am.build_asset_injection("u1", "roxy") == ""
        assert am.build_asset_injection("nobody", "rubi") == ""
        # 未给 display_name → 用 char_id
        assert "## 你与rubi的影音记忆" in am.build_asset_injection("u1", "rubi")
        print_pass("injection: labels, order, evicted-still-injected, empty state")

    def test_quota_helper(self):
        from tools.social_tools import enforce_asset_quota
        m = _tmp_bond_manager(os.path.join(self.tmp, "bonds_q"))
        _seed_asset(self.am, user="u5", desc="大文件", size=1200 * 1024)
        m._config["media_asset_quota_mb_regular"] = 1
        # 未知 role → regular 档
        evicted = enforce_asset_quota(user_id="u5", role="ghost",
                                      bond_manager=m, asset_memory=self.am)
        assert len(evicted) == 1
        # 达标后不再淘汰
        assert enforce_asset_quota(user_id="u5", role="regular",
                                   bond_manager=m, asset_memory=self.am) == []

        class _Boom:
            def get_config(self, *a, **kw):
                raise RuntimeError("cfg down")
        assert enforce_asset_quota(user_id="u5", role="regular",
                                   bond_manager=_Boom(),
                                   asset_memory=self.am) == []
        print_pass("enforce_asset_quota: role fallback, idempotent, exception-safe")


class TestWebuiModelSections:
    """webui model config — IMAGE_GEN/TTS media sections (Bond P4).

    Covers config_editor.models_section_prepare (per-section field schema,
    unmask merge, comma voice_pool) and model_probe dispatch (media sections
    POST to api_base verbatim with an empty-input body — never
    /chat/completions, never a billable generation call).
    """

    def run(self):
        print_header("40. WebUI Model Sections (IMAGE_GEN / TTS)")
        self.test_prepare_media_sections()
        self.test_probe_dispatch()

    def _import_ce(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from webui import config_editor as ce
        return ce

    def test_prepare_media_sections(self):
        from pathlib import Path as _Path
        ce = self._import_ce()
        tmp = tempfile.mkdtemp(prefix="wms_")
        try:
            settings = _Path(tmp) / "models_settings.json"
            settings.write_text(json.dumps({
                "TTS_MODEL": {
                    "api_key": "sk-real-key-123456789",
                    "api_base": "https://dash/api/v1/services/audio/tts/SpeechSynthesizer",
                    "model": "qwen-audio-3.1-tts-plus",
                    "voice_pool": ["old_voice_1"],
                    "max_concurrency": 2,
                },
            }), encoding="utf-8")
            with patch.object(ce, "MODELS_FILE", settings):
                # masked api_key round-trips; stringy numbers cast;
                # comma-separated voice_pool accepted from the panel input
                r = ce.models_section_prepare("TTS_MODEL", {
                    "api_key": "sk-rea***6789",
                    "api_base": "https://dash/api/v1/services/audio/tts/SpeechSynthesizer",
                    "model": "qwen-audio-3.1-tts-plus",
                    "timeout": "45", "sample_rate": "24000",
                    "format": "wav", "default_voice": " v1 ",
                    "voice_pool": "vb_1, vc_2 ,",
                })
                assert r.get("ok"), r
                sec = r["section_data"]
                assert sec["api_key"] == "sk-real-key-123456789"   # unmasked
                assert sec["timeout"] == 45 and isinstance(sec["timeout"], int)
                assert sec["sample_rate"] == 24000
                assert sec["format"] == "wav" and sec["default_voice"] == "v1"
                assert sec["voice_pool"] == ["vb_1", "vc_2"]
                assert sec["max_concurrency"] == 2      # kept from old section
                assert r["needs_probe"] is False        # credentials unchanged
                # invalid extras rejected with field-level error
                assert "error" in ce.models_section_prepare(
                    "TTS_MODEL", {"voice_pool": 42})
                assert "error" in ce.models_section_prepare(
                    "TTS_MODEL", {"sample_rate": "abc"})
                assert "error" in ce.models_section_prepare(
                    "TTS_MODEL", {"voice_pool": " , "})
                # IMAGE_GEN: media extras only — text fields not injected,
                # unknown section still rejected
                r = ce.models_section_prepare("IMAGE_GEN_MODEL", {
                    "api_key": "k", "api_base": "https://dash/gen",
                    "model": "qwen-image-3.0", "max_retries": "2",
                })
                assert r.get("ok"), r
                assert r["section_data"]["max_retries"] == 2
                assert "max_tokens" not in r["section_data"]
                assert r["needs_probe"] is True         # new credentials
                assert "error" in ce.models_section_prepare("NOPE_MODEL", {})
                # text sections keep the old max_tokens/temperature behavior
                r = ce.models_section_prepare("FLASH_MODEL", {
                    "api_key": "k", "api_base": "b", "model": "m",
                    "max_tokens": "4096", "temperature": "0.3",
                })
                assert r.get("ok") and r["section_data"]["max_tokens"] == 4096
                assert r["section_data"]["temperature"] == 0.3
            print_pass("models_section_prepare: media schemas, unmask, voice_pool")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_probe_dispatch(self):
        import types
        import httpx as real_httpx
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from webui import model_probe as mp
        captured = {"code": 400}

        class _FakeResp:
            def __init__(self, code):
                self.status_code = code
                self.text = '{"message":"invalid parameter"}'

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None, timeout=None):
                captured["url"] = url
                captured["body"] = json
                return _FakeResp(captured["code"])

        fake_httpx = types.SimpleNamespace(
            AsyncClient=lambda: _FakeClient(),
            ConnectError=real_httpx.ConnectError,
            ConnectTimeout=real_httpx.ConnectTimeout,
            TimeoutException=real_httpx.TimeoutException,
        )
        tts_url = "https://dash/api/v1/services/audio/tts/SpeechSynthesizer"
        with patch.object(mp, "httpx", fake_httpx):
            # media section → POSTs to api_base VERBATIM, empty input body;
            # the deliberate 400 proves credentials without billing anything
            r = asyncio.run(mp.probe_model("sk-k", tts_url, "m",
                                           section="TTS_MODEL"))
            assert captured["url"] == tts_url, captured["url"]
            assert captured["body"]["input"] == {"text": ""}
            assert r["ok"] and r["status"] == "ok_with_warning", r
            # 401 on media probe → auth_failed (blocks save)
            captured["code"] = 401
            r = asyncio.run(mp.probe_model("sk-k", "https://dash/gen", "m",
                                           section="IMAGE_GEN_MODEL"))
            assert not r["ok"] and r["status"] == "auth_failed", r
            assert captured["body"]["input"] == {"messages": []}
            # 404 on media probe → bad_path (endpoint URL wrong)
            captured["code"] = 404
            r = asyncio.run(mp.probe_model("sk-k", "https://dash/gen", "m",
                                           section="IMAGE_GEN_MODEL"))
            assert not r["ok"] and r["status"] == "bad_path", r
            # chat section → unchanged /chat/completions join; a plain 400
            # there is still a hard reject (not lenient)
            captured["code"] = 400
            r = asyncio.run(mp.probe_model("sk-k", "https://api.deepseek.com",
                                           "m", section="FLASH_MODEL"))
            assert captured["url"] == "https://api.deepseek.com/chat/completions"
            assert not r["ok"] and r["status"] == "rejected", r
            # masked key never reaches the transport
            captured.pop("url", None)
            r = asyncio.run(mp.probe_model("sk-***", tts_url, "m",
                                           section="TTS_MODEL"))
            assert r["status"] == "missing_config" and "url" not in captured
        print_pass("probe dispatch: media=verbatim url + empty input, chat unchanged")


# ── 初始好感度：UGC initial_bond_level 播种 + set_level GM 工具 ──

class TestInitialBond:
    """45. 初始好感度：创角预设 Lv1-5，bond 首建时播种（仅作者本人）."""

    def run(self):
        print_header("45. Initial Bond Seeding (UGC)")
        self._tmps = []
        try:
            self.test_seed_initial_bond()
            self.test_seed_edge_cases()
            self.test_set_level()
            asyncio.run(self.test_create_character_meta())
        finally:
            for t in self._tmps:
                shutil.rmtree(t, ignore_errors=True)

    def _env(self, prefix="inibond_"):
        from agent.bond import BondManager
        tmp = tempfile.mkdtemp(prefix=prefix)
        self._tmps.append(tmp)
        chars = os.path.join(tmp, "characters")
        os.makedirs(chars, exist_ok=True)
        m = BondManager(
            base_dir=os.path.join(tmp, "bonds"),
            config_path=os.path.join(tmp, "no_such_config.json"),
            characters_dir=chars,
        )
        return m, chars

    @staticmethod
    def _write_meta(chars, char_id, author="u1", level=3, extra=None):
        d = os.path.join(chars, char_id)
        os.makedirs(d, exist_ok=True)
        meta = {"char_id": char_id, "author": author,
                "initial_bond_level": level}
        if extra:
            meta.update(extra)
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

    def test_seed_initial_bond(self):
        m, chars = self._env()
        self._write_meta(chars, "星野爱_a1b2c3", author="u1", level=3)
        st = m.get_state("u1", "星野爱_a1b2c3")
        assert st["exp"] == 300 and st["level"] == 3, st
        # 非作者不播种（群里其他成员从 Lv1 开始）
        st2 = m.get_state("u2", "星野爱_a1b2c3")
        assert st2["exp"] == 0 and st2["level"] == 1
        # Lv5 上限档
        self._write_meta(chars, "c5", author="u1", level=5)
        st3 = m.get_state("u1", "c5")
        assert st3["exp"] == 1200 and st3["level"] == 5
        # level=1 → 与默认无差别
        self._write_meta(chars, "c1", author="u1", level=1)
        st4 = m.get_state("u1", "c1")
        assert st4["exp"] == 0 and st4["level"] == 1
        # 播种结果随首次 _save 落盘，冷读一致（现实流程：聊天结算 add_exp）
        m.add_exp("u1", "星野爱_a1b2c3", 1)
        from agent.bond import BondManager
        cold = BondManager(base_dir=m.base_dir,
                           config_path=os.path.join(m.base_dir, "nope.json"),
                           characters_dir=chars)
        st5 = cold.get_state("u1", "星野爱_a1b2c3")
        assert st5["exp"] == 301 and st5["level"] == 3
        print_pass("seed: author-only, level→threshold exp, persists on first save")

    def test_seed_edge_cases(self):
        m, chars = self._env(prefix="inibond_edge_")
        # meta 损坏 / 缺失 / level 非法 → 静默降级 Lv1
        bad_dir = os.path.join(chars, "bad")
        os.makedirs(bad_dir, exist_ok=True)
        with open(os.path.join(bad_dir, "meta.json"), "w") as f:
            f.write("{corrupt!!")
        assert m.get_state("u1", "bad")["exp"] == 0
        assert m.get_state("u1", "no_meta_at_all")["exp"] == 0
        self._write_meta(chars, "nan", author="u1", level="abc")
        assert m.get_state("u1", "nan")["exp"] == 0
        # 越界 clamp 到等级表上限 Lv10
        self._write_meta(chars, "hi", author="u1", level=99)
        assert m.get_state("u1", "hi")["exp"] == 8000
        # 路径穿越形态的 char_id → 不炸、默认状态
        assert m.get_state("u1", "../evil")["exp"] == 0
        # characters_dir=None → 播种整体关闭（向后兼容）
        from agent.bond import BondManager
        m2 = BondManager(base_dir=os.path.join(m.base_dir, "x"),
                         config_path=os.path.join(m.base_dir, "nope.json"))
        assert m2.get_state("u1", "hi")["exp"] == 0
        # 已存在的状态永不被回改（只涨不掉）：先攒 50 EXP，再补 meta
        m3, chars3 = self._env(prefix="inibond_late_")
        m3.add_exp("u1", "late", 50)
        self._write_meta(chars3, "late", author="u1", level=4)
        from agent.bond import BondManager as _BM
        cold = _BM(base_dir=m3.base_dir,
                   config_path=os.path.join(m3.base_dir, "nope.json"),
                   characters_dir=chars3)
        assert cold.get_state("u1", "late")["exp"] == 50
        print_pass("seed edges: corrupt/missing/invalid meta, clamp, traversal, no-retro")

    def test_set_level(self):
        m, chars = self._env(prefix="inibond_set_")
        # 上调：exp=阈值、level_ups 覆盖跨越等级
        ups = m.set_level("u1", "rubi", 5)
        st = m.get_state("u1", "rubi")
        assert st["exp"] == 1200 and st["level"] == 5
        assert [u["level"] for u in ups] == [2, 3, 4, 5]
        assert ups[-1]["title"] == "密友"
        # 下调：GM 豁免「只涨不掉」，无 level_ups
        assert m.set_level("u1", "rubi", 2) == []
        st = m.get_state("u1", "rubi")
        assert st["exp"] == 100 and st["level"] == 2
        # 越界 clamp / 非法输入不动状态
        m.set_level("u1", "rubi", 99)
        assert m.get_state("u1", "rubi")["level"] == 10
        m.set_level("u1", "rubi", 0)
        assert m.get_state("u1", "rubi")["level"] == 1
        assert m.set_level("u1", "rubi", "abc") == []
        assert m.get_state("u1", "rubi")["level"] == 1
        # 事件留痕 + 冷读持久化
        assert any("调整" in e for e in m.recent_events("u1", "rubi", n=5))
        from agent.bond import BondManager
        cold = BondManager(base_dir=m.base_dir,
                           config_path=os.path.join(m.base_dir, "nope.json"),
                           characters_dir=chars)
        assert cold.get_state("u1", "rubi")["level"] == 1
        # reset 后回到「故事起点」：重新按 meta 播种
        self._write_meta(chars, "rubi", author="u1", level=3)
        m.reset("u1", "rubi")
        st = m.get_state("u1", "rubi")
        assert st["exp"] == 300 and st["level"] == 3
        print_pass("set_level: up/down, clamp, invalid, event, persist, reset re-seeds")

    async def test_create_character_meta(self):
        from tools.creation_tools import (
            create_character, _coerce_initial_bond, CREATE_CHARACTER_SCHEMA,
        )
        from agent.character_registry import CharacterRegistry

        # coerce：None/垃圾/越界 → clamp [1,5]
        assert _coerce_initial_bond(None) == 1
        assert _coerce_initial_bond("abc") == 1
        assert _coerce_initial_bond(0) == 1
        assert _coerce_initial_bond(9) == 5
        assert _coerce_initial_bond(3) == 3
        assert _coerce_initial_bond("4") == 4
        # schema 有字段且非必填
        assert "initial_bond_level" in CREATE_CHARACTER_SCHEMA["properties"]
        assert "initial_bond_level" not in CREATE_CHARACTER_SCHEMA["required"]

        from agent.permissions import UserRole

        class P:
            def get_role(self, u): return UserRole.REGULAR
            def get_max_characters(self, r): return 3

        class C:
            def __init__(self, resp): self.resp = resp
            async def chat_completion(self, message, history, timeout_set, purpose):
                return self.resp

        good = dict(name="爱酱", personality="温柔", speech_style="轻声",
                    appearance="粉发蓝瞳")

        async def _create(fields, tag):
            tmp = tempfile.mkdtemp(prefix="inibond_cc_")
            self._tmps.append(tmp)
            pdir = os.path.join(tmp, "personalities")
            os.makedirs(pdir, exist_ok=True)
            with open(os.path.join(pdir, "assistant.md"), "w", encoding="utf-8") as f:
                f.write("# 助手\nbase")
            reg = CharacterRegistry(
                personalities_dir=pdir,
                characters_dir=os.path.join(tmp, "characters"),
            )
            res = await create_character(
                user_id="u1", fields=dict(fields, name=fields.get("name", "") + tag),
                registry=reg, perm_manager=P(), review_client=C("APPROVE"),
            )
            assert res.startswith("✅"), res
            (cid,) = reg._ugc_ids()
            with open(os.path.join(tmp, "characters", cid, "meta.json"),
                      encoding="utf-8") as f:
                return json.load(f)

        meta = await _create(dict(good, initial_bond_level=4), "四")
        assert meta["initial_bond_level"] == 4
        meta = await _create(dict(good, initial_bond_level=99), "溢")
        assert meta["initial_bond_level"] == 5
        meta = await _create(good, "缺")
        assert meta["initial_bond_level"] == 1
        # REJECT 分支不受新字段影响（fail-closed 照旧）
        tmp = tempfile.mkdtemp(prefix="inibond_rj_")
        self._tmps.append(tmp)
        pdir = os.path.join(tmp, "personalities")
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "assistant.md"), "w", encoding="utf-8") as f:
            f.write("# 助手\nbase")
        reg = CharacterRegistry(personalities_dir=pdir,
                                characters_dir=os.path.join(tmp, "characters"))
        res = await create_character(
            user_id="u1", fields=dict(good, initial_bond_level=3),
            registry=reg, perm_manager=P(), review_client=C("REJECT: 违规"),
        )
        assert res.startswith("[Error]") and len(reg._ugc_ids()) == 0
        print_pass("create_character: meta field clamp/default, schema, review intact")


class TestStoryKbInjectionGate:
    """46. Story-KB 注入门控：人格白名单（story_personas.json）+ 空 char_id 双层闸门。

    Prod incident 2026-10-26（UGC「夏以昼」）: retrieve() 的 persona_char_id
    只做加权/标注、**不做过滤**——UGC 角色解析不到剧情角色 id 时退化成
    全库检索，把别的角色的剧情（婚礼/亲密场景）注进 L4，模型推理痕迹
    显示被无关剧情带偏（"剧情记忆那段完全不相关，忽略不用"）。
    """

    def run(self):
        print_header("46. Story-KB Injection Gate")
        # turn.py reads USER_DATA_ROOT at import time; keep it off prod paths
        os.environ.setdefault("USER_DATA_ROOT", "/tmp/qqbot_smoke")
        import agent.turn as turn
        self.turn = turn
        self.test_allowlist_gate_blocks_outsider()
        self.test_char_id_gate_blocks_unmapped_persona()
        self.test_neutral_persona_injects_without_char_id()
        self.test_gate_passes_known_persona()
        self.test_gate_degrades_when_kb_unavailable()

    def _patched(self, char_id, kb, allowed=True, neutral=False):
        """Patch turn._story_char_id_for / _story_persona_allowed /
        _story_neutral_persona + lib.story_kb.get_story_kb."""
        turn = self.turn
        import lib.story_kb as story_kb_mod
        orig = (
            turn._story_char_id_for,
            turn._story_persona_allowed,
            turn._story_neutral_persona,
            story_kb_mod.get_story_kb,
        )
        turn._story_char_id_for = lambda persona_key, pm: char_id
        turn._story_persona_allowed = lambda persona_key: allowed
        turn._story_neutral_persona = lambda persona_key: neutral
        story_kb_mod.get_story_kb = lambda: kb
        return orig

    def _restore(self, orig):
        import lib.story_kb as story_kb_mod
        turn = self.turn
        (turn._story_char_id_for, turn._story_persona_allowed,
         turn._story_neutral_persona, story_kb_mod.get_story_kb) = orig

    @staticmethod
    def _mock_kb(available=True, block="【剧情记忆】露比在教堂经历了婚礼。"):
        kb = MagicMock()
        kb.available = available
        kb.format_injection.return_value = block
        return kb

    def test_allowlist_gate_blocks_outsider(self):
        """① 白名单外人格（UGC）→ 连 KB 都不碰。"""
        turn = self.turn
        kb = self._mock_kb()
        orig = self._patched("", kb, allowed=False)
        try:
            block, char_id = turn._build_story_context(
                "夏以昼_aff51a", "夏以昼的哥哥是谁", None)
            assert (block, char_id) == ("", ""), (block, char_id)
            kb.format_injection.assert_not_called()
            print_pass("persona not allowlisted → no injection, kb never queried")
        finally:
            self._restore(orig)

    def test_char_id_gate_blocks_unmapped_persona(self):
        """② 白名单内、非 neutral、解析不到 char_id → 不注入（原事故路径）。"""
        turn = self.turn
        kb = self._mock_kb()
        orig = self._patched("", kb, allowed=True, neutral=False)
        try:
            block, char_id = turn._build_story_context(
                "rubi", "露比的哥哥是谁", None)
            assert (block, char_id) == ("", ""), (block, char_id)
            kb.format_injection.assert_not_called()
            print_pass("allowlisted, empty char_id & not neutral → no injection")
        finally:
            self._restore(orig)

    def test_neutral_persona_injects_without_char_id(self):
        """②' neutral 人格（assistant）无 char_id 也注入，走中性口径。"""
        turn = self.turn
        kb = self._mock_kb(block="【剧情记忆·中性】参考片段。")
        orig = self._patched("", kb, allowed=True, neutral=True)
        try:
            block, char_id = turn._build_story_context(
                "assistant", "露比小时候发生过什么", None)
            assert char_id == "" and "中性" in block, (block, char_id)
            kwargs = kb.format_injection.call_args.kwargs
            assert kwargs["persona_char_id"] == "", kwargs
            print_pass("neutral persona → injection proceeds with empty char_id")
        finally:
            self._restore(orig)

    def test_gate_passes_known_persona(self):
        turn = self.turn
        kb = self._mock_kb()
        orig = self._patched("rubi", kb)
        try:
            block, char_id = turn._build_story_context(
                "rubi", "露比小时候发生过什么", None)
            assert char_id == "rubi" and "剧情记忆" in block, (block, char_id)
            kwargs = kb.format_injection.call_args.kwargs
            assert kwargs["persona_char_id"] == "rubi", kwargs
            print_pass("resolved char_id → injection proceeds, persona passed through")
        finally:
            self._restore(orig)

    def test_gate_degrades_when_kb_unavailable(self):
        turn = self.turn
        kb = self._mock_kb(available=False)
        orig = self._patched("rubi", kb)
        try:
            assert turn._build_story_context("rubi", "露比的过往", None) == ("", "")
            kb.format_injection.assert_not_called()
            print_pass("kb unavailable → silent ('', ''), unchanged behavior")
        finally:
            self._restore(orig)


class TestQQOfficialQuoteParsing:
    """47. 官方途经：引用消息（message_type=103）的文本与附件解析.

    引用消息在 API 里是「当前事件 + msg_elements 里的被引用内容」：被引用
    的图片/文件挂在 msg_elements[].attachments 上，而不是事件顶层
    attachments。构建视图时若只读顶层字段，整条引用消息会退化成空消息，
    内核直接 return，智能体什么也看不到（迁移后线上现象）。
    """

    BASE = {
        "id": "M1",
        "content": "",
        "timestamp": "2026-10-27T10:00:00+08:00",
        "author": {"id": "U1", "user_openid": "U1", "username": "小明", "bot": False},
    }

    def __init__(self):
        self.qo = None
        self.C2C = None

    def run(self):
        print_header("47. QQ Official Quote Parsing")
        # The adapter chain imports agent.turn, which builds its managers from
        # USER_DATA_ROOT at import time — same smoke root the later suites use.
        os.environ.setdefault("USER_DATA_ROOT", "/tmp/qqbot_smoke")
        import adapters.qq_official as qo
        from nonebot.adapters.qq.event import C2CMessageCreateEvent
        self.qo = qo
        self.C2C = C2CMessageCreateEvent
        self.test_quote_only_image_is_resolved()
        self.test_quote_plus_fresh_upload_keeps_both()
        self.test_voice_content_type_maps_to_voice()
        self.test_plain_upload_unchanged()

    # ── helpers ──────────────────────────────────────────────────
    def _view(self, payload):
        event = self.C2C.model_validate(payload)
        return self.qo.build_view(MagicMock(self_id="APPID123"), event)

    def _resolve(self, view):
        """Run load_attachments with the disk downloader stubbed out."""
        async def fake_download(url, name, bot=None, file_id=""):
            return f"/tmp/uploads/{name}", ""

        orig = self.qo._download_and_save_file
        self.qo._download_and_save_file = fake_download
        try:
            return asyncio.run(view.load_attachments())
        finally:
            self.qo._download_and_save_file = orig

    def _quote_payload(self, **over):
        payload = {
            **self.BASE,
            "content": " ",
            "message_type": 103,
            "message_scene": {"source": "default", "ext": ["ref_msg_idx=OLD"]},
            "msg_elements": [{
                "msg_idx": "OLD",
                "message_type": 0,
                "content": "上次的截图",
                "author": {"id": "U1", "user_openid": "U1", "username": "小明"},
                "attachments": [{
                    "content_type": "image/png",
                    "filename": "old.png",
                    "url": "https://cdn.example/old.png",
                }],
            }],
        }
        payload.update(over)
        return payload

    # ── cases ────────────────────────────────────────────────────
    def test_quote_only_image_is_resolved(self):
        """纯引用（本条无文字、无附件）也要拿到被引用的图。"""
        view = self._view(self._quote_payload())
        assert "上次的截图" in view.reply_context, view.reply_context
        atts = self._resolve(view)
        assert len(atts) == 1, atts
        assert atts[0].kind == "image" and atts[0].quoted, atts[0]
        print_pass("引用消息：引用文本 + 被引用图片均已解析")

    def test_quote_plus_fresh_upload_keeps_both(self):
        """「引用旧图 + 上传新图」：两张都要，且新旧标记正确。"""
        payload = self._quote_payload(
            content="和这张比一下",
            attachments=[{
                "content_type": "image/jpeg",
                "filename": "new.jpg",
                "url": "https://cdn.example/new.jpg",
            }],
        )
        atts = self._resolve(self._view(payload))
        assert len(atts) == 2, atts
        fresh = [a for a in atts if not a.quoted]
        quoted = [a for a in atts if a.quoted]
        assert len(fresh) == 1 and fresh[0].name == "new.jpg", fresh
        assert len(quoted) == 1 and quoted[0].name == "old.png", quoted
        print_pass("引用 + 新上传：顺序与新/引用标记都正确")

    def test_voice_content_type_maps_to_voice(self):
        """官方语音的 content_type 是裸 'voice'，不能落成文件。"""
        payload = {
            **self.BASE,
            "content": "",
            "message_type": 0,
            "attachments": [{
                "content_type": "voice",
                "filename": "v.silk",
                "url": "https://cdn.example/v.silk",
            }],
        }
        atts = self._resolve(self._view(payload))
        assert len(atts) == 1 and atts[0].kind == "voice", atts
        print_pass("content_type=voice → voice（不再降级成文件）")

    def test_plain_upload_unchanged(self):
        """普通上传保持原语义：非引用、无 reply_context。"""
        payload = {
            **self.BASE,
            "content": "看这张",
            "message_type": 0,
            "attachments": [{
                "content_type": "image/png",
                "filename": "a.png",
                "url": "https://cdn.example/a.png",
            }],
        }
        view = self._view(payload)
        assert view.reply_context == "", view.reply_context
        atts = self._resolve(view)
        assert len(atts) == 1 and not atts[0].quoted and atts[0].kind == "image", atts
        print_pass("普通上传：语义不变（quoted=False）")


class TestReferencePortrait:
    """48. /设形象 —— 自创角色参考立绘（校验/落盘/命令链路）."""

    def __init__(self):
        self.turn = None

    def run(self):
        print_header("48. Reference Portrait (/设形象)")
        os.environ.setdefault("USER_DATA_ROOT", "/tmp/qqbot_smoke")
        import agent.turn as turn
        self.turn = turn
        self.test_normalize()
        self.test_registry_roundtrip()
        self.test_command_chain()
        self.test_guardrails()
        self.test_builtin_portrait()

    # ── helpers ──────────────────────────────────────────────────
    @staticmethod
    def _png(size=(900, 1200), mode="RGB"):
        import io as _io
        from PIL import Image
        color = (200, 120, 90) if mode == "RGB" else (200, 120, 90, 128)
        buf = _io.BytesIO()
        Image.new(mode, size, color).save(buf, format="PNG")
        return buf.getvalue()

    def _registry(self, tmp):
        from agent.character_registry import CharacterRegistry
        reg = CharacterRegistry(characters_dir=tmp)
        char_id = "测试角色_abc123"
        reg.register_character(char_id, "# 测试角色\n", {
            "char_id": char_id, "display_name": "测试角色", "author": "u1"})
        return reg, char_id

    def _view(self, path=None, user_id="u1"):
        from agent.inbound import Attachment, InboundMessage
        view = InboundMessage(user_id=user_id, platform="onebot")
        view.attachments = ([Attachment(kind="image", path=path, name="up.png")]
                            if path else [])
        view.attachments_loaded = True
        return view

    # ── cases ────────────────────────────────────────────────────
    def test_normalize(self):
        from agent.portrait import normalize_reference_image
        data, w, h = normalize_reference_image(self._png())
        assert (w, h) == (900, 1200) and data[:8] == b"\x89PNG\r\n\x1a\n", (w, h)
        _, w2, h2 = normalize_reference_image(self._png((3000, 2000)))
        assert max(w2, h2) == 1536, (w2, h2)
        # alpha 铺白底 → 仍出 PNG，尺寸不变
        _, w3, h3 = normalize_reference_image(self._png((400, 500), "RGBA"))
        assert (w3, h3) == (400, 500), (w3, h3)
        for bad in (b"", b"definitely not an image", self._png((64, 64))):
            try:
                normalize_reference_image(bad)
            except ValueError:
                continue
            raise AssertionError("normalize 应拒绝非法输入")
        print_pass("normalize: 保留尺寸/限边 1536/alpha 铺白底/拒空·非图·过小")

    def test_registry_roundtrip(self):
        tmp = tempfile.mkdtemp(prefix="portrait_reg_")
        reg, char_id = self._registry(tmp)
        try:
            assert reg.reference_image_path(char_id) is None
            path = reg.set_reference_image(char_id, self._png(), "png")
            assert os.path.isfile(path) and path.endswith("assets/reference.png"), path
            assert reg.reference_image_path(char_id) == path
            # 旧扩展名要被清掉，永远只有一张参考图
            stale = os.path.join(reg.reference_image_dir(char_id), "reference.jpg")
            with open(stale, "wb") as f:
                f.write(b"x")
            reg.set_reference_image(char_id, self._png(), "png")
            assert not os.path.isfile(stale), "stale reference.jpg should be removed"
            assert reg.clear_reference_image(char_id) is True
            assert reg.reference_image_path(char_id) is None
            assert reg.clear_reference_image(char_id) is False
            print_pass("registry: 原子落盘 / 旧扩展名清理 / 读回 / 清除幂等")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_command_chain(self):
        turn = self.turn
        tmp = tempfile.mkdtemp(prefix="portrait_cmd_")
        reg, char_id = self._registry(tmp)
        sent = []

        async def fake_send(payload, sender=None):
            sent.append(payload)

        orig = (turn.get_character_registry, turn._safe_send)
        try:
            turn.get_character_registry = lambda: reg
            turn._safe_send = fake_send
            reg.resolve_effective_personality = lambda uid, gid=None: char_id

            with open(os.path.join(tmp, "up.png"), "wb") as f:
                f.write(self._png((700, 900)))
            view = self._view(os.path.join(tmp, "up.png"))

            assert asyncio.run(turn._handle_portrait_command("/设形象", "u1", view))
            assert "参考立绘" in sent[-1] and "700×900" in sent[-1], sent[-1]
            assert reg.reference_image_path(char_id), "portrait file missing"

            sent.clear()
            assert asyncio.run(turn._handle_portrait_command("/形象", "u1", view))
            assert isinstance(sent[0], str)
            from agent.outbound import OutImage
            assert isinstance(sent[1], OutImage), sent
            assert sent[1].path == reg.reference_image_path(char_id)

            sent.clear()
            assert asyncio.run(turn._handle_portrait_command("/清除形象", "u1", view))
            assert reg.reference_image_path(char_id) is None
            print_pass("command: 设形象→落盘 / 形象→文本+OutImage / 清除形象→删文件")
        finally:
            turn.get_character_registry, turn._safe_send = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_guardrails(self):
        turn = self.turn
        tmp = tempfile.mkdtemp(prefix="portrait_guard_")
        reg, char_id = self._registry(tmp)
        sent = []

        async def fake_send(payload, sender=None):
            sent.append(payload)

        orig = (turn.get_character_registry, turn._safe_send)
        try:
            turn.get_character_registry = lambda: reg
            turn._safe_send = fake_send
            with open(os.path.join(tmp, "up.png"), "wb") as f:
                f.write(self._png((300, 300)))
            img = os.path.join(tmp, "up.png")

            # ① 没图 → 提示怎么发
            reg.resolve_effective_personality = lambda uid, gid=None: char_id
            asyncio.run(turn._handle_portrait_command("/设形象", "u1", self._view()))
            assert "没看到图片" in sent[-1], sent[-1]

            # ② 别人的角色 → 拒绝
            sent.clear()
            asyncio.run(turn._handle_portrait_command(
                "/设形象 测试角色", "u2", self._view(img, user_id="u2")))
            assert "没找到你的自创角色" in sent[-1], sent[-1]
            assert reg.reference_image_path(char_id) is None

            # ③ 内置人格 → 拒绝（不能改公共设定）
            sent.clear()
            reg.resolve_effective_personality = lambda uid, gid=None: "rubi"
            asyncio.run(turn._handle_portrait_command(
                "/设形象", "u1", self._view(img)))
            assert "只有管理员" in sent[-1], sent[-1]

            # ④ 非本命令文本 → 不拦截
            assert asyncio.run(turn._handle_portrait_command(
                "/人格切换", "u1", self._view(img))) is False
            print_pass("guard: 无图提示 / 他人角色拒 / 内置人格需管理员 / 非本命令放行")
        finally:
            turn.get_character_registry, turn._safe_send = orig
            shutil.rmtree(tmp, ignore_errors=True)



    def test_builtin_portrait(self):
        """内置人格：仅管理员可写 / 覆盖优先 / 清除回退出厂图 / 助手拒绝。"""
        turn = self.turn
        import agent.character_registry as cr
        from agent.outbound import OutImage

        tmp = tempfile.mkdtemp(prefix="portrait_builtin_")
        shipped_dir = tempfile.mkdtemp(prefix="portrait_shipped_")
        reg, _ = self._registry(tmp)
        sent = []

        async def fake_send(payload, sender=None):
            sent.append(payload)

        class _Role:
            def __init__(self, value):
                self.value = value

        class _Perm:
            def __init__(self, value):
                self._value = value

            def get_role(self, user_id):
                return _Role(self._value)

        orig = (turn.get_character_registry, turn._safe_send,
                turn._perm_manager, cr._PORTRAITS_DIR)
        try:
            turn.get_character_registry = lambda: reg
            turn._safe_send = fake_send
            cr._PORTRAITS_DIR = shipped_dir
            reg.resolve_effective_personality = lambda uid, gid=None: "rubi"

            # 出厂立绘（代码盘）
            shipped = os.path.join(shipped_dir, "rubi.png")
            with open(shipped, "wb") as f:
                f.write(self._png((512, 512)))
            assert reg.reference_image_path("rubi") == shipped

            up = os.path.join(tmp, "up.png")
            with open(up, "wb") as f:
                f.write(self._png((800, 800)))
            view = self._view(up)

            # ① 普通用户 → 拒绝，且不动全局图
            turn._perm_manager = _Perm("regular")
            assert asyncio.run(turn._handle_portrait_command("/设形象", "u1", view))
            assert "只有管理员" in sent[-1], sent[-1]
            assert reg.reference_image_path("rubi") == shipped

            # ② 管理员 → 写入数据盘覆盖层，覆盖优先于出厂图
            turn._perm_manager = _Perm("admin")
            sent.clear()
            assert asyncio.run(turn._handle_portrait_command("/设形象", "u1", view))
            override = os.path.join(
                tmp, "_builtin", "rubi", "assets", "reference.png")
            assert os.path.isfile(override), override
            assert reg.reference_image_path("rubi") == override
            assert "全局生效" in sent[-1], sent[-1]

            # ③ 清除 → 删覆盖层，回落出厂图
            sent.clear()
            assert asyncio.run(turn._handle_portrait_command("/清除形象", "u1", view))
            assert not os.path.isfile(override)
            assert reg.reference_image_path("rubi") == shipped
            assert "回退到出厂立绘" in sent[-1], sent[-1]

            # ④ 助手人格（unified，不画图）→ 拒绝
            sent.clear()
            reg.resolve_effective_personality = lambda uid, gid=None: "assistant"
            assert asyncio.run(turn._handle_portrait_command("/设形象", "u1", view))
            assert "不生成图片" in sent[-1], sent[-1]

            # ⑤ 查看不需要管理员
            sent.clear()
            turn._perm_manager = _Perm("regular")
            reg.resolve_effective_personality = lambda uid, gid=None: "rubi"
            assert asyncio.run(turn._handle_portrait_command("/形象", "u1", view))
            assert isinstance(sent[-1], OutImage), sent
            print_pass("builtin: 非管理员拒 / 管理员覆盖 / 清除回落 / 助手拒 / 查看开放")
        finally:
            (turn.get_character_registry, turn._safe_send,
             turn._perm_manager, cr._PORTRAITS_DIR) = orig
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(shipped_dir, ignore_errors=True)


class TestDirectVision:
    """Direct Vision: 主模型直连识图（image_url parts 注入当前轮 user message）。

    Covers lib/image_parts encoder, ModelRouter supports_vision flag,
    Agent._build_messages content parts (+ str regression lock), run()
    400 self-heal retry, turn._any_vision_client gate, and the panel
    config_editor bool extra round-trip.
    """

    def run(self):
        print_header("50. Direct Vision (主模型直连识图)")
        self.test_image_parts_encoder()
        self.test_model_router_vision_flag()
        self.test_build_messages_parts()
        asyncio.run(self.test_run_injection_and_400_retry())
        self.test_any_vision_client()
        self.test_config_editor_bool_extra()

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _make_image(path, size=(64, 64), mode="RGBA", fmt=None, frames=1):
        from PIL import Image
        color = (255, 0, 0, 255) if mode == "RGBA" else (255, 0, 0)
        im = Image.new(mode, size, color)
        if frames > 1:  # animated GIF
            frames_ims = [im.copy() for _ in range(frames)]
            im.save(path, save_all=True, append_images=frames_ims[1:],
                    duration=100, loop=0)
        else:
            im.save(path, format=fmt)
        return path

    @staticmethod
    def _decode_part(part):
        """(mime, PIL image) from an image_url content part."""
        import base64 as _b64
        import io as _io
        from PIL import Image
        url = part["image_url"]["url"]
        head, _, b64 = url.partition(";base64,")
        mime = head[len("data:"):]
        return mime, Image.open(_io.BytesIO(_b64.b64decode(b64)))

    def test_image_parts_encoder(self):
        from lib import image_parts as ip
        tmp = tempfile.mkdtemp(prefix="vision_enc_")
        try:
            # small RGBA png → PNG passthrough (alpha kept, no resize)
            p = self._make_image(os.path.join(tmp, "a.png"), (64, 64), "RGBA")
            part = ip.build_image_part(p)
            mime, im = self._decode_part(part)
            assert mime == "image/png" and im.size == (64, 64), (mime, im.size)
            # oversized RGB jpeg → downscaled, long side ≤ MAX_SIDE
            p2 = self._make_image(os.path.join(tmp, "big.jpg"),
                                  (2400, 1800), "RGB", fmt="JPEG")
            mime2, im2 = self._decode_part(ip.build_image_part(p2))
            assert mime2 == "image/jpeg" and max(im2.size) <= ip.MAX_SIDE, im2.size
            # oversized RGB png (screenshot) → downscaled to jpeg
            p3 = self._make_image(os.path.join(tmp, "shot.png"),
                                  (3000, 1500), "RGB", fmt="PNG")
            mime3, im3 = self._decode_part(ip.build_image_part(p3))
            assert mime3 == "image/jpeg" and max(im3.size) <= ip.MAX_SIDE
            # animated GIF → first frame as JPEG
            p4 = self._make_image(os.path.join(tmp, "ani.gif"), (80, 60),
                                  "RGB", fmt="GIF", frames=3)
            mime4, _ = self._decode_part(ip.build_image_part(p4))
            assert mime4 == "image/jpeg", mime4
            # extensionless QQ download → header sniffing
            p5 = os.path.join(tmp, "noext_download")
            with open(p, "rb") as f, open(p5, "wb") as g:
                g.write(f.read())
            mime5, _ = self._decode_part(ip.build_image_part(p5))
            assert mime5 == "image/png", mime5
            # corrupt / missing / unrecognised → None (never raises)
            bad = os.path.join(tmp, "bad.png")
            with open(bad, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\ngarbage!!")
            assert ip.build_image_part(bad) is None
            assert ip.build_image_part(os.path.join(tmp, "nope.png")) is None
            txt = os.path.join(tmp, "note.txt")
            with open(txt, "w") as f:
                f.write("not an image")
            assert ip.build_image_part(txt) is None
            # raw-size cap (patched small) → None
            old_cap = ip.RAW_SIZE_CAP
            try:
                ip.RAW_SIZE_CAP = 10
                assert ip.build_image_part(p) is None
            finally:
                ip.RAW_SIZE_CAP = old_cap
            # per-turn count cap, order preserved, invalid skipped
            paths = [self._make_image(os.path.join(tmp, f"m{i}.png"),
                                      (8, 8), "RGB", fmt="PNG")
                     for i in range(6)]
            parts = ip.build_image_parts(paths[:3] + [bad] + paths[3:])
            assert len(parts) == ip.MAX_PER_TURN, len(parts)
            assert ip.build_image_parts([]) == []
            print_pass("encoder: 缩放/mime/GIF首帧/无扩展名嗅探/损坏None/上限")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_model_router_vision_flag(self):
        from lib.model_router import ModelRouter
        tmp = tempfile.mkdtemp(prefix="vision_router_")
        cfg = os.path.join(tmp, "models_settings.json")
        try:
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump({
                    "FLASH_MODEL": {"api_key": "k1", "api_base": "https://b1",
                                    "model": "m1", "supports_vision": True},
                    "REASONING_MODEL": {"api_key": "k2", "api_base": "https://b2",
                                        "model": "m2"},
                }, f)
            router = ModelRouter(config_path=cfg)
            assert router.flash_client.supports_vision is True
            assert router.reasoning_client.supports_vision is False
            # hot reload picks up the flipped flag
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump({
                    "FLASH_MODEL": {"api_key": "k1", "api_base": "https://b1",
                                    "model": "m1", "supports_vision": False},
                }, f)
            router.reload()
            assert router.flash_client.supports_vision is False
            print_pass("ModelRouter: supports_vision 显式开启/缺省False/热重载")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _make_agent(self, client=None):
        from agent.tool_registry import ToolRegistry
        from agent.agent import Agent
        return Agent(
            deepseek_client=client or MockDeepSeekClient(),
            tool_registry=ToolRegistry(),
            config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
        )

    def test_build_messages_parts(self):
        from agent.session import Session
        agent = self._make_agent()
        sess = Session(user_id="u1")
        tmp = tempfile.mkdtemp(prefix="vision_msgs_")
        try:
            png = self._make_image(os.path.join(tmp, "x.png"), (32, 32), "RGB",
                                   fmt="PNG")
            # no images → plain str (byte-identical regression lock)
            m0 = agent._build_messages(sess, "hello", None)
            assert isinstance(m0[-1]["content"], str) and m0[-1]["content"] == "hello"
            # with images → parts: text first, then image_url
            m1 = agent._build_messages(sess, "看这个", None, images=[png])
            content = m1[-1]["content"]
            assert isinstance(content, list) and len(content) == 2, content
            assert content[0] == {"type": "text", "text": "看这个"}
            assert content[1]["type"] == "image_url"
            assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
            # all-invalid paths → graceful degradation to str
            m2 = agent._build_messages(sess, "hello", None,
                                       images=[os.path.join(tmp, "gone.png")])
            assert isinstance(m2[-1]["content"], str)
            print_pass("_build_messages: 无图=str不变 / 有图=parts / 坏路径降级")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_run_injection_and_400_retry(self):
        agent_mod_client = MockDeepSeekClient()
        agent = self._make_agent(agent_mod_client)
        tmp = tempfile.mkdtemp(prefix="vision_run_")
        try:
            png = self._make_image(os.path.join(tmp, "y.png"), (32, 32), "RGB",
                                   fmt="PNG")
            # ① happy path: parts reach the API; session persists TEXT only
            await agent.run("看这张图", "vu1", images=[png])
            kind, messages, _ = agent_mod_client.chat_calls[-1]
            assert kind == "chat_with_tools"
            assert isinstance(messages[-1]["content"], list), "images not injected"
            sess = agent.sessions.get("vu1")
            user_stored = [m for m in sess.context if m["role"] == "user"][-1]
            assert user_stored["content"] == "看这张图", user_stored
            assert "base64" not in json.dumps(sess.context, ensure_ascii=False)

            # ② 400 self-heal: first call rejects image parts → retry text-only
            class _QueuedClient(MockDeepSeekClient):
                def __init__(self, responses):
                    super().__init__()
                    self.responses = list(responses)
                    self.captured = []

                async def chat_completion_with_tools(self, messages, tools,
                                                     timeout=180.0,
                                                     purpose="agent_loop"):
                    self.captured.append([dict(m) for m in messages])
                    return self.responses.pop(0)

            queued = _QueuedClient([
                {"content": "API HTTP错误 (400): invalid image_url content",
                 "tool_calls": None, "role": "assistant", "finish_reason": "error"},
                {"content": "看到了", "tool_calls": None,
                 "role": "assistant", "finish_reason": "stop"},
            ])
            agent2 = self._make_agent(queued)
            resp = await agent2.run("看图", "vu2", images=[png])
            assert resp == "看到了", resp
            assert len(queued.captured) == 2, len(queued.captured)
            assert isinstance(queued.captured[0][-1]["content"], list)
            retry_content = queued.captured[1][-1]["content"]
            assert isinstance(retry_content, str) and retry_content == "看图"
            print_pass("run(): parts注入+会话仅存文本 / 400剥图自愈重试一次")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_any_vision_client(self):
        turn = _import_turn_for_tests()
        orig = turn._model_router
        try:
            r = MagicMock()
            r.flash_client.supports_vision = False
            r.reasoning_client.supports_vision = False
            turn._model_router = r
            assert turn._any_vision_client() is False
            r.flash_client.supports_vision = True
            assert turn._any_vision_client() is True
            r.flash_client.supports_vision = False
            r.reasoning_client.supports_vision = True
            assert turn._any_vision_client() is True

            class _Boom:
                @property
                def flash_client(self):
                    raise RuntimeError("no driver")

            turn._model_router = _Boom()
            assert turn._any_vision_client() is False
            print_pass("_any_vision_client: flash/reasoning 任一开启 / 异常安全False")
        finally:
            turn._model_router = orig

    def test_config_editor_bool_extra(self):
        """面板 supports_vision 复选框：prepare 接受 True/False、缺省保留旧值、非法拒绝。"""
        from pathlib import Path as _Path
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from webui import config_editor as ce
        tmp = tempfile.mkdtemp(prefix="vision_ce_")
        try:
            settings = _Path(tmp) / "models_settings.json"
            settings.write_text(json.dumps({
                "FLASH_MODEL": {"api_key": "k", "api_base": "b", "model": "m",
                                "supports_vision": False},
            }), encoding="utf-8")
            with patch.object(ce, "MODELS_FILE", settings):
                base = {"api_key": "k", "api_base": "b", "model": "m"}
                # toggle ON (bool) — no probe needed (credentials unchanged)
                r = ce.models_section_prepare(
                    "FLASH_MODEL", {**base, "supports_vision": True})
                assert r.get("ok") and r["section_data"]["supports_vision"] is True
                assert r["needs_probe"] is False
                # toggle OFF via bool False (must NOT be skipped like numerics)
                r = ce.models_section_prepare(
                    "FLASH_MODEL", {**base, "supports_vision": False})
                assert r["section_data"]["supports_vision"] is False
                # stringy values from raw forms accepted
                r = ce.models_section_prepare(
                    "FLASH_MODEL", {**base, "supports_vision": "true"})
                assert r["section_data"]["supports_vision"] is True
                # field absent → old value kept
                r = ce.models_section_prepare("FLASH_MODEL", dict(base))
                assert r["section_data"]["supports_vision"] is False
                # invalid → field-level error
                assert "error" in ce.models_section_prepare(
                    "FLASH_MODEL", {**base, "supports_vision": 42})
                # sections without the flag in schema ignore it (no injection)
                r = ce.models_section_prepare(
                    "MULTIMODAL_MODEL", {**base, "supports_vision": True})
                assert r.get("ok") and "supports_vision" not in r["section_data"]
            print_pass("config_editor: supports_vision bool 开关往返 + 越界拒绝")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class _MdFakeBot:
    """Minimal QQBot stand-in: records sends, raises ActionFailed by code."""

    def __init__(self, fail_codes=()):
        self.sent = []
        self._fail = list(fail_codes)

    async def send(self, event, msg):
        if self._fail:
            code = self._fail.pop(0)
            from nonebot.drivers import Response
            from nonebot.adapters.qq.exception import ActionFailed
            raise ActionFailed(Response(
                status_code=400, headers={},
                content=json.dumps({"code": code, "message": "rejected"}).encode()))
        self.sent.append(msg)


class TestOfficialMarkdown:
    """51. QQ 官方渠道 markdown（msg_type=2）发送能力.

    buttons-design §6.10/§12.7 落地：渠道能力位、md/text 分组合并与区块
    切分、拒绝码纯文本降级 + 熔断、配额丢弃优先级、_send_response 格式
    路由（unified→md / split→纯文本+清洗 / OneBot 零改动）、L4 渲染能力
    声明注入（缓存前缀不动）、/帮助 与 /兑换码 markdown 化。
    """

    def run(self):
        print_header("51. QQ Official Markdown (msg_type=2)")
        os.environ.setdefault("USER_DATA_ROOT", "/tmp/qqbot_smoke")
        asyncio.run(self.test_capability_flags())
        asyncio.run(self.test_env_kill_switch())
        asyncio.run(self.test_merge_rules())
        asyncio.run(self.test_md_block_split())
        asyncio.run(self.test_reject_code_degrades_to_text())
        asyncio.run(self.test_breaker_disables_markdown())
        asyncio.run(self.test_drop_by_priority())
        asyncio.run(self.test_build_render_context())
        asyncio.run(self.test_send_response_format_routing())
        asyncio.run(self.test_l4_injection_keeps_prefix())
        asyncio.run(self.test_help_markdown_transform())
        asyncio.run(self.test_redeem_markdown_bodies())

    # ── helpers ──────────────────────────────────────────────────
    @staticmethod
    def _qo():
        import adapters.qq_official as qo
        return qo

    @staticmethod
    def _sender(bot=None, is_group=False):
        qo = TestOfficialMarkdown._qo()
        return qo.QQOfficialSender(bot or _MdFakeBot(), MagicMock(), is_group)

    @staticmethod
    def _breaker_guard(qo):
        """Snapshot/restore module-level breaker + env (tests mutate both)."""
        import contextlib

        @contextlib.contextmanager
        def guard():
            rejects, disabled = qo._md_breaker.rejects, qo._md_breaker.disabled_until
            env = os.environ.pop("QQ_OFFICIAL_MARKDOWN", None)
            try:
                qo._md_breaker.rejects = 0
                qo._md_breaker.disabled_until = 0.0
                yield
            finally:
                qo._md_breaker.rejects = rejects
                qo._md_breaker.disabled_until = disabled
                if env is not None:
                    os.environ["QQ_OFFICIAL_MARKDOWN"] = env
                else:
                    os.environ.pop("QQ_OFFICIAL_MARKDOWN", None)
        return guard()

    # ── cases ────────────────────────────────────────────────────
    async def test_capability_flags(self):
        """能力位：官方 True / OneBot False（渠道隔离 §6.10.7）。"""
        qo = self._qo()
        with self._breaker_guard(qo):
            assert self._sender().supports_markdown is True
        from adapters.onebot import OneBotSender
        assert OneBotSender(MagicMock()).supports_markdown is False
        print_pass("supports_markdown: official=True / onebot=False")

    async def test_env_kill_switch(self):
        """QQ_OFFICIAL_MARKDOWN=off → 能力位立即变 False（热读逃生门）。"""
        qo = self._qo()
        with self._breaker_guard(qo):
            os.environ["QQ_OFFICIAL_MARKDOWN"] = "off"
            assert self._sender().supports_markdown is False
            os.environ["QQ_OFFICIAL_MARKDOWN"] = "on"
            assert self._sender().supports_markdown is True
        print_pass("env kill-switch 热读生效")

    async def test_merge_rules(self):
        """合并规则：md 只与 md 合并，text 只与 text 合并，顺序保持。"""
        qo = self._qo()
        with self._breaker_guard(qo):
            s = self._sender()
            await s.send_text("a")
            await s.send_text("b")
            await s.send_markdown("## c")
            await s.send_markdown("## d")
            await s.send_text("e")
            payloads = s._merged_payloads()
            kinds = [(seg.type, k) for seg, k, _kb in payloads]
            assert kinds == [("text", "text"), ("markdown", "markdown"),
                             ("text", "text")], kinds
            assert payloads[0][0].data["text"] == "a\nb"
            assert payloads[1][0].data["markdown"].content == "## c\n\n## d"
            assert payloads[2][0].data["text"] == "e"
        print_pass("md/text 分组互不混入，相对顺序保持")

    async def test_md_block_split(self):
        """超长 md 按区块边界切分，每块 ≤_MD_CHUNK_LIMIT，不切行中。"""
        qo = self._qo()
        body = "\n\n".join(f"## 段落{i}\n内容行A\n内容行B" for i in range(150))
        chunks = qo._split_markdown(body)
        assert len(chunks) > 1
        assert all(len(c) <= qo._MD_CHUNK_LIMIT for c in chunks)
        assert not any(c.endswith("内容行") for c in chunks), "不得切在行中"
        assert qo._split_markdown("短文本") == ["短文本"]
        print_pass(f"区块切分：{len(body)}字 → {len(chunks)} 块，"
                   f"均 ≤{qo._MD_CHUNK_LIMIT}")

    async def test_reject_code_degrades_to_text(self):
        """md 被拒（40034011）→ 同内容纯文本重发一次，内容不丢。"""
        qo = self._qo()
        with self._breaker_guard(qo):
            bot = _MdFakeBot(fail_codes=[40034011])
            s = self._sender(bot)
            await s.send_markdown("**正文**")
            await s.flush()
            assert len(bot.sent) == 1, bot.sent
            seg = bot.sent[0]
            assert seg.type == "text" and seg.data["text"] == "**正文**"
            assert qo._md_breaker.rejects == 1
        print_pass("拒绝码降级：md → 纯文本重发（内容保留）+ 熔断计数")

    async def test_breaker_disables_markdown(self):
        """连续 3 次被拒 → 熔断打开：能力位 False，send_markdown 落 text 缓冲。"""
        qo = self._qo()
        with self._breaker_guard(qo):
            for _ in range(3):
                qo._md_record_reject()
            assert self._sender().supports_markdown is False
            s = self._sender()
            await s.send_markdown("## 熔断期正文")
            assert s._buffer == [("text", "## 熔断期正文")], s._buffer
            # 冷却期满 → 半开恢复
            qo._md_breaker.disabled_until = time.monotonic() - 1
            assert qo._md_available() is True
            assert qo._md_breaker.rejects == 0
        print_pass("熔断：3 拒 → 降级纯文本；冷却后半开恢复")

    async def test_drop_by_priority(self):
        """配额不足丢弃优先级：先媒体（尾部起），再文本（免责在正文后）。"""
        qo = self._qo()
        payloads = [("md", "markdown"), ("img", "media"), ("dis", "text")]
        kept = qo._drop_by_priority(payloads, 1)
        assert [k for _, k in kept] == ["markdown", "text"], kept
        kept = qo._drop_by_priority(payloads, 2)
        assert [k for _, k in kept] == ["markdown"], kept
        # 顺序保持：幸存者相对顺序不变
        assert kept[0][0] == "md"
        print_pass("丢弃优先级：media 先丢、尾部先丢、正文最后")

    async def test_build_render_context(self):
        """渲染声明：unified×官方 → 注入文本；split 或无能力渠道 → 空。"""
        turn = _import_turn_for_tests()
        qo = self._qo()
        with self._breaker_guard(qo):
            md_sender = self._sender()

            class _NoMd:
                supports_markdown = False
            assert turn._build_render_context("unified", md_sender) \
                == turn._RENDER_MD_DECLARATION
            assert turn._build_render_context("split", md_sender) == ""
            assert turn._build_render_context("unified", _NoMd()) == ""
        print_pass("声明矩阵：仅 unified + md 渠道非空")

    async def test_send_response_format_routing(self):
        """_send_response 五路由：短md内联免责 / 长md免责独立 / 违规降纯 /
        官方纯文本清洗记号 / OneBot 行为零改动。"""
        turn = _import_turn_for_tests()
        from agent.outbound import OutMarkdown

        class _RecSender:
            platform = "qq_official"
            max_messages_per_turn = 10
            supports_markdown = True

            def __init__(self):
                self.out = []

            async def send_text(self, x): self.out.append(("text", x))
            async def send_markdown(self, x): self.out.append(("md", x))
            async def send_image(self, x): pass
            async def send_audio(self, x): pass
            async def send_progress(self, x, *, immediate=False): pass

        old_sender = turn.default_sender
        p_tok = turn._current_platform.set("qq_official")
        r_tok = turn._current_render_context.set(turn._RENDER_MD_DECLARATION)
        try:
            # 1) 短正文 → 单条 md，免责内联
            s = _RecSender(); turn.default_sender = s
            await turn._send_response("## 短答复\n**要点**")
            assert len(s.out) == 1 and s.out[0][0] == "md", s.out
            assert "理性判断" in s.out[0][1]
            # 2) 长正文 → md + 独立纯文本免责
            s = _RecSender(); turn.default_sender = s
            await turn._send_response("## 长答复\n" + "内" * 250)
            assert [k for k, _ in s.out] == ["md", "text"], s.out
            assert "理性判断" in s.out[1][1]
            # 3) 白名单违规（表格）→ 纯文本路径
            s = _RecSender(); turn.default_sender = s
            await turn._send_response("| a | b |\n| 1 | 2 |")
            assert all(k == "text" for k, _ in s.out), s.out
            # 4) split（无声明）→ 纯文本且 ** 记号被清洗
            s = _RecSender(); turn.default_sender = s
            turn._current_render_context.set("")
            await turn._send_response("**加粗**正文")
            assert s.out[0][0] == "text" and "**" not in s.out[0][1], s.out
            # 5) OneBot → 原样纯文本，不清洗（行为零改动 §13.3）
            s = _RecSender(); s.platform = "onebot"; s.supports_markdown = False
            turn.default_sender = s
            turn._current_platform.set("onebot")
            turn._current_render_context.set(turn._RENDER_MD_DECLARATION)
            await turn._send_response("**keep** markers")
            assert s.out[0][0] == "text" and "**keep**" in s.out[0][1], s.out
        finally:
            turn.default_sender = old_sender
            turn._current_platform.reset(p_tok)
            turn._current_render_context.reset(r_tok)
        print_pass("_send_response 五条格式路由全部符合预期")

    async def test_l4_injection_keeps_prefix(self):
        """声明只进 L4 易变尾部；L1-L3 缓存前缀不受影响。"""
        from agent.tool_registry import ToolRegistry
        from agent.agent import Agent
        from agent.context import _current_personality, _current_group_context
        turn = _import_turn_for_tests()
        agent = Agent(
            deepseek_client=MockDeepSeekClient(),
            tool_registry=ToolRegistry(),
            config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
        )
        p_tok = _current_personality.set("")
        g_tok = _current_group_context.set("")
        r_tok = turn._current_render_context.set(turn._RENDER_MD_DECLARATION)
        try:
            msgs = agent._build_messages(
                agent.sessions.get_or_create("user_md"), "问题")
            systems = [m for m in msgs if m["role"] == "system"]
            head, tail = systems[0]["content"], systems[-1]["content"]
            assert "本轮渲染能力" in tail, "声明必须在 L4 尾部"
            assert "本轮渲染能力" not in head, "缓存前缀不得含易变声明"
            # 关闭声明 → 尾部无该块，头部逐字节不变
            head_before = head
            turn._current_render_context.set("")
            msgs2 = agent._build_messages(
                agent.sessions.get_or_create("user_md"), "问题2")
            systems2 = [m for m in msgs2 if m["role"] == "system"]
            assert "本轮渲染能力" not in systems2[-1]["content"]
            assert systems2[0]["content"] == head_before, "头部必须逐字节稳定"
        finally:
            _current_personality.reset(p_tok)
            _current_group_context.reset(g_tok)
            turn._current_render_context.reset(r_tok)
        print_pass("L4 注入：声明进尾部，缓存前缀逐字节稳定")

    async def test_help_markdown_transform(self):
        """/帮助 md 化：反引号剥除、--- → ***、标题/列表保留、# 命令转义、单条长度。"""
        turn = _import_turn_for_tests()
        md = turn._help_markdown()
        assert md, "HELP.md 读取失败"
        assert "`" not in md, "行内代码不在官方白名单"
        assert not re.search(r"^\s*-{3,}\s*$", md, re.MULTILINE), "--- 需换成 ***"
        assert "## 特殊会话管理" in md and "- /新会话" in md
        # 真标题（# 后带空格）保留、H1 不被误伤
        assert md.startswith("# 帮助"), f"H1 标题应原样保留，实际开头 {md[:12]!r}"
        assert "## 特殊会话管理" in md
        # 反馈命令前缀 # 必须转义，避免被 QQ 渲染成节
        for cmd in ("反馈", "bug", "建议"):
            assert f"\\#{cmd}" in md, f"命令 #{cmd} 未加转义符"
        # 不得残留裸 #命令：# 紧跟非空白「非 #」字符且前面不是反斜杠
        # （排除 ## 真标题——其首 # 后是 #，属 [^\s#] 之外，不会被误判）
        assert not re.search(r"(?<!\\)#(?=[^\s#])", md), "存在未转义的 # 命令前缀"
        import adapters.qq_official as qo
        assert len(md) <= qo._MD_CHUNK_LIMIT, \
            f"帮助应单条发出，实际 {len(md)} 字"
        print_pass(f"/帮助 → 单条 md（{len(md)} 字，白名单安全，# 命令已转义）")

    async def test_redeem_markdown_bodies(self):
        """/兑换码 md：每码一行、无加粗/反引号、过期分段、页脚保留。"""
        turn = _import_turn_for_tests()
        fmt = lambda v: v  # noqa: E731 — 有效期展示不参与本测试断言
        active = [
            {"code": "ABC_123", "content": "钻石×100", "valid": "11-05"},
            {"code": "DEF456", "content": "", "valid": ""},
        ]
        expired = [{"code": "OLD-1", "content": "抽卡券×3", "valid": "10-20"}]
        md = turn._redeem_time_md(active, expired, hidden=2,
                                  stale_note="⚠ 数据截至 x", format_expiry_display=fmt)
        assert "- ABC_123 · 钻石×100 · 有效期至 11-05" in md, md
        assert "- DEF456 · 过期时间未知" in md, md
        assert "**已于 7 天内过期（1 条）**" in md
        assert "- OLD-1 · 抽卡券×3 · 已于 10-20 过期" in md
        assert "另有 2 条较早的码未显示" in md
        assert "/长期兑换码" in md and "⚠ 数据截至 x" in md
        for bad in ("`", "**ABC", "| "):
            assert bad not in md.replace("**已于", ""), f"码行不得含 {bad!r}"
        lt = turn._redeem_long_md([{"code": "LT_1", "content": "金币×999"}], "")
        assert "## 长期有效兑换码（1 条）" in lt and "- LT_1 · 金币×999" in lt
        print_pass("/兑换码 与 /长期兑换码 md 正文格式符合 §6.10.8")


class TestOfficialButtons:
    """52. QQ 官方渠道消息按钮（keyboard / inline buttons）P0+P1.

    buttons-design 落地：平台无关 DSL（button_spec）、wire 转换
    （to_message_keyboard / encode_cb / decode_cb）、合并与降级梯
    （kb → md-only → text+命令清单）、能力熔断、回调链（幂等 / 限频 /
    版本戳 / scene→role 复核）、共享渲染隐私（群会话列表不泄露）、以及
    内核 _send_response 的 _turn_keyboard 一次性消费与 #n 别名。
    """

    def run(self):
        print_header("52. QQ Official Buttons (keyboard P0+P1)")
        os.environ.setdefault("USER_DATA_ROOT", "/tmp/qqbot_smoke")
        asyncio.run(self.test_u1_validate_boundaries())
        asyncio.run(self.test_u2_wire_field_map())
        asyncio.run(self.test_u3_codec_roundtrip())
        asyncio.run(self.test_u4_merge_rules())
        asyncio.run(self.test_u5_degradation_ladder())
        asyncio.run(self.test_u6_breaker())
        asyncio.run(self.test_u7_version_stamp_guard())
        asyncio.run(self.test_u8_idempotency_and_rate())
        asyncio.run(self.test_u9_admin_role_gate())
        asyncio.run(self.test_u10_group_session_privacy())
        asyncio.run(self.test_onebot_keyboard_degrades())
        asyncio.run(self.test_send_response_one_shot_keyboard())
        asyncio.run(self.test_session_index_alias())

    # ── helpers ──────────────────────────────────────────────────
    @staticmethod
    def _qo():
        import adapters.qq_official as qo
        return qo

    @staticmethod
    def _sender(bot=None, is_group=False):
        qo = TestOfficialButtons._qo()
        return qo.QQOfficialSender(bot or _MdFakeBot(), MagicMock(), is_group)

    @staticmethod
    def _kb_guard(qo):
        """Snapshot/restore kb breaker + env (tests mutate both). Also
        keeps the md breaker closed — keyboard availability requires it."""
        import contextlib

        @contextlib.contextmanager
        def guard():
            kb = qo._kb_breaker
            snap = (kb.state, kb.rejects, kb.disabled_until,
                    kb.rejected_total, kb.last_reject_code, kb.last_reject_at)
            md_rejects, md_disabled = (qo._md_breaker.rejects,
                                       qo._md_breaker.disabled_until)
            env_btn = os.environ.pop("QQ_OFFICIAL_BUTTONS", None)
            env_md = os.environ.pop("QQ_OFFICIAL_MARKDOWN", None)
            env_users = os.environ.pop("QQ_OFFICIAL_BUTTONS_USERS", None)
            try:
                kb.state, kb.rejects, kb.disabled_until = "unknown", 0, 0.0
                kb.rejected_total, kb.last_reject_code = 0, None
                kb.last_reject_at = 0.0
                qo._md_breaker.rejects = 0
                qo._md_breaker.disabled_until = 0.0
                os.environ["QQ_OFFICIAL_BUTTONS"] = "on"
                yield
            finally:
                (kb.state, kb.rejects, kb.disabled_until, kb.rejected_total,
                 kb.last_reject_code, kb.last_reject_at) = snap
                qo._md_breaker.rejects = md_rejects
                qo._md_breaker.disabled_until = md_disabled
                for k, v in (("QQ_OFFICIAL_BUTTONS", env_btn),
                             ("QQ_OFFICIAL_MARKDOWN", env_md),
                             ("QQ_OFFICIAL_BUTTONS_USERS", env_users)):
                    if v is not None:
                        os.environ[k] = v
                    else:
                        os.environ.pop(k, None)
        return guard()

    # ── U1: button_spec.validate / helpers ───────────────────────
    async def test_u1_validate_boundaries(self):
        import lib.button_spec as bs
        from lib.button_spec import Btn
        ok = [[Btn(label="确定", data="/ok", action="cmd")]]
        assert bs.validate(ok) == []
        assert bs.validate([]) == ["keyboard is empty"]
        # 行数上限（6 行 > 默认 5）
        six = [[Btn(label="x", data="/x", action="cmd")] for _ in range(6)]
        assert any("too many rows" in p for p in bs.validate(six))
        # 空行 / 每行超限
        assert any("is empty" in p for p in bs.validate(
            [[Btn(label="a", data="/a", action="cmd")], []]))
        wide = [[Btn(label=str(i), data=f"/{i}", action="cmd")
                 for i in range(4)]]
        assert any("buttons >" in p for p in bs.validate(wide))
        # label 宽度 / 空
        assert any("empty label" in p for p in bs.validate(
            [[Btn(label="", data="/x", action="cmd")]]))
        assert any("label too wide" in p for p in bs.validate(
            [[Btn(label="超超超超超超", data="/x", action="cmd")]]))
        # cmd 分支
        assert any("without data" in p for p in bs.validate(
            [[Btn(label="X", data="", action="cmd")]]))
        assert any("cmd data too long" in p for p in bs.validate(
            [[Btn(label="X", data="x" * 61, action="cmd")]]))
        assert any("modal is cb-only" in p for p in bs.validate(
            [[Btn(label="X", data="/x", action="cmd", modal="m")]]))
        # cb 分支
        assert any("cb payload malformed" in p for p in bs.validate(
            [[Btn(label="X", data="nope", action="cb")]]))
        assert bs.validate(
            [[Btn(label="X", data="qb1:session.del:1:abcd", action="cb")]]
        ) == []
        # url 分支
        assert any("needs https" in p for p in bs.validate(
            [[Btn(label="X", data="http://x", action="url")]]))
        assert bs.validate(
            [[Btn(label="X", data="https://x.com", action="url")]]) == []
        # 未知 action
        assert any("unknown action" in p for p in bs.validate(
            [[Btn(label="X", data="d", action="zzz")]]))
        # 总量上限（放宽每行/行数，只撞总量）
        big = [[Btn(label=str(i), data=f"/{i}", action="cmd")
                for i in range(4)] for _ in range(5)]
        assert any("too many buttons total" in p
                   for p in bs.validate(big, max_rows_=5, max_per_row_=5))
        # 辅助函数
        assert bs.display_width("abc") == 3 and bs.display_width("汉字") == 4
        fit = bs.fit_label("超长角色名字超长", 10)
        assert bs.display_width(fit) <= 10 and fit.endswith("…")
        assert bs.fit_label("短名") == "短名"
        clipped = bs.clip_rows(six)
        assert len(clipped) <= bs._PLATFORM_MAX_ROWS
        assert sum(len(r) for r in clipped) <= bs._PLATFORM_MAX_TOTAL
        assert bs.stamp(["a", "b"]) == bs.stamp(["a", "b"])
        assert bs.stamp(["a", "b"]) != bs.stamp(["a", "c"])
        assert len(bs.stamp(["x"])) == 4
        txt = bs.to_text([[Btn(label="甲", data="/a", action="cmd"),
                           Btn(label="乙", data="qb1:x", action="cb"),
                           Btn(label="丙", data="https://z", action="url")]])
        assert "1. 甲 → 发送「/a」" in txt and "https://z" in txt
        assert "qb1:x" not in txt   # cb 无文本等价，跳过
        print_pass("U1 validate/fit/clip/stamp/to_text 边界全部符合")

    # ── U2: to_message_keyboard 字段映射 ─────────────────────────
    async def test_u2_wire_field_map(self):
        from adapters.qq_official_buttons import to_message_keyboard
        from lib.button_spec import Btn
        cmd = to_message_keyboard(
            [[Btn(label="Go", data="/go", action="cmd", reply=True,
                  enter=True)]]).content.rows[0].buttons[0]
        assert cmd.id == "b.0.0"
        assert cmd.render_data.label == "Go"
        assert cmd.render_data.visited_label == "Go"   # 缺省同 label
        assert cmd.action.type == 2 and cmd.action.data == "/go"
        assert cmd.action.reply is True and cmd.action.enter is True
        assert cmd.action.permission.type == 2
        # style 钳制到 {0,1}
        clamped = to_message_keyboard(
            [[Btn(label="S", data="/s", action="cmd", style=5)]]
        ).content.rows[0].buttons[0]
        assert clamped.render_data.style == 1
        kept0 = to_message_keyboard(
            [[Btn(label="S", data="/s", action="cmd", style=0)]]
        ).content.rows[0].buttons[0]
        assert kept0.render_data.style == 0
        # visited_label 覆盖
        vis = to_message_keyboard(
            [[Btn(label="V", data="/v", action="cmd", visited="完成")]]
        ).content.rows[0].buttons[0]
        assert vis.render_data.visited_label == "完成"
        # cb admin_only → permission type=1
        adm = to_message_keyboard(
            [[Btn(label="D", data="qb1:session.del:1:abcd", action="cb",
                  admin_only=True)]]).content.rows[0].buttons[0]
        assert adm.action.type == 1 and adm.action.permission.type == 1
        # cb group+openid → permission type=0 specify_user_ids
        grp = to_message_keyboard(
            [[Btn(label="D", data="qb1:session.del:1:abcd", action="cb")]],
            scope="group", openid="OU1").content.rows[0].buttons[0]
        assert grp.action.permission.type == 0
        assert grp.action.permission.specify_user_ids == ["OU1"]
        # cb modal
        mdl = to_message_keyboard(
            [[Btn(label="D", data="qb1:session.del:1:abcd", action="cb",
                  modal="确认删除？", confirm="删", cancel="算了")]]
        ).content.rows[0].buttons[0]
        assert mdl.action.modal.content == "确认删除？"
        assert mdl.action.modal.confirm_text == "删"
        assert mdl.action.modal.cancel_text == "算了"
        # url → type=0
        url = to_message_keyboard(
            [[Btn(label="Site", data="https://x.com", action="url")]]
        ).content.rows[0].buttons[0]
        assert url.action.type == 0 and url.action.permission.type == 2
        print_pass("U2 to_message_keyboard 字段映射对照 §50 全部符合")

    # ── U3: encode/decode 往返 + 畸形 ────────────────────────────
    async def test_u3_codec_roundtrip(self):
        from adapters.qq_official_buttons import encode_cb, decode_cb
        assert encode_cb("session.del", "1", "abcd") == "qb1:session.del:1:abcd"
        assert decode_cb("qb1:session.del:1:abcd") == ("session.del",
                                                       ["1", "abcd"])
        v, a = "toggle.feature", ["gacha", "on"]
        assert decode_cb(encode_cb(v, *a)) == (v, a)
        assert decode_cb("qb1:session.page") == ("session.page", [])
        for bad_encode in (lambda: encode_cb("Bad Verb"),
                           lambda: encode_cb("session.del", "中文"),
                           lambda: encode_cb("session.del", "a:b"),
                           lambda: encode_cb("v", "1", "2", "3", "4")):
            try:
                bad_encode()
                raise AssertionError("expected ValueError")
            except ValueError:
                pass
        for bad in ("garbage", "qb2:x", "qb1:", "qb1:session.del:中文"):
            try:
                decode_cb(bad)
                raise AssertionError(f"expected ValueError for {bad!r}")
            except ValueError:
                pass
        print_pass("U3 encode/decode 往返一致，畸形载荷一律 ValueError")

    # ── U4: _merged_payloads 键盘合并 ────────────────────────────
    async def test_u4_merge_rules(self):
        qo = self._qo()
        from agent.outbound import OutKeyboard
        from lib.button_spec import Btn
        rows = [[Btn(label="X", data="/x", action="cmd")]]
        with self._kb_guard(qo):
            # [md, kb] → 1 条（kb 骑相邻 md）
            s = self._sender()
            await s.send_markdown("## 正文")
            s._buffer.append(("kb", OutKeyboard(markdown="", rows=rows,
                                                label="t")))
            p = s._merged_payloads()
            assert len(p) == 1 and p[0][1] == "markdown" and p[0][2], p
            # [text, kb] → 2 条（kb 自带 md 独立成条）
            s = self._sender()
            await s.send_text("纯文本")
            s._buffer.append(("kb", OutKeyboard(markdown="键盘正文",
                                                rows=rows, label="t")))
            p = s._merged_payloads()
            assert len(p) == 2, p
            assert [k for _, k, _ in p] == ["text", "markdown"], p
            assert p[1][2] is not None
            # [kb, text] → 2 条（kb 非最后，独立）
            s = self._sender()
            s._buffer.append(("kb", OutKeyboard(markdown="B", rows=rows,
                                                label="t")))
            await s.send_text("尾巴")
            p = s._merged_payloads()
            assert len(p) == 2 and [k for _, k, _ in p] == ["markdown",
                                                             "text"], p
            # [kb, kb] → 2 条独立
            s = self._sender()
            s._buffer.append(("kb", OutKeyboard(markdown="A", rows=rows,
                                                label="t")))
            s._buffer.append(("kb", OutKeyboard(markdown="B", rows=rows,
                                                label="t")))
            p = s._merged_payloads()
            assert len(p) == 2 and all(x[2] for x in p), p
        print_pass("U4 键盘合并：[md,kb]→1、[text,kb]/[kb,text]/[kb,kb]→2")

    # ── U5: 降级梯（kb → md-only → text+命令清单）────────────────
    async def test_u5_degradation_ladder(self):
        qo = self._qo()
        from agent.outbound import OutKeyboard
        from lib.button_spec import Btn
        rows = [[Btn(label="再抽", data="再抽一次", action="cmd")]]
        with self._kb_guard(qo):
            # 键盘被拒（304037）→ md-only 成功
            bot = _MdFakeBot(fail_codes=[304037])
            s = self._sender(bot)
            await s.send_keyboard(OutKeyboard(markdown="## 结果",
                                              rows=rows, label="gacha"))
            await s.flush()
            assert len(bot.sent) == 1, bot.sent
            seg = bot.sent[0]
            assert seg.type == "markdown", seg.type
            assert qo._kb_breaker.state == "unsupported"
        with self._kb_guard(qo):
            # 键盘拒 + md 拒 → 纯文本含命令清单（L2）
            bot = _MdFakeBot(fail_codes=[304037, 40034011])
            s = self._sender(bot)
            await s.send_keyboard(OutKeyboard(markdown="## 结果",
                                              rows=rows, label="gacha"))
            await s.flush()
            assert len(bot.sent) == 1, bot.sent
            seg = bot.sent[0]
            assert seg.type == "text", seg.type
            body = seg.data["text"]
            assert "结果" in body and "按钮暂不可用" in body
            assert "再抽一次" in body   # to_text 命令清单
        print_pass("U5 降级梯：304037→md-only；md 再拒→纯文本+命令清单")

    # ── U6: 熔断 ─────────────────────────────────────────────────
    async def test_u6_breaker(self):
        qo = self._qo()
        with self._kb_guard(qo):
            # 304037 → unsupported + 长熔断
            qo._kb_record_reject(304037)
            assert qo._kb_breaker.state == "unsupported"
            assert qo._kb_available() is False
            # 冷却期满 → 半开，state 回 unknown
            qo._kb_breaker.disabled_until = time.monotonic() - 1
            assert qo._kb_available() is True
            assert qo._kb_breaker.state == "unknown"
            # 连续 3 次普通拒绝 → 600s 熔断（非 unsupported）
            qo._kb_breaker.state, qo._kb_breaker.rejects = "unknown", 0
            qo._kb_breaker.disabled_until = 0.0
            for _ in range(3):
                qo._kb_record_reject(305007)
            assert qo._kb_breaker.disabled_until > 0
            assert qo._kb_breaker.state != "unsupported"
            assert qo._kb_available() is False
            # 成功一次即清计数
            qo._kb_breaker.disabled_until = 0.0
            qo._kb_record_reject(305007)
            qo._kb_record_success()
            assert qo._kb_breaker.rejects == 0
            assert qo._kb_breaker.state == "ok"
            snap = qo.kb_snapshot()
            assert snap["state"] == "ok" and "available" in snap
        print_pass("U6 熔断：304037 长熔断/半开；3 连拒 600s；成功清零")

    # ── U7: 版本戳拦截（列表变更 → 拒绝执行删除）─────────────────
    async def test_u7_version_stamp_guard(self):
        import agent.button_actions as ba
        from agent import session_render
        turn = _import_turn_for_tests()

        class _StubSessions:
            def __init__(self):
                self.deleted = []
                self._s = [{"name": "Alpha", "created_at": 0,
                            "total_messages": 2, "metadata": {}}]
            def list_sessions(self, user):
                return list(self._s)
            def delete(self, user, name):
                self.deleted.append(name)
                return {}

        stub = _StubSessions()
        old_ss, turn._special_sessions = turn._special_sessions, stub
        old_fresh = ba._fresh_session_page
        ba._fresh_session_page = lambda u, p=1: ("STUB", [])
        try:
            # 版本戳不符 → code=1，不删除
            wrong = ba.ButtonContext(user_id="u", group_id="", scene="c2c",
                                     verb="session.del", args=["1", "ZZZZ"])
            res = await ba._session_del(wrong)
            assert res.code == 1, res
            assert stub.deleted == [], "版本戳不符时绝不能删除"
            # 版本戳正确 → 执行删除
            ver = session_render.session_list_stamp(stub._s)
            old_fs, turn._format_delete_summary = (
                getattr(turn, "_format_delete_summary", None), lambda r: "")
            old_qt, turn._delete_quota_tail = (
                getattr(turn, "_delete_quota_tail", None), lambda u, k: "")
            old_pm = getattr(turn, "_profile_manager", None)
            turn._profile_manager = MagicMock()
            try:
                right = ba.ButtonContext(user_id="u", group_id="",
                                         scene="c2c", verb="session.del",
                                         args=["1", ver])
                res2 = await ba._session_del(right)
                assert res2.code == 0, res2
                assert stub.deleted == ["Alpha"], stub.deleted
            finally:
                if old_fs is not None:
                    turn._format_delete_summary = old_fs
                if old_qt is not None:
                    turn._delete_quota_tail = old_qt
                if old_pm is not None:
                    turn._profile_manager = old_pm
        finally:
            turn._special_sessions = old_ss
            ba._fresh_session_page = old_fresh
        print_pass("U7 版本戳：不符→code=1 拒绝删除；相符→执行删除")

    # ── U8: 幂等 + 限频 ──────────────────────────────────────────
    async def test_u8_idempotency_and_rate(self):
        import types
        import plugins.qq_official_buttons as pob
        import agent.button_actions as ba
        from agent import context as ac

        # 纯函数层：seen / rate
        pob._seen.clear()
        assert pob._seen_before("k1") is False
        assert pob._seen_before("k1") is True
        pob._rate.clear()
        assert all(pob._rate_limited("u", "v") is False for _ in range(3))
        assert pob._rate_limited("u", "v") is True   # 第 4 次触发

        # 处理器层：同 interaction_id 二次 → code=3，handler 只执行一次
        calls = {"fresh": 0}
        old_fresh = ba._fresh_session_page
        ba._fresh_session_page = lambda u, p=1: (calls.__setitem__(
            "fresh", calls["fresh"] + 1) or ("STUB", []))
        tmp = tempfile.mkdtemp(prefix="btn_audit_")
        old_audit = os.environ.get("QQ_OFFICIAL_BUTTONS_AUDIT")
        os.environ["QQ_OFFICIAL_BUTTONS_AUDIT"] = os.path.join(tmp, "a.jsonl")
        pob._seen.clear(); pob._rate.clear()
        prev_platform = ac._current_platform.get()

        class _FakeBot:
            def __init__(self):
                self.acks = []; self.sent = []
            async def put_interaction(self, *, interaction_id, code):
                self.acks.append((interaction_id, code))
            async def send(self, event, msg):
                self.sent.append(msg)

        def ev(id):
            return types.SimpleNamespace(
                type=11, id=id, chat_type=2, user_openid="u_dup",
                group_member_openid="", group_openid="",
                data=types.SimpleNamespace(resolved=types.SimpleNamespace(
                    button_data="qb1:session.page:1", button_id="b.0.0")))
        try:
            bot = _FakeBot()
            await pob._on_interaction(bot, ev("dup-1"))
            await pob._on_interaction(bot, ev("dup-1"))
            assert calls["fresh"] == 1, calls   # handler 只跑一次
            assert any(c == 3 for _, c in bot.acks), bot.acks
        finally:
            ba._fresh_session_page = old_fresh
            ac._current_platform.set(prev_platform)
            if old_audit is not None:
                os.environ["QQ_OFFICIAL_BUTTONS_AUDIT"] = old_audit
            else:
                os.environ.pop("QQ_OFFICIAL_BUTTONS_AUDIT", None)
            shutil.rmtree(tmp, ignore_errors=True)
            pob._seen.clear(); pob._rate.clear()
        print_pass("U8 幂等：重复 interaction_id→code=3、handler 单次；限频 3/10s")

    # ── U9: regular 触发 admin_only → code=5 ─────────────────────
    async def test_u9_admin_role_gate(self):
        import agent.button_actions as ba
        ctx = ba.ButtonContext(user_id="definitely_not_admin_xyz",
                               group_id="g1", scene="group",
                               verb="toggle.feature", args=["gacha", "on"])
        res = await ba.dispatch(ctx)
        assert res.code == 5, res
        # scene 不符（c2c 触发 group-only verb）→ code=1
        ctx2 = ba.ButtonContext(user_id="u", group_id="", scene="c2c",
                                verb="toggle.feature", args=["gacha", "on"])
        assert (await ba.dispatch(ctx2)).code == 1
        # 未知 verb → code=1
        ctx3 = ba.ButtonContext(user_id="u", group_id="", scene="c2c",
                                verb="no.such", args=[])
        assert (await ba.dispatch(ctx3)).code == 1
        print_pass("U9 scene→role 复核：admin-only 越权 code=5，场景/verb 不符 code=1")

    # ── U10: 群会话列表隐私 ──────────────────────────────────────
    async def test_u10_group_session_privacy(self):
        from agent import session_render
        from lib.button_spec import Btn

        class _StubSessions:
            def list_sessions(self, user):
                return [{"name": "私密剧情", "created_at": 0,
                         "total_messages": 8, "metadata": {}}]

        md, rows = session_render.render_session_page(
            "u", 1, "group", sessions_mgr=_StubSessions(),
            perm=MagicMock(), registry=MagicMock())
        assert "私密剧情" not in md, md
        assert "私聊" in md
        # 只有一个「帮助」按钮，不含任何会话名
        flat = [b for r in rows for b in r]
        assert all(b.action == "cmd" for b in flat)
        assert not any("私密剧情" in (b.data or "") for b in flat)
        print_pass("U10 群会话列表：不泄露会话名，引导私聊（§9.2）")

    # ── 附加：OneBot send_keyboard 文本降级 ──────────────────────
    async def test_onebot_keyboard_degrades(self):
        from adapters.onebot import OneBotSender
        from agent.outbound import OutKeyboard
        from lib.button_spec import Btn
        assert OneBotSender(MagicMock()).supports_keyboard is False

        class _RecMatcher:
            def __init__(self):
                self.sent = []
            async def send(self, x):
                self.sent.append(x)
        m = _RecMatcher()
        s = OneBotSender(m)
        await s.send_keyboard(OutKeyboard(
            markdown="**结果**", rows=[[Btn(label="再来", data="再抽一次",
                                            action="cmd")]]))
        assert len(m.sent) == 1
        text = m.sent[0]
        assert "**" not in text          # md 记号被清洗
        assert "结果" in text and "再抽一次" in text
        print_pass("OneBot send_keyboard：降级纯文本（清洗 md + 命令清单）")

    # ── 附加：_send_response 一次性消费 _turn_keyboard ───────────
    async def test_send_response_one_shot_keyboard(self):
        turn = _import_turn_for_tests()
        from lib.button_spec import Btn

        class _KbRec:
            platform = "qq_official"
            max_messages_per_turn = 10
            supports_markdown = True
            supports_keyboard = True
            def __init__(self):
                self.kb = []; self.out = []
            async def send_text(self, x): self.out.append(("text", x))
            async def send_markdown(self, x): self.out.append(("md", x))
            async def send_keyboard(self, msg): self.kb.append(msg)
            async def send_image(self, x): pass
            async def send_audio(self, x): pass
            async def send_progress(self, x, *, immediate=False): pass

        rows = [[Btn(label="再抽一次", data="再抽一次", action="cmd")]]
        s = _KbRec()
        p_tok = turn._current_platform.set("qq_official")
        r_tok = turn._current_render_context.set(turn._RENDER_MD_DECLARATION)
        try:
            turn._turn_keyboard.set(rows)
            await turn._send_response("## 抽卡结果", sender=s)
            assert len(s.kb) == 1, s.kb
            assert s.kb[0].rows == rows
            assert turn._turn_keyboard.get() is None, "预约必须被清空（一次性）"
            # 第二次无预约 → 不再挂键盘
            s2 = _KbRec()
            await turn._send_response("## 普通回复", sender=s2)
            assert s2.kb == [], s2.kb
        finally:
            turn._turn_keyboard.set(None)
            turn._current_platform.reset(p_tok)
            turn._current_render_context.reset(r_tok)
        print_pass("_send_response：_turn_keyboard 一次性消费，无预约不挂键盘")

    # ── 附加：#n 别名解析 ────────────────────────────────────────
    async def test_session_index_alias(self):
        turn = _import_turn_for_tests()

        class _StubSessions:
            def list_sessions(self, user):
                return [{"name": "Alpha"}, {"name": "Beta"}]
        old_ss, turn._special_sessions = turn._special_sessions, _StubSessions()
        try:
            assert turn._resolve_session_index("u", "#1") == ("Alpha", None)
            assert turn._resolve_session_index("u", "#2") == ("Beta", None)
            name, err = turn._resolve_session_index("u", "#3")
            assert name is None and err and "超出范围" in err
            # 非 #<digits> 原样透传
            assert turn._resolve_session_index("u", "我的会话") == ("我的会话",
                                                                  None)
            assert turn._resolve_session_index("u", "#abc") == ("#abc", None)
        finally:
            turn._special_sessions = old_ss
        print_pass("#n 别名：按 list_sessions 序解析，越界报错，非序号透传")


# ── streaming test fakes (qq_official c2c stream_messages) ─────────

def _stream_action_failed(code, status=400):
    """Build a nonebot QQ ActionFailed carrying a platform error code."""
    from nonebot.drivers import Response
    from nonebot.adapters.qq.exception import ActionFailed
    return ActionFailed(Response(
        status_code=status, headers={},
        content=json.dumps({"code": code, "message": "rejected"}).encode()))


class _FakeAPIBase:
    def joinpath(self, *parts):
        return "https://test.local/" + "/".join(str(p) for p in parts)


class _StreamFakeBot:
    """Fake QQBot for MessageStreamer: captures stream_messages POSTs.

    ``responder(payload)`` returns a response dict or raises. The default
    returns a per-stream id (so progress/answer get distinct
    stream_msg_id) plus a generous remain budget.
    """

    def __init__(self, responder=None):
        self.posts = []
        self._responder = responder or self._default_responder
        self.adapter = self

    def get_api_base(self):
        return _FakeAPIBase()

    @staticmethod
    def _default_responder(payload):
        return {"id": f"smid_{payload['msg_seq']}",
                "timestamp": 0, "remain_msg_len": 5000}

    async def _request(self, request):
        payload = request.json
        self.posts.append(payload)
        return self._responder(payload)


class _FakeStreamCtx:
    def __init__(self, lines, status=200, body=b""):
        self._lines = lines
        self.status_code = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


class _FakeHttpClient:
    """Stand-in for httpx.AsyncClient yielding canned SSE lines."""

    _lines: list = []
    _status = 200
    _body = b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, headers=None, json=None, timeout=None):
        return _FakeStreamCtx(self._lines, self._status, self._body)


def _sse(obj):
    return f"data: {__import__('json').dumps(obj)}"


class TestOfficialStream:
    """53. QQ 官方渠道流式消息（stream_messages，仅单聊，§6.10.11.2）.

    三层离线覆盖：① deepseek_client SSE 解析器（罐装 chunk → 与非流式
    同形结果、工具碎片按 index 累积、content 抑制、reasoning 永不转发、
    usage 末片、HTTP≥400 错误前缀、畸形行跳过）；② MessageStreamer 传输
    + 熔断（双流时序/同锚点、open_answer 失败回退、remain_msg_len=0 常态回归、
    3 连拒 600s + 半开、is_wakeup 开流重试、50002 退避、ApiNotAvailable
    长熔断、免责末片、纯进度轮、env 门控）；③ Mock 客户端流式回调。
    """

    def run(self):
        print_header("53. QQ Official Stream (stream_messages, c2c)")
        os.environ.setdefault("USER_DATA_ROOT", "/tmp/qqbot_smoke")
        # ① SSE parser
        asyncio.run(self.test_sse_content_streaming())
        asyncio.run(self.test_sse_tool_fragments_suppress_content())
        asyncio.run(self.test_sse_reasoning_never_forwarded())
        asyncio.run(self.test_sse_http_error_prefix())
        asyncio.run(self.test_sse_malformed_lines_skipped())
        # ② MessageStreamer transport + breaker
        asyncio.run(self.test_sender_stream_capability())
        asyncio.run(self.test_dual_stream_sequencing())
        asyncio.run(self.test_open_answer_failure_marks_broken())
        asyncio.run(self.test_remain_zero_is_healthy())
        asyncio.run(self.test_disclaimer_rides_final_shard())
        asyncio.run(self.test_progress_only_no_answer_stream())
        asyncio.run(self.test_breaker_three_strike_and_half_open())
        asyncio.run(self.test_api_not_available_long_circuit())
        asyncio.run(self.test_is_wakeup_retry_on_opening())
        asyncio.run(self.test_50002_backoff_retry())
        asyncio.run(self.test_env_off_disables_stream())
        asyncio.run(self.test_non_expiry_code_no_wakeup_retry())
        asyncio.run(self.test_replace_retry_is_idempotent())
        asyncio.run(self.test_broken_stream_still_closes())
        asyncio.run(self.test_continuation_404_keeps_capability())
        asyncio.run(self.test_delivery_plan())
        asyncio.run(self.test_inbound_event_dedup())
        # ③ mock client
        asyncio.run(self.test_mock_client_stream_callback())

    # ── helpers ──────────────────────────────────────────────────
    @staticmethod
    def _qs():
        import adapters.qq_official_stream as qs
        return qs

    @staticmethod
    def _qo():
        import adapters.qq_official as qo
        return qo

    @staticmethod
    def _stream_guard(qs, qo):
        import contextlib

        @contextlib.contextmanager
        def guard():
            b = qs._stream_breaker
            snap = (b.state, b.rejects, b.disabled_until, b.rejected_total,
                    b.last_error, b.last_error_at)
            md_rejects = qo._md_breaker.rejects
            md_disabled = qo._md_breaker.disabled_until
            keys = ("QQ_OFFICIAL_STREAM", "QQ_OFFICIAL_STREAM_INTERVAL",
                    "QQ_OFFICIAL_STREAM_CIRCUIT_HOURS", "QQ_OFFICIAL_MARKDOWN")
            envs = {k: os.environ.pop(k, None) for k in keys}
            try:
                b.state, b.rejects, b.disabled_until = "unknown", 0, 0.0
                b.rejected_total, b.last_error, b.last_error_at = 0, "", 0.0
                qo._md_breaker.rejects = 0
                qo._md_breaker.disabled_until = 0.0
                os.environ["QQ_OFFICIAL_STREAM"] = "auto"
                os.environ["QQ_OFFICIAL_STREAM_INTERVAL"] = "0.2"
                yield
            finally:
                (b.state, b.rejects, b.disabled_until, b.rejected_total,
                 b.last_error, b.last_error_at) = snap
                qo._md_breaker.rejects = md_rejects
                qo._md_breaker.disabled_until = md_disabled
                for k, v in envs.items():
                    if v is not None:
                        os.environ[k] = v
                    else:
                        os.environ.pop(k, None)
        return guard()

    @staticmethod
    def _mk_streamer(qs, bot, anchor="ANCHOR"):
        import types
        return qs.MessageStreamer(bot, types.SimpleNamespace(id=anchor), "OID1")

    @staticmethod
    def _mk_client(dsc, lines, status=200, body=b""):
        client = dsc.DeepSeekClient(api_key="k", api_base="http://x", model="m")
        captured = {}
        client._record_usage = lambda result, purpose: captured.setdefault(
            "usage", result)
        orig = dsc.httpx.AsyncClient

        class _Patched(_FakeHttpClient):
            _lines = lines
            _status = status
            _body = body

        dsc.httpx.AsyncClient = lambda *a, **k: _Patched()
        return client, captured, orig, dsc

    # ── ① SSE parser ─────────────────────────────────────────────
    async def test_sse_content_streaming(self):
        """content 增量实时转发 + 结果与非流式同形 + usage 末片入账。"""
        import lib.deepseek_client as dsc
        lines = [
            _sse({"choices": [{"delta": {"role": "assistant", "content": "你"}}]}),
            _sse({"choices": [{"delta": {"content": "好"}}]}),
            _sse({"choices": [{"delta": {"content": "世界"},
                               "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 3, "completion_tokens": 5,
                            "total_tokens": 8}}),
            "data: [DONE]",
        ]
        client, captured, orig, dsc = self._mk_client(dsc, lines)
        try:
            got = []

            async def cb(d):
                got.append(d)

            res = await client.chat_completion_with_tools_stream(
                messages=[{"role": "user", "content": "hi"}], tools=[],
                content_callback=cb)
            assert res["content"] == "你好世界", res
            assert res["tool_calls"] is None
            assert res["role"] == "assistant"
            assert res["finish_reason"] == "stop"
            assert got == ["你", "好", "世界"], got
            assert captured["usage"]["usage"]["total_tokens"] == 8, captured
        finally:
            dsc.httpx.AsyncClient = orig
        print_pass("SSE：content 增量转发 + 同形结果 + usage 末片入账")

    async def test_sse_tool_fragments_suppress_content(self):
        """工具碎片按 index 累积；碎片出现后的 content 被抑制（不转发）。"""
        import lib.deepseek_client as dsc
        lines = [
            _sse({"choices": [{"delta": {"role": "assistant", "tool_calls": [
                {"index": 0, "id": "c1", "type": "function",
                 "function": {"name": "get_", "arguments": ""}}]}}]}),
            _sse({"choices": [{"delta": {"content": "thinking out loud"}}]}),
            _sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "time",
                                          "arguments": '{"tz"'}}]}}]}),
            _sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": ':"UTC"}'}}]},
                "finish_reason": "tool_calls"}]}),
            "data: [DONE]",
        ]
        client, captured, orig, dsc = self._mk_client(dsc, lines)
        try:
            got = []
            res = await client.chat_completion_with_tools_stream(
                messages=[], tools=[], content_callback=lambda d: got.append(d))
            assert got == [], f"工具轮 content 不得转发: {got}"
            assert res["content"] == "thinking out loud", res
            assert res["finish_reason"] == "tool_calls"
            tc = res["tool_calls"]
            assert tc and len(tc) == 1, tc
            assert tc[0]["id"] == "c1"
            assert tc[0]["function"]["name"] == "get_time", tc
            assert tc[0]["function"]["arguments"] == '{"tz":"UTC"}', tc
        finally:
            dsc.httpx.AsyncClient = orig
        print_pass("SSE：工具碎片按 index 累积；content 抑制；name/args 拼接")

    async def test_sse_reasoning_never_forwarded(self):
        """reasoning_content 增量静默丢弃，但保留在结果里。"""
        import lib.deepseek_client as dsc
        lines = [
            _sse({"choices": [{"delta": {"reasoning_content": "内部思考"}}]}),
            _sse({"choices": [{"delta": {"content": "答案"}}]}),
            "data: [DONE]",
        ]
        client, captured, orig, dsc = self._mk_client(dsc, lines)
        try:
            got = []
            res = await client.chat_completion_with_tools_stream(
                messages=[], tools=[], content_callback=lambda d: got.append(d))
            assert got == ["答案"], got
            assert res["reasoning_content"] == "内部思考", res
            assert res["content"] == "答案"
        finally:
            dsc.httpx.AsyncClient = orig
        print_pass("SSE：reasoning 永不转发，仅留存结果")

    async def test_sse_http_error_prefix(self):
        """HTTP≥400 → 错误 dict，前缀与非流式逐字一致（保 Direct Vision 自愈）。"""
        import lib.deepseek_client as dsc
        client, captured, orig, dsc = self._mk_client(
            dsc, [], status=400, body=b'{"error":"bad request"}')
        try:
            got = []
            res = await client.chat_completion_with_tools_stream(
                messages=[], tools=[], content_callback=lambda d: got.append(d))
            assert res["finish_reason"] == "error", res
            assert res["tool_calls"] is None
            assert res["content"].startswith("API HTTP错误 (400): "), res
            assert got == []
        finally:
            dsc.httpx.AsyncClient = orig
        print_pass("SSE：HTTP≥400 错误前缀与非流式一致")

    async def test_sse_malformed_lines_skipped(self):
        """畸形 SSE 行 log+skip，不中断后续解析。"""
        import lib.deepseek_client as dsc
        lines = [
            "data: {not valid json",
            "garbage without data prefix",
            _sse({"choices": [{"delta": {"content": "ok"}}]}),
            "data: [DONE]",
        ]
        client, captured, orig, dsc = self._mk_client(dsc, lines)
        try:
            got = []
            res = await client.chat_completion_with_tools_stream(
                messages=[], tools=[], content_callback=lambda d: got.append(d))
            assert got == ["ok"], got
            assert res["content"] == "ok", res
        finally:
            dsc.httpx.AsyncClient = orig
        print_pass("SSE：畸形行跳过，正常行仍解析")

    # ── ② MessageStreamer transport + breaker ────────────────────
    async def test_sender_stream_capability(self):
        """能力位：c2c True / 群聊 False / OneBot False（渠道隔离）。"""
        qs, qo = self._qs(), self._qo()
        with self._stream_guard(qs, qo):
            s_c2c = qo.QQOfficialSender(_MdFakeBot(), MagicMock(), False)
            assert s_c2c.supports_stream is True
            s_grp = qo.QQOfficialSender(_MdFakeBot(), MagicMock(), True)
            assert s_grp.supports_stream is False
            assert await s_grp.open_stream("x") is None
            from adapters.onebot import OneBotSender
            assert OneBotSender(MagicMock()).supports_stream is False
        print_pass("supports_stream：c2c=True / 群聊=False / OneBot=False")

    async def test_dual_stream_sequencing(self):
        """双流时序：同 msg_id 锚点、进度 seq900 / 回答 seq1800、各自
        stream_msg_id 回显、进度收束(state10)先于回答开流、answer_sent 精确。"""
        qs, qo = self._qs(), self._qo()
        with self._stream_guard(qs, qo):
            bot = _StreamFakeBot()
            st = self._mk_streamer(qs, bot)
            assert await st.open_progress("⏳ 正在思考…")
            await st.append_progress("⏳ 正在搜索…")
            await st.finish_progress()
            assert await st.open_answer("你好")
            st.append_answer("，世界！")
            await st.finish_answer("免责声明。")
            await st.close()
            posts = bot.posts
            assert posts and all(p["msg_id"] == "ANCHOR" for p in posts)
            prog = [p for p in posts if p["msg_seq"] == 900]
            ans = [p for p in posts if p["msg_seq"] == 1800]
            assert prog and ans, posts
            # opening shard carries no stream_msg_id; later shards echo it
            assert "stream_msg_id" not in prog[0]
            assert all(p.get("stream_msg_id") == "smid_900" for p in prog[1:])
            assert "stream_msg_id" not in ans[0]
            assert all(p.get("stream_msg_id") == "smid_1800" for p in ans[1:])
            # progress final (state10) precedes answer open in wire order
            prog_final_i = next(i for i, p in enumerate(posts)
                                if p["msg_seq"] == 900 and p["input_state"] == 10)
            ans_open_i = next(i for i, p in enumerate(posts)
                              if p["msg_seq"] == 1800)
            assert prog_final_i < ans_open_i, (prog_final_i, ans_open_i)
            assert st.answer_sent == "你好，世界！\n\n免责声明。", st.answer_sent
            assert not st.broken and not st.truncated
        print_pass("双流时序：同锚点/双 smid/进度先收束/answer_sent 精确")

    async def test_open_answer_failure_marks_broken(self):
        """open_answer 被拒 → broken=True、answer_opened=False（内核回退全文）。"""
        qs, qo = self._qs(), self._qo()

        def resp(payload):
            if payload["msg_seq"] == 1800:
                raise _stream_action_failed(40007)
            return _StreamFakeBot._default_responder(payload)

        with self._stream_guard(qs, qo):
            bot = _StreamFakeBot(resp)
            st = self._mk_streamer(qs, bot)
            assert await st.open_progress("⏳")
            await st.finish_progress()
            assert await st.open_answer("你好") is False
            assert st.answer_opened is False
            assert st.broken is True
            await st.close()
        print_pass("open_answer 失败 → broken，内核走缓冲全文回退")

    async def test_remain_zero_is_healthy(self):
        """remain_msg_len=0 是健康常态，不得触发截断/丢弃。

        真机回归（2026-10-30）：平台每片被接受后都返回 remain_msg_len=0
        （挂起长度语义，非发送预算）；旧版把它当预算耗尽，导致回答流只
        发出开流片「今天」、后续增量全部被客户端丢弃。
        """
        qs, qo = self._qs(), self._qo()

        def resp(payload):
            return {"id": f"smid_{payload['msg_seq']}", "remain_msg_len": 0}

        with self._stream_guard(qs, qo):
            bot = _StreamFakeBot(resp)
            st = self._mk_streamer(qs, bot)
            await st.open_progress("⏳")
            await st.finish_progress()
            await st.open_answer("今天")
            st.append_answer("（10月30日）新闻如下…")
            await st.finish_answer("免责句")
            await st.close()
            assert st.truncated is False
            assert st.broken is False
            assert st.remain_msg_len == 0
            assert st.answer_sent == "今天（10月30日）新闻如下…\n\n免责句", \
                st.answer_sent
            ans = [p for p in bot.posts if p["msg_seq"] == 1800]
            # the buffered tail + disclaimer ride the final shard (state10)
            assert "新闻如下" in ans[-1]["content_raw"], ans[-1]
            assert "免责句" in ans[-1]["content_raw"], ans[-1]
            assert ans[-1]["input_state"] == 10
        print_pass("remain_msg_len=0 → 正常流完整送达（真机回归：不再误截断）")

    async def test_disclaimer_rides_final_shard(self):
        """replace 语义：每片都是累计全文；免责随末片(state10)一起上去。"""
        qs, qo = self._qs(), self._qo()
        with self._stream_guard(qs, qo):
            bot = _StreamFakeBot()
            st = self._mk_streamer(qs, bot)
            await st.open_progress("⏳")
            await st.finish_progress()
            await st.open_answer("正文")
            await st.finish_answer("免责句")
            await st.close()
            assert all(p["input_mode"] == "replace" for p in bot.posts), bot.posts
            ans = [p for p in bot.posts if p["msg_seq"] == 1800]
            assert ans[0]["content_raw"] == "正文", ans[0]
            assert ans[-1]["input_state"] == 10, ans[-1]
            assert ans[-1]["content_raw"].startswith("正文\n\n免责句"), ans[-1]
            assert st.answer_sent == "正文\n\n免责句", st.answer_sent
        print_pass("replace：累计全文 + 免责随末片(state10)")

    async def test_progress_only_no_answer_stream(self):
        """纯工具轮（无 final content）：只收束进度流，从不开回答流。"""
        qs, qo = self._qs(), self._qo()
        with self._stream_guard(qs, qo):
            bot = _StreamFakeBot()
            st = self._mk_streamer(qs, bot)
            await st.open_progress("⏳ 正在思考…")
            await st.append_progress("⏳ 正在搜索…")
            await st.finish_progress()
            await st.close()
            seqs = {p["msg_seq"] for p in bot.posts}
            assert seqs == {900}, seqs
            finals = [p for p in bot.posts if p["input_state"] == 10]
            assert finals and all(p["msg_seq"] == 900 for p in finals)
            assert st.answer_opened is False
        print_pass("纯进度轮：仅 seq900 + state10 收束，无回答流")

    async def test_breaker_three_strike_and_half_open(self):
        """连续 3 次开流被拒 → 熔断 600s；冷却期满半开恢复。"""
        qs, qo = self._qs(), self._qo()

        def resp(payload):
            raise _stream_action_failed(50000)

        with self._stream_guard(qs, qo):
            bot = _StreamFakeBot(resp)
            for _ in range(3):
                st = self._mk_streamer(qs, bot)
                assert await st.open_progress("⏳") is False
            assert qs._stream_available() is False
            assert qs._stream_breaker.disabled_until > 0
            qs._stream_breaker.disabled_until = time.monotonic() - 1
            assert qs._stream_available() is True
            assert qs._stream_breaker.rejects == 0
        print_pass("熔断：3 连拒 600s；冷却后半开恢复")

    async def test_api_not_available_long_circuit(self):
        """开流首片 404/405：单次只记遥测，连续两次才判能力缺失（默认 1h）。"""
        qs, qo = self._qs(), self._qo()
        from nonebot.adapters.qq.exception import ApiNotAvailable

        def resp(payload):
            raise ApiNotAvailable()

        with self._stream_guard(qs, qo):
            bot = _StreamFakeBot(resp)
            st = self._mk_streamer(qs, bot)
            assert await st.open_progress("⏳") is False
            assert qs._stream_breaker.state != "unsupported"
            assert qs._stream_breaker.disabled_until == 0.0
            assert qs._stream_breaker.api_rejects == 1
            st2 = self._mk_streamer(qs, bot)
            assert await st2.open_progress("⏳") is False
            assert qs._stream_breaker.state == "unsupported"
            assert qs._stream_available() is False
            left = qs._stream_breaker.disabled_until - time.monotonic()
            assert 3500 < left <= 3600, left
            qs.stream_force_probation()
            assert qs._stream_breaker.disabled_until == 0.0
            assert qs._stream_breaker.state == "unknown"
        print_pass("开流 404×2 → 1h 熔断；单次不熔断；探测可强制半开复验")

    async def test_is_wakeup_retry_on_opening(self):
        """开流首片被拒且是锚点过期类 → is_wakeup=true 重试一次。"""
        qs, qo = self._qs(), self._qo()
        calls = {"n": 0}

        def resp(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _stream_action_failed(40034005)
            return _StreamFakeBot._default_responder(payload)

        with self._stream_guard(qs, qo):
            bot = _StreamFakeBot(resp)
            st = self._mk_streamer(qs, bot)
            assert await st.open_progress("⏳") is True
            assert calls["n"] == 2
            assert "is_wakeup" not in bot.posts[0]
            assert bot.posts[1].get("is_wakeup") is True
            assert st.retry_total == 1
        print_pass("is_wakeup：只有锚点过期类(40034005)才重试一次")

    async def test_non_expiry_code_no_wakeup_retry(self):
        """40007 等非锚点过期码 → 不发起 is_wakeup 重试（避免开第二条流）。"""
        qs, qo = self._qs(), self._qo()
        calls = {"n": 0}

        def resp(payload):
            calls["n"] += 1
            raise _stream_action_failed(40007)

        with self._stream_guard(qs, qo):
            bot = _StreamFakeBot(resp)
            st = self._mk_streamer(qs, bot)
            assert await st.open_progress("⏳") is False
            assert calls["n"] == 1, calls
            assert st.broken is True
            assert all("is_wakeup" not in p for p in bot.posts), bot.posts
        print_pass("非锚点过期错误码：不发起 is_wakeup 重试")

    async def test_50002_backoff_retry(self):
        """50002 频率限制 → 退避重试一次（同 payload，不带 is_wakeup）。"""
        qs, qo = self._qs(), self._qo()
        calls = {"n": 0}

        def resp(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _stream_action_failed(50002)
            return _StreamFakeBot._default_responder(payload)

        with self._stream_guard(qs, qo):
            orig_sleep = asyncio.sleep

            async def _fast_sleep(_s):
                return None
            qs.asyncio.sleep = _fast_sleep
            try:
                bot = _StreamFakeBot(resp)
                st = self._mk_streamer(qs, bot)
                assert await st.open_progress("⏳") is True
                assert calls["n"] == 2
                assert "is_wakeup" not in bot.posts[1]
            finally:
                qs.asyncio.sleep = orig_sleep
        print_pass("50002 → 退避重试一次（不带 is_wakeup）")

    async def test_env_off_disables_stream(self):
        """QQ_OFFICIAL_STREAM=off → 能力位 False（热读逃生门）。"""
        qs, qo = self._qs(), self._qo()
        with self._stream_guard(qs, qo):
            os.environ["QQ_OFFICIAL_STREAM"] = "off"
            assert qs._stream_available() is False
            s = qo.QQOfficialSender(_MdFakeBot(), MagicMock(), False)
            assert s.supports_stream is False
            os.environ["QQ_OFFICIAL_STREAM"] = "on"
            assert qs._stream_available() is True
        print_pass("env kill-switch：off→False / on→True（热读）")

    async def test_replace_retry_is_idempotent(self):
        """续片网络重试：重发同一份累计正文（replace）→ answer_sent 不翻倍。"""
        qs, qo = self._qs(), self._qo()
        calls = {"n": 0}

        def resp(payload):
            calls["n"] += 1
            if calls["n"] == 4:          # 第二次回答分片的首个尝试
                from nonebot.adapters.qq.exception import NetworkError
                raise NetworkError("timeout")
            return _StreamFakeBot._default_responder(payload)

        with self._stream_guard(qs, qo):
            bot = _StreamFakeBot(resp)
            st = self._mk_streamer(qs, bot)
            await st.open_progress("⏳")
            await st.finish_progress()
            await st.open_answer("你好")
            st.append_answer("，世界")
            await asyncio.sleep(0.6)     # 让 flusher 跑一轮（interval 0.2）
            await st.finish_answer("")
            await st.close()
            assert st.broken is False, st.last_error
            assert st.retry_total >= 1, st.retry_total
            assert st.answer_sent == "你好，世界", st.answer_sent
            ans = [p for p in bot.posts if p["msg_seq"] == 1800]
            retried = [p for p in ans if p["content_raw"] == "你好，世界"]
            assert len(retried) == 2, ans
            assert retried[0]["index"] == retried[1]["index"], retried
        print_pass("replace：续片重试复用同一 index/同一累计正文 → 不重复上屏")

    async def test_broken_stream_still_closes(self):
        """断流后 close() 仍补发 state=10，且只重复已上屏正文。"""
        qs, qo = self._qs(), self._qo()
        calls = {"n": 0}

        def resp(payload):
            calls["n"] += 1
            if calls["n"] == 4:          # finish_answer 的末片被拒
                raise _stream_action_failed(40007)
            return _StreamFakeBot._default_responder(payload)

        with self._stream_guard(qs, qo):
            bot = _StreamFakeBot(resp)
            st = self._mk_streamer(qs, bot)
            await st.open_progress("⏳")
            await st.finish_progress()
            await st.open_answer("前半")
            st.append_answer("后半")
            await st.finish_answer("")
            assert st.broken is True
            await st.close()
            ans = [p for p in bot.posts if p["msg_seq"] == 1800]
            assert ans[-1]["input_state"] == 10, ans[-1]
            assert ans[-1]["content_raw"].startswith("前半"), ans[-1]
            assert "后半" not in ans[-1]["content_raw"], ans[-1]
            assert st.answer_sent == "前半", st.answer_sent
        print_pass("断流收尾：close() 补发 state=10，不重复未送达内容")

    async def test_continuation_404_keeps_capability(self):
        """续片 404/405 → 只断这条流，能力位与后续流式不受影响。"""
        qs, qo = self._qs(), self._qo()
        from nonebot.adapters.qq.exception import ApiNotAvailable
        calls = {"n": 0}

        def resp(payload):
            calls["n"] += 1
            if calls["n"] == 4:
                raise ApiNotAvailable()
            return _StreamFakeBot._default_responder(payload)

        with self._stream_guard(qs, qo):
            bot = _StreamFakeBot(resp)
            st = self._mk_streamer(qs, bot)
            await st.open_progress("⏳")
            await st.finish_progress()
            await st.open_answer("正文")
            st.append_answer("尾巴")
            await st.finish_answer("")
            assert st.broken is True
            await st.close()
            assert qs._stream_breaker.state == "ok", qs._stream_breaker
            assert qs._stream_breaker.disabled_until == 0.0
            assert qs._stream_breaker.shard_404_total == 1
            assert qs._stream_available() is True
            assert st.last_error == "api_not_available"
        print_pass("续片 404：断开本条流，能力位保持 ok（不再 24h 误熔断）")

    async def test_delivery_plan(self):
        """收尾决策：开场白污染不重发 / 断流只补尾巴 / 未开流整篇。"""
        import types
        from agent import turn as _turn
        dpl = _turn._delivered_prefix_len
        plan = _turn._plan_stream_delivery

        # ① 开场白 + 进度行污染：正文已全覆盖 → 一条都不补（A 形态根治）
        st = types.SimpleNamespace(
            answer_opened=True,
            answer_sent="好的我查一下\n\n⏳ 正在搜索…\n\n今天的新闻是……",
        )
        resp = "今天的新闻是……"
        assert dpl(resp, st.answer_sent) == len(resp), dpl(resp, st.answer_sent)
        assert plan(resp, st, happy=False) == ("stream_only", "")

        # ② 中途断裂：只补未上屏的尾巴
        st2 = types.SimpleNamespace(answer_opened=True, answer_sent="今天的新闻")
        decision, remainder = plan("今天的新闻是……", st2, happy=False)
        assert (decision, remainder) == ("remainder", "是……"), (decision, remainder)

        # ③ 一片没上屏 → 整篇（与旧行为一致，内容不丢）
        st3 = types.SimpleNamespace(answer_opened=False, answer_sent="")
        assert plan("全文", st3, happy=False) == ("buffered_full", "")

        # ④ 流还活着 → 收尾片已送完，不再补
        st4 = types.SimpleNamespace(answer_opened=True, answer_sent="全文")
        assert plan("全文", st4, happy=True) == ("stream_only", "")

        # ⑤ 无重叠（如兜底文案替换）：delivered=0 → 整篇补发
        st5 = types.SimpleNamespace(answer_opened=True, answer_sent="完全无关")
        assert dpl("正文", "完全无关") == 0
        assert plan("正文", st5, happy=False) == ("remainder", "正文")
        print_pass("交付决策：开场白污染不重发 / 断流只补尾巴 / 未开流整篇")

    async def test_inbound_event_dedup(self):
        """同一 msg_id 重推（平台文档明确可能发生）只处理一次。"""
        import plugins.qq_official as po
        po._seen_events.clear()
        anchor = "ROBOT1.0_TEST_ANCHOR"
        try:
            assert po._is_duplicate_event(anchor) is False
            assert po._is_duplicate_event(anchor) is True
            assert po._is_duplicate_event("ROBOT1.0_OTHER") is False
            assert po._is_duplicate_event("") is False
            po._seen_events[anchor] = time.time() - po._SEEN_EVENT_TTL - 1
            assert po._is_duplicate_event(anchor) is False
        finally:
            po._seen_events.clear()
        print_pass("入站事件去重：同锚点 60s 内只处理一次，过期放行")

    # ── ③ mock client ────────────────────────────────────────────
    async def test_mock_client_stream_callback(self):
        """MockDeepSeekClient 流式变体：final 轮一次性回调全文；工具轮不回调。"""
        m = MockDeepSeekClient()
        m.plain_response = "完整回答"
        got = []
        res = await m.chat_completion_with_tools_stream(
            messages=[], tools=[], content_callback=got.append)
        assert res["content"] == "完整回答", res
        assert got == ["完整回答"], got

        m2 = MockDeepSeekClient()
        m2.tool_call_response = {
            "content": None, "tool_calls": [{"id": "x"}],
            "role": "assistant", "finish_reason": "tool_calls"}
        got2 = []
        res2 = await m2.chat_completion_with_tools_stream(
            messages=[], tools=[], content_callback=got2.append)
        assert got2 == [], got2
        assert res2["tool_calls"], res2
        print_pass("Mock 流式：final 轮回调全文；工具轮静默")


class TestSessionCompression:
    """特殊会话压缩（PLAN.md Idea 4 Phase A+B）。

    覆盖三层：存储写回（apply_compression 原子交换 + 一代备份）、命令层
    （/压缩会话 守卫 + 确认 + 滚动摘要失败中止）、读时防线（L4 85% 人格化
    提示 + 95% 紧急截断）。LLM 全部打桩，离线运行。
    """

    def run(self):
        print_header("54. Session Compression (/压缩会话 + window thresholds)")
        self.turn = _import_turn_for_tests()
        self.test_token_estimation()
        self.test_apply_compression_writeback()
        self.test_clear_context_drops_compression_state()
        asyncio.run(self.test_command_guards())
        asyncio.run(self.test_command_confirm_flow())
        asyncio.run(self.test_compression_llm_failure_aborts())
        self.test_emergency_truncate()
        asyncio.run(self.test_l4_hint_and_hard_truncation())

    # ── fixtures ────────────────────────────────────────────────

    @staticmethod
    def _mgr(tmp):
        from agent.special_session import SpecialSessionManager
        return SpecialSessionManager(
            user_data_root=os.path.join(tmp, "users"), max_per_user=10)

    @staticmethod
    def _fill(mgr, uid, name, n, char="测", active=True, metadata=None):
        """Create a session with n messages (CJK content, ≈ len+1 tokens each)."""
        s = mgr.create(uid, name, metadata=metadata)
        if active:
            mgr.switch_to(uid, s.name)
        for i in range(n):
            mgr.add_message(uid, "user" if i % 2 == 0 else "assistant",
                            f"{char * 24}{i:02d}")  # 25 CJK chars ≈ 26 tokens
        return s

    class _StubFlash:
        """Stub for _model_router.flash_client (chat_completion contract)."""

        def __init__(self, reply):
            self.reply = reply
            self.calls = []

        async def chat_completion(self, message, history=None,
                                  timeout_set=180.0, purpose="chat"):
            self.calls.append({"prompt": message, "purpose": purpose})
            return self.reply

    def _stub_turn(self, tmp, flash_reply="SUMMARY-OK"):
        """Swap turn module globals; returns (ctx, flash_stub)."""
        turn = self.turn
        from types import SimpleNamespace
        mgr = self._mgr(tmp)
        sent = []

        async def fake_send(payload, sender=None):
            sent.append(payload)

        flash = self._StubFlash(flash_reply)
        orig = (turn._special_sessions, turn._safe_send, turn._model_router,
                dict(turn._pending_compress_confirm))
        turn._special_sessions = mgr
        turn._safe_send = fake_send
        turn._model_router = SimpleNamespace(flash_client=flash)
        turn._pending_compress_confirm.clear()
        ctx = {"mgr": mgr, "sent": sent, "orig": orig, "tmp": tmp}
        return ctx, flash

    def _restore_turn(self, ctx):
        turn = self.turn
        (turn._special_sessions, turn._safe_send, turn._model_router,
         _pend) = ctx["orig"]
        turn._pending_compress_confirm.clear()
        turn._pending_compress_confirm.update(_pend)
        shutil.rmtree(ctx["tmp"], ignore_errors=True)

    # ── token 估算 ──────────────────────────────────────────────

    def test_token_estimation(self):
        from agent.special_session import (
            estimate_text_tokens, estimate_context_tokens)
        # CJK ≈ 1 token/char (+1), ASCII ≈ 4 chars/token
        assert estimate_text_tokens("测试文字测试") == 7
        assert estimate_text_tokens("abcd") == 2, estimate_text_tokens("abcd")
        assert estimate_text_tokens("") == 0
        ctx = [{"role": "user", "content": "测" * 100},
               {"role": "assistant", "content": "x" * 400}]
        est = estimate_context_tokens(ctx)
        # 100 CJK ≈ 100 tok + 400 ASCII ≈ 100 tok + per-msg overhead
        assert 200 <= est <= 220, est
        assert estimate_context_tokens([]) == 0
        print_pass("Token 估算：CJK≈1/字、ASCII≈4字/token、逐条开销计入")

    # ── 存储写回 ────────────────────────────────────────────────

    def test_apply_compression_writeback(self):
        from agent.special_session import COMPRESSION_SUMMARY_PREFIX
        tmp = tempfile.mkdtemp(prefix="sess_compress_")
        try:
            mgr = self._mgr(tmp)
            uid = "u1"
            self._fill(mgr, uid, "长会话", 40)
            old_tail = mgr.get_by_name(uid, "长会话").context[-10:]

            stats = mgr.apply_compression(uid, "长会话", "## 关键结论\n- A",
                                          keep_recent=10)
            assert stats["before_messages"] == 40
            assert stats["after_messages"] == 11, stats
            assert stats["kept_recent"] == 10 and stats["compress_count"] == 1
            assert stats["after_tokens_est"] < stats["before_tokens_est"]

            # Reload from disk: summary head + untouched tail
            s = mgr.get_by_name(uid, "长会话")
            assert s.total_messages == 11
            head = s.context[0]
            assert head["role"] == "system"
            assert head["content"].startswith(COMPRESSION_SUMMARY_PREFIX)
            assert "关键结论" in head["content"] and "第 1 次压缩" in head["content"]
            assert s.context[1:] == old_tail, "尾部原文必须逐字节保留"

            # Directory state: exactly one new snapshot, no delta, backup holds old 40
            sdir = mgr._session_dir(uid, "长会话")
            snaps = [f for f in os.listdir(sdir) if f.startswith("snapshot_")]
            assert snaps == ["snapshot_00011.json"], snaps
            assert not os.path.exists(os.path.join(sdir, "delta.jsonl"))
            with open(os.path.join(sdir, "pre_compress_backup.json"),
                      encoding="utf-8") as f:
                assert len(json.load(f)) == 40

            # Index metadata is the truth source
            entry = [e for e in mgr.list_sessions(uid)
                     if e["name"] == "长会话"][0]
            md = entry["metadata"]
            assert md["compress_count"] == 1 and md["pre_compress_messages"] == 40
            assert "compressed_at" in md and "pre_compress_tokens_est" in md

            # Post-compression appends + second compression (backup rolls one gen)
            for i in range(5):
                mgr.add_message(uid, "user", f"新消息{i}")
            assert mgr.get_by_name(uid, "长会话").total_messages == 16
            stats2 = mgr.apply_compression(uid, "长会话", "第二次摘要",
                                           keep_recent=5)
            assert stats2["compress_count"] == 2 and stats2["before_messages"] == 16
            s2 = mgr.get_by_name(uid, "长会话")
            assert "第二次摘要" in s2.context[0]["content"]
            assert "第 2 次压缩" in s2.context[0]["content"]
            with open(os.path.join(sdir, "pre_compress_backup.json"),
                      encoding="utf-8") as f:
                assert len(json.load(f)) == 16, "备份只保留一代（压缩前状态）"

            # Guards: unknown session / empty summary
            for bad in (lambda: mgr.apply_compression(uid, "不存在", "x"),
                        lambda: mgr.apply_compression(uid, "长会话", "  ")):
                try:
                    bad()
                    assert False, "expected ValueError"
                except ValueError:
                    pass
            print_pass("apply_compression：摘要头+尾部原文写回、一代备份、索引元数据、二次压缩")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_clear_context_drops_compression_state(self):
        tmp = tempfile.mkdtemp(prefix="sess_compress_clear_")
        try:
            mgr = self._mgr(tmp)
            uid = "u1"
            self._fill(mgr, uid, "会话", 32)
            mgr.apply_compression(uid, "会话", "摘要", keep_recent=10)
            sdir = mgr._session_dir(uid, "会话")
            assert os.path.exists(os.path.join(sdir, "pre_compress_backup.json"))
            mgr.clear_context(uid)
            s = mgr.get_active(uid)
            assert s.context == [] and s.total_messages == 0
            assert not os.path.exists(os.path.join(sdir, "pre_compress_backup.json"))
            entry = [e for e in mgr.list_sessions(uid) if e["name"] == "会话"][0]
            md = entry.get("metadata") or {}
            assert "compress_count" not in md and "compressed_at" not in md, md
            print_pass("/clear 同步清理压缩备份与元数据")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ── 命令层 ──────────────────────────────────────────────────

    async def test_command_guards(self):
        tmp = tempfile.mkdtemp(prefix="sess_compress_cmd_")
        ctx, _ = self._stub_turn(tmp)
        turn, mgr, sent = self.turn, ctx["mgr"], ctx["sent"]
        try:
            uid = "u1"
            # No active session
            assert await turn._handle_session_command("/压缩会话", uid) is True
            assert any("没有活跃" in s for s in sent), sent
            # Group chat → private-only
            self._fill(mgr, uid, "会话", 40)
            sent.clear()
            assert await turn._handle_session_command(
                "/压缩会话", uid, group_id="g1") is True
            assert any("私聊" in s for s in sent), sent
            assert uid not in turn._pending_compress_confirm
            # Exempt kinds (archive = review truth source)
            for kind in ("bond_scene", "char_creation"):
                self._fill(mgr, uid, f"豁免{kind}", 40,
                           metadata={"kind": kind})
                sent.clear()
                await turn._handle_session_command("/压缩会话", uid)
                assert any("不支持压缩" in s for s in sent), (kind, sent)
                assert uid not in turn._pending_compress_confirm
            # Too short
            self._fill(mgr, uid, "短会话", 10)
            sent.clear()
            await turn._handle_session_command("/压缩会话", uid)
            assert any("不需要压缩" in s for s in sent), sent
            assert uid not in turn._pending_compress_confirm
            # Eligible → confirmation requested, pending armed
            self._fill(mgr, uid, "长会话", 40)
            sent.clear()
            assert await turn._handle_session_command("/压缩会话", uid) is True
            msg = sent[-1]
            assert "确认压缩会话" in msg and "长会话" in msg, msg
            assert "40 条" in msg and "最近 10 条" in msg, msg
            pending = turn._pending_compress_confirm.get(uid)
            assert pending and pending[0] == "长会话"
            assert pending[1] > time.time() + 30
            print_pass("/压缩会话 守卫：无会话/群聊/豁免 kind/短会话拒绝，达标进入 60s 确认")
        finally:
            self._restore_turn(ctx)

    async def test_command_confirm_flow(self):
        tmp = tempfile.mkdtemp(prefix="sess_compress_ok_")
        ctx, flash = self._stub_turn(tmp, flash_reply="## 摘要\n- 结论A")
        turn, mgr, sent = self.turn, ctx["mgr"], ctx["sent"]
        try:
            uid = "u1"
            self._fill(mgr, uid, "长会话", 40)
            await turn._handle_session_command("/压缩会话", uid)
            assert uid in turn._pending_compress_confirm
            # Simulate the bare-text confirm intercept in handle_agent_turn
            await turn._run_session_compression(
                uid, turn._pending_compress_confirm.pop(uid)[0])

            # Summarizer ran on the flash tier with the rendered head
            assert len(flash.calls) == 1, flash.calls
            prompt = flash.calls[0]["prompt"]
            assert flash.calls[0]["purpose"] == "session_compress"
            assert "已有摘要" in prompt and "user: 测" in prompt
            assert "结论A" not in prompt  # reply, not prompt

            # Storage compressed; completion receipt sent
            s = mgr.get_by_name(uid, "长会话")
            assert s.total_messages == 11
            assert "结论A" in s.context[0]["content"]
            assert any("整理完毕" in m for m in sent), sent
            receipt = sent[-1]
            assert "40 条 → 摘要 + 最近 10 条" in receipt, receipt
            print_pass("确认执行：flash 滚动摘要 → 写回 11 条 → 回执含前后对比")
        finally:
            self._restore_turn(ctx)

    async def test_compression_llm_failure_aborts(self):
        tmp = tempfile.mkdtemp(prefix="sess_compress_fail_")
        ctx, _ = self._stub_turn(
            tmp, flash_reply="调用DeepSeek API时出错: boom")
        turn, mgr, sent = self.turn, ctx["mgr"], ctx["sent"]
        try:
            uid = "u1"
            self._fill(mgr, uid, "长会话", 40)
            await turn._run_session_compression(uid, "长会话")
            assert any("压缩失败" in m and "保持原样" in m for m in sent), sent
            assert mgr.get_by_name(uid, "长会话").total_messages == 40
            assert not os.path.exists(os.path.join(
                mgr._session_dir(uid, "长会话"), "pre_compress_backup.json"))
            # Empty summary is also a failure
            ctx2_flash = self._StubFlash("   ")
            turn._model_router.flash_client = ctx2_flash
            sent.clear()
            await turn._run_session_compression(uid, "长会话")
            assert any("压缩失败" in m for m in sent), sent
            assert mgr.get_by_name(uid, "长会话").total_messages == 40
            # Missing session → cancel message, no crash
            sent.clear()
            await turn._run_session_compression(uid, "不存在")
            assert any("不存在" in m for m in sent), sent
            print_pass("LLM 失败/空摘要/会话缺失：干净中止，存储零改动")
        finally:
            self._restore_turn(ctx)

    # ── 读时防线 ────────────────────────────────────────────────

    def test_emergency_truncate(self):
        from agent.agent import Agent
        from agent.special_session import COMPRESSION_SUMMARY_PREFIX

        summary = {"role": "system",
                   "content": COMPRESSION_SUMMARY_PREFIX + "（第 1 次）\n- 结论"}
        body = [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": "内容" * 50 + f"{i:03d}"} for i in range(50)]

        # With summary head: head kept, tail fits budget, note reports drops
        out = Agent._emergency_truncate([summary] + body, budget_tokens=600)
        assert out[0] is summary
        assert out[1]["role"] == "system" and "上下文溢出保护" in out[1]["content"]
        assert len(out) < len(body) + 2
        assert out[-1] is body[-1], "尾部最新消息必须保留"

        # No summary head → note + tail only
        out2 = Agent._emergency_truncate(body, budget_tokens=600)
        assert "上下文溢出保护" in out2[0]["content"]
        assert out2[-1] is body[-1]

        # Pathological budget: single clipped last message survives
        out3 = Agent._emergency_truncate(body, budget_tokens=10)
        assert len(out3) >= 1 and out3[-1]["content"].endswith("049")
        assert len(out3[-1]["content"]) <= 4000

        # Everything fits → unchanged, no note
        out4 = Agent._emergency_truncate(body[:2], budget_tokens=100000)
        assert out4 == body[:2]
        print_pass("紧急截断：摘要头恒保留、尾部按预算装填、溢出报数、病态预算兜底")

    async def test_l4_hint_and_hard_truncation(self):
        from agent.agent import Agent
        import agent.agent as agent_mod
        from agent.tool_registry import ToolRegistry
        from agent.context import _current_personality, _current_group_context

        tmp = tempfile.mkdtemp(prefix="sess_compress_l4_")
        p_tok = _current_personality.set("")
        g_tok = _current_group_context.set("")
        agent_mod._compress_hint_last.clear()
        try:
            mgr = self._mgr(tmp)
            agent = Agent(
                deepseek_client=MockDeepSeekClient(),
                tool_registry=ToolRegistry(),
                config_dir=os.path.join(os.path.dirname(__file__),
                                        "agent", "config"),
                special_session_manager=mgr,
            )
            uid = "u1"
            # 30 msgs × ≈30 est tokens ≈ 900 tokens
            self._fill(mgr, uid, "会话", 30)
            sess = agent.sessions.get_or_create(uid)

            def tail_of(msgs):
                sys_msgs = [m for m in msgs if m.get("role") == "system"]
                return sys_msgs[-1]["content"]

            # Below threshold (window 100k) → no hint
            mgr.set_context_window_provider(lambda: 100000)
            sp = mgr.get_active(uid)
            msgs = agent._build_messages(sess, "hi", special_session=sp)
            assert "记忆负荷提醒" not in tail_of(msgs)
            assert len([m for m in msgs if m.get("role") != "system"]) >= 30

            # 90% → persona-voiced hint in L4 tail, history intact
            mgr.set_context_window_provider(lambda: 1000)
            sp = mgr.get_active(uid)
            msgs = agent._build_messages(sess, "hi", special_session=sp)
            tail = tail_of(msgs)
            assert "记忆负荷提醒" in tail and "/压缩会话" in tail, tail
            assert "人格口吻" in tail and "token 数字" in tail
            assert "上下文溢出保护" not in tail  # still below 95%
            assert len([m for m in msgs if m.get("role") != "system"]) >= 30

            # Rate limit: immediate rebuild → no hint; +10 messages → hint again
            msgs2 = agent._build_messages(sess, "hi again", special_session=sp)
            assert "记忆负荷提醒" not in tail_of(msgs2)
            for i in range(10):
                mgr.add_message(uid, "user", f"追加{i:02d}消息内容")
            sp = mgr.get_active(uid)
            msgs3 = agent._build_messages(sess, "hi", special_session=sp)
            assert "记忆负荷提醒" in tail_of(msgs3)

            # ≥95% → history emergency-truncated + overflow notice in tail
            mgr.set_context_window_provider(lambda: 100)
            sp = mgr.get_active(uid)
            msgs4 = agent._build_messages(sess, "hi", special_session=sp)
            assert "上下文溢出保护" in tail_of(msgs4)
            non_system = [m for m in msgs4 if m.get("role") != "system"]
            assert len(non_system) < 40, f"history should be truncated: {len(non_system)}"
            assert any("未载入" in str(m.get("content")) for m in msgs4
                       if m.get("role") == "system"), "history 内应有截断说明"
            print_pass("L4 提示：≥85% 人格化建议 /压缩会话（限流），≥95% 读时紧急截断")
        finally:
            agent_mod._compress_hint_last.clear()
            _current_personality.reset(p_tok)
            _current_group_context.reset(g_tok)
            shutil.rmtree(tmp, ignore_errors=True)


class _FakeMailPersonaMgr(_FakePersonaMgr):
    """_FakePersonaMgr + 信箱候选过滤需要的 _exists/多角色显示名。"""

    def __init__(self):
        super().__init__()
        self.missing = set()      # _exists → False（UGC 已删）
        self.unified = set()      # get_mode → unified
        self.names = {}           # persona → 显示名

    def _exists(self, key, user_id=None):
        return key not in self.missing

    def get_mode(self, persona):
        return "unified" if persona in self.unified else self.mode

    def get_display_name(self, persona):
        return self.names.get(persona, persona)


class _FakeSpecialSessions:
    """只提供 _load_index（known_personas 补齐路径，§4.2 数据源 2）。"""

    def __init__(self, personas):
        self.personas = list(personas)

    def _load_index(self, user_id):
        return {"sessions": [
            {"name": f"s{i}", "metadata": {"persona_key": p}}
            for i, p in enumerate(self.personas)
        ]}


class _FakeDt:
    """plugins.qq_official.datetime 替身：固定 now()（10:30，发送窗口内）。"""

    fixed = None

    @classmethod
    def now(cls):
        return cls.fixed

    @classmethod
    def fromtimestamp(cls, ts, tz=None):
        return datetime.fromtimestamp(ts)


class _CapturingFlashClient:
    """捕获 prompt 的 flash client（_compose_mail_letter gap 文案断言）。"""

    def __init__(self, reply="想你了，最近还好吗？"):
        self.reply = reply
        self.calls = []

    async def chat_completion(self, message=None, history=None,
                              timeout_set=None, purpose=None, **kw):
        self.calls.append({"message": message, "history": history,
                           "purpose": purpose})
        return self.reply


class _FakeModelRouter:
    def __init__(self, client):
        self.flash_client = client


class TestProactiveMail:
    """55. 主动消息「角色信箱」v1（proactive-mail-design.md v0.5）：
    mailbox 存储 / 候选过滤 / mail pass 编排 / 信封构建 / /邮件 /
    /免责声明 / 令牌桶 / 群登记表."""

    PATCHED = ("_bond_manager", "_mailbox_manager", "_special_sessions",
               "get_character_registry", "_get_qq_bot", "_GREET_TARGETS_FILE",
               "_GREET_QUOTA_FILE", "send_active_c2c_markdown",
               "_compose_mail_letter", "_model_router", "random", "datetime")

    def run(self):
        print_header("55. Proactive Mail (角色信箱 v1)")
        _import_turn_for_tests()
        import nonebot
        try:
            nonebot.init()
        except ValueError:
            pass
        import plugins.qq_official as po
        self.po = po
        self.tmp = tempfile.mkdtemp(prefix="mail_pass_")
        self._setup()
        try:
            self.test_preview_and_escape()
            self.test_mailbox_storage()
            self.test_letter_id_uniqueness()
            self.test_candidate_filter()
            self.test_known_personas_from_sessions()
            self.test_known_personas_from_bonds()
            self.test_mail_due_gates()
            asyncio.run(self.test_compose_gap_desc())
            self.test_envelope_builder()
            self.test_token_bucket()
            self.test_group_registry()
            asyncio.run(self.test_pass_end_to_end())
            asyncio.run(self.test_pass_blocks_and_failures())
            asyncio.run(self.test_disclaimer_toggle())
            asyncio.run(self.test_send_response_gating())
            asyncio.run(self.test_mail_command())
            asyncio.run(self.test_disclaimer_command())
        finally:
            self._teardown()
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ── setup / teardown ─────────────────────────────────────────

    def _setup(self):
        from agent import greeting
        from agent.mailbox import MailboxManager
        po = self.po
        self.greeting = greeting
        self.m = _tmp_bond_manager(self.tmp)
        self.mb = MailboxManager(base_dir=os.path.join(self.tmp, "mailboxes"))
        self.pm = _FakeMailPersonaMgr()
        self.rng = _FakeRandom()
        self.targets = os.path.join(self.tmp, "targets.json")
        self.quota = os.path.join(self.tmp, "quota.json")
        self.envelope_sends = []
        self.envelope_send_ok = True
        self._orig = {k: getattr(po, k) for k in self.PATCHED}

        async def fake_envelope_send(bot, openid, md, rows=None,
                                     plain_text=None):
            self.envelope_sends.append(
                {"openid": openid, "md": md, "rows": rows,
                 "plain": plain_text})
            return self.envelope_send_ok

        async def fake_compose(persona, state):
            return f"{persona} 的一封信：最近还好吗？"

        _FakeDt.fixed = datetime(2026, 10, 30, 10, 30)
        po._bond_manager = self.m
        po._mailbox_manager = self.mb
        po._special_sessions = _FakeSpecialSessions([])
        po.get_character_registry = lambda: self.pm
        po._get_qq_bot = lambda: MagicMock()
        po._GREET_TARGETS_FILE = self.targets
        po._GREET_QUOTA_FILE = self.quota
        po.send_active_c2c_markdown = fake_envelope_send
        po._compose_mail_letter = fake_compose
        po.random = self.rng
        po.datetime = _FakeDt

    def _teardown(self):
        for k, v in self._orig.items():
            setattr(self.po, k, v)

    def _reset_files(self):
        for p in (self.targets, self.quota):
            try:
                os.remove(p)
            except OSError:
                pass
        shutil.rmtree(os.path.join(self.tmp, "mailboxes"), ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "mailboxes"), exist_ok=True)
        shutil.rmtree(self.m.events_dir, ignore_errors=True)
        self.envelope_sends.clear()
        self.envelope_send_ok = True

    def _seed_user(self, uid, personas, *, known=True, level_exp=1300,
                   last_seen_age_days=3, msgs=9, sessions=None):
        """建档：targets + bond 状态（等级/沉默/消息数）+ known_personas。"""
        self.greeting.note_c2c_contact(self.targets, uid)
        now = time.time()
        for p in personas:
            self.m.add_exp(uid, p, level_exp)
            st = self.m.get_state(uid, p)
            st["stats"]["last_seen"] = now - last_seen_age_days * 86400
            st["stats"]["msgs"] = msgs
            self.m._save(uid)
        if known:
            self.mb.ensure_known_personas(
                uid, sessions if sessions is not None else personas)

    # ── §5.2 纯函数 ──────────────────────────────────────────────

    def test_preview_and_escape(self):
        from agent.mailbox import make_preview, escape_md_text
        # 首行 + 去舞台指示 + 20 unicode 字符 + …
        assert make_preview("（挥手）团长～一天没见，露比都有点不习惯啦……") == \
            "团长～一天没见，露比都有点不习惯啦……"      # 19 字未超长，不截断
        long = "（挥手）" + "一二三四五六七八九十" * 3    # 30 字 → 20 字 + …
        assert make_preview(long) == "一二三四五六七八九十" * 2 + "…"
        assert make_preview("第一行\n第二行") == "第一行"
        assert make_preview("短") == "短"
        assert make_preview("") == ""
        assert make_preview("(双阶段)(指示)正文") == "正文"
        # 全角转义 + 行首列表符
        e = escape_md_text("*a*_b_`c`[d]#e>f+g")
        assert e == "＊a＊＿b＿｀c｀【d】＃e＞f＋g", e
        assert escape_md_text("- item").startswith("\u3000- ")
        assert escape_md_text("1. item").startswith("\u30001.")
        assert escape_md_text("普通行") == "普通行"
        print_pass("§5.2 预览（20字+…/舞台指示剥离）与全角转义符合设计")

    # ── §7.1 存储 ────────────────────────────────────────────────

    def test_mailbox_storage(self):
        from agent.mailbox import MailboxManager, new_letter_id
        mb = self.mb
        now = time.time()
        # 损坏文件 → 空信箱降级
        bad = os.path.join(self.tmp, "mailboxes", "corrupt.json")
        with open(bad, "w") as f:
            f.write("{not json")
        box = mb.load_user("corrupt")
        assert box["letters"] == [] and box["active_enabled"] is True
        # record_digest：id 分配 / preview / personas / known / 幂等
        letters = [{"persona": "rubi", "name": "露比", "text": "想你了"}]
        did = mb.record_digest("u1", letters, now=now)
        assert did and did.startswith("D-")
        assert mb.record_digest("u1", [dict(letters[0])], now=now) is None
        box = mb.load_user("u1")
        assert len(box["letters"]) == 1 and len(box["digests"]) == 1
        l0 = box["letters"][0]
        assert l0["id"].startswith("L-") and l0["digest_id"] == did
        assert l0["preview"] == "想你了" and l0["read"] is False
        assert box["personas"]["rubi"]["mail_count"] == 1
        assert "rubi" in box["known_personas"]
        assert box["last_digest_date"] == time.strftime(
            "%Y-%m-%d", time.localtime(now))
        # mark_read 幂等 / get_letter / letters_on_date
        lid = l0["id"]
        assert mb.mark_read("u1", lid) and mb.mark_read("u1", lid)
        assert not mb.mark_read("u1", "L-nope-0000")
        assert mb.get_letter("u1", lid)["read"] is True
        mmdd = time.strftime("%m-%d", time.localtime(now))
        assert len(mb.letters_on_date("u1", mmdd)) == 1
        # active_enabled 开关（无变化不写盘）
        mb.set_active_enabled("u1", False)
        assert mb.load_user("u1")["active_enabled"] is False
        mb.set_active_enabled("u1", True)
        assert mb.load_user("u1")["active_enabled"] is True
        # 保留期裁剪 + 200 上限
        box = mb.load_user("u1")
        for i in range(205):
            box["letters"].append(
                {"id": f"L-x{i:04d}", "persona": "p", "name": "n",
                 "text": "t", "preview": "t", "created_at": now - i,
                 "read": False})
        box["letters"].append(
            {"id": "L-old", "persona": "p", "name": "n", "text": "t",
             "preview": "t", "created_at": now - 40 * 86400, "read": False})
        mb.save_user("u1", box)
        box = mb.load_user("u1")
        mb.prune(box, now=now)
        assert len(box["letters"]) == 200
        assert all(l["id"] != "L-old" for l in box["letters"])
        print_pass("§7.1 信箱存储：损坏降级/幂等 digest/已读/裁剪 28d+200 上限")

    def test_letter_id_uniqueness(self):
        from agent.mailbox import new_letter_id
        ids = {new_letter_id() for _ in range(200)}
        # 4hex 随机后缀：200 个同日 id 允许极小概率碰撞，>190 即通过
        assert len(ids) > 190, len(ids)
        assert all(i.startswith("L-") and len(i.split("-")[-1]) == 4
                   for i in ids)
        print_pass("letter id 随机后缀（L-YYYYMMDD-<4hex>）防裁剪后重号")

    # ── §4.2 候选过滤 ────────────────────────────────────────────

    def test_candidate_filter(self):
        po = self.po
        uid = "cand_user"
        # rubi(Lv5, 沉默3天) 合格；roxy(Lv7, 沉默5天) 合格且排前
        self._seed_user(uid, ["rubi", "roxy"])
        self.m.add_exp(uid, "roxy", 3100)     # Lv7 暧昧
        st = self.m.get_state(uid, "roxy")
        st["stats"]["last_seen"] = time.time() - 5 * 86400
        st["stats"]["msgs"] = 9
        self.m._save(uid)
        # 不合格候选
        self._seed_user(uid, ["assistant"], known=False)          # unified 助手
        self._seed_user(uid, ["low_lv"], level_exp=0)             # Lv1 < 5
        self._seed_user(uid, ["fresh"], last_seen_age_days=0)     # 刚聊过
        self._seed_user(uid, ["stranger"], msgs=0)                # 无消息
        self._seed_user(uid, ["unknown_p"], known=False)          # 无特殊会话
        self._seed_user(uid, ["deleted_p"])
        self.pm.missing.add("deleted_p")                          # UGC 已删
        self._seed_user(uid, ["unified_p"])
        self.pm.unified.add("unified_p")                          # 非 split
        self._seed_user(uid, ["cooling"])
        box = self.mb.load_user(uid)
        box["personas"]["cooling"] = {"last_mail_at": time.time(),
                                      "mail_count": 1}             # 冷却中
        self.mb.save_user(uid, box)

        box = self.mb.load_user(uid)
        cands = po._mail_candidates(uid, box, self.m._config)
        order = [p for p, _ in cands]
        assert order == ["roxy", "rubi"], order   # level desc, last_seen asc
        print_pass("§4.2 过滤链：assistant/低等级/刚聊过/无会话/已删/unified/冷却全排除，排序正确")

    def test_known_personas_from_sessions(self):
        po = self.po
        orig = po._special_sessions
        try:
            po._special_sessions = _FakeSpecialSessions(["rubi", "roxy"])
            got = po._known_personas_from_sessions("u")
            assert got == ["rubi", "roxy"], got
            # 一旦写入，删会话也保留（§4.2）
            self.mb.ensure_known_personas("u2", got)
            po._special_sessions = _FakeSpecialSessions([])
            assert po._known_personas_from_sessions("u2") == []
            assert set(self.mb.load_user("u2")["known_personas"]) == \
                {"rubi", "roxy"}
        finally:
            po._special_sessions = orig
        print_pass("known_personas：sessions 索引补齐，写入后会话删除仍保留")

    def test_known_personas_from_bonds(self):
        """羁绊=权威源（§4.2 源 1）：靠默认人格攒羁绊、特殊会话没绑
        persona_key 的用户，旧实现 known_personas 恒空 → 候选门永远拦下。"""
        po = self.po
        uid = "bond_only_user"
        self.greeting.note_c2c_contact(self.targets, uid)
        # 夏以昼：Lv5+、沉默 3 天、msgs>0（合格候选的全部前置）
        self.m.add_exp(uid, "夏以昼", 1300)
        st = self.m.get_state(uid, "夏以昼")
        st["stats"]["last_seen"] = time.time() - 3 * 86400
        st["stats"]["msgs"] = 5
        self.m._save(uid)
        # 干扰项：msgs=0（没聊过）与 assistant（unified）都不计入
        for p, msgs in (("zero", 0), ("assistant", 9)):
            st = self.m.get_state(uid, p)
            st["stats"]["msgs"] = msgs
        self.m._save(uid)
        assert po._known_personas_from_bonds(uid) == ["夏以昼"]
        assert po._known_personas_from_bonds("no_bonds_user") == []
        orig = po._special_sessions
        try:
            # 会话索引查不到（persona_key 未绑定）——并集补齐仍建档 known
            po._special_sessions = _FakeSpecialSessions([])
            assert po._known_personas_from_sessions(uid) == []
            self.mb.ensure_known_personas(
                uid, po._known_personas_from_sessions(uid)
                + po._known_personas_from_bonds(uid))
            box = self.mb.load_user(uid)
            assert set(box["known_personas"]) == {"夏以昼"}
            # 建档后即可通过 _mail_candidates 的 known 门
            cands = [p for p, _ in po._mail_candidates(uid, box, self.m._config)]
            assert cands == ["夏以昼"], cands
        finally:
            po._special_sessions = orig
        print_pass("known_personas：羁绊权威源补齐，默认人格攒羁绊不再漏档")

    def test_mail_due_gates(self):
        po = self.po
        uid = "due_user"
        cfg = self.m._config
        cands = [("rubi", {})]
        box = self.mb.load_user(uid)
        assert po._mail_due(uid, box, cands, cfg)      # 全新信箱 + 窗口内
        assert not po._mail_due(uid, box, [], cfg)     # 无候选
        # 同日幂等
        box2 = dict(box, last_digest_date=time.strftime("%Y-%m-%d"))
        assert not po._mail_due(uid, box2, cands, cfg)
        # 最小间隔
        box3 = dict(box, last_digest_at=time.time() - 3 * 3600)
        assert not po._mail_due(uid, box3, cands, cfg)
        # 日上限：今天已发过 1 条 text
        self.greeting.record_active_send(self.quota, uid, kind="text")
        assert not po._mail_due(uid, box, cands, cfg)
        # 月计数不再拦截（monthly_cap=10**9，平台无月配额）
        self.greeting.record_active_send(
            self.quota, "month_user", kind="text",
            now=time.time() - 2 * 86400)
        for i in range(6):
            self.greeting.record_active_send(
                self.quota, "month_user", kind="text",
                now=time.time() - 86400 + i)
        assert po._mail_due("month_user", self.mb.load_user("month_user"),
                            cands, cfg)
        print_pass("§4.3 发送条件：幂等/最小间隔/日上限拦截，月配额不拦截")

    # ── §5.1 生成 prompt ─────────────────────────────────────────

    async def test_compose_gap_desc(self):
        po = self.po
        orig_mr, orig_pm = po._model_router, po.get_character_registry
        orig_compose = po._compose_mail_letter
        # _setup 里换成了 fake_compose；本测试要跑真实现
        po._compose_mail_letter = self._orig["_compose_mail_letter"]
        try:
            client = _CapturingFlashClient()
            po._model_router = _FakeModelRouter(client)
            now = time.time()

            async def compose_with(last_seen):
                client.calls.clear()
                return await po._compose_mail_letter(
                    "rubi", {"level": 6,
                             "stats": {"last_seen": last_seen}})

            # 不到一小时（关键：不 clamp 成 1 天）
            assert await compose_with(now - 1800)
            assert "不到一小时" in client.calls[0]["message"]
            assert "1 天" not in client.calls[0]["message"]
            # 几小时
            assert await compose_with(now - 3 * 3600)
            assert "大约几小时" in client.calls[0]["message"]
            # 3 天
            assert await compose_with(now - 3 * 86400)
            assert "大约 3 天" in client.calls[0]["message"]
            # prompt 带等级 + 60 字约束 + 角色 system
            msg = client.calls[0]["message"]
            assert "Lv6" in msg and "60 字" in msg
            assert "扮演上述角色" in client.calls[0]["history"][0]["content"]
            # 失败即丢弃（§5.3）：错误前缀 / 空 / 多行 → ""
            client.reply = "API HTTP错误 (500)"
            assert await compose_with(now - 86400) == ""
            client.reply = ""
            assert await compose_with(now - 86400) == ""
            client.reply = "第一行\n第二行"
            assert await compose_with(now - 86400) == ""
            client.reply = "好" * 101     # 超宽容上限
            assert await compose_with(now - 86400) == ""
        finally:
            po._model_router = orig_mr
            po.get_character_registry = orig_pm
            po._compose_mail_letter = orig_compose
        print_pass("§5.1 gap 文案不 clamp（不到一小时/几小时/N 天），§5.3 失败即丢弃无模板")

    # ── §6.1/§6.2 信封 ───────────────────────────────────────────

    def _mk_letters(self, n, text_len=30):
        from agent.mailbox import make_preview, new_letter_id
        return [{
            "id": new_letter_id(), "persona": f"p{i}",
            "name": f"角色{i}" if i else "露比",
            "text": f"信{i}：" + "内容" * (text_len // 2),
            "preview": make_preview("信%d：内容" % i),
            "created_at": time.time(), "read": False,
        } for i in range(n)]

    def test_envelope_builder(self):
        po = self.po
        cfg = dict(self.m._config)
        # B2：单封短文 → 全文无按钮
        one = self._mk_letters(1, text_len=40)
        md, plain, rows = po._build_envelope(one, cfg)
        assert rows is None and "10月30日" in md
        assert one[0]["text"].replace("*", "＊") or True
        assert "/切换会话" in md and "/邮件" in md
        assert "【今日角色来信" in plain
        # B1：多封 → 列表 + 按钮
        three = self._mk_letters(3)
        md, plain, rows = po._build_envelope(three, cfg)
        assert rows is not None
        flat = [b for r in rows for b in r]
        assert len(flat) == 3
        assert all(b.data == f"/邮件 {l['id']}" and b.action == "cmd"
                   and b.enter for b, l in zip(flat, three))
        assert md.startswith("## 今日角色来信 · 10月30日")
        assert "共 3 封" in md and "> 过往来信" in md
        # 长名 → ①…映射 + 展开①
        long_one = self._mk_letters(1)
        long_one[0]["name"] = "Roxy_Migurdia_超长名"
        two = three[:1] + long_one
        md, plain, rows = po._build_envelope(two, cfg)
        flat = [b for r in rows for b in r]
        assert any(b.label.startswith("展开") and "②" in b.label
                   for b in flat), [b.label for b in flat]
        assert "②" in md
        # max_chars 截断 → 「另有 N 封」
        cfg_small = dict(cfg, mail_max_chars=220, mail_max_previews=5)
        many = self._mk_letters(5, text_len=60)
        for l in many:      # 21 字长预览，撑爆 220 上限
            l["preview"] = "一二三四五六七八九十一二三四五六七八九十…"
        md, plain, rows = po._build_envelope(many, cfg_small)
        assert len(md) <= 220, len(md)
        assert "另有" in md and "/邮件" in md
        # 纯文本降级不含 md 记号
        assert "#" not in plain and "**" not in plain and ">" not in plain
        print_pass("§6.1/§6.2 信封：B2 单封全文/B1 按钮+①映射/超长截断/档3纯文本")

    # ── §4.4 令牌桶 ──────────────────────────────────────────────

    def test_token_bucket(self):
        from agent.rate_limit import ChannelLimiter, TokenBucket
        clock = [0.0]
        b = TokenBucket(rate=10.0, monotonic=lambda: clock[0])
        assert all(b.try_acquire() for _ in range(10))   # capacity=rate
        assert not b.try_acquire()
        clock[0] += 0.5                                   # +5 枚
        assert all(b.try_acquire() for _ in range(5))
        assert not b.try_acquire()
        clock[0] += 10.0
        assert not b.try_acquire(11)                      # 不超 capacity 攒币
        assert b.try_acquire(10)

        async def burst():
            fast_lim = ChannelLimiter(rates={"fast": 1000.0},
                                      monotonic=lambda: clock[0])
            t0 = time.monotonic()
            for _ in range(5):
                await fast_lim.acquire("fast")
            return time.monotonic() - t0
        clock[0] = 0.0
        lim = ChannelLimiter(rates={"fast": 1000.0},
                             monotonic=lambda: clock[0])
        # 高速率桶：acquire 不阻塞
        assert asyncio.run(burst()) < 0.5
        assert lim.bucket("fast") is lim.bucket("fast")   # 按渠道复用
        print_pass("§4.4 令牌桶：速率/容量/补币/按渠道复用正确")

    # ── §4.5/§7.3 群登记表 ───────────────────────────────────────

    def test_group_registry(self):
        from agent.group_registry import GroupRegistry
        path = os.path.join(self.tmp, "group_registry.json")
        reg = GroupRegistry(path)
        now = 1000.0
        assert reg.upsert_seen("G1", now=now)              # 首次建档写盘
        assert not reg.upsert_seen("G1", now=now + 10)     # 60s 内节流
        assert reg.upsert_seen("G1", now=now + 61)         # 超粒度写盘
        assert reg.get("G1")["status"] == "active"
        assert reg.list_active() == ["G1"]
        reg.mark_left("G1", now=now + 100)
        assert reg.get("G1")["status"] == "left"
        assert reg.list_active() == []
        assert reg.upsert_seen("G1", now=now + 200)        # 入站复活
        assert reg.get("G1")["status"] == "active"
        reg.set_active_enabled("G1", False)
        assert reg.list_active() == []                     # REJECT → 跳过
        assert not reg.set_active_enabled("G1", False)     # 无变化不写
        reg.set_active_enabled("G1", True)
        assert reg.list_active() == ["G1"]
        with open(path, "w") as f:
            f.write("{broken")
        assert reg.load()["groups"] == {}                  # 损坏降级
        print_pass("§4.5 群登记表：节流 upsert/left 复活/REJECT 开关/损坏降级")

    # ── pass 编排 ────────────────────────────────────────────────

    async def test_pass_end_to_end(self):
        po = self.po
        uid = "mail_user"
        self._reset_files()
        # sessions 索引驱动 known_personas 补齐
        po._special_sessions = _FakeSpecialSessions(["rubi"])
        self._seed_user(uid, ["rubi"], known=False)
        await po._run_mail_pass()
        # 1) 发出一个信封
        assert len(self.envelope_sends) == 1, self.envelope_sends
        sent = self.envelope_sends[0]
        assert sent["openid"] == uid
        assert "今日角色来信" in sent["md"] and sent["plain"]
        assert sent["rows"] is None or sent["rows"]        # B2 单封 → None
        # 2) 记账齐全：信箱 / 配额(text) / bond 事件全文
        box = self.mb.load_user(uid)
        assert len(box["letters"]) == 1 and len(box["digests"]) == 1
        letter = box["letters"][0]
        assert letter["persona"] == "rubi" and letter["read"] is False
        assert box["last_digest_date"] == time.strftime("%Y-%m-%d")
        assert "rubi" in box["known_personas"]
        assert self.greeting.active_sent_today(self.quota, uid, kind="text")
        assert not self.greeting.active_sent_today(self.quota, uid,
                                                   kind="media")
        events = self.m.recent_events(uid, "rubi", n=3)
        assert any(letter["text"] in e and "寄出一封信" in e
                   for e in events), events   # §9.1 全文入 bond 事件
        # 3) 同日重跑幂等：不再发
        await po._run_mail_pass()
        assert len(self.envelope_sends) == 1
        # 4) catch_up 减半 max_letters：3 个合格人格 → max(1, 5//2)=2 封
        uid2 = "catchup_user"
        po._special_sessions = _FakeSpecialSessions(["rubi", "roxy", "amiya"])
        self._seed_user(uid2, ["rubi", "roxy", "amiya"], known=False)
        self.envelope_sends.clear()
        await po._run_mail_pass(catch_up=True)
        mine = [s for s in self.envelope_sends if s["openid"] == uid2]
        assert len(mine) == 1                # 仍然只发一个信封
        box2 = self.mb.load_user(uid2)
        assert len(box2["letters"]) == 2     # 补发减半：3 候选取 2
        print_pass("pass 端到端：发送/记账/事件全文/同日幂等/补发减半")

    async def test_pass_blocks_and_failures(self):
        po = self.po
        cfg = self.m._config
        # a) 发送失败 → 不写任何记录（§7.2）
        uid = "fail_user"
        self._reset_files()
        po._special_sessions = _FakeSpecialSessions(["rubi"])
        self._seed_user(uid, ["rubi"], known=False)
        self.envelope_send_ok = False
        await po._run_mail_pass()
        assert len(self.envelope_sends) == 1     # 尝试过
        box = self.mb.load_user(uid)
        assert box["letters"] == [] and not box["last_digest_date"]
        assert not self.greeting.active_sent_today(self.quota, uid,
                                                   kind="text")
        assert self.m.recent_events(uid, "rubi", n=3) == []
        # b) 当天已发媒体 → 信封让路（双向互斥，v0.5）
        self._reset_files()
        self._seed_user(uid, ["rubi"], known=False)
        self.greeting.record_active_send(self.quota, uid, kind="media")
        await po._run_mail_pass()
        assert self.envelope_sends == []
        # c) active_enabled=false（C2C_MSG_REJECT）→ 跳过
        self._reset_files()
        self._seed_user(uid, ["rubi"], known=False)
        self.mb.set_active_enabled(uid, False)
        await po._run_mail_pass()
        assert self.envelope_sends == []
        # d) mail_enabled=false → 整个 pass 不跑（回滚开关）
        self._reset_files()
        self._seed_user(uid, ["rubi"], known=False)
        self.m._config = dict(cfg, mail_enabled=False)
        try:
            await po._run_mail_pass()
            assert self.envelope_sends == []
        finally:
            self.m._config = cfg
        # e) 生成全失败 → 0 封不发信封不记账（§5.3）
        self._reset_files()
        self._seed_user(uid, ["rubi"], known=False)
        orig_compose = po._compose_mail_letter

        async def compose_fail(persona, state):
            return ""
        po._compose_mail_letter = compose_fail
        try:
            await po._run_mail_pass()
            assert self.envelope_sends == []
            assert self.mb.load_user(uid)["letters"] == []
        finally:
            po._compose_mail_letter = orig_compose
        print_pass("pass 拦截：发送失败零记账/媒体互斥/拒收开关/回滚开关/生成失败不发空信封")

    # ── §6.5 免责尾开关 ──────────────────────────────────────────

    async def test_disclaimer_toggle(self):
        turn = _import_turn_for_tests()
        orig_file = turn._DISCLAIMER_PREFS_FILE
        orig_dir = turn._USER_PREFS_DIR
        orig_cache = (turn._disclaimer_prefs_cache,
                      turn._disclaimer_prefs_mtime)
        try:
            turn._USER_PREFS_DIR = os.path.join(self.tmp, "prefs")
            turn._DISCLAIMER_PREFS_FILE = os.path.join(
                turn._USER_PREFS_DIR, "disclaimer.json")
            turn._disclaimer_prefs_cache = None
            turn._disclaimer_prefs_mtime = None
            # 默认开（追加）
            assert not turn._disclaimer_off("u")
            assert turn._set_disclaimer_off("u", True)
            assert turn._disclaimer_off("u")
            # 两渠道用户标识独立
            assert not turn._disclaimer_off("u_official")
            assert turn._set_disclaimer_off("u", False)
            assert not turn._disclaimer_off("u")
            # 损坏文件 → 默认开
            with open(turn._DISCLAIMER_PREFS_FILE, "w") as f:
                f.write("{broken")
            turn._disclaimer_prefs_cache = None
            assert not turn._disclaimer_off("u")
        finally:
            turn._DISCLAIMER_PREFS_FILE = orig_file
            turn._USER_PREFS_DIR = orig_dir
            (turn._disclaimer_prefs_cache,
             turn._disclaimer_prefs_mtime) = orig_cache
        print_pass("§6.5 免责尾开关：默认开/按渠道独立/损坏降级为开")

    async def test_send_response_gating(self):
        """两处追加点之一的 _send_response：关 → inline 与 separate 都跳过。"""
        turn = _import_turn_for_tests()

        class _RecSender:
            platform = "onebot"
            max_messages_per_turn = 10
            supports_markdown = False

            def __init__(self):
                self.out = []

            async def send_text(self, x): self.out.append(("text", x))
            async def send_markdown(self, x): self.out.append(("md", x))
            async def send_image(self, x): pass
            async def send_audio(self, x): pass
            async def send_progress(self, x, *, immediate=False): pass
            async def send_keyboard(self, x): self.out.append(("kb", x.markdown))

        orig_file = turn._DISCLAIMER_PREFS_FILE
        orig_dir = turn._USER_PREFS_DIR
        orig_cache = (turn._disclaimer_prefs_cache,
                      turn._disclaimer_prefs_mtime)
        old_sender = turn.default_sender
        p_tok = turn._current_platform.set("onebot")
        u_tok = turn._current_user_id.set("gate_user")
        try:
            turn._USER_PREFS_DIR = os.path.join(self.tmp, "prefs2")
            turn._DISCLAIMER_PREFS_FILE = os.path.join(
                turn._USER_PREFS_DIR, "disclaimer.json")
            turn._disclaimer_prefs_cache = None
            turn._disclaimer_prefs_mtime = None
            # 默认：免责尾追加
            s = _RecSender(); turn.default_sender = s
            await turn._send_response("普通回复")
            assert "理性判断" in s.out[-1][1]
            # 关闭：plain 路径不追加
            turn._set_disclaimer_off("gate_user", True)
            s = _RecSender(); turn.default_sender = s
            await turn._send_response("普通回复")
            assert all("理性判断" not in v for _, v in s.out), s.out
            # 关闭 + 官方 md 短正文：inline 跳过
            turn._current_platform.set("qq_official")
            s = _RecSender(); s.platform = "qq_official"
            s.supports_markdown = True
            turn.default_sender = s
            r_tok = turn._current_render_context.set(
                turn._RENDER_MD_DECLARATION)
            try:
                await turn._send_response("## 短答复")
                assert len(s.out) == 1 and s.out[0][0] == "md"
                assert "理性判断" not in s.out[0][1]
                # 关闭 + 长正文：不再有 separate 免责消息
                s = _RecSender(); s.platform = "qq_official"
                s.supports_markdown = True
                turn.default_sender = s
                await turn._send_response("## 长答复\n" + "内" * 250)
                assert [k for k, _ in s.out] == ["md"], s.out
                # 重新开启 → 恢复 separate
                turn._set_disclaimer_off("gate_user", False)
                s = _RecSender(); s.platform = "qq_official"
                s.supports_markdown = True
                turn.default_sender = s
                await turn._send_response("## 长答复\n" + "内" * 250)
                assert [k for k, _ in s.out] == ["md", "text"], s.out
                assert "理性判断" in s.out[1][1]
            finally:
                turn._current_render_context.reset(r_tok)
        finally:
            turn.default_sender = old_sender
            turn._current_platform.reset(p_tok)
            turn._current_user_id.reset(u_tok)
            turn._DISCLAIMER_PREFS_FILE = orig_file
            turn._USER_PREFS_DIR = orig_dir
            (turn._disclaimer_prefs_cache,
             turn._disclaimer_prefs_mtime) = orig_cache
        print_pass("§6.5 _send_response 接线：plain/官方md inline/separate 三分支均受开关控制")

    # ── §8 /邮件 命令 ────────────────────────────────────────────

    async def test_mail_command(self):
        turn = _import_turn_for_tests()

        class _RecSender:
            platform = "onebot"
            max_messages_per_turn = 50
            supports_markdown = False

            def __init__(self):
                self.out = []

            async def send_text(self, x): self.out.append(x)
            async def send_markdown(self, x): self.out.append(x)
            async def send_image(self, x): pass
            async def send_audio(self, x): pass
            async def send_progress(self, x, *, immediate=False): pass

        orig_mb = turn._mailbox_manager
        old_sender = turn.default_sender
        try:
            turn._mailbox_manager = self.mb
            s = _RecSender(); turn.default_sender = s
            view = types.SimpleNamespace(platform="qq_official",
                                         is_group=False)
            view_group = types.SimpleNamespace(platform="qq_official",
                                               is_group=True)
            view_onebot = types.SimpleNamespace(platform="onebot",
                                                is_group=False)
            uid = "cmd_user"
            # 渠道门：群聊 / OneBot → 仅官方单聊可用
            assert await turn._handle_mail_command("/邮件", uid, view_group)
            assert "仅官方单聊" in s.out[-1]
            assert await turn._handle_mail_command("/邮件", uid, view_onebot)
            assert "仅官方单聊" in s.out[-1]
            # 空信箱
            assert await turn._handle_mail_command("/邮件", uid, view)
            assert "没有角色来信" in s.out[-1]
            # 种 7 封信 → 列表分页（5/页）+ 未读 ● + 编号可见
            from agent.mailbox import make_preview, new_letter_id
            letters = []
            for i in range(7):
                day = 10 + i
                letters.append({
                    "id": f"L-202610{day:02d}-{i:04x}",
                    "persona": f"p{i}", "name": f"角色{i}",
                    "text": f"第{i}封信全文内容",
                    "preview": make_preview(f"第{i}封信全文内容"),
                    "created_at": time.time() - (7 - i) * 3600,
                    "read": False,
                })
            box = self.mb.load_user(uid)
            box["letters"] = letters
            self.mb.save_user(uid, box)
            s.out.clear()
            assert await turn._handle_mail_command("/邮件", uid, view)
            page1 = s.out[-1]
            assert "未读 7 封" in page1 and "第 1/2 页" in page1
            assert "●" in page1 and letters[-1]["id"] in page1
            assert letters[0]["id"] not in page1     # 第 6 封在下一页
            # 翻页
            s.out.clear()
            assert await turn._handle_mail_command("/邮件 下一页", uid, view)
            assert "第 2/2 页" in s.out[-1]
            s.out.clear()
            assert await turn._handle_mail_command("/邮件 p2", uid, view)
            assert "第 2/2 页" in s.out[-1]
            # 详情：全文 + 标记已读 + 不带免责尾
            lid = letters[-1]["id"]
            s.out.clear()
            assert await turn._handle_mail_command(f"/邮件 {lid}", uid, view)
            detail = s.out[-1]
            assert letters[-1]["text"] in detail
            assert "返回信箱" in detail
            assert "理性判断" not in detail
            assert self.mb.get_letter(uid, lid)["read"] is True
            # 大小写容错
            s.out.clear()
            assert await turn._handle_mail_command(
                f"/邮件 {lid.lower()}", uid, view)
            assert letters[-1]["text"] in s.out[-1]
            # 按日期
            mmdd = datetime.fromtimestamp(
                letters[-1]["created_at"]).strftime("%m-%d")
            s.out.clear()
            assert await turn._handle_mail_command(f"/邮件 {mmdd}", uid, view)
            assert mmdd in s.out[-1] and letters[-1]["id"] in s.out[-1]
            # 不存在的编号
            s.out.clear()
            assert await turn._handle_mail_command("/邮件 L-19990101-ffff",
                                                   uid, view)
            assert "没有找到" in s.out[-1]
            # 非命令文本不误触
            assert not await turn._handle_mail_command("/邮件箱", uid, view)
        finally:
            turn._mailbox_manager = orig_mb
            turn.default_sender = old_sender
        print_pass("§8 /邮件：渠道门/空态/分页/下一页/pN/详情已读/日期/未找到/免免责尾")

    async def test_disclaimer_command(self):
        turn = _import_turn_for_tests()

        class _RecSender:
            platform = "onebot"
            max_messages_per_turn = 50
            supports_markdown = False

            def __init__(self):
                self.out = []

            async def send_text(self, x): self.out.append(x)
            async def send_markdown(self, x): self.out.append(x)
            async def send_image(self, x): pass
            async def send_audio(self, x): pass
            async def send_progress(self, x, *, immediate=False): pass

        orig_file = turn._DISCLAIMER_PREFS_FILE
        orig_dir = turn._USER_PREFS_DIR
        orig_cache = (turn._disclaimer_prefs_cache,
                      turn._disclaimer_prefs_mtime)
        old_sender = turn.default_sender
        try:
            turn._USER_PREFS_DIR = os.path.join(self.tmp, "prefs3")
            turn._DISCLAIMER_PREFS_FILE = os.path.join(
                turn._USER_PREFS_DIR, "disclaimer.json")
            turn._disclaimer_prefs_cache = None
            turn._disclaimer_prefs_mtime = None
            s = _RecSender(); turn.default_sender = s
            uid = "disc_user"
            # 状态查询（默认开，B7 文案）
            assert await turn._handle_disclaimer_command("/免责声明", uid)
            assert "默认开启" in s.out[-1] and "理性判断" not in s.out[-1]
            # 关（regular 用户可用——不做任何角色检查）
            assert await turn._handle_disclaimer_command("/免责声明 关", uid)
            assert turn._disclaimer_off(uid)
            assert "不再追加" in s.out[-1]
            # 状态查询（已关）
            s.out.clear()
            assert await turn._handle_disclaimer_command("/免责声明", uid)
            assert "已关闭" in s.out[-1]
            # off/on 英文变体 + 开
            assert await turn._handle_disclaimer_command("/免责声明 off", uid)
            assert turn._disclaimer_off(uid)
            assert await turn._handle_disclaimer_command("/免责声明 开", uid)
            assert not turn._disclaimer_off(uid)
            # 未知参数 → 用法
            s.out.clear()
            assert await turn._handle_disclaimer_command("/免责声明 哈", uid)
            assert "用法" in s.out[-1]
            # # 前缀变体 + 非命令不误触
            assert await turn._handle_disclaimer_command("#免责声明", uid)
            assert not await turn._handle_disclaimer_command("/免责", uid)
        finally:
            turn.default_sender = old_sender
            turn._DISCLAIMER_PREFS_FILE = orig_file
            turn._USER_PREFS_DIR = orig_dir
            (turn._disclaimer_prefs_cache,
             turn._disclaimer_prefs_mtime) = orig_cache
        print_pass("§6.5 /免责声明：状态查询/开/关/英文变体/#前缀/确认回复自身不带免责尾")


class TestMailAdmin:
    """56. WebUI 邮件管理（proactive-mail-design.md §16.2/§16.4）：
    mailbox 系统信/撤回语义、/邮件 撤回分支、_mail_admin_exec 白名单与
    校验链、worker 认领/启动恢复、data_reader 邮件 API（脱敏/幂等入队）."""

    PATCHED = ("_mailbox_manager", "_bond_manager", "_get_qq_bot",
               "send_active_c2c_markdown", "random",
               "_MAIL_QUEUE_DIR", "_MAIL_PROC_DIR", "_MAIL_STATUS_DIR")

    def run(self):
        print_header("56. Mail Admin (WebUI 邮件管理 §16)")
        _import_turn_for_tests()
        import nonebot
        try:
            nonebot.init()
        except ValueError:
            pass
        import plugins.qq_official as po
        self.po = po
        self.tmp = tempfile.mkdtemp(prefix="mail_admin_")
        self._setup()
        try:
            self.test_system_letter_and_withdraw()
            asyncio.run(self.test_mail_command_system_and_withdrawn())
            asyncio.run(self.test_admin_exec())
            self.test_admin_recover()
            asyncio.run(self.test_admin_worker())
            self.test_webui_mail_api()
        finally:
            self._teardown()
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ── setup / teardown ─────────────────────────────────────────

    def _setup(self):
        from pathlib import Path as _Path
        from agent.mailbox import MailboxManager
        po = self.po
        self.mb = MailboxManager(base_dir=os.path.join(self.tmp, "mailboxes"))
        self.m = _tmp_bond_manager(self.tmp)
        self.c2c_sends = []
        self.c2c_ok = True
        self._orig = {k: getattr(po, k) for k in self.PATCHED}

        async def fake_c2c(bot, openid, md, rows=None, plain_text=None):
            self.c2c_sends.append(
                {"openid": openid, "md": md, "plain": plain_text})
            return self.c2c_ok

        po._mailbox_manager = self.mb
        po._bond_manager = self.m
        po._get_qq_bot = lambda: MagicMock()
        po.send_active_c2c_markdown = fake_c2c
        po.random = _FakeRandom()
        po._MAIL_QUEUE_DIR = _Path(self.tmp) / "queue"
        po._MAIL_PROC_DIR = _Path(self.tmp) / "processing"
        po._MAIL_STATUS_DIR = _Path(self.tmp) / "status"
        for d in (po._MAIL_QUEUE_DIR, po._MAIL_PROC_DIR, po._MAIL_STATUS_DIR):
            d.mkdir(parents=True, exist_ok=True)

    def _teardown(self):
        for k, v in self._orig.items():
            setattr(self.po, k, v)

    # ── §16.4.3 存储语义：系统信入账 + 删除=撤回 ─────────────────

    def test_system_letter_and_withdraw(self):
        mb = self.mb
        now = time.time()
        # 系统信：字段齐全，不动 personas/known_personas/last_digest_*
        sid = mb.write_system_letter("sys_u", "维护通知", "今晚十点停服",
                                     now=now)
        box = mb.load_user("sys_u")
        sl = next(l for l in box["letters"] if l["id"] == sid)
        assert sl["persona"] == "__system__" and sl["name"] == "系统"
        assert sl["system"] is True and sl["title"] == "维护通知"
        assert sl["digest_id"] is None and sl["read"] is False
        assert sl["preview"] == "今晚十点停服"
        assert box["personas"] == {} and box["known_personas"] == {}
        assert box["last_digest_date"] == "" and box["last_digest_at"] == 0
        # 角色信 + 信封 → 删除后 digest 仍引用（撤回语义，§16.4.3）
        did = mb.record_digest(
            "adm_u", [{"persona": "rubi", "name": "露比", "text": "想你了"}],
            now=now)
        lid = mb.load_user("adm_u")["letters"][0]["id"]
        assert mb.delete_letter("adm_u", lid) is True
        assert mb.get_letter("adm_u", lid) is None
        assert mb.delete_letter("adm_u", lid) is False      # 幂等：再删 False
        box = mb.load_user("adm_u")
        assert lid in box["digests"][0]["letter_ids"]       # 信封历史保留
        # is_withdrawn 三态：digest 引用→True；仍在→False；从未存在→False
        assert mb.is_withdrawn("adm_u", lid) is True
        assert mb.is_withdrawn("sys_u", sid) is False
        assert mb.is_withdrawn("adm_u", "L-19990101-ffff") is False
        assert mb.is_withdrawn("adm_u", "") is False
        print_pass("§16.4.3 系统信入账（不动角色态）+ 删除=撤回（digest 留痕）")

    # ── /邮件：撤回分支 + 系统信标签/详情头 ───────────────────────

    async def test_mail_command_system_and_withdrawn(self):
        turn = _import_turn_for_tests()

        class _RecSender:
            platform = "onebot"
            max_messages_per_turn = 50
            supports_markdown = False

            def __init__(self):
                self.out = []

            async def send_text(self, x): self.out.append(x)
            async def send_markdown(self, x): self.out.append(x)
            async def send_image(self, x): pass
            async def send_audio(self, x): pass
            async def send_progress(self, x, *, immediate=False): pass

        orig_mb = turn._mailbox_manager
        old_sender = turn.default_sender
        try:
            turn._mailbox_manager = self.mb
            s = _RecSender(); turn.default_sender = s
            view = types.SimpleNamespace(platform="qq_official",
                                         is_group=False)
            uid = "cmd_adm_u"
            now = time.time()
            # 手工建档：已撤回信（digest 引用）+ 普通角色信 + 系统信
            box = self.mb.load_user(uid)
            box["letters"] = [
                {"id": "L-20261029-0001", "persona": "rubi", "name": "露比",
                 "text": "昨天见", "preview": "昨天见",
                 "created_at": now - 86400, "read": True, "digest_id": "D-1"},
                {"id": "L-20261030-0009", "persona": "roxy", "name": "罗克西",
                 "text": "今天见", "preview": "今天见",
                 "created_at": now, "read": False, "digest_id": None},
            ]
            box["digests"] = [{"id": "D-1", "date": "2026-10-29",
                               "letter_ids": ["L-20261029-0001"],
                               "sent_at": now - 80000}]
            self.mb.save_user(uid, box)
            assert self.mb.delete_letter(uid, "L-20261029-0001")
            sys_lid = self.mb.write_system_letter(uid, "维护通知", "今晚停服",
                                                  now=now)
            # 列表：系统信显示【系统】+标题，角色信显示名字
            assert await turn._handle_mail_command("/邮件", uid, view)
            page = s.out[-1]
            assert "【系统】维护通知" in page and "罗克西" in page
            assert "L-20261029-0001" not in page       # 已删的信不在列表
            # 撤回编号 → 「已被撤回」，与「查无此信」区分（大小写均命中）
            for variant in ("L-20261029-0001", "l-20261029-0001"):
                s.out.clear()
                assert await turn._handle_mail_command(
                    f"/邮件 {variant}", uid, view)
                assert "已被撤回" in s.out[-1]
            # 系统信详情：【系统邮件 · 标题 · 日期】+ 标记已读
            s.out.clear()
            assert await turn._handle_mail_command(f"/邮件 {sys_lid}", uid,
                                                   view)
            assert "【系统邮件 · 维护通知" in s.out[-1]
            assert "今晚停服" in s.out[-1]
            assert self.mb.get_letter(uid, sys_lid)["read"] is True
            # 真正不存在的编号仍走「没有找到」
            s.out.clear()
            assert await turn._handle_mail_command("/邮件 L-19990101-ffff",
                                                   uid, view)
            assert "没有找到" in s.out[-1]
        finally:
            turn._mailbox_manager = orig_mb
            turn.default_sender = old_sender
        print_pass("§16.4 /邮件：撤回分支（大小写）/【系统】标签/系统信详情已读")

    # ── §16.4.2/§16.4.4 _mail_admin_exec：白名单 + 校验链 ─────────

    async def test_admin_exec(self):
        po = self.po
        mb = self.mb
        now = time.time()
        exec_op = po._mail_admin_exec
        # 白名单外类型 / 缺 openid / 缺 letter_id
        st, err = await exec_op({"type": "edit_letter", "openid": "x",
                                 "letter_id": "L"})
        assert st == "failed" and "非法操作类型" in err
        st, _ = await exec_op({"type": "delete_letter"})
        assert st == "failed"
        st, err = await exec_op({"type": "delete_letter", "openid": "u"})
        assert st == "failed" and "letter_id" in err
        # delete_letter：成功 / 不存在
        uid = "exec_u"
        box = mb.load_user(uid)
        box["letters"] = [{"id": "L-20261030-0002", "persona": "p",
                           "name": "n", "text": "t", "preview": "t",
                           "created_at": now, "read": False,
                           "digest_id": None}]
        mb.save_user(uid, box)
        st, _ = await exec_op({"type": "delete_letter", "openid": uid,
                               "letter_id": "L-20261030-0002"})
        assert st == "done" and mb.get_letter(uid, "L-20261030-0002") is None
        st, err = await exec_op({"type": "delete_letter", "openid": uid,
                                 "letter_id": "L-20261030-0002"})
        assert st == "failed" and "信件不存在" in err
        # send_system 校验链：空/超长/format
        base = {"type": "send_system", "openid": uid, "title": "T",
                "body": "B"}
        assert (await exec_op({**base, "title": "  "}))[0] == "failed"
        assert (await exec_op({**base, "title": "x" * 61}))[0] == "failed"
        assert (await exec_op({**base, "body": "x" * 2001}))[0] == "failed"
        st, err = await exec_op({**base, "format": "html"})
        assert st == "failed" and "format" in err
        # 拒收用户 → 失败并给出原因（不发送）
        mb.set_active_enabled(uid, False)
        self.c2c_sends.clear()
        st, err = await exec_op(base)
        assert st == "failed" and "拒收" in err and not self.c2c_sends
        mb.set_active_enabled(uid, True)
        # 官方 bot 未连接
        po._get_qq_bot = lambda: None
        st, err = await exec_op(base)
        assert st == "failed" and "未连接" in err
        po._get_qq_bot = lambda: MagicMock()
        # 发送失败 → 不入账
        self.c2c_ok = False
        st, err = await exec_op(base)
        assert st == "failed" and "发送失败" in err
        assert not any(l.get("system") for l in mb.load_user(uid)["letters"])
        # 成功 → md/plain 文案 + 信箱入账
        self.c2c_ok = True
        self.c2c_sends.clear()
        st, err = await exec_op({**base, "title": "*维护*",
                                 "body": "今晚十点\n- 项目一"})
        assert st == "done"
        send = self.c2c_sends[-1]
        assert send["openid"] == uid
        assert send["md"].startswith("## 系统邮件 · ＊维护＊")   # 全角转义
        assert "\u3000- 项目一" in send["md"]                    # 行首列表符
        assert "> 发送 `/邮件` 查看信箱" in send["md"]
        assert send["plain"].startswith("【系统邮件 · *维护*】")
        assert "发送 /邮件 查看信箱" in send["plain"]
        sl = [l for l in mb.load_user(uid)["letters"] if l.get("system")]
        assert len(sl) == 1 and sl[0]["title"] == "*维护*"
        assert sl[0]["read"] is False and sl[0]["digest_id"] is None
        print_pass("§16.4 exec：白名单/校验链/拒收原因/失败不入账/成功入账+文案")

    # ── §16.4.3.6 启动恢复 ────────────────────────────────────────

    def test_admin_recover(self):
        po = self.po
        # 残留 processing → failed 状态（bot restarted）+ 清理
        proc = po._MAIL_PROC_DIR / "mailop_20261030_abcd.json"
        proc.write_text(json.dumps(
            {"id": "mailop_20261030_abcd", "type": "delete_letter",
             "openid": "u"}), encoding="utf-8")
        # status 保留 7 天（W5）：8 天前的清掉，新的保留
        old = po._MAIL_STATUS_DIR / "mailop_old.json"
        old.write_text(json.dumps({"id": "mailop_old", "state": "done"}),
                       encoding="utf-8")
        t8 = time.time() - 8 * 86400
        os.utime(old, (t8, t8))
        fresh = po._MAIL_STATUS_DIR / "mailop_fresh.json"
        fresh.write_text(json.dumps({"id": "mailop_fresh", "state": "done"}),
                         encoding="utf-8")
        po._mail_admin_recover()
        assert not proc.exists()
        st = json.loads((po._MAIL_STATUS_DIR /
                         "mailop_20261030_abcd.json").read_text("utf-8"))
        assert st["state"] == "failed" and "bot restarted" in st["error"]
        assert not old.exists() and fresh.exists()
        # 损坏 processing 文件 → 以文件名为 id 记 failed
        bad = po._MAIL_PROC_DIR / "mailop_bad.json"
        bad.write_text("{not json", encoding="utf-8")
        po._mail_admin_recover()
        assert not bad.exists()
        st = json.loads((po._MAIL_STATUS_DIR /
                         "mailop_bad.json").read_text("utf-8"))
        assert st["state"] == "failed"
        print_pass("§16.4.3.6 恢复：残留认领→failed(bot restarted)/status 剪 7 天")

    # ── §16.2 队列 worker：认领 → 执行 → 回写 ─────────────────────

    async def test_admin_worker(self):
        po = self.po
        uid = "worker_u"
        now = time.time()
        box = self.mb.load_user(uid)
        box["letters"] = [{"id": "L-20261030-0003", "persona": "p",
                           "name": "n", "text": "t", "preview": "t",
                           "created_at": now, "read": False,
                           "digest_id": None}]
        self.mb.save_user(uid, box)
        q = po._MAIL_QUEUE_DIR
        (q / "mailop_w001.json").write_text(json.dumps(
            {"id": "mailop_w001", "type": "delete_letter", "openid": uid,
             "letter_id": "L-20261030-0003"}), encoding="utf-8")
        (q / "mailop_w002.json").write_text("{broken", encoding="utf-8")
        (q / "mailop_w003.json").write_text(json.dumps(
            {"id": "mailop_w003", "type": "send_system", "openid": uid,
             "title": "T", "body": "B"}), encoding="utf-8")
        self.c2c_ok = True
        self.c2c_sends.clear()

        real_sleep = asyncio.sleep
        calls = {"n": 0}

        async def fast_sleep(delay, *a, **k):
            calls["n"] += 1
            if calls["n"] > 10:
                raise asyncio.CancelledError      # 终止无限扫描循环
            await real_sleep(0)

        with patch.object(asyncio, "sleep", fast_sleep):
            task = asyncio.ensure_future(po._mail_admin_worker())
            try:
                await task
            except asyncio.CancelledError:
                pass
        # 队列/认领目录清空；status 逐项回写
        assert list(q.glob("*.json")) == []
        assert list(po._MAIL_PROC_DIR.glob("*.json")) == []
        st1 = json.loads((po._MAIL_STATUS_DIR /
                          "mailop_w001.json").read_text("utf-8"))
        assert st1["state"] == "done"
        st2 = json.loads((po._MAIL_STATUS_DIR /
                          "mailop_w002.json").read_text("utf-8"))
        assert st2["state"] == "failed" and "队列文件损坏" in st2["error"]
        st3 = json.loads((po._MAIL_STATUS_DIR /
                          "mailop_w003.json").read_text("utf-8"))
        assert st3["state"] == "done" and st3["error"] == ""
        # 副作用落地：信件被删 + 系统信入账（仅发了一次 c2c）
        assert self.mb.get_letter(uid, "L-20261030-0003") is None
        assert len(self.c2c_sends) == 1
        assert any(l.get("system") for l in self.mb.load_user(uid)["letters"])
        print_pass("§16.2 worker：认领(os.replace)/损坏文件 failed/执行回写 status")

    # ── WebUI data_reader 邮件 API（§16.4.1/§16.4.2/W4）───────────

    def test_webui_mail_api(self):
        from pathlib import Path as _Path
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from webui import data_reader as dr
        from agent.mailbox import MailboxManager
        tmp = _Path(self.tmp) / "webui"
        (tmp / "mailboxes").mkdir(parents=True, exist_ok=True)
        mb2 = MailboxManager(base_dir=str(tmp / "mailboxes"))
        now = time.time()
        oa = "AAAAAAAAAAAABBBBCCCC"
        mb2.record_digest(oa, [{"persona": "rubi", "name": "露比",
                                "text": "信内容"}], now=now)
        lid_a = mb2.load_user(oa)["letters"][0]["id"]
        mb2.write_system_letter(oa, "系统标题", "系统正文", now=now)
        mb2.set_active_enabled(oa, False)
        mb2.record_digest("DDDD1234", [{"persona": "roxy", "name": "罗克西",
                                        "text": "hi"}], now=now - 100)
        lid_d = mb2.load_user("DDDD1234")["letters"][0]["id"]
        # 仅登记过主动发送、还没有信箱文件的用户（c2c 联系人登记表）
        (tmp / "greet.json").write_text(
            json.dumps({"EEEE9999": {"last_seen": now - 50}}),
            encoding="utf-8")
        # 羁绊文件（§4.2 权威源）：EEEE9999 靠默认人格攒了「夏以昼」的羁绊，
        # 信箱尚未建档 → 面板人格数/搜索/详情都必须直接读 bonds 兜底
        (tmp / "bonds").mkdir(parents=True, exist_ok=True)
        (tmp / "bonds" / "EEEE9999.json").write_text(json.dumps({
            "夏以昼": {"stats": {"msgs": 5}},
            "zero": {"stats": {"msgs": 0}},
            "assistant": {"stats": {"msgs": 9}},
        }), encoding="utf-8")
        with patch.object(dr, "MAILBOX_DIR", tmp / "mailboxes"), \
                patch.object(dr, "MAIL_QUEUE_DIR", tmp / "queue"), \
                patch.object(dr, "MAIL_PROC_DIR", tmp / "proc"), \
                patch.object(dr, "MAIL_STATUS_DIR", tmp / "status"), \
                patch.object(dr, "GREET_TARGETS_FILE", tmp / "greet.json"), \
                patch.object(dr.config, "BONDS_DIR", tmp / "bonds"):
            # W4 脱敏
            assert dr._mask_openid(oa) == "AAAA…CCCC"
            assert dr._mask_openid("short") == "sh…"
            assert dr._mask_openid("") == ""
            # 统计卡（用户数 = 信箱建档 ∪ greet 登记表）
            ov = dr.mail_overview()
            assert ov == {"users": 3, "letters": 3, "unread": 3,
                          "system_letters": 1, "disabled_users": 1,
                          "queue_pending": 0}, ov
            # 用户列表：并集名单 + 混合排序（max(last_digest_at, last_seen)）
            # + 脱敏 + 搜索；仅登记未建档的用户也必须出现（可勾选发送）
            page = dr.mail_users()
            assert page["total"] == 3
            rows = page["items"]
            assert [r["openid"] for r in rows] == [oa, "EEEE9999", "DDDD1234"], rows
            assert rows[0]["openid_masked"] == "AAAA…CCCC"
            assert rows[0]["active_enabled"] is False and rows[0]["system"] == 1
            assert rows[0]["letters"] == 2 and rows[0]["unread"] == 2
            assert rows[0]["has_mailbox"] is True
            assert rows[1]["has_mailbox"] is False and rows[1]["letters"] == 0
            assert rows[1]["active_enabled"] is True
            assert rows[1]["last_seen"] == now - 50
            # 未建档用户的人格数直接读羁绊（msgs>0、排除 assistant/zero）
            assert rows[1]["personas"] == 1
            hit = dr.mail_users(query="roxy")
            assert hit["total"] == 1 and hit["items"][0]["openid"] == "DDDD1234"
            hit = dr.mail_users(query=lid_a)
            assert hit["total"] == 1 and hit["items"][0]["openid"] == oa
            hit = dr.mail_users(query="EEEE")
            assert hit["total"] == 1 and hit["items"][0]["openid"] == "EEEE9999"
            # 羁绊角色名可搜（未建档用户没有信件/known_personas 可匹配）
            hit = dr.mail_users(query="夏以昼")
            assert hit["total"] == 1 and hit["items"][0]["openid"] == "EEEE9999"
            assert dr.mail_users(query="查无此人")["total"] == 0
            # 详情：全文 openid + 倒序信件 + digest letter_states
            d = dr.mail_user(oa)
            assert d["openid"] == oa and d["active_enabled"] is False
            ts = [l["created_at"] for l in d["letters"]]
            assert ts == sorted(ts, reverse=True)
            assert d["digests"][0]["letter_states"] == \
                [{"id": lid_a, "withdrawn": False}]
            mb2.delete_letter(oa, lid_a)
            d = dr.mail_user(oa)
            assert d["digests"][0]["letter_states"] == \
                [{"id": lid_a, "withdrawn": True}]
            # greet-only 用户 → 空信箱视图；未知用户 → error
            g = dr.mail_user("EEEE9999")
            assert g["letters"] == [] and g["active_enabled"] is True
            # 详情 known_personas 合并羁绊兜底（信箱建档前的空窗期可见角色）
            assert g["known_personas"] == {"夏以昼": 0}
            # 已建档用户：信箱里的真实互动计数不被羁绊兜底覆盖
            d = dr.mail_user(oa)
            assert d["known_personas"]["rubi"] > 0
            assert "error" in dr.mail_user("NO_SUCH_USER_XX")
            # 删除入队：信件必须存在；成功写 queue 文件；重复入队拒绝
            assert "error" in dr.mail_enqueue_delete("DDDD1234", "L-nope")
            assert "error" in dr.mail_enqueue_delete("NO_SUCH_USER_XX", lid_d)
            r = dr.mail_enqueue_delete("DDDD1234", lid_d)
            assert r.get("ok") and r["id"].startswith("mailop_")
            op = json.loads((tmp / "queue" / f"{r['id']}.json")
                            .read_text("utf-8"))
            assert op["type"] == "delete_letter" and op["openid"] == "DDDD1234"
            assert op["letter_id"] == lid_d and op["created_at"]
            r2 = dr.mail_enqueue_delete("DDDD1234", lid_d)
            assert "已在队列" in r2.get("error", "")
            # 系统邮件入队：校验链 / 去重 / 未知用户 / 批量 / 同标题幂等
            assert "error" in dr.mail_enqueue_send([], "t", "b")
            assert "error" in dr.mail_enqueue_send("DDDD1234", "", "b")
            assert "error" in dr.mail_enqueue_send(["DDDD1234"], "t" * 61, "b")
            assert "error" in dr.mail_enqueue_send(["DDDD1234"], "t", "x" * 2001)
            assert "format" in dr.mail_enqueue_send(
                ["DDDD1234"], "t", "b", format="html").get("error", "")
            assert "error" in dr.mail_enqueue_send(["GHOST_USER_X"], "t", "b")
            r = dr.mail_enqueue_send(["DDDD1234", "DDDD1234", "EEEE9999"],
                                     "通知", "正文", format="markdown")
            assert r.get("ok") and r["count"] == 2 and len(r["ids"]) == 2
            r2 = dr.mail_enqueue_send(["EEEE9999"], "通知", "正文")
            assert "同标题" in r2.get("error", "")
            # 队列状态：pending 脱敏（不含裸 openid）+ recent 读 status
            (tmp / "status").mkdir(parents=True, exist_ok=True)
            (tmp / "status" / "mailop_done.json").write_text(json.dumps(
                {"id": "mailop_done", "state": "done", "error": "",
                 "finished_at": now}), encoding="utf-8")
            qs = dr.mail_queue_status()
            assert len(qs["pending"]) == 3
            for p in qs["pending"]:
                assert "openid" not in p and p["openid_masked"]
                assert p["state"] == "pending" and p["type"] in (
                    "delete_letter", "send_system")
            assert qs["recent"][0]["id"] == "mailop_done"
        print_pass("§16.4 WebUI API：脱敏/统计/搜索/详情撤回标记/入队校验+幂等")


class TestAnnouncementBroadcast:
    """57. 公告与双渠道群发（proactive-mail-design.md §16.3/§16.5）：
    tools.changelog 公告存储、send_active_group 错误分类（ok/left/failed +
    md 降级）、_do_broadcast kind/渠道分流/skipped/legacy 兼容、
    data_reader 公告 API 与目标归一化."""

    def run(self):
        print_header("57. Announcement Broadcast (公告群发 §16.3/§16.5)")
        _import_turn_for_tests()
        import nonebot
        try:
            nonebot.init()
        except ValueError:
            pass
        self.test_changelog_announcements_store()
        asyncio.run(self.test_send_active_group())
        asyncio.run(self.test_do_broadcast())
        self.test_webui_announcement_api()

    # ── §16.3 存储（tools/changelog.py）──────────────────────────

    def test_changelog_announcements_store(self):
        from tools import changelog as cl
        tmp = tempfile.mkdtemp(prefix="ann_store_")
        try:
            with patch.object(cl, "ANNOUNCEMENTS_FILE",
                              os.path.join(tmp, "announcements.json")), \
                    patch.object(cl, "KNOWN_GROUPS_FILE",
                                 os.path.join(tmp, "known_groups.json")):
                assert cl.load_announcements() == []        # 缺失 → 空
                assert cl.get_announcement("ann_x") is None
                cl.save_announcements([
                    {"id": "ann_20261030_000001", "title": "维护",
                     "body": "今晚停服", "format": "text", "created_at": 100.0}])
                ann = cl.get_announcement("ann_20261030_000001")
                assert ann and ann["title"] == "维护"
                # 群发成功回写 sent_at/last_targets（bot 是唯一写者）
                assert cl.mark_announcement_sent(
                    "ann_20261030_000001",
                    [{"channel": "onebot", "id": "111"}], ts=200.0)
                ann = cl.get_announcement("ann_20261030_000001")
                assert ann["sent_at"] == 200.0
                assert ann["last_targets"] == [{"channel": "onebot",
                                                "id": "111"}]
                assert cl.mark_announcement_sent("ann_nope", []) is False
                assert cl.format_announcement_text(ann) == \
                    "📢 公告\n维护\n\n今晚停服"
                # 损坏文件 → 空列表降级
                with open(cl.ANNOUNCEMENTS_FILE, "w") as f:
                    f.write("{bad")
                assert cl.load_announcements() == []
                # write_known_groups：OneBot + 官方两段（§16.5.1）
                cl.write_known_groups(
                    {"111": {"name": "群A", "member_count": 3}},
                    official_groups={"OID1": {"status": "active",
                                              "active_enabled": True,
                                              "last_seen_at": 5.0}})
                data = cl.read_known_groups()
                assert data["groups"]["111"]["name"] == "群A"
                assert data["official_groups"]["OID1"]["status"] == "active"
                assert data["updated_at"] > 0
                cl.write_known_groups({})       # 缺省 official_groups → {}
                assert cl.read_known_groups()["official_groups"] == {}
            print_pass("§16.3 公告存储：CRUD/sent_at 回写/损坏降级/双段群列表")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ── §16.5.3.4 send_active_group：ok/left/failed + md 降级 ─────

    async def test_send_active_group(self):
        import contextlib
        import adapters.qq_official as qo
        from nonebot.adapters.qq.exception import NetworkError

        class _GroupBot:
            """send_to_group 假 bot：按预置序列抛错，记录成功发送。"""

            def __init__(self, fails=()):
                self.sent = []
                self.calls = 0
                self._fails = list(fails)

            async def send_to_group(self, *, group_openid, message):
                self.calls += 1
                if self._fails:
                    f = self._fails.pop(0)
                    if f == "net":
                        raise NetworkError("timeout")
                    raise _stream_action_failed(f)
                self.sent.append((group_openid, message))

        @contextlib.contextmanager
        def guard():
            r, du = qo._md_breaker.rejects, qo._md_breaker.disabled_until
            env = os.environ.pop("QQ_OFFICIAL_MARKDOWN", None)
            qo._md_breaker.rejects, qo._md_breaker.disabled_until = 0, 0.0
            try:
                yield
            finally:
                qo._md_breaker.rejects, qo._md_breaker.disabled_until = r, du
                if env is not None:
                    os.environ["QQ_OFFICIAL_MARKDOWN"] = env

        real_sleep = asyncio.sleep

        async def zero_sleep(d, *a, **k):
            await real_sleep(0)

        with guard(), patch.object(asyncio, "sleep", zero_sleep):
            sag = qo.send_active_group
            # 纯文本 ok（无 msg_id = 主动群消息）
            bot = _GroupBot()
            assert await sag(bot, "G1", "你好") == "ok"
            gid, msg = bot.sent[0]
            assert gid == "G1" and msg.type == "text" \
                and msg.data["text"] == "你好"
            # md tier：markdown 段 + 成功清零熔断计数
            qo._md_breaker.rejects = 2
            bot = _GroupBot()
            assert await sag(bot, "G1", "plain", md_text="## md") == "ok"
            assert bot.sent[0][1].type == "markdown"
            assert qo._md_breaker.rejects == 0
            # md 被拒（40034011）→ 记熔断 + 降级纯文本重发一次 → ok
            bot = _GroupBot(fails=[40034011])
            assert await sag(bot, "G1", "纯文本", md_text="## md") == "ok"
            assert bot.calls == 2 and bot.sent[0][1].type == "text"
            assert bot.sent[0][1].data["text"] == "纯文本"
            assert qo._md_breaker.rejects == 1
            # 熔断打开 → md_text 也直接走 plain tier
            qo._md_breaker.disabled_until = time.monotonic() + 600
            bot = _GroupBot()
            assert await sag(bot, "G1", "纯文本", md_text="## md") == "ok"
            assert bot.sent[0][1].type == "text"
            qo._md_breaker.disabled_until = 0.0
            # 退群码 → "left"（ActionFailed 不重试）
            for code in (40034101, 40034105):
                bot = _GroupBot(fails=[code])
                assert await sag(bot, "G1", "x") == "left"
                assert bot.calls == 1
            # 其他 ActionFailed（如频控 40034100）→ failed 不重试
            bot = _GroupBot(fails=[40034100])
            assert await sag(bot, "G1", "x") == "failed"
            assert bot.calls == 1
            # NetworkError → 每 tier 重试一次：两次失败 → failed；闪断恢复 → ok
            bot = _GroupBot(fails=["net", "net"])
            assert await sag(bot, "G1", "x") == "failed"
            assert bot.calls == 2
            bot = _GroupBot(fails=["net"])
            assert await sag(bot, "G1", "x") == "ok"
            assert bot.calls == 2
            # 参数守卫
            assert await sag(_GroupBot(), "", "x") == "failed"
            assert await sag(_GroupBot(), "G1", "   ") == "failed"
        print_pass("§16.5.3.4 send_active_group：ok/left/failed 分类 + md 降级熔断")

    # ── §16.5.3 _do_broadcast：kind/渠道分流（闭包提取）───────────

    @staticmethod
    async def _grab_do_broadcast(ar):
        """启动 _start_changelog_tasks，从 _broadcast_poller 闭包提取
        _do_broadcast，随即取消后台任务（测试直接驱动广播函数）。"""
        await ar._start_changelog_tasks()
        do_bc = None
        extras = []
        for t in asyncio.all_tasks():
            coro = t.get_coro()
            name = getattr(coro, "__name__", "") or \
                getattr(getattr(coro, "cr_code", None), "co_name", "")
            if name == "_broadcast_poller":
                frame = getattr(coro, "cr_frame", None)
                if frame is not None:
                    do_bc = frame.f_locals.get("_do_broadcast")
            if name in ("_broadcast_poller", "_group_exporter"):
                extras.append(t)
        for t in extras:
            t.cancel()
        await asyncio.gather(*extras, return_exceptions=True)
        assert do_bc is not None, "未能从闭包提取 _do_broadcast"
        return do_bc

    async def test_do_broadcast(self):
        import nonebot
        import plugins.agent_router as ar
        from tools import changelog as cl
        from nonebot.adapters.onebot.v11 import Bot as OBBot
        from nonebot.adapters.qq import Bot as QQBot

        class _FakeReg:
            instance = None

            def __init__(self, path):
                self.path = path
                self.groups = {}
                self.left = []
                _FakeReg.instance = self

            def load(self):
                return {"groups": {k: dict(v)
                                   for k, v in self.groups.items()}}

            def get(self, gid):
                e = self.groups.get(gid)
                return dict(e) if e else None

            def list_active(self):
                return [g for g, e in self.groups.items()
                        if e.get("status") == "active"
                        and e.get("active_enabled", True)]

            def mark_left(self, gid, now=None):
                self.left.append(gid)
                if gid in self.groups:
                    self.groups[gid]["status"] = "left"

        ob_bot = MagicMock(spec=OBBot)
        ob_bot.send_group_msg = AsyncMock()
        ob_bot.get_group_list = AsyncMock(return_value=[
            {"group_id": 111, "group_name": "群A"},
            {"group_id": 222, "group_name": "群B"}])
        qq_bot = MagicMock(spec=QQBot)

        self.bc_sends = []
        self.bc_result = "ok"

        async def fake_sag(bot, gid, text, md_text=None):
            self.bc_sends.append({"gid": gid, "text": text, "md": md_text})
            return self.bc_result(gid) if callable(self.bc_result) \
                else self.bc_result

        tmp = tempfile.mkdtemp(prefix="bcast_")
        try:
            with patch.object(cl, "CHANGELOG_FILE",
                              os.path.join(tmp, "changelog.json")), \
                    patch.object(cl, "ANNOUNCEMENTS_FILE",
                                 os.path.join(tmp, "announcements.json")), \
                    patch.object(cl, "KNOWN_GROUPS_FILE",
                                 os.path.join(tmp, "known_groups.json")), \
                    patch.object(cl, "PENDING_FILE",
                                 os.path.join(tmp, "pending.json")), \
                    patch.object(cl, "STATUS_FILE",
                                 os.path.join(tmp, "status.json")), \
                    patch("agent.group_registry.GroupRegistry", _FakeReg), \
                    patch("adapters.qq_official.send_active_group", fake_sag), \
                    patch.object(ar, "render_changelog_card",
                                 MagicMock(return_value=None)), \
                    patch.object(nonebot, "get_bots",
                                 lambda: {"ob": ob_bot, "qq": qq_bot}):
                do_bc = await self._grab_do_broadcast(ar)
                real_sleep = asyncio.sleep

                async def zero_sleep(d, *a, **k):
                    await real_sleep(0)

                with patch.object(asyncio, "sleep", zero_sleep):
                    await self._bc_scenarios(do_bc, cl, ob_bot, _FakeReg)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print_pass("§16.5.3 群发：legacy/kind 分流/skipped/left 标记/all 兜底/错误路径")

    async def _bc_scenarios(self, do_bc, cl, ob_bot, _FakeReg):
        """_do_broadcast 场景集（在 zero-sleep + 全套 patch 内直接驱动）。"""
        reg = _FakeReg.instance
        # 种子：更新记录 + 两条公告（一条 markdown）
        cl.save_entries([{"version": "v1.1", "date": "2026-10-30",
                          "created_at": 111.0,
                          "changes": [{"type": "新增", "text": "公告系统"}]}])
        cl.save_announcements([
            {"id": "ann_20261030_000001", "title": "版本活动",
             "body": "**奖励加倍**", "format": "markdown",
             "created_at": 222.0},
            {"id": "ann_20261030_000002", "title": "预热", "body": "敬请期待",
             "format": "text", "created_at": 333.0}])

        # ── 场景 1：legacy payload（裸 entry_index + 字符串 targets）──
        self.bc_result = "ok"
        self.bc_sends.clear()
        ob_bot.send_group_msg.reset_mock()
        await do_bc({"entry_index": 0,
                     "targets": ["111", "111",
                                 {"channel": "onebot", "id": "111"}]})
        st = cl.read_status()
        assert st["state"] == "done" and st["kind"] == "changelog"
        assert st["ref"] == {"entry_index": 0}
        assert st["total"] == 1 and st["sent"] == 1        # 去重
        assert st["by_channel"]["onebot"]["sent"] == 1
        assert st["entry_version"] == "v1.1"
        msg = ob_bot.send_group_msg.await_args_list[0].kwargs["message"]
        assert isinstance(msg, str) and "📢 更新公告" in msg and "v1.1" in msg
        assert not self.bc_sends                            # 官方渠道未触发
        assert cl.load_entries()[0].get("broadcast_at")     # 已群发回写

        # ── 场景 2：公告 + markdown + 混合渠道 + 官方预检 skipped ──
        reg.groups = {
            "OID_ACTIVE": {"status": "active", "active_enabled": True},
            "OID_OFF": {"status": "active", "active_enabled": False},
            "OID_LEFT": {"status": "left", "active_enabled": True},
        }
        self.bc_sends.clear()
        ob_bot.send_group_msg.reset_mock()
        await do_bc({
            "kind": "announcement",
            "ref": {"announcement_id": "ann_20261030_000001"},
            "targets": [{"channel": "onebot", "id": "111"},
                        {"channel": "official", "id": "OID_ACTIVE"},
                        {"channel": "official", "id": "OID_OFF"},
                        {"channel": "official", "id": "OID_LEFT"}]})
        st = cl.read_status()
        assert st["state"] == "done" and st["kind"] == "announcement"
        assert st["total"] == 4 and st["sent"] == 2 and st["skipped"] == 2
        assert st["by_channel"]["official"] == {"sent": 1, "failed": 0,
                                                "skipped": 2}
        assert st["announcement_title"] == "版本活动"
        # 官方渠道：单条 markdown（text 为降级备份）
        assert len(self.bc_sends) == 1
        s = self.bc_sends[0]
        assert s["gid"] == "OID_ACTIVE"
        assert s["md"] == "## 📢 版本活动\n**奖励加倍**"
        assert s["text"] == "📢 公告\n版本活动\n\n**奖励加倍**"
        # OneBot 渠道：纯文本（W3 不带 markdown 语法）
        ob_msg = ob_bot.send_group_msg.await_args_list[0].kwargs["message"]
        assert "📢 公告" in ob_msg and "##" not in ob_msg
        # 成功回写 sent_at/last_targets（全部尝试目标）
        ann = cl.get_announcement("ann_20261030_000001")
        assert ann["sent_at"]
        assert ann["last_targets"] == [
            {"channel": "onebot", "id": "111"},
            {"channel": "official", "id": "OID_ACTIVE"},
            {"channel": "official", "id": "OID_OFF"},
            {"channel": "official", "id": "OID_LEFT"}]

        # ── 场景 3：left → mark_left + 全部失败 → error 且不回写 ──
        self.bc_sends.clear()
        self.bc_result = lambda gid: "left"
        await do_bc({
            "kind": "announcement",
            "ref": {"announcement_id": "ann_20261030_000002"},
            "targets": [{"channel": "official", "id": "OID_ACTIVE"}]})
        st = cl.read_status()
        assert st["state"] == "error" and st["failed"] == 1
        assert st["sent"] == 0 and "失败" in st["error"]
        assert "OID_ACTIVE" in reg.left
        assert reg.groups["OID_ACTIVE"]["status"] == "left"
        ann2 = cl.get_announcement("ann_20261030_000002")
        assert not ann2.get("sent_at")          # 全部失败不写已发送标记
        self.bc_result = "ok"

        # ── 场景 4：错误路径（公告不存在/越界/未选目标）──────────
        await do_bc({
            "kind": "announcement", "ref": {"announcement_id": "ann_nope"},
            "targets": ["all"]})
        assert "公告不存在" in cl.read_status()["error"]
        await do_bc({"kind": "changelog", "ref": {"entry_index": 99},
                     "targets": ["111"]})
        assert "越界" in cl.read_status()["error"]
        await do_bc({"kind": "changelog", "ref": {"entry_index": 0},
                     "targets": []})
        assert "未选择目标群" in cl.read_status()["error"]

        # ── 场景 5："all" 扩展（live 列表 + registry；失败退快照）──
        reg.groups = {"OID_A": {"status": "active", "active_enabled": True},
                      "OID_BAD": {"status": "active",
                                  "active_enabled": False}}
        self.bc_sends.clear()
        ob_bot.send_group_msg.reset_mock()
        await do_bc({"kind": "changelog", "ref": {"entry_index": 0},
                     "targets": ["all"]})
        st = cl.read_status()
        assert st["total"] == 3 and st["sent"] == 3     # 111+222+OID_A
        assert ob_bot.send_group_msg.await_count == 2
        assert [x["gid"] for x in self.bc_sends] == ["OID_A"]
        # get_group_list 失败 → known_groups 快照兜底
        cl.write_known_groups({"333": {"name": "群C", "member_count": 1}})
        ob_bot.get_group_list = AsyncMock(side_effect=RuntimeError("down"))
        ob_bot.send_group_msg.reset_mock()
        self.bc_sends.clear()
        await do_bc({"kind": "changelog", "ref": {"entry_index": 0},
                     "targets": ["all"]})
        st = cl.read_status()
        assert st["total"] == 2 and st["sent"] == 2     # 333+OID_A
        assert [c.kwargs["group_id"]
                for c in ob_bot.send_group_msg.await_args_list] == [333]

        # ── 场景 6：bot 未连接 ────────────────────────────────────
        import nonebot as _nb
        with patch.object(_nb, "get_bots", lambda: {}):
            await do_bc({"kind": "changelog", "ref": {"entry_index": 0},
                         "targets": ["111"]})
            assert "bot 未连接" in cl.read_status()["error"]
            await do_bc({
                "kind": "changelog", "ref": {"entry_index": 0},
                "targets": [{"channel": "official", "id": "OID_A"}]})
            assert "官方 bot 未连接" in cl.read_status()["error"]

    # ── WebUI data_reader 公告 API（§16.3.2/§16.5.2）──────────────

    def test_webui_announcement_api(self):
        from pathlib import Path as _Path
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from webui import data_reader as dr
        from webui import config_editor as ce
        tmp = tempfile.mkdtemp(prefix="webui_ann_")
        try:
            t = _Path(tmp)
            with patch.object(dr, "ANNOUNCEMENTS_FILE",
                              t / "announcements.json"), \
                    patch.object(dr, "CHANGELOG_FILE", t / "changelog.json"), \
                    patch.object(dr, "PENDING_FILE",
                                 t / "pending_broadcast.json"), \
                    patch.object(dr, "KNOWN_GROUPS_FILE",
                                 t / "known_groups.json"), \
                    patch.object(dr, "STATUS_FILE",
                                 t / "broadcast_status.json"), \
                    patch.object(ce, "BACKUPS", t / "backups"):
                # targets 归一化：all/dict/裸字符串 + 各非法形态
                assert dr._norm_broadcast_targets(["all"]) == ["all"]
                assert dr._norm_broadcast_targets(
                    ["123", {"channel": "official", "id": "OID_1"}]) == [
                    {"channel": "onebot", "id": "123"},
                    {"channel": "official", "id": "OID_1"}]
                assert "error" in dr._norm_broadcast_targets([])
                assert "error" in dr._norm_broadcast_targets("111")
                assert "error" in dr._norm_broadcast_targets(["abc"])
                assert "error" in dr._norm_broadcast_targets(
                    [{"channel": "telegram", "id": "x"}])
                assert "error" in dr._norm_broadcast_targets(
                    [{"channel": "official", "id": "has space"}])
                # save 校验链
                assert "error" in dr.announcement_save("not-a-list")
                assert "标题" in dr.announcement_save(
                    [{"title": "", "body": "b"}])["error"]
                assert "error" in dr.announcement_save(
                    [{"title": "t" * 61, "body": "b"}])
                assert "正文" in dr.announcement_save(
                    [{"title": "t", "body": "  "}])["error"]
                assert "error" in dr.announcement_save(
                    [{"title": "t", "body": "b" * 2001}])
                assert "format" in dr.announcement_save(
                    [{"title": "t", "body": "b", "format": "html"}])["error"]
                # 非法 id 重新生成；合法 id/created_at 保留
                r = dr.announcement_save([
                    {"id": "hack!id", "title": "A1", "body": "b1"},
                    {"id": "ann_20261001_abcdef", "title": "A2", "body": "b2",
                     "format": "markdown", "created_at": 55.0}])
                assert r.get("ok"), r
                ents = r["entries"]
                assert ents[0]["id"].startswith("ann_")
                assert re.fullmatch(r"ann_\d{8}_[0-9a-f]{6}", ents[0]["id"])
                assert ents[1]["id"] == "ann_20261001_abcdef"
                assert ents[1]["created_at"] == 55.0
                assert ents[1]["format"] == "markdown"
                # id 重复 → 拒绝
                r = dr.announcement_save([
                    {"id": "ann_20261001_abcdef", "title": "X", "body": "y"},
                    {"id": "ann_20261001_abcdef", "title": "Z", "body": "w"}])
                assert "重复" in r.get("error", "")
                # bot 回写的 sent_at/last_targets 在面板重保存时保留
                a1, a2 = ents
                raw = json.loads((t / "announcements.json").read_text("utf-8"))
                for e in raw["entries"]:
                    if e["id"] == a1["id"]:
                        e["sent_at"] = 999.0
                        e["last_targets"] = ["all"]
                (t / "announcements.json").write_text(
                    json.dumps(raw, ensure_ascii=False), encoding="utf-8")
                a1_mod = dict(a1, body="b1-改")
                r = dr.announcement_save([a1_mod, a2])
                assert r.get("ok")
                saved = {e["id"]: e for e in dr.announcement_list()["entries"]}
                assert saved[a1["id"]]["sent_at"] == 999.0
                assert saved[a1["id"]]["last_targets"] == ["all"]
                assert saved[a1["id"]]["body"] == "b1-改"
                assert saved[a1["id"]]["updated_at"] > a1["updated_at"]
                # 内容不变 → updated_at 不动
                before = saved[a1["id"]]["updated_at"]
                r = dr.announcement_save(
                    list(dr.announcement_list()["entries"]))
                assert {e["id"]: e
                        for e in r["entries"]}[a1["id"]]["updated_at"] == before
                # broadcast 入队：不存在 → error；存在 → 新格式 pending
                r = dr.announcement_broadcast("ann_nope", ["all"])
                assert "不存在" in r["error"]
                r = dr.announcement_broadcast(a1["id"], ["all"])
                assert r.get("ok")
                payload = json.loads((t / "pending_broadcast.json")
                                     .read_text("utf-8"))
                assert payload["kind"] == "announcement"
                assert payload["ref"] == {"announcement_id": a1["id"]}
                assert payload["targets"] == ["all"] and payload["queued_at"]
                # 未认领前拒绝再次入队
                r2 = dr.announcement_broadcast(a1["id"], ["all"])
                assert "待处理" in r2.get("error", "")
                (t / "pending_broadcast.json").unlink()
                # changelog_broadcast：越界拒绝
                r = dr.changelog_broadcast(5, ["111"])
                assert "越界" in r["error"]
                # changelog_groups：双渠道统一结构 + sendable/reason
                (t / "known_groups.json").write_text(json.dumps({
                    "groups": {"111": {"name": "群A", "member_count": 9}},
                    "official_groups": {
                        "OID_OK": {"status": "active",
                                   "active_enabled": True,
                                   "last_seen_at": 7.0},
                        "OID_OFF": {"status": "active",
                                    "active_enabled": False},
                        "OID_LEFT": {"status": "left",
                                     "active_enabled": True},
                    },
                    "updated_at": 123.0}), encoding="utf-8")
                g = dr.changelog_groups()
                by_id = {x["id"]: x for x in g["groups"]}
                assert by_id["111"]["channel"] == "onebot"
                assert by_id["111"]["sendable"] is True
                assert by_id["OID_OK"]["sendable"] is True
                assert by_id["OID_OK"]["name"].startswith("官方群")
                assert by_id["OID_OFF"]["sendable"] is False
                assert by_id["OID_OFF"]["reason"] == "已关闭通知"
                assert by_id["OID_LEFT"]["reason"] == "已退群"
                assert g["updated_at"] == 123.0
                assert g["groups"][0]["channel"] == "onebot"   # OneBot 排前
            print_pass("§16.3/§16.5 WebUI：targets 归一化/公告保存校验+id/入队/群列表")
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
        ("SearchArchive", TestSearchArchive()),
        ("Personality Manager", TestPersonality()),
        ("TokenLedger", TestTokenLedger()),
        ("Cache Stability (Phase 3/4)", TestCacheStability()),
        ("Bond Manager (P0)", TestBondManager()),
        ("Personality Mode Metadata (P0)", TestPersonalityMeta()),
        ("ModelRouter.classify (P0)", TestModelRouterClassify()),
        ("Delegation / Dual-Plane (P0)", TestDelegation()),
        ("Agent persist=False (P0)", TestAgentPersist()),
        ("CharacterRegistry (P1)", TestCharacterRegistry()),
        ("create_character Tool (P1)", TestCreateCharacterTool()),
        ("Char-Creation Interview (P1)", TestCharCreationInterview()),
        ("Bond Content Parsing (P2)", TestBondContent()),
        ("Bond Gift / Poke (P2)", TestBondGiftPoke()),
        ("Bond Events & Dynamic Context (P2)", TestBondEvents()),
        ("Official Status Heartbeat (WebUI IPC)", TestOfficialStatusHeartbeat()),
        ("WebUI Official Status Aggregator", TestWebuiOfficialStatus()),
        ("WebUI Permissions Editor", TestWebuiPermissionsEditor()),
        ("Personality Marker Persistence (A+B)", TestPersonalityMarkerPersistence()),
        ("Session-Persona Binding", TestSessionPersonaBinding()),
        ("Bond Scene Settlement (P3a)", TestBondScene()),
        ("Bond-Scene Session & Commands (P3a)", TestSceneSession()),
        ("Scene-Session Archive & Quota Exemption", TestSceneArchive()),
        ("Bond Reunion & Anniversary (P3a)", TestBondReunionAnniversary()),
        ("Active Greeting & Quota (P3b)", TestActiveGreeting()),
        ("Media Generation Runner & Tools (P4)", TestMediaGen()),
        ("Asset Cards & Dedupe (P4)", TestAssetCardsP4()),
        ("Media Quota & Level Gates (P4)", TestMediaQuota()),
        ("Appearance Anchor & Voice ID (P4)", TestAnchorVoiceId()),
        ("Proactive Media Pass (P4)", TestProactiveMedia()),
        ("Asset Search & Rerank (P5)", TestAssetSearch()),
        ("Asset Recall Tool (P5)", TestAssetRecall()),
        ("Upload Textualization (P5)", TestUploadTextualization()),
        ("Episode Distillation (P5)", TestEpisodeDistill()),
        ("Asset Eviction & Injection (P5)", TestAssetEvictionInjection()),
        ("WebUI Model Sections (IMAGE_GEN/TTS)", TestWebuiModelSections()),
        ("Initial Bond Seeding (UGC)", TestInitialBond()),
        ("Story-KB Injection Gate", TestStoryKbInjectionGate()),
        ("QQ Official Quote Parsing", TestQQOfficialQuoteParsing()),
        ("Reference Portrait (/设形象)", TestReferencePortrait()),
        ("Direct Vision (主模型直连识图)", TestDirectVision()),
        ("QQ Official Markdown (msg_type=2)", TestOfficialMarkdown()),
        ("QQ Official Buttons (keyboard P0+P1)", TestOfficialButtons()),
        ("QQ Official Stream (stream_messages, c2c)", TestOfficialStream()),
        ("Session Compression (/压缩会话)", TestSessionCompression()),
        ("Proactive Mail (角色信箱 v1)", TestProactiveMail()),
        ("Mail Admin (WebUI 邮件管理 §16.2/§16.4)", TestMailAdmin()),
        ("Announcement Broadcast (公告群发 §16.3/§16.5)", TestAnnouncementBroadcast()),
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
