"""Auth FastAPI dependencies (README: router layer wiring).

Bridges HTTP → the auth domain. The admin console and resource-management APIs
use JWT-only dependencies; LLM inference and MCP protocol handlers can opt into
the broader JWT-or-sk-key dependency. ``RequiredPerms`` is the admin gate
factory and therefore always uses a JWT user.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.auth.credentials import CredentialKind, extract_credential
from src.auth.service import AuthenticatedUser, AuthService
from src.db.session import get_session
from src.enums import ErrorCode
from src.exceptions import AppError


def get_auth_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> AuthService:
    """Construct an :class:`AuthService` bound to the request session."""
    return AuthService(session)


async def get_current_user(
    service: Annotated[AuthService, Depends(get_auth_service)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> AuthenticatedUser:
    """Resolve JWT or sk-key to a user (programmatic protocol surface)."""
    credential = extract_credential(authorization, x_api_key, allow_api_key=True)
    if credential.kind == CredentialKind.api_key:
        return await service.resolve_api_key(credential.value)
    return await service.resolve_access_token(credential.value)


async def get_current_jwt_user(
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> AuthenticatedUser:
    """Resolve the current admin user.

    Admin routes intentionally reject sk-key credentials. The middleware usually
    pre-populates ``request.state.user``; the header fallback keeps direct
    dependency calls and tests simple.
    """
    state_user = getattr(request.state, "user", None)
    if isinstance(state_user, AuthenticatedUser):
        if state_user.credential_kind == CredentialKind.api_key:
            raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)
        return state_user

    credential = extract_credential(authorization, x_api_key, allow_api_key=False)
    return await service.resolve_access_token(credential.value)


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]
CurrentJwtUser = Annotated[AuthenticatedUser, Depends(get_current_jwt_user)]


def get_trace_id(request: Request) -> str:
    """Expose the per-request trace id (set by TraceIdMiddleware) to routes."""
    trace_id: str = getattr(request.state, "trace_id", "")
    return trace_id


TraceId = Annotated[str, Depends(get_trace_id)]


class RequiredPerms:
    """Dependency factory gating a route on a single permission code.

    Usage::

        @router.post("/users", dependencies=[Depends(RequiredPerms("system:user:add"))])

    Super-admin (``superadmin`` role code) bypasses; lacking the code raises 403
    (``auth_forbidden``). Returns the user so routes can also inject it.
    """

    def __init__(self, permission: str) -> None:
        self.permission = permission

    async def __call__(self, user: CurrentJwtUser) -> AuthenticatedUser:
        if not user.has_permission(self.permission):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)
        return user
