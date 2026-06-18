"""Audit log read service (service layer).

Wraps the append-only :class:`AuditRepository` with sort-allowlist resolution
and paging. Read-only: no create/update/delete, and **no data-scope** — audit
reads are permission-gated only, because auditors need global visibility by
design (a deliberate departure from the data-scoped admin resources).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.audit.repository import AuditRepository
from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
from src.db.models.audit import LoginLog, OperLog

OPER_SORT_COLUMNS = {
    "id": OperLog.id,
    "module": OperLog.module,
    "status": OperLog.status,
    "actor_id": OperLog.actor_id,
    "created_at": OperLog.created_at,
}

LOGIN_SORT_COLUMNS = {
    "id": LoginLog.id,
    "username": LoginLog.username,
    "status": LoginLog.status,
    "user_id": LoginLog.user_id,
    "created_at": LoginLog.created_at,
}


class AuditQueryService:
    """Read-only paged queries over the operation + login audit logs."""

    def __init__(self, session: AsyncSession) -> None:
        self.repo = AuditRepository(session)

    async def list_oper_logs(
        self,
        *,
        query: ListQuery | None = None,
        module: str | None = None,
        status: str | None = None,
        actor_id: int | None = None,
    ) -> PageResult[OperLog]:
        query = query or ListQuery()
        sort = resolve_sort(query, allowed=OPER_SORT_COLUMNS, default="created_at")
        total = await self.repo.count_oper_logs(
            keyword=query.keyword, module=module, status=status, actor_id=actor_id
        )
        items = await self.repo.list_oper_logs(
            keyword=query.keyword,
            module=module,
            status=status,
            actor_id=actor_id,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        return page_result(
            list(items), total=total, limit=query.limit, offset=query.offset
        )

    async def list_login_logs(
        self,
        *,
        query: ListQuery | None = None,
        status: str | None = None,
        user_id: int | None = None,
    ) -> PageResult[LoginLog]:
        query = query or ListQuery()
        sort = resolve_sort(query, allowed=LOGIN_SORT_COLUMNS, default="created_at")
        total = await self.repo.count_login_logs(
            keyword=query.keyword, status=status, user_id=user_id
        )
        items = await self.repo.list_login_logs(
            keyword=query.keyword,
            status=status,
            user_id=user_id,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        return page_result(
            list(items), total=total, limit=query.limit, offset=query.offset
        )
