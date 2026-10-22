"""
Turn logic — the platform-independent core of the QQBot message pipeline.

Stage-1 of the QQ-official worktree plan (feature/qq-official): moved
verbatim out of plugins/agent_router.py so a second entry adapter (QQ
official bot API) can share one kernel.

Stage-2: the inbound side is now platform-agnostic — the entry points
consume agent.inbound.InboundMessage views built by adapters/onebot.py,
which also absorbed _build_reply_context / _download_voice /
_download_and_save_file / _get_uploads_dir (all OneBot/NapCat-specific).

Stage-3: the outbound side is now platform-agnostic — _safe_send()
dispatches on the agent.outbound contract (str → send_text, OutImage →
send_image) and retries only on SendFailed. Each entry adapter supplies
a Sender implementation (adapters/onebot.py::OneBotSender now; stage 4
adds QQOfficialSender, which turns OutImage.path into a base64 upload).

Temporary OneBot couplings still living here (escape hatches):
  - _forced_refresh_wiki_data() grabs a Bot via nonebot.get_bots()

Outbound sending goes through sender slots (default_sender /
continuous_sender / poke_sender) assigned by the entry plugin at
import time — this keeps turn.py free of plugin imports (no cycle).
"""

# ── Load .env into os.environ BEFORE any module-level reads ──────
# Same bootstrap as plugins/agent_router.py (load_dotenv is idempotent).
# Must stay at the very top: the singletons below read USER_DATA_ROOT,
# quotas and session limits from os.environ at import time.
from pathlib import Path
from dotenv import load_dotenv as _load_dotenv
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_load_dotenv(_ENV_FILE)

import asyncio
import json
import os
import random
import re
import sys
import time
import uuid

import httpx

from agent.agent import Agent
from agent.inbound import InboundMessage
from agent.outbound import OutImage, OutProgress, SendFailed
from agent.continuous_session import ContinuousSessionManager
from agent.context import (
    _current_sender, _turn_sender, _current_platform,
    _current_user_workspace,
    _current_user_role, _current_code_limits,
    _current_user_id, _current_group_id,
    _current_group_context, _current_personality,
    _personality_transition, _on_file_created, _current_quota_bytes,
    _pending_task_fold, _current_bond_context, _delegate_depth,
)
from agent.bond import BondManager
from agent import bond_content
from agent.delegation import (
    SOCIAL_TOOLS, run_delegated_task, revoice_task_result, repatch_last_assistant,
    _LLM_ERROR_PREFIXES,
)
from agent.task_record import build_record, build_compact_line, append_task_log
from agent.search_archive import get_default_archive
from agent.group_features import get_group_features
from agent.permissions import PermissionManager, UserRole
from agent.character_registry import get_character_registry
from agent.hardware import HardwareDetector
from agent.special_session import SpecialSessionManager
from agent.tool_registry import ToolRegistry
from agent.session import SessionManager
from agent.memory import MemorySystem, TieredMemory
from agent.profile import ProfileManager
from agent.workspace import UserWorkspaceManager
from agent.workspace_snapshot import build_tree, rel_to_root, fmt_bytes
from agent.quota_cleanup import (
    list_candidates,
    execute_cleanup,
    format_cleanup_prompt,
    resolve_targets,
)
from lib.errors import log_exception
from lib.model_router import ModelRouter
from lib.token_ledger import set_usage_context
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
from tools.card_renderer import (
    render_help_card, render_feature_card, render_changelog_card, render_scene_card,
)
from tools.creation_tools import create_character as _create_character_impl
from tools.social_tools import (
    give_gift as _give_gift_impl,
    complete_scene as _complete_scene_impl,
    COMPLETE_SCENE_SCHEMA,
)

# ── Sender slots (assigned by the entry plugin at import time) ─────
# _safe_send() and the continuous/poke paths send through
# agent.outbound.Sender implementations; the entry plugin wraps its
# platform matchers in them (adapters/onebot.py::OneBotSender now).
# The plugin assigns these right after creating its matchers; keeping
# them as module vars avoids a turn -> plugin circular import.
default_sender = None       # agent_router      (priority=1 on_message)
continuous_sender = None    # continuous_router (priority=2 on_message)
poke_sender = None          # poke_router       (on_notice)


class _ToolSender:
    """Bridge exposed to tools via the _current_sender contextvar.

    Tools expect an agent.outbound.Sender, but pre-stage-3 they sent
    through _safe_send (retry + backoff + non-retryable-error logging).
    Routing tool sends back through _safe_send keeps that behavior
    identical; the platform Sender in the slot does the actual
    translation. sender=None → _safe_send falls back to default_sender.
    """

    platform = "kernel"
    max_messages_per_turn = 100

    def __init__(self, sender=None):
        self._sender = sender

    async def send_text(self, text: str) -> None:
        await _safe_send(text, sender=self._sender)

    async def send_image(self, img: OutImage) -> None:
        await _safe_send(img, sender=self._sender)

    async def send_markdown(self, text: str) -> None:
        await _safe_send(text, sender=self._sender)

    async def send_progress(self, text: str, *, immediate: bool = False) -> None:
        await _safe_send(OutProgress(text, immediate=immediate),
                          sender=self._sender)


# ── Configuration Paths ───────────────────────────────────────────

_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_DIR = os.path.join(_AGENT_DIR, "agent", "config")
_DATA_DIR = os.path.join(_AGENT_DIR, "data")
_USER_DATA_ROOT = os.environ.get("USER_DATA_ROOT", os.path.join(_AGENT_DIR, "data", "users_store"))
_HELP_MD_PATH = os.path.join(_CONFIG_DIR, "HELP.md")
# 公开使用站点（bot.oneweblog.cn）短链，随更新公告文本/卡片指路。可经 .env 覆盖。
_PUBLIC_SITE_URL = os.environ.get("BOT_PUBLIC_SITE_URL", "https://bot.oneweblog.cn")


