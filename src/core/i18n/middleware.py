from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from src.core.i18n.context import set_locale

RequestHandler = Callable[[Request], Awaitable[Response]]


class LocaleMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        query_locale = request.query_params.get("lang")
        cookie_locale = request.cookies.get("lang")
        header_locale = request.headers.get("accept-language")
        set_locale(query_locale or cookie_locale or header_locale)
        return await call_next(request)
