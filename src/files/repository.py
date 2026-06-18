"""Attachment data access (repository layer)."""

from __future__ import annotations

from src.db.models.attach import SysAttach
from src.db.repository import BaseRepository


class SysAttachRepository(BaseRepository[SysAttach]):
    model = SysAttach
