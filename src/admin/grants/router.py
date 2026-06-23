"""Admin model-grant endpoints (router layer)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.grants.schemas import GrantCreate, GrantRead, GrantUpdate
from src.admin.grants.service import GrantService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.core.pagination import Page, page
from src.core.query import ListQuery
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/grants", tags=["admin:grants"])


def get_grant_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> GrantService:
    return GrantService(session)


ServiceDep = Annotated[GrantService, Depends(get_grant_service)]


def _query(
    sort_by: str | None,
    sort_order: Literal["asc", "desc"],
    limit: int,
    offset: int,
) -> ListQuery:
    return ListQuery(
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )


@router.get("", response_model=SuccessEnvelope[Page[GrantRead]])
async def list_grants(
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:grant:list"))],
    scope: Literal["user", "department"] | None = None,
    scope_id: Annotated[int | None, Query(ge=0)] = None,
    logical_model_id: Annotated[int | None, Query(ge=0)] = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "asc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[GrantRead]]:
    result = await service.list_grants(
        scope=scope,
        scope_id=scope_id,
        logical_model_id=logical_model_id,
        query=_query(sort_by, sort_order, limit, offset),
        actor=user,
    )
    return success(
        page(
            [GrantRead.model_validate(row) for row in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        ),
        trace_id=trace_id,
    )


@router.get("/{grant_id}", response_model=SuccessEnvelope[GrantRead])
async def get_grant(
    grant_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:grant:query"))],
) -> SuccessEnvelope[GrantRead]:
    grant = await service.get_grant(grant_id, actor=user)
    return success(GrantRead.model_validate(grant), trace_id=trace_id)


@router.post("", response_model=SuccessEnvelope[GrantRead])
async def create_grant(
    payload: GrantCreate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:grant:add"))],
) -> SuccessEnvelope[GrantRead]:
    grant = await service.create_grant(payload, actor=user)
    return success(GrantRead.model_validate(grant), trace_id=trace_id)


@router.put("/{grant_id}", response_model=SuccessEnvelope[GrantRead])
async def update_grant(
    grant_id: int,
    payload: GrantUpdate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:grant:edit"))],
) -> SuccessEnvelope[GrantRead]:
    grant = await service.update_grant(grant_id, payload, actor=user)
    return success(GrantRead.model_validate(grant), trace_id=trace_id)


@router.delete("/{grant_id}", response_model=SuccessEnvelope[None])
async def delete_grant(
    grant_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:grant:remove"))],
) -> SuccessEnvelope[None]:
    await service.delete_grant(grant_id, actor=user)
    return success(None, trace_id=trace_id)
