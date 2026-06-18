"""Tests for trusted-proxy client IP extraction (src/core/client_ip.py)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from src.config import get_settings
from src.core.client_ip import client_ip


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Minimal Request stand-in exposing only .client and .headers."""

    def __init__(self, peer: str | None, headers: dict[str, str]) -> None:
        self.client = _FakeClient(peer) if peer is not None else None
        # Starlette headers are case-insensitive; lower-case keys suffice here.
        self.headers = {k.lower(): v for k, v in headers.items()}


def _req(peer: str | None = "10.0.0.1", **headers: str) -> Any:
    return _FakeRequest(peer, headers)


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield


def test_no_trusted_proxy_ignores_xff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "trusted_proxy_count", 0)
    req = _req(peer="10.0.0.1", **{"X-Forwarded-For": "1.2.3.4"})
    # XFF is untrusted at 0 hops → connection peer is authoritative.
    assert client_ip(req) == "10.0.0.1"


def test_one_trusted_proxy_takes_rightmost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "trusted_proxy_count", 1)
    # nginx appends the real client; spoofed leading entry must be discarded.
    req = _req(peer="10.0.0.1", **{"X-Forwarded-For": "9.9.9.9, 203.0.113.7"})
    assert client_ip(req) == "203.0.113.7"


def test_spoofed_xff_cannot_override_real_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "trusted_proxy_count", 1)
    # Client forges a single-entry XFF; nginx appends the true peer to the right.
    req = _req(peer="10.0.0.1", **{"X-Forwarded-For": "evil-spoof, 198.51.100.5"})
    assert client_ip(req) == "198.51.100.5"


def test_two_trusted_proxies_counts_from_right(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "trusted_proxy_count", 2)
    req = _req(
        peer="10.0.0.1",
        **{"X-Forwarded-For": "203.0.113.7, 172.16.0.1, 192.168.0.1"},
    )
    # 2 hops from the right → entry index len-2 == "172.16.0.1".
    assert client_ip(req) == "172.16.0.1"


def test_trusted_proxy_but_no_xff_falls_back_to_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "trusted_proxy_count", 1)
    req = _req(peer="10.0.0.1")
    assert client_ip(req) == "10.0.0.1"


def test_chain_shorter_than_proxy_count_falls_back_to_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "trusted_proxy_count", 3)
    req = _req(peer="10.0.0.1", **{"X-Forwarded-For": "203.0.113.7"})
    # Misconfigured/truncated chain → leftmost (most client-ward) is safest.
    assert client_ip(req) == "203.0.113.7"


def test_no_peer_no_xff_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "trusted_proxy_count", 0)
    req = _req(peer=None)
    assert client_ip(req) is None
