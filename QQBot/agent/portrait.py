"""角色参考立绘：校验 / 规整 / 描述文案（纯函数，便于单测）。

参考立绘是 P4「视觉一致性」通路的**图片锚**：角色卡里的 ``## Appearance
Anchor`` 是文字锚，``CHARACTERS_DIR/<char_id>/assets/reference.*`` 则是
I2I（图像编辑）用的图片锚——两者一起决定 ``create_image`` 与主动发图时
角色的脸。（读取端见 CharacterRegistry.reference_image_path，落盘端见
CharacterRegistry.set_reference_image。）

本模块只做一件事：把用户上传的图片变成「可以安全当参考图用」的字节流。

- 解码校验（不是图片 / 损坏 / 空文件 → 用户可读的 ValueError）
- 规整（EXIF 旋转、alpha 铺白底、长边 ≤ 1536、PNG 编码、体积兜底）
- 回传尺寸，落盘交给调用方

不碰任何路径、不 import NoneBot：生产上 CHARACTERS_DIR 在数据盘、与代码
分盘存放，路径解析统一由 registry 负责。
"""
from __future__ import annotations

import io
from typing import Tuple

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_SOURCE_BYTES = 10 * 1024 * 1024   # 原图上限（超过直接拒，省得解码爆内存）
MAX_EDGE = 1536                       # 参考图长边上限
MIN_EDGE = 128                        # 太小没有参考价值
MAX_OUTPUT_BYTES = 4 * 1024 * 1024    # 规整后 PNG 体积兜底
FALLBACK_EDGE = 1024                  # 超体积时二次缩边
SUPPORTED_FORMATS = "PNG / JPG / WEBP / GIF / BMP"

# Pillow 9.x 用 Image.LANCZOS，10+ 迁到 Image.Resampling.LANCZOS
_RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS


def _load_image(raw: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()                      # 强制解码，截断文件在这里就暴露
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise ValueError(f"这张图我读不出来（支持 {SUPPORTED_FORMATS}）") from e
    return img


def _flatten_alpha(img: Image.Image) -> Image.Image:
    """任意模式 → RGB；带透明底的铺白底（生图模型对 alpha 处理不一致）。"""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return img.convert("RGB")


def _encode_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def normalize_reference_image(raw: bytes) -> Tuple[bytes, int, int]:
    """上传图字节 → (PNG 字节, 宽, 高)。

    校验失败抛 ``ValueError``，文案可直接回给用户（不含内部异常细节）。
    """
    if not raw:
        raise ValueError("这张图是空的，换一张试试")
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError(
            f"图片太大（{len(raw) / 1024 / 1024:.1f} MB），"
            f"请压缩到 {MAX_SOURCE_BYTES // 1024 // 1024} MB 以内再发")

    img = _load_image(raw)
    try:
        img = ImageOps.exif_transpose(img)   # 手机竖拍图不要躺倒
    except Exception:  # noqa: BLE001 — EXIF 坏了不影响画面
        pass

    width, height = img.size
    if min(width, height) < MIN_EDGE:
        raise ValueError(
            f"图片太小（{width}×{height}），参考立绘至少要有 {MIN_EDGE}px 的短边")

    img = _flatten_alpha(img)
    if max(img.size) > MAX_EDGE:
        img.thumbnail((MAX_EDGE, MAX_EDGE), _RESAMPLE)
    data = _encode_png(img)
    if len(data) > MAX_OUTPUT_BYTES:
        img.thumbnail((FALLBACK_EDGE, FALLBACK_EDGE), _RESAMPLE)
        data = _encode_png(img)
    return data, img.size[0], img.size[1]


def describe_size(width: int, height: int, nbytes: int) -> str:
    """``1024×1024，PNG 86 KB``——回给用户的尺寸/体积描述。"""
    if nbytes >= 1024 * 1024:
        size = f"{nbytes / 1024 / 1024:.1f} MB"
    else:
        size = f"{max(1, round(nbytes / 1024))} KB"
    return f"{width}×{height}，PNG {size}"
