"""Admin observability router — gateway logs, DLQ, queue health endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.observability.schemas import DlqItemRead, GatewayLogRead, QueueHealthRead
from src.admin.observability.service import ObservabilityService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.core.pagination import Page, page
from src.core.query import ListQuery
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/observability", tags=["admin:observability"])


def get_observability_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> ObservabilityService:
    return ObservabilityService(session)


ServiceDep = Annotated[ObservabilityService, Depends(get_observability_service)]


@router.get("/logs", response_model=SuccessEnvelope[Page[GatewayLogRead]])
async def list_gateway_logs(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:observability:list"))],
    user_id: int | None = None,
    model: str | None = None,
    channel_id: int | None = None,
    provider: str | None = None,
    status_code: int | None = None,
    error_code: str | None = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[GatewayLogRead]]:
    result = await service.list_logs(
        ListQuery(
            keyword=None,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
        ),
        user_id=user_id,
        model=model,
        channel_id=channel_id,
        provider=provider,
        status_code=status_code,
        error_code=error_code,
    )
    return success(
        page(
            [GatewayLogRead.model_validate(row) for row in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        ),
        trace_id=trace_id,
    )


@router.get("/logs/{request_id}", response_model=SuccessEnvelope[GatewayLogRead])
async def get_gateway_log(
    request_id: str,
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:observability:query"))],
) -> SuccessEnvelope[GatewayLogRead]:
    from starlette import status as http_status

    from src.enums import ErrorCode
    from src.exceptions import AppError

    log = await service.get_log_by_request_id(request_id)
    if log is None:
        raise AppError(ErrorCode.resource_not_found, http_status.HTTP_404_NOT_FOUND)
    return success(GatewayLogRead.model_validate(log), trace_id=trace_id)


@router.get("/queue-health", response_model=SuccessEnvelope[QueueHealthRead])
async def get_queue_health(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:observability:list"))],
) -> SuccessEnvelope[QueueHealthRead]:
    health = await service.get_queue_health()
    return success(QueueHealthRead(**health), trace_id=trace_id)


@router.get("/dlq/usage", response_model=SuccessEnvelope[list[DlqItemRead]])
async def peek_usage_dlq(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:observability:list"))],
    count: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SuccessEnvelope[list[DlqItemRead]]:
    items = await service.peek_usage_dlq(count)
    return success([DlqItemRead(data=item) for item in items], trace_id=trace_id)


@router.get("/dlq/logs", response_model=SuccessEnvelope[list[DlqItemRead]])
async def peek_log_dlq(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:observability:list"))],
    count: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SuccessEnvelope[list[DlqItemRead]]:
    items = await service.peek_log_dlq(count)
    return success([DlqItemRead(data=item) for item in items], trace_id=trace_id)


@router.delete("/dlq/usage", response_model=SuccessEnvelope[int])
async def flush_usage_dlq(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:observability:delete"))],
) -> SuccessEnvelope[int]:
    count = await service.flush_usage_dlq()
    return success(count, trace_id=trace_id)


@router.delete("/dlq/logs", response_model=SuccessEnvelope[int])
async def flush_log_dlq(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:observability:delete"))],
) -> SuccessEnvelope[int]:
    count = await service.flush_log_dlq()
    return success(count, trace_id=trace_id)
