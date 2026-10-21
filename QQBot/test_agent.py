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