# ── File logging (launcher-independent) ──────────────────────────
# Production runs the bot via `cd QQBot && nb run`, which never executes
# bot.py, so the loguru file sink must be installed here (this plugin is
# always imported under nb run). Mirrors all loguru output — including
# NoneBot's own logger — to QQBot/logs/bot_YYYY-MM-DD.log so the WebUI
# panel's log viewer has a stable file to read regardless of how the bot
# was launched. Idempotent: added once per process, never raises.
def _setup_file_logging():
    try:
        from loguru import logger as _logger
        log_dir = os.path.join(_AGENT_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        _logger.add(
            os.path.join(log_dir, "bot_{time:YYYY-MM-DD}.log"),
            rotation="10 MB",
            retention="7 days",
            encoding="utf-8",
            enqueue=True,
            level="INFO",
        )
    except Exception:
        pass  # 日志文件化失败绝不影响机器人运行


_setup_file_logging()


# ── Workspace Initialization ─────────────────────────────────────

def _init_workspace():
    """Create workspace directories if they don't exist."""
    from tools.builtin_tools import _ensure_workspace_dirs
    _ensure_workspace_dirs()

_init_workspace()


# ── File Download Helpers ──────────────────────────────────────────
# Stage 2: _get_uploads_dir / _download_voice / _download_and_save_file
# moved to adapters/onebot.py (OneBot/NapCat-specific by nature). The
# turn kernel consumes them through view.load_attachments().



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
        "文本和PDF返回文字内容，图片返回基础信息+AI分析，音频返回元数据+AI转录和情绪分析（需配置音频模型）。"
        "大文本文件单次最多返回 50K 字符，截断时会提示下一段的 offset，可分段续读。",
        {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件路径。用户上传的文件会自动保存到 data/workspace/uploads/ 目录。",
                },
                "offset": {
                    "type": "integer",
                    "description": "（可选）分段读取的字符偏移量，仅对文本文件生效。上次读取被截断时，按截断提示传入对应 offset 继续读下一段；不传默认从头读。",
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
        "首次抽卡：先调用 begin_task 记录任务，再询问用户'要先看抽卡动画还是直接看结果？'，"
        "得到回复后调此工具，给出结果后务必调用 finalize_subtask 收尾。"
        "用户说「再来一次」「再抽」「继续抽」等：直接调用此工具（用与上次相同的参数，不再询问动画），"
        "给出结果后同样调用 finalize_subtask 收尾。",
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
        "支持两种模式（mode 参数）：light=轻量（仅提取角色名与行动值，约10秒，跳过技能解析与行动值修正）；"
        "full=全量（完整流程：技能解析+行动值修正，约90秒）。默认 full。"
        "当用户上传/引用战斗截图并要求测速或分析行动值时，先调用 begin_task 记录任务，"
        "再告知用户两种模式的区别（流程与预计用时），按用户选择传入 mode 调用，不要擅自替用户决定。"
        "得出速度结果（通常经 calculate_speed）后，务必调用 finalize_subtask 收尾。"
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
                "mode": {
                    "type": "string",
                    "enum": ["light", "full"],
                    "description": "解析模式。light=轻量（仅提取角色名与行动值，约10秒）；full=全量（完整流程：技能解析+行动值修正，约90秒）。默认 full",
                    "default": "full",
                },
            },
            "required": ["paths"],
        },
    )

    # ── Redeem Code ────────────────────────────────────────────

    async def _redeem_code_tool(scope: str = "time_limited") -> str:
        """Agent-facing tool: query redeem codes by scope."""
        from plugins.check_redeem_code import (
            get_time_limited_codes, get_long_term_codes, check_and_refresh,
            get_cache_info, format_expiry_display, _write_admin_alert,
            is_refresh_in_flight,
        )

        await check_and_refresh()

        # Cap list size to protect the LLM context window
        MAX_TOOL_CODES = 30
        tl = get_time_limited_codes() if scope in ("time_limited", "all") else []
        lt = get_long_term_codes() if scope in ("long_term", "all") else []
        active = [c for c in tl if not c.get("recently_expired")]
        recent_expired = [c for c in tl if c.get("recently_expired")]

        if scope == "time_limited" and not tl:
            # A browser-engine refresh may still be running in the background
            # (check_and_refresh returns immediately). Don't report failure or
            # raise an alert for a scrape that hasn't finished yet.
            if is_refresh_in_flight():
                return ("兑换码正在后台更新中，请稍等片刻再试。"
                        "如需长期兑换码，请以 scope=long_term 重新调用。")
            # No time-limited codes AND scraper failing → ping the admin
            # via the WebUI panel alert banner.
            if get_cache_info().get("stale"):
                _write_admin_alert("no_valid_codes")
                return ("当前兑换码均已过期，网页爬取失败，等待管理员添加。"
                        "如需长期兑换码，请以 scope=long_term 重新调用。")
            return ("当前没有有效的限时兑换码（7 天内也没有刚过期的）。"
                    "如需长期兑换码，请以 scope=long_term 重新调用。")
        if scope == "long_term" and not lt:
            return "还没有收录长期有效的兑换码。"

        lines = []
        if scope in ("time_limited", "all"):
            shown_active = active[:MAX_TOOL_CODES]
            lines.append(f"【限时兑换码】当前有效 {len(active)} 条:")
            for entry in shown_active:
                line = f"  {entry.get('code', '')}"
                if entry.get("content"):
                    line += f" — 兑换内容: {entry['content']}"
                if entry.get("valid"):
                    line += f" (有效期至: {format_expiry_display(entry['valid'])})"
                else:
                    line += " (过期时间未知，尽快兑换)"
                lines.append(line)
            if len(active) > MAX_TOOL_CODES:
                lines.append(f"  …另有 {len(active) - MAX_TOOL_CODES} 条未列出")
            if recent_expired:
                lines.append(f"  （另有 {len(recent_expired)} 条限时码已于 7 天内过期："
                             + "、".join(e.get("code", "") for e in recent_expired[:10])
                             + ("…" if len(recent_expired) > 10 else "") + "）")
        if scope in ("long_term", "all"):
            lines.append(f"【长期兑换码】无过期时间，共 {len(lt)} 条:")
            for entry in lt[:MAX_TOOL_CODES]:
                line = f"  {entry.get('code', '')}"
                if entry.get("content"):
                    line += f" — 兑换内容: {entry['content']}"
                lines.append(line)
            if len(lt) > MAX_TOOL_CODES:
                lines.append(f"  …另有 {len(lt) - MAX_TOOL_CODES} 条未列出")

        # Send each active time-limited code as a standalone message so users
        # can copy it directly. Skipped for long_term/all — those lists can be
        # huge; the user gets them as copyable list text instead.
        if scope == "time_limited" and active[:MAX_TOOL_CODES]:
            try:
                from agent.context import _current_sender
                sender = _current_sender.get()
                if sender is not None:
                    await sender.send_text("以下逐条发送兑换码，方便复制：")
                    for entry in active[:MAX_TOOL_CODES]:
                        await asyncio.sleep(1.0)  # QQ rate limiting
                        await sender.send_text(entry["code"])
                    lines.append("（限时兑换码已逐条单独发送给用户，方便复制；"
                                 "你的回复中不必再逐字罗列它们）")
            except Exception as e:
                import sys
                print(f"[redeem_code] per-code send failed: {type(e).__name__}: {e}",
                      file=sys.stderr)

        return "\n".join(lines)

    registry.register(
        "redeem_code", _redeem_code_tool,
        "查询游戏兑换码。默认返回当前有效的限时兑换码（含兑换内容与有效期）"
        "及 7 天内刚过期的限时码；scope=long_term 返回长期有效（无过期时间）"
        "的兑换码；scope=all 返回两类。当用户询问兑换码相关问题时使用此工具，"
        "用户明确提到「长期兑换码」「永久码」时用 scope=long_term。",
        {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["time_limited", "long_term", "all"],
                    "description": "time_limited=限时兑换码（默认）；long_term=长期兑换码；all=全部",
                    "default": "time_limited",
                },
            },
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

    # ── Changelog（更新记录）──────────────────────────────────────
    async def _get_changelog_tool(count: int = 3) -> str:
        """Agent-facing tool: 返回机器人自身最近 count 条更新记录（纯文本）。"""
        from tools.changelog import get_recent, format_for_qq
        entries = get_recent(count)
        return "\n".join(format_for_qq(entries, requested=count))

    registry.register(
        "get_changelog", _get_changelog_tool,
        "查询机器人自身的更新记录/更新日志/版本变更。当用户询问"
        "「最近有什么更新」「更新日志」「有哪些新功能」「这版更新了什么」"
        "「机器人最近升级了什么」等关于本机器人版本变更的问题时使用此工具。"
        "注意：这是机器人自己的更新记录，不是游戏或第三方软件的更新。",
        {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "返回最近多少条更新记录，默认3，最多20",
                    "default": 3,
                },
            },
            "required": [],
        },
    )

    # ── 双平面路由: 角色面兜底委托工具（Bond-System-Design.md §3.3）──
    # 只在角色面（split 人格 + social 平面）的 allowed_tools 中出现；
    # 任务面/统一人格的 allowed_tools 不含它 —— schema 过滤 + agent 内
    # _execute_tool_calls 硬拒绝 + delegation 内 _delegate_depth 守卫，
    # 三层防线确保单层嵌套、任务面无法再委派。
    async def _delegate_task_tool(task: str) -> str:
        user_id = _current_user_id.get()
        group_id = _current_group_id.get()
        disabled: set = set()
        if group_id:
            try:
                gf = get_group_features()
                gf.refresh()
                disabled = gf.get_disabled_tools(group_id)
            except Exception:
                disabled = set()

        async def _inner_progress(msg: str):
            sender = _current_sender.get()
            if sender is not None:
                await sender.send_progress(msg)

        return await run_delegated_task(
            task,
            agent=agent,
            model_router=_model_router,
            perm_manager=_perm_manager,
            user_id=user_id,
            group_id=group_id,
            disabled_tools=disabled,
            progress_callback=_inner_progress,
        )

    registry.register(
        "delegate_task", _delegate_task_tool,
        "把需要工具能力的任务（搜索实时信息、抓取网页、执行代码、读写文件、"
        "查询数据、翻译等）委托给任务智能体执行，并返回其结果。"
        "凡用户请求涉及实时信息、计算、代码、文件操作，必须调用此工具获取结果，"
        "再以你自己的口吻转达给用户；绝不能凭记忆编造此类内容。"
        "注意：任务智能体看不到你们的对话历史，task 描述必须自包含全部必要上下文"
        "（用户原话、相关文件路径、前情摘要等）。",
        {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "交给任务智能体的完整任务描述，必须自包含所有必要上下文",
                },
            },
            "required": ["task"],
        },
    )

    # ── UGC 角色创作工具（Bond-System-Design.md §5.1, P1）──
    # 仅在「创建角色」访谈特殊会话（metadata.kind == char_creation）中，由路由
    # 层通过 allowed_tools 下发；其它场景 schema 过滤掉、_execute_tool_calls 硬
    # 拒绝。user_id / registry / 审查用 flash client 全部在调用时经模块全局解析。
    async def _create_character_tool(
        name: str,
        personality: str,
        speech_style: str,
        appearance: str,
        gender: str = "",
        worldview: str = "",
        relationship: str = "",
        greeting: str = "",
        gift_loved: list = None,
        gift_normal: list = None,
        gift_disliked: list = None,
        bond_levels: str = "",
    ) -> str:
        fields = {
            "name": name, "personality": personality,
            "speech_style": speech_style, "appearance": appearance,
            "gender": gender, "worldview": worldview,
            "relationship": relationship, "greeting": greeting,
            "gift_loved": gift_loved or [], "gift_normal": gift_normal or [],
            "gift_disliked": gift_disliked or [], "bond_levels": bond_levels,
        }
        uid = _current_user_id.get()
        result = await _create_character_impl(
            user_id=uid,
            fields=fields,
            registry=get_character_registry(),
            perm_manager=_perm_manager,
            review_client=_model_router.flash_client,
        )
        # Flag success so the handler auto-closes the interview session post-run.
        if uid and not result.startswith("[Error]"):
            _pending_char_creation_done[uid] = True
        return result

    registry.register(
        "create_character", _create_character_tool,
        "创建用户自创角色（UGC 人格）。仅在角色创作访谈会话中、收集齐角色设定后调用一次，"
        "把访谈得到的各字段原样填入。工具会做安全审查与配额校验，成功后角色即可用 "
        "/人格切换 选用。必填：name（角色名）、personality（性格）、speech_style（说话风格与示例台词）、"
        "appearance（外观锚点）；其余字段用户未提供可留空或省略。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "角色名（≤24字符）"},
                "gender": {"type": "string", "description": "性别（可选）"},
                "personality": {"type": "string", "description": "性格关键词与描述（必填）"},
                "worldview": {"type": "string", "description": "世界观背景（可选，默认现代日常）"},
                "relationship": {"type": "string", "description": "角色与用户的关系设定（可选）"},
                "greeting": {"type": "string", "description": "对用户的初始称呼/开场白（可选）"},
                "speech_style": {"type": "string", "description": "说话风格描述与示例台词（必填）"},
                "appearance": {"type": "string", "description": "外观锚点：发型/发色/瞳色/服饰等固定特征（必填）"},
                "gift_loved": {"type": "array", "items": {"type": "string"}, "description": "喜爱的礼物（≤10项，可选）"},
                "gift_normal": {"type": "array", "items": {"type": "string"}, "description": "普通的礼物（可选）"},
                "gift_disliked": {"type": "array", "items": {"type": "string"}, "description": "讨厌的礼物（可选）"},
                "bond_levels": {"type": "string", "description": "各羁绊等级的态度变化/解锁档案描述（可选）"},
            },
            "required": ["name", "personality", "speech_style", "appearance"],
        },
    )

    # ── 角色面社交工具：送礼（Bond-System-Design.md §4.4, P2）──
    # 只在角色面（split + social）allowed_tools 中出现（SOCIAL_TOOLS）。当前
    # 角色 persona_key 从 _current_personality 取；角色卡经 registry.load 取，
    # 用于礼物偏好匹配；好感结算走全局 _bond_manager。
    async def _give_gift_tool(gift_name: str) -> str:
        user_id = _current_user_id.get()
        persona_key = _current_personality.get()
        card = ""
        try:
            card = get_character_registry().load(persona_key) or ""
        except Exception:
            card = ""
        return await _give_gift_impl(
            user_id=user_id,
            gift_name=gift_name,
            char_id=persona_key,
            card_content=card,
            bond_manager=_bond_manager,
        )

    registry.register(
        "give_gift", _give_gift_tool,
        "当用户在对话中送给你礼物（例如「送你一朵花」「给你买了蛋糕」）时调用此工具，"
        "把礼物名填入 gift_name。工具会根据你对该礼物的喜好结算好感与心情，并返回结果，"
        "你再以角色口吻自然回应。仅凭对话中的真实送礼意图调用，不要臆造。",
        {
            "type": "object",
            "properties": {
                "gift_name": {
                    "type": "string",
                    "description": "用户送出的礼物名称（从用户消息里提炼，如「一朵花」「草莓蛋糕」）",
                },
            },
            "required": ["gift_name"],
        },
    )

    # ── 剧情场景收尾（Bond-System-Design.md §4.4 形态3, P3）──
    # 仅经 bond_scene 特殊会话的 allowed_tools 下发（不在 SOCIAL_TOOLS / 全局
    # 工具集）。场景名/角色 id 从当前活跃 bond_scene 会话 metadata 读取（LLM 不
    # 传）；结算（+scene_exp/mark/事件日志）在 social_tools.complete_scene 内完成，
    # 成功后把 result 记进 _pending_bond_scene_done，post-run 钩子据此发升级通知 +
    # 纪念卡片并 end_active。
    async def _complete_scene_tool(memento: str = "") -> str:
        user_id = _current_user_id.get()
        persona_key = _current_personality.get()
        char_id = persona_key
        scene_title = ""
        try:
            active = _special_sessions.get_active(user_id)
            md = (active.metadata or {}) if active else {}
            scene_title = str(md.get("scene_title") or "")
            char_id = str(md.get("char_id") or persona_key)
        except Exception:
            pass
        out = await _complete_scene_impl(
            user_id=user_id,
            char_id=char_id,
            scene_title=scene_title,
            bond_manager=_bond_manager,
            memento=memento,
        )
        result = out.get("result") or {}
        if out.get("ok") and result.get("accepted"):
            _pending_bond_scene_done[user_id] = result
        return out.get("observation", "")

    registry.register(
        "complete_scene", _complete_scene_tool,
        "当你与用户共同经历的剧情场景走到**结局**（脚本最后一幕完成、或你们自然抵达"
        "终点）时调用此工具收尾。可用 memento 参数以你的口吻写一句纪念语（≤40 字）"
        "镌刻在纪念卡片上。调用后请再说一段温柔的收尾白。仅在场景结局调用一次，"
        "场景中途不要调用。",
        COMPLETE_SCENE_SCHEMA,
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

# Model router is built first so downstream components use the JSON-configured
# reasoning model (models_settings.json) with a fallback to the .env DeepSeek
# config — instead of the .env-only global client.
_model_router = ModelRouter()

_profile_manager = ProfileManager(
    base_dir=_USER_DATA_ROOT,
)
# Layer 3 (Plan §7.1/§7.9): profile extraction is a batched, low-stakes
# classification task → use the FLASH model, not the reasoning model.
_profile_manager.set_client(_model_router.flash_client)

# P1 three-tier memory engine (Profile-Fact-Extraction-Plan §7). Shares the
# {data}/memory base dir with the legacy MemorySystem but owns a separate tiers/
# subdir → no collision, trivial rollback (delete tiers/). load_all() pre-warms
# the per-user cache at startup; set_memory wires it into the merged extract+judge
# call so ProfileManager routes memory_candidates into SHORT/MEDIUM/LONG.
_tiered_memory = TieredMemory(base_dir=os.path.join(_DATA_DIR, "memory"))
_tiered_memory.load_all()
_profile_manager.set_memory(_tiered_memory)

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
    llm_client=_model_router.reasoning_client,
)

# Track sessions pending LLM auto-naming: user_id -> True
_pending_naming: dict = {}

# Track pending deletion confirmations: user_id -> (session_name, expiry_timestamp)
_pending_delete_confirm: dict = {}

# Track pending UGC character deletion: user_id -> (char_id, expiry_timestamp)
_pending_char_delete_confirm: dict = {}

# Set by the create_character tool closure on success; the message handler
# pops it after the run to auto-close the interview special session.
_pending_char_creation_done: dict = {}

# Set by the complete_scene tool closure on success (value = settlement result
# dict: level_ups/exp/scene_title/char_id/memento); the post-run hook pops it to
# send the level-up notice + commemorative card and end_active the scene session.
_pending_bond_scene_done: dict = {}

# Track pending personality-switch choice: user_id -> (old_display, new_display)
# Set by /人格切换, resolved on the user's next message: "清空" clears context,
# anything else keeps history and injects a one-line transition notice.
_pending_personality_switch: dict = {}

# Persistent transition notice per user (injected until context is cleared or
# personality changes again): user_id -> notice string.
# Persisted to data/personality_markers.json — the notice MUST survive process
# restarts (panel restart / watchdog). Prod incident 2026-10-22: a restart
# between the switch and the user's next message wiped this dict, so the L4
# transition note never reached the model and the persona-styled history of a
# special session dragged the reply back into the OLD personality's voice.
_MARKERS_FILE = os.path.join(_DATA_DIR, "personality_markers.json")


def _load_personality_markers() -> dict:
    try:
        with open(_MARKERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: v for k, v in data.items()
                    if isinstance(k, str) and isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_personality_markers() -> None:
    """Atomically persist the marker dict (tmp + os.replace)."""
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = _MARKERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_personality_switch_marker, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _MARKERS_FILE)
    except OSError as e:
        from loguru import logger
        logger.warning("[personality] marker 持久化失败: {}", e)


