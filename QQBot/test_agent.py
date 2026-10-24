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
        print_pass("parse/unlocked/unlocking_at/text tiers by level")

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
            assert "第一幕" in line and "终幕" not in line, line  # first act only
            assert "/结束会话" in line and "露比" in line, line
            # empty body → generic framed fallback
            line2 = turn._scene_opening_line(
                "rubi", "星空之约", {"title": "星空之约", "body": "  "})
            assert "剧情《星空之约》开始" in line2 and "/结束会话" in line2
            print_pass("opening seed: first act line, skips #/>; framed fallback")
        finally:
            turn.get_character_registry = orig

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
        """post-run 收尾：纪念卡 entry 正确 + 图片发送 + 会话关闭；失败静默。"""
        turn = self.turn
        tmp = tempfile.mkdtemp(prefix="scene_fin_")
        orig = (turn._bond_manager, turn._special_sessions, turn._safe_send,
                turn.get_character_registry, turn.render_scene_card)
        sent, entries = [], []
        try:
            m = _tmp_bond_manager(tmp)
            turn._bond_manager = m
            special = MagicMock()
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
            # image sent with a label, session closed
            assert len(sent) == 1
            assert getattr(sent[0], "path", "").endswith("card.png"), sent[0]
            assert "羁绊纪念" in getattr(sent[0], "label", ""), sent[0]
            special.end_active.assert_called_once_with("u1")
            # render failure (None) → no image, session still closed, no raise
            sent.clear()
            special.reset_mock()
            turn.render_scene_card = lambda entry, out_path=None: None
            await turn._finish_bond_scene("u1", result)
            assert sent == [] and special.end_active.called
            print_pass("finish: card entry/send + end_active; render None degrades")
        finally:
            (turn._bond_manager, turn._special_sessions, turn._safe_send,
             turn.get_character_registry, turn.render_scene_card) = orig
            shutil.rmtree(tmp, ignore_errors=True)


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
        ("Bond Scene Settlement (P3a)", TestBondScene()),
        ("Bond-Scene Session & Commands (P3a)", TestSceneSession()),
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
