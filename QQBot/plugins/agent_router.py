"""
Agent Router — Unified message entry point for the QQBot agent.

This plugin catches ALL incoming QQ messages and routes them through
the Agent. The Agent decides whether to respond directly or invoke tools.

This replaces the old distributed on_command architecture with a single
intelligent entry point.
"""

import asyncio
import os
import re
import uuid

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, Message, ActionFailed
from nonebot.rule import to_me

from ..agent.agent import Agent
from ..agent.continuous_session import ContinuousSessionManager
from ..agent.tool_registry import ToolRegistry
from ..agent.session import SessionManager
from ..agent.memory import MemorySystem
from ..agent.profile import ProfileManager
from ..lib.deepseek_client import deepseek_client as _global_client, DeepSeekClient as _DeepSeekClient
from ..lib.model_router import ModelRouter

# Handle case where NoneBot is not running (testing)
deepseek_client = _global_client if _global_client is not None else _DeepSeekClient()
from ..tools.builtin_tools import (
    execute_code,
    get_time,
    search_web,
    download_repo,
    summarize_pdf,
    WORKSPACE_UPLOADS,
    _ensure_workspace_dirs,
)
from ..tools.file_tools import read_file
from ..tools.legacy_tools import (
    calculate_speed,
    compare_speed_probability,
    explain_code_tool,
    gacha_pull,
    translate_text,
)

# ── Configuration Paths ───────────────────────────────────────────

_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_DIR = os.path.join(_AGENT_DIR, "agent", "config")
_DATA_DIR = os.path.join(_AGENT_DIR, "data")

# ── Workspace Initialization ─────────────────────────────────────

def _init_workspace():
    """Create workspace directories if they don't exist."""
    from ..tools.builtin_tools import _ensure_workspace_dirs
    _ensure_workspace_dirs()

_init_workspace()


# ── File Download Helper ──────────────────────────────────────────

async def _download_and_save_file(url: str, filename: str, max_size_mb: int = 50) -> tuple:
    """Download a file from QQ and save to workspace uploads.

    Args:
        url: Download URL from the message segment.
        filename: Original filename (used for extension detection).
        max_size_mb: Maximum file size in MB.

    Returns:
        (saved_path, error_message) — one is None, the other is not.
    """
    if not url:
        return None, "文件 URL 为空，无法下载。"

    max_size_bytes = max_size_mb * 1024 * 1024

    # Ensure the uploads directory exists
    _ensure_workspace_dirs()

    # Generate safe filename: uuid8 prefix + sanitized original name
    ext = os.path.splitext(filename)[1] or ""
    safe_name = re.sub(r'[^\w\-_.]', '_', os.path.splitext(filename)[0])
    if not safe_name:
        safe_name = "file"
    unique_name = f"{uuid.uuid4().hex[:8]}-{safe_name}{ext}"
    save_path = os.path.join(WORKSPACE_UPLOADS, unique_name)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=120.0, follow_redirects=True)
            response.raise_for_status()

            content_length = len(response.content)
            if content_length > max_size_bytes:
                return None, (
                    f"文件过大 ({content_length / 1024 / 1024:.1f} MB)，"
                    f"超过限制 ({max_size_mb} MB)。请压缩后重试。"
                )

            # Determine actual extension from Content-Type if possible
            content_type = response.headers.get("content-type", "")
            ct_map = {
                "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                "image/webp": ".webp", "image/bmp": ".bmp",
                "application/pdf": ".pdf",
            }
            for ct_prefix, ct_ext in ct_map.items():
                if ct_prefix in content_type and not save_path.endswith(ct_ext):
                    save_path = save_path + ct_ext
                    break

            with open(save_path, "wb") as f:
                f.write(response.content)

            return save_path, None

    except httpx.HTTPStatusError as e:
        return None, f"下载失败 (HTTP {e.response.status_code}): {e.response.reason_phrase}"
    except httpx.TimeoutException:
        return None, "下载超时 (120秒)。文件可能过大或网络不稳定。"
    except httpx.RequestError as e:
        return None, f"网络请求失败: {e}"
    except IOError as e:
        return None, f"文件保存失败: {e}"
    except Exception as e:
        return None, f"下载文件时出现意外错误: {e}"


# ── Build Tool Registry ──────────────────────────────────────────

