"""Admin model-grant business logic (service layer)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.grants.repository import GrantRepository
from src.admin.grants.schemas import GrantCreate, GrantUpdate
from src.auth.service import AuthenticatedUser, AuthService
from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
from src.db.models.grant import UserModelGrant
from src.db.session import add_after_commit_hook
from src.enums import ErrorCode
from src.exceptions import AppError

SORT_COLUMNS = {
    "id": UserModelGrant.id,
    "scope": UserModelGrant.scope,
    "scope_id": UserModelGrant.scope_id,
    "logical_model_id": UserModelGrant.logical_model_id,
    "is_default": UserModelGrant.is_default,
    "created_at": UserModelGrant.created_at,
}


class GrantService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.grants = GrantRepository(session)
        self.auth = AuthService(session)

    async def _require_grant(self, grant_id: int) -> UserModelGrant:
        grant = await self.grants.get(grant_id)
        if grant is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return grant

    async def _clear_default(
        self, *, scope: str, scope_id: int, actor: AuthenticatedUser
    ) -> None:
        current = await self.grants.get_default(
            scope=scope, scope_id=scope_id, for_update=True
        )
        if current is None:
            return
        await self.grants.update(
            current,
            is_default=False,
            updated_by=actor.user_id,
        )

    async def list_grants(
        self,
        *,
        scope: str | None = None,
        scope_id: int | None = None,
        logical_model_id: int | None = None,
        query: ListQuery | None = None,
        actor: AuthenticatedUser,
    ) -> PageResult[UserModelGrant]:
        query = query or ListQuery()
        sort = resolve_sort(query, allowed=SORT_COLUMNS, default="created_at")
        total = await self.grants.count_grants(
            keyword=query.keyword,
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
        )
        items = await self.grants.list_grants(
            keyword=query.keyword,
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        return page_result(
            list(items), total=total, limit=query.limit, offset=query.offset
        )

    async def get_grant(
        self, grant_id: int, *, actor: AuthenticatedUser
    ) -> UserModelGrant:
        return await self._require_grant(grant_id)

    async def create_grant(
        self, payload: GrantCreate, *, actor: AuthenticatedUser
    ) -> UserModelGrant:
        if payload.scope == "user":
            if not await self.grants.user_exists(payload.scope_id):
                raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        elif payload.scope == "department" and not await self.grants.department_exists(
            payload.scope_id
        ):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

        if not await self.grants.model_exists(payload.logical_model_id):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

        existing = await self.grants.get_existing(
            scope=payload.scope,
            scope_id=payload.scope_id,
            logical_model_id=payload.logical_model_id,
        )
        if existing is not None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

        if payload.is_default:
            await self._clear_default(
                scope=payload.scope,
                scope_id=payload.scope_id,
                actor=actor,
            )

        grant = UserModelGrant(
            scope=payload.scope,
            scope_id=payload.scope_id,
            logical_model_id=payload.logical_model_id,
            is_default=payload.is_default,
            remark=payload.remark,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.grants.create(grant)
        hook = _bump_grant_cache_generation(payload.scope, payload.scope_id)
        add_after_commit_hook(self.session, hook)
        return grant

    async def update_grant(
        self,
        grant_id: int,
        payload: GrantUpdate,
        *,
        actor: AuthenticatedUser,
    ) -> UserModelGrant:
        grant = await self._require_grant(grant_id)
        values = payload.model_dump(exclude_unset=True)
        if values.get("is_default") is True and not grant.is_default:
            await self._clear_default(
                scope=grant.scope,
                scope_id=grant.scope_id,
                actor=actor,
            )
        values["updated_by"] = actor.user_id
        await self.grants.update(grant, **values)
        await self.session.refresh(grant)
        hook = _bump_grant_cache_generation(grant.scope, grant.scope_id)
        add_after_commit_hook(self.session, hook)
        return grant

    async def delete_grant(self, grant_id: int, *, actor: AuthenticatedUser) -> None:
        grant = await self._require_grant(grant_id)
        grant.updated_by = actor.user_id
        await self.grants.soft_delete(grant)
        hook = _bump_grant_cache_generation(grant.scope, grant.scope_id)
        add_after_commit_hook(self.session, hook)


def _bump_grant_cache_generation(
    scope: str, scope_id: int
) -> Callable[[], Awaitable[None]]:
    """Return an async callback that bumps the grant generation for a specific subject."""

    async def _hook() -> None:
        with suppress(Exception):
            from src.gateway.cache import bump_grant_generation

            if scope == "user":
                await bump_grant_generation(user_id=scope_id)
            elif scope == "department":
                await bump_grant_generation(dept_id=scope_id)

    return _hook
