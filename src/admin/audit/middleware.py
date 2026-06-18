"""Operation-audit middleware (admin write 留痕).

Records one ``oper_log`` row for every admin-console **write** (POST/PUT/PATCH/
DELETE under ``/admin``). Design (Oracle-reviewed):

- **Pure middleware for v1.** HTTP-level facts (actor, method, path, result,
  latency) are cleanest at the outer boundary; ``before_value``/``after_value``
  stay ``NULL`` until a snapshot requirement appears (the model marks them
  "不适用").
- **Synchronous append in a *fresh* session after ``call_next``.** The route's
  own session is already closed by the time this outer middleware regains
  control, so the audit row is written in a separate session — deliberately
  *not* atomic with the business mutation (best-effort audit-after-response).
  Append is wrapped so a failure **never** breaks the user's request.
- **Status from ``response.status_code``** (``>=400`` ⇒ failure); ``error_code``
  is read opportunistically from ``request.state.error_code`` (stashed by the
  exception handlers) — the streamed response body is never buffered.

Ordering (see ``src.main``): ``Locale → TraceId → Auth → AdminAudit → route``,
so ``request.state.user`` and ``request.state.trace_id`` are both populated when
this middleware runs.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette import status as http_status
from starlette.middleware.base import BaseHTTPMiddleware

from src.admin.audit.repository import AuditRepository
from src.auth.middleware import _strip_api_prefix
from src.auth.service import AuthenticatedUser
from src.core.logging import get_logger
from src.db.models.audit import OperLog
from src.db.session import async_session_factory
from src.enums import AuditOutcome

RequestHandler = Callable[[Request], Awaitable[Response]]

_log = get_logger(__name__)

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ADMIN_PREFIX = "/admin/"

# 资源段(复数)→ 业务模块名(单数,与 OperLog.module 注释示例对齐)。
_MODULE_BY_SEGMENT: dict[str, str] = {
    "users": "user",
    "roles": "role",
    "menus": "menu",
    "departments": "dept",
}


def should_audit(method: str, path: str) -> bool:
    """Whether this request is an admin write that should be audited.

    Only POST/PUT/PATCH/DELETE under ``/admin/...``. Reads and non-admin paths
    (login/logout/refresh/health) are excluded — login is audited separately by
    the login-log path.
    """
    if method.upper() not in _WRITE_METHODS:
        return False
    return path.startswith(_ADMIN_PREFIX)


def classify(method: str, path: str) -> tuple[str, str, str | None]:
    """Derive ``(module, action, target_id)`` from an admin write method+path.

    - ``module`` from the resource segment (``users``→``user`` …); unknown
      segments fall back to the raw segment so new endpoints stay *visible*.
    - ``action``: ``batch-delete`` segment ⇒ ``batch_delete``; else by method
      (POST=create, PUT/PATCH=update, DELETE=delete).
    - ``target_id``: the trailing numeric path segment for ``/{id}`` routes;
      ``None`` for collection routes (create) and batch endpoints.
    """
    segments = [s for s in path.split("/") if s]
    # segments[0] == "admin"; segments[1] == resource; segments[2:] == id/sub.
    resource = segments[1] if len(segments) > 1 else "unknown"
    module = _MODULE_BY_SEGMENT.get(resource, resource)

    tail = segments[2] if len(segments) > 2 else None
    if tail == "batch-delete":
        return module, "batch_delete", None

    method_upper = method.upper()
    if method_upper == "POST":
        action = "create"
    elif method_upper in {"PUT", "PATCH"}:
        action = "update"
    else:  # DELETE
        action = "delete"

    target_id = tail if (tail is not None and tail.isdigit()) else None
    return module, action, target_id


class AdminAuditMiddleware(BaseHTTPMiddleware):
    """Append an ``oper_log`` row for every admin write (best-effort)."""

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        api_prefix = getattr(request.app.state, "api_prefix", "")
        path = _strip_api_prefix(request.url.path, api_prefix)
        if not should_audit(request.method, path):
            return await call_next(request)

        started = time.perf_counter()
        response = await call_next(request)
        latency_ms = int((time.perf_counter() - started) * 1000)

        try:
            await self._record(request, response, path=path, latency_ms=latency_ms)
        except Exception:  # noqa: BLE001 — audit must never break the request.
            _log.warning("audit.oper_log.append_failed", path=path, exc_info=True)

        return response

    async def _record(
        self,
        request: Request,
        response: Response,
        *,
        path: str,
        latency_ms: int,
    ) -> None:
        module, action, target_id = classify(request.method, path)

        actor = getattr(request.state, "user", None)
        actor_id: int | None = None
        actor_name: str | None = None
        if isinstance(actor, AuthenticatedUser):
            actor_id = actor.user_id
            actor_name = actor.username

        succeeded = response.status_code < http_status.HTTP_400_BAD_REQUEST
        status_value = (
            AuditOutcome.success.value if succeeded else AuditOutcome.failure.value
        )
        error_code: str | None = None
        if not succeeded:
            stashed = getattr(request.state, "error_code", None)
            error_code = str(stashed) if stashed is not None else None

        client = request.client
        request_ip = client.host if client is not None else None

        row = OperLog(
            actor_id=actor_id,
            actor_name=actor_name,
            module=module,
            action=action,
            method=request.method.upper(),
            path=request.url.path,
            target_id=target_id,
            request_ip=request_ip,
            user_agent=request.headers.get("user-agent"),
            trace_id=getattr(request.state, "trace_id", None),
            before_value=None,
            after_value=None,
            status=status_value,
            error_code=error_code,
            latency_ms=latency_ms,
        )

        session_factory = getattr(
            request.app.state, "session_factory", async_session_factory
        )
        async with session_factory() as session:
            repo = AuditRepository(session)
            await repo.append_oper_log(row)
            await session.commit()