def _build_tool_registry() -> ToolRegistry:
    """Register all available tools."""
    registry = ToolRegistry()

    # Built-in tools
    registry.register(
        "get_time", get_time,
        "获取当前日期和时间",
        {"type": "object", "properties": {}, "required": []},
    )
    registry.register(
        "search_web", search_web,
        "搜索互联网获取信息（天气、新闻、百科等），通过SearXNG聚合多引擎结果",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词。支持中文和英文。查天气请包含'天气'+城市名"},
                "num_results": {"type": "integer", "description": "返回结果数量，默认5条", "default": 5},
            },
            "required": ["query"],
        },
    )
    registry.register(
        "execute_code", execute_code,
        "执行Python代码并返回输出结果",
        {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的Python代码"},
                "timeout": {"type": "integer", "description": "超时秒数，默认30", "default": 30},
            },
            "required": ["code"],
        },
    )
    registry.register(
        "download_repo", download_repo,
        "下载(Git Clone)一个代码仓库到服务器",
        {
            "type": "object",
            "properties": {"repo_url": {"type": "string", "description": "Git仓库URL"}},
            "required": ["repo_url"],
        },
    )
    registry.register(
        "summarize_pdf", summarize_pdf,
        "提取并总结PDF文件内容",
        {
            "type": "object",
            "properties": {"file_path": {"type": "string", "description": "服务器上的PDF文件路径"}},
            "required": ["file_path"],
        },
    )
    registry.register(
        "read_file", read_file,
        "读取用户上传的文件内容。支持文本文件（代码、日志、配置等）、PDF文件和图片。"
        "文本和PDF返回文字内容，图片返回基础信息+AI分析（如果多模态LLM已配置且可用）。",
        {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件路径。用户上传的文件会自动保存到 data/workspace/uploads/ 目录。",
                },
            },
            "required": ["file_path"],
        },
    )

    # Legacy tools (game features)
    registry.register(
        "gacha_pull", gacha_pull,
        "模拟游戏抽卡/招募。支持单抽和十连抽，四种卡池类型。",
        {
            "type": "object",
            "properties": {
                "pool_type": {
                    "type": "string",
                    "description": "卡池类型",
                    "enum": ["常规招募", "几率up招募", "神秘招募", "银河招募"],
                },
                "count": {"type": "integer", "description": "抽卡次数: 1=单抽, 10=十连", "enum": [1, 10], "default": 1},
                "up_character": {"type": "string", "description": "UP角色名(几率up招募和神秘招募需要)", "default": None},
            },
            "required": ["pool_type", "count"],
        },
    )
    registry.register(
        "calculate_speed", calculate_speed,
        "根据战斗行动值数据计算敌方速度。输入需包含'我方'和'敌方'两个区域的行动值数据。",
        {
            "type": "object",
            "properties": {"battle_data": {"type": "string", "description": "战斗数据(含我方/敌方行动值)"}},
            "required": ["battle_data"],
        },
    )
    registry.register(
        "compare_speed_probability", compare_speed_probability,
        "计算两个速度值之间的乱速概率",
        {
            "type": "object",
            "properties": {
                "speed_1": {"type": "integer", "description": "速度值1"},
                "speed_2": {"type": "integer", "description": "速度值2"},
            },
            "required": ["speed_1", "speed_2"],
        },
    )
    registry.register(
        "explain_code", explain_code_tool,
        "用中文详细解释一段代码的功能和原理",
        {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "要解释的代码"}},
            "required": ["code"],
        },
    )
    registry.register(
        "translate_text", translate_text,
        "在不同语言之间翻译文本",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要翻译的文本"},
                "target_language": {"type": "string", "description": "目标语言，默认中文", "default": "Chinese"},
            },
            "required": ["text"],
        },
    )

    return registry


# ── Global Agent Instance ────────────────────────────────────────

_tool_registry = _build_tool_registry()

_session_manager = SessionManager(
    max_context_messages=20,
    session_timeout=1800.0,
    persistence_dir=os.path.join(_DATA_DIR, "sessions"),
)

_memory_system = MemorySystem(
    base_dir=os.path.join(_DATA_DIR, "memory"),
)

_profile_manager = ProfileManager(
    base_dir=os.path.join(_DATA_DIR, "users"),
)
# The client is set after agent creation since agent owns the validated client
_profile_manager.set_client(deepseek_client)

agent = Agent(
    deepseek_client=deepseek_client,
    tool_registry=_tool_registry,
    config_dir=_CONFIG_DIR,
    session_manager=_session_manager,
    memory_system=_memory_system,
    profile_manager=_profile_manager,
    max_tool_iterations=5,
    thinking_timeout=180.0,
)

_model_router = ModelRouter()

_continuous_sessions = ContinuousSessionManager(timeout_minutes=5.0)


# ── Message Handlers ─────────────────────────────────────────────

# Catch ALL messages directed at the bot (requires @mention)
agent_router = on_message(priority=1, block=False, rule=to_me())


