"""Attachment data access (repository layer)."""

from __future__ import annotations

from src.db.models.attach import Attach
from src.db.repository import BaseRepository


class AttachRepository(BaseRepository[Attach]):
    model = Attach
