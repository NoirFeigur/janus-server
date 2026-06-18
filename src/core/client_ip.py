"""Real client IP extraction behind trusted reverse proxies.

Replicas sit behind nginx, so ``request.client.host`` is the proxy's address, not
the caller's — using it for per-IP rate limiting would bucket every user into one
counter. ``X-Forwarded-For`` carries the real chain, but a client can forge
*leading* entries, so we only trust the rightmost ``trusted_proxy_count`` hops
(the ones our own infra appended).

nginx with ``proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`` appends
the caller's address to any inbound XFF. So with one trusted proxy (nginx) the
real client is the rightmost XFF entry; a spoofed ``X-Forwarded-For: 1.2.3.4`` the
client sends lands to the *left* of the real entry and is discarded.
"""

from __future__ import annotations

from fastapi import Request

from src.config import get_settings


def client_ip(request: Request) -> str | None:
    """Resolve the caller's IP, honouring ``settings.trusted_proxy_count``.

    With ``trusted_proxy_count == 0`` (direct, no trusted proxy) the connection
    peer is authoritative and ``X-Forwarded-For`` is ignored entirely (untrusted).
    With ``N > 0`` the client is the ``N``-th entry from the right of XFF (the
    hops our infra appended); forged leading entries are dropped.
    """
    settings = get_settings()
    peer = request.client.host if request.client is not None else None

    if settings.trusted_proxy_count <= 0:
        return peer

    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer

    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if not parts:
        return peer

    # The real client is N entries from the right (N = trusted hops that append to
    # XFF). If the chain is shorter than configured, the proxy count is misconfigured
    # or the header was truncated — fall back to the leftmost (most-client-ward) entry.
    index = len(parts) - settings.trusted_proxy_count
    return parts[index] if index >= 0 else parts[0]
