"""
Agent Router — Unified message entry point for the QQBot agent.

This plugin catches ALL incoming QQ messages and routes them through
the Agent. The Agent decides whether to respond directly or invoke tools.

This replaces the old distributed on_command architecture with a single
intelligent entry point.
"""

# ── Load .env into os.environ BEFORE any module-level reads ──────
# NoneBot2 (nb run) loads .env into its own pydantic config only, NOT
# into os.environ. Downstream module-level reads of os.environ (e.g.
# USER_DATA_ROOT, quota, session limits) would silently get defaults.
# This load_dotenv call must be the very first thing in this module.
from pathlib import Path
from dotenv import load_dotenv as _load_dotenv
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_load_dotenv(_ENV_FILE)

import asyncio
import json
import os
import re
import time
import uuid

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, Message, ActionFailed
from agent.agent import Agent
from agent.continuous_session import ContinuousSessionManager
from agent.context import (
    _send_msg, _current_user_workspace,
    _current_user_role, _current_code_limits,
    _current_user_id,
)
from agent.permissions import PermissionManager
from agent.hardware import HardwareDetector
from agent.special_session import SpecialSessionManager
from agent.tool_registry import ToolRegistry
from agent.session import SessionManager
from agent.memory import MemorySystem
from agent.profile import ProfileManager
from agent.workspace import UserWorkspaceManager
from lib.deepseek_client import deepseek_client as _global_client, DeepSeekClient as _DeepSeekClient
from lib.model_router import ModelRouter

# Handle case where NoneBot is not running (testing)
deepseek_client = _global_client if _global_client is not None else _DeepSeekClient()
from tools.builtin_tools import (
    execute_code,
    get_system_load,
    get_time,
    search_web,
    web_fetch,
    download_repo,
    shell_exec,
    summarize_pdf,
    _ensure_workspace_dirs,
)
from tools.file_tools import read_file
from tools.map_tools import (
    geocode,
    reverse_geocode,
    get_weather as map_get_weather,
    search_poi,
    plan_route,
)
from tools.legacy_tools import (
    calculate_speed,
    compare_speed_probability,
    explain_code_tool,
    gacha_pull,
    play_gacha_animation,
    translate_text,
)

# ── Configuration Paths ───────────────────────────────────────────

_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_DIR = os.path.join(_AGENT_DIR, "agent", "config")
_DATA_DIR = os.path.join(_AGENT_DIR, "data")
_USER_DATA_ROOT = os.environ.get("USER_DATA_ROOT", os.path.join(_AGENT_DIR, "data", "users_store"))

# ── Workspace Initialization ─────────────────────────────────────

def _init_workspace():
    """Create workspace directories if they don't exist."""
    from tools.builtin_tools import _ensure_workspace_dirs
    _ensure_workspace_dirs()

_init_workspace()


# ── File Download Helpers ──────────────────────────────────────────

def _get_uploads_dir() -> str:
    """Get the uploads directory at runtime, respecting user workspace contextvar.

    Unlike the module-level WORKSPACE_UPLOADS constant (frozen at import time),
    this checks _current_user_workspace on every call so files are saved to the
    correct per-user workspace during special sessions.
    """
    user_ws = _current_user_workspace.get()
    if user_ws:
        return os.path.join(user_ws, "uploads")
    # Fallback: shared workspace default
    return os.path.join(_AGENT_DIR, "data", "workspace", "uploads")


