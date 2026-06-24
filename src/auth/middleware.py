"""Central authentication middleware.

This middleware gives the service a default-deny authentication layer: public
paths are explicit, admin/auth/resource-management endpoints require JWT, and
only LLM inference endpoints plus the MCP protocol endpoint may use sk-key. RBAC
remains in route dependencies.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from src.auth.credentials import CredentialKind, extract_credential
from src.auth.service import AuthService
from src.db.session import async_session_factory
from src.exceptions import AppError, error_envelope

RequestHandler = Callable[[Request], Awaitable[Response]]

PUBLIC_PATHS = frozenset(
    {
        "/auth/login",
        "/auth/refresh",
        "/health/live",
        "/health/ready",
        "/docs",
        "/docs/oauth2-redirect",
        "/openapi.json",
        "/redoc",
    }
)

LLM_API_KEY_PATHS = frozenset(
    {
        "/v1/chat/completions",
        "/v1/embeddings",
        "/v1/messages",
        "/v1/models",
        "/v1/responses",
    }
)
LLM_API_KEY_METHODS = frozenset({"GET", "POST"})
MCP_API_KEY_PATHS = frozenset({"/mcp", "/mcp/"})
MCP_API_KEY_METHODS = frozenset({"GET", "POST"})
GEMINI_API_KEY_ACTIONS = (":generateContent", ":streamGenerateContent")


def _is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS


def _is_gemini_generation_path(path: str) -> bool:
    prefix = "/v1beta/models/"
    if not path.startswith(prefix):
        return False
    model_and_action = path.removeprefix(prefix)
    return bool(model_and_action) and model_and_action.endswith(GEMINI_API_KEY_ACTIONS)


def _allows_api_key(path: str, method: str) -> bool:
    if method in LLM_API_KEY_METHODS and path in LLM_API_KEY_PATHS:
        return True
    if method == "POST" and _is_gemini_generation_path(path):
        return True
    return method in MCP_API_KEY_METHODS and path in MCP_API_KEY_PATHS


def _strip_api_prefix(path: str, api_prefix: str) -> str:
    if not api_prefix or api_prefix == "/":
        return path
    prefix = api_prefix.rstrip("/")
    if path == prefix:
        return "/"
    if path.startswith(f"{prefix}/"):
        return path[len(prefix) :]
    return path


class AuthMiddleware(BaseHTTPMiddleware):
    """Populate ``request.state.user`` for every non-public request."""

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        api_prefix = getattr(request.app.state, "api_prefix", "")
        path = _strip_api_prefix(request.url.path, api_prefix)
        if _is_public_path(path):
            return await call_next(request)

        try:
            allow_api_key = _allows_api_key(path, request.method.upper())
            credential = extract_credential(
                request.headers.get("authorization"),
                request.headers.get("x-api-key"),
                allow_api_key=allow_api_key,
            )

            session_factory = getattr(request.app.state, "session_factory", async_session_factory)
            async with session_factory() as session:
                service = AuthService(session)
                if credential.kind == CredentialKind.api_key:
                    request.state.user = await service.resolve_api_key(credential.value)
                else:
                    request.state.user = await service.resolve_access_token(credential.value)
        except AppError as exc:
            return error_envelope(
                request,
                code=exc.code,
                status_code=exc.status_code,
                params=exc.params,
            )

        return await call_next(request)
