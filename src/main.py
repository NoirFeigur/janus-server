from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from src.admin.router import router as admin_router
from src.auth.router import router as auth_router
from src.config import get_settings
from src.core.i18n.middleware import LocaleMiddleware
from src.exceptions import register_exception_handlers
from src.gateway.router import router as gateway_router

RequestHandler = Callable[[Request], Awaitable[Response]]


class TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        trace_id = request.headers.get("x-request-id") or str(uuid4())
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = trace_id
        return response


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, debug=settings.debug)

    app.add_middleware(TraceIdMiddleware)
    app.add_middleware(LocaleMiddleware)
    register_exception_handlers(app)

    app.include_router(auth_router, prefix=settings.api_prefix)
    app.include_router(admin_router, prefix=settings.api_prefix)
    app.include_router(gateway_router, prefix=settings.api_prefix)

    @app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def readiness() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
