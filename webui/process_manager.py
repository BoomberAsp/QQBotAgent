"""
ProcessManager — start/stop/status for nonebot / napcat / searxng,
plus an optional watchdog that auto-restarts crashed processes.

Design notes
------------
* popen processes are spawned with ``start_new_session=True`` so they
  survive a panel restart; PID files live in ``webui/data/{name}.pid``.
* Processes started outside the panel are *adopted*: status detects
  them via pgrep-style regex patterns, and stop sends them SIGTERM.
  Only panel-spawned processes are killed as a whole process group.
* The docker method shells out to ``docker compose`` / ``docker``.
  When docker is missing the process reports ``unavailable``.
* Watchdog state (enabled flag, per-process should_run, crash counters,
  recent restarts) persists in ``webui/data/watchdog_state.json``.
  Rate limit: at most 3 auto-restarts per process within 5 minutes.
"""

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections import deque

import psutil

from . import config

POLL_INTERVAL = 30            # watchdog tick seconds
RATE_WINDOW = 300             # 5 minutes
MAX_RESTARTS = 3              # max auto-restarts per window
STOP_TIMEOUT = 8              # seconds between SIGTERM and SIGKILL

_STATE_FILE = config.WEBUI_DATA / "watchdog_state.json"
_RESTARTS: dict[str, deque] = {}


# ── Persistent watchdog state ─────────────────────────────────────

def _load_state() -> dict:
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"enabled": False, "processes": {}}


def _save_state(state: dict) -> None:
    tmp = str(_STATE_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, _STATE_FILE)


def _proc_state(state: dict, name: str) -> dict:
    return state.setdefault("processes", {}).setdefault(name, {
        "should_run": False,
        "crash_count": 0,
        "last_crash": None,
    })


# ── PID helpers ───────────────────────────────────────────────────

def _pid_file(name: str) -> config.Path:
    return config.PID_DIR / f"{name}.pid"


def _read_pid(name: str) -> int | None:
    try:
        return int(_pid_file(name).read_text().strip())
    except Exception:
        return None


def _write_pid(name: str, pid: int) -> None:
    _pid_file(name).write_text(str(pid))


def _clear_pid(name: str) -> None:
    try:
        _pid_file(name).unlink()
    except OSError:
        pass


def _alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user


def _cmdline(pid: int) -> str:
    try:
        return " ".join(psutil.Process(pid).cmdline())
    except Exception:
        try:
            return open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            return ""


def _find_pids(spec: dict) -> list[int]:
    """PIDs for a popen-style process: PID file first, then pattern scan."""
    pids: list[int] = []
    pid = _read_pid(spec["_name"])
    if pid and os.path.exists(f"/proc/{pid}"):
        cmd = _cmdline(pid)
        # Guard against PID reuse: trust the PID file only when the
        # cmdline matches a pattern (or is unreadable, e.g. zombie).
        if not cmd or any(re.search(p, cmd) for p in spec.get("match", [])):
            pids.append(pid)
    if not pids:
        patterns = [re.compile(p) for p in spec.get("match", [])]
        if patterns:
            for proc in psutil.process_iter(["pid", "cmdline"]):
                try:
                    cmd = " ".join(proc.info["cmdline"] or [])
                except Exception:
                    continue
                if cmd and any(p.search(cmd) for p in patterns):
                    pids.append(proc.info["pid"])
    return sorted(set(pids))


# ── Docker helpers ────────────────────────────────────────────────

_DOCKER_OK: tuple[float, bool] | None = None


def _docker_available() -> bool:
    global _DOCKER_OK
    now = time.time()
    if _DOCKER_OK and now - _DOCKER_OK[0] < 60:
        return _DOCKER_OK[1]
    ok = False
    if shutil.which("docker"):
        try:
            ok = subprocess.run(["docker", "info"], capture_output=True,
                                timeout=10).returncode == 0
        except Exception:
            ok = False
    _DOCKER_OK = (now, ok)
    return ok


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()[-800:]
    except FileNotFoundError:
        return 127, f"命令不存在: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "命令超时"
    except Exception as e:
        return 1, str(e)


def _docker_state(spec: dict) -> str:
    """running | exited | missing | unavailable"""
    if not _docker_available():
        return "unavailable"
    rc, out = _run(["docker", "inspect", "-f", "{{.State.Status}}", spec["container"]])
    if rc != 0:
        return "missing"
    out = out.strip()
    return "running" if out == "running" else (out or "exited")


# ── Core operations (synchronous; call via asyncio.to_thread) ─────

def status_all() -> dict:
    state = _load_state()
    result = {}
    for name, spec in config.PROCESSES.items():
        spec = dict(spec, _name=name)
        ps = _proc_state(state, name)
        entry = {
            "label": spec.get("label", name),
            "method": spec["method"],
            "should_run": ps.get("should_run", False),
            "crash_count": ps.get("crash_count", 0),
            "last_crash": ps.get("last_crash"),
            "watchdog_throttled": _throttled(name),
        }
        if spec["method"] == "docker":
            st = _docker_state(spec)
            entry["state"] = "running" if st == "running" else (
                "unavailable" if st == "unavailable" else "stopped")
            entry["docker_state"] = st
        else:
            pids = _find_pids(spec)
            if pids:
                entry["state"] = "running"
                entry["pid"] = pids[0]
                try:
                    entry["uptime"] = int(time.time() - psutil.Process(pids[0]).create_time())
                except Exception:
                    entry["uptime"] = None
                entry["adopted"] = pids[0] != _read_pid(name)
            else:
                entry["state"] = "stopped"
                entry["pid"] = None
            entry["log_file"] = spec.get("log_file")
        result[name] = entry
    return result


