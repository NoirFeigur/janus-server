"""Online-session read DTOs (router layer contracts).

Wire model for one live session in the allowlist. ``access_jti`` is the kick
handle; ``username`` is resolved from the DB for display (not stored in Redis).
"""

from __future__ import annotations

from pydantic import BaseModel


class OnlineSessionRead(BaseModel):
    """One active session in the allowlist (online-user list row)."""

    access_jti: str
    user_id: str
    username: str | None
    ip: str | None
    user_agent: str | None
    login_at: str