@agent_router.handle()
async def handle_agent_message(event: MessageEvent):
    """Route incoming QQ messages through the Agent."""
    user_id = str(event.user_id)
    text_content = event.get_plaintext().strip()

    # ── Detect and download file/image attachments ─────────────────
    file_context_parts = []
    for seg in event.message:
        if seg.type == "image":
            url = seg.data.get("url", "")
            file_id = seg.data.get("file", "")
            if url:
                saved_path, error = await _download_and_save_file(url, f"image-{file_id}")
                if saved_path:
                    file_context_parts.append(f"[用户上传了图片，已保存至: {saved_path}]")
                elif error:
                    file_context_parts.append(f"[用户上传了图片，但下载失败: {error}]")

        elif seg.type == "file":
            url = seg.data.get("url", "")
            name = seg.data.get("name", "file")
            if url:
                saved_path, error = await _download_and_save_file(url, name)
                if saved_path:
                    file_context_parts.append(f"[用户上传了文件 {name}，已保存至: {saved_path}]")
                elif error:
                    file_context_parts.append(f"[用户上传了文件 {name}，但下载失败: {error}]")

    # ── Build augmented message ────────────────────────────────────
    if file_context_parts:
        file_context = "\n".join(file_context_parts)
        if text_content:
            augmented_message = f"{file_context}\n用户说: {text_content}"
        else:
            augmented_message = f"{file_context}\n用户发送了文件，请使用 read_file 工具查看文件内容。"
    else:
        augmented_message = text_content

    # Guard: nothing to process
    if not augmented_message:
        return

    # Handle special commands that bypass the agent
    if augmented_message in ["/clear", "清除上下文", "新对话", "/status"]:
        await _handle_special_command(augmented_message, user_id)
        return

    # Send thinking indicator (non-critical, ignore send failures)
    try:
        await _safe_send("Roxy 正在思考...")
    except Exception:
        pass

    try:
        # ── Triage: classify complexity and route to appropriate model ──
        is_special = augmented_message in ["/clear", "清除上下文", "新对话", "/status"]
        if not is_special:
            complexity = await _model_router.classify_complexity(augmented_message)
            if complexity == "simple":
                client = _model_router.flash_client
            else:
                client = _model_router.reasoning_client
        else:
            client = None  # Use default client for special commands

        # Run the agent loop with timeout
        async def _progress(msg: str):
            await _safe_send(msg)
        response = await asyncio.wait_for(
            agent.run(augmented_message, user_id, client=client, progress_callback=_progress),
            timeout=200.0,  # Slightly more than thinking_timeout
        )

        # Send response (split long messages)
        await _send_response(response)

        # Start continuous session window for group chats (5 min)
        if isinstance(event, GroupMessageEvent):
            _continuous_sessions.start(str(event.group_id), user_id)

    except asyncio.TimeoutError:
        await _safe_send("抱歉，思考超时了。请尝试用更简单的方式提问~")
    except Exception as e:
        await _safe_send(f"处理消息时出现错误: {str(e)}")


# ── Continuous Mode Handler ───────────────────────────────────────

# Catch messages from users in continuous mode (no @mention needed)
continuous_router = on_message(priority=2, block=False)


