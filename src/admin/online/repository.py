"""Online-session data access (repository layer).

The only DB touch this resource needs: resolve a batch of ``user_id`` to their
current ``username`` for display. Session state itself lives in Redis, not the
DB, so there is no model CRUD here.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.identity import User


class OnlineSessionRepository:
    """Username resolution for online-session display."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def usernames_for(self, user_ids: Sequence[int]) -> dict[int, str]:
        """Map ``user_id -> username`` for the given ids in one query.

        Ids with no matching (or deleted) user are absent from the map; the
        caller defaults them to ``None``.
        """
        if not user_ids:
            return {}
        stmt = select(User.id, User.username).where(User.id.in_(set(user_ids)))
        result = await self.session.execute(stmt)
        return {user_id: username for user_id, username in result.all()}