_personality_switch_marker: dict = _load_personality_markers()


def _inject_personality_transition_into_history(user_id: str) -> None:
    """Write the transition notice INTO the conversation history (belt to the
    L4-tail note's braces). The volatile-tail note sits far below a long
    persona-styled history; an inline system message right next to the
    offending messages suppresses style drift much more reliably.

    Injected once, when the user confirms 「保留」 (or simply keeps talking
    after the switch prompt). Targets whichever store is actually in use:
    the active special session's delta, or the non-empty temp session.
    """
    note = _personality_switch_marker.get(user_id)
    if not note:
        return
    msg = (f"[系统提示] {note}"
           f"请严格遵守：从本条之后的所有回复都使用当前人格的语气、自称与用词，"
           f"不得模仿上方历史消息中旧人格的说话风格。")
    try:
        if _special_sessions.get_active(user_id) is not None:
            _special_sessions.add_message(user_id, "system", msg)
        else:
            session = _session_manager.get_or_create(user_id)
            if session.context:
                session.add_message("system", msg)
                _session_manager.update(user_id, session)
    except Exception as e:
        from loguru import logger
        logger.warning("[personality] 历史注入失败 user={}: {}", user_id, e)

# Active subtask window for multi-turn tool flows (gacha / speed-check):
#   user_id -> {task_id, goal, boundary, opened_at}
# Opened by the ``begin_task`` tool before the agent asks a setup/clarifying
# question; ``boundary`` is the session message count at that moment so the
# setup turns can be folded away when ``finalize_subtask`` closes the window.
_active_task: dict = {}
# Task windows older than this are treated as abandoned and ignored on finalize.
_TASK_WINDOW_TTL = 15 * 60

agent = Agent(
    deepseek_client=_model_router.reasoning_client,
    tool_registry=_tool_registry,
    config_dir=_CONFIG_DIR,
    session_manager=_session_manager,
    memory_system=_memory_system,
    tiered_memory=_tiered_memory,
    profile_manager=_profile_manager,
    hardware_detector=_hardware_detector,
    workspace_manager=_workspace_manager,
    special_session_manager=_special_sessions,
    max_tool_iterations=20,
    thinking_timeout=180.0,
)

_continuous_sessions = ContinuousSessionManager(timeout_minutes=1.5)

_perm_manager = PermissionManager()

# ── Bond System（Bond-System-Design.md §4，P0）─────────────────────
# 好感/心情/陪伴统计核心。data/bonds/{user_id}.json 持久化，
# mtime 校验缓存（外部写入优先，同 ProfileManager 纪律）。
# 结算钩子在回复成功发送后调用；仅 split 模式人格参与。
_bond_manager = BondManager(base_dir=os.path.join(_DATA_DIR, "bonds"))


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


# Stage 2: _build_reply_context moved to adapters/onebot.py as
# build_reply_context() — reply-segment / [CQ:reply] parsing is
# OneBot-specific. The entry plugin pre-computes it into
# view.reply_context.



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


def _end_continuous_mode() -> str:
    """结束当前群聊的连续对话窗口（仅连续对话模式下暴露给 LLM）。

    当用户在连续对话中表达「谢谢」「好的」「不用了」「再见」等结束/告别意图，
    且任务已完成时，调用此工具主动结束连续窗口，之后用户需 @机器人 才能再次触发。
    """
    group_id = _current_group_id.get()
    user_id = _current_user_id.get()
    if not group_id or not user_id:
        return "无法结束连续对话：当前不在群聊连续对话上下文中。"
    _continuous_sessions.end(group_id, user_id)
    return "已结束连续对话模式，之后需要@我才能触发。"


# end_continuous_mode is registered in the registry but NOT in any permission
# set — it is unioned into ``allowed_tools`` only in the continuous-mode path
# (_handle_continuous_message_impl), so it's never offered in the @-mention path.
_tool_registry.register(
    "end_continuous_mode", _end_continuous_mode,
    "结束当前群聊的连续对话窗口。当用户表达「谢谢」「好的」「不用了」「再见」"
    "等结束或告别意图，且当前任务已完成、无需继续追问时，调用此工具主动退出"
    "连续模式。调用后用户需要 @机器人 才能再次触发对话。",
    {"type": "object", "properties": {}, "required": []},
)


# ── Subtask encapsulation tools (begin_task / finalize_subtask) ──
#
# Multi-turn tool flows (gacha, battle speed-check) pollute context: each setup
# question and each verbose result gets persisted verbatim. These two tools let
# the agent demarcate such a flow so it is folded into one compact structured
# record (goal / result / params / refs / status / follow-ups / traceability)
# instead of a pile of raw turns. Full detail is kept in data/task_log/.

def _begin_task(goal: str) -> str:
    """Mark the start of a multi-turn subtask and open a fold window."""
    user_id = _current_user_id.get()
    if not user_id:
        return "[begin_task] 无法开始子任务：当前请求未设置用户上下文。"
    _active_task[user_id] = {
        "task_id": uuid.uuid4().hex[:12],
        "goal": (goal or "").strip(),
        "boundary": _session_manager.message_count(user_id),
        "opened_at": time.time(),
    }
    return (
        "[begin_task] 已记录子任务「" + (goal or "").strip() + "」的起点。"
        "现在请向用户提出必要的设置/澄清问题（如抽卡动画、测速模式），"
        "得到回答后继续执行相应工具，任务完成时务必调用 finalize_subtask 收尾。"
    )


def _finalize_subtask(
    goal: str,
    result: str,
    tool: str = "",
    params: dict = None,
    refs: list = None,
    status: str = "success",
    failure: str = "",
    follow_ups: list = None,
) -> str:
    """Finalize a subtask: persist a structured record and fold this flow."""
    user_id = _current_user_id.get()
    if not user_id:
        return "[finalize_subtask] 无法收尾：当前请求未设置用户上下文。"

    task = _active_task.pop(user_id, None)
    boundary = None
    if task and (time.time() - task.get("opened_at", 0)) <= _TASK_WINDOW_TTL:
        boundary = task.get("boundary")

    record = build_record(
        goal=goal or (task or {}).get("goal", ""),
        result=result or "",
        tool=tool or "",
        params=params,
        refs=refs,
        status=status,
        failure=failure,
        follow_ups=follow_ups,
    )
    append_task_log(user_id, record)
    line = build_compact_line(record)
    _pending_task_fold.set({"line": line, "boundary": boundary})
    return "[finalize_subtask] 子任务结果已结构化记录，本轮交互将被折叠为一条摘要。"


_tool_registry.register(
    "begin_task", _begin_task,
    "开始一个需要多轮交互的工具型子任务（如抽卡、战斗测速）。当你接下来必须先向"
    "用户询问设置/澄清问题（例如抽卡是否看动画、测速用轻量还是全量模式）时，"
    "先调用此工具记录任务起点，再提出该问题。它让后续的问答与结果被折叠成一条"
    "结构化记录，避免污染上下文。仅在确有澄清环节的工具任务开始时调用一次。",
    {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "子任务目标的简短描述，如「十连抽卡」「战斗截图测速」"},
        },
        "required": ["goal"],
    },
)


_tool_registry.register(
    "finalize_subtask", _finalize_subtask,
    "结束一个工具型子任务并提交结构化结果。在抽卡、战斗测速等多轮工具任务给出"
    "最终结果后必须调用：它把整个任务折叠成一条精炼记录存入上下文，完整细节归档"
    "到任务日志以便追溯。result 用 1-3 句话概括最终结果；params 填关键参数（如"
    "卡池/次数/测速模式）；有未完成事项或建议时写入 follow_ups。",
    {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "子任务目标"},
            "result": {"type": "string", "description": "最终结果的精炼概括（1-3 句）"},
            "tool": {"type": "string", "description": "主要使用的工具名，如 gacha_pull / parse_battle_screenshots"},
            "params": {"type": "object", "description": "关键参数/约束，如 {pool_type, count, mode}"},
            "refs": {"type": "array", "items": {"type": "string"}, "description": "引用的角色名/文件路径/会话名等"},
            "status": {"type": "string", "enum": ["success", "partial", "failed"], "default": "success"},
            "failure": {"type": "string", "description": "失败类型（仅 status=failed 时填写）"},
            "follow_ups": {"type": "array", "items": {"type": "string"}, "description": "未完成事项或后续建议"},
        },
        "required": ["goal", "result"],
    },
)


# ── Search result archive recall (recall_search_result) ──
#
# search_web / web_fetch archive their full results to data/search_cache/
# (agent/search_archive.py) and append an archive id line to the tool return
# text. The id rides the append-only message stream, so after context
# folding/compression the model can still dereference it via this tool
# instead of re-searching. Retention: 7 days / 50 per user, scoped by
# contextvar user_id — never trust the id alone.

def _recall_search_result(archive_id: str) -> str:
    """Retrieve a full archived search_web/web_fetch result by its id."""
    user_id = _current_user_id.get()
    if not user_id:
        return "[recall] 无法取回：当前请求未设置用户上下文。"
    record = get_default_archive().recall(user_id, archive_id)
    if not record:
        return (
            "[recall] 未找到该存档：id 无效、已过期（保留期7天）或不属于当前用户。"
            "建议重新调用 search_web 获取最新结果。"
        )
    try:
        age_hours = max(0.0, (time.time() - float(record.get("ts", 0))) / 3600)
        age_note = f"（距今约 {age_hours:.1f} 小时，时效敏感内容请酌情重新搜索）"
    except (TypeError, ValueError):
        age_note = ""
    header = (
        f"[recall] 存档时间: {record.get('iso_time', '未知')}{age_note}\n"
        f"工具: {record.get('tool', '')} | 查询: {record.get('query', '')}\n\n"
    )
    return header + record.get("content", "")


_tool_registry.register(
    "recall_search_result", _recall_search_result,
    "按存档 id 取回此前 search_web / web_fetch 的完整结果。当上下文中的搜索结果"
    "已被折叠或压缩、而你需要其中的 URL 或细节时，优先用此工具取回存档而不是"
    "重新搜索。存档 id 来自工具返回末尾的「[存档] id: xxx」行，保留 7 天，"
    "仅限取回自己的存档。",
    {
        "type": "object",
        "properties": {
            "archive_id": {
                "type": "string",
                "description": "存档 id（12位十六进制，来自工具返回末尾的 [存档] 行）",
            },
        },
        "required": ["archive_id"],
    },
)


# ── LONG-tier memory recall (recall_memory) ──
#
# P2-lite of the three-tier memory engine (Memory-Optimization-Plan ①).
# The system prompt carries a one-line index of the user's LONG memories;
# this tool dereferences an index line into the full entry + the original
# conversation snapshot. Scoped by contextvar user_id — never trust the id
# alone. Same pattern as recall_search_result above.

def _recall_memory(query: str) -> str:
    """Retrieve the current user's own LONG-tier memory by id or keyword."""
    user_id = _current_user_id.get()
    if not user_id:
        return "[recall] 无法取回：当前请求未设置用户上下文。"
    matches = _tiered_memory.recall_long(user_id, query)
    if not matches:
        return (
            "[recall] 未找到匹配的长期记忆：id 无效或关键词不匹配。"
            "可换用「用户长期记忆索引」中的其他 id/关键词；仅限取回当前用户自己的记忆。"
        )
    parts = []
    for lo, snapshot_text in matches:
        when = (
            time.strftime("%Y-%m-%d", time.localtime(lo.snapshot_time))
            if lo.snapshot_time else "未知"
        )
        parts.append(
            f"[长期记忆 {lo.id}]（快照时间 {when}）\n"
            f"{lo.summary}\n"
            f"对话快照（{len(lo.snapshot_turns)} 轮）:\n{snapshot_text}"
        )
    return "\n\n".join(parts)


_tool_registry.register(
    "recall_memory", _recall_memory,
    "按 id 或关键词取回当前用户的长期记忆全文与对话快照。系统提示词中的"
    "「用户长期记忆索引」列出了每条记忆的 id 与标题；当索引里的某条记忆与"
    "当前问题相关、而你需要完整细节或当时的对话原文时，调用此工具。"
    "仅限取回当前用户自己的长期记忆。",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "长期记忆 id（来自索引行 [id]），或标题/内容关键词",
            },
        },
        "required": ["query"],
    },
)


# ── Turn entry points (busy-guard wrappers) ────────────────────────
# Platform-specific guards (@-mention detection, poke filtering) and
# event->view conversion live in plugins/agent_router.py +
# adapters/onebot.py; everything below consumes InboundMessage views.

