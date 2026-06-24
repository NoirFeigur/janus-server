"""Admin API key credential business logic (service layer)."""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.credentials.repository import ApiKeyRepository
from src.admin.credentials.schemas import ApiKeyCreate, ApiKeyUpdate
from src.auth.service import AuthenticatedUser, AuthService, DataScopeFilter
from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
from src.db.models.credential import ApiKey
from src.enums import ErrorCode
from src.exceptions import AppError

SORT_COLUMNS = {
    "id": ApiKey.id,
    "user_id": ApiKey.user_id,
    "name": ApiKey.name,
    "status": ApiKey.status,
    "created_at": ApiKey.created_at,
    "expires_at": ApiKey.expires_at,
    "last_used_at": ApiKey.last_used_at,
}


class CredentialService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = ApiKeyRepository(session)
        self.auth = AuthService(session)

    async def _scope(self, actor: AuthenticatedUser) -> DataScopeFilter:
        return await self.auth.resolve_data_scope(actor)

    async def _require(self, key_id: int) -> ApiKey:
        key = await self.repo.get(key_id)
        if key is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return key

    async def _require_owner_in_scope(
        self, user_id: int, actor: AuthenticatedUser
    ) -> None:
        scope = await self._scope(actor)
        if not await self.repo.user_in_scope(user_id, scope, actor_id=actor.user_id):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def _require_visible_key(
        self, key_id: int, actor: AuthenticatedUser
    ) -> ApiKey:
        key = await self._require(key_id)
        await self._require_owner_in_scope(key.user_id, actor)
        return key

    async def list_keys(
        self,
        *,
        user_id: int | None = None,
        status_filter: str | None = None,
        query: ListQuery | None = None,
        actor: AuthenticatedUser,
    ) -> PageResult[ApiKey]:
        query = query or ListQuery()
        scope = await self._scope(actor)
        sort = resolve_sort(query, allowed=SORT_COLUMNS, default="created_at")
        total = await self.repo.count_keys(
            user_id=user_id,
            status=status_filter,
            keyword=query.keyword,
            scope_filter=scope,
            actor_id=actor.user_id,
        )
        items = await self.repo.list_keys(
            user_id=user_id,
            status=status_filter,
            keyword=query.keyword,
            scope_filter=scope,
            actor_id=actor.user_id,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        return page_result(
            list(items), total=total, limit=query.limit, offset=query.offset
        )

    async def get_key(self, key_id: int, *, actor: AuthenticatedUser) -> ApiKey:
        return await self._require_visible_key(key_id, actor)

    async def create_key(
        self, payload: ApiKeyCreate, *, actor: AuthenticatedUser
    ) -> tuple[ApiKey, str]:
        if not await self.repo.user_exists(payload.user_id):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        # An sk-key is a plaintext bearer credential. Minting one *for another
        # user* lets the actor impersonate them, so it requires superuser or a
        # dedicated grant — data-scope membership alone is not sufficient. Issuing
        # one's own key only needs the endpoint's base ``ai:credential:add`` perm.
        if payload.user_id != actor.user_id and not actor.has_permission(
            "ai:credential:issue"
        ):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)
        await self._require_owner_in_scope(payload.user_id, actor)

        plain_key = f"sk-{secrets.token_hex(24)}"
        key = ApiKey(
            user_id=payload.user_id,
            name=payload.name,
            key_hash=hashlib.sha256(plain_key.encode()).hexdigest(),
            key_prefix=plain_key[:8],
            status="active",
            expires_at=payload.expires_at,
            remark=payload.remark,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.repo.create(key)
        return key, plain_key

    async def update_key(
        self, key_id: int, payload: ApiKeyUpdate, *, actor: AuthenticatedUser
    ) -> ApiKey:
        key = await self._require_visible_key(key_id, actor)
        values = payload.model_dump(exclude_unset=True)
        values["updated_by"] = actor.user_id
        await self.repo.update(key, **values)
        await self.session.refresh(key)
        return key

    async def delete_key(self, key_id: int, *, actor: AuthenticatedUser) -> None:
        key = await self._require_visible_key(key_id, actor)
        key.updated_by = actor.user_id
        await self.repo.soft_delete(key)
