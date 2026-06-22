"""Admin usage endpoints (router layer)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status as http_status

from src.admin.usage.schemas import UsageRecordRead, UsageStats
from src.admin.usage.service import UsageService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.core.pagination import Page, page
from src.core.query import ListQuery
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/usage", tags=["admin:usage"])


def get_usage_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> UsageService:
    return UsageService(session)


ServiceDep = Annotated[UsageService, Depends(get_usage_service)]


@router.get(
    "",
    response_model=SuccessEnvelope[Page[UsageRecordRead]],
    status_code=http_status.HTTP_200_OK,
)
async def list_usage_records(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:usage:list"))],
    user_id: int | None = None,
    logical_model_id: int | None = None,
    status: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "asc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[UsageRecordRead]]:
    query = ListQuery(
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    result = await service.list_records(
        user_id=user_id,
        logical_model_id=logical_model_id,
        status=status,
        date_from=date_from,
        date_to=date_to,
        query=query,
    )
    return success(
        page(
            [UsageRecordRead.model_validate(row) for row in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        ),
        trace_id=trace_id,
    )


@router.get(
    "/stats",
    response_model=SuccessEnvelope[UsageStats],
    status_code=http_status.HTTP_200_OK,
)
async def get_usage_stats(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:usage:list"))],
    user_id: int | None = None,
    logical_model_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> SuccessEnvelope[UsageStats]:
    stats = await service.get_stats(
        user_id=user_id,
        logical_model_id=logical_model_id,
        date_from=date_from,
        date_to=date_to,
    )
    return success(stats, trace_id=trace_id)
