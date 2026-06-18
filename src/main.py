from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from src.admin.audit.middleware import AdminAuditMiddleware
from src.admin.router import router as admin_router
from src.auth.middleware import AuthMiddleware
from src.auth.router import router as auth_router
from src.config import get_settings
from src.core.i18n.middleware import LocaleMiddleware
from src.core.logging import bind_trace_id, clear_context, configure_logging, get_logger
from src.core.redis import close_redis
from src.core.redis import ping as redis_ping
from src.db.session import async_session_factory, engine
from src.exceptions import register_exception_handlers
from src.gateway.router import router as gateway_router

RequestHandler = Callable[[Request], Awaitable[Response]]

_log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 启动:进程级初始化结构化日志(幂等)。
    configure_logging()
    _log.info("app.startup", app=app.title)
    try:
        yield
    finally:
        # 关闭:回收 Redis 连接池(DB engine 由 SQLAlchemy 自身在进程退出时处理)。
        await close_redis()
        _log.info("app.shutdown")


class TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        trace_id = request.headers.get("x-request-id") or str(uuid4())
        request.state.trace_id = trace_id
        # 绑到 structlog contextvars:本请求协程内所有日志自动携带 trace_id。
        bind_trace_id(trace_id)
        try:
            response = await call_next(request)
        finally:
            # 清空绑定,防止 contextvars 跨请求泄漏(连接复用场景)。
            clear_context()
        response.headers["X-Request-ID"] = trace_id
        return response


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
    app.state.session_factory = async_session_factory
    app.state.api_prefix = settings.api_prefix

    # add_middleware 是 LIFO 包裹:后加的在外层。入站执行序需为
    # Locale → TraceId → Auth → AdminAudit → route,故按相反顺序添加
    # (AdminAudit 最先加 = 最内层,在 Auth 之后、路由之前运行,此时
    # request.state.user / trace_id 均已就绪)。
    app.add_middleware(AdminAuditMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(TraceIdMiddleware)
    app.add_middleware(LocaleMiddleware)
    register_exception_handlers(app)

    app.include_router(auth_router, prefix=settings.api_prefix)
    app.include_router(admin_router, prefix=settings.api_prefix)
    app.include_router(gateway_router, prefix=settings.api_prefix)

    # health 探针走裸格式,不套统一信封——它服务于 k8s/LB 存活探测,约定是
    # 极简 {"status":"ok"},不属于管理面业务 API,无 i18n / trace 语义需求。
    @app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    # readiness 真实探测下游依赖(PG + Redis):任一不可达返回 503,让 LB 把本副本
    # 摘出轮转。liveness 只表「进程活着」,readiness 表「能接流量」,语义不同。
    @app.get("/health/ready", tags=["health"])
    async def readiness() -> Response:
        checks: dict[str, str] = {}
        healthy = True
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception:
            checks["postgres"] = "down"
            healthy = False
        try:
            await redis_ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "down"
            healthy = False

        status_code = 200 if healthy else 503
        return JSONResponse(
            status_code=status_code,
            content={"status": "ok" if healthy else "unavailable", "checks": checks},
        )

    return app


app = create_app()
