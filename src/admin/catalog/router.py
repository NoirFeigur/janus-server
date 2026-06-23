"""Admin catalog endpoints (router layer)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.catalog.schemas import (
    ChannelKeyCreate,
    ChannelKeyRead,
    ChannelKeyRotate,
    ChannelKeyUpdate,
    LogicalModelCreate,
    LogicalModelRead,
    LogicalModelUpdate,
    ModelDeploymentCreate,
    ModelDeploymentRead,
    ModelDeploymentUpdate,
    UpstreamChannelCreate,
    UpstreamChannelRead,
    UpstreamChannelUpdate,
)
from src.admin.catalog.service import CatalogService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.core.pagination import Page, page
from src.core.query import ListQuery
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/catalog", tags=["admin:catalog"])


def get_catalog_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> CatalogService:
    return CatalogService(session)


ServiceDep = Annotated[CatalogService, Depends(get_catalog_service)]


def _query(
    keyword: str | None,
    sort_by: str | None,
    sort_order: Literal["asc", "desc"],
    limit: int,
    offset: int,
) -> ListQuery:
    return ListQuery(
        keyword=keyword,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )


@router.get("/channels", response_model=SuccessEnvelope[Page[UpstreamChannelRead]])
async def list_channels(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:list"))],
    keyword: str | None = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "asc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[UpstreamChannelRead]]:
    result = await service.list_channels(
        query=_query(keyword, sort_by, sort_order, limit, offset)
    )
    return success(
        page(
            [UpstreamChannelRead.model_validate(row) for row in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        ),
        trace_id=trace_id,
    )


@router.get("/channels/{channel_id}", response_model=SuccessEnvelope[UpstreamChannelRead])
async def get_channel(
    channel_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:query"))],
) -> SuccessEnvelope[UpstreamChannelRead]:
    channel = await service.get_channel(channel_id)
    return success(UpstreamChannelRead.model_validate(channel), trace_id=trace_id)


@router.post("/channels", response_model=SuccessEnvelope[UpstreamChannelRead])
async def create_channel(
    payload: UpstreamChannelCreate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:add"))],
) -> SuccessEnvelope[UpstreamChannelRead]:
    channel = await service.create_channel(payload, actor=user)
    return success(UpstreamChannelRead.model_validate(channel), trace_id=trace_id)


@router.put("/channels/{channel_id}", response_model=SuccessEnvelope[UpstreamChannelRead])
async def update_channel(
    channel_id: int,
    payload: UpstreamChannelUpdate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:edit"))],
) -> SuccessEnvelope[UpstreamChannelRead]:
    channel = await service.update_channel(channel_id, payload, actor=user)
    return success(UpstreamChannelRead.model_validate(channel), trace_id=trace_id)


@router.delete("/channels/{channel_id}", response_model=SuccessEnvelope[None])
async def delete_channel(
    channel_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:remove"))],
) -> SuccessEnvelope[None]:
    await service.delete_channel(channel_id, actor=user)
    return success(None, trace_id=trace_id)


@router.get("/channels/{channel_id}/keys", response_model=SuccessEnvelope[Page[ChannelKeyRead]])
async def list_keys(
    channel_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:list"))],
    keyword: str | None = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "asc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[ChannelKeyRead]]:
    result = await service.list_keys(
        channel_id=channel_id,
        query=_query(keyword, sort_by, sort_order, limit, offset),
    )
    return success(
        page(
            [ChannelKeyRead.model_validate(row) for row in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        ),
        trace_id=trace_id,
    )


@router.post("/channels/{channel_id}/keys", response_model=SuccessEnvelope[ChannelKeyRead])
async def create_key(
    channel_id: int,
    payload: ChannelKeyCreate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:add"))],
) -> SuccessEnvelope[ChannelKeyRead]:
    key = await service.create_key(
        payload.model_copy(update={"channel_id": channel_id}), actor=user
    )
    return success(ChannelKeyRead.model_validate(key), trace_id=trace_id)


@router.get("/keys/{key_id}", response_model=SuccessEnvelope[ChannelKeyRead])
async def get_key(
    key_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:query"))],
) -> SuccessEnvelope[ChannelKeyRead]:
    key = await service.get_key(key_id)
    return success(ChannelKeyRead.model_validate(key), trace_id=trace_id)


@router.put("/keys/{key_id}", response_model=SuccessEnvelope[ChannelKeyRead])
async def update_key(
    key_id: int,
    payload: ChannelKeyUpdate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:edit"))],
) -> SuccessEnvelope[ChannelKeyRead]:
    key = await service.update_key(key_id, payload, actor=user)
    return success(ChannelKeyRead.model_validate(key), trace_id=trace_id)


@router.delete("/keys/{key_id}", response_model=SuccessEnvelope[None])
async def delete_key(
    key_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:remove"))],
) -> SuccessEnvelope[None]:
    await service.delete_key(key_id, actor=user)
    return success(None, trace_id=trace_id)


@router.post("/keys/{key_id}/rotate", response_model=SuccessEnvelope[ChannelKeyRead])
async def rotate_key(
    key_id: int,
    payload: ChannelKeyRotate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:edit"))],
) -> SuccessEnvelope[ChannelKeyRead]:
    key = await service.rotate_key(key_id, payload.api_key, actor=user)
    return success(ChannelKeyRead.model_validate(key), trace_id=trace_id)


@router.get("/models", response_model=SuccessEnvelope[Page[LogicalModelRead]])
async def list_models(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:list"))],
    keyword: str | None = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "asc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[LogicalModelRead]]:
    result = await service.list_models(
        query=_query(keyword, sort_by, sort_order, limit, offset)
    )
    return success(
        page(
            [LogicalModelRead.model_validate(row) for row in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        ),
        trace_id=trace_id,
    )


@router.post("/models", response_model=SuccessEnvelope[LogicalModelRead])
async def create_model(
    payload: LogicalModelCreate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:add"))],
) -> SuccessEnvelope[LogicalModelRead]:
    model = await service.create_model(payload, actor=user)
    return success(LogicalModelRead.model_validate(model), trace_id=trace_id)


@router.get("/models/{model_id}", response_model=SuccessEnvelope[LogicalModelRead])
async def get_model(
    model_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:query"))],
) -> SuccessEnvelope[LogicalModelRead]:
    model = await service.get_model(model_id)
    return success(LogicalModelRead.model_validate(model), trace_id=trace_id)


@router.put("/models/{model_id}", response_model=SuccessEnvelope[LogicalModelRead])
async def update_model(
    model_id: int,
    payload: LogicalModelUpdate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:edit"))],
) -> SuccessEnvelope[LogicalModelRead]:
    model = await service.update_model(model_id, payload, actor=user)
    return success(LogicalModelRead.model_validate(model), trace_id=trace_id)


@router.delete("/models/{model_id}", response_model=SuccessEnvelope[None])
async def delete_model(
    model_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:remove"))],
) -> SuccessEnvelope[None]:
    await service.delete_model(model_id, actor=user)
    return success(None, trace_id=trace_id)


@router.get("/deployments", response_model=SuccessEnvelope[Page[ModelDeploymentRead]])
async def list_deployments(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:list"))],
    logical_model_id: int | None = None,
    keyword: str | None = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "asc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[ModelDeploymentRead]]:
    result = await service.list_deployments(
        logical_model_id=logical_model_id,
        query=_query(keyword, sort_by, sort_order, limit, offset),
    )
    return success(
        page(
            [ModelDeploymentRead.model_validate(row) for row in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        ),
        trace_id=trace_id,
    )


@router.post("/deployments", response_model=SuccessEnvelope[ModelDeploymentRead])
async def create_deployment(
    payload: ModelDeploymentCreate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:add"))],
) -> SuccessEnvelope[ModelDeploymentRead]:
    deployment = await service.create_deployment(payload, actor=user)
    return success(ModelDeploymentRead.model_validate(deployment), trace_id=trace_id)


@router.get("/deployments/{deployment_id}", response_model=SuccessEnvelope[ModelDeploymentRead])
async def get_deployment(
    deployment_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:query"))],
) -> SuccessEnvelope[ModelDeploymentRead]:
    deployment = await service.get_deployment(deployment_id)
    return success(ModelDeploymentRead.model_validate(deployment), trace_id=trace_id)


@router.put("/deployments/{deployment_id}", response_model=SuccessEnvelope[ModelDeploymentRead])
async def update_deployment(
    deployment_id: int,
    payload: ModelDeploymentUpdate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:edit"))],
) -> SuccessEnvelope[ModelDeploymentRead]:
    deployment = await service.update_deployment(deployment_id, payload, actor=user)
    return success(ModelDeploymentRead.model_validate(deployment), trace_id=trace_id)


@router.delete("/deployments/{deployment_id}", response_model=SuccessEnvelope[None])
async def delete_deployment(
    deployment_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:remove"))],
) -> SuccessEnvelope[None]:
    await service.delete_deployment(deployment_id, actor=user)
    return success(None, trace_id=trace_id)
