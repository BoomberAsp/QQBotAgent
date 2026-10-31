"""
Resource sampling via psutil — CPU / memory / disk / uptime.

Results are cached for 5 seconds so the dashboard poll and the
websocket push don't hammer psutil.
"""

import os
import time

import psutil

from . import config

_CACHE_TTL = 5.0
_cache: dict | None = None
_cache_at: float = 0.0

# Warm the internal counter so the first real sample is meaningful.
try:
    psutil.cpu_percent(interval=None)
except Exception:
    pass


def _disk_path() -> str:
    """Disk to report: wherever the user data lives, falling back to repo root."""
    candidate = str(config.USER_DATA_ROOT)
    # Walk up until an existing path is found
    while candidate and not os.path.exists(candidate):
        candidate = os.path.dirname(candidate)
    return candidate or str(config.ROOT)


def sample() -> dict:
    """Return a resource snapshot (cached for _CACHE_TTL seconds)."""
    global _cache, _cache_at
    now = time.time()
    if _cache is not None and now - _cache_at < _CACHE_TTL:
        return _cache

    data: dict = {"ts": int(now)}
    try:
        data["cpu"] = psutil.cpu_percent(interval=None)
    except Exception:
        data["cpu"] = None
    try:
        mem = psutil.virtual_memory()
        data.update(mem_percent=mem.percent, mem_used=mem.used, mem_total=mem.total)
    except Exception:
        data.update(mem_percent=None, mem_used=None, mem_total=None)
    try:
        du = psutil.disk_usage(_disk_path())
        data.update(disk_percent=du.percent, disk_used=du.used,
                    disk_total=du.total, disk_path=_disk_path())
    except Exception:
        data.update(disk_percent=None, disk_used=None,
                    disk_total=None, disk_path=None)
    try:
        data["uptime"] = int(now - psutil.boot_time())
    except Exception:
        data["uptime"] = None
    try:
        data["load"] = list(os.getloadavg())
    except (OSError, AttributeError):
        data["load"] = None

    _cache = data
    _cache_at = now
    return data