@continuous_router.handle()
async def handle_continuous_message(event: MessageEvent):
    """Route messages from group users in continuous mode to the Agent."""
    # Only applies to group chats
    if not isinstance(event, GroupMessageEvent):
        return

    user_id = str(event.user_id)
    group_id = str(event.group_id)

    # Skip messages that @ the bot — agent_router (priority=1) already handled them
    if event.is_tome():
        return

    # Check if user is in continuous mode
    if not _continuous_sessions.is_active(group_id, user_id):
        return

    text_content = event.get_plaintext().strip()

    # Cancel detection: slash/hash commands
    if text_content in ["/取消", "#取消", "/结束", "#结束"]:
        _continuous_sessions.end(group_id, user_id)
        await _safe_send("已结束连续对话模式，之后需要@我才能触发~", matcher=continuous_router)
        return

    # Guard: nothing to process
    if not text_content:
        return

    # Renew the window on each message
    _continuous_sessions.touch(group_id, user_id)

    # Detect and download file/image attachments
    file_context_parts = []
    for seg in event.message:
        if seg.type == "image":
            url = seg.data.get("url", "")
            file_id = seg.data.get("file", "")
            if url:
                saved_path, error = await _download_and_save_file(url, f"image-{file_id}")
                if saved_path:
                    file_context_parts.append(f"[用户上传了图片，已保存至: {saved_path}]")
                elif error:
                    file_context_parts.append(f"[用户上传了图片，但下载失败: {error}]")

        elif seg.type == "file":
            url = seg.data.get("url", "")
            name = seg.data.get("name", "file")
            if url:
                saved_path, error = await _download_and_save_file(url, name)
                if saved_path:
                    file_context_parts.append(f"[用户上传了文件 {name}，已保存至: {saved_path}]")
                elif error:
                    file_context_parts.append(f"[用户上传了文件 {name}，但下载失败: {error}]")

    # Build augmented message with continuous mode context
    if file_context_parts:
        file_context = "\n".join(file_context_parts)
        augmented_message = (
            f"[连续对话模式] 用户未@你，正在继续之前的任务。"
            f"回复保持简洁。如果任务已完成，可以建议用户发送 /取消 来退出连续模式。\n"
            f"{file_context}\n用户说: {text_content}"
        )
    else:
        augmented_message = (
            f"[连续对话模式] 用户未@你，正在继续之前的任务。"
            f"回复保持简洁。如果任务已完成，可以建议用户发送 /取消 来退出连续模式。\n"
            f"用户说: {text_content}"
        )

    try:
        # Triage and route
        complexity = await _model_router.classify_complexity(augmented_message)
        if complexity == "simple":
            client = _model_router.flash_client
        else:
            client = _model_router.reasoning_client

        response = await asyncio.wait_for(
            agent.run(augmented_message, user_id, client=client,
                       progress_callback=lambda msg: _safe_send(msg, matcher=continuous_router)),
            timeout=200.0,
        )

        await _send_response(response, matcher=continuous_router)

    except asyncio.TimeoutError:
        await _safe_send("抱歉，思考超时了。请尝试用更简单的方式提问~", matcher=continuous_router)
    except Exception as e:
        await _safe_send(f"处理消息时出现错误: {str(e)}", matcher=continuous_router)


async def _safe_send(message: str, max_retries: int = 2, matcher=None):
    """Send a message with retry on timeout.

    Args:
        message: Message text to send.
        max_retries: Number of retry attempts on ActionFailed.
        matcher: Optional matcher to use for sending. Defaults to agent_router.
    """
    sender = matcher or agent_router
    last_error = None
    for attempt in range(max_retries):
        try:
            await sender.send(message)
            return
        except ActionFailed as e:
            last_error = e
            if attempt < max_retries - 1:
                await asyncio.sleep(1.0 * (attempt + 1))  # Exponential backoff
        except Exception:
            return  # Non-retryable errors (connection lost, etc.)
    # All retries exhausted — log but don't crash
    if last_error:
        from nonebot import logger
        logger.warning(f"Failed to send message after {max_retries} retries: {last_error.info}")


async def _handle_special_command(command: str, user_id: str):
    """Handle special meta-commands."""
    if command in ["/clear", "清除上下文", "新对话"]:
        agent.clear_user_session(user_id)
        await _safe_send("已清除对话上下文，开始新对话~")
    elif command == "/status":
        status = agent.get_status()
        tool_list = "\n  ".join(status["tool_names"])
        await _safe_send(
            f"Roxy 状态:\n"
            f"  活跃会话: {status['active_sessions']}\n"
            f"  已注册工具 ({status['tools_registered']}):\n  {tool_list}"
        )


async def _send_response(response: str, matcher=None):
    """Send response, splitting long messages into chunks with rate limiting.

    Args:
        response: The response text to send.
        matcher: Optional matcher to use for sending. Defaults to agent_router.
    """
    if not response:
        return

    # Shorter chunks + longer delays to avoid QQ rate limiting
    max_len = 300
    if len(response) <= max_len:
        await _safe_send(response, matcher=matcher)
    else:
        # Split on sentence boundaries when possible
        chunks = _split_text(response, max_len)
        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            await _safe_send(chunk.strip(), matcher=matcher)
            if i < len(chunks) - 1:
                await asyncio.sleep(1.0)  # Longer delay between chunks for QQ rate limit


def _split_text(text: str, max_len: int) -> list:
    """Split text into chunks, preferring sentence boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    current = ""
    # Split on sentence boundaries (Chinese + English punctuation)
    sentences = text.replace("。", "。|").replace("！", "！|").replace("？", "？|") \
                   .replace(".\n", ". |").replace("!\n", "! |").replace("?\n", "? |") \
                   .replace("\n\n", "\n\n|").split("|")

    for sentence in sentences:
        if len(current) + len(sentence) <= max_len:
            current += sentence
        else:
            if current.strip():
                chunks.append(current.strip())
            current = sentence

    if current.strip():
        chunks.append(current.strip())

    return chunks
