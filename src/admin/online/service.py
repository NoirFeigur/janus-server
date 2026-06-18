"""Online-session read + kick service (service layer).

Bridges the Redis-backed :class:`SessionStore` (the live allowlist) with a thin
DB lookup that resolves ``user_id`` → ``username`` for display. Two operations:
list all online sessions (optionally narrowed to one user) and force-kick a
single session by its ``access_jti``.

No data-scope: like the audit reads, online-session visibility is permission-
gated only — an operator with ``system:online:list`` sees the whole platform.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.online.repository import OnlineSessionRepository
from src.core.session_store import SessionInfo, SessionStore


@dataclass(frozen=True, slots=True)
class OnlineSession:
    """One live session enriched with the display ``username`` (None if absent)."""

    access_jti: str
    user_id: int
    username: str | None
    ip: str | None
    user_agent: str | None
    login_at: str


class OnlineSessionService:
    """List the live session allowlist and force-kick individual sessions."""

    def __init__(self, session: AsyncSession, store: SessionStore) -> None:
        self.repo = OnlineSessionRepository(session)
        self.store = store

    async def list_sessions(self, *, user_id: int | None = None) -> list[OnlineSession]:
        """List online sessions (whole platform, or one user when ``user_id`` set).

        Sorted by ``login_at`` descending (most-recent first) so the freshest
        sessions surface at the top of the online list.
        """
        infos: list[SessionInfo]
        if user_id is None:
            infos = await self.store.list_all_sessions()
        else:
            infos = await self.store.list_user_sessions(user_id)
        usernames = await self.repo.usernames_for([info.user_id for info in infos])
        enriched = [
            OnlineSession(
                access_jti=info.access_jti,
                user_id=info.user_id,
                username=usernames.get(info.user_id),
                ip=info.ip,
                user_agent=info.user_agent,
                login_at=info.login_at,
            )
            for info in infos
        ]
        enriched.sort(key=lambda s: s.login_at, reverse=True)
        return enriched

    async def kick(self, access_jti: str) -> None:
        """Force-revoke a single session by its access ``jti`` (idempotent)."""
        await self.store.revoke_access(access_jti)
