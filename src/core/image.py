"""头像图片校验 + webp 转码 —— core 层无业务横切原语。

后端代理上传的核心一环:用户传来的原始字节先过这里,做三件事再落对象存储——

1. **大小闸**:解码前先按字节数拒超限(防超大文件喂给解码器 → 解压炸弹/内存打爆)。
2. **真伪校验**:用 Pillow 实际解码,非图片(伪装扩展名 / 损坏文件)直接拒。
3. **归一化**:统一转 webp + 按需等比缩放 + **重新编码即剥除 EXIF**(隐私元数据如
   GPS/相机型号不随头像外泄),省存储且消除原格式的解析面差异。

纯计算、不碰 web/DB/存储层;失败用本模块自有异常表达,由上层 service 翻成 ``AppError``。
Pillow 是同步 CPU 活,调用方(service)用 ``asyncio.to_thread`` 卸载,别在事件循环里直跑。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

# 转码后的 webp 质量(0-100)。80 是肉眼几乎无损与体积的务实平衡点(头像场景足够)。
_WEBP_QUALITY = 80
# 头像默认最长边上限(像素)。超过则等比缩小——头像无需原图分辨率,挡超大图省存储/带宽。
_DEFAULT_MAX_DIMENSION = 512


class InvalidImageError(Exception):
    """上传内容不是可解码的图片(伪装扩展名 / 损坏 / 非图片字节)。"""


class ImageTooLargeError(Exception):
    """上传字节数超过允许上限(解码前即拒)。"""


@dataclass(frozen=True, slots=True)
class ConvertedImage:
    """转码结果:webp 字节 + 其 MIME。"""

    data: bytes
    content_type: str


def to_webp_avatar(
    raw: bytes,
    *,
    max_bytes: int,
    max_dimension: int = _DEFAULT_MAX_DIMENSION,
) -> ConvertedImage:
    """校验原始图片字节并转成 webp(剥 EXIF + 按需等比缩放)。

    超过 ``max_bytes`` 抛 :class:`ImageTooLargeError`(解码前);无法解码为图片抛
    :class:`InvalidImageError`。成功返回 webp 字节 + ``image/webp``。
    """
    if len(raw) > max_bytes:
        raise ImageTooLargeError(
            f"image is {len(raw)} bytes, exceeds limit {max_bytes}"
        )

    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()  # Force full decode so a truncated/corrupt file fails here.
            # Drop alpha/palette quirks: convert to RGB so webp re-encode is uniform
            # and EXIF/ICC side-channels do not carry over.
            rgb = image.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError(f"not a decodable image: {exc}") from exc

    rgb.thumbnail((max_dimension, max_dimension))  # In-place, keeps aspect ratio.

    out = io.BytesIO()
    rgb.save(out, format="WEBP", quality=_WEBP_QUALITY)  # Fresh encode → no EXIF.
    return ConvertedImage(data=out.getvalue(), content_type="image/webp")
