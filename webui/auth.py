"""
WebUI authentication — single admin password, scrypt hash, session cookies.

- Password hash stored in ``webui/data/auth.json`` (first-run setup flow).
- Sessions are random 32-byte hex tokens kept in memory with a TTL
  (panel restart = re-login, acceptable for a single-admin panel).
- Brute-force guard: MAX_LOGIN_FAILS failures within LOCK_SECONDS locks
  further attempts.

No third-party dependency: hashlib.scrypt + secrets from the stdlib.
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

from fastapi import Request, Response

from . import config

# ── Password hashing ──────────────────────────────────────────────

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
    ).hex()


def _load_auth() -> dict:
    try:
        with open(config.AUTH_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_auth(data: dict) -> None:
    tmp = str(config.AUTH_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, config.AUTH_FILE)
    try:
        os.chmod(config.AUTH_FILE, 0o600)
    except OSError:
        pass


def is_configured() -> bool:
    return bool(_load_auth().get("password_hash"))


def setup_password(password: str) -> bool:
    """Set the admin password. Only allowed when not yet configured."""
    if is_configured():
        return False
    salt = secrets.token_bytes(16)
    _save_auth({
        "password_hash": _hash_password(password, salt),
        "salt": salt.hex(),
        "created_at": int(time.time()),
    })
    return True


def change_password(old: str, new: str) -> bool:
    """Change the admin password (requires the current one)."""
    if not verify_password(old):
        return False
    salt = secrets.token_bytes(16)
    data = _load_auth()
    data["password_hash"] = _hash_password(new, salt)
    data["salt"] = salt.hex()
    data["changed_at"] = int(time.time())
    _save_auth(data)
    return True


def verify_password(password: str) -> bool:
    data = _load_auth()
    if not data.get("password_hash") or not data.get("salt"):
        return False
    try:
        salt = bytes.fromhex(data["salt"])
        candidate = _hash_password(password, salt)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, data["password_hash"])


# ── Login rate limiting ───────────────────────────────────────────

_fail_times: list = []
_locked_until: float = 0.0


def _check_lock() -> Optional[float]:
    """Return remaining lock seconds, or None when not locked."""
    global _locked_until
    if time.time() < _locked_until:
        return _locked_until - time.time()
    return None


def record_failure() -> Optional[float]:
    """Record a failed login. Returns lock seconds if this triggered a lock."""
    global _locked_until
    now = time.time()
    _fail_times.append(now)
    cutoff = now - config.LOCK_SECONDS
    _fail_times[:] = [t for t in _fail_times if t > cutoff]
    if len(_fail_times) >= config.MAX_LOGIN_FAILS:
        _locked_until = now + config.LOCK_SECONDS
        _fail_times.clear()
        return config.LOCK_SECONDS
    return None


def lock_remaining() -> float:
    rem = _check_lock()
    return rem or 0.0


# ── Sessions ──────────────────────────────────────────────────────

COOKIE_NAME = "webui_session"
_sessions: dict = {}  # token -> expiry epoch


def _prune() -> None:
    now = time.time()
    expired = [t for t, exp in _sessions.items() if exp < now]
    for t in expired:
        _sessions.pop(t, None)


def create_session(response: Response) -> None:
    _prune()
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + config.SESSION_TTL
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=config.SESSION_TTL,
        httponly=True, samesite="strict", path="/",
    )


def destroy_session(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE_NAME, "")
    _sessions.pop(token, None)
    response.delete_cookie(COOKIE_NAME, path="/")


def is_authenticated(request: Request) -> bool:
    _prune()
    token = request.cookies.get(COOKIE_NAME, "")
    if not token:
        return False
    expiry = _sessions.get(token)
    return expiry is not None and expiry > time.time()


def require_setup() -> bool:
    """True when the panel has no password yet (setup mode: only auth APIs)."""
    return not is_configured()
