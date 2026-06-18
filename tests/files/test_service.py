"""Tests for the attachment service (avatar upload + presigned read).

Drives the service directly against in-memory SQLite with a fake ObjectStorage
that records uploads and hands back canned presigned URLs — so the test proves
the *orchestration* (validate → convert → upload → persist row → presign),
never touching a real bucket. A 1x1 PNG stands in for an avatar upload.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.auth.service import AuthenticatedUser
from src.db.base import Base
from src.db.models.attach import SysAttach
from src.enums import AttachBizType, ErrorCode
from src.exceptions import AppError
from src.files.service import AttachService

ACTOR = AuthenticatedUser(
    user_id=1000,
    username="admin",
    department_id=7,
    permissions=frozenset(),
)


class _FakeStorage:
    """Records uploads/deletes; returns deterministic presigned URLs."""

    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.deletes: list[str] = []
        self.presigns: list[dict[str, Any]] = []

    @property
    def bucket(self) -> str:
        return "private"

    async def upload(self, *, object_key: str, data: bytes, content_type: str) -> None:
        self.uploads.append(
            {"object_key": object_key, "data": data, "content_type": content_type}
        )

    async def presign_get(self, object_key: str, *, force_download: bool = False) -> str:
        self.presigns.append({"object_key": object_key, "force_download": force_download})
        return f"https://signed.example/{object_key}"

    async def delete(self, object_key: str) -> None:
        self.deletes.append(object_key)


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "green").save(buf, format="PNG")
    return buf.getvalue()


@pytest_asyncio.fixture
async def session(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    tables = [Base.metadata.tables[SysAttach.__tablename__]]
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    s = sqlite_session_factory()
    try:
        yield s
    finally:
        await s.close()


async def test_upload_avatar_persists_row_and_returns_presigned_url(
    session: AsyncSession,
) -> None:
    storage = _FakeStorage()
    service = AttachService(session, storage)  # type: ignore[arg-type]

    attach, url = await service.upload_avatar(
        _png_bytes(), original_name="me.png", actor=ACTOR, max_bytes=2 * 1024 * 1024
    )

    # Row persisted with webp content type + avatar biz type + audit columns.
    assert attach.content_type == "image/webp"
    assert attach.biz_type == AttachBizType.avatar
    assert attach.bucket == "private"
    assert attach.created_by == ACTOR.user_id
    assert attach.original_name == "me.png"
    # Object key namespaced under avatar/ and ends with the row id + .webp.
    assert attach.object_key.startswith("avatar/")
    assert attach.object_key.endswith(f"{attach.id}.webp")
    # Exactly one upload issued, body is the converted webp (not the raw png).
    assert len(storage.uploads) == 1
    assert storage.uploads[0]["object_key"] == attach.object_key
    assert storage.uploads[0]["content_type"] == "image/webp"
    # Presigned URL points at the stored key.
    assert url == f"https://signed.example/{attach.object_key}"


async def test_upload_avatar_rejects_non_image(session: AsyncSession) -> None:
    storage = _FakeStorage()
    service = AttachService(session, storage)  # type: ignore[arg-type]

    with pytest.raises(AppError) as exc_info:
        await service.upload_avatar(
            b"not-an-image", original_name="x.txt", actor=ACTOR, max_bytes=1024
        )
    assert exc_info.value.code is ErrorCode.attach_invalid_image
    assert storage.uploads == []  # Nothing uploaded on a rejected file.


async def test_upload_avatar_rejects_oversized(session: AsyncSession) -> None:
    storage = _FakeStorage()
    service = AttachService(session, storage)  # type: ignore[arg-type]
    raw = _png_bytes()

    with pytest.raises(AppError) as exc_info:
        await service.upload_avatar(
            raw, original_name="big.png", actor=ACTOR, max_bytes=len(raw) - 1
        )
    assert exc_info.value.code is ErrorCode.attach_too_large
    assert storage.uploads == []


async def test_presigned_url_for_existing_attachment(session: AsyncSession) -> None:
    storage = _FakeStorage()
    service = AttachService(session, storage)  # type: ignore[arg-type]
    attach, _ = await service.upload_avatar(
        _png_bytes(), original_name=None, actor=ACTOR, max_bytes=2 * 1024 * 1024
    )

    url = await service.presigned_url(attach.id)

    assert url == f"https://signed.example/{attach.object_key}"


async def test_presigned_url_missing_attachment_returns_none(
    session: AsyncSession,
) -> None:
    storage = _FakeStorage()
    service = AttachService(session, storage)  # type: ignore[arg-type]

    assert await service.presigned_url(999999) is None


async def test_upload_attachment_stores_raw_and_preserves_type(
    session: AsyncSession,
) -> None:
    storage = _FakeStorage()
    service = AttachService(session, storage)  # type: ignore[arg-type]
    raw = b"%PDF-1.7 fake pdf bytes"

    attach, url = await service.upload_attachment(
        raw,
        original_name="report.pdf",
        content_type="application/pdf",
        actor=ACTOR,
        max_bytes=10 * 1024 * 1024,
    )

    # Stored as-is: no transcode, declared content type + biz type preserved.
    assert attach.biz_type == AttachBizType.attachment
    assert attach.content_type == "application/pdf"
    assert attach.file_size == len(raw)
    assert attach.original_name == "report.pdf"
    # Object key namespaced under attachment/ and preserves the extension.
    assert attach.object_key.startswith("attachment/")
    assert attach.object_key.endswith(f"{attach.id}.pdf")
    # Exactly one upload, raw bytes unchanged.
    assert len(storage.uploads) == 1
    assert storage.uploads[0]["data"] == raw
    assert storage.uploads[0]["content_type"] == "application/pdf"
    assert url == f"https://signed.example/{attach.object_key}"


async def test_upload_attachment_defaults_content_type_when_missing(
    session: AsyncSession,
) -> None:
    storage = _FakeStorage()
    service = AttachService(session, storage)  # type: ignore[arg-type]

    attach, _ = await service.upload_attachment(
        b"data",
        original_name=None,
        content_type=None,
        actor=ACTOR,
        max_bytes=1024,
    )

    # No declared type and no filename → octet-stream + no extension on the key.
    assert attach.content_type == "application/octet-stream"
    assert attach.object_key.endswith(str(attach.id))


async def test_upload_attachment_rejects_oversized(session: AsyncSession) -> None:
    storage = _FakeStorage()
    service = AttachService(session, storage)  # type: ignore[arg-type]
    raw = b"0123456789"

    with pytest.raises(AppError) as exc_info:
        await service.upload_attachment(
            raw,
            original_name="big.bin",
            content_type="application/octet-stream",
            actor=ACTOR,
            max_bytes=len(raw) - 1,
        )
    assert exc_info.value.code is ErrorCode.attach_too_large
    assert storage.uploads == []  # Nothing uploaded on a rejected file.


async def test_upload_attachment_sanitizes_unsafe_extension(
    session: AsyncSession,
) -> None:
    storage = _FakeStorage()
    service = AttachService(session, storage)  # type: ignore[arg-type]

    # A traversal-flavored "extension" must not leak into the object key.
    attach, _ = await service.upload_attachment(
        b"data",
        original_name="evil.tar.gz/../../etc/passwd",
        content_type=None,
        actor=ACTOR,
        max_bytes=1024,
    )

    assert ".." not in attach.object_key
    # The final path segment is exactly the id (unsafe suffix dropped, no extension).
    assert attach.object_key.split("/")[-1] == str(attach.id)


async def test_attachment_presign_forces_download_but_avatar_inline(
    session: AsyncSession,
) -> None:
    """Generic attachments must presign with force_download (stored-XSS guard);
    avatars stay inline (already transcoded to safe webp for <img> rendering)."""
    storage = _FakeStorage()
    service = AttachService(session, storage)  # type: ignore[arg-type]

    await service.upload_avatar(
        _png_bytes(), original_name="me.png", actor=ACTOR, max_bytes=2 * 1024 * 1024
    )
    await service.upload_attachment(
        b"<svg onload=alert(1)>",
        original_name="x.svg",
        content_type="image/svg+xml",
        actor=ACTOR,
        max_bytes=1024,
    )

    by_key = {p["object_key"]: p["force_download"] for p in storage.presigns}
    avatar_keys = [k for k in by_key if k.startswith("avatar/")]
    attach_keys = [k for k in by_key if k.startswith("attachment/")]
    assert avatar_keys and by_key[avatar_keys[0]] is False
    assert attach_keys and by_key[attach_keys[0]] is True
