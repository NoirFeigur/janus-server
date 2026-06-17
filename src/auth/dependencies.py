"""Auth FastAPI dependencies (README: router layer wiring).

Bridges HTTP → the auth domain. Extracts the bearer credential, resolves it to
an :class:`AuthenticatedAccount` via :class:`AuthService`, and exposes a
permission-gate factory for routes.

Two credential surfaces share one resolution path:
- **Platform JWT** (admin console) — ``Authorization: Bearer <jwt>``.
- **sk-key** (programmatic) — ``Authorization: Bearer sk-...`` or the
  ``X-API-Key`` header. The ``sk-`` prefix routes to sk-key resolution.

``RequiredPerms("system:user:add")`` is the gate factory: it depends on the
current account and raises 403 (``auth_forbidden``) unless the permission is
held (super-admin ``*:*:*`` bypasses).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.auth.service import AuthenticatedAccount, AuthService
from src.db.session import get_session
from src.enums import ErrorCode
from src.exceptions import AppError

_SK_PREFIX = "sk-"


def get_auth_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthService:
    """Construct an :class:`AuthService` bound to the request session."""
    return AuthService(session)


def _extract_credential(
    authorization: str | None, x_api_key: str | None
) -> tuple[str, bool]:
    """Pull the raw credential and whether it's an sk-key.

    Precedence: ``X-API-Key`` (always sk-key) → ``Authorization: Bearer``. A
    bearer value starting with ``sk-`` is treated as an sk-key, otherwise a JWT.
    Missing/malformed → 401.
    """
    if x_api_key:
        return x_api_key, True
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value, value.startswith(_SK_PREFIX)
    raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)


async def get_current_account(
    service: Annotated[AuthService, Depends(get_auth_service)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> AuthenticatedAccount:
    """Resolve the request's credential to an authenticated principal (401 else)."""
    credential, is_api_key = _extract_credential(authorization, x_api_key)
    if is_api_key:
        return await service.resolve_api_key(credential)
    return await service.resolve_access_token(credential)


CurrentAccount = Annotated[AuthenticatedAccount, Depends(get_current_account)]


def get_trace_id(request: Request) -> str:
    """Expose the per-request trace id (set by TraceIdMiddleware) to routes."""
    trace_id: str = getattr(request.state, "trace_id", "")
    return trace_id


TraceId = Annotated[str, Depends(get_trace_id)]


class RequiredPerms:
    """Dependency factory gating a route on a single permission code.

    Usage::

        @router.post("/users", dependencies=[Depends(RequiredPerms("system:user:add"))])

    Super-admin (``*:*:*``) bypasses; lacking the code raises 403
    (``auth_forbidden``). Returns the account so routes can also inject it.
    """

    def __init__(self, permission: str) -> None:
        self.permission = permission

    async def __call__(self, account: CurrentAccount) -> AuthenticatedAccount:
        if not account.has_permission(self.permission):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)
        return account
