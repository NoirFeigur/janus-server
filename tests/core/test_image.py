"""Tests for avatar image validation + webp conversion.

Drives the real Pillow pipeline with in-memory images (no disk, no network):
proves a valid raster is re-encoded to webp, a non-image is rejected, an
oversized payload is rejected, and EXIF metadata does not survive the round-trip.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from src.core.image import ImageTooLargeError, InvalidImageError, to_webp_avatar


def _png_bytes(size: tuple[int, int] = (64, 64), color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_with_exif() -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (32, 32), "blue")
    exif = img.getexif()
    exif[0x010F] = "SecretCameraMake"  # Make tag — should not survive conversion.
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def test_valid_png_is_converted_to_webp() -> None:
    out = to_webp_avatar(_png_bytes(), max_bytes=2 * 1024 * 1024)

    assert out.content_type == "image/webp"
    assert Image.open(io.BytesIO(out.data)).format == "WEBP"


def test_non_image_payload_is_rejected() -> None:
    with pytest.raises(InvalidImageError):
        to_webp_avatar(b"this is not an image", max_bytes=2 * 1024 * 1024)


def test_oversized_payload_is_rejected_before_decode() -> None:
    big = _png_bytes(size=(8, 8))
    with pytest.raises(ImageTooLargeError):
        to_webp_avatar(big, max_bytes=len(big) - 1)


def test_exif_metadata_is_stripped() -> None:
    out = to_webp_avatar(_jpeg_with_exif(), max_bytes=2 * 1024 * 1024)

    converted = Image.open(io.BytesIO(out.data))
    assert not dict(converted.getexif())  # No EXIF tags carried over.


def test_oversized_dimensions_are_downscaled() -> None:
    out = to_webp_avatar(
        _png_bytes(size=(2000, 2000)), max_bytes=2 * 1024 * 1024, max_dimension=512
    )

    w, h = Image.open(io.BytesIO(out.data)).size
    assert max(w, h) <= 512


def test_decompression_bomb_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A small file declaring huge dimensions (a decompression bomb) must be
    refused as an invalid image — the byte-size gate alone cannot catch it
    because the compressed bytes are tiny. Lower Pillow's pixel ceiling so a
    modest test image trips the same DecompressionBombError guard."""
    # 64x64 = 4096 px; set the ceiling below that so .load() raises the bomb error.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1024)
    with pytest.raises(InvalidImageError, match="bomb"):
        to_webp_avatar(_png_bytes(size=(64, 64)), max_bytes=2 * 1024 * 1024)


def test_oversized_pixel_count_rejected_before_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An image whose declared dimensions exceed our explicit ``_MAX_PIXELS`` is
    refused at the header (before ``load()``), independent of Pillow's own bomb
    ceiling. This closes the band Pillow only *warns* on (MAX_IMAGE_PIXELS .. 2×)
    where ``load()`` would otherwise decode a huge raster into memory.

    Pillow's ceiling stays HIGH so this image would NOT trip the bomb guard —
    proving our own pre-load pixel check is what rejects it (distinct message)."""
    monkeypatch.setattr("src.core.image._MAX_PIXELS", 1024)  # 32x32=1024 → 64x64 over
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10_000_000)  # Pillow would allow it.
    with pytest.raises(InvalidImageError, match="pixel limit"):
        to_webp_avatar(_png_bytes(size=(64, 64)), max_bytes=2 * 1024 * 1024)


def test_image_at_pixel_limit_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boundary: an image exactly at ``_MAX_PIXELS`` passes (``>`` not ``>=``)."""
    monkeypatch.setattr("src.core.image._MAX_PIXELS", 4096)  # 64x64 == 4096 px exactly.
    out = to_webp_avatar(_png_bytes(size=(64, 64)), max_bytes=2 * 1024 * 1024)
    assert out.content_type == "image/webp"
