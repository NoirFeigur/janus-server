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

from fastapi import APIRouter, Depends, Request

from src.auth.credentials import extract_credential
from src.auth.dependencies import CurrentJwtUser, TraceId, get_auth_service
from src.auth.schemas import (
    ChangePasswordRequest,
    CurrentUserRead,
    CurrentUserUpdate,
    LoginRequest,
    RefreshRequest,
    TokenRead,
)
from src.auth.service import AuthenticatedUser, AuthService
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/auth", tags=["auth"])


def _to_current_user_read(user: AuthenticatedUser) -> CurrentUserRead:
    return CurrentUserRead(
        user_id=str(user.user_id),
        username=user.username,
        real_name=user.real_name,
        email=user.email,
        mobile=user.mobile,
        department_id=str(user.department_id) if user.department_id is not None else None,
        preferred_locale=user.preferred_locale,
        permissions=sorted(user.permissions),
        is_superuser=user.is_superuser,
    )


@router.post("/login", response_model=SuccessEnvelope[TokenRead])
async def login(
    payload: LoginRequest,
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
    trace_id: TraceId,
) -> SuccessEnvelope[TokenRead]:
    """Authenticate by username/password and issue a platform access token."""
    client = request.client
    token, ttl, refresh_token = await service.authenticate_password(
        payload.username,
        payload.password,
        request_ip=client.host if client is not None else None,
        user_agent=request.headers.get("user-agent"),
        trace_id=trace_id,
    )
    return success(
        TokenRead(access_token=token, expires_in=ttl, refresh_token=refresh_token),
        trace_id=trace_id,
    )


@router.post("/logout", response_model=SuccessEnvelope[None])
async def logout(
    request: Request,
    user: CurrentJwtUser,
    service: Annotated[AuthService, Depends(get_auth_service)],
    trace_id: TraceId,
) -> SuccessEnvelope[None]:
    """Revoke the current session so its access token stops working immediately.

    Gated by :data:`CurrentJwtUser` (a valid, non-revoked JWT). The same bearer
    token is then revoked from the session allowlist, dropping its refresh too.
    """
    credential = extract_credential(
        request.headers.get("authorization"), None, allow_api_key=False
    )
    await service.logout(credential.value)
    return success(None, trace_id=trace_id)


@router.post("/refresh", response_model=SuccessEnvelope[TokenRead])
async def refresh(
    payload: RefreshRequest,
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
    trace_id: TraceId,
) -> SuccessEnvelope[TokenRead]:
    """Rotate a refresh token into a fresh access+refresh pair.

    Public path (the access token may already be expired). The presented refresh
    is consumed and rotated; an unknown/expired/already-rotated refresh fails
    with ``auth_refresh_invalid`` (reuse additionally revokes the whole session).
    """
    client = request.client
    token, ttl, refresh_token = await service.refresh_session(
        payload.refresh_token,
        request_ip=client.host if client is not None else None,
        user_agent=request.headers.get("user-agent"),
    )
    return success(
        TokenRead(access_token=token, expires_in=ttl, refresh_token=refresh_token),
        trace_id=trace_id,
    )


@router.get("/me", response_model=SuccessEnvelope[CurrentUserRead])
async def me(user: CurrentJwtUser, trace_id: TraceId) -> SuccessEnvelope[CurrentUserRead]:
    """Return the authenticated user's profile + effective permissions."""
    return success(_to_current_user_read(user), trace_id=trace_id)


@router.put("/me", response_model=SuccessEnvelope[CurrentUserRead])
async def update_me(
    payload: CurrentUserUpdate,
    user: CurrentJwtUser,
    service: Annotated[AuthService, Depends(get_auth_service)],
    trace_id: TraceId,
) -> SuccessEnvelope[CurrentUserRead]:
    """Update self-service profile fields for the current user."""
    values: dict[str, str | None] = payload.model_dump(exclude_unset=True)
    updated = await service.update_current_user(user, values)
    return success(_to_current_user_read(updated), trace_id=trace_id)


@router.put("/me/password", response_model=SuccessEnvelope[None])
async def change_password(
    payload: ChangePasswordRequest,
    user: CurrentJwtUser,
    service: Annotated[AuthService, Depends(get_auth_service)],
    trace_id: TraceId,
) -> SuccessEnvelope[None]:
    """Change the current user's password after verifying the old password."""
    await service.change_current_password(
        user,
        old_password=payload.old_password,
        new_password=payload.new_password,
    )
    return success(None, trace_id=trace_id)