async def run_agent_turn(view: InboundMessage):
    """Run one agent turn under the per-user busy guard."""
    # Platform identity (stage 4, §7.7): PermissionManager.get_role() picks
    # the openid tables for qq_official, the QQ-number tables for onebot.
    _current_platform.set(view.platform)
    user_id = view.user_id
    # ── Per-user concurrency guard ──
    if user_id in _user_busy:
        _pm = get_character_registry()
        _short = _pm.get_short_name(_pm.resolve_effective_personality(user_id, view.group_id))
        await _safe_send(f"{_short} 正在处理你的上一条消息，请稍等~")
        return
    _user_busy.add(user_id)
    try:
        return await handle_agent_turn(view)
    finally:
        _user_busy.discard(user_id)


async def handle_agent_turn(view: InboundMessage):
    """Inner implementation — called under per-user busy guard."""
    user_id = view.user_id

    # Token-usage attribution: set before triage so every LLM call of this
    # turn (triage / agent_loop / profile extraction) is tied to this user.
    _gid = view.group_id
    set_usage_context(user_id, _gid, "group" if _gid else "private")

    # Set user workspace for tool scoping
    _workspace_manager.ensure_dirs(user_id)
    _current_user_workspace.set(_workspace_manager.get_workspace(user_id))
    _on_file_created.set(lambda p: _record_session_file(user_id, p))

    text_content = view.text

    # ── Handle pending delete confirmation (bare text, no / prefix) ──
    pending_delete = _pending_delete_confirm.get(user_id)
    if pending_delete:
        expected = f"确认删除 {pending_delete[0]}"
        if text_content == expected or text_content == f"/{expected}":
            if time.time() < pending_delete[1]:
                try:
                    # Capture the session's turn count BEFORE delete, for the
                    # profile delete-flush (Decision J): a substantive session
                    # (>K turns) submits its un-extracted profile tail; a
                    # transient one (≤K turns) drops it.
                    _del_turns = 0
                    for _s in _special_sessions.list_sessions(user_id):
                        if _s.get("name") == pending_delete[0]:
                            _del_turns = _s.get("total_messages", 0) // 2
                            break
                    result = _special_sessions.delete(user_id, pending_delete[0])
                    _pending_delete_confirm.pop(user_id, None)
                    try:
                        _profile_manager.flush_on_session_end(user_id, _del_turns)
                    except Exception:
                        pass  # profile flush must never break session deletion
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

    # ── Handle pending UGC character deletion confirmation (bare text) ──
    pending_char = _pending_char_delete_confirm.get(user_id)
    if pending_char:
        expected = f"确认删除角色 {pending_char[0]}"
        if text_content == expected or text_content == f"/{expected}":
            _pending_char_delete_confirm.pop(user_id, None)
            if time.time() < pending_char[1]:
                registry = get_character_registry()
                char_id = registry.resolve_name(pending_char[0], user_id=user_id)
                if (char_id and registry.is_ugc(char_id)
                        and (registry.get_meta(char_id) or {}).get("author") == user_id):
                    display = registry.get_display_name(char_id)
                    registry.delete(char_id)
                    purged = registry.purge_references(char_id)
                    note = f"\n（已解除 {purged} 处人格设置引用）" if purged else ""
                    await _safe_send(f"已删除自创角色「{display}」。{note}")
                else:
                    await _safe_send(f"未找到你的自创角色「{pending_char[0]}」。")
            else:
                await _safe_send("确认已超时（60秒），请重新发起 /删除角色。")
            return

    # ── Handle pending personality-switch choice (keep vs clear) ────
    pending_switch = _pending_personality_switch.pop(user_id, None)
    if pending_switch:
        _old_display, new_display = pending_switch
        _reply = text_content.strip()
        _clear_kw = ("清空", "清除", "清空上下文", "清除上下文", "不保留", "清")
        _keep_kw = ("保留", "保留历史", "保留记录", "是", "好的", "嗯", "行", "可以", "yes", "keep")
        if _reply in _clear_kw:
            agent.clear_user_session(user_id)
            _special_sessions.clear_context(user_id)
            _temp_session_files.pop(user_id, None)
            if _personality_switch_marker.pop(user_id, None) is not None:
                _save_personality_markers()
            await _safe_send(f"已清空对话上下文，开始以「{new_display}」人格的新对话~")
            return
        if _reply.lower() in _keep_kw:
            _inject_personality_transition_into_history(user_id)
            await _safe_send(f"已保留历史对话，后续回复将遵循「{new_display}」人格。")
            return
        # Otherwise: user asked a new question — keep history (marker persists)
        # and fall through so the agent answers with the transition notice.
        # Inject the notice into the history now, before this turn's messages
        # are appended, so it sits directly after the old-persona turns.
        _inject_personality_transition_into_history(user_id)

    # ── Handle feedback / bug report (before agent, zero token cost) ─
    if text_content.startswith(("#反馈", "#bug", "#建议")):
        await _handle_feedback(text_content, user_id)
        return

    # ── Handle session management commands ──────────────────────────
    if text_content.startswith("/") or text_content.startswith("#"):
        # /取消 / #取消 / /结束 / #结束 — end continuous mode (group only).
        # The continuous_router (priority=2) skips @mentioned messages, so this
        # must also be handled here when the user @-mentions the bot; otherwise
        # the command falls through to the agent loop and never ends the window.
        if view.is_group and text_content in ("/取消", "#取消", "/结束", "#结束"):
            _continuous_sessions.end(view.group_id, user_id)
            await _safe_send("已结束连续对话模式，之后需要@我才能触发~")
            return
        # /toggle command (group only, superuser only)
        cmd_handled = await _handle_toggle_command(text_content, user_id, view)
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
        # /刷新角色数据 command (admin-only forced wiki cache refresh)
        cmd_handled = await _handle_refresh_data_command(text_content, user_id)
        if cmd_handled:
            return
        # /功能 / /features command (direct, no agent)
        cmd_handled = await _handle_features_command(text_content, user_id)
        if cmd_handled:
            return
        # /更新日志 [d] / /update record [d] command (direct, no agent, zero token)
        cmd_handled = await _handle_changelog_command(text_content, user_id)
        if cmd_handled:
            return
        # /personality / /人格切换 command
        cmd_handled = await _handle_personality_command(text_content, user_id, view)
        if cmd_handled:
            return
        # /好感度 #好感度 /bond command (direct, no agent, zero token)
        cmd_handled = await _handle_bond_command(text_content, user_id, view)
        if cmd_handled:
            return
        # /剧情 #剧情 command (bond story scenes, P3; private-chat only)
        cmd_handled = await _handle_scene_command(text_content, user_id, view)
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

    # ── Detect reply/quote context (built by adapters/onebot.py) ───
    reply_context = view.reply_context
    if reply_context:
        import sys; print(f"[REPLY_DIAG] reply_context built ({len(reply_context)} chars): {reply_context[:300]}", file=sys.stderr, flush=True)

    # ── Detect and download file/image attachments ─────────────────
    # Stage 2: the segment walk + NapCat download calls moved to
    # adapters/onebot.py (OneBotAttachmentResolver). This loop keeps the
    # prompt strings and _recent_files bookkeeping byte-identical.
    file_context_parts = []
    has_voice = False
    msg_id = view.message_id
    for att in await view.load_attachments():
        if att.kind == "image":
            if att.path:
                file_context_parts.append(f"[用户上传了图片，已保存至: {att.path}]")
                _record_file(msg_id, att.name, att.path)
                _record_session_file(user_id, att.path)
            elif att.error:
                file_context_parts.append(f"[用户上传了图片，但下载失败: {att.error}]")
                _record_file(msg_id, att.name, error=att.error)

        elif att.kind == "file":
            if att.path:
                file_context_parts.append(f"[用户上传了文件 {att.name}，已保存至: {att.path}]")
                _record_file(msg_id, att.name, att.path)
                _record_session_file(user_id, att.path)
            elif att.error:
                file_context_parts.append(f"[用户上传了文件 {att.name}，但下载失败: {att.error}]")
                _record_file(msg_id, att.name, error=att.error)

        elif att.kind == "voice":
            has_voice = True
            if att.path:
                file_context_parts.append(
                    f"[用户发送了语音消息，已保存至: {att.path}]"
                )
                _record_file(msg_id, att.name, att.path)
                _record_session_file(user_id, att.path)
            elif att.error:
                file_context_parts.append(f"[用户发送了语音消息，但下载失败: {att.error}]")
                _record_file(msg_id, att.name, error=att.error)

    # ── Quota warning after any upload ─────────────────────────────
    quota_warn = _quota_warning(user_id)

    # ── File-only messages: acknowledge and skip agent ─────────────
    # Voice messages are exempt: a bare voice message is a conversational
    # turn (the user expects the LLM to listen and respond), not a file
    # upload awaiting later analysis. Skipping the agent for voice broke
    # the pre-existing "语音直达 LLM" behavior (regression from f7e5f4e).
    has_files = bool(file_context_parts)
    if has_files and not has_voice and not text_content and not reply_context:
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
        elif has_voice:
            # Bare voice turn: instruct the agent to listen and respond,
            # matching the pre-f7e5f4e behavior where 语音直达 LLM.
            augmented_message = (
                f"{context_prefix}\n用户发送了语音消息，请使用 read_file 工具"
                f"分析音频内容，并根据其内容直接回应用户。"
            )
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

    # Send thinking indicator (non-critical, ignore send failures).
    # immediate=True: on the official route this is the one progress
    # line worth spending passive-reply quota on — it must go out as a
    # standalone message right away, not merged into the final reply.
    try:
        _gid = view.group_id
        _pm = get_character_registry()
        _short = _pm.get_short_name(_pm.resolve_effective_personality(user_id, _gid))
        await _safe_send(OutProgress(f"{_short} 正在思考...", immediate=True))
    except Exception:
        pass

    try:
        # ── Dual-plane routing: resolve persona & its mode first ──
        # split persona → each message is triaged to social (角色面) or task
        # (任务面) plane; unified persona → single plane (existing behavior).
        group_id = view.group_id
        pm = get_character_registry()
        persona_key = pm.resolve_effective_personality(user_id, group_id)
        persona_mode = pm.get_mode(persona_key)

        # Detect session type (also gates the UGC creation interview below)
        active_special = _special_sessions.get_active(user_id)
        session_type = "special" if active_special else "temporary"
        is_creation = bool(active_special) and (
            (active_special.metadata or {}).get("kind") == "char_creation"
        )
        # Bond story-scene session (Bond-System-Design.md §4.4 形态3, P3).
        is_scene = bool(active_special) and (
            (active_special.metadata or {}).get("kind") == "bond_scene"
        )
        if is_scene:
            # The scene belongs to the character it was started with; keep the
            # turn in that character's voice even if the user switched persona.
            persona_key = str(
                (active_special.metadata or {}).get("char_id") or persona_key
            )
            persona_mode = pm.get_mode(persona_key)

        # ── Triage: classify complexity (and plane, for split personas) ──
        # Creation interviews skip triage entirely: neutral assistant persona,
        # flash model, only create_character exposed (Bond-System-Design.md §5.1).
        # Scene sessions skip triage too: full-RP as the character on flash.
        is_special = augmented_message in ["/clear", "清除上下文", "新对话", "/status"]
        plane = "task"
        if is_creation or is_scene:
            client = _model_router.flash_client
        elif not is_special:
            if persona_mode == "split":
                _cls = await _model_router.classify(augmented_message)
                plane = _cls.get("plane", "task")
                complexity = _cls.get("complexity", "complex")
            else:
                complexity = await _model_router.classify_complexity(augmented_message)
            if complexity == "simple":
                client = _model_router.flash_client
            else:
                client = _model_router.reasoning_client
        else:
            client = None  # Use default client for special commands

        # Run the agent loop with timeout
        async def _progress(msg: str):
            await _safe_send(OutProgress(msg))
        token = _current_sender.set(_ToolSender())

        # Resolve user permissions
        role = _perm_manager.get_role(user_id)
        allowed_tools = _perm_manager.get_allowed_tools(role)
        code_limits = _perm_manager.get_code_limits(role)

        # Resolve group feature restrictions
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

        # ── Plane context (Bond-System-Design.md §3) ──
        # character_plane: split persona routed to social → L2 = 角色卡,
        #   tools = SOCIAL_TOOLS only (delegate_task fallback), bond state
        #   injected into the L4 volatile tail.
        # Otherwise (split→task plane, or unified persona): L2 = neutral
        #   "assistant" for split task plane (globally-shared cache prefix),
        #   or the full persona for unified; no bond injection.
        character_plane = (
            persona_mode == "split" and plane == "social"
            and not is_creation and not is_scene
        )
        if is_creation:
            # UGC creation interview: neutral assistant persona (globally-shared
            # cache prefix), only the create_character tool exposed, no bond.
            allowed_tools = {"create_character"}
            _current_bond_context.set("")
            _current_personality.set("assistant")
        elif is_scene:
            # Bond story scene (P3): full-RP as the scene's character on flash;
            # only complete_scene exposed (pure RP — no delegate_task / dangerous
            # tools); bond context injected so the scene reflects current bond.
            client = _model_router.flash_client
            allowed_tools = {"complete_scene"}
            _current_personality.set(persona_key)
            _current_bond_context.set(
                _build_bond_context(user_id, persona_key, pm)
            )
        elif character_plane:
            client = _model_router.flash_client
            allowed_tools = set(SOCIAL_TOOLS)
            _current_personality.set(persona_key)
            _current_bond_context.set(
                _build_bond_context(user_id, persona_key, pm)
            )
        else:
            _current_bond_context.set("")
            _current_personality.set(
                "assistant" if persona_mode == "split" else persona_key
            )
        _delegate_depth.set(0)
        _personality_transition.set(_personality_switch_marker.get(user_id, ""))

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
            _current_sender.reset(token)

        # ── Revoicing layer (Bond-System-Design.md §3.3) ──
        # Only the split persona's *task plane* output needs re-voicing: the
        # task agent answered as neutral Roxy, so wrap its result in the
        # character's voice (opening line) while passing the body verbatim.
        # The social plane already speaks in character; unified personas are
        # never re-voiced. Creation interviews stay neutral (no revoice).
        if (persona_mode == "split" and not character_plane
                and not is_creation and not is_scene):
            # Restore persona so _send_response's disclaimer is signed by the
            # character and revoice speaks in the character's voice.
            _current_personality.set(persona_key)
            _use_llm_voice = (
                not group_id and pm.get_task_voice(persona_key) == "character"
            )
            raw_response = response
            response = await revoice_task_result(
                client=_model_router.flash_client,
                persona_content=pm.load(persona_key) or "",
                short_name=pm.get_short_name(persona_key),
                user_request=text_content or augmented_message,
                response=response,
                use_llm=_use_llm_voice,
            )
            # Best-effort: rewrite the persisted assistant turn so stored
            # session history reads in the character's voice (verbatim-match
            # guard inside skips it if the turn was already folded/compressed).
            if session_type == "temporary":
                repatch_last_assistant(_session_manager, user_id, raw_response, response)

        # Send response (split long messages)
        await _send_response(response)

        # ── Creation session auto-close: once create_character succeeded this
        # turn (the tool closure flags it), leave the interview session so the
        # next /人格切换 works cleanly. ──
        if is_creation and _pending_char_creation_done.pop(user_id, False):
            try:
                _special_sessions.end_active(user_id)
            except Exception:
                pass

        # ── Scene session completion (P3): complete_scene succeeded this turn
        # (the tool closure flagged it) → level-up notice + commemorative card,
        # then leave the scene session. Popped unconditionally so a stale flag
        # can't survive into a later turn. ──
        scene_result = _pending_bond_scene_done.pop(user_id, None)
        if is_scene and scene_result:
            await _finish_bond_scene(user_id, scene_result)

        # ── Bond settlement hook (both planes accrue 好感度) ──
        if persona_mode == "split" and not is_creation:
            await _settle_bond_turn(user_id, persona_key, is_group=bool(view.is_group))

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
        if view.is_group:
            _continuous_sessions.start(view.group_id, user_id)

    except asyncio.TimeoutError:
        await _safe_send("抱歉，思考超时了。请尝试用更简单的方式提问~")
    except Exception as e:
        code = log_exception("agent message handling failed", e)
        await _safe_send(f"处理消息时出现内部错误（诊断编号 {code}）。请稍后重试；若持续出现请用 #bug 反馈。")


