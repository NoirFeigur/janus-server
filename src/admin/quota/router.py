"""Admin quota rule endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.quota.schemas import QuotaCreate, QuotaRead, QuotaUpdate
from src.admin.quota.service import QuotaService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.core.pagination import Page, page
from src.core.query import ListQuery
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/quotas", tags=["admin:quotas"])


def get_quota_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> QuotaService:
    return QuotaService(session)


ServiceDep = Annotated[QuotaService, Depends(get_quota_service)]


@router.get("", response_model=SuccessEnvelope[Page[QuotaRead]])
async def list_quotas(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:quota:list"))],
    scope: str | None = None,
    scope_id: int | None = None,
    logical_model_id: int | None = None,
    status: str | None = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "asc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[QuotaRead]]:
    query = ListQuery(
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    result = await service.list_quotas(
        scope=scope,
        scope_id=scope_id,
        logical_model_id=logical_model_id,
        status_filter=status,
        query=query,
    )
    return success(
        page(
            [QuotaRead.model_validate(row) for row in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        ),
        trace_id=trace_id,
    )


@router.get("/{quota_id}", response_model=SuccessEnvelope[QuotaRead])
async def get_quota(
    quota_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:quota:query"))],
) -> SuccessEnvelope[QuotaRead]:
    quota = await service.get_quota(quota_id)
    return success(QuotaRead.model_validate(quota), trace_id=trace_id)


@router.post("", response_model=SuccessEnvelope[QuotaRead])
async def create_quota(
    payload: QuotaCreate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:quota:add"))],
) -> SuccessEnvelope[QuotaRead]:
    quota = await service.create_quota(payload, actor=user)
    return success(QuotaRead.model_validate(quota), trace_id=trace_id)


@router.put("/{quota_id}", response_model=SuccessEnvelope[QuotaRead])
async def update_quota(
    quota_id: int,
    payload: QuotaUpdate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:quota:edit"))],
) -> SuccessEnvelope[QuotaRead]:
    quota = await service.update_quota(quota_id, payload, actor=user)
    return success(QuotaRead.model_validate(quota), trace_id=trace_id)


@router.delete("/{quota_id}", response_model=SuccessEnvelope[None])
async def delete_quota(
    quota_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:quota:remove"))],
) -> SuccessEnvelope[None]:
    await service.delete_quota(quota_id, actor=user)
    return success(None, trace_id=trace_id)