def start(name: str) -> dict:
    spec = config.PROCESSES.get(name)
    if not spec:
        return {"ok": False, "error": f"未知进程: {name}"}
    spec = dict(spec, _name=name)

    if spec["method"] == "docker":
        if not _docker_available():
            return {"ok": False, "error": "docker 不可用"}
        rc, out = _run(["docker", "compose", "up", "-d", spec["container"]],
                       cwd=spec.get("compose_cwd"))
        if rc != 0:  # fallback: plain docker start
            rc, out = _run(["docker", "start", spec["container"]])
        ok = rc == 0
    else:
        if _find_pids(spec):
            _mark_should_run(name, True)
            return {"ok": True, "note": "already_running"}
        log_file = spec.get("log_file")
        try:
            fh = open(log_file, "ab") if log_file else subprocess.DEVNULL
        except OSError as e:
            return {"ok": False, "error": f"无法打开日志文件: {e}"}
        try:
            with open(os.devnull, "rb") as devnull:
                proc = subprocess.Popen(
                    spec["cmd"],
                    cwd=spec.get("cwd"),
                    stdin=devnull,
                    stdout=fh,
                    stderr=subprocess.STDOUT,
                    shell=spec.get("shell", False),
                    start_new_session=True,
                )
        except Exception as e:
            return {"ok": False, "error": f"启动失败: {e}"}
        finally:
            if fh is not subprocess.DEVNULL:
                fh.close()
        _write_pid(name, proc.pid)
        ok = True

    if ok:
        _mark_should_run(name, True)
    return {"ok": ok}


def stop(name: str) -> dict:
    spec = config.PROCESSES.get(name)
    if not spec:
        return {"ok": False, "error": f"未知进程: {name}"}
    spec = dict(spec, _name=name)
    _mark_should_run(name, False)

    if spec["method"] == "docker":
        if not _docker_available():
            return {"ok": False, "error": "docker 不可用"}
        rc, out = _run(["docker", "compose", "stop", spec["container"]],
                       cwd=spec.get("compose_cwd"), timeout=90)
        if rc != 0:
            rc, out = _run(["docker", "stop", spec["container"]], timeout=90)
        return {"ok": rc == 0, "detail": out}

    pids = _find_pids(spec)
    if not pids:
        _clear_pid(name)
        return {"ok": True, "note": "not_running"}

    owned_pid = _read_pid(name)
    for pid in pids:
        try:
            if pid == owned_pid:
                # Panel-spawned: kill the whole session/process group
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    # Wait, then escalate to SIGKILL for anything left
    deadline = time.time() + STOP_TIMEOUT
    while time.time() < deadline:
        pids = [p for p in pids if os.path.exists(f"/proc/{p}")]
        if not pids:
            break
        time.sleep(0.3)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _clear_pid(name)
    return {"ok": True}


def restart(name: str) -> dict:
    r = stop(name)
    if not r.get("ok") and r.get("error"):
        return r
    time.sleep(1)
    return start(name)


def _mark_should_run(name: str, flag: bool) -> None:
    state = _load_state()
    _proc_state(state, name)["should_run"] = flag
    _save_state(state)


# ── Watchdog ──────────────────────────────────────────────────────

def watchdog_enabled() -> bool:
    return bool(_load_state().get("enabled"))


def set_watchdog(enabled: bool) -> None:
    state = _load_state()
    state["enabled"] = bool(enabled)
    _save_state(state)


def _throttled(name: str) -> bool:
    dq = _RESTARTS.get(name)
    if not dq:
        return False
    cutoff = time.time() - RATE_WINDOW
    recent = [t for t in dq if t > cutoff]
    return len(recent) >= MAX_RESTARTS


def watchdog_tick() -> list[str]:
    """One watchdog pass. Returns list of process names restarted."""
    restarted: list[str] = []
    if not watchdog_enabled():
        return restarted
    state = _load_state()
    for name, spec in config.PROCESSES.items():
        ps = _proc_state(state, name)
        if not ps.get("should_run"):
            continue
        if _throttled(name):
            continue
        info = status_all().get(name, {})
        if info.get("state") in ("running", "unavailable"):
            continue
        # Crashed / not running while it should be → restart
        ps["crash_count"] = int(ps.get("crash_count", 0)) + 1
        ps["last_crash"] = int(time.time())
        _save_state(state)
        dq = _RESTARTS.setdefault(name, deque())
        dq.append(time.time())
        r = start(name)
        if r.get("ok"):
            restarted.append(name)
    return restarted


async def watchdog_loop() -> None:
    """Background task; started from the FastAPI lifespan."""
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            restarted = await asyncio.to_thread(watchdog_tick)
            for name in restarted:
                try:
                    from . import audit
                    audit.log_action("watchdog.restart", f"看门狗自动重启 {name}", "system")
                except Exception:
                    pass
        except Exception:
            pass
