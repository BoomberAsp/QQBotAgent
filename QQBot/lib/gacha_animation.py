"""抽卡动画 GIF 生成与缓存。

官方富媒体接口支持动图 GIF；把原来的 6 张 PNG 合成 1 张 GIF 后，
一轮抽卡动画只占 1 条图片被动回复，而不是 6 条。本模块保持为叶子
模块：只依赖 Pillow 与标准库，不导入 agent / nonebot。

缓存目录默认 ``QQBot/data/gacha_anim``（Docker 的 ``qqbot_data`` 卷
会持久化），可用 ``QQ_GACHA_GIF_DIR`` 覆盖。
"""
from __future__ import annotations

import os
import threading

from PIL import Image, ImageOps

GIF_WIDTH_DEFAULT = 1280
GIF_HEIGHT_DEFAULT = 720
# 官方二进制直传硬限制 5 MiB；留一点余量，避免恰好撞线。
GIF_MAX_BYTES = 5_000_000

FRAME_DURATIONS_FULL = (400, 500, 700, 900, 500, 1800)  # 末帧停留 1.8s
FRAME_DURATIONS_KEY = (800, 900, 1800)

_BUILD_LOCK = threading.Lock()


def cache_dir() -> str:
    """GIF 缓存目录：``$QQ_GACHA_GIF_DIR`` 或 ``QQBot/data/gacha_anim``。"""
    env = (os.environ.get("QQ_GACHA_GIF_DIR") or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "gacha_anim")


def durations_for(interval: float = 0.75, mode: str = "full") -> tuple[int, ...]:
    """按工具参数 ``interval`` 生成每帧毫秒时长。

    GIF 在客户端播放，不需要服务端逐帧 sleep；这里保留 interval 的
    语义（默认 0.75s），末帧额外停留，形成“播放一轮 + 定格”。
    """
    try:
        base = int(round(float(interval) * 1000))
    except (TypeError, ValueError):
        base = 750
    base = max(250, min(base, 2000))
    if str(mode).lower() in ("key", "3", "frames3"):
        return (max(300, base),
                max(350, int(base * 1.2)),
                max(1500, int(base * 2.4)))
    return (max(250, int(base * 0.55)),
            max(300, int(base * 0.70)),
            max(350, int(base * 0.95)),
            max(450, int(base * 1.20)),
            max(300, int(base * 0.65)),
            max(1500, int(base * 2.40)))


def ensure_gif(frame_paths, *, star_level: int, is_single: bool,
               mode: str = "full", width: int = GIF_WIDTH_DEFAULT,
               interval: float = 0.75, loop: int | None = None
               ) -> tuple[str, int]:
    """返回 ``(gif_path, total_duration_ms)``；必要时生成并写缓存。

    参数：
        frame_paths: 本地 PNG 路径列表（已按播放顺序排好）。
        star_level: 3/4/5/6，仅用于缓存 key 与文件名。
        is_single: 单抽/十连只影响开始帧，但缓存分开更直观。
        mode: ``full``（默认 6 帧）或 ``key``（3 关键帧）。
        width: 目标宽度；高度按 16:9 计算。
        interval: 帧间隔基数（秒）。
        loop: 0=无限循环；1=播放一轮（默认）；None=读环境变量。
    """
    paths = [_clean_path(p) for p in (frame_paths or [])]
    paths = [p for p in paths if p and os.path.isfile(p)]
    if len(paths) < 2:
        raise ValueError("gacha gif needs at least 2 existing frames")

    mode = "key" if str(mode).lower() in ("key", "3", "frames3") else "full"
    req_width = _clamp_width(width)
    height = max(2, int(round(req_width * 9 / 16)))
    durations = durations_for(interval, mode)
    if len(durations) != len(paths):
        durations = _durations_for_count(len(paths), interval)
    total_ms = int(sum(durations))
    loop = _loop_value(loop)

    out = _cache_path(star_level, is_single, req_width, height, mode, interval, loop)
    if _fresh(out, paths):
        return out, total_ms

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with _BUILD_LOCK:
        if _fresh(out, paths):
            return out, total_ms
        last_error: Exception | None = None
        for try_width in _width_candidates(req_width):
            try_height = max(2, int(round(try_width * 9 / 16)))
            tmp = f"{out}.tmp.{os.getpid()}.{threading.get_ident()}"
            try:
                _write_gif(paths, tmp, try_width, try_height, durations, loop)
                size = os.path.getsize(tmp)
                if size > GIF_MAX_BYTES:
                    last_error = ValueError(
                        f"gif too large at {try_width}px: {size} bytes")
                    continue
                os.replace(tmp, out)
                return out, total_ms
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            finally:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
        raise ValueError(f"gacha gif build failed: {last_error}")


