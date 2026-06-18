"""Attachment endpoints (router layer).

``POST /attach/upload`` — backend-proxied multipart upload. Any authenticated
JWT user may upload; the file is stored in the private bucket and a ``sys_attach``
row is created. Returns the attachment id + a freshly presigned URL.

Dispatches on ``biz_type``:

- ``avatar`` — validated + transcoded to webp (size/type gated, EXIF stripped).
- ``attachment`` — generic file, size-gated only, stored as-is (type/extension
  preserved, no decode/transcode).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.auth.dependencies import CurrentJwtUser, TraceId
from src.config import get_settings
from src.core.oss import ObjectStorageDep
from src.db.models.attach import SysAttach
from src.db.session import get_session
from src.enums import AttachBizType, ErrorCode
from src.exceptions import AppError
from src.files.schemas import AttachRead
from src.files.service import AttachService
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/attach", tags=["attach"])

# 流式读取的分块大小（64KiB）。逐块累加并在越过上限的瞬间停手——绝不把整个 multipart
# 体读进内存（旧代码 `await file.read()` 在 10MB×500 并发下 = 5GB 内存面）。
_UPLOAD_CHUNK_SIZE = 64 * 1024


async def _read_capped(file: UploadFile, *, max_bytes: int) -> bytes:
    """分块读取上传体，累计超过 ``max_bytes`` 立即拒（``attach.too_large`` / 400）。

    在把字节交给业务层（解码/转码/落桶）之前先封顶内存：只要多读到第 ``max_bytes+1``
    个字节就停手并拒绝，杜绝「先整包读进内存、业务层才查大小」的内存耗尽面。这是应用层
    兜底；生产仍应在 nginx 配 ``client_max_body_size`` 做边缘第一道闸。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise AppError(ErrorCode.attach_too_large, status.HTTP_400_BAD_REQUEST)
        chunks.append(chunk)
    return b"".join(chunks)


def get_attach_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
    storage: ObjectStorageDep,
) -> AttachService:
    return AttachService(session, storage)


ServiceDep = Annotated[AttachService, Depends(get_attach_service)]


@router.post("/upload", response_model=SuccessEnvelope[AttachRead])
async def upload_attachment(
    service: ServiceDep,
    user: CurrentJwtUser,
    trace_id: TraceId,
    file: Annotated[UploadFile, File()],
    biz_type: Annotated[AttachBizType, Form()] = AttachBizType.avatar,
) -> SuccessEnvelope[AttachRead]:
    """Upload a file (backend-proxied) and return the attachment + presigned URL.

    ``avatar`` is validated as an image and transcoded to webp; ``attachment`` is
    stored as-is after a size check. Image/size errors are translated to
    ``AppError`` inside the service, so this handler stays free of
    error-translation boilerplate.
    """
    settings = get_settings()
    # Read with a hard byte cap matching the biz_type's limit so an oversized
    # body is rejected mid-stream (never fully buffered). The service still
    # re-checks size as a defense-in-depth invariant.
    max_bytes = (
        settings.avatar_max_bytes
        if biz_type is AttachBizType.avatar
        else settings.attachment_max_bytes
    )
    raw = await _read_capped(file, max_bytes=max_bytes)

    attach: SysAttach
    url: str
    if biz_type is AttachBizType.avatar:
        attach, url = await service.upload_avatar(
            raw,
            original_name=file.filename,
            actor=user,
            max_bytes=settings.avatar_max_bytes,
        )
    else:
        attach, url = await service.upload_attachment(
            raw,
            original_name=file.filename,
            content_type=file.content_type,
            actor=user,
            max_bytes=settings.attachment_max_bytes,
        )

    read = AttachRead.from_attach(attach, url=url)
    return success(read, trace_id=trace_id)
