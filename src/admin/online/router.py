"""Admin online-session endpoints (router layer).

Two permission-gated endpoints over the Redis session allowlist:
- ``GET    /online/sessions``            — list who is currently online.
- ``DELETE /online/sessions/{jti}``      — force-kick one session.

State is Redis-only; the service does a thin DB read for username display. The
router is thin: resolve deps, call the service, wrap in the success envelope.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.online.schemas import OnlineSessionRead
from src.admin.online.service import OnlineSession, OnlineSessionService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.core.redis import get_redis
from src.core.session_store import SessionStore
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/online", tags=["admin:online"])


def get_online_session_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> OnlineSessionService:
    return OnlineSessionService(session, SessionStore(get_redis()))


ServiceDep = Annotated[OnlineSessionService, Depends(get_online_session_service)]


def _to_read(item: OnlineSession) -> OnlineSessionRead:
    return OnlineSessionRead(
        access_jti=item.access_jti,
        user_id=str(item.user_id),
        username=item.username,
        ip=item.ip,
        user_agent=item.user_agent,
        login_at=item.login_at,
    )


@router.get("/sessions", response_model=SuccessEnvelope[list[OnlineSessionRead]])
async def list_online_sessions(
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:online:list"))],
    user_id: int | None = Query(default=None, ge=1),
) -> SuccessEnvelope[list[OnlineSessionRead]]:
    """List active sessions (whole platform, or one user via ``user_id``)."""
    sessions = await service.list_sessions(user_id=user_id)
    return success([_to_read(s) for s in sessions], trace_id=trace_id)


@router.delete("/sessions/{access_jti}", response_model=SuccessEnvelope[None])
async def kick_session(
    access_jti: str,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:online:kick"))],
) -> SuccessEnvelope[None]:
    """Force-kick one session by its access ``jti`` (idempotent)."""
    await service.kick(access_jti)
    return success(None, trace_id=trace_id)