async def run_continuous_turn(view: InboundMessage):
    """Run one continuous-mode turn (active-window check + busy guard)."""
    _current_platform.set(view.platform)
    user_id = view.user_id
    group_id = view.group_id
    # Check if user is in continuous mode
    if not _continuous_sessions.is_active(group_id, user_id):
        return

    # ── Per-user concurrency guard ──
    if user_id in _user_busy:
        _pm = get_character_registry()
        _short = _pm.get_short_name(_pm.resolve_effective_personality(user_id, group_id))
        await _safe_send(f"{_short} 正在处理你的上一条消息，请稍等~", sender=continuous_sender)
        return
    _user_busy.add(user_id)
    try:
        return await handle_continuous_turn(view)
    finally:
        _user_busy.discard(user_id)


async def handle_continuous_turn(view: InboundMessage):
    """Inner implementation — called under per-user busy guard."""
    user_id = view.user_id
    group_id = view.group_id

    # Token-usage attribution (see handle_agent_turn).
    set_usage_context(user_id, group_id, "group")

    text_content = view.text

    # Cancel detection: slash/hash commands
    if text_content in ["/取消", "#取消", "/结束", "#结束"]:
        _continuous_sessions.end(group_id, user_id)
        await _safe_send("已结束连续对话模式，之后需要@我才能触发~", sender=continuous_sender)
        return

    # /好感度 #好感度 /bond (direct, no agent, zero token)
    if await _handle_bond_command(text_content, user_id, view, sender=continuous_sender):
        return

    # ── Handle pending delete confirmation (intercept before agent) ──
    pending_delete = _pending_delete_confirm.get(user_id)
    if pending_delete:
        expected = f"确认删除 {pending_delete[0]}"
        if text_content == expected or text_content == f"/{expected}":
            if time.time() < pending_delete[1]:
                try:
                    # Capture the session's turn count BEFORE delete, for the
                    # profile delete-flush (Decision J): a substantive session
                    # (>K turns) submits its un-extracted profile tail; a
                    # transient one (≤K turns) drops it.
                    _del_turns = 0
                    for _s in _special_sessions.list_sessions(user_id):
                        if _s.get("name") == pending_delete[0]:
                            _del_turns = _s.get("total_messages", 0) // 2
                            break
                    result = _special_sessions.delete(user_id, pending_delete[0])
                    _pending_delete_confirm.pop(user_id, None)
                    try:
                        _profile_manager.flush_on_session_end(user_id, _del_turns)
                    except Exception:
                        pass  # profile flush must never break session deletion
                    role = _perm_manager.get_role(user_id)
                    max_sess = _perm_manager.get_max_special_sessions(role)
                    sessions = _special_sessions.list_sessions(user_id)
                    summary = _format_delete_summary(result)
                    msg = f"已删除特殊会话「{pending_delete[0]}」。"
                    if summary:
                        msg += "\n" + summary
                    msg += f"\n当前特殊会话: {len(sessions)}/{max_sess}"
                    await _safe_send(msg, sender=continuous_sender)
                except ValueError as e:
                    await _safe_send(str(e), sender=continuous_sender)
            else:
                _pending_delete_confirm.pop(user_id, None)
                await _safe_send(
                    "确认已超时（60秒），请重新发起 /删除会话。",
                    sender=continuous_sender
                )
            return

    # ── Detect reply/quote context and file attachments ────────────
    # These run BEFORE the text guard so files are always saved and
    # recorded, even for file-only messages that may be replied to later.
    reply_context = view.reply_context
    if reply_context:
        print(f"[REPLY_DIAG] continuous reply_context built ({len(reply_context)} chars): {reply_context[:300]}")

    # Stage 2: downloads moved to adapters/onebot.py (see handle_agent_turn).
    file_context_parts = []
    msg_id = view.message_id
    for att in await view.load_attachments():
        if att.kind == "image":
            if att.path:
                file_context_parts.append(f"[用户上传了图片，已保存至: {att.path}]")
                _record_file(msg_id, att.name, att.path)
                _record_session_file(user_id, att.path)
            elif att.error:
                file_context_parts.append(f"[用户上传了图片，但下载失败: {att.error}]")
                _record_file(msg_id, att.name, error=att.error)

        elif att.kind == "file":
            if att.path:
                file_context_parts.append(f"[用户上传了文件 {att.name}，已保存至: {att.path}]")
                _record_file(msg_id, att.name, att.path)
                _record_session_file(user_id, att.path)
            elif att.error:
                file_context_parts.append(f"[用户上传了文件 {att.name}，但下载失败: {att.error}]")
                _record_file(msg_id, att.name, error=att.error)

        elif att.kind == "voice":
            if att.path:
                file_context_parts.append(
                    f"[用户发送了语音消息，已保存至: {att.path}]"
                )
                _record_file(msg_id, att.name, att.path)
                _record_session_file(user_id, att.path)
            elif att.error:
                file_context_parts.append(f"[用户发送了语音消息，但下载失败: {att.error}]")
                _record_file(msg_id, att.name, error=att.error)

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
        "回复保持简洁。如果用户表达了「谢谢」「好的」「不用了」「再见」等结束或"
        "告别意图，且任务已完成、无需继续追问，请调用 end_continuous_mode 工具"
        "主动结束连续模式，而不是建议用户手动发送 /取消。"
    )
    if context_parts:
        augmented_message = f"{continuous_prefix}\n{'\n'.join(context_parts)}\n用户说: {text_content}"
    else:
        augmented_message = f"{continuous_prefix}\n用户说: {text_content}"

    try:
        # ── Dual-plane routing (Bond-System-Design.md §3) ──
        pm = get_character_registry()
        persona_key = pm.resolve_effective_personality(user_id, group_id)
        persona_mode = pm.get_mode(persona_key)

        plane = "task"
        if persona_mode == "split":
            _cls = await _model_router.classify(augmented_message)
            plane = _cls.get("plane", "task")
            complexity = _cls.get("complexity", "complex")
        else:
            complexity = await _model_router.classify_complexity(augmented_message)
        if complexity == "simple":
            client = _model_router.flash_client
        else:
            client = _model_router.reasoning_client

        token = _current_sender.set(_ToolSender(continuous_sender))
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

            # Resolve group feature restrictions (BEFORE plane context so the
            # role-tool subtraction lands on the task plane's allowed set)
            gf = get_group_features()
            gf.refresh()
            disabled_tools = gf.get_disabled_tools(group_id)
            if disabled_tools:
                allowed_tools = allowed_tools - disabled_tools
            # end_continuous_mode is only offered in the continuous path, so the
            # agent can end the window on a natural-language "thanks/done" signal.
            allowed_tools = allowed_tools | {"end_continuous_mode"}
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

            # ── Plane context ──
            character_plane = (persona_mode == "split" and plane == "social")
            if character_plane:
                client = _model_router.flash_client
                # Social plane keeps only delegate_task (+ window control).
                allowed_tools = set(SOCIAL_TOOLS) | {"end_continuous_mode"}
                _current_personality.set(persona_key)
                _current_bond_context.set(
                    _build_bond_context(user_id, persona_key, pm)
                )
            else:
                _current_bond_context.set("")
                _current_personality.set(
                    "assistant" if persona_mode == "split" else persona_key
                )
            _delegate_depth.set(0)
            _personality_transition.set(_personality_switch_marker.get(user_id, ""))

            response = await asyncio.wait_for(
                agent.run(augmented_message, user_id, client=client,
                           progress_callback=lambda msg: _safe_send(OutProgress(msg), sender=continuous_sender),
                           allowed_tools=allowed_tools,
                           user_role=role.value),
                timeout=300.0,
            )
        finally:
            _current_sender.reset(token)

        # ── Revoicing layer (task plane of split persona; group → template) ──
        if persona_mode == "split" and not character_plane:
            _current_personality.set(persona_key)
            raw_response = response
            response = await revoice_task_result(
                client=_model_router.flash_client,
                persona_content=pm.load(persona_key) or "",
                short_name=pm.get_short_name(persona_key),
                user_request=text_content or augmented_message,
                response=response,
                use_llm=False,
            )
            repatch_last_assistant(_session_manager, user_id, raw_response, response)

        await _send_response(response, sender=continuous_sender)

        # ── Bond settlement hook ──
        if persona_mode == "split":
            await _settle_bond_turn(user_id, persona_key, sender=continuous_sender,
                                     is_group=True)

    except asyncio.TimeoutError:
        _pm = get_character_registry()
        _short = _pm.get_short_name(_pm.resolve_effective_personality(user_id, group_id))
        await _safe_send(f"抱歉，{_short}思考时间超过您的配额时长了。请尝试用更简单的方式提问~", sender=continuous_sender)
    except Exception as e:
        code = log_exception("continuous agent message handling failed", e)
        await _safe_send(f"处理消息时出现内部错误（诊断编号 {code}）。请稍后重试；若持续出现请用 #bug 反馈。", sender=continuous_sender)


async def _safe_send(message, max_retries: int = 2, sender=None):
    """Send a message (str, OutImage or OutProgress) with retry on SendFailed.

    Args:
        message: Message text (str), agent.outbound.OutImage or OutProgress.
        max_retries: Number of retry attempts on SendFailed.
        sender: Optional Sender to use. Falls back to the _turn_sender
            contextvar (QQ official entry binds a per-event sender there)
            and then to default_sender.
    """
    s = sender or _turn_sender.get() or default_sender
    last_error = None
    for attempt in range(max_retries):
        try:
            if isinstance(message, OutImage):
                await s.send_image(message)
            elif isinstance(message, OutProgress):
                await s.send_progress(message.text, immediate=message.immediate)
            else:
                await s.send_text(message)
            return
        except SendFailed as e:
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


