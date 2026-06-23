"""Admin observability package — gateway logs, DLQ, queue health."""

from __future__ import annotations

from src.admin.observability.router import router

__all__ = ["router"]
