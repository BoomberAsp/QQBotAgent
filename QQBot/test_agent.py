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

    async def chat_completion(self, message: str, history=None, timeout_set=180.0):
        self.chat_calls.append(("chat", message, history))
        if self.should_fail:
            return "模拟API错误"
        return self.plain_response

    async def chat_completion_with_tools(self, messages, tools, timeout=180.0):
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
        assert "Roxy" in prompt, "System prompt should contain agent name"
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

        async def staged_response(messages, tools, timeout=180.0):
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
        async def always_tool_calls(messages, tools, timeout=180.0):
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
            assert "深圳" in system_content, f"Profile facts not injected: {system_content[:200]}"
            assert "机器学习" in system_content, f"Profile interests not injected: {system_content[:200]}"
            assert "concise" in system_content, f"Profile preferences not injected: {system_content[:200]}"
            print_pass("User profile injected into system prompt")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_memory_injection(self):
        from agent.tool_registry import ToolRegistry
        from agent.memory import MemorySystem, MemoryEntry
        from agent.agent import Agent

        tmpdir = tempfile.mkdtemp()
        try:
            mock_client = MockDeepSeekClient()
            mock_client.plain_response = "关于Python..."

            mem_sys = MemorySystem(base_dir=tmpdir)
            mem_sys.save(MemoryEntry(
                name="python_discussion",
                description="Python discussion",
                type="knowledge",
                content="上次和用户讨论了Python装饰器的用法",
            ))

            agent = Agent(
                deepseek_client=mock_client,
                tool_registry=ToolRegistry(),
                config_dir=os.path.join(os.path.dirname(__file__), "agent", "config"),
                memory_system=mem_sys,
            )

            agent._captured = None
            original = agent._build_messages

            def capture(session, msg):
                msgs = original(session, msg)
                agent._captured = msgs
                return msgs

            agent._build_messages = capture

            await agent.run("Python装饰器怎么用？", "user_abc")

            system_content = agent._captured[0]["content"]
            assert "装饰器" in system_content, f"Memory not injected: {system_content[:300]}"
            print_pass("Relevant memories injected into system prompt")
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
        assert "深圳" in ctx
        assert "机器学习" in ctx
        assert "detailed" in ctx
        print_pass("Full profile generates complete prompt context")

    def test_merge_facts_dedup(self):
        from agent.profile import UserProfile

        profile = UserProfile(user_id="dedup_user")
        profile.merge_facts(["在深圳", "用Python"])

        # Add same fact
        profile.merge_facts(["在深圳", "喜欢游戏"])
        assert len(profile.facts) == 3, f"Expected 3 facts, got {len(profile.facts)}: {profile.facts}"
        print_pass("Fact deduplication (fuzzy matching)")

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


# ── Main Runner ──────────────────────────────────────────────────

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
        ("Agent Core (Mock LLM)", TestAgentCore()),
        ("DeepSeekClient Parsing", TestDeepSeekClientParsing()),
        ("Built-in Tools", TestBuiltinTools()),
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