async def _send_text_chunks(lines: list):
    """Send lines as ≤300-char chunks on line boundaries, 1s apart."""
    chunks = []
    current = ""
    for ln in "\n".join(lines).split("\n"):
        candidate = f"{current}\n{ln}" if current else ln
        if len(candidate) <= 300:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = ln
    if current:
        chunks.append(current)
    for i, chunk in enumerate(chunks):
        await _safe_send(chunk)
        if i < len(chunks) - 1:
            await asyncio.sleep(1.0)  # QQ rate limiting


async def _send_codes_individually(codes: list):
    """Send each code as a standalone message so users can copy it directly."""
    entries = [c for c in codes if c.get("code")]
    if not entries:
        return
    await _safe_send("以下逐条发送兑换码，方便复制：")
    for entry in entries:
        await asyncio.sleep(1.0)  # QQ rate limiting
        await _safe_send(entry["code"])


async def _handle_redeem_code_command(text: str, user_id: str) -> bool:
    """Handle /兑换码 (time-limited) and /长期兑换码 commands — direct, no agent.

    /兑换码       → currently-valid time-limited codes + codes expired ≤7 days
    /长期兑换码   → codes with no known expiry (long-term / permanent)

    Returns True if the command was handled.
    """
    cmd = text.strip().split()[0] if text.strip() else ""
    time_cmds = ("/兑换码", "/redeem-code", "#兑换码", "#redeem-code")
    long_cmds = ("/长期兑换码", "#长期兑换码")
    if cmd not in time_cmds and cmd not in long_cmds:
        return False

    from plugins.check_redeem_code import (
        get_time_limited_codes, get_long_term_codes,
        check_and_refresh, get_cache_info,
        format_expiry_display, _write_admin_alert, is_refresh_in_flight,
    )

    # Trigger a refresh if stale, then use cached data. With the browser
    # engine enabled this returns immediately while the scrape runs in the
    # background (in_flight True); the HTTP path blocks until done.
    await check_and_refresh()
    info = get_cache_info()
    in_flight = is_refresh_in_flight()

    # Staleness footer: shown when cache is old (scrape failing / in progress)
    stale_note = ""
    if info.get("stale"):
        iso = info.get("scraped_at_iso", "")
        date_part = iso[:10] if iso else "未知时间"
        if in_flight:
            stale_note = f"⚠ 数据截至 {date_part}，正在后台更新中…"
        else:
            stale_note = f"⚠ 数据截至 {date_part}（自动更新失败，可能不是最新）"

    # ── /长期兑换码 branch ──────────────────────────────────────
    if cmd in long_cmds:
        lt_codes = get_long_term_codes()
        if not lt_codes:
            msg = "还没有收录长期有效的兑换码。"
            if stale_note:
                msg += f"\n{stale_note}"
            await _safe_send(msg)
            return True

        lines = [f"长期有效兑换码（无过期时间，共 {len(lt_codes)} 条）:", ""]
        for entry in lt_codes:
            line = f"  {entry.get('code', '')}"
            if entry.get("content"):
                line += f"\n  兑换内容: {entry['content']}"
            lines.append(line)
        lines.append("")
        lines.append("（数量较多，不逐条发送；长按上方消息可复制）")
        if stale_note:
            lines.append(stale_note)
        await _send_text_chunks(lines)
        return True

    # ── /兑换码 branch: time-limited codes ─────────────────────
    codes = get_time_limited_codes()
    active = [c for c in codes if not c.get("recently_expired")]
    recent_expired = [c for c in codes if c.get("recently_expired")]

    if not codes:
        if in_flight:
            # Background (browser-engine) refresh still running — don't claim
            # failure or alert the admin for a scrape that hasn't finished.
            msg = ("兑换码正在后台更新中，请稍等片刻再试～"
                   "\n长期有效的兑换码请发送 /长期兑换码 查询。")
            await _safe_send(msg)
            return True
        if info.get("stale"):
            # No time-limited codes AND scraper failing → tell the user
            # and raise the WebUI panel alert so the admin adds codes.
            _write_admin_alert("no_valid_codes")
            msg = "当前兑换码均已过期，网页爬取失败，等待管理员添加"
        else:
            msg = "当前没有有效的限时兑换码哦Σ( ° △ °)"
        msg += "\n长期有效的兑换码请发送 /长期兑换码 查询。"
        if stale_note:
            msg += f"\n{stale_note}"
        await _safe_send(msg)
        return True

    # Cap the active list so a bloated cache can never produce an
    # unsendable message or an endless per-code flood
    MAX_DISPLAY = 30
    shown_active = active[:MAX_DISPLAY]
    hidden = len(active) - len(shown_active)

    lines = [f"当前有效限时兑换码（{len(active)} 条）:", ""]
    for entry in shown_active:
        line = f"  {entry.get('code', '')}"
        if entry.get("content"):
            line += f"\n  兑换内容: {entry['content']}"
        if entry.get("valid"):
            line += f"\n  有效期至: {format_expiry_display(entry['valid'])}"
        else:
            line += "\n  过期时间未知（新收录，建议尽快兑换）"
        lines.append(line)

    if recent_expired:
        lines.append("")
        lines.append(f"以下限时码已于 7 天内过期（{len(recent_expired)} 条）：")
        for entry in recent_expired:
            line = f"  {entry.get('code', '')}"
            if entry.get("content"):
                line += f"\n  兑换内容: {entry['content']}"
            expired_disp = format_expiry_display(entry.get("valid", "")) or "?"
            line += f"\n  已于 {expired_disp} 过期"
            lines.append(line)

    if hidden > 0:
        lines.append(f"  …另有 {hidden} 条较早的码未显示")
    lines.append("")
    lines.append("长期有效的兑换码请发送 /长期兑换码")
    if stale_note:
        lines.append(stale_note)

    await _send_text_chunks(lines)

    # Send each active code as a standalone message (expired ones excluded —
    # single-sending them would invite users to copy dead codes)
    await _send_codes_individually(shown_active)
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
        await _safe_send(OutImage(path=card_path, label="character card"))
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
        await _safe_send(OutImage(path=card_path, label="bond card"))
    else:
        await _safe_send(text_result)
    return True


# Guard so repeated /刷新角色数据 presses don't stack concurrent refreshes.
_forced_refresh_running = False


