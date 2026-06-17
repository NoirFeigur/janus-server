"""Auth domain HTTP endpoints (router layer).

Two endpoints:
- ``POST /auth/login`` — username/password → platform access token.
- ``GET  /auth/me``    — the current principal's profile + effective perms.

Responses use the management-plane :class:`SuccessEnvelope`; failures raise
:class:`AppError` (rendered as the uniform error envelope by the global
handlers). The trace id is injected via the :data:`TraceId` dependency so the
envelope's ``trace_id`` matches the ``X-Request-ID`` header.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from src.auth.dependencies import CurrentAccount, TraceId, get_auth_service
from src.auth.schemas import CurrentUserRead, LoginRequest, TokenRead
from src.auth.service import AuthService
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=SuccessEnvelope[TokenRead])
async def login(
    payload: LoginRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
    trace_id: TraceId,
) -> SuccessEnvelope[TokenRead]:
    """Authenticate by username/password and issue a platform access token."""
    token, ttl = await service.authenticate_password(payload.username, payload.password)
    return success(
        TokenRead(access_token=token, expires_in=ttl),
        trace_id=trace_id,
    )


@router.get("/me", response_model=SuccessEnvelope[CurrentUserRead])
async def me(account: CurrentAccount, trace_id: TraceId) -> SuccessEnvelope[CurrentUserRead]:
    """Return the authenticated principal's profile + effective permissions."""
    return success(
        CurrentUserRead(
            account_id=str(account.account_id),
            username=account.username,
            department_id=str(account.department_id)
            if account.department_id is not None
            else None,
            permissions=sorted(account.permissions),
            is_superuser=account.is_superuser,
        ),
        trace_id=trace_id,
    )
