"""Route-level e2e for ``POST /attach/upload`` (multipart, through the app).

Proves the upload contract end-to-end against a fake storage: avatar uploads are
transcoded to webp, generic attachments are stored as-is, the endpoint is
auth-gated, and image errors surface as the uniform error envelope.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlalchemy import select

from src.config import get_settings
from src.db.models.attach import Attach
from tests.files.conftest import AttachCtx

pytestmark = pytest.mark.asyncio


def _png_bytes(size: int = 16) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), "blue").save(buf, format="PNG")
    return buf.getvalue()


async def test_upload_avatar_transcodes_and_persists(attach_ctx: AttachCtx) -> None:
    resp = await attach_ctx.client.post(
        "/attach/upload",
        files={"file": ("me.png", _png_bytes(), "image/png")},
        data={"biz_type": "avatar"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    # Snowflake id serialized as string; webp content type after transcode.
    assert isinstance(data["id"], str)
    assert data["content_type"] == "image/webp"
    assert data["biz_type"] == "avatar"
    assert data["url"] == f"https://signed.example/{await _object_key(attach_ctx)}"
    # Exactly one upload captured, body is webp (not the raw png).
    assert len(attach_ctx.storage.uploads) == 1
    assert attach_ctx.storage.uploads[0]["content_type"] == "image/webp"


async def test_upload_attachment_stores_as_is(attach_ctx: AttachCtx) -> None:
    raw = b"%PDF-1.7 fake pdf"
    resp = await attach_ctx.client.post(
        "/attach/upload",
        files={"file": ("report.pdf", raw, "application/pdf")},
        data={"biz_type": "attachment"},
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["content_type"] == "application/pdf"
    assert data["biz_type"] == "attachment"
    assert data["file_size"] == len(raw)
    # Stored as-is: the captured upload bytes equal the raw input.
    assert attach_ctx.storage.uploads[0]["data"] == raw
    assert attach_ctx.storage.uploads[0]["object_key"].startswith("attachment/")


async def test_upload_defaults_to_avatar_biz_type(attach_ctx: AttachCtx) -> None:
    # biz_type omitted → defaults to avatar (the form field has a default).
    resp = await attach_ctx.client.post(
        "/attach/upload",
        files={"file": ("a.png", _png_bytes(), "image/png")},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["biz_type"] == "avatar"


async def test_upload_rejects_non_image_avatar(attach_ctx: AttachCtx) -> None:
    resp = await attach_ctx.client.post(
        "/attach/upload",
        files={"file": ("x.txt", b"not-an-image", "text/plain")},
        data={"biz_type": "avatar"},
    )

    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "attach.invalid_image"
    # Rejected before any storage write.
    assert attach_ctx.storage.uploads == []


async def test_upload_requires_authentication(attach_ctx: AttachCtx) -> None:
    resp = await attach_ctx.client.post(
        "/attach/upload",
        files={"file": ("me.png", _png_bytes(), "image/png")},
        headers={"Authorization": ""},  # strip the bearer token
    )

    assert resp.status_code == 401


async def test_upload_rejects_oversized_body_before_buffering(
    attach_ctx: AttachCtx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The router caps the read at the biz_type limit and rejects mid-stream —
    an oversized attachment is refused with attach.too_large and never stored."""
    settings = get_settings()
    # Shrink the attachment limit to 16 bytes (the router reads the same cached
    # settings singleton), then send 1KiB → the cap must trip before any write.
    monkeypatch.setattr(settings, "attachment_max_bytes", 16, raising=False)

    resp = await attach_ctx.client.post(
        "/attach/upload",
        files={"file": ("big.bin", b"x" * 1024, "application/octet-stream")},
        data={"biz_type": "attachment"},
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "attach.too_large"
    # Rejected before any storage write.
    assert attach_ctx.storage.uploads == []


async def _object_key(attach_ctx: AttachCtx) -> str:
    """The single persisted attachment's object key (for URL assertions)."""
    row = (await attach_ctx.session.scalars(select(Attach))).first()
    assert row is not None
    return row.object_key
