"""Attachment aggregate (table sys_attach).

Generic object-storage attachment registry. Each row is the metadata for one
object stored in the private MinIO/S3 bucket: the bucket-relative ``object_key``
is the file's stable identity, while access always goes through a short-lived
presigned URL computed at read time (a private bucket has no public URL, so a
permanent URL is never stored).

Business tables reference an attachment by its snowflake id (e.g.
``sys_user.avatar``), a logical reference with no physical FK (§0.7). The
``biz_type`` column classifies the usage (avatar / generic attachment) so future
features (exports, business uploads) reuse this one table.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import BaseEntity
from src.enums import AttachBizType


class SysAttach(BaseEntity):
    """Object-storage attachment metadata (one row per stored object)."""

    __tablename__ = "sys_attach"
    __table_args__ = (
        Index(
            "uq_sysattach_object_key", "object_key", unique=True
        ),  # One row per stored object; the key is the file's stable identity.
        {"comment": "附件：对象存储文件元数据；私有桶读取走短期预签名 URL，DB 只存 object key"},
    )

    object_key: Mapped[str] = mapped_column(
        String(512),
        comment="桶内相对路径，如 avatar/2026/06/{id}.webp",
    )
    bucket: Mapped[str] = mapped_column(
        String(128),
        comment="所在桶名（多桶/迁移/审计用）",
    )
    original_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="上传时原始文件名（下载用）"
    )
    content_type: Mapped[str] = mapped_column(
        String(128), comment="MIME 类型，如 image/webp"
    )
    file_size: Mapped[int] = mapped_column(BigInteger, comment="文件字节数")
    biz_type: Mapped[str] = mapped_column(
        String(32),
        default=AttachBizType.attachment,
        index=True,
        comment="用途 AttachBizType：avatar | attachment",
    )