async def _download_voice(bot, seg_data: dict, message_id: str = "", max_size_mb: int = 50) -> tuple:
    """Download a QQ voice message.

    Args:
        bot: NoneBot2 OneBot V11 Bot instance.
        seg_data: The ``data`` dict from the record message segment.
        message_id: The QQ message ID (some NapCat versions require this).
        max_size_mb: Maximum file size in MB.

    Returns:
        (saved_path, error_message) — one is None, the other is not.
    """
    file_id = seg_data.get("file", "")
    if not file_id:
        return None, "语音文件 ID 为空，无法下载。"

    max_size_bytes = max_size_mb * 1024 * 1024
    _ensure_workspace_dirs()
    uploads_dir = _get_uploads_dir()
    os.makedirs(uploads_dir, exist_ok=True)
    save_path = os.path.join(
        uploads_dir,
        f"{uuid.uuid4().hex[:8]}-{file_id}"
    )

    diag = []  # Collect diagnostics for the final error message

    # ── Strategy 1: read local file directly ──────────────────────
    for field in ("path", "url"):
        local = seg_data.get(field, "")
        if not local:
            diag.append(f"[{field}] 字段为空")
            continue
        try:
            with open(local, "rb") as src:
                data = src.read()
            if len(data) == 0:
                diag.append(f"[{field}] 文件为空: {local}")
                continue
            if len(data) > max_size_bytes:
                return None, f"语音文件过大 ({len(data) / 1024 / 1024:.1f} MB)，无法处理。"
            with open(save_path, "wb") as dst:
                dst.write(data)
            return save_path, None
        except FileNotFoundError:
            diag.append(f"[{field}] 文件不存在: {local[:120]}")
        except PermissionError:
            diag.append(f"[{field}] 无权限读取: {local[:120]}")
        except (IOError, OSError) as e:
            diag.append(f"[{field}] IO错误: {e} — {local[:120]}")

    # ── Strategy 2: OneBot get_record API ──────────────────────────
    # Build base params: file / file_id + optional message_id
    base_params = {}
    if message_id:
        base_params["message_id"] = message_id

    # NapCat may use non-standard action/param names — try combinations
    for action in ("get_record", "get_file", "getRecord", "download_file"):
        for param_name in ("file", "file_id"):
            params = {param_name: file_id, **base_params}
            try:
                result = await bot.call_api(action, **params)
            except Exception as e:
                diag.append(f"[API {action} {param_name}=] 异常: {e}")
                continue

            if isinstance(result, dict):
                url_or_data = result.get("file", "") or result.get("path", "") or ""
            elif isinstance(result, str):
                url_or_data = result
            else:
                diag.append(f"[API {action} {param_name}=] 返回类型异常: {type(result).__name__}")
                continue

            if not url_or_data:
                diag.append(f"[API {action} {param_name}=] 无 file/path 字段: {json.dumps(result, ensure_ascii=False)[:200]}")
                continue

            try:
                if url_or_data.startswith("base64://"):
                    import base64
                    data = base64.b64decode(url_or_data[len("base64://"):])
                elif url_or_data.startswith(("http://", "https://")):
                    async with httpx.AsyncClient() as client:
                        response = await client.get(url_or_data, timeout=120.0, follow_redirects=True)
                        response.raise_for_status()
                        data = response.content
                elif os.path.isfile(url_or_data):
                    with open(url_or_data, "rb") as src:
                        data = src.read()
                else:
                    diag.append(f"[API {action} {param_name}=] 无法识别返回格式: {url_or_data[:120]}")
                    continue

                if len(data) > max_size_bytes:
                    return None, f"语音文件过大 ({len(data) / 1024 / 1024:.1f} MB)，无法处理。"

                with open(save_path, "wb") as dst:
                    dst.write(data)
                return save_path, None

            except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
                diag.append(f"[API {action} {param_name}=] 下载失败: {e}")
                continue
            except Exception as e:
                diag.append(f"[API {action} {param_name}=] 处理返回值出错: {e}")
                continue

    # ── Strategy 3: NapCat HTTP API ────────────────────────────────
    napcat_base = os.environ.get("NAPCAT_HTTP_BASE", "http://127.0.0.1:6099")
    http_endpoints = (
        ("POST", "/api/get_record"),
        ("POST", "/api/getRecord"),
        ("POST", "/api/record"),
        ("POST", "/api/onebot/get_record"),
        ("GET",  "/api/get_record"),
    )
    for method, endpoint in http_endpoints:
        try:
            async with httpx.AsyncClient() as client:
                if method == "GET":
                    resp = await client.get(
                        f"{napcat_base.rstrip('/')}{endpoint}",
                        params={"file": file_id},
                        timeout=10.0,
                    )
                else:
                    resp = await client.post(
                        f"{napcat_base.rstrip('/')}{endpoint}",
                        json={"file": file_id},
                        timeout=10.0,
                    )
                if resp.status_code != 200:
                    diag.append(f"[HTTP {method} {endpoint}] HTTP {resp.status_code}")
                    continue

                # Try to extract file data from various response shapes
                body = resp.text
                result = None
                try:
                    result = resp.json()
                except Exception:
                    pass

                file_data = ""
                if isinstance(result, dict):
                    file_data = (
                        result.get("file", "") or
                        result.get("data", {}).get("file", "") if isinstance(result.get("data"), dict) else "" or
                        result.get("result", {}).get("file", "") if isinstance(result.get("result"), dict) else "" or
                        str(result)
                    )
                elif isinstance(result, str):
                    file_data = result

                if file_data and file_data.startswith("base64://"):
                    import base64
                    data = base64.b64decode(file_data[len("base64://"):])
                elif file_data and os.path.isfile(str(file_data)):
                    with open(str(file_data), "rb") as src:
                        data = src.read()
                elif file_data and len(file_data) > 100:
                    # Might be raw base64 (without the base64:// prefix)
                    try:
                        import base64
                        data = base64.b64decode(file_data)
                    except Exception:
                        # Also try raw binary response
                        if resp.content and len(resp.content) > 10:
                            data = resp.content
                        else:
                            diag.append(f"[HTTP {method} {endpoint}] 无法解析响应: {body[:120]}")
                            continue
                elif resp.content and len(resp.content) > 10:
                    # Response might be raw binary (the audio file itself)
                    data = resp.content
                else:
                    diag.append(f"[HTTP {method} {endpoint}] 无法解析响应: {body[:120]}")
                    continue

                if len(data) <= max_size_bytes:
                    with open(save_path, "wb") as dst:
                        dst.write(data)
                    return save_path, None

        except Exception as e:
            diag.append(f"[HTTP {method} {endpoint}] 异常: {e}")
            continue

    return None, f"语音下载失败。诊断: {'; '.join(diag)}"


