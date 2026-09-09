"""
Agent Playground — isolated, tool-less agent runs for the panel.

v1 (this file): **pure conversation, no tools.** Used to validate prompt /
personality / model changes without touching production data.

Isolation guarantees
--------------------
* DeepSeekClient is built with explicit api_key/api_base/model read straight
  from models_settings.json → the early-return in its __init__ skips
  ``get_driver()``, so the panel process never needs NoneBot initialised.
* Agent gets an EMPTY ToolRegistry (no tools), an in-memory SessionManager
  (no persistence_dir → nothing written to data/sessions/), and
  memory/profile/hardware/workspace/special = None.
* user_id is fixed to "playground"; the agent contextvars are set inside the
  request coroutine, which FastAPI runs in its own asyncio task → the values
  are task-local and never leak to other requests or to the bot.
* Token usage is attributed via set_usage_context("playground", "", purpose)
  so playground calls are identifiable/filterable in the ledger.

The panel NEVER imports QQBot/plugins/* (module-level nonebot matchers need a
driver). It only imports agent.* / lib.* which are driver-free.
"""

import asyncio
import json
from pathlib import Path

from . import config

MODELS_FILE = config.QQBOT_DIR / "config" / "models_settings.json"
CONFIG_DIR = config.QQBOT_DIR / "agent" / "config"
PERSONALITIES_DIR = CONFIG_DIR / "personalities"

_TIER_KEY = {"reasoning": "REASONING_MODEL", "flash": "FLASH_MODEL"}

# Serialise playground runs so concurrent requests can't interleave contextvars
# within the same task tree (defence-in-depth; FastAPI already isolates tasks).
_LOCK = asyncio.Lock()


# ── Model / personality discovery ────────────────────────────────

def available_models() -> list:
    """[{tier, model, api_base}] for the tiers present in models_settings.json."""
    try:
        data = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for tier, key in _TIER_KEY.items():
        block = data.get(key)
        if isinstance(block, dict) and block.get("model"):
            out.append({"tier": tier, "model": block["model"],
                        "api_base": block.get("api_base", "")})
    return out


def available_personalities() -> list:
    try:
        return sorted(p.stem for p in PERSONALITIES_DIR.glob("*.md"))
    except OSError:
        return []


def _load_model_block(tier: str) -> dict:
    try:
        data = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"_error": f"models_settings.json 读取失败: {e}"}
    key = _TIER_KEY.get(tier, "REASONING_MODEL")
    block = data.get(key) or data.get("REASONING_MODEL") or {}
    if not isinstance(block, dict):
        return {"_error": f"{key} 不是对象"}
    return block


# ── Agent construction (driver-free) ─────────────────────────────

def _build_agent(client):
    from agent.agent import Agent
    from agent.tool_registry import ToolRegistry
    from agent.session import SessionManager
    return Agent(
        deepseek_client=client,
        tool_registry=ToolRegistry(),          # empty → no tools exposed
        config_dir=str(CONFIG_DIR),
        session_manager=SessionManager(),       # in-memory (no persistence_dir)
        memory_system=None,
        profile_manager=None,
        hardware_detector=None,
        workspace_manager=None,
        special_session_manager=None,
    )


def _set_context(personality: str, role: str):
    """Set the agent contextvars for an isolated playground turn."""
    import agent.context as ctx
    ctx._current_user_id.set("playground")
    ctx._current_user_role.set(role or "admin")
    ctx._current_personality.set(personality or "")
    # A throwaway workspace path; no tools run so it's never touched.
    try:
        ctx._current_user_workspace.set("/tmp/playground-workspace")
    except Exception:
        pass


# ── System-prompt preview (no network) ───────────────────────────

def preview_system_prompt(personality: str = "", role: str = "admin") -> dict:
    """Build the exact system prompt the agent would use, without any API call."""
    try:
        from lib.deepseek_client import DeepSeekClient
    except Exception as e:
        return {"error": f"导入失败: {e}"}
    # Client construction must not hit the network; pass explicit dummies so the
    # early-return path is taken (no get_driver).
    client = DeepSeekClient(api_key="preview", api_base="http://localhost", model="preview")
    agent = _build_agent(client)
    _set_context(personality, role)
    try:
        session = agent.sessions.get_or_create("playground")
        messages = agent._build_messages(session, "", None, role_hint=role)
        system = messages[0]["content"] if messages else ""
    except Exception as e:
        return {"error": f"构建系统提示词失败: {e}"}
    return {
        "personality": personality or "(none)",
        "role": role,
        "length": len(system),
        "content": system,
    }


# ── Full playground run (network) ────────────────────────────────

async def run_playground(message: str, personality: str = "", tier: str = "reasoning",
                         role: str = "admin", history: list | None = None) -> dict:
    """Run one tool-less conversation turn and return the reply + usage."""
    message = (message or "").strip()
    if not message:
        return {"error": "消息为空"}

    block = _load_model_block(tier)
    if "_error" in block:
        return {"error": block["_error"]}
    api_key = block.get("api_key")
    api_base = block.get("api_base")
    model = block.get("model")
    if not api_key or not api_base:
        return {"error": "models_settings.json 缺少 api_key / api_base"}

    async with _LOCK:
        try:
            from lib.deepseek_client import DeepSeekClient
            from lib.token_ledger import set_usage_context
        except Exception as e:
            return {"error": f"导入失败: {e}"}

        client = DeepSeekClient(api_key=api_key, api_base=api_base, model=model)
        agent = _build_agent(client)
        _set_context(personality, role)
        # Attribute token usage to the playground so it's filterable.
        try:
            set_usage_context("playground", "", "playground")
        except Exception:
            pass

        # Seed prior turns into the in-memory session (optional multi-turn).
        try:
            if history:
                session = agent.sessions.get_or_create("playground")
                for turn in history[-20:]:
                    role_ = "user" if turn.get("role") == "user" else "assistant"
                    content = turn.get("content", "")
                    if content:
                        session.context.append({"role": role_, "content": content})
                agent.sessions.update("playground", session)
        except Exception:
            pass

        t0 = asyncio.get_event_loop().time()
        try:
            reply = await agent.run(message, user_id="playground", user_role=role)
        except Exception as e:
            return {"error": f"对话失败: {e}"}
        elapsed = round(asyncio.get_event_loop().time() - t0, 2)

    return {
        "ok": True,
        "reply": reply,
        "model": model,
        "tier": tier,
        "personality": personality or "(none)",
        "elapsed_sec": elapsed,
    }
