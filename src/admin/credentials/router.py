"""Admin API key credential endpoints (router layer)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.credentials.schemas import (
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyRead,
    ApiKeyUpdate,
)
from src.admin.credentials.service import CredentialService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.core.pagination import Page, page
from src.core.query import ListQuery
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/credentials", tags=["admin:credentials"])


def get_credential_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> CredentialService:
    return CredentialService(session)


ServiceDep = Annotated[CredentialService, Depends(get_credential_service)]


@router.get("", response_model=SuccessEnvelope[Page[ApiKeyRead]])
async def list_keys(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:credential:list"))],
    user_id: int | None = None,
    status: str | None = None,
    keyword: str | None = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "asc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[ApiKeyRead]]:
    query = ListQuery(
        keyword=keyword,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    result = await service.list_keys(
        user_id=user_id,
        status_filter=status,
        query=query,
    )
    return success(
        page(
            [ApiKeyRead.model_validate(row) for row in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        ),
        trace_id=trace_id,
    )


@router.get("/{key_id}", response_model=SuccessEnvelope[ApiKeyRead])
async def get_key(
    key_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:credential:query"))],
) -> SuccessEnvelope[ApiKeyRead]:
    key = await service.get_key(key_id)
    return success(ApiKeyRead.model_validate(key), trace_id=trace_id)


@router.post("", response_model=SuccessEnvelope[ApiKeyCreateResponse])
async def create_key(
    payload: ApiKeyCreate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:credential:add"))],
) -> SuccessEnvelope[ApiKeyCreateResponse]:
    key, plain_key = await service.create_key(payload, actor=user)
    read = ApiKeyCreateResponse.model_validate(
        {**ApiKeyRead.model_validate(key).model_dump(), "plain_key": plain_key}
    )
    return success(read, trace_id=trace_id)


@router.put("/{key_id}", response_model=SuccessEnvelope[ApiKeyRead])
async def update_key(
    key_id: int,
    payload: ApiKeyUpdate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:credential:edit"))],
) -> SuccessEnvelope[ApiKeyRead]:
    key = await service.update_key(key_id, payload, actor=user)
    return success(ApiKeyRead.model_validate(key), trace_id=trace_id)


@router.delete("/{key_id}", response_model=SuccessEnvelope[None])
async def delete_key(
    key_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:credential:remove"))],
) -> SuccessEnvelope[None]:
    await service.delete_key(key_id, actor=user)
    return success(None, trace_id=trace_id)
