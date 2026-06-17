"""Tests for structured logging setup (src/core/logging.py).

These assert behavior contracts — that configure_logging is idempotent, that
trace_id binding flows through structlog's contextvars into emitted events, and
that clearing context removes the binding. They never snapshot rendered output
strings (which would be change-detector tests).
"""

from __future__ import annotations

import structlog

from src.core.logging import (
    bind_trace_id,
    clear_context,
    configure_logging,
    get_logger,
)


def test_configure_logging_is_idempotent() -> None:
    """重复调用 configure_logging() 不应抛错或重复装配。"""
    configure_logging()
    configure_logging()  # 第二次是 no-op
    assert structlog.is_configured()


def test_get_logger_returns_bound_logger() -> None:
    configure_logging()
    logger = get_logger("test.module")
    assert logger is not None
    # structlog logger 暴露标准日志方法。
    assert hasattr(logger, "info")
    assert hasattr(logger, "error")


def test_bind_trace_id_writes_into_contextvars() -> None:
    """bind_trace_id() 把 trace_id 写入 structlog contextvars。

    配置链里的 merge_contextvars 会在渲染时读取这些 contextvars,从而让该请求协程内
    所有日志自动携带 trace_id。这里断言的是真实契约——绑定写进了 contextvars——而非
    用 capture_logs()(后者会替换整条处理链,绕过 merge_contextvars,测不到该行为)。
    """
    configure_logging()
    clear_context()
    bind_trace_id("trace-abc")
    assert structlog.contextvars.get_contextvars().get("trace_id") == "trace-abc"
    clear_context()


def test_merge_contextvars_in_configured_chain() -> None:
    """配置链确实包含 merge_contextvars(绑定的 trace_id 才能流进日志输出)。"""
    configure_logging()
    config = structlog.get_config()
    assert structlog.contextvars.merge_contextvars in config["processors"]


def test_clear_context_removes_trace_id() -> None:
    """clear_context() 后,contextvars 不再携带先前的 trace_id。"""
    configure_logging()
    bind_trace_id("trace-xyz")
    clear_context()
    assert "trace_id" not in structlog.contextvars.get_contextvars()
