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
        "label": "NoneBot (机器人)",
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
