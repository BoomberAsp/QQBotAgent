"""
WebUI panel configuration — paths, ports, managed-process command table.

Everything here is overridable via environment variables so the panel can
be adapted to a different deployment without code changes.
"""

import os
import sys
from pathlib import Path

from dotenv import dotenv_values

# ── Paths ─────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent          # repo root
QQBOT_DIR = ROOT / "QQBot"
WEBUI_DIR = ROOT / "webui"
WEBUI_DATA = WEBUI_DIR / "data"
LOG_DIR = WEBUI_DATA / "logs"
BACKUP_DIR = WEBUI_DATA / "backups"
PID_DIR = WEBUI_DATA

BOT_LOG_DIR = QQBOT_DIR / "logs"                       # bot.py loguru sink

for _d in (WEBUI_DATA, LOG_DIR, BACKUP_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Make QQBot/ importable (agent.*, lib.*, tools.*) — the panel never
# imports QQBot/plugins/* (module-level nonebot matchers need a driver).
_QQBOT_STR = str(QQBOT_DIR)
if _QQBOT_STR not in sys.path:
    sys.path.insert(0, _QQBOT_STR)

# ── Bot .env values (read-only for the panel) ─────────────────────

_BOT_ENV = dotenv_values(QQBOT_DIR / ".env")


def bot_env(key: str, default: str = "") -> str:
    return str(_BOT_ENV.get(key, os.getenv(key, default)) or default)


USER_DATA_ROOT = Path(bot_env("USER_DATA_ROOT", str(ROOT / "data" / "users")))

# 角色卡 / 羁绊 / 多模态资产（与 bot 侧同源约定）。
# ⚠️ 面板进程**不调用 load_dotenv**（只用 dotenv_values 读 _BOT_ENV），所以
# os.environ 里没有 CHARACTERS_DIR —— 必须在这里显式解析，并显式传参给
# CharacterRegistry(characters_dir=...)。否则会退回系统盘的空目录
# QQBot/data/characters：列表是空的，写还会写到错误的位置。
# 同源处：QQBot/agent/character_registry.py:74-78、QQBot/agent/turn.py:222-225。
CHARACTERS_DIR = Path(bot_env("CHARACTERS_DIR",
                              str(QQBOT_DIR / "data" / "characters")))
BONDS_DIR = Path(bot_env("BONDS_DIR", str(QQBOT_DIR / "data" / "bonds")))
ASSETS_DIR = Path(bot_env("ASSETS_DIR", str(QQBOT_DIR / "data" / "assets")))

# 同时写进 os.environ：agent.* 里有一部分代码路径读的是 os.environ 而不是
# dotenv_values —— 典型例子是 `get_character_registry()` 的默认 characters_dir，
# 而 `agent._build_messages` 的 L2 注入正是走那个单例。不补这一步，
# 「注入预览」会预览不到 UGC 卡（读到的却是系统盘上的空目录）。
# setdefault：显式环境变量优先，不覆盖部署者的选择。
for _key, _path in (("CHARACTERS_DIR", CHARACTERS_DIR),
                    ("BONDS_DIR", BONDS_DIR),
                    ("ASSETS_DIR", ASSETS_DIR)):
    os.environ.setdefault(_key, str(_path))

# gamedata（解包产物）目录：与 lib/gamedata_source.default_dir() 同源的解析
# 优先级 —— QQBot/.env 的 GAMEDATA_DIR，其次 USER_DATA_ROOT/gamedata。面板
# 进程不 load_dotenv，而 gamedata_source 读的是 os.environ：不注入的话，
# wiki 页的「解包」合并/待补录增强会静默回落到仓库内空目录（表现为
# H806 蜜拉贝儿这类 wiki 未收录角色在面板上不可见）。两处 .env 都没配时
# 不注入，保留 gamedata_source 自己的仓库内回落（开发机行为不变）。
_GAMEDATA_ENV = bot_env("GAMEDATA_DIR", "")
if not _GAMEDATA_ENV:
    _UDR = bot_env("USER_DATA_ROOT", "")
    if _UDR:
        _GAMEDATA_ENV = str(Path(_UDR) / "gamedata")
if _GAMEDATA_ENV:
    os.environ.setdefault("GAMEDATA_DIR", _GAMEDATA_ENV)
GAMEDATA_DIR = Path(_GAMEDATA_ENV) if _GAMEDATA_ENV else None

# 角色卡版本快照根目录（每次保存前全量备份，见 webui/character_admin.py）
CHARACTER_BACKUP_DIR = BACKUP_DIR / "characters"

# 公开使用站点（bot.oneweblog.cn）：面板每次保存更新记录时向 PUBLIC_SITE_DIR
# 导出脱敏 changelog.json；nginx root 指向该目录即完成发布（见 docs/bot-site-nginx.md）。
# 与 USER_DATA_ROOT 同经 bot_env 读取 —— 部署专属路径（如 /var/www/bot-oneweblog）写在
# gitignore 的 QQBot/.env 里，不硬编码进版本库脚本；缺省回落到仓库内 public_site。
PUBLIC_SITE_DIR = Path(bot_env("WEBUI_PUBLIC_SITE_DIR", str(ROOT / "public_site")))
PUBLIC_SITE_BASE_URL = bot_env("WEBUI_PUBLIC_SITE_URL", "https://bot.oneweblog.cn")

# ── Panel server ──────────────────────────────────────────────────

HOST = os.environ.get("WEBUI_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEBUI_PORT", "8090"))

