"""Admin rate limits router — CRUD endpoints for rate limit rules."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.rate_limits.schemas import (
    RateLimitRuleCreate,
    RateLimitRuleRead,
    RateLimitRuleUpdate,
)
from src.admin.rate_limits.service import RateLimitService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.core.pagination import Page, page
from src.core.query import ListQuery
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/rate-limits", tags=["admin:rate-limits"])


def get_rate_limit_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> RateLimitService:
    return RateLimitService(session)


ServiceDep = Annotated[RateLimitService, Depends(get_rate_limit_service)]


@router.get("", response_model=SuccessEnvelope[Page[RateLimitRuleRead]])
async def list_rate_limit_rules(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:rate_limit:list"))],
    subject_type: str | None = None,
    subject_id: int | None = None,
    logical_model_id: int | None = None,
    status: str | None = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "asc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[RateLimitRuleRead]]:
    result = await service.list_rules(
        ListQuery(
            keyword=None,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
        ),
        subject_type=subject_type,
        subject_id=subject_id,
        logical_model_id=logical_model_id,
        rule_status=status,
    )
    return success(
        page(
            [RateLimitRuleRead.model_validate(row) for row in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        ),
        trace_id=trace_id,
    )


@router.get("/{rule_id}", response_model=SuccessEnvelope[RateLimitRuleRead])
async def get_rate_limit_rule(
    rule_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:rate_limit:query"))],
) -> SuccessEnvelope[RateLimitRuleRead]:
    rule = await service.get_rule(rule_id)
    return success(RateLimitRuleRead.model_validate(rule), trace_id=trace_id)


@router.post("", response_model=SuccessEnvelope[RateLimitRuleRead])
async def create_rate_limit_rule(
    payload: RateLimitRuleCreate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:rate_limit:add"))],
) -> SuccessEnvelope[RateLimitRuleRead]:
    rule = await service.create_rule(payload, actor=user)
    return success(RateLimitRuleRead.model_validate(rule), trace_id=trace_id)


@router.patch("/{rule_id}", response_model=SuccessEnvelope[RateLimitRuleRead])
async def update_rate_limit_rule(
    rule_id: int,
    payload: RateLimitRuleUpdate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:rate_limit:edit"))],
) -> SuccessEnvelope[RateLimitRuleRead]:
    rule = await service.update_rule(rule_id, payload, actor=user)
    return success(RateLimitRuleRead.model_validate(rule), trace_id=trace_id)


@router.delete("/{rule_id}", response_model=SuccessEnvelope[None])
async def delete_rate_limit_rule(
    rule_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("ai:rate_limit:delete"))],
) -> SuccessEnvelope[None]:
    await service.delete_rule(rule_id, actor=user)
    return success(None, trace_id=trace_id)