def _write_gif(paths, out_path: str, width: int, height: int,
               durations: tuple[int, ...], loop: int) -> None:
    frames = [_palette_frame(_fit_frame(p, width, height)) for p in paths]
    if not frames:
        raise ValueError("no frames")
    if len(durations) != len(frames):
        durations = _durations_for_count(len(frames), 0.75)
    frames[0].save(
        out_path,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=list(durations),
        loop=loop,
        optimize=True,
        disposal=2,
    )


def _fit_frame(path: str, width: int, height: int):
    with Image.open(path) as im:
        rgb = im.convert("RGB")
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:  # Pillow < 9.1
            resample = Image.LANCZOS
        return ImageOps.fit(rgb, (width, height), method=resample,
                            centering=(0.5, 0.5))


def _palette_frame(frame):
    """256 色 GIF 调色板；保留渐变细节，失败时退回 ADAPTIVE。"""
    try:
        return frame.quantize(
            colors=256,
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.FLOYDSTEINBERG,
        )
    except (AttributeError, TypeError):
        palette = getattr(Image, "ADAPTIVE", getattr(Image, "Palette", None))
        return frame.convert("P", palette=palette, colors=256)


def _durations_for_count(n: int, interval: float) -> tuple[int, ...]:
    if n == 6:
        return durations_for(interval, "full")
    if n == 3:
        return durations_for(interval, "key")
    try:
        base = int(round(float(interval) * 1000))
    except (TypeError, ValueError):
        base = 750
    base = max(250, min(base, 2000))
    return tuple([max(300, base)] * max(1, n - 1)
                 + [max(1500, int(base * 2.2))])


def _cache_path(star_level: int, is_single: bool, width: int, height: int,
                mode: str, interval: float, loop: int = 1) -> str:
    try:
        star = int(star_level)
    except (TypeError, ValueError):
        star = 0
    ms = int(round(float(interval) * 1000)) if interval is not None else 750
    name = (f"gacha_s{star}_{'single' if is_single else 'ten'}"
            f"_{width}x{height}_{mode}_{ms}ms_{loop}loop.gif")
    return os.path.join(cache_dir(), name)


def _fresh(path: str, source_paths) -> bool:
    try:
        gif_mtime = os.path.getmtime(path)
        if os.path.getsize(path) <= 0:
            return False
    except OSError:
        return False
    for src in source_paths:
        try:
            if os.path.getmtime(src) > gif_mtime:
                return False
        except OSError:
            return False
    return True


def _clean_path(path) -> str:
    text = str(path or "")
    if text.startswith("file://"):
        text = text[len("file://"):]
    return text


def _clamp_width(width) -> int:
    try:
        value = int(width)
    except (TypeError, ValueError):
        value = GIF_WIDTH_DEFAULT
    return max(640, min(1920, value))


def _width_candidates(width: int) -> tuple[int, ...]:
    out: list[int] = []
    for value in (width, 960, 800, 640, 480):
        value = max(480, min(1920, int(value)))
        if value not in out:
            out.append(value)
    return tuple(out)


def _loop_value(loop) -> int:
    if loop is None:
        raw = (os.environ.get("QQ_GACHA_GIF_LOOP") or "").strip()
        try:
            loop = int(raw) if raw else 1
        except ValueError:
            loop = 1
    try:
        return max(0, min(10, int(loop)))
    except (TypeError, ValueError):
        return 1