async def _download_and_save_file(
    url: str, filename: str, max_size_mb: int = 50,
    bot=None, file_id: str = "",
) -> tuple:
    """Download a file from QQ and save to workspace uploads.

    When the direct ``url`` is available it is used first (standard for
    group-chat files).  When ``url`` is empty (common in private chats) the
    function falls back to the OneBot ``get_file`` API if *bot* and *file_id*
    are provided.

    Args:
        url: Download URL from the message segment (may be empty).
        filename: Original filename (used for extension detection).
        max_size_mb: Maximum file size in MB.
        bot: Optional OneBot V11 Bot instance for API fallback.
        file_id: File ID from the message segment for API fallback.

    Returns:
        (saved_path, error_message) — one is None, the other is not.
    """
    # ── Pick uploads dir at runtime (respects per-user workspace) ──
    _ensure_workspace_dirs()
    uploads_dir = _get_uploads_dir()
    os.makedirs(uploads_dir, exist_ok=True)

    # Generate safe filename: uuid8 prefix + sanitized original name
    ext = os.path.splitext(filename)[1] or ""
    safe_name = re.sub(r'[^\w\-_.]', '_', os.path.splitext(filename)[0])
    if not safe_name:
        safe_name = "file"
    unique_name = f"{uuid.uuid4().hex[:8]}-{safe_name}{ext}"
    save_path = os.path.join(uploads_dir, unique_name)

    max_size_bytes = max_size_mb * 1024 * 1024

    # ── Strategy 1: direct URL download ──────────────────────────
    if url:
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

    # ── Strategy 2: OneBot get_file API fallback (private chats) ──
    if bot is not None and file_id:
        diag = []
        for action in ("get_file", "getFile", "download_file"):
            for param_name in ("file", "file_id"):
                try:
                    result = await bot.call_api(action, **{param_name: file_id})
                except Exception as e:
                    diag.append(f"[API {action} {param_name}=] 异常: {e}")
                    continue

                if isinstance(result, dict):
                    file_data = result.get("file", "") or result.get("path", "") or result.get("url", "") or ""
                elif isinstance(result, str):
                    file_data = result
                else:
                    diag.append(f"[API {action} {param_name}=] 返回类型异常: {type(result).__name__}")
                    continue

                if not file_data:
                    diag.append(f"[API {action} {param_name}=] 无 file/path/url 字段: {json.dumps(result, ensure_ascii=False)[:200]}")
                    continue

                try:
                    import base64
                    if file_data.startswith("base64://"):
                        data = base64.b64decode(file_data[len("base64://"):])
                    elif file_data.startswith(("http://", "https://")):
                        async with httpx.AsyncClient() as client:
                            resp = await client.get(file_data, timeout=120.0, follow_redirects=True)
                            resp.raise_for_status()
                            data = resp.content
                    elif os.path.isfile(file_data):
                        with open(file_data, "rb") as src:
                            data = src.read()
                    else:
                        diag.append(f"[API {action} {param_name}=] 无法识别返回格式: {file_data[:120]}")
                        continue

                    if len(data) > max_size_bytes:
                        return None, f"文件过大 ({len(data) / 1024 / 1024:.1f} MB)，超过限制 ({max_size_mb} MB)。"

                    with open(save_path, "wb") as f:
                        f.write(data)
                    return save_path, None

                except Exception as e:
                    diag.append(f"[API {action} {param_name}=] 处理返回值出错: {e}")
                    continue

        return None, f"文件下载失败 (API fallback 已尝试 {len(diag)} 次)。诊断: {'; '.join(diag)}"

    if not url and not file_id:
        return None, "文件缺少下载地址和文件 ID，可能未成功上传或 QQ 客户端限制了文件传输。"
    if not url:
        return None, "文件下载地址为空（私聊文件可能需通过 OneBot API 下载，但缺少 bot 连接）。"


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
        "web_fetch", web_fetch,
        "抓取指定网页URL的内容并提取纯文本。仅支持HTTPS。当搜索无结果或需要完整页面内容时使用。",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的网页URL（必须是HTTPS链接）"},
            },
            "required": ["url"],
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
        "shell_exec", shell_exec,
        "在服务器上执行只读 shell 命令。支持管道 (|) 串联多个命令，每个命令必须属于白名单。"
        "允许的命令包括: ls/find/cat/head/tail/grep/wc/sort/uniq/du/df/free/file/stat/python3 -c 等。"
        "禁止: 重定向 (>/>>/<)、命令替换 ($()/``)、后台运行 (&)、链接执行 (;/&&/||)、sed -i。"
        "适合: 查看目录结构、统计文件行数、检查磁盘内存、快速文本处理、git log/status。",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令。支持管道，如 'ls -la | wc -l'"},
                "timeout": {"type": "integer", "description": "超时秒数，默认15", "default": 15},
            },
            "required": ["command"],
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
        "读取用户上传的文件内容。支持文本文件（代码、日志、配置等）、PDF文件、图片和语音/音频消息。"
        "文本和PDF返回文字内容，图片返回基础信息+AI分析，音频返回元数据+AI转录和情绪分析（需配置音频模型）。",
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

    # Map / location tools (Amap API)
    registry.register(
        "geocode", geocode,
        "将地址转换为经纬度坐标。输入地址（如'深圳南山科技园'）返回坐标和规范化地址。",
        {
            "type": "object",
            "properties": {
                "address": {"type": "string", "description": "要查询的地址或地名"},
                "city": {"type": "string", "description": "可选城市名，用于缩小搜索范围"},
            },
            "required": ["address"],
        },
    )
    registry.register(
        "reverse_geocode", reverse_geocode,
        "将经纬度坐标转换为地址。输入坐标（格式'经度,纬度'）返回详细地址、周边POI和行政区划。",
        {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "经纬度坐标，格式'经度,纬度'，如'113.952,22.542'"},
            },
            "required": ["location"],
        },
    )
    registry.register(
        "get_weather", map_get_weather,
        "查询指定城市的天气。支持实时天气和4天预报。比搜索更精准。",
        {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名称或行政区划代码，如'深圳'、'北京'"},
                "forecast": {"type": "boolean", "description": "是否查询预报。false=实时天气，true=4天预报", "default": False},
            },
            "required": ["city"],
        },
    )
    registry.register(
        "search_poi", search_poi,
        "搜索地点/Poi。查找餐厅、地铁站、银行、商场、景点等。",
        {
            "type": "object",
            "properties": {
                "keywords": {"type": "string", "description": "搜索关键词，如'餐厅'、'地铁站'、'北京大学'"},
                "city": {"type": "string", "description": "可选城市名，用于限定搜索范围"},
                "num_results": {"type": "integer", "description": "返回结果数量，默认5条", "default": 5},
            },
            "required": ["keywords"],
        },
    )
    registry.register(
        "plan_route", plan_route,
        "规划两点之间的出行路线。支持驾车、步行、公交三种方式。返回距离、时间和步骤。",
        {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "起点。可以是坐标（'113.95,22.54'）或地址"},
                "destination": {"type": "string", "description": "终点。格式同起点"},
                "mode": {
                    "type": "string",
                    "description": "出行方式",
                    "enum": ["driving", "walking", "transit"],
                    "default": "driving",
                },
            },
            "required": ["origin", "destination"],
        },
    )

    # Legacy tools (game features)
    registry.register(
        "gacha_pull", gacha_pull,
        "模拟游戏抽卡/招募。支持单抽和十连抽，四种卡池类型。"
        "重要：调用前必须先询问用户是否要播放抽卡动画！不要直接给出结果。"
        "如果用户选择播放动画，先调用 play_gacha_animation 播放动画，再给出文字结果。"
        "如果用户选择跳过动画，直接调用此工具给出文字结果。",
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
        "play_gacha_animation", play_gacha_animation,
        "播放抽卡动画。传入最高星级（3=蓝色, 4=紫色, 5=金色, 6=红色）和是否为单抽。"
        "动画会直接发送到QQ聊天窗口。应该在给出文字抽卡结果之前调用此工具。",
        {
            "type": "object",
            "properties": {
                "star_level": {"type": "integer", "description": "最高星级。3=蓝色, 4=紫色, 5=金色, 6=红色", "enum": [3, 4, 5, 6]},
                "is_single": {"type": "boolean", "description": "是否为单抽。true=单抽, false=十连", "default": False},
            },
            "required": ["star_level", "is_single"],
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

    # ── System Load ─────────────────────────────────────────────
    registry.register(
        "get_system_load", get_system_load,
        "获取服务器实时负载信息（CPU/内存/磁盘使用率）。"
        "在执行高负载任务之前调用此工具，判断服务器是否有足够资源。"
        "返回 CPU 负载、内存使用量、磁盘剩余空间及负载评估。",
        {
            "type": "object",
            "properties": {},
            "required": [],
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
    base_dir=_USER_DATA_ROOT,
)
# The client is set after agent creation since agent owns the validated client
_profile_manager.set_client(deepseek_client)

_hardware_detector = HardwareDetector(cache_dir=_USER_DATA_ROOT)

_user_workspace_quota_mb = int(os.environ.get("USER_WORKSPACE_QUOTA_MB", "500"))
_workspace_manager = UserWorkspaceManager(
    user_data_root=_USER_DATA_ROOT,
    quota_mb=_user_workspace_quota_mb,
)

_max_special_sessions = int(os.environ.get("MAX_SPECIAL_SESSIONS", "3"))
_special_sessions = SpecialSessionManager(
    user_data_root=_USER_DATA_ROOT,
    max_per_user=_max_special_sessions,
    llm_client=deepseek_client,
)

# Track sessions pending LLM auto-naming: user_id -> True
_pending_naming: dict = {}

# Track pending deletion confirmations: user_id -> (session_name, expiry_timestamp)
_pending_delete_confirm: dict = {}

agent = Agent(
    deepseek_client=deepseek_client,
    tool_registry=_tool_registry,
    config_dir=_CONFIG_DIR,
    session_manager=_session_manager,
    memory_system=_memory_system,
    profile_manager=_profile_manager,
    hardware_detector=_hardware_detector,
    workspace_manager=_workspace_manager,
    special_session_manager=_special_sessions,
    max_tool_iterations=20,
    thinking_timeout=180.0,
)

_model_router = ModelRouter()

_continuous_sessions = ContinuousSessionManager(timeout_minutes=5.0)

_perm_manager = PermissionManager()

# Per-user busy flag — prevents concurrent message processing for the
# same user. When a user's message is being processed, subsequent
# messages from that user are rejected with a brief "busy" reply.
_user_busy: set = set()

# Recent file tracking — maps message_id to downloaded file info so that
# when a user replies to a file message we can resolve which file they mean.
_MAX_RECENT_FILES = 200
_recent_files: dict[str, list[dict]] = {}


def _record_file(message_id: str, name: str, path: str):
    """Record a downloaded file against its source message for reply resolution."""
    if message_id not in _recent_files:
        _recent_files[message_id] = []
    _recent_files[message_id].append({"name": name, "path": path})
    # Prune oldest entries if cache grows too large
    while len(_recent_files) > _MAX_RECENT_FILES:
        oldest = next(iter(_recent_files))
        del _recent_files[oldest]


def _build_reply_context(event: MessageEvent) -> str:
    """Extract reply/quote context from a message's reply segment.

    Returns a string for injection into the augmented message, or "" if
    there is no reply segment.
    """
    for seg in event.message:
        if seg.type != "reply":
            continue
        reply_id = seg.data.get("id", "")
        reply_text = seg.data.get("text", "") or seg.data.get("message", "") or ""

        parts = []
        if reply_text:
            parts.append(f"[用户引用了消息: \"{reply_text}\"]")
        elif reply_id:
            parts.append(f"[用户回复了消息 {reply_id}]")

        # Check if the replied-to message had files
        if reply_id and reply_id in _recent_files:
            for f in _recent_files[reply_id]:
                parts.append(
                    f"[被引用消息中包含文件 {f['name']}，已保存至: {f['path']}]"
                )

        return "\n".join(parts) if parts else ""

    return ""


# ── User Info Tool ─────────────────────────────────────────────────

def _get_user_info() -> str:
    """返回当前用户的系统信息快照（权限、会话、工作区），零推理 token 消耗。

    适用场景：用户询问「我的设置」「我有什么权限」「我的工作区」「我的会话」等。

    此工具直接读取系统状态，无需 LLM 推理即可返回结构化信息。
    """
    user_id = _current_user_id.get()
    if not user_id:
        return "无法获取用户信息：当前请求未设置用户上下文。"

    role = _perm_manager.get_role(user_id)
    role_label = {"admin": "管理员", "vip": "会员", "regular": "普通用户"}[role.value]

    lines = [f"用户信息快照", f"", f"用户 ID: {user_id}", f"权限级别: {role_label} ({role.value})"]

    # ── 特殊会话 ──
    sessions = _special_sessions.list_sessions(user_id)
    active = _special_sessions.get_active(user_id)
    max_sessions = _perm_manager.get_max_special_sessions(role)
    lines.append(f"")
    lines.append(f"特殊会话 ({len(sessions)}/{max_sessions}):")
    if sessions:
        for s in sessions:
            marker = " ★ 当前" if active and s["name"] == active.name else ""
            lines.append(f"  · {s['name']}{marker} — {s['total_messages']} 条消息")
    else:
        lines.append(f"  (无特殊会话)")

    # ── 工作区 ──
    ws_path = _workspace_manager.get_workspace(user_id)
    ws_size = _workspace_manager.get_size(user_id)
    ws_quota_mb = _perm_manager.get_workspace_quota_mb(role)
    ws_usage_mb = ws_size / (1024 * 1024)
    pct = (ws_size / (ws_quota_mb * 1024 * 1024)) * 100 if ws_quota_mb > 0 else 0
    lines.append(f"")
    lines.append(f"工作区:")
    lines.append(f"  路径: {ws_path}")
    lines.append(f"  用量: {ws_usage_mb:.1f} MB / {ws_quota_mb} MB ({pct:.1f}%)")

    # ── 工作区目录快照 ──
    lines.append(f"")
    lines.append(f"  目录快照:")
    ws = Path(ws_path)
    if ws.is_dir():
        for subdir_name in ["code", "output", "projects", "uploads"]:
            subdir = ws / subdir_name
            if not subdir.is_dir():
                continue
            try:
                entries = sorted(subdir.iterdir(), key=lambda e: e.name.lower())
            except PermissionError:
                lines.append(f"    {subdir_name}/ (无权限访问)")
                continue

            if not entries:
                lines.append(f"    {subdir_name}/ (空)")
            else:
                lines.append(f"    {subdir_name}/ ({len(entries)} 项)")
                for entry in entries[:20]:  # cap at 20 entries per dir
                    if entry.is_symlink():
                        lines.append(f"      {entry.name} -> (符号链接)")
                    elif entry.is_dir():
                        item_count = sum(1 for _ in entry.rglob("*"))
                        lines.append(f"      {entry.name}/ ({item_count} 项)")
                    else:
                        size = entry.stat().st_size
                        if size < 1024:
                            size_str = f"{size} B"
                        elif size < 1024 * 1024:
                            size_str = f"{size / 1024:.1f} KB"
                        else:
                            size_str = f"{size / (1024 * 1024):.1f} MB"
                        lines.append(f"      {entry.name} ({size_str})")
                if len(entries) > 20:
                    lines.append(f"      ... 还有 {len(entries) - 20} 项未显示")
    else:
        lines.append(f"    (工作区目录不存在)")

    # ── 权限范围 ──
    allowed = _perm_manager.get_allowed_tools(role)
    code_limits = _perm_manager.get_code_limits(role)

    lines.append(f"")
    lines.append(f"可用工具: {len(allowed)} 个")

    # Categorize tools
    info_tools = {"search_web", "get_time", "get_weather", "read_file", "summarize_pdf",
                  "geocode", "reverse_geocode", "search_poi", "plan_route"}
    dev_tools = {"execute_code", "shell_exec", "web_fetch", "download_repo", "get_system_load"}
    fun_tools = {"gacha_pull", "play_gacha_animation", "calculate_speed",
                 "compare_speed_probability", "explain_code", "translate_text"}
    misc = allowed - info_tools - dev_tools - fun_tools

    sections = [
        ("信息查询", sorted(allowed & info_tools)),
        ("开发工具", sorted(allowed & dev_tools)),
        ("娱乐工具", sorted(allowed & fun_tools)),
    ]
    if misc:
        sections.append(("其他", sorted(misc)))

    for label, tools in sections:
        if tools:
            lines.append(f"  [{label}] {', '.join(tools)}")
        else:
            lines.append(f"  [{label}] (无)")

    if code_limits:
        lines.append(f"")
        lines.append(f"代码执行限制:")
        lines.append(f"  超时: {code_limits.max_timeout}s")
        lines.append(f"  输出上限: {code_limits.max_output // 1024}KB")
        lines.append(f"  内存: {code_limits.max_memory_mb}MB")

    return "\n".join(lines)


# Register user info tool (must happen after _get_user_info definition and after
# singletons like _workspace_manager, _special_sessions are initialized)
_tool_registry.register(
    "get_user_info", _get_user_info,
    "获取当前用户的系统信息，包括：权限级别、特殊会话列表、工作区用量、可用工具范围、"
    "代码执行限制（如有）。当用户询问「我的设置」「我的权限」「我的工作区」「我的会话」"
    "「我能用什么工具」或类似用户自身信息相关问题时，应调用此工具。此工具返回结构化"
    "系统数据，可避免 LLM 在系统信息类问题上浪费推理 token。",
    {"type": "object", "properties": {}, "required": []},
)


# ── Message Handlers ─────────────────────────────────────────────

# Catch ALL messages. For group messages, we manually check for @mentions
# instead of relying on to_me() rule, which depends on NapCat setting
# the "to_me" field in the raw event data (which may not always happen).
agent_router = on_message(priority=1, block=False)


@agent_router.handle()
async def handle_agent_message(bot: Bot, event: MessageEvent):
    """Route incoming QQ messages through the Agent."""
    user_id = str(event.user_id)

    # Manual @mention check for group messages (more reliable than to_me())
    if isinstance(event, GroupMessageEvent):
        bot_qq = str(event.self_id)
        is_at_bot = any(
            seg.type == "at" and seg.data.get("qq") == bot_qq
            for seg in event.message
        )
        if not is_at_bot and not event.is_tome():
            return  # Not directed at bot, skip silently

    # ── Per-user concurrency guard ──
    if user_id in _user_busy:
        await _safe_send("Roxy 正在处理你的上一条消息，请稍等~")
        return
    _user_busy.add(user_id)
    try:
        return await _handle_agent_message_impl(bot, event, user_id)
    finally:
        _user_busy.discard(user_id)


async def _handle_agent_message_impl(bot: Bot, event: MessageEvent, user_id: str):
    """Inner implementation — called under per-user busy guard."""

    # Set user workspace for tool scoping
    _workspace_manager.ensure_dirs(user_id)
    _current_user_workspace.set(_workspace_manager.get_workspace(user_id))

    text_content = event.get_plaintext().strip()

    # ── Handle feedback / bug report (before agent, zero token cost) ─
    if text_content.startswith(("#反馈", "#bug", "#建议")):
        await _handle_feedback(text_content, user_id)
        return

    # ── Handle session management commands ──────────────────────────
    if text_content.startswith("/") or text_content.startswith("#"):
        cmd_handled = await _handle_session_command(text_content, user_id)
        if cmd_handled:
            return

    # ── Detect reply/quote context ─────────────────────────────────
    reply_context = _build_reply_context(event)

    # ── Detect and download file/image attachments ─────────────────
    file_context_parts = []
    msg_id = str(event.message_id)
    for seg in event.message:
        if seg.type == "image":
            url = seg.data.get("url", "")
            file_id = seg.data.get("file", "")
            saved_path, error = await _download_and_save_file(url, f"image-{file_id}", bot=bot, file_id=file_id)
            if saved_path:
                file_context_parts.append(f"[用户上传了图片，已保存至: {saved_path}]")
                _record_file(msg_id, f"image-{file_id}", saved_path)
            elif error:
                file_context_parts.append(f"[用户上传了图片，但下载失败: {error}]")

        elif seg.type == "file":
            url = seg.data.get("url", "")
            name = seg.data.get("name", "file")
            file_id = seg.data.get("file", "")
            saved_path, error = await _download_and_save_file(url, name, bot=bot, file_id=file_id)
            if saved_path:
                file_context_parts.append(f"[用户上传了文件 {name}，已保存至: {saved_path}]")
                _record_file(msg_id, name, saved_path)
            elif error:
                file_context_parts.append(f"[用户上传了文件 {name}，但下载失败: {error}]")

        elif seg.type == "record":
            saved_path, error = await _download_voice(bot, seg.data, str(event.message_id))
            if saved_path:
                file_context_parts.append(
                    f"[用户发送了语音消息，已保存至: {saved_path}]"
                )
                _record_file(msg_id, "语音消息", saved_path)
            elif error:
                file_context_parts.append(f"[用户发送了语音消息，但下载失败: {error}]")

    # ── File-only messages: acknowledge and skip agent ─────────────
    has_files = bool(file_context_parts)
    if has_files and not text_content and not reply_context:
        names = []
        for part in file_context_parts:
            m = re.search(r"文件 (.+?)，", part) or re.search(r"上传了(\w+)，", part)
            if m:
                names.append(m.group(1))
        if names:
            ack = f"已收到 {'、'.join(names)}，需要分析的话引用这条消息告诉我~"
        else:
            ack = "已收到文件，需要分析的话引用这条消息告诉我~"
        await _safe_send(ack)
        return

    # ── Build augmented message ────────────────────────────────────
    context_parts = []
    if reply_context:
        context_parts.append(reply_context)
    if file_context_parts:
        context_parts.append("\n".join(file_context_parts))
    context_prefix = "\n".join(context_parts)

    if context_prefix:
        if text_content:
            augmented_message = f"{context_prefix}\n用户说: {text_content}"
        else:
            augmented_message = f"{context_prefix}\n用户引用了文件/语音消息，请使用 read_file 工具查看内容。"
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
        async def _send_image(seg):
            await _safe_send(seg)
        token = _send_msg.set(_send_image)

        # Detect session type
        active_special = _special_sessions.get_active(user_id)
        session_type = "special" if active_special else "temporary"

        # Resolve user permissions
        role = _perm_manager.get_role(user_id)
        allowed_tools = _perm_manager.get_allowed_tools(role)
        code_limits = _perm_manager.get_code_limits(role)

        # Set permission contextvars for downstream tools
        _current_user_id.set(user_id)
        _current_user_role.set(role.value)
        if code_limits:
            _current_code_limits.set(code_limits.to_dict())

        try:
            response = await asyncio.wait_for(
                agent.run(
                    augmented_message, user_id,
                    client=client,
                    progress_callback=_progress,
                    session_type=session_type,
                    allowed_tools=allowed_tools,
                    user_role=role.value,
                ),
                timeout=300.0,
            )
        finally:
            _send_msg.reset(token)

        # Send response (split long messages)
        await _send_response(response)

        # Auto-name special session after first interaction
        if session_type == "special" and _pending_naming.pop(user_id, False):
            try:
                first_msg = augmented_message[:200]
                asyncio.create_task(
                    _special_sessions.auto_name(user_id, first_msg, response)
                )
            except Exception:
                pass

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
async def handle_continuous_message(bot: Bot, event: MessageEvent):
    """Route messages from group users in continuous mode to the Agent."""
    # Only applies to group chats
    if not isinstance(event, GroupMessageEvent):
        return

    user_id = str(event.user_id)
    group_id = str(event.group_id)

    # Skip messages that @ the bot — agent_router (priority=1) already handled them
    if event.is_tome():
        return
    bot_qq = str(event.self_id)
    if any(seg.type == "at" and seg.data.get("qq") == bot_qq for seg in event.message):
        return  # agent_router will handle this (manual @mention detection)

    # Check if user is in continuous mode
    if not _continuous_sessions.is_active(group_id, user_id):
        return

    # ── Per-user concurrency guard ──
    if user_id in _user_busy:
        await _safe_send("Roxy 正在处理你的上一条消息，请稍等~", matcher=continuous_router)
        return
    _user_busy.add(user_id)
    try:
        return await _handle_continuous_message_impl(bot, event, user_id, group_id)
    finally:
        _user_busy.discard(user_id)


async def _handle_continuous_message_impl(bot: Bot, event: MessageEvent, user_id: str, group_id: str):
    """Inner implementation — called under per-user busy guard."""

    text_content = event.get_plaintext().strip()

    # Cancel detection: slash/hash commands
    if text_content in ["/取消", "#取消", "/结束", "#结束"]:
        _continuous_sessions.end(group_id, user_id)
        await _safe_send("已结束连续对话模式，之后需要@我才能触发~", matcher=continuous_router)
        return

    # ── Detect reply/quote context and file attachments ────────────
    # These run BEFORE the text guard so files are always saved and
    # recorded, even for file-only messages that may be replied to later.
    reply_context = _build_reply_context(event)

    file_context_parts = []
    msg_id = str(event.message_id)
    for seg in event.message:
        if seg.type == "image":
            url = seg.data.get("url", "")
            file_id = seg.data.get("file", "")
            saved_path, error = await _download_and_save_file(url, f"image-{file_id}", bot=bot, file_id=file_id)
            if saved_path:
                file_context_parts.append(f"[用户上传了图片，已保存至: {saved_path}]")
                _record_file(msg_id, f"image-{file_id}", saved_path)
            elif error:
                file_context_parts.append(f"[用户上传了图片，但下载失败: {error}]")

        elif seg.type == "file":
            url = seg.data.get("url", "")
            name = seg.data.get("name", "file")
            file_id = seg.data.get("file", "")
            saved_path, error = await _download_and_save_file(url, name, bot=bot, file_id=file_id)
            if saved_path:
                file_context_parts.append(f"[用户上传了文件 {name}，已保存至: {saved_path}]")
                _record_file(msg_id, name, saved_path)
            elif error:
                file_context_parts.append(f"[用户上传了文件 {name}，但下载失败: {error}]")

        elif seg.type == "record":
            saved_path, error = await _download_voice(bot, seg.data, str(event.message_id))
            if saved_path:
                file_context_parts.append(
                    f"[用户发送了语音消息，已保存至: {saved_path}]"
                )
                _record_file(msg_id, "语音消息", saved_path)
            elif error:
                file_context_parts.append(f"[用户发送了语音消息，但下载失败: {error}]")

    # Renew the window on each message
    _continuous_sessions.touch(group_id, user_id)

    # Guard: nothing to process (no text, no files, no reply)
    if not text_content and not file_context_parts and not reply_context:
        return

    # Build augmented message with continuous mode context
    context_parts = []
    if reply_context:
        context_parts.append(reply_context)
    if file_context_parts:
        context_parts.append("\n".join(file_context_parts))

    continuous_prefix = (
        "[连续对话模式] 用户未@你，正在继续之前的任务。"
        "回复保持简洁。如果任务已完成，可以建议用户发送 /取消 来退出连续模式。"
    )
    if context_parts:
        augmented_message = f"{continuous_prefix}\n{'\n'.join(context_parts)}\n用户说: {text_content}"
    else:
        augmented_message = f"{continuous_prefix}\n用户说: {text_content}"

    try:
        # Triage and route
        complexity = await _model_router.classify_complexity(augmented_message)
        if complexity == "simple":
            client = _model_router.flash_client
        else:
            client = _model_router.reasoning_client

        async def _send_image(seg):
            await _safe_send(seg, matcher=continuous_router)
        token = _send_msg.set(_send_image)
        try:
            # Resolve user permissions
            role = _perm_manager.get_role(user_id)
            allowed_tools = _perm_manager.get_allowed_tools(role)
            code_limits = _perm_manager.get_code_limits(role)
            _current_user_id.set(user_id)
            _current_user_role.set(role.value)
            if code_limits:
                _current_code_limits.set(code_limits.to_dict())

            response = await asyncio.wait_for(
                agent.run(augmented_message, user_id, client=client,
                           progress_callback=lambda msg: _safe_send(msg, matcher=continuous_router),
                           allowed_tools=allowed_tools,
                           user_role=role.value),
                timeout=300.0,
            )
        finally:
            _send_msg.reset(token)

        await _send_response(response, matcher=continuous_router)

    except asyncio.TimeoutError:
        await _safe_send("抱歉，Roxy思考时间超过您的配额时长了。请尝试用更简单的方式提问~", matcher=continuous_router)
    except Exception as e:
        await _safe_send(f"处理消息时出现错误: {str(e)}", matcher=continuous_router)


async def _safe_send(message, max_retries: int = 2, matcher=None):
    """Send a message (str or MessageSegment) with retry on timeout.

    Args:
        message: Message text (str) or MessageSegment (e.g. image).
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
        except Exception as e:
            from nonebot import logger
            logger.warning(f"Send failed with non-retryable error: {e}")
            return
    # All retries exhausted — log but don't crash
    if last_error:
        from nonebot import logger
        logger.warning(f"Failed to send message after {max_retries} retries: {last_error.info}")


async def _handle_session_command(text: str, user_id: str) -> bool:
    """Handle special session management commands.

    Returns True if the command was handled (should skip agent processing).
    """
    cmd, _, args = text.partition(" ")
    args = args.strip()

    # ── /新会话 [名称] ─────────────────────────────────────────
    if cmd in ("/新会话", "#新会话"):
        try:
            name = args if args else None
            session = _special_sessions.create(user_id, name)
            # Activate the session — create() only persists it, doesn't set active_session
            _special_sessions.switch_to(user_id, session.name)
            if args:
                await _safe_send(
                    f"已创建特殊会话「{session.name}」。\n"
                    f"当前处于特殊会话模式，上下文将持续保存。\n"
                    f"使用 /结束会话 退出，/会话列表 查看所有会话。\n"
                    f"当前特殊会话: {len(_special_sessions.list_sessions(user_id))}/{_max_special_sessions}"
                )
            else:
                _pending_naming[user_id] = True
                await _safe_send(
                    f"已创建特殊会话「{session.name}」（名称待精炼）。\n"
                    f"首次交互后会自动生成更贴切的名称。\n"
                    f"当前处于特殊会话模式，上下文将持续保存。\n"
                    f"当前特殊会话: {len(_special_sessions.list_sessions(user_id))}/{_max_special_sessions}"
                )
        except ValueError as e:
            await _safe_send(str(e))
        return True

    # ── /会话列表 ──────────────────────────────────────────────
    if cmd in ("/会话列表", "#会话列表", "/会话", "#会话"):
        sessions = _special_sessions.list_sessions(user_id)
        if not sessions:
            await _safe_send(
                "你目前没有特殊会话。\n"
                f"使用 /新会话 [名称] 创建一个（最多 {_max_special_sessions} 个）。"
            )
            return True

        active_name = _special_sessions._load_index(user_id).get("active_session")
        lines = ["你的特殊会话:"]
        for s in sessions:
            marker = " ← 当前" if s["name"] == active_name else ""
            created = time.strftime("%m/%d %H:%M", time.localtime(s["created_at"]))
            lines.append(
                f"  {s['name']}{marker}\n"
                f"    创建: {created} | 消息数: {s['total_messages']}"
            )
        lines.append(f"\n共 {len(sessions)}/{_max_special_sessions} 个会话")
        await _safe_send("\n".join(lines))
        return True

    # ── /切换会话 <名称> ───────────────────────────────────────
    if cmd in ("/切换会话", "#切换会话"):
        if not args:
            await _safe_send("用法: /切换会话 <会话名称>")
            return True
        try:
            session = _special_sessions.switch_to(user_id, args)
            await _safe_send(
                f"已切换到特殊会话「{session.name}」"
                f"（{session.total_messages} 条消息）。"
            )
        except ValueError as e:
            await _safe_send(str(e))
        return True

    # ── /重命名会话 <旧名> <新名> ──────────────────────────────
    if cmd in ("/重命名会话", "#重命名会话"):
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            await _safe_send("用法: /重命名会话 <旧名称> <新名称>")
            return True
        try:
            session = _special_sessions.rename(user_id, parts[0], parts[1])
            await _safe_send(f"已将会话「{parts[0]}」重命名为「{session.name}」。")
        except ValueError as e:
            await _safe_send(str(e))
        return True

    # ── /删除会话 <名称> ───────────────────────────────────────
    if cmd in ("/删除会话", "#删除会话"):
        if not args:
            await _safe_send("用法: /删除会话 <会话名称>")
            return True

        # Check for confirmation
        confirm_key = f"确认删除 {args}"
        if text == confirm_key:
            # Check pending confirmation
            pending = _pending_delete_confirm.get(user_id)
            if pending and pending[0] == args:
                if time.time() < pending[1]:
                    try:
                        _special_sessions.delete(user_id, args)
                        _pending_delete_confirm.pop(user_id, None)
                        sessions = _special_sessions.list_sessions(user_id)
                        await _safe_send(
                            f"已删除特殊会话「{args}」。\n"
                            f"当前特殊会话: {len(sessions)}/{_max_special_sessions}"
                        )
                    except ValueError as e:
                        await _safe_send(str(e))
                    return True
                else:
                    _pending_delete_confirm.pop(user_id, None)
                    await _safe_send("确认已超时（60秒），请重新发起 /删除会话。")
                    return True

        # First call — request confirmation
        sessions = _special_sessions.list_sessions(user_id)
        if not any(s["name"] == args for s in sessions):
            await _safe_send(f"会话「{args}」不存在。")
            return True

        _pending_delete_confirm[user_id] = (args, time.time() + 60)
        await _safe_send(
            f"确认删除特殊会话「{args}」？此操作不可撤销。\n"
            f"请回复「确认删除 {args}」来执行（60秒内有效）。"
        )
        return True

    # ── /结束会话 ──────────────────────────────────────────────
    if cmd in ("/结束会话", "#结束会话", "/临时会话", "#临时会话"):
        active = _special_sessions.get_active(user_id)
        if active:
            _special_sessions.end_active(user_id)
            await _safe_send(
                f"已退出特殊会话「{active.name}」，回到临时会话模式。\n"
                f"特殊会话内容已保存，随时可以用 /切换会话 {active.name} 恢复。"
            )
        else:
            await _safe_send("当前没有活跃的特殊会话。")
        return True

    # ── /保存为会话 <名称> ─────────────────────────────────────
    if cmd in ("/保存为会话", "#保存为会话"):
        active = _special_sessions.get_active(user_id)
        if active:
            await _safe_send("你已经在特殊会话中。请先 /结束会话 再使用此命令。")
            return True

        temp_session = _session_manager.get(user_id)
        if not temp_session or not temp_session.context:
            await _safe_send("临时会话中没有可保存的上下文。")
            return True

        name = args if args else None
        try:
            session = _special_sessions.create(user_id, name)
            # Activate the session so add_message() calls below actually work
            _special_sessions.switch_to(user_id, session.name)
        except ValueError as e:
            await _safe_send(str(e))
            return True

        # Copy temporary session context to the new special session
        for msg in temp_session.context[-20:]:  # Last 20 messages max
            _special_sessions.add_message(
                user_id,
                msg["role"],
                msg["content"],
                msg.get("reasoning_content"),
            )
        # Force name update (user specified name)
        if name and session.name == name:
            pass  # Already named
        elif not name:
            _pending_naming[user_id] = True

        sessions = _special_sessions.list_sessions(user_id)
        await _safe_send(
            f"已将当前临时会话（最近 {min(len(temp_session.context), 20)} 条消息）"
            f"保存为特殊会话「{session.name}」。\n"
            f"现在处于特殊会话模式，后续对话将持续保存。\n"
            f"当前特殊会话: {len(sessions)}/{_max_special_sessions}"
        )
        return True

    # Not a session command
    return False


async def _handle_feedback(text: str, user_id: str):
    """Record user feedback / bug report to JSONL with context snapshot.

    Commands: #反馈 <content>, #bug <content>, #建议 <content>
    Zero LLM token cost — intercepted before agent processing.
    """
    # Parse command and content
    cmd, _, content = text.partition(" ")
    content = content.strip()

    if cmd == "#反馈" and not content:
        await _safe_send(
            "请按格式提交反馈：\n"
            "#反馈 <你的建议或问题>\n"
            "例如：#反馈 execute_code 超时后临时文件没有清理"
        )
        return
    if cmd == "#bug" and not content:
        await _safe_send(
            "请按格式提交 Bug 报告：\n"
            "#bug <Bug 描述>\n"
            "例如：#bug shell_exec 对大文件处理超时"
        )
        return
    if cmd == "#建议" and not content:
        await _safe_send(
            "请按格式提交改进建议：\n"
            "#建议 <你的建议>\n"
            "例如：#建议 get_user_info 增加工作区目录快照"
        )
        return

    fb_type = {"#反馈": "feedback", "#bug": "bug", "#建议": "suggestion"}[cmd]

    # ── Build context snapshot ──
    role = _perm_manager.get_role(user_id)
    active_special = _special_sessions.get_active(user_id)
    ws_path = _workspace_manager.get_workspace(user_id)
    ws_size = _workspace_manager.get_size(user_id)
    ws_quota_mb = _perm_manager.get_workspace_quota_mb(role)

    ctx = {
        "role": role.value,
        "workspace_path": ws_path,
        "workspace_usage_mb": round(ws_size / (1024 * 1024), 2),
        "workspace_quota_mb": ws_quota_mb,
        "active_special_session": active_special.name if active_special else None,
    }

    entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user_id,
        "type": fb_type,
        "content": content,
        "context": ctx,
    }

    # ── Write to JSONL ──
    feedback_dir = os.path.join(_AGENT_DIR, "data", "feedback")
    os.makedirs(feedback_dir, exist_ok=True)
    feedback_file = os.path.join(feedback_dir, f"feedback_{time.strftime('%Y-%m')}.jsonl")

    try:
        with open(feedback_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        await _safe_send("反馈记录失败，请稍后重试或直接联系管理员。")
        return

    type_label = {"feedback": "反馈", "bug": "Bug 报告", "suggestion": "改进建议"}[fb_type]
    await _safe_send(f"已记录你的{type_label}，感谢！")


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

    # Append disclaimer to every agent response
    disclaimer = "\n\nRoxy 的回答并非总是准确无误，请理性判断。"
    response += disclaimer

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