# ── Python used to launch managed child processes ─────────────────

VENV_PYTHON = os.environ.get(
    "WEBUI_VENV_PYTHON",
    os.path.expanduser("~/.virtualenvs/QQBotAgent/bin/python"),
)
# `nb` CLI lives next to the venv python (…/bin/nb)
VENV_NB = os.environ.get(
    "WEBUI_VENV_NB",
    str(Path(VENV_PYTHON).parent / "nb"),
)

# ── Managed processes ─────────────────────────────────────────────
# Command whitelist. ``method`` selects how the process is controlled:
#   "popen"  — panel spawns it directly (PID file + start_new_session)
#   "docker" — controlled through docker compose commands
#
# VERIFIED on the deploy server (2026-09): production runs the bot as
#   cd QQBot && nb run
# `nb run` reads QQBot/pyproject.toml (plugin_dirs=["plugins"]) and the
# cwd=QQBot is what makes `from agent.agent import Agent` / lib.* / tools.*
# importable; port 8081 comes from QQBot/.env (HOST/PORT). Running
# `python bot.py` from the repo root FAILS (ModuleNotFoundError: 'agent').
# Override via WEBUI_NONEBOT_CMD / WEBUI_NONEBOT_CWD if a deployment differs.

_NONEBOT_CMD = os.environ.get("WEBUI_NONEBOT_CMD")
PROCESSES = {
    "nonebot": {
        "label": "NoneBot (OneBot + QQ官方)",
        "method": "popen",
        "cmd": _NONEBOT_CMD if _NONEBOT_CMD else [VENV_NB, "run"],
        "shell": bool(_NONEBOT_CMD),
        "cwd": os.environ.get("WEBUI_NONEBOT_CWD", str(QQBOT_DIR)),
        "log_file": str(LOG_DIR / "nonebot.log"),
        # pgrep patterns used for adopting processes started outside the panel.
        # `nb run` is a launcher: it spawns a child `python3 -c "...load_from_toml..."`
        # worker that actually binds port 8081, so match both (else stopping an
        # adopted bot orphans the worker). bot.py kept as a legacy fallback.
        "match": [r"bin/nb run", r"nonebot\.load_from_toml", r"python.*bot\.py$"],
    },
    "napcat": {
        "label": "NapCat (QQ 协议)",
        "method": "popen",
        "cmd": os.environ.get(
            "WEBUI_NAPCAT_CMD",
            "xvfb-run -a ~/Napcat/opt/QQ/qq --no-sandbox",
        ),
        "shell": True,
        "cwd": os.path.expanduser("~"),
        "log_file": str(LOG_DIR / "napcat.log"),
        "match": [r"QQ/qq"],
    },
    "searxng": {
        "label": "SearXNG (搜索)",
        "method": "docker",
        "container": "searxng",
        "compose_cwd": str(ROOT),
    },
}

# ── NapCat 登录二维码 ──────────────────────────────────────────────
# NapCat 需要扫码登录时，会把二维码 PNG 写到 cachePath/qrcode.png，并自带
# 一个 WebUI（默认 6099 端口）暴露 /QQLogin/CheckLoginStatus 判断登录态。
# 全部可用环境变量覆盖，缺省匹配当前部署路径（见 config.py 顶部约定）。

_NAPCAT_HOME = Path(os.path.expanduser(
    os.environ.get("NAPCAT_HOME", "~/Napcat/opt/QQ/resources/app/app_launcher/napcat")))
NAPCAT_QRCODE_PATH = Path(os.environ.get(
    "WEBUI_NAPCAT_QRCODE", str(_NAPCAT_HOME / "cache" / "qrcode.png")))
NAPCAT_WEBUI_CONFIG = Path(os.environ.get(
    "WEBUI_NAPCAT_WEBUI_CONFIG", str(_NAPCAT_HOME / "config" / "webui.json")))
NAPCAT_HTTP_BASE = os.environ.get("NAPCAT_HTTP_BASE", "http://127.0.0.1:6099")


# ── Auth ──────────────────────────────────────────────────────────

AUTH_FILE = WEBUI_DATA / "auth.json"
SESSION_TTL = int(os.environ.get("WEBUI_SESSION_TTL", "86400"))   # 24h
MAX_LOGIN_FAILS = 5
LOCK_SECONDS = 300

# ── Log sources for the log viewer ────────────────────────────────


def log_sources() -> dict:
    """Map source name -> file path (or special handler key)."""
    sources = {
        "nonebot": str(LOG_DIR / "nonebot.log"),
        "napcat": str(LOG_DIR / "napcat.log"),
        "webui": str(LOG_DIR / "webui.log"),
        "searxng": "docker:searxng",
    }
    # Prefer the bot's own loguru sink when present (newest day file wins)
    try:
        bot_logs = sorted(BOT_LOG_DIR.glob("bot_*.log"))
        if bot_logs:
            sources["nonebot"] = str(bot_logs[-1])
    except OSError:
        pass
    return sources
