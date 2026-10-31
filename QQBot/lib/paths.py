"""路径解析的统一入口。

背景：``data/wiki_cache`` 原先被 9 个模块各自用 ``os.path.dirname(__file__)`` 拼出来，
生产上把缓存迁到数据盘（``USER_DATA_ROOT/wiki_cache``）后，要么改 9 处代码、要么在仓库里
建符号链接。这里提供单一入口：**优先读环境变量 WIKI_CACHE_DIR**，未设置时回落到仓库内
``data/wiki_cache``（开发机行为不变）。

约定：只做路径拼接，不创建目录、不读写文件。
"""

from __future__ import annotations

import os

_QQBOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repo_dir() -> str:
    """``QQBot/`` 目录的绝对路径。"""
    return _QQBOT_DIR


def wiki_cache_dir() -> str:
    """wiki 缓存根目录：``$WIKI_CACHE_DIR`` 或 ``QQBot/data/wiki_cache``。"""
    env = (os.environ.get("WIKI_CACHE_DIR") or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(_QQBOT_DIR, "data", "wiki_cache")


def wiki_cache(*parts: str) -> str:
    """缓存目录下的文件路径：``wiki_cache("character_details.json")``。"""
    return os.path.join(wiki_cache_dir(), *parts)
