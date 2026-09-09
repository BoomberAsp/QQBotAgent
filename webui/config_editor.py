"""
Config hot-management for the panel.

Editable targets:
  prompts        QQBot/agent/config/**/*.md      → hot-reloaded by the
                                                   bot-side watcher (5 s)
  permissions    QQBot/.env  SUPERUSERS/VIP_USERS → effective immediately
                                                   (PermissionManager re-reads)
  models         QQBot/config/models_settings.json → needs a NoneBot restart
  group_features QQBot/data/group_features.json   → effective on next message
                                                   (GroupFeatures.refresh)
  personality    QQBot/data/personality_config.json + group_personality.json

Every write is backed up to webui/data/backups/configs/ first.
API keys in models_settings.json are masked on read; a masked value
submitted back is replaced with the stored original (placeholder merge).
"""

import json
import re
import shutil
import time
from pathlib import Path

from . import config

CONFIG_DIR = config.QQBOT_DIR / "agent" / "config"
ENV_FILE = config.QQBOT_DIR / ".env"
MODELS_FILE = config.QQBOT_DIR / "config" / "models_settings.json"
GROUP_FEATURES_FILE = config.QQBOT_DIR / "data" / "group_features.json"
PERSONALITY_FILE = config.QQBOT_DIR / "data" / "personality_config.json"
GROUP_PERSONALITY_FILE = config.QQBOT_DIR / "data" / "group_personality.json"

BACKUPS = config.BACKUP_DIR / "configs"

MAX_SIZE = 512 * 1024   # refuse to load/write anything bigger


# ── helpers ───────────────────────────────────────────────────────

def _backup(src: Path) -> str | None:
    try:
        BACKUPS.mkdir(parents=True, exist_ok=True)
        dst = BACKUPS / f"{src.parent.name}_{src.name}_{int(time.time())}"
        shutil.copy2(src, dst)
        return str(dst)
    except OSError:
        return None


def _resolve_config_path(name: str) -> Path | None:
    """Resolve a prompt name (may contain '/') safely inside CONFIG_DIR."""
    if not name or name.startswith(("/", "\\")) or ".." in name.split("/"):
        return None
    p = (CONFIG_DIR / name).resolve()
    try:
        p.relative_to(CONFIG_DIR.resolve())
    except ValueError:
        return None
    return p


# ── Prompts (markdown) ────────────────────────────────────────────

def prompts_list() -> list:
    out = []
    for fp in sorted(CONFIG_DIR.rglob("*.md")):
        st = fp.stat()
        out.append({
            "name": str(fp.relative_to(CONFIG_DIR)),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        })
    return out


def prompt_read(name: str) -> dict:
    p = _resolve_config_path(name)
    if p is None or p.suffix != ".md":
        return {"error": "非法文件名"}
    if not p.exists():
        return {"error": "文件不存在"}
    try:
        return {"name": name, "content": p.read_text(encoding="utf-8"),
                "mtime": int(p.stat().st_mtime)}
    except (OSError, UnicodeDecodeError) as e:
        return {"error": str(e)}


