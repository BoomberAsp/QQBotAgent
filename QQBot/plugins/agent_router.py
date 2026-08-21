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
    _current_user_id, _current_group_id,
    _current_group_context, _current_personality,
    _on_file_created, _current_quota_bytes,
)
from agent.group_features import get_group_features
from agent.permissions import PermissionManager
from agent.personality import get_personality_manager
from agent.hardware import HardwareDetector
from agent.special_session import SpecialSessionManager
from agent.tool_registry import ToolRegistry
from agent.session import SessionManager
from agent.memory import MemorySystem
from agent.profile import ProfileManager
from agent.workspace import UserWorkspaceManager
from agent.workspace_snapshot import build_tree, rel_to_root, fmt_bytes
from agent.quota_cleanup import (
    list_candidates,
    execute_cleanup,
    format_cleanup_prompt,
    resolve_targets,
)
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
    delete_workspace_file,
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
from tools.character_detail import character_detail, character_detail_with_card
from tools.bond_detail import bond_detail, bond_detail_with_card
from tools.battle_parser import parse_battle_screenshots

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

    # ── Helper: detect file type from magic bytes ─────────────────
    def _magic_ext(data: bytes) -> str:
        if data[:4] == b'%PDF':
            return ".pdf"
        if data[:4] == b'\x89PNG':
            return ".png"
        if data[:3] == b'\xff\xd8\xff':
            return ".jpg"
        if data[:6] in (b'GIF87a', b'GIF89a'):
            return ".gif"
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return ".webp"
        if data[:2] == b'BM':
            return ".bmp"
        return ""

    # ── Strategy 1: direct URL download ──────────────────────────
    if url:
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
            async with httpx.AsyncClient(headers=headers) as client:
                response = await client.get(url, timeout=120.0, follow_redirects=True)
                response.raise_for_status()

                content_length = len(response.content)
                if content_length > max_size_bytes:
                    return None, (
                        f"文件过大 ({content_length / 1024 / 1024:.1f} MB)，"
                        f"超过限制 ({max_size_mb} MB)。请压缩后重试。"
                    )

                # Determine actual extension: Content-Type first, then magic bytes
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
                else:
                    # Content-Type didn't match — try magic bytes
                    magic_ext = _magic_ext(response.content)
                    if magic_ext and not save_path.endswith(magic_ext):
                        save_path = save_path + magic_ext

                with open(save_path, "wb") as f:
                    f.write(response.content)

                return save_path, None

        except httpx.HTTPStatusError as e:
            # QQ CDN URLs may return 400/403 with default headers — fall back to API
            if e.response.status_code in (400, 403) and bot is not None and file_id:
                pass  # Fall through to Strategy 2 below
            else:
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

                    # Fix extension from magic bytes if missing
                    magic_ext = _magic_ext(data)
                    if magic_ext and not save_path.endswith(magic_ext):
                        new_path = save_path + magic_ext
                        os.rename(save_path, new_path)
                        save_path = new_path

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
    registry.register(
        "delete_workspace_file", delete_workspace_file,
        "删除当前用户工作区内的指定文件或空目录，释放磁盘空间。"
        "参数 path 为相对于工作区根目录的路径，如 'uploads/abc.png' 或 'repos/my-repo'。"
        "只能删除空目录；非空目录会拒绝删除。禁止删除隐藏文件。"
        "当用户表达删除工作区文件/仓库的意图时使用此工具，删除前先调用 get_user_info 获取快照确认目标。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于工作区根目录的路径，如 'uploads/abc.png' 或 'repos/my-repo'"},
            },
            "required": ["path"],
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
        "执行游戏抽卡（单抽/十连，四种卡池）。这是唯一能产生真实抽卡结果的工具——绝对禁止编造抽卡结果。"
        "首次抽卡：先询问用户'要先看抽卡动画还是直接看结果？'，得到回复后再调此工具。"
        "用户说「再来一次」「再抽」「继续抽」等：直接调用此工具，使用与上次相同的参数，不再询问动画。",
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
        "动画会直接发送到QQ聊天窗口。应该在给出文字抽卡结果之前调用此工具。"
        "可以通过 interval 参数控制帧间隔（默认 0.75 秒）。",
        {
            "type": "object",
            "properties": {
                "star_level": {"type": "integer", "description": "最高星级。3=蓝色, 4=紫色, 5=金色, 6=红色", "enum": [3, 4, 5, 6]},
                "is_single": {"type": "boolean", "description": "是否为单抽。true=单抽, false=十连", "default": False},
                "interval": {"type": "number", "description": "帧间隔秒数（默认 0.75）", "default": 0.75},
            },
            "required": ["star_level", "is_single"],
        },
    )
    registry.register(
        "calculate_speed", calculate_speed,
        "根据战斗行动值数据计算敌方速度。输入可为「模版文本」或 parse_battle_screenshots 的返回结果。"
        "当用户没有截图、或不愿上传截图时，引导用户按以下模版文本格式粘贴战斗数据：\n"
        "我方\n角色名1 初始行动值 结束行动值 速度\n角色名2 初始行动值 结束行动值 速度\n"
        "（至少一名我方角色需提供速度）\n"
        "敌方\n敌方名1 初始行动值 结束行动值\n敌方名2 初始行动值 结束行动值\n"
        "⚠️ 速度填0表示未知。示例：\n"
        "我方\n兔子 0 100 220\n盖儿 3 56 0\n敌方\n金人司阍 0 88\n丰饶灵兽 0 77",
        {
            "type": "object",
            "properties": {"battle_data": {"type": "string", "description": "战斗数据(含我方/敌方行动值)，模版文本或 parse_battle_screenshots 输出"}},
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
    registry.register(
        "character_detail", character_detail,
        "查询 Ark Re:Code 角色的详细资料（面板成长系数、技能、倍率、属性、天赋、潜能）。"
        "当用户只发送一个角色名/别名（如「夏妮」「狼团长」「Shani」「瞎泥」）而没有其他请求时，"
        "调用此工具返回该角色详情。",
        {
            "type": "object",
            "properties": {
                "character_name": {"type": "string", "description": "角色名或别名"},
            },
            "required": ["character_name"],
        },
    )
    registry.register(
        "bond_detail", bond_detail,
        "查询 Ark Re:Code 羁绊（Bond/神器）的详细资料（类别、星级、攻击/生命、羁绊技能、"
        "获取方式、出售价格、经验值、上线时间）。"
        "当用户只发送一个羁绊名/别名（如「驰骋的快感」「复活甲」「Pleasure of Exploration」）"
        "而没有其他请求时，调用此工具返回该羁绊详情。",
        {
            "type": "object",
            "properties": {
                "bond_name": {"type": "string", "description": "羁绊名或别名"},
            },
            "required": ["bond_name"],
        },
    )
    registry.register(
        "parse_battle_screenshots", parse_battle_screenshots,
        "解析 Ark Re:Code 战斗截图，用 OCR 提取角色名、行动值、阵营（我方/敌方）。"
        "接收 1-2 张截图路径（跑条前 + 跑条后），返回结构化 JSON 和 calculate_speed 兼容格式。"
        "当用户上传/引用战斗截图并要求测速或分析行动值时，调用此工具提取数据。"
        "截图路径会以「[用户引用了文件 ... 文件路径: xxx]」的形式出现在上下文中，"
        "直接取该路径调用，不要改用 read_file（read_file 无法做战斗 OCR）。"
        "团战/镜像匹配时，同一角色可能同时出现在我方和敌方（同名不同阵营），属正常情况，勿视为错误。"
        "本工具专用于 Ark Re:Code，不要根据「战意/爆裂/气魄」等术语误判为其他游戏。",
        {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "截图文件路径列表（1-2 张，跑条前+跑条后）",
                },
            },
            "required": ["paths"],
        },
    )

    # ── Redeem Code ────────────────────────────────────────────

    async def _redeem_code_tool() -> str:
        """Agent-facing tool: query valid redeem codes."""
        from plugins.check_redeem_code import get_redeem_codes, check_and_refresh

        await check_and_refresh()
        codes = get_redeem_codes()

        if not codes:
            return "当前没有有效的兑换码。"

        lines = ["当前有效兑换码:"]
        for entry in codes:
            code = entry.get("code", "")
            content = entry.get("content", "")
            valid = entry.get("valid", "")
            line = f"  {code}"
            if content:
                line += f" — {content}"
            if valid:
                line += f" (有效期至: {valid})"
            lines.append(line)
        return "\n".join(lines)

    registry.register(
        "redeem_code", _redeem_code_tool,
        "查询当前有效的游戏兑换码列表。返回兑换码、奖励内容和有效期。"
        "当用户询问兑换码相关问题时使用此工具。",
        {
            "type": "object",
            "properties": {},
            "required": [],
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

_continuous_sessions = ContinuousSessionManager(timeout_minutes=1.5)

_perm_manager = PermissionManager()

# Per-user busy flag — prevents concurrent message processing for the
# same user. When a user's message is being processed, subsequent
# messages from that user are rejected with a brief "busy" reply.
_user_busy: set = set()

# Recent file tracking — maps message_id to downloaded file info so that
# when a user replies to a file message we can resolve which file they mean.
_MAX_RECENT_FILES = 200
_recent_files: dict[str, list[dict]] = {}

# Temporary-session file provenance — tracks workspace-relative paths uploaded
# while no special session is active, so /保存为会话 can migrate ownership.
_temp_session_files: dict[str, set[str]] = {}

# Idea 3 — suggest upgrading a long temporary session to a special session.
UPGRADE_HINT_THRESHOLD = 15   # message count at which to start suggesting
UPGRADE_HINT_INTERVAL = 10    # re-suggest at most every N messages
_upgrade_hint_last: dict[str, int] = {}  # user_id -> message count at last hint


def _record_file(message_id: str, name: str, path: str = "", error: str = ""):
    """Record a downloaded file (or failed download) against its source
    message for reply resolution."""
    if message_id not in _recent_files:
        _recent_files[message_id] = []
    _recent_files[message_id].append(
        {"name": name, "path": path, "error": error}
    )
    # Prune oldest entries if cache grows too large
    while len(_recent_files) > _MAX_RECENT_FILES:
        oldest = next(iter(_recent_files))
        del _recent_files[oldest]


def _record_session_file(user_id: str, abs_path: str) -> None:
    """Attribute a newly written workspace file to the active special session.

    When no special session is active, the file is recorded into
    ``_temp_session_files`` so ``/保存为会话`` can migrate the provenance
    later. ``abs_path`` is converted to a workspace-relative path; paths
    outside the user's workspace are ignored.
    """
    ws = _workspace_manager.get_workspace(user_id)
    try:
        rel = os.path.relpath(abs_path, ws)
    except ValueError:
        return
    if rel.startswith("..") or os.path.isabs(rel):
        return

    active = _special_sessions.get_active(user_id)
    if active is not None:
        _special_sessions.add_file(user_id, active.name, rel)
    else:
        _temp_session_files.setdefault(user_id, set()).add(rel)


def _format_delete_summary(result: dict) -> str:
    """Render the file-cleanup portion of a session-deletion notification."""
    lines = []
    if result.get("deleted"):
        lines.append(
            f"已清理 {len(result['deleted'])} 个文件"
            f"（释放 {fmt_bytes(result['freed_bytes'])}）："
        )
        lines.extend(f"  - {rel}" for rel in result["deleted"])
    if result.get("kept_repos"):
        lines.append("以下仓库予以保留（工作区内仍可用）：")
        lines.extend(f"  - {rel}" for rel in result["kept_repos"])
    return "\n".join(lines)


def _quota_warning(user_id: str) -> str:
    """Return a quota warning if workspace usage >= 80% of the role quota.

    Uses the same usage metric (``_workspace_manager.get_size``) and per-role
    quota (``_perm_manager.get_workspace_quota_mb``) as ``_get_user_info`` and
    ``_handle_cleanup_flow``. Returns "" below the 80% threshold.
    """
    role = _perm_manager.get_role(user_id)
    quota_mb = _perm_manager.get_workspace_quota_mb(role)
    quota_bytes = quota_mb * 1024 * 1024
    used = _workspace_manager.get_size(user_id)
    if used >= quota_bytes * 0.8:
        pct = used / quota_bytes * 100
        used_mb = used / (1024 * 1024)
        return (
            f"⚠️ 工作区容量已使用 {pct:.0f}%（{used_mb:.1f} MB / {quota_mb} MB）。"
            f"建议发送 /管理工作区 查看详情并清理不需要的文件，"
            f"或直接告诉我「帮我清理工作区」。"
        )
    return ""


# ── Quota Cleanup Protocol (Feature 2) ───────────────────────────
# Elastic-quota cleanup: when a user exceeds their workspace quota, the next
# message triggers a cleanup protocol. The agent lists the earliest-mtime
# candidates, the user may customize (but not skip), and after 10 minutes the
# agent deletes autonomously without approval.

CLEANUP_TIMEOUT = 600  # seconds (10 minutes)

# user_id -> {"candidates": list[CleanupCandidate], "deadline": float}
_cleanup_plans: dict = {}


async def _handle_cleanup_flow(user_id: str, text_content: str) -> bool:
    """Intercept messages during the quota-cleanup protocol.

    Returns True when the message was fully consumed (caller should return
    without running the agent).
    """
    plan = _cleanup_plans.get(user_id)
    if plan is not None:
        if time.time() >= plan["deadline"]:
            await _execute_cleanup_plan(user_id, plan, plan["candidates"], 0.8)
        else:
            await _handle_cleanup_response(user_id, text_content, plan)
        return True

    # No active plan — trigger if over quota.
    quota_mb = _perm_manager.get_workspace_quota_mb(_perm_manager.get_role(user_id))
    quota_bytes = quota_mb * 1024 * 1024
    if _workspace_manager.get_size(user_id) < quota_bytes:
        return False

    candidates = list_candidates(_workspace_manager.get_workspace(user_id))
    if not candidates:
        return False

    deadline = time.time() + CLEANUP_TIMEOUT
    _cleanup_plans[user_id] = {"candidates": candidates, "deadline": deadline}
    await _send_cleanup_prompt(user_id, candidates)
    asyncio.create_task(_auto_cleanup_after(user_id, deadline))
    return True


async def _handle_cleanup_response(user_id: str, text_content: str, plan: dict) -> None:
    """Interpret a user message while a cleanup plan is active."""
    decision = resolve_targets(text_content, plan["candidates"])

    if decision.mode == "skip":
        await _safe_send(
            "⚠️ 无法跳过清理。工作区已超出配额，必须清理后才能继续使用。\n"
            "你可以：\n"
            "1. 回复「删吧」确认删除上列文件\n"
            "2. 回复「保留 X」指定要保留的文件（其余删除）\n"
            "3. 指定要删除的文件路径或编号\n"
            "⏳ 10 分钟内未回复将自动删除上列候选文件。"
        )
        return

    if decision.mode == "confirm":
        await _execute_cleanup_plan(user_id, plan, plan["candidates"], 0.8)
        return

    if decision.mode in ("keep", "explicit"):
        # Explicit user choice — delete exactly the selected targets.
        await _execute_cleanup_plan(user_id, plan, decision.targets, 0.0)
        return

    await _safe_send(
        "请明确你的清理指令：\n"
        "1. 回复「删吧」删除上列所有文件\n"
        "2. 回复「保留 X」保留指定文件\n"
        "3. 回复要删除的文件路径或编号\n"
        "⏳ 10 分钟内未回复将自动删除上列候选文件。"
    )


async def _send_cleanup_prompt(user_id: str, candidates) -> None:
    quota_mb = _perm_manager.get_workspace_quota_mb(_perm_manager.get_role(user_id))
    usage_mb = _workspace_manager.get_size(user_id) / (1024 * 1024)
    await _safe_send(format_cleanup_prompt(candidates, usage_mb, quota_mb))


async def _execute_cleanup_plan(user_id: str, plan: dict, targets, target_ratio: float) -> None:
    """Delete the given candidates (mtime asc) and report the result."""
    _cleanup_plans.pop(user_id, None)
    quota_mb = _perm_manager.get_workspace_quota_mb(_perm_manager.get_role(user_id))
    quota_bytes = quota_mb * 1024 * 1024
    ws = _workspace_manager.get_workspace(user_id)
    result = execute_cleanup(ws, quota_bytes, candidates=targets, target_ratio=target_ratio)
    deleted = result["deleted"]

    if deleted:
        lines = [f"✅ 已清理工作区，删除 {len(deleted)} 项，释放 {fmt_bytes(result['freed_bytes'])}："]
        for c in deleted:
            lines.append(f"  - {c.rel_path}")
        usage_mb = _workspace_manager.get_size(user_id) / (1024 * 1024)
        if usage_mb * 1024 * 1024 < quota_bytes:
            lines.append(f"当前占用 {usage_mb:.1f} MB / {quota_mb} MB，已回到配额内。")
        else:
            lines.append(f"当前占用 {usage_mb:.1f} MB / {quota_mb} MB，仍超出配额，请继续清理。")
        await _safe_send("\n".join(lines))
    else:
        await _safe_send("未删除任何文件。请手动检查工作区，或发送「帮我清理工作区」重新发起。")

    _workspace_manager.clear_quota_flag(user_id)


async def _auto_cleanup_after(user_id: str, deadline: float) -> None:
    """Autonomous deletion fallback after the 10-minute timeout."""
    await asyncio.sleep(CLEANUP_TIMEOUT)
    plan = _cleanup_plans.get(user_id)
    if plan is None or plan["deadline"] != deadline:
        return  # already resolved (or superseded)
    await _execute_cleanup_plan(user_id, plan, plan["candidates"], 0.8)


def _build_reply_context(event: MessageEvent) -> str:
    """Extract reply/quote context from a message's reply segment.

    Returns a string for injection into the augmented message, or "" if
    there is no reply segment.
    """
    # ── Diagnostic: log all message segments ───────────────────────
    import sys
    seg_types = [seg.type for seg in event.message]
    print(f"[REPLY_DIAG] msg_id={event.message_id}  seg_types={seg_types}", file=sys.stderr, flush=True)

    for seg in event.message:
        if seg.type != "reply":
            continue
        reply_id = str(seg.data.get("id", ""))
        reply_text = seg.data.get("text", "") or seg.data.get("message", "") or ""

        parts = []

        # ── Diagnostic: log reply lookup ──────────────────────────
        print(f"[REPLY_DIAG] reply_id={reply_id!r}  recent_keys={list(_recent_files.keys())!r}  match={reply_id in _recent_files}", file=sys.stderr, flush=True)

        if reply_text:
            parts.append(f"[用户引用了消息: \"{reply_text}\"]")
        elif reply_id:
            parts.append(f"[用户回复了消息 {reply_id}]")

        # Check if the replied-to message had files
        if reply_id and reply_id in _recent_files:
            files = _recent_files[reply_id]
            for f in files:
                if f.get("error"):
                    parts.append(
                        f"[用户引用了文件 \"{f['name']}\"，"
                        f"但该文件之前下载失败（{f['error']}）。"
                        f"请告知用户文件无法读取，建议重新上传。]"
                    )
                else:
                    parts.append(
                        f"[用户引用了文件 \"{f['name']}\"。"
                        f"请根据用户意图选择合适的工具处理此文件："
                        f"战斗截图测速/行动值分析用 parse_battle_screenshots，"
                        f"一般图片或文档用 read_file，"
                        f"忽略对话历史中关于其他文件的提及。"
                        f"文件路径: {f['path']}]"
                    )

        return "\n".join(parts) if parts else ""

    # ── Fallback: extract reply id from multiple sources ─────────
    import re as _re

    # 1) Try event.reply attribute (OneBot V11 field)
    event_reply = getattr(event, 'reply', None)
    if event_reply is not None:
        # event.reply can be a dict {'message_id': ..., ...} or a Reply object
        if isinstance(event_reply, dict):
            reply_id = str(event_reply.get('message_id', '') or event_reply.get('id', ''))
        else:
            reply_id = str(getattr(event_reply, 'message_id', '') or getattr(event_reply, 'id', ''))
        if reply_id:
            print(f"[REPLY_DIAG] event.reply: reply_id={reply_id!r}  recent_keys={list(_recent_files.keys())!r}  match={reply_id in _recent_files}", file=sys.stderr, flush=True)
            if reply_id in _recent_files:
                files = _recent_files[reply_id]
                parts = []
                for f in files:
                    parts.append(
                        f"[用户引用了文件 \"{f['name']}\"。"
                        f"请根据用户意图选择合适的工具处理此文件："
                        f"战斗截图测速/行动值分析用 parse_battle_screenshots，"
                        f"一般图片或文档用 read_file，"
                        f"忽略对话历史中关于其他文件的提及。"
                        f"文件路径: {f['path']}]"
                    )
                return "\n".join(parts) if parts else ""
            else:
                return f"[用户回复了消息 {reply_id}]"

    # 2) Parse [CQ:reply,id=XXX] or [reply:id=XXX] from raw message
    raw_msg = getattr(event, 'raw_message', '') or str(event.message)
    m = _re.search(r'\[(?:CQ:)?reply[,:]id=(\d+)\]', raw_msg)
    if m:
        reply_id = m.group(1)
        print(f"[REPLY_DIAG] regex fallback: reply_id={reply_id!r}  recent_keys={list(_recent_files.keys())!r}  match={reply_id in _recent_files}", file=sys.stderr, flush=True)
        if reply_id in _recent_files:
            files = _recent_files[reply_id]
            parts = []
            for f in files:
                parts.append(
                    f"[用户引用了文件 \"{f['name']}\"。"
                    f"请根据用户意图选择合适的工具处理此文件："
                    f"战斗截图测速/行动值分析用 parse_battle_screenshots，"
                    f"一般图片或文档用 read_file，"
                    f"忽略对话历史中关于其他文件的提及。"
                    f"文件路径: {f['path']}]"
                )
            return "\n".join(parts) if parts else ""
        else:
            return f"[用户回复了消息 {reply_id}]"

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
    # Hide the absolute data-root prefix; show paths relative to USER_DATA_ROOT.
    rel_ws = rel_to_root(ws_path, _workspace_manager.user_data_root)
    lines.append(f"")
    lines.append(f"工作区:")
    lines.append(f"  路径: {rel_ws}/")
    lines.append(f"  用量: {ws_usage_mb:.1f} MB / {ws_quota_mb} MB ({pct:.1f}%)")
    if pct >= 80:
        lines.append(f"  ⚠️ 容量警告：工作区已使用 {pct:.1f}%，接近上限。"
                     f"建议清理不需要的文件（如「帮我清理工作区」）。")

    # ── 工作区目录快照 ──
    lines.append(f"")
    lines.append(f"  目录快照:")
    lines.extend(build_tree(Path(ws_path), f"{rel_ws}/", indent="  "))

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
                 "compare_speed_probability", "explain_code", "translate_text",
                 "character_detail", "bond_detail", "parse_battle_screenshots"}
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
    "获取当前用户的系统信息，包括：权限级别、特殊会话列表、工作区用量与目录快照"
    "（含 repos 仓库清单、uploads 文件清单）、可用工具范围、代码执行限制（如有）。"
    "当用户询问「我的设置」「我的权限」「我的工作区」「查看工作区」「工作区里有什么」"
    "「我的会话」「我能用什么工具」或类似用户自身信息相关问题时，应调用此工具。"
    "此工具返回结构化系统数据，可避免 LLM 在系统信息类问题上浪费推理 token。",
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
        _gid = str(event.group_id) if isinstance(event, GroupMessageEvent) else ""
        _pm = get_personality_manager()
        _short = _pm.get_short_name(_pm.resolve_effective_personality(user_id, _gid))
        await _safe_send(f"{_short} 正在处理你的上一条消息，请稍等~")
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
    _on_file_created.set(lambda p: _record_session_file(user_id, p))

    text_content = event.get_plaintext().strip()

    # ── Handle pending delete confirmation (bare text, no / prefix) ──
    pending_delete = _pending_delete_confirm.get(user_id)
    if pending_delete:
        expected = f"确认删除 {pending_delete[0]}"
        if text_content == expected or text_content == f"/{expected}":
            if time.time() < pending_delete[1]:
                try:
                    result = _special_sessions.delete(user_id, pending_delete[0])
                    _pending_delete_confirm.pop(user_id, None)
                    role = _perm_manager.get_role(user_id)
                    max_sess = _perm_manager.get_max_special_sessions(role)
                    sessions = _special_sessions.list_sessions(user_id)
                    summary = _format_delete_summary(result)
                    msg = f"已删除特殊会话「{pending_delete[0]}」。"
                    if summary:
                        msg += "\n" + summary
                    msg += f"\n当前特殊会话: {len(sessions)}/{max_sess}"
                    await _safe_send(msg)
                except ValueError as e:
                    await _safe_send(str(e))
            else:
                _pending_delete_confirm.pop(user_id, None)
                await _safe_send("确认已超时（60秒），请重新发起 /删除会话。")
            return

    # ── Handle feedback / bug report (before agent, zero token cost) ─
    if text_content.startswith(("#反馈", "#bug", "#建议")):
        await _handle_feedback(text_content, user_id)
        return

    # ── Handle session management commands ──────────────────────────
    if text_content.startswith("/") or text_content.startswith("#"):
        # /toggle command (group only, superuser only)
        cmd_handled = await _handle_toggle_command(text_content, user_id, event)
        if cmd_handled:
            return
        # /兑换码 / /redeem-code command (direct, no agent)
        cmd_handled = await _handle_redeem_code_command(text_content, user_id)
        if cmd_handled:
            return
        # /角色详情 command (direct, no agent, zero token)
        cmd_handled = await _handle_character_detail_command(text_content, user_id)
        if cmd_handled:
            return
        # /羁绊详情 command (direct, no agent, zero token)
        cmd_handled = await _handle_bond_detail_command(text_content, user_id)
        if cmd_handled:
            return
        # /功能 / /features command (direct, no agent)
        cmd_handled = await _handle_features_command(text_content, user_id)
        if cmd_handled:
            return
        # /personality / /人格切换 command
        cmd_handled = await _handle_personality_command(text_content, user_id, event)
        if cmd_handled:
            return
        cmd_handled = await _handle_session_command(text_content, user_id)
        if cmd_handled:
            return

    # ── Quota cleanup protocol intercept ───────────────────────────
    # Runs before file download / agent so that, when the user is over quota,
    # new space allocations (uploads, code output, clones) are blocked and the
    # cleanup protocol takes over this turn.
    if await _handle_cleanup_flow(user_id, text_content):
        return

    # ── Detect reply/quote context ─────────────────────────────────
    reply_context = _build_reply_context(event)
    if reply_context:
        import sys; print(f"[REPLY_DIAG] reply_context built ({len(reply_context)} chars): {reply_context[:300]}", file=sys.stderr, flush=True)

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
                _record_session_file(user_id, saved_path)
            elif error:
                file_context_parts.append(f"[用户上传了图片，但下载失败: {error}]")
                _record_file(msg_id, f"image-{file_id}", error=error)

        elif seg.type == "file":
            url = seg.data.get("url", "")
            name = seg.data.get("name") or seg.data.get("filename") or seg.data.get("file_name") or seg.data.get("title") or seg.data.get("file") or "file"
            file_id = seg.data.get("file_id", "")
            saved_path, error = await _download_and_save_file(url, name, bot=bot, file_id=file_id)
            if saved_path:
                file_context_parts.append(f"[用户上传了文件 {name}，已保存至: {saved_path}]")
                _record_file(msg_id, name, saved_path)
                _record_session_file(user_id, saved_path)
            elif error:
                file_context_parts.append(f"[用户上传了文件 {name}，但下载失败: {error}]")
                _record_file(msg_id, name, error=error)

        elif seg.type == "record":
            saved_path, error = await _download_voice(bot, seg.data, str(event.message_id))
            if saved_path:
                file_context_parts.append(
                    f"[用户发送了语音消息，已保存至: {saved_path}]"
                )
                _record_file(msg_id, "语音消息", saved_path)
                _record_session_file(user_id, saved_path)
            elif error:
                file_context_parts.append(f"[用户发送了语音消息，但下载失败: {error}]")
                _record_file(msg_id, "语音消息", error=error)

    # ── Quota warning after any upload ─────────────────────────────
    quota_warn = _quota_warning(user_id)

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
        if quota_warn:
            ack += f"\n\n{quota_warn}"
        await _safe_send(ack)
        return

    # ── Build augmented message ────────────────────────────────────
    if quota_warn:
        file_context_parts.append(
            f"[系统提醒] {quota_warn}\n请在回复末尾原样提醒用户这一容量警告。"
        )
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
            augmented_message = f"{context_prefix}\n用户引用了文件/语音消息，请根据用户意图选择合适的工具查看内容。"
    else:
        augmented_message = text_content

    # Guard: nothing to process
    if not augmented_message:
        return

    # Handle special commands that bypass the agent
    if augmented_message in ["/clear", "清除上下文", "新对话", "/status", "/帮助", "#帮助", "/help", "#help"]:
        await _handle_special_command(augmented_message, user_id)
        return

    # Send thinking indicator (non-critical, ignore send failures)
    try:
        _gid = str(event.group_id) if isinstance(event, GroupMessageEvent) else ""
        _pm = get_personality_manager()
        _short = _pm.get_short_name(_pm.resolve_effective_personality(user_id, _gid))
        await _safe_send(f"{_short} 正在思考...")
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

        # Resolve group feature restrictions
        group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else ""
        if group_id:
            gf = get_group_features()
            gf.refresh()
            disabled_tools = gf.get_disabled_tools(group_id)
            if disabled_tools:
                allowed_tools = allowed_tools - disabled_tools
            _current_group_id.set(group_id)
            _current_group_context.set(gf.get_disabled_context(group_id))
            # Prepend restriction notice to user message so the LLM
            # can't miss it (system prompt alone isn't always enough)
            disabled_features = gf.get_disabled_features(group_id)
            if disabled_features:
                names = "、".join(disabled_features.values())
                augmented_message = (
                    f"[系统提示] 当前群聊的以下功能已被机器人超级用户关闭：{names}。"
                    f"如果用户请求这些功能，请直接告知已被限制，不要尝试调用工具或提供替代方案。\n\n"
                    f"{augmented_message}"
                )
        else:
            _current_group_id.set("")
            _current_group_context.set("")

        # Set permission contextvars for downstream tools
        _current_user_id.set(user_id)
        _current_user_role.set(role.value)
        _current_quota_bytes.set(
            _perm_manager.get_workspace_quota_mb(role) * 1024 * 1024
        )
        if code_limits:
            _current_code_limits.set(code_limits.to_dict())

        # Set personality contextvar
        pm = get_personality_manager()
        _current_personality.set(pm.resolve_effective_personality(user_id, group_id))

        # Idea 3 — suggest upgrading a long temporary session to a special session.
        if session_type == "temporary":
            n = _session_manager.message_count(user_id)
            last = _upgrade_hint_last.get(user_id, -UPGRADE_HINT_INTERVAL)
            if n >= UPGRADE_HINT_THRESHOLD and (n - last) >= UPGRADE_HINT_INTERVAL:
                _upgrade_hint_last[user_id] = n
                augmented_message = (
                    f"[系统提示] 当前临时会话已累积 {n} 条消息，接近 20 条上限，"
                    f"超出后早期上下文会丢失。若用户当前话题明确且可能继续，"
                    f"请在回复末尾用一句话自然建议其发送「/保存为会话 <名称>」"
                    f"持久化这段对话。\n\n{augmented_message}"
                )

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
        _pm = get_personality_manager()
        _short = _pm.get_short_name(_pm.resolve_effective_personality(user_id, group_id))
        await _safe_send(f"{_short} 正在处理你的上一条消息，请稍等~", matcher=continuous_router)
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

    # ── Handle pending delete confirmation (intercept before agent) ──
    pending_delete = _pending_delete_confirm.get(user_id)
    if pending_delete:
        expected = f"确认删除 {pending_delete[0]}"
        if text_content == expected or text_content == f"/{expected}":
            if time.time() < pending_delete[1]:
                try:
                    result = _special_sessions.delete(user_id, pending_delete[0])
                    _pending_delete_confirm.pop(user_id, None)
                    role = _perm_manager.get_role(user_id)
                    max_sess = _perm_manager.get_max_special_sessions(role)
                    sessions = _special_sessions.list_sessions(user_id)
                    summary = _format_delete_summary(result)
                    msg = f"已删除特殊会话「{pending_delete[0]}」。"
                    if summary:
                        msg += "\n" + summary
                    msg += f"\n当前特殊会话: {len(sessions)}/{max_sess}"
                    await _safe_send(msg, matcher=continuous_router)
                except ValueError as e:
                    await _safe_send(str(e), matcher=continuous_router)
            else:
                _pending_delete_confirm.pop(user_id, None)
                await _safe_send(
                    "确认已超时（60秒），请重新发起 /删除会话。",
                    matcher=continuous_router
                )
            return

    # ── Detect reply/quote context and file attachments ────────────
    # These run BEFORE the text guard so files are always saved and
    # recorded, even for file-only messages that may be replied to later.
    reply_context = _build_reply_context(event)
    if reply_context:
        print(f"[REPLY_DIAG] continuous reply_context built ({len(reply_context)} chars): {reply_context[:300]}")

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
                _record_session_file(user_id, saved_path)
            elif error:
                file_context_parts.append(f"[用户上传了图片，但下载失败: {error}]")
                _record_file(msg_id, f"image-{file_id}", error=error)

        elif seg.type == "file":
            url = seg.data.get("url", "")
            name = seg.data.get("name") or seg.data.get("filename") or seg.data.get("file_name") or seg.data.get("title") or seg.data.get("file") or "file"
            file_id = seg.data.get("file_id", "")
            saved_path, error = await _download_and_save_file(url, name, bot=bot, file_id=file_id)
            if saved_path:
                file_context_parts.append(f"[用户上传了文件 {name}，已保存至: {saved_path}]")
                _record_file(msg_id, name, saved_path)
                _record_session_file(user_id, saved_path)
            elif error:
                file_context_parts.append(f"[用户上传了文件 {name}，但下载失败: {error}]")
                _record_file(msg_id, name, error=error)

        elif seg.type == "record":
            saved_path, error = await _download_voice(bot, seg.data, str(event.message_id))
            if saved_path:
                file_context_parts.append(
                    f"[用户发送了语音消息，已保存至: {saved_path}]"
                )
                _record_file(msg_id, "语音消息", saved_path)
                _record_session_file(user_id, saved_path)
            elif error:
                file_context_parts.append(f"[用户发送了语音消息，但下载失败: {error}]")
                _record_file(msg_id, "语音消息", error=error)

    # ── Quota warning after any upload this turn ───────────────────
    if file_context_parts:
        quota_warn = _quota_warning(user_id)
        if quota_warn:
            file_context_parts.append(
                f"[系统提醒] {quota_warn}\n请在回复末尾原样提醒用户这一容量警告。"
            )

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
            _current_quota_bytes.set(
                _perm_manager.get_workspace_quota_mb(role) * 1024 * 1024
            )
            _on_file_created.set(lambda p: _record_session_file(user_id, p))
            if code_limits:
                _current_code_limits.set(code_limits.to_dict())

            # Set personality contextvar
            pm = get_personality_manager()
            _current_personality.set(pm.resolve_effective_personality(user_id, group_id))

            # Resolve group feature restrictions
            gf = get_group_features()
            gf.refresh()
            disabled_tools = gf.get_disabled_tools(group_id)
            if disabled_tools:
                allowed_tools = allowed_tools - disabled_tools
            _current_group_id.set(group_id)
            _current_group_context.set(gf.get_disabled_context(group_id))
            # Prepend restriction notice to user message
            disabled_features = gf.get_disabled_features(group_id)
            if disabled_features:
                names = "、".join(disabled_features.values())
                augmented_message = (
                    f"[系统提示] 当前群聊的以下功能已被机器人超级用户关闭：{names}。"
                    f"如果用户请求这些功能，请直接告知已被限制，不要尝试调用工具或提供替代方案。\n\n"
                    f"{augmented_message}"
                )

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
        _pm = get_personality_manager()
        _short = _pm.get_short_name(_pm.resolve_effective_personality(user_id, group_id))
        await _safe_send(f"抱歉，{_short}思考时间超过您的配额时长了。请尝试用更简单的方式提问~", matcher=continuous_router)
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


async def _handle_redeem_code_command(text: str, user_id: str) -> bool:
    """Handle /兑换码 / /redeem-code command — direct, no agent.

    Returns True if the command was handled.
    """
    cmd = text.strip().split()[0] if text.strip() else ""
    if cmd not in ("/兑换码", "/redeem-code", "#兑换码", "#redeem-code"):
        return False

    from plugins.check_redeem_code import get_redeem_codes, check_and_refresh

    # Trigger background refresh if stale, then use cached data
    refreshed = await check_and_refresh()
    codes = get_redeem_codes()

    if not codes:
        status = " (已是最新)" if refreshed else ""
        await _safe_send(f"现在还没有兑换码哦Σ( ° △ °){status}")
        return True

    lines = ["当前有效兑换码:" if not refreshed else "当前有效兑换码 (已更新):", ""]
    for entry in codes:
        code = entry.get("code", "")
        content = entry.get("content", "")
        valid = entry.get("valid", "")
        line = f"  {code}"
        if content:
            line += f"\n  内容: {content}"
        if valid:
            line += f"\n  有效期至: {valid}"
        lines.append(line)

    await _safe_send("\n".join(lines))
    return True


async def _handle_character_detail_command(text: str, user_id: str) -> bool:
    """Handle /角色详情 command — direct, no agent (zero token).

    Returns True if the command was handled.
    """
    cmd = text.strip().split()[0] if text.strip() else ""
    if cmd not in ("/角色详情", "#角色详情"):
        return False

    # Extract the name argument (everything after the command word)
    rest = text.strip()[len(cmd):].strip()
    if not rest:
        await _safe_send("用法: /角色详情 <角色名或别名>\n例如: /角色详情 夏妮")
        return True

    text_result, card_path = await character_detail_with_card(rest)
    if card_path:
        from pathlib import Path
        from nonebot.adapters.onebot.v11 import MessageSegment
        await _safe_send(MessageSegment.image(Path(card_path)))
    else:
        await _safe_send(text_result)
    return True


async def _handle_bond_detail_command(text: str, user_id: str) -> bool:
    """Handle /羁绊详情 command — direct, no agent (zero token).

    Returns True if the command was handled.
    """
    cmd = text.strip().split()[0] if text.strip() else ""
    if cmd not in ("/羁绊详情", "#羁绊详情"):
        return False

    rest = text.strip()[len(cmd):].strip()
    if not rest:
        await _safe_send("用法: /羁绊详情 <羁绊名或别名>\n例如: /羁绊详情 驰骋的快感")
        return True

    text_result, card_path = await bond_detail_with_card(rest)
    if card_path:
        from pathlib import Path
        from nonebot.adapters.onebot.v11 import MessageSegment
        await _safe_send(MessageSegment.image(Path(card_path)))
    else:
        await _safe_send(text_result)
    return True


async def _handle_personality_command(text: str, user_id: str, event: MessageEvent) -> bool:
    """Handle /personality / /人格切换 command for personality switching.

    Returns True if the command was handled.
    """
    cmd, _, args = text.partition(" ")
    if cmd not in ("/personality", "/人格切换"):
        return False

    pm = get_personality_manager()
    personalities = pm.list_personalities()
    args = args.strip()
    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else ""

    # /personality — show current and available
    if not args:
        current_key = pm.resolve_effective_personality(user_id, group_id)
        current_display = pm.get_display_name(current_key)
        # Annotate where the effective personality comes from
        source = "全局默认"
        if pm.get_personal_personality(user_id) == current_key:
            source = "个人设置"
        elif pm.get_group_personality(group_id) == current_key:
            source = "群默认"
        lines = [f"当前人格: {current_display} ({current_key}，{source})", "", "可用人格:"]
        for p in personalities:
            marker = " ← 当前" if p["key"] == current_key else ""
            lines.append(f"  {p['display_name']} ({p['key']}){marker}")
        lines.append(f"\n使用 /人格切换 <名称> 切换，例如: /人格切换 {personalities[0]['display_name']}")
        await _safe_send("\n".join(lines))
        return True

    # /personality <name> — switch
    try:
        resolved = pm.set_user_personality(user_id, args)
        display = pm.get_display_name(resolved)
        await _safe_send(f"已切换至「{display}」人格。")
    except ValueError as e:
        await _safe_send(str(e))
    except Exception as e:
        from nonebot import logger
        logger.error(f"Personality switch error: {e}")
        await _safe_send(f"人格切换失败，请稍后重试。如果问题持续存在，请使用 #bug 反馈。")

    return True


async def _handle_features_command(text: str, user_id: str) -> bool:
    """Handle /功能 / /features command — direct, no agent.

    Reads FEATURES.md and sends it to the user in segments.
    Returns True if the command was handled.
    """
    cmd = text.strip().split()[0] if text.strip() else ""
    if cmd not in ("/功能", "/features"):
        return False

    features_path = os.path.join(
        os.path.dirname(__file__), "..", "agent", "config", "FEATURES.md"
    )
    try:
        with open(features_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except FileNotFoundError:
        await _safe_send("功能表文件不存在，请联系管理员。")
        return True
    except Exception as e:
        await _safe_send(f"读取功能表失败: {e}")
        return True

    if not content:
        await _safe_send("功能表为空。")
        return True

    # Segment by ## section headers, keeping each section intact
    parts = []
    current = ""
    for line in content.split("\n"):
        if line.startswith("## ") and current:
            parts.append(current.rstrip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current:
        parts.append(current.rstrip())

    # Merge short parts to fit QQ message limits (~400 chars)
    merged = []
    buf = ""
    for part in parts:
        if len(buf) + len(part) < 500:
            buf += "\n\n" + part if buf else part
        else:
            if buf:
                merged.append(buf)
            buf = part
    if buf:
        merged.append(buf)

    for i, chunk in enumerate(merged):
        await _safe_send(chunk)
        if i < len(merged) - 1:
            await asyncio.sleep(1.0)

    return True


async def _handle_toggle_command(text: str, user_id: str, event: MessageEvent) -> bool:
    """Handle /toggle command for group feature management.

    Only works in group chats, only for superusers.
    Returns True if the command was handled.
    """
    cmd, _, args = text.partition(" ")
    if cmd not in ("/toggle", "#toggle"):
        return False

    # Only in group chats
    if not isinstance(event, GroupMessageEvent):
        await _safe_send("此命令仅在群聊中可用。")
        return True

    group_id = str(event.group_id)
    gf = get_group_features()
    gf.refresh()

    # Check superuser
    if not gf.is_superuser(user_id):
        await _safe_send("此命令仅限超级用户（代码开发者）使用。")
        return True

    from agent.group_features import FEATURE_LABELS, FEATURE_ORDER

    args = args.strip().lower()

    # /toggle — show current settings
    if not args:
        features = gf.get_all_features(group_id)
        lines = ["当前群聊功能状态："]
        for key in FEATURE_ORDER:
            label = FEATURE_LABELS[key]
            state = "开启" if features[key] else "关闭"
            lines.append(f"  {label}: {state}")
        pm = get_personality_manager()
        group_key = pm.get_group_personality(group_id)
        if group_key:
            lines.append(f"  默认人格: {pm.get_display_name(group_key)} ({group_key})")
        else:
            lines.append(f"  默认人格: 未绑定（全局默认 {pm.get_display_name(pm.get_default())}）")
        lines.append(f"\n用法: /toggle <功能名> <on|off>  例如: /toggle gacha off")
        lines.append(f"用法: /toggle personality <名称>  设置本群默认人格；/toggle personality 默认 清除")
        await _safe_send("\n".join(lines))
        return True

    # /toggle personality [名称|默认] — group default personality
    if args == "personality" or args.startswith("personality "):
        pm = get_personality_manager()
        rest = args[len("personality"):].strip()
        if not rest:
            group_key = pm.get_group_personality(group_id)
            global_key = pm.get_default()
            if group_key:
                lines = [
                    f"当前本群默认人格: {pm.get_display_name(group_key)} ({group_key})",
                    f"全局默认人格: {pm.get_display_name(global_key)} ({global_key})",
                ]
            else:
                lines = [
                    "当前本群未绑定默认人格（使用全局默认）。",
                    f"全局默认人格: {pm.get_display_name(global_key)} ({global_key})",
                ]
            lines.append("\n用法: /toggle personality <名称> 设置；/toggle personality 默认 清除绑定")
            await _safe_send("\n".join(lines))
            return True
        if rest == "默认":
            pm.clear_group_personality(group_id)
            global_key = pm.get_default()
            await _safe_send(f"已清除本群默认人格，回落到全局默认「{pm.get_display_name(global_key)}」。")
            return True
        try:
            resolved = pm.set_group_personality(group_id, rest)
            await _safe_send(f"已设置本群默认人格为「{pm.get_display_name(resolved)}」。")
        except ValueError as e:
            await _safe_send(str(e))
        return True

    # /toggle <feature> <on|off>
    parts = args.split()
    if len(parts) != 2:
        await _safe_send("用法: /toggle <功能名> <on|off>\n例如: /toggle gacha off\n\n可用的功能名: " + ", ".join(FEATURE_ORDER))
        return True

    feature, action = parts

    if action not in ("on", "off"):
        await _safe_send("第二个参数必须是 on 或 off。\n例如: /toggle gacha off")
        return True

    try:
        enabled = (action == "on")
        gf.set_feature(group_id, feature, enabled)
        label = FEATURE_LABELS.get(feature, feature)
        state = "开启" if enabled else "关闭"
        await _safe_send(f"已在本群{state}「{label}」。")
    except ValueError as e:
        await _safe_send(str(e))

    return True


async def _handle_session_command(text: str, user_id: str) -> bool:
    """Handle special session management commands.

    Returns True if the command was handled (should skip agent processing).
    """
    cmd, _, args = text.partition(" ")
    args = args.strip()

    # ── /新会话 [名称] ─────────────────────────────────────────
    if cmd in ("/新会话", "#新会话"):
        role = _perm_manager.get_role(user_id)
        max_sessions = _perm_manager.get_max_special_sessions(role)
        sessions = _special_sessions.list_sessions(user_id)
        if len(sessions) >= max_sessions:
            names = ", ".join(s["name"] for s in sessions)
            await _safe_send(
                f"已达到最大特殊会话数 ({max_sessions})。"
                f"现有会话: {names}。请先删除一个再创建。"
            )
            return True
        try:
            name = args if args else None
            session = _special_sessions.create(user_id, name)
            # Activate the session — create() only persists it, doesn't set active_session
            _special_sessions.switch_to(user_id, session.name)
            # Starting a fresh special session; drop any pending temp-session file
            # provenance (the user did not choose /保存为会话 to carry it over).
            _temp_session_files.pop(user_id, None)
            quota_warn = _quota_warning(user_id)
            if args:
                msg = (
                    f"已创建特殊会话「{session.name}」。\n"
                    f"当前处于特殊会话模式，上下文将持续保存。\n"
                    f"使用 /结束会话 退出，/会话列表 查看所有会话。\n"
                    f"当前特殊会话: {len(sessions)+1}/{max_sessions}"
                )
            else:
                _pending_naming[user_id] = True
                msg = (
                    f"已创建特殊会话「{session.name}」（名称待精炼）。\n"
                    f"首次交互后会自动生成更贴切的名称。\n"
                    f"当前处于特殊会话模式，上下文将持续保存。\n"
                    f"当前特殊会话: {len(sessions)+1}/{max_sessions}"
                )
            if quota_warn:
                msg += f"\n\n{quota_warn}"
            await _safe_send(msg)
        except ValueError as e:
            await _safe_send(str(e))
        return True

    # ── /会话列表 ──────────────────────────────────────────────
    if cmd in ("/会话列表", "#会话列表", "/会话", "#会话"):
        role = _perm_manager.get_role(user_id)
        max_sessions = _perm_manager.get_max_special_sessions(role)
        sessions = _special_sessions.list_sessions(user_id)
        if not sessions:
            await _safe_send(
                "你目前没有特殊会话。\n"
                f"使用 /新会话 [名称] 创建一个（最多 {max_sessions} 个）。"
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
        lines.append(f"\n共 {len(sessions)}/{max_sessions} 个会话")
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
                        result = _special_sessions.delete(user_id, args)
                        _pending_delete_confirm.pop(user_id, None)
                        role = _perm_manager.get_role(user_id)
                        max_sess = _perm_manager.get_max_special_sessions(role)
                        sessions = _special_sessions.list_sessions(user_id)
                        summary = _format_delete_summary(result)
                        msg = f"已删除特殊会话「{args}」。"
                        if summary:
                            msg += "\n" + summary
                        msg += f"\n当前特殊会话: {len(sessions)}/{max_sess}"
                        await _safe_send(msg)
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

        files = _special_sessions.get_files(user_id, args)
        deletable = [
            f for f in files
            if f != "repos" and not f.startswith("repos" + os.sep)
        ]
        kept_repos = [
            f for f in files
            if f == "repos" or f.startswith("repos" + os.sep)
        ]

        msg = f"确认删除特殊会话「{args}」？此操作不可撤销。\n"
        if deletable:
            msg += f"删除后将清理 {len(deletable)} 个关联文件：\n"
            msg += "\n".join(f"  - {rel}" for rel in deletable) + "\n"
        if kept_repos:
            msg += "以下仓库予以保留（工作区内仍可用）：\n"
            msg += "\n".join(f"  - {rel}" for rel in kept_repos) + "\n"
        msg += f"请回复「确认删除 {args}」来执行（60秒内有效）。"
        await _safe_send(msg)
        return True

    # ── /结束会话 ──────────────────────────────────────────────
    if cmd in ("/结束会话", "#结束会话", "/临时会话", "#临时会话", "/退出特殊会话", "/退出会话"):
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

        role = _perm_manager.get_role(user_id)
        max_sessions = _perm_manager.get_max_special_sessions(role)
        sessions = _special_sessions.list_sessions(user_id)
        if len(sessions) >= max_sessions:
            names = ", ".join(s["name"] for s in sessions)
            await _safe_send(
                f"已达到最大特殊会话数 ({max_sessions})。"
                f"现有会话: {names}。请先删除一个再创建。"
            )
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

        # Migrate temporary-session file provenance to the new session
        for rel in _temp_session_files.pop(user_id, set()):
            _special_sessions.add_file(user_id, session.name, rel)

        # Force name update (user specified name)
        if name and session.name == name:
            pass  # Already named
        elif not name:
            _pending_naming[user_id] = True

        sessions = _special_sessions.list_sessions(user_id)
        role = _perm_manager.get_role(user_id)
        max_sess = _perm_manager.get_max_special_sessions(role)
        quota_warn = _quota_warning(user_id)
        msg = (
            f"已将当前临时会话（最近 {min(len(temp_session.context), 20)} 条消息）"
            f"保存为特殊会话「{session.name}」。\n"
            f"现在处于特殊会话模式，后续对话将持续保存。\n"
            f"当前特殊会话: {len(sessions)}/{max_sess}"
        )
        if quota_warn:
            msg += f"\n\n{quota_warn}"
        await _safe_send(msg)
        return True

    # ── /帮助 — 命令列表 ───────────────────────────────────────
    if cmd in ("/帮助", "#帮助", "/help", "#help", "/命令", "#命令"):
        help_text = (
            "**系统命令列表**\n\n"
            "🟢 **特殊会话管理**\n"
            "/新会话 [名称] — 创建特殊会话\n"
            "/切换会话 <名称> — 切换到已有会话\n"
            "/会话列表 或 /会话 — 查看所有会话\n"
            "/重命名会话 <旧名> <新名> — 重命名会话\n"
            "/删除会话 <名称> — 删除会话（需确认）\n"
            "/结束会话 或 /临时会话 或 /退出特殊会话 — 退出特殊会话\n"
            "/保存为会话 <名称> — 将临时上下文保存为会话\n\n"
            "🟦 **工作区**\n"
            "/管理工作区 — 查看工作区占用并引导清理\n\n"
            "🟣 **人格切换**\n"
            "/人格切换 — 查看当前人格和可用列表\n"
            "/人格切换 <名称> — 切换人格（支持模糊匹配）\n\n"
            "🟠 **游戏工具**\n"
            "/兑换码 或 /redeem-code — 查询有效兑换码\n\n"
            "🔵 **连续对话（群聊）**\n"
            "/取消 或 #取消 — 退出连续对话模式\n\n"
            "🟡 **反馈**\n"
            "#反馈 <内容> — 提交功能建议\n"
            "#bug <内容> — 提交 Bug 报告\n"
            "#建议 <内容> — 提交改进建议\n\n"
            "⚪ **其他**\n"
            "/功能 或 /features — 查看完整功能一览\n"
            "/status — 查看机器人运行状态\n"
            "/clear 或 新对话 — 清除临时上下文"
        )
        await _safe_send(help_text)
        return True

    # ── /管理工作区 ─────────────────────────────────────────────
    # Not intercepted: falls through to the agent, which (per AGENTS.md)
    # calls get_user_info to show the snapshot and guide cleanup.
    if cmd in ("/管理工作区", "#管理工作区"):
        return False

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
        _special_sessions.clear_context(user_id)
        _temp_session_files.pop(user_id, None)
        await _safe_send("已清除对话上下文，开始新对话~")
    elif command == "/status":
        status = agent.get_status()
        tool_list = "\n  ".join(status["tool_names"])
        _short = get_personality_manager().get_short_name(
            get_personality_manager().get_user_personality(user_id)
        )
        await _safe_send(
            f"{_short} 状态:\n"
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
    _pm = get_personality_manager()
    _persona = _current_personality.get() or _pm.get_default()
    _short = _pm.get_short_name(_persona)
    disclaimer = f"\n\n{_short} 的回答并非总是准确无误，请理性判断。"
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
