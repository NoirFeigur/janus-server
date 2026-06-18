"""Admin audit-log query endpoints (router layer).

Two read-only, paged, permission-gated endpoints over the append-only audit
tables. Filtering is part of each list endpoint; there are no mutation routes
(audit rows are immutable).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.audit.schemas import LoginLogRead, OperLogRead
from src.admin.audit.service import AuditQueryService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.core.pagination import Page, page
from src.core.query import ListQuery
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/audit", tags=["admin:audit"])


def get_audit_query_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> AuditQueryService:
    return AuditQueryService(session)


ServiceDep = Annotated[AuditQueryService, Depends(get_audit_query_service)]


@router.get("/oper-logs", response_model=SuccessEnvelope[Page[OperLogRead]])
async def list_oper_logs(
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:operlog:list"))],
    keyword: str | None = None,
    module: str | None = None,
    status: str | None = None,
    actor_id: int | None = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[OperLogRead]]:
    query = ListQuery(
        keyword=keyword, sort_by=sort_by, sort_order=sort_order, limit=limit, offset=offset
    )
    result = await service.list_oper_logs(
        query=query, module=module, status=status, actor_id=actor_id
    )
    return success(
        page(
            [OperLogRead.model_validate(row) for row in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        ),
        trace_id=trace_id,
    )


@router.get("/login-logs", response_model=SuccessEnvelope[Page[LoginLogRead]])
async def list_login_logs(
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:loginlog:list"))],
    keyword: str | None = None,
    status: str | None = None,
    user_id: int | None = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[LoginLogRead]]:
    query = ListQuery(
        keyword=keyword, sort_by=sort_by, sort_order=sort_order, limit=limit, offset=offset
    )
    result = await service.list_login_logs(query=query, status=status, user_id=user_id)
    return success(
        page(
            [LoginLogRead.model_validate(row) for row in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        ),
        trace_id=trace_id,
    )
