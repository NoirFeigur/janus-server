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
from src.channel_health.schemas import ChannelHealthAction, ChannelHealthRead
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


# ---------------------------------------------------------------------------
# Channel health status
# ---------------------------------------------------------------------------


@router.get(
    "/channels/{channel_id}/health",
    response_model=SuccessEnvelope[ChannelHealthRead],
)
async def get_channel_health(
    channel_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:query"))],
) -> SuccessEnvelope[ChannelHealthRead]:
    """Get channel health status (degraded / healthy / disabled)."""
    from src.channel_health.redis_store import (
        get_channel_state,
        get_error_rate,
        is_degraded,
    )

    # Verify channel exists
    await service.get_channel(channel_id)

    degraded = await is_degraded(channel_id)
    state = await get_channel_state(channel_id)
    total, errors, rate = await get_error_rate(channel_id, window_seconds=300)

    status_str = "degraded" if degraded else "healthy"
    if state.get("status") == "disabled":
        status_str = "disabled"

    return success(
        ChannelHealthRead(
            channel_id=channel_id,
            status=status_str,
            error_rate=round(rate, 4) if total > 0 else None,
            total_requests=total if total > 0 else None,
            error_count=errors if errors > 0 else None,
            degraded_since=state.get("degraded_since"),
            probe_failures=int(state["probe_failures"]) if "probe_failures" in state else None,
            last_probe_at=state.get("last_probe_at"),
        ),
        trace_id=trace_id,
    )


@router.post(
    "/channels/{channel_id}/health/recover",
    response_model=SuccessEnvelope[None],
)
async def recover_channel(
    channel_id: int,
    payload: ChannelHealthAction,
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:edit"))],
) -> SuccessEnvelope[None]:
    """Manually recover a degraded channel (admin override)."""
    from src.channel_health.service import ChannelHealthService

    # Verify channel exists
    await service.get_channel(channel_id)
    health_service = ChannelHealthService()
    await health_service.record_probe_success(channel_id)
    return success(None, trace_id=trace_id)


# ---------------------------------------------------------------------------
# Catalog change log
# ---------------------------------------------------------------------------


@router.get("/changelog", response_model=SuccessEnvelope[Page[dict]])
async def list_changelog(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:catalog:list"))],
    resource_type: str | None = None,
    action: str | None = None,
    actor_id: int | None = None,
    sort_order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[dict]]:
    """List catalog change log entries."""
    from sqlalchemy import desc as sa_desc
    from sqlalchemy import func, select

    from src.db.models.catalog_ops import CatalogChangeLog

    stmt = select(CatalogChangeLog)
    if resource_type is not None:
        stmt = stmt.where(CatalogChangeLog.resource_type == resource_type)
    if action is not None:
        stmt = stmt.where(CatalogChangeLog.action == action)
    if actor_id is not None:
        stmt = stmt.where(CatalogChangeLog.actor_id == actor_id)

    if sort_order == "desc":
        stmt = stmt.order_by(sa_desc(CatalogChangeLog.id))
    else:
        stmt = stmt.order_by(CatalogChangeLog.id)

    # Count (DB-side aggregate — never materialize rows just to count them).
    count_stmt = select(func.count()).select_from(CatalogChangeLog)
    if resource_type is not None:
        count_stmt = count_stmt.where(CatalogChangeLog.resource_type == resource_type)
    if action is not None:
        count_stmt = count_stmt.where(CatalogChangeLog.action == action)
    if actor_id is not None:
        count_stmt = count_stmt.where(CatalogChangeLog.actor_id == actor_id)
    total = int(await service.session.scalar(count_stmt) or 0)

    stmt = stmt.offset(offset).limit(limit)
    result = await service.session.execute(stmt)
    items = [
        {
            "id": row.id,
            "actor_id": row.actor_id,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "action": row.action,
            "before_value": row.before_value,
            "after_value": row.after_value,
            "diff": row.diff,
            "trace_id": row.trace_id,
            "created_at": str(row.created_at) if row.created_at else None,
        }
        for row in result.scalars().all()
    ]

    return success(page(items, total=total, limit=limit, offset=offset), trace_id=trace_id)
