"""Platform config business logic (service layer).

Enforces the rules the DB does not:

- ``config_key`` is globally unique among non-deleted rows (create rejects a
  duplicate; the DB unique index is the hard backstop, this is the friendly 400).
- ``config_value`` must parse under its ``value_type`` (an ``int`` row cannot hold
  ``"abc"``); validated before persist via :func:`parse_config_value`.
- Builtin rows (``is_builtin = true``) may be updated but never deleted.

The transaction is owned by the request-level Unit of Work, not here: this layer
only ``flush()``es. Every successful mutation must invalidate the runtime cache
for that key so other replicas pick up the change within the short TTL window —
that invalidation is registered as an **after-commit hook** so it fires only
once the write actually lands (a rolled-back request leaves the cache untouched,
never publishing a phantom change).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.config.repository import SysConfigRepository
from src.admin.config.schemas import SysConfigCreate, SysConfigUpdate
from src.auth.service import AuthenticatedUser
from src.core.config_accessor import invalidate_config, parse_config_value
from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
from src.db.models.sys_config import SysConfig
from src.db.session import add_after_commit_hook
from src.enums import ConfigValueType, ErrorCode
from src.exceptions import AppError

SORT_COLUMNS = {
    "id": SysConfig.id,
    "config_key": SysConfig.config_key,
    "config_name": SysConfig.config_name,
    "created_at": SysConfig.created_at,
    "updated_at": SysConfig.updated_at,
}


class SysConfigService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = SysConfigRepository(session)

    async def _require(self, config_id: int) -> SysConfig:
        config = await self.repo.get(config_id)
        if config is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return config

    def _validate_value(self, value: str, value_type: ConfigValueType) -> None:
        """Reject a value that does not parse under its declared type (400)."""
        try:
            parse_config_value(value, value_type)
        except ValueError as exc:
            raise AppError(
                ErrorCode.request_invalid,
                status.HTTP_400_BAD_REQUEST,
                params={"value_type": value_type.value, "reason": str(exc)},
            ) from exc

    async def list_configs(
        self, *, query: ListQuery | None = None
    ) -> PageResult[SysConfig]:
        query = query or ListQuery()
        sort = resolve_sort(query, allowed=SORT_COLUMNS, default="config_key")
        total = await self.repo.count_configs(keyword=query.keyword)
        items = await self.repo.list_configs(
            keyword=query.keyword,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        return page_result(
            list(items), total=total, limit=query.limit, offset=query.offset
        )

    async def get_config(self, config_id: int) -> SysConfig:
        return await self._require(config_id)

    async def create_config(
        self, payload: SysConfigCreate, *, actor: AuthenticatedUser
    ) -> SysConfig:
        if await self.repo.get_by_key(payload.config_key) is not None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        self._validate_value(payload.config_value, payload.value_type)
        config = SysConfig(
            config_key=payload.config_key,
            config_value=payload.config_value,
            value_type=payload.value_type,
            config_name=payload.config_name,
            is_builtin=payload.is_builtin,
            remark=payload.remark,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.repo.create(config)
        await self.session.flush()
        key = config.config_key
        add_after_commit_hook(self.session, lambda: invalidate_config(key))
        return config

    async def update_config(
        self, config_id: int, payload: SysConfigUpdate, *, actor: AuthenticatedUser
    ) -> SysConfig:
        config = await self._require(config_id)
        values = payload.model_dump(exclude_unset=True)
        # The effective (value, type) after this update must parse together: a
        # type-only change must still validate the existing value, and vice versa.
        new_value = values.get("config_value", config.config_value)
        new_type = values.get("value_type", ConfigValueType(config.value_type))
        if "config_value" in values or "value_type" in values:
            self._validate_value(new_value, new_type)
        values["updated_by"] = actor.user_id
        await self.repo.update(config, **values)
        # ``updated_at`` is server-computed via onupdate=func.now(); the UPDATE
        # flush expires it (the ORM has no value), so refresh (an in-transaction
        # read) before the caller serializes the row — otherwise a lazy load
        # fires async IO in a sync context (pydantic model_validate) and raises
        # MissingGreenlet. Refresh happens pre-commit now (commit moved to the
        # request UoW edge); the row is already flushed so the read sees it.
        await self.session.flush()
        await self.session.refresh(config)
        key = config.config_key
        add_after_commit_hook(self.session, lambda: invalidate_config(key))
        return config

    async def delete_config(self, config_id: int, *, actor: AuthenticatedUser) -> None:
        config = await self._require(config_id)
        if config.is_builtin:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        config.updated_by = actor.user_id
        await self.repo.soft_delete(config)
        key = config.config_key
        add_after_commit_hook(self.session, lambda: invalidate_config(key))
