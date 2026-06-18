"""Admin online-session resource package.

Exposes the management-plane endpoints for the live session allowlist
(:mod:`src.core.session_store`): list who is currently online and force-kick a
specific session. State lives entirely in Redis (no online-session table), so
this resource has no ORM model — only a thin DB read to resolve ``user_id`` →
``username`` for display.
"""

from __future__ import annotations

from src.admin.online.router import router

__all__ = ["router"]