async def _forced_refresh_wiki_data(user_id: str, notify_sender=None) -> None:
    """Force a full character + bond cache refresh in the background.

    Runs after the triggering handler has returned, so it has no active event
    context and ``_safe_send`` is NOT usable:
      - onebot: completion is reported by a direct private message to the
        requesting admin (get_bots() → send_private_msg, as before);
      - qq_official: there is no active-message equivalent within quota
        reason, so the caller passes the triggering turn's bound sender
        (``_turn_sender`` snapshot). We send into its buffer and flush()
        explicitly — nobody else will, the turn is long over. Delivery is
        best-effort by platform semantics: passive replies ride the trigger
        message's window (group 5min / c2c 60min); a refresh that outlives
        the window is rejected API-side and _send_once logs + swallows it.
    """
    global _forced_refresh_running
    summary = ""
    try:
        from tools.wiki_scraper import WikiScraper
        from tools import character_detail as _cd
        from tools import bond_detail as _bd
        from lib.model_router import model_router

        scraper = WikiScraper(llm_client=model_router.flash_client)
        chars = await scraper.refresh_characters()
        if chars:
            _cd._invalidate()
        bonds = await scraper.refresh_bonds()
        if bonds:
            _bd._invalidate()
        summary = f"角色/羁绊数据库刷新完成（角色 {len(chars)} 条，羁绊 {len(bonds)} 条），卡片将自动重绘。"
        print(f"[agent_router] forced wiki refresh done: "
              f"{len(chars)} characters, {len(bonds)} bonds", file=sys.stderr)
    except Exception as e:
        summary = f"刷新失败：{type(e).__name__}，请稍后重试。"
        print(f"[agent_router] forced wiki refresh failed: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
    finally:
        _forced_refresh_running = False

    # Best-effort completion notice to the requesting admin (see docstring:
    # platform-forked delivery, never _safe_send — no live event context here).
    if summary:
        if notify_sender is not None and getattr(notify_sender, "platform", "") == "qq_official":
            try:
                await notify_sender.send_text(summary)
                await notify_sender.flush()
            except Exception as e:
                print(f"[agent_router] refresh notice send failed (official): "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
        else:
            try:
                import nonebot
                from nonebot.adapters.onebot.v11 import Bot as _Bot
                bots = nonebot.get_bots()
                bot = next((b for b in bots.values() if isinstance(b, _Bot)), None)
                if bot is not None:
                    await bot.send_private_msg(user_id=int(user_id), message=summary)
            except Exception as e:
                print(f"[agent_router] refresh notice send failed: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)


async def _handle_refresh_data_command(text: str, user_id: str) -> bool:
    """Handle /刷新角色数据 — admin-only forced wiki cache refresh.

    The character/bond caches auto-refresh weekly (Wednesday 20:00 anchor), so
    scraper fixes deployed between anchors would otherwise wait up to a week.
    This command forces an immediate background refresh (scrape + translate +
    image download + card re-render). Returns True if the command was handled.
    """
    global _forced_refresh_running
    if text.strip() not in ("/刷新角色数据", "#刷新角色数据"):
        return False
    if _perm_manager.get_role(user_id) != UserRole.ADMIN:
        await _safe_send("此命令仅限管理员使用。")
        return True
    if _forced_refresh_running:
        await _safe_send("刷新正在进行中，请稍候~")
        return True
    _forced_refresh_running = True
    # Snapshot the triggering turn's bound sender for the completion notice:
    # on qq_official this is the event-scoped buffering sender (passive-reply
    # window applies — see _forced_refresh_wiki_data docstring); on onebot it
    # is None and the notice goes out as a direct private message instead.
    asyncio.create_task(_forced_refresh_wiki_data(user_id, _turn_sender.get()))
    _notice_tail = (
        "完成后会尽力在本会话通知你（群聊里受平台 5 分钟被动回复窗口限制，"
        "刷新超时则收不到通知，可自行用 /角色详情 验证）。"
        if _current_platform.get() == "qq_official"
        else "完成后会自动通知。"
    )
    await _safe_send(
        "已开始后台刷新角色/羁绊数据库（抓取+翻译+图片下载），通常需要几分钟，"
        + _notice_tail
    )
    return True


async def _handle_personality_command(text: str, user_id: str, view: InboundMessage) -> bool:
    """Handle /personality / /人格切换 command for personality switching.

    Returns True if the command was handled.
    """
    cmd, _, args = text.partition(" ")
    if cmd not in ("/personality", "/人格切换"):
        return False

    pm = get_character_registry()
    personalities = pm.list_personalities(user_id=user_id)
    args = args.strip()
    group_id = view.group_id

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
            tag = "　[自创]" if p.get("source") == "ugc" else ""
            lines.append(f"  {p['display_name']} ({p['key']}){tag}{marker}")
        lines.append(
            "\n使用 /人格切换 <名称> 切换，例如: /人格切换 "
            f"{personalities[0]['display_name']}"
        )
        lines.append("用 /创建角色 访谈式创建你自己的角色。")
        await _safe_send("\n".join(lines))
        return True

    # /personality <name> — switch
    try:
        old_key = pm.resolve_effective_personality(user_id, group_id)
        old_display = pm.get_display_name(old_key)
        resolved = pm.set_user_personality(user_id, args)
        new_display = pm.get_display_name(resolved)
        if resolved == old_key:
            # Already this personality — no transition prompt/marker needed.
            await _safe_send(f"当前已是「{new_display}」人格，无需切换~")
            return True
        _pending_personality_switch[user_id] = (old_display, new_display)
        _personality_switch_marker[user_id] = (
            f"人格已从「{old_display}」切换为「{new_display}」，"
            f"历史回复风格不代表当前人格。"
        )
        _save_personality_markers()
        await _safe_send(
            f"已切换至「{new_display}」人格。\n"
            f"是否保留历史对话记录？\n"
            f"回复「保留」保留历史并注入切换提示词；\n"
            f"回复「清空」清除当前上下文。"
        )
    except ValueError as e:
        await _safe_send(str(e))
    except Exception as e:
        code = log_exception("personality switch failed", e)
        await _safe_send(f"人格切换失败（诊断编号 {code}），请稍后重试。如果问题持续存在，请使用 #bug 反馈。")

    return True


# ── Bond system helpers (Bond-System-Design.md §3-4) ──────────────

def _build_bond_context(user_id: str, persona_key: str, pm) -> str:
    """Assemble the L4 bond block for the character plane (P2 dynamic layer).

    角色卡解析（已解锁档案/称呼演化）与共同经历日志都汇入
    build_prompt_context。任何一步失败都降级为「只有基础羁绊状态」，绝不
    因增益功能抛错阻断角色面回复。
    """
    short_name = pm.get_short_name(persona_key)
    unlocked_text = ""
    address_line = ""
    recent = None
    try:
        card = pm.load(persona_key) or ""
        state = _bond_manager.get_state(user_id, persona_key, create=False)
        level = int((state or {}).get("level", 1))
        if card:
            unlocked_text = bond_content.unlocked_tiers_text(card, level)
            address_line = bond_content.address_directive(level)
        recent = _bond_manager.recent_events(user_id, persona_key, 3) or None
    except Exception as e:
        log_exception("bond dynamic context failed", e)
    return _bond_manager.build_prompt_context(
        user_id, persona_key, short_name,
        unlocked_text=unlocked_text,
        address_line=address_line,
        recent=recent,
    )


async def _settle_bond_turn(user_id: str, persona_key: str, sender=None,
                            is_group: bool = False):
    """好感结算钩子：split 人格每轮对话结束后累计 EXP/心情，升级时发庆祝。

    结算失败必须静默——它是增益功能，绝不能因抛错中断已生成回复的发送。
    turn_count 用于「长聊奖励」（≥long_session_turns 当日一次性加成）。
    is_group：群聊不发场景邀请（剧情场景仅私聊体验）。
    """
    try:
        try:
            turn_count = _session_manager.message_count(user_id) // 2
        except Exception:
            turn_count = 0
        result = _bond_manager.settle_turn(user_id, persona_key, turn_count=turn_count)
        # P3: 久别重逢 / 纪念日共同经历事件（一次性守卫在 bond.py 内）
        if result.get("reunion") and _bond_manager.note_reunion(user_id, persona_key):
            _bond_manager.append_event(user_id, persona_key, "久别重逢")
        milestone = _bond_manager.note_anniversary(user_id, persona_key)
        if milestone:
            _bond_manager.append_event(
                user_id, persona_key, f"相识第 {milestone} 天纪念日"
            )
        await _emit_level_ups(user_id, persona_key, result.get("level_ups", []),
                              sender=sender, is_group=is_group)
    except Exception as e:
        log_exception("bond settlement failed", e)


async def _emit_level_ups(user_id, persona_key, level_ups, sender=None,
                          is_group: bool = False):
    """升级庆祝 + 分段解锁（P3 修正）：档案 tier 发解锁提示，场景 tier 发私聊邀请。"""
    level_ups = level_ups or []
    if not level_ups:
        return
    card = ""
    try:
        card = get_character_registry().load(persona_key) or ""
    except Exception:
        card = ""
    for up in level_ups:
        lv = up.get("level")
        _bond_manager.append_event(
            user_id, persona_key,
            f"羁绊提升到了 Lv{lv}（{up.get('title', '')}）",
        )
        await _send_level_up_notice(user_id, persona_key, up, sender=sender)
        # 非场景 tier（档案/语气解禁等）：方舟式解锁提示
        for tier in bond_content.tiers_unlocking_at(card, int(lv)):
            if bond_content.is_scene_tier(tier):
                continue  # 场景走下面的邀请
            title = str(tier.get("title", "")).strip()
            _bond_manager.append_event(
                user_id, persona_key, f"解锁了新的羁绊档案「{title}」"
            )
            await _safe_send(
                f"🔓 羁绊档案解锁：Lv{lv} ·《{title}》",
                sender=sender,
            )
        # 场景 tier（P3）：该等级新解锁的场景 → 私聊邀请
        for sc in bond_content.scenes_unlocking_at(card, int(lv)):
            await _invite_scene(user_id, persona_key, sc,
                                is_group=is_group, sender=sender)


async def _invite_scene(user_id, persona_key, scene, is_group=False, sender=None):
    """升级场景邀请（P3）：仅私聊 + 未完成；每级解锁自然一次性。"""
    if is_group:
        return
    title = str(scene.get("title", "")).strip()
    lv = scene.get("level", "")
    if not title or _bond_manager.has_done_scene(user_id, persona_key, title):
        return
    short = get_character_registry().get_short_name(persona_key)
    await _safe_send(
        f"✨ 羁绊到了 Lv{lv}，{short} 好像想和你一起经历新的剧情「{title}」～\n"
        f"想现在开始的话，私聊发 /剧情 {title} 即可。",
        sender=sender,
    )


async def _finish_bond_scene(user_id: str, result: dict, sender=None):
    """场景完成 post-run 收尾（P3）：升级通知 + 纪念卡片 + 关闭会话。

    结算（+scene_exp/mark/事件日志）已在 social_tools.complete_scene 完成；
    这里只做呈现。失败静默（增益功能不阻断）。
    """
    try:
        char_id = str(result.get("char_id") or "")
        scene_title = str(result.get("scene_title") or "")
        pm = get_character_registry()
        await _emit_level_ups(
            user_id, char_id, result.get("level_ups") or [],
            sender=sender, is_group=False,
        )
        level = int(result.get("level") or 1)
        entry = {
            "char_name": pm.get_display_name(char_id) or pm.get_short_name(char_id),
            "scene_title": scene_title,
            "level": level,
            "level_title": _bond_manager.title_for(level),
            "date": time.strftime("%Y-%m-%d"),
            "memento": str(result.get("memento") or ""),
        }
        path = render_scene_card(entry)
        if path:
            await _safe_send(
                OutImage(path=path, label=f"羁绊纪念 · {scene_title}"),
                sender=sender,
            )
        try:
            _special_sessions.end_active(user_id)
        except Exception:
            pass
    except Exception as e:
        log_exception("bond scene finish failed", e)


def _scene_opening_line(char_id: str, title: str, scene: dict) -> str:
    """零 LLM 的场景开场种子消息：取脚本首个有效行作开场幕。"""
    body = str(scene.get("body") or "").strip()
    first = ""
    for ln in body.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith(("#", ">", "-")):
            first = ln
            break
    short = get_character_registry().get_short_name(char_id)
    head = f"—— 剧情《{title}》开始 ——"
    if first:
        return (f"{head}\n{first}\n\n（{short} 看向你，等待你的回应。"
                f"随时可 /结束会话 退出）")
    return (f"{head}\n{short} 轻轻拉住你的衣袖，眼里带着期待。"
            f"「我们出发吧？」\n\n（随时可 /结束会话 退出）")


async def _start_bond_scene(user_id: str, char_id: str, scene: dict) -> None:
    """启动羁绊剧情特殊会话（仅私聊；等级/已完成/会话槽守卫齐全）。"""
    title = str(scene.get("title") or "")
    try:
        lv = int(scene.get("level") or 1)
    except (TypeError, ValueError):
        lv = 1
    state = _bond_manager.get_state(user_id, char_id, create=False)
    level = int((state or {}).get("level", 1))
    if level < lv:
        await _safe_send(
            f"「{title}」需要羁绊 Lv{lv} 才能体验（当前 Lv{level}），再相处一阵子吧～"
        )
        return
    if _bond_manager.has_done_scene(user_id, char_id, title):
        await _safe_send(
            f"「{title}」你们已经经历过了，回忆已珍藏在纪念卡里～ 试试别的剧情吧。"
        )
        return
    active = _special_sessions.get_active(user_id)
    if active:
        await _safe_send(
            "你现在还有一个进行中的特殊会话，先 /结束会话 再开始剧情吧～"
        )
        return
    role = _perm_manager.get_role(user_id)
    max_sessions = _perm_manager.get_max_special_sessions(role)
    sessions = _special_sessions.list_sessions(user_id)
    if len(sessions) >= max_sessions:
        await _safe_send(
            f"开始剧情需要占用一个特殊会话槽，但你已达到上限"
            f"（{len(sessions)}/{max_sessions}）。请先 /删除会话 清理一个。"
        )
        return
    try:
        session = _special_sessions.create(
            user_id, f"剧情·{title}",
            metadata={
                "kind": "bond_scene", "char_id": char_id,
                "scene_title": title,
                "script": str(scene.get("body") or ""),
                "completed": False,
            },
        )
        _special_sessions.switch_to(user_id, session.name)
        seed = _scene_opening_line(char_id, title, scene)
        _special_sessions.add_message(user_id, "assistant", seed)
        await _safe_send(seed)
    except Exception as e:
        code = log_exception("bond scene start failed", e)
        await _safe_send(f"开始剧情失败（诊断编号 {code}），请稍后重试。")


async def _handle_scene_command(text: str, user_id: str, view) -> bool:
    """/剧情 #剧情 — 列出可用场景或启动一个（P3，仅私聊体验）。"""
    cmd, _, args = text.partition(" ")
    args = args.strip()
    if cmd not in ("/剧情", "#剧情"):
        return False
    pm = get_character_registry()
    persona_key = pm.resolve_effective_personality(user_id, view.group_id)
    try:
        card = pm.load(persona_key) or ""
    except Exception:
        card = ""
    scenes = bond_content.parse_scenes(card)
    short = pm.get_short_name(persona_key)
    if not scenes:
        await _safe_send(f"和 {short} 还没有准备好的剧情场景哦～")
        return True
    state = _bond_manager.get_state(user_id, persona_key, create=False)
    level = int((state or {}).get("level", 1))
    if not args:
        lines = [f"与 {pm.get_display_name(persona_key)} 的剧情场景："]
        for s in scenes:
            lv = int(s["level"])
            title = str(s["title"])
            if lv <= level:
                mark = ("（已完成）" if _bond_manager.has_done_scene(
                    user_id, persona_key, title) else "（可体验）")
                lines.append(f"  · Lv{lv} 《{title}》{mark}")
            else:
                lines.append(f"  · Lv{lv} 《{title}》（羁绊 Lv{lv} 解锁）")
        lines.append("\n私聊发 /剧情 <标题> 开始一段剧情。")
        await _safe_send("\n".join(lines))
        return True
    if view.is_group:
        await _safe_send("剧情场景只能在私聊里体验哦～ 请私聊我再发 /剧情。")
        return True
    scene = bond_content.get_scene(card, args)
    if not scene:
        await _safe_send(f"没有找到叫「{args}」的剧情，发 /剧情 看看列表吧～")
        return True
    await _start_bond_scene(user_id, persona_key, scene)
    return True


async def _send_level_up_notice(user_id: str, persona_key: str, up: dict, sender=None):
    """羁绊升级庆祝：角色口吻一句话（flash）+ 等级提示；LLM 失败降级固定文案。"""
    pm = get_character_registry()
    short = pm.get_short_name(persona_key)
    fallback = f"✨ 你与 {short} 的羁绊达到了 Lv{up['level']}（{up['title']}）！"
    opening = None
    try:
        persona_content = pm.load(persona_key) or ""
        if persona_content:
            system = (
                persona_content
                + "\n\n---\n你正在扮演上述角色。你与用户的羁绊刚刚提升到了新的等级，"
                  "请写一句符合角色性格的简短庆祝/感慨，表达你们的关系更近了一步。"
            )
            prompt = (
                f"羁绊提升到了 Lv{up['level']}（{up['title']}）。"
                f"请以角色口吻写一句话庆祝。要求：不超过60字；不使用markdown；只输出这句话本身。"
            )
            raw = await _model_router.flash_client.chat_completion(
                message=prompt,
                history=[{"role": "system", "content": system}],
                timeout_set=15.0,
                purpose="bond_levelup",
            )
            candidate = (raw or "").strip()
            if (
                candidate
                and not candidate.startswith(_LLM_ERROR_PREFIXES)
                and len(candidate) <= 120
                and "\n" not in candidate
            ):
                opening = candidate
    except Exception:
        opening = None
    if opening:
        await _safe_send(f"{opening}\n\n{fallback}", sender=sender)
    else:
        await _safe_send(fallback, sender=sender)


# LLM 反应失败/群聊默认使用的模板（{name} = 角色短名）
_POKE_REACTIONS = (
    "欸？{name}被你戳了一下，回头看了你一眼～",
    "{name}揉了揉被戳的地方，无奈地笑了笑。",
    "别戳啦～{name}在呢。",
    "{name}眨了眨眼：「怎么啦？」",
)


async def _handle_poke(user_id: str, group_id: str, private: bool):
    pm = get_character_registry()
    persona_key = pm.resolve_effective_personality(user_id, group_id)
    if pm.get_mode(persona_key) != "split":
        return  # 羁绊养成仅 split 人格
    short = pm.get_short_name(persona_key)

    result = _bond_manager.poke(user_id, persona_key)
    if not result.get("accepted"):
        # 每日上限：仅模板轻提示，不调 LLM
        await _safe_send(
            f"{short}：「今天已经被你戳够啦，明天再来找我玩～」",
            sender=poke_sender,
        )
        return

    _bond_manager.append_event(user_id, persona_key, "用户戳了戳你，你回应了")
    for up in result.get("level_ups", []):
        _bond_manager.append_event(
            user_id, persona_key,
            f"羁绊提升到了 Lv{up['level']}（{up['title']}）",
        )

    # 私聊用角色口吻 LLM 反应；群聊默认模板（省 token，与 revoice 策略一致）
    reaction = None
    if private:
        reaction = await _poke_reaction(persona_key, short)
    if not reaction:
        reaction = random.choice(_POKE_REACTIONS).format(name=short)
    await _safe_send(reaction, sender=poke_sender)

    for up in result.get("level_ups", []):
        await _send_level_up_notice(user_id, persona_key, up, sender=poke_sender)


async def _poke_reaction(persona_key: str, short: str):
    """In-character one-liner for a poke (flash). None → caller falls back."""
    try:
        persona_content = get_character_registry().load(persona_key) or ""
        if not persona_content:
            return None
        system = (
            persona_content
            + "\n\n---\n你正在扮演上述角色。用户刚刚「戳了戳」你（QQ 的戳一戳互动，"
              "类似轻轻碰你一下/摸头），请写一句符合角色性格的简短自然反应。"
        )
        prompt = (
            "用户戳了戳你。请以角色口吻写一句简短反应。"
            "要求：不超过40字；不使用markdown；不报任何数值；只输出这句话本身。"
        )
        raw = await _model_router.flash_client.chat_completion(
            message=prompt,
            history=[{"role": "system", "content": system}],
            timeout_set=15.0,
            purpose="poke_reaction",
        )
        candidate = (raw or "").strip()
        if (
            candidate
            and not candidate.startswith(_LLM_ERROR_PREFIXES)
            and len(candidate) <= 120
            and "\n" not in candidate
        ):
            return candidate
    except Exception:
        pass
    return None


async def _handle_bond_command(
    text: str, user_id: str, view: InboundMessage, sender=None
) -> bool:
    """Handle /好感度 #好感度 /bond — 直接展示当前角色羁绊状态，零 token。"""
    cmd = text.strip().split()[0] if text.strip() else ""
    if cmd not in ("/好感度", "#好感度", "/bond"):
        return False
    pm = get_character_registry()
    group_id = view.group_id
    persona_key = pm.resolve_effective_personality(user_id, group_id)
    display = pm.get_display_name(persona_key)
    if pm.get_mode(persona_key) != "split":
        await _safe_send(
            f"当前人格「{display}」未启用羁绊养成。\n"
            f"用 /人格切换 切换到支持养成的角色后再查询好感度哦~",
            sender=sender,
        )
        return True
    await _safe_send(
        _bond_manager.format_status(user_id, persona_key, display_name=display),
        sender=sender,
    )
    return True


async def _send_markdown_doc(path: str, label: str = "文档") -> bool:
    """Read a markdown doc and send it segmented by ``## `` sections.

    Merges short sections to fit QQ message limits (~500 chars) with a 1s delay
    between chunks. Returns True if any content was sent.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except FileNotFoundError:
        await _safe_send(f"{label}不存在，请联系管理员。")
        return False
    except Exception as e:
        await _safe_send(f"读取{label}失败: {e}")
        return False

    if not content:
        await _safe_send(f"{label}为空。")
        return False

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

    # Merge short parts to fit QQ message limits (~500 chars)
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


async def _handle_features_command(text: str, user_id: str) -> bool:
    """Handle /功能 / /features command — direct, no agent.

    Renders FEATURES.md as a feature card image and sends only the image
    (no text). Returns True if the command was handled.
    """
    cmd = text.strip().split()[0] if text.strip() else ""
    if cmd not in ("/功能", "/features"):
        return False

    features_path = os.path.join(_CONFIG_DIR, "FEATURES.md")

    try:
        card_path = await asyncio.to_thread(render_feature_card, features_path)
        if card_path:
            await _safe_send(OutImage(path=card_path, label="feature card"))
        else:
            await _safe_send("功能表为空或不存在，请联系管理员。")
    except Exception:
        await _safe_send("功能卡片渲染失败，请稍后重试。")

    return True


# 匹配 /更新日志 [d] / #更新日志 [d] / /update record [d] / /changelog [d] 等
_CHANGELOG_CMD_RE = re.compile(
    r"^\s*[/#](更新日志|更新记录|update\s+record|update\s+log|changelog)\s*(\d+)?\s*$",
    re.IGNORECASE,
)


async def _handle_changelog_command(text: str, user_id: str) -> bool:
    """Handle /更新日志 [d] / /update record [d] — direct, no agent, zero token.

    d = 返回最近多少条更新记录（缺省 3，clamp 到 1..20）。数据源为
    QQBot/data/changelog/changelog.json，由 WebUI 维护。
    Returns True if the command was handled.
    """
    m = _CHANGELOG_CMD_RE.match(text or "")
    if not m:
        return False

    from tools.changelog import get_recent, format_for_qq, DEFAULT_COUNT

    d = int(m.group(2)) if m.group(2) else DEFAULT_COUNT
    entries = get_recent(d)
    await _send_text_chunks(format_for_qq(entries, requested=d, site_url=_PUBLIC_SITE_URL))

    # 2. Render and send the changelog card (best-effort; text already sent)
    try:
        card_path = await asyncio.to_thread(
            render_changelog_card, entries, site_url=_PUBLIC_SITE_URL)
        if card_path:
            await asyncio.sleep(1.0)
            await _safe_send(OutImage(path=card_path, label="changelog card"))
    except Exception:
        pass  # 卡片是锦上添花，渲染/发送失败绝不影响文本回复
    return True


async def _handle_toggle_command(text: str, user_id: str, view: InboundMessage) -> bool:
    """Handle /toggle command for group feature management.

    Only works in group chats, only for superusers.
    Returns True if the command was handled.
    """
    cmd, _, args = text.partition(" ")
    if cmd not in ("/toggle", "#toggle"):
        return False

    # Only in group chats
    if not view.is_group:
        await _safe_send("此命令仅在群聊中可用。")
        return True

    group_id = view.group_id
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
        pm = get_character_registry()
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
        pm = get_character_registry()
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
            # owner_id=user_id lets a superuser bind their own UGC characters
            resolved = pm.set_group_personality(group_id, rest, owner_id=user_id)
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

    # ── /创建角色 ──────────────────────────────────────────────
    # 访谈式 UGC 角色创作（Bond-System-Design.md §5.1, P1）。占用一个特殊会话
    # 槽，metadata.kind = char_creation 触发 agent L4 访谈指令 + create_character。
    if cmd in ("/创建角色", "#创建角色"):
        registry = get_character_registry()
        role = _perm_manager.get_role(user_id)
        # Already in a creation interview?
        active = _special_sessions.get_active(user_id)
        if active and (active.metadata or {}).get("kind") == "char_creation":
            await _safe_send(
                "你已经在一个角色创作访谈中了，直接回答我的问题即可；"
                "想放弃请用 /结束会话 退出。"
            )
            return True
        # Character quota
        max_chars = _perm_manager.get_max_characters(role)
        owned = registry.list_ugc(user_id)
        if len(owned) >= max_chars:
            names = "、".join(registry.get_display_name(m["char_id"]) for m in owned) or "无"
            await _safe_send(
                f"你的自创角色数量已达上限（{len(owned)}/{max_chars}）：{names}。\n"
                "请先用 /删除角色 <名称> 清理，再创建新角色。"
            )
            return True
        # Special-session slot quota (creation session occupies one)
        max_sessions = _perm_manager.get_max_special_sessions(role)
        sessions = _special_sessions.list_sessions(user_id)
        if len(sessions) >= max_sessions:
            await _safe_send(
                f"创建角色需要占用一个特殊会话槽，但你已达到上限（{len(sessions)}/{max_sessions}）。\n"
                "请先用 /删除会话 <名称> 清理一个会话后再试。"
            )
            return True
        try:
            session = _special_sessions.create(
                user_id, "角色创作", metadata={"kind": "char_creation"}
            )
            _special_sessions.switch_to(user_id, session.name)
            _temp_session_files.pop(user_id, None)
            seed = (
                "开始创作你的专属角色吧！我会逐项问你一些设定，"
                "每项都可以直接回答；也可以说「跳过」用默认值，或「随机」让我替你拟一个。\n\n"
                f"（当前自创角色 {len(owned)}/{max_chars}；随时可用 /结束会话 放弃创作）\n\n"
                "第一项：给你的角色起个名字吧（不超过 24 个字）。"
            )
            # Record the greeting as the assistant's first interview turn so the
            # session context stays coherent (zero-LLM seeding).
            _special_sessions.add_message(user_id, "assistant", seed)
            await _safe_send(seed)
        except Exception as e:
            code = log_exception("create-character session failed", e)
            await _safe_send(f"启动角色创作失败（诊断编号 {code}），请稍后重试。")
        return True

    # ── /删除角色 <名称> ───────────────────────────────────────
    # 删除自己的 UGC 角色（二次确认）。保留 bond 好感数据（重逢仍在）。
    if cmd in ("/删除角色", "#删除角色"):
        registry = get_character_registry()
        if not args:
            owned = registry.list_ugc(user_id)
            if not owned:
                await _safe_send("你还没有自创角色。用 /创建角色 访谈式创建一个吧~")
                return True
            lines = ["你的自创角色:"]
            for m in owned:
                lines.append(f"  {registry.get_display_name(m['char_id'])} ({m['char_id']})")
            lines.append("\n用法: /删除角色 <名称>")
            await _safe_send("\n".join(lines))
            return True

        confirm_key = f"确认删除角色 {args}"
        if text == confirm_key:
            pending = _pending_char_delete_confirm.get(user_id)
            if pending and time.time() < pending[1] and pending[0] == args:
                char_id = registry.resolve_name(args, user_id=user_id)
                _pending_char_delete_confirm.pop(user_id, None)
                if not (char_id and registry.is_ugc(char_id)
                        and (registry.get_meta(char_id) or {}).get("author") == user_id):
                    await _safe_send(f"未找到你的自创角色「{args}」。")
                    return True
                display = registry.get_display_name(char_id)
                registry.delete(char_id)
                purged = registry.purge_references(char_id)
                note = f"\n（已解除 {purged} 处人格设置引用）" if purged else ""
                await _safe_send(
                    f"已删除自创角色「{display}」。{note}\n"
                    "该角色的好感度记录已保留，若日后重建同名角色不会自动继承。"
                )
                return True
            _pending_char_delete_confirm.pop(user_id, None)
            await _safe_send("确认已超时（60秒）或已失效，请重新发起 /删除角色。")
            return True

        # First call — resolve + request confirmation
        char_id = registry.resolve_name(args, user_id=user_id)
        if not (char_id and registry.is_ugc(char_id)
                and (registry.get_meta(char_id) or {}).get("author") == user_id):
            await _safe_send(
                f"未找到你的自创角色「{args}」。\n"
                "（只能删除你自己创建的角色；内置人格请用 /人格切换 切换。）"
            )
            return True
        display = registry.get_display_name(char_id)
        _pending_char_delete_confirm[user_id] = (args, time.time() + 60)
        await _safe_send(
            f"确认删除自创角色「{display}」？此操作不可撤销（好感度记录会保留）。\n"
            f"请回复「确认删除角色 {args}」来执行（60秒内有效）。"
        )
        return True

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

    # ── /帮助 — 命令列表 + 帮助卡片 ─────────────────────────────
    if cmd in ("/帮助", "#帮助", "/help", "#help", "/命令", "#命令"):
        # 1. Send the HELP.md text (segmented)
        await _send_markdown_doc(_HELP_MD_PATH, label="帮助文档")
        # 2. Render and send the help card (best-effort; text already sent)
        try:
            card_path = await asyncio.to_thread(render_help_card, _HELP_MD_PATH)
            if card_path:
                await _safe_send(OutImage(path=card_path, label="help card"))
        except Exception:
            pass
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
        if _personality_switch_marker.pop(user_id, None) is not None:
            _save_personality_markers()
        await _safe_send("已清除对话上下文，开始新对话~")
    elif command == "/status":
        status = agent.get_status()
        tool_list = "\n  ".join(status["tool_names"])
        _short = get_character_registry().get_short_name(
            get_character_registry().get_user_personality(user_id)
        )
        await _safe_send(
            f"{_short} 状态:\n"
            f"  活跃会话: {status['active_sessions']}\n"
            f"  已注册工具 ({status['tools_registered']}):\n  {tool_list}"
        )


async def _send_response(response: str, sender=None):
    """Send response, splitting long messages into chunks with rate limiting.

    Args:
        response: The response text to send.
        sender: Optional Sender to use. Defaults to default_sender.
    """
    if not response:
        return

    # Append disclaimer to every agent response
    _pm = get_character_registry()
    _persona = _current_personality.get() or _pm.get_default()
    _short = _pm.get_short_name(_persona)
    disclaimer = f"\n\n{_short} 的回答并非总是准确无误，请理性判断。"
    response += disclaimer

    # Shorter chunks + longer delays to avoid QQ rate limiting
    max_len = 300
    if len(response) <= max_len:
        await _safe_send(response, sender=sender)
    else:
        # Split on sentence boundaries when possible
        chunks = _split_text(response, max_len)
        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            await _safe_send(chunk.strip(), sender=sender)
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
