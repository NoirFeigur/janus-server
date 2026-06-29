"""Attachment DTOs (router layer contracts).

Snowflake ``id`` serializes as a string (JS int64 precision). ``url`` is the
freshly computed presigned GET URL — short-lived, recomputed on every read (a
private bucket has no permanent URL).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_serializer

from src.db.models.attach import Attach
from src.enums import AttachBizType


class AttachRead(BaseModel):
    """Uploaded attachment + a presigned URL to fetch it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str  # Freshly computed presigned GET URL (short-lived).
    original_name: str | None
    content_type: str
    file_size: int
    biz_type: AttachBizType

    @classmethod
    def from_attach(cls, attach: Attach, *, url: str) -> AttachRead:
        """Build the read model from a persisted row + its freshly presigned URL.

        ``url`` is not a column (a private bucket has no permanent URL — it is
        recomputed per read), so it is supplied separately rather than read off
        the ORM object.
        """
        return cls(
            id=attach.id,
            url=url,
            original_name=attach.original_name,
            content_type=attach.content_type,
            file_size=attach.file_size,
            biz_type=AttachBizType(attach.biz_type),
        )

    @field_serializer("id")
    def _ser_id(self, value: int) -> str:
        return str(value)
