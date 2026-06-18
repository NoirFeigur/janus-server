from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from src.admin.audit.middleware import AdminAuditMiddleware
from src.admin.router import router as admin_router
from src.auth.middleware import AuthMiddleware
from src.auth.router import router as auth_router
from src.config import get_settings, validate_runtime
from src.core.i18n.middleware import LocaleMiddleware
from src.core.logging import bind_trace_id, clear_context, configure_logging, get_logger
from src.core.redis import close_redis
from src.core.redis import ping as redis_ping
from src.core.security_headers import SecurityHeadersMiddleware
from src.core.worker_id import acquire_worker_id
from src.db.session import async_session_factory, engine
from src.exceptions import register_exception_handlers
from src.files.router import router as attach_router
from src.gateway.router import router as gateway_router

RequestHandler = Callable[[Request], Awaitable[Response]]

_log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 启动:进程级初始化结构化日志(幂等)。
    configure_logging()
    _log.info("app.startup", app=app.title)
    # 生产配置 fail-fast:非 local 环境缺 JWT key / 开 debug / CORS 通配 / 未设信任代理
    # 跳数,直接拒启动(胜过到首次登录才在 500 里暴露,或带着跨域凭据窃取洞上线)。
    validate_runtime(get_settings())
    # 租约一个唯一的 snowflake worker-id(多副本防主键撞车);生产拿不到即 fail-fast,
    # local 回落 0。返回的 lease 持有后台心跳续租,关闭时释放。
    worker_id_lease = await acquire_worker_id()
    if worker_id_lease is not None:
        worker_id_lease.start_heartbeat()
    try:
        yield
    finally:
        # 关闭:释放 worker-id 租约(停心跳 + compare-and-delete),再回收 Redis 连接池
        # (DB engine 由 SQLAlchemy 自身在进程退出时处理)。
        if worker_id_lease is not None:
            await worker_id_lease.release()
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
    # SecurityHeaders → CORS → Locale → TraceId → Auth → AdminAudit → route,故按相反
    # 顺序添加(AdminAudit 最先加 = 最内层,在 Auth 之后、路由之前运行,此时
    # request.state.user / trace_id 均已就绪)。
    app.add_middleware(AdminAuditMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(TraceIdMiddleware)
    app.add_middleware(LocaleMiddleware)
    # CORS 在安全头之内:跨域预检(OPTIONS)在鉴权前被拦截响应,浏览器才能拿到
    # Access-Control-* 头。仅当配置了 origins 才挂载(默认空 = 同源部署,不开放跨域)。
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    # 安全响应头最后加 = 最外层:对每个响应(含 CORS 预检、错误信封、docs)兜底加
    # nosniff / 防点击劫持 / CSP / 条件 HSTS,不依赖 nginx 是否配齐。
    app.add_middleware(SecurityHeadersMiddleware)
    register_exception_handlers(app)

    app.include_router(auth_router, prefix=settings.api_prefix)
    app.include_router(admin_router, prefix=settings.api_prefix)
    app.include_router(attach_router, prefix=settings.api_prefix)
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
