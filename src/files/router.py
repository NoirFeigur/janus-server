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

from src.auth.dependencies import CurrentJwtUser, TraceId
from src.config import get_settings
from src.core.oss import ObjectStorageDep
from src.db.models.attach import SysAttach
from src.db.session import get_session
from src.enums import AttachBizType
from src.files.schemas import AttachRead
from src.files.service import AttachService
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/attach", tags=["attach"])


def get_attach_service(
    session: Annotated[AsyncSession, Depends(get_session)],
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
    raw = await file.read()

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
