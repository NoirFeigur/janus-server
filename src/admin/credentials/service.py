"""Admin API key credential business logic (service layer)."""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.credentials.repository import ApiKeyRepository
from src.admin.credentials.schemas import ApiKeyCreate, ApiKeyUpdate
from src.auth.service import AuthenticatedUser
from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
from src.db.models.credential import ApiKey
from src.db.models.identity import User
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

    async def _require(self, key_id: int) -> ApiKey:
        key = await self.repo.get(key_id)
        if key is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return key

    async def list_keys(
        self,
        *,
        user_id: int | None = None,
        status_filter: str | None = None,
        query: ListQuery | None = None,
    ) -> PageResult[ApiKey]:
        query = query or ListQuery()
        sort = resolve_sort(query, allowed=SORT_COLUMNS, default="created_at")
        total = await self.repo.count_keys(
            user_id=user_id,
            status=status_filter,
            keyword=query.keyword,
        )
        items = await self.repo.list_keys(
            user_id=user_id,
            status=status_filter,
            keyword=query.keyword,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        return page_result(
            list(items), total=total, limit=query.limit, offset=query.offset
        )

    async def get_key(self, key_id: int) -> ApiKey:
        return await self._require(key_id)

    async def create_key(
        self, payload: ApiKeyCreate, *, actor: AuthenticatedUser
    ) -> tuple[ApiKey, str]:
        user_exists = await self.session.scalar(
            select(User.id).where(
                User.id == payload.user_id,
                User.is_deleted.is_(False),
            )
        )
        if user_exists is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

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
        key = await self._require(key_id)
        values = payload.model_dump(exclude_unset=True)
        values["updated_by"] = actor.user_id
        await self.repo.update(key, **values)
        await self.session.refresh(key)
        return key

    async def delete_key(self, key_id: int, *, actor: AuthenticatedUser) -> None:
        key = await self._require(key_id)
        key.updated_by = actor.user_id
        await self.repo.soft_delete(key)