def prompt_write(name: str, content: str) -> dict:
    p = _resolve_config_path(name)
    if p is None or p.suffix != ".md":
        return {"error": "非法文件名"}
    if len(content.encode("utf-8")) > MAX_SIZE:
        return {"error": "内容过大"}
    bak = _backup(p) if p.exists() else None
    try:
        tmp = p.with_suffix(".md.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        return {"error": str(e)}
    return {"ok": True, "backup": bak,
            "note": "bot 侧 watcher 将在 5 秒内热重载"}


# ── Permissions (.env) ────────────────────────────────────────────

_QQ_ID_LIST = re.compile(r"^\d+(,\d+)*$")


def _validate_id_list(value: str) -> bool:
    v = value.strip()
    if not v:
        return True  # empty list allowed
    # tolerate the ['a','b'] python-literal style used by nonebot SUPERUSERS
    if v.startswith("[") and v.endswith("]"):
        try:
            items = json.loads(v.replace("'", '"'))
            return all(re.fullmatch(r"\d+", str(i).strip()) for i in items)
        except json.JSONDecodeError:
            return False
    return bool(_QQ_ID_LIST.fullmatch(v.replace(" ", "")))


def permissions_read() -> dict:
    env = config._BOT_ENV  # dotenv_values snapshot, refreshed below
    fresh = {}
    try:
        from dotenv import dotenv_values
        fresh = dotenv_values(ENV_FILE)
    except Exception:
        fresh = env
    return {
        "SUPERUSERS": fresh.get("SUPERUSERS", ""),
        "VIP_USERS": fresh.get("VIP_USERS", ""),
        "note": "PermissionManager 每次调用都会重读 .env，保存后立即生效",
    }


def permissions_write(superusers: str, vip_users: str) -> dict:
    for name, val in (("SUPERUSERS", superusers), ("VIP_USERS", vip_users)):
        if not _validate_id_list(val):
            return {"error": f"{name} 格式非法：应为逗号分隔的 QQ 号，或 ['123','456'] 形式"}
    if not ENV_FILE.exists():
        return {"error": ".env 不存在"}
    _backup(ENV_FILE)
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        updated = {"SUPERUSERS": False, "VIP_USERS": False}
        for i, ln in enumerate(lines):
            for key, val in (("SUPERUSERS", superusers), ("VIP_USERS", vip_users)):
                if ln.startswith(f"{key}="):
                    lines[i] = f"{key}={val.strip()}"
                    updated[key] = True
        for key, val in (("SUPERUSERS", superusers), ("VIP_USERS", vip_users)):
            if not updated[key]:
                lines.append(f"{key}={val.strip()}")
        tmp = ENV_FILE.with_suffix(".env.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(ENV_FILE)
    except OSError as e:
        return {"error": str(e)}
    return {"ok": True, "note": "已保存，权限变更立即生效"}


# ── Models (models_settings.json — contains live API keys) ───────

def _mask(key: str) -> str:
    if not key:
        return ""
    return key[:6] + "***" + key[-4:] if len(key) > 12 else "***"


def _mask_tree(obj):
    if isinstance(obj, dict):
        return {k: (_mask(v) if k == "api_key" and isinstance(v, str) else _mask_tree(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_tree(v) for v in obj]
    return obj


def _unmask_merge(new, old):
    """Replace masked placeholder values ('***') with the stored originals."""
    if isinstance(new, dict) and isinstance(old, dict):
        return {k: _unmask_merge(v, old.get(k)) for k, v in new.items()}
    if isinstance(new, list) and isinstance(old, list):
        return [_unmask_merge(n, o) for n, o in zip(new, old)] if len(new) == len(old) else new
    if isinstance(new, str) and "***" in new:
        return old if isinstance(old, str) else new
    return new


def models_read() -> dict:
    if not MODELS_FILE.exists():
        return {"error": "models_settings.json 不存在"}
    try:
        data = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"error": f"JSON 损坏: {e}"}
    return {
        "content": json.dumps(_mask_tree(data), ensure_ascii=False, indent=2),
        "note": "api_key 已脱敏；保存时保留 *** 占位的字段将维持原值。"
                "修改模型配置后需重启 NoneBot 才生效。",
    }


def models_write(raw: str) -> dict:
    if len(raw) > MAX_SIZE:
        return {"error": "内容过大"}
    try:
        new = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": f"JSON 语法错误: {e}"}
    if not isinstance(new, dict):
        return {"error": "顶层必须是 JSON 对象"}
    old = {}
    if MODELS_FILE.exists():
        try:
            old = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            old = {}
    merged = _unmask_merge(new, old)
    _backup(MODELS_FILE)
    try:
        tmp = MODELS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(MODELS_FILE)
    except OSError as e:
        return {"error": str(e)}
    return {"ok": True, "note": "已保存 — 需重启 NoneBot 生效"}


# ── JSON configs: group_features / personality ───────────────────

def json_config_read(path: Path, default) -> dict:
    if not path.exists():
        return {"content": json.dumps(default, ensure_ascii=False, indent=2),
                "exists": False}
    try:
        raw = path.read_text(encoding="utf-8")
        json.loads(raw)  # validate
    except (OSError, json.JSONDecodeError) as e:
        return {"error": str(e)}
    return {"content": raw, "exists": True}


def json_config_write(path: Path, raw: str) -> dict:
    if len(raw) > MAX_SIZE:
        return {"error": "内容过大"}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": f"JSON 语法错误: {e}"}
    if path.exists():
        _backup(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        return {"error": str(e)}
    return {"ok": True}


def group_features_read() -> dict:
    r = json_config_read(GROUP_FEATURES_FILE, {})
    r["note"] = "群功能开关，bot 每条消息都会 refresh()，保存后下条消息即生效"
    return r


def personality_read() -> dict:
    return {
        "personality": json_config_read(PERSONALITY_FILE, {"default": "assistant"}),
        "group_personality": json_config_read(GROUP_PERSONALITY_FILE, {}),
        "available": sorted(p.stem for p in
                            (CONFIG_DIR / "personalities").glob("*.md")),
    }
