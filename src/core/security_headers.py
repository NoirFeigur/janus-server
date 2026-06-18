"""Security response headers middleware.

Applies a baseline set of defensive HTTP response headers to every response at
the application edge. Doing it in-app (not only in nginx) keeps the guarantee
consistent across replicas and survives a misconfigured / absent reverse proxy
in non-prod — the header floor travels with the code.

Scope is deliberately conservative for a JSON API + admin backend:

- ``X-Content-Type-Options: nosniff`` — stop MIME sniffing (a JSON 200 must
  never be reinterpreted as HTML/JS by the browser).
- ``X-Frame-Options: DENY`` + CSP ``frame-ancestors 'none'`` — clickjacking
  defense; this API is never meant to be framed.
- ``Referrer-Policy: no-referrer`` — never leak URLs (which may carry ids) to
  cross-origin destinations.
- ``Content-Security-Policy: default-src 'none'`` — a JSON API serves no active
  content; the tightest policy that still lets responses render as data. (The
  Swagger UI / ReDoc HTML pages are public docs paths and are exempted so their
  inline assets keep working.)
- ``Strict-Transport-Security`` — only emitted when the request arrived over
  HTTPS (or was forwarded as such by the trusted proxy), so local plain-HTTP dev
  is unaffected and we never pin HSTS on a non-TLS origin.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

RequestHandler = Callable[[Request], Awaitable[Response]]

# Static headers applied to every response. HSTS is added conditionally (HTTPS
# only) in dispatch, so it is intentionally absent here.
_BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

# A JSON API serves no active content: lock everything down and forbid framing.
_API_CSP = "default-src 'none'; frame-ancestors 'none'"

# 1 year, allow subdomains, eligible for preload. Only emitted over HTTPS.
_HSTS_VALUE = "max-age=31536000; includeSubDomains"

# Swagger UI / ReDoc are HTML docs pages with inline styles/scripts; the strict
# API CSP would blank them. They are public, static, first-party docs — exempt
# them from the CSP (the other headers still apply).
_DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})


def _is_https(request: Request) -> bool:
    """True when the request reached us over TLS.

    Trusts ``X-Forwarded-Proto`` (set by our reverse proxy that terminates TLS)
    in addition to the direct ASGI scheme, so HSTS is emitted in the normal
    nginx-fronted deployment while staying off for local plain-HTTP dev.
    """
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach baseline security headers to every response."""

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        response = await call_next(request)
        for key, value in _BASE_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path not in _DOCS_PATHS:
            response.headers.setdefault("Content-Security-Policy", _API_CSP)
        if _is_https(request):
            response.headers.setdefault("Strict-Transport-Security", _HSTS_VALUE)
        return response
