"""
Config hot-management for the panel.

Editable targets:
  prompts        QQBot/agent/config/**/*.md      → hot-reloaded by the
                                                   bot-side watcher (5 s)
  permissions    QQBot/.env identity tables       → effective immediately
                 OneBot route: SUPERUSERS / VIP_USERS (QQ numbers)
                 QQ official route: QQ_OFFICIAL_SUPERUSERS /
                 QQ_OFFICIAL_VIP_USERS (opaque alphanumeric openids)
                 (PermissionManager re-reads .env on every get_role() call;
                  it picks the table by the turn's platform contextvar)
  models         QQBot/config/models_settings.json → hot-reloaded by the
                 chat sections: bot-side watcher (~10 s)
                 media sections (IMAGE_GEN_MODEL/TTS_MODEL): fresh-read
                 per call by lib/media_gen.py (max_concurrency latches
                 at first use → needs bot restart)
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


# ── Permissions (.env — per-platform identity tables) ────────────
#
# Since the QQ official bot route (plugins/qq_official.py), the bot keeps
# TWO independent identity tables in .env — PermissionManager.get_role()
# picks one by the turn's platform:
#   onebot      → SUPERUSERS / VIP_USERS               (QQ numbers, digits)
#   qq_official → QQ_OFFICIAL_SUPERUSERS / QQ_OFFICIAL_VIP_USERS
#                                                   (opaque alphanumeric openids)
# QQ_BOTS (appid+secret) is deliberately NOT editable here: it contains
# credentials and is only read at bot startup — changes need a restart
# via the process manager, not a hot edit.

_QQ_ID_LIST = re.compile(r"^\d+(,\d+)*$")
# openid: opaque token issued by the official API (letters/digits/-/_).
# Length bounds are generous on purpose — the format is platform-owned.
_OPENID_TOKEN = r"[A-Za-z0-9_-]{4,64}"
_OPENID_LIST = re.compile(rf"^{_OPENID_TOKEN}(,{_OPENID_TOKEN})*$")


def _validate_list(value: str, item_re: str) -> bool:
    """Comma-separated list, or JSON/python-literal ['a','b'] style.
    Empty value = empty list = allowed (clears the table)."""
    v = value.strip()
    if not v:
        return True
    if v.startswith("[") and v.endswith("]"):
        try:
            items = json.loads(v.replace("'", '"'))
        except json.JSONDecodeError:
            return False
        return isinstance(items, list) and all(
            re.fullmatch(item_re, str(i).strip()) for i in items)
    return bool(re.fullmatch(rf"{item_re}(,{item_re})*", v.replace(" ", "")))


def _validate_id_list(value: str) -> bool:
    return _validate_list(value, r"\d+")


def _validate_openid_list(value: str) -> bool:
    return _validate_list(value, _OPENID_TOKEN)


# key → (validator, human-readable format hint for error messages)
_PERMISSION_FIELDS: dict[str, tuple] = {
    "SUPERUSERS": (_validate_id_list, "逗号分隔的 QQ 号，或 ['123','456'] 形式"),
    "VIP_USERS": (_validate_id_list, "逗号分隔的 QQ 号，或 ['123','456'] 形式"),
    "QQ_OFFICIAL_SUPERUSERS": (_validate_openid_list,
                               "逗号分隔的 openid（字母/数字/-/_），或 ['xx','yy'] 形式"),
    "QQ_OFFICIAL_VIP_USERS": (_validate_openid_list,
                              "逗号分隔的 openid（字母/数字/-/_），或 ['xx','yy'] 形式"),
}


def permissions_read() -> dict:
    env = config._BOT_ENV  # dotenv_values snapshot, refreshed below
    fresh = {}
    try:
        from dotenv import dotenv_values
        fresh = dotenv_values(ENV_FILE)
    except Exception:
        fresh = env
    out = {key: (fresh.get(key) or "") for key in _PERMISSION_FIELDS}
    out["note"] = ("PermissionManager 每次调用都会重读 .env，保存后立即生效。"
                   "OneBot 路由按 QQ 号匹配 SUPERUSERS/VIP_USERS；"
                   "QQ 官方路由按 openid 匹配 QQ_OFFICIAL_* —— 两张表相互独立。")
    return out


def permissions_write(superusers=None, vip_users=None,
                      qq_official_superusers=None, qq_official_vip_users=None) -> dict:
    """Update identity tables in .env. None = key untouched (kept as-is);
    "" = table cleared. Line-oriented rewrite: every other .env line
    (QQ_BOTS, tokens, …) passes through byte-identical."""
    values = {
        "SUPERUSERS": superusers,
        "VIP_USERS": vip_users,
        "QQ_OFFICIAL_SUPERUSERS": qq_official_superusers,
        "QQ_OFFICIAL_VIP_USERS": qq_official_vip_users,
    }
    for key, val in values.items():
        if val is None:
            continue
        if not isinstance(val, str):
            return {"error": f"{key} 必须是字符串"}
        validator, hint = _PERMISSION_FIELDS[key]
        if not validator(val):
            return {"error": f"{key} 格式非法：应为{hint}"}
    if not ENV_FILE.exists():
        return {"error": ".env 不存在"}
    _backup(ENV_FILE)
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        updated: set[str] = set()
        for i, ln in enumerate(lines):
            for key, val in values.items():
                if val is not None and ln.startswith(f"{key}="):
                    lines[i] = f"{key}={val.strip()}"
                    updated.add(key)
        for key, val in values.items():
            if val is not None and key not in updated:
                lines.append(f"{key}={val.strip()}")
        tmp = ENV_FILE.with_suffix(".env.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(ENV_FILE)
    except OSError as e:
        return {"error": str(e)}
    return {"ok": True, "note": "已保存，权限变更立即生效（两条路由各自匹配对应名单）"}


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
                "保存后约 10 秒 bot 自动热重载，无需重启。",
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
    return {"ok": True, "note": "已保存 — bot 约 10 秒内自动热重载"}


# ── Models: structured per-section edit (probe before write) ──────

MODEL_SECTIONS = ("REASONING_MODEL", "FLASH_MODEL", "MULTIMODAL_MODEL",
                  "AUDIO_MODEL", "OCR_MODEL", "IMAGE_GEN_MODEL", "TTS_MODEL")

# Per-section editable extras beyond api_key/api_base/model.
# Kinds: "int" | "float" | "num" (int when whole) | "str" | "str_list" | "bool".
# IMAGE_GEN/TTS are the Bond-P4 media sections consumed by lib/media_gen.py
# (fresh-read per call → hot; only max_concurrency latches at first use and
# needs a bot restart). voice_pool also accepts a comma-separated string
# for the panel input's convenience.
# supports_vision (Direct Vision gate, lib/image_parts.py): explicit opt-in
# on the two main chat tiers — when true, user-uploaded images are attached
# to the model request as image_url content parts; false/absent keeps the
# text-placeholder + read_file lazy-analysis path. Pure client-side flag →
# toggling it never triggers an API probe.
_TEXT_EXTRAS = {"max_tokens": "int", "temperature": "float"}
_VISION_TEXT_EXTRAS = {**_TEXT_EXTRAS, "supports_vision": "bool"}
_MEDIA_EXTRAS = {"timeout": "num", "max_retries": "int",
                 "max_concurrency": "int"}
_SECTION_EXTRAS = {
    "REASONING_MODEL": _VISION_TEXT_EXTRAS,
    "FLASH_MODEL": _VISION_TEXT_EXTRAS,
    "MULTIMODAL_MODEL": _TEXT_EXTRAS,
    "AUDIO_MODEL": _TEXT_EXTRAS,
    "OCR_MODEL": _TEXT_EXTRAS,
    "IMAGE_GEN_MODEL": _MEDIA_EXTRAS,
    "TTS_MODEL": {**_MEDIA_EXTRAS, "format": "str", "sample_rate": "int",
                  "default_voice": "str", "voice_pool": "str_list"},
}


def _cast_extra(kind: str, v):
    """Cast one extra field; raises ValueError with a Chinese message."""
    if kind == "bool":
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("1", "true", "yes", "on"):
                return True
            if s in ("0", "false", "no", "off"):
                return False
        raise ValueError("必须是布尔值")
    if kind == "str":
        return str(v).strip()
    if kind == "str_list":
        if isinstance(v, str):
            v = v.split(",")
        if not isinstance(v, list) or not all(isinstance(i, str) for i in v):
            raise ValueError("必须是字符串数组（或逗号分隔字符串）")
        out = [i.strip() for i in v if i.strip()]
        if not out:
            raise ValueError("不能为空数组")
        return out
    if isinstance(v, bool):
        raise ValueError("必须是数字")
    num = float(v)
    if kind == "int":
        if not num.is_integer():
            raise ValueError("必须是整数")
        return int(num)
    if kind == "float":
        return num
    return int(num) if num.is_integer() else num   # "num"


def models_section_prepare(section: str, data: dict) -> dict:
    """Validate + unmask one model section against the on-disk config.

    Does NOT write anything — the caller (main.py) probes the API first and
    only commits via models_section_commit() when the probe passes.

    Returns {"ok": True, "prepared_full": dict, "section_data": dict,
             "old_section": dict, "needs_probe": bool} or {"error": str}.
    """
    from .model_probe import section_needs_probe

    if section not in MODEL_SECTIONS:
        return {"error": f"非法模型段: {section}"}
    if not isinstance(data, dict):
        return {"error": "config 必须是 JSON 对象"}

    cleaned: dict = {}
    for f in ("api_key", "api_base", "model"):
        v = data.get(f, "")
        if v is None:
            v = ""
        if not isinstance(v, str):
            return {"error": f"{f} 必须是字符串"}
        cleaned[f] = v.strip()
    for f, kind in _SECTION_EXTRAS.get(section, {}).items():
        v = data.get(f)
        if kind == "bool":
            # False is a meaningful toggle-off — don't skip it (the generic
            # bool-skip below guards numeric fields against True/1 pollution).
            if v is None or v == "":
                continue  # field absent → keep whatever the old section had
            try:
                cleaned[f] = _cast_extra(kind, v)
            except (TypeError, ValueError) as e:
                return {"error": f"{f} {e}"}
            continue
        if isinstance(v, bool) or v in (None, ""):
            continue  # keep whatever the old section had
        try:
            cleaned[f] = _cast_extra(kind, v)
        except (TypeError, ValueError) as e:
            return {"error": f"{f} {e}"}

    if not MODELS_FILE.exists():
        return {"error": "models_settings.json 不存在"}
    try:
        full = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"error": f"现有配置 JSON 损坏，请先用原始 JSON 编辑器修复: {e}"}
    if not isinstance(full, dict):
        return {"error": "现有配置顶层必须是 JSON 对象"}

    old_section = full.get(section)
    if not isinstance(old_section, dict):
        old_section = {}

    # masked '***' placeholders → stored originals (frontend convention)
    cleaned = _unmask_merge(cleaned, old_section)
    # preserve any extra section fields (e.g. provider-specific keys)
    merged_section = {**old_section, **cleaned}

    # Fully-empty credentials = section intentionally unconfigured (bot
    # falls back to .env / disables the capability) — nothing to probe.
    # A *partially* configured section stays probe-able so the probe's
    # missing_config verdict blocks saving a half-broken config.
    configured = bool(merged_section.get("api_key")) or \
        bool(merged_section.get("api_base"))
    needs_probe = configured and section_needs_probe(merged_section, old_section)

    prepared_full = dict(full)
    prepared_full[section] = merged_section
    return {"ok": True, "prepared_full": prepared_full,
            "section_data": merged_section, "old_section": old_section,
            "needs_probe": needs_probe}


def models_section_commit(prepared_full: dict) -> dict:
    """Atomically write the full prepared config (backup first)."""
    _backup(MODELS_FILE)
    try:
        tmp = MODELS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(prepared_full, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(MODELS_FILE)
    except OSError as e:
        return {"error": str(e)}
    return {"ok": True}


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
