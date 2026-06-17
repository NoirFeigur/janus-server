"""结构化日志配置（structlog）。

全异步服务里，请求级上下文（trace_id 等）必须随 ``contextvars`` 流动，不能用
线程局部或手动透传参数。这里用 structlog 原生的 ``merge_contextvars`` 处理器：
中间件用 ``bind_trace_id()`` 把 trace_id 绑到当前 context，之后该请求协程内任何
``get_logger().info(...)`` 都自动带上 trace_id，无需逐层传参。

``configure_logging()`` 在应用启动（lifespan）调用一次，进程级幂等：
- ``log_json=True`` → JSON 行（生产/日志采集）
- ``log_json=False`` → 彩色控制台（本地开发）
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

from src.config import get_settings

_configured = False


def configure_logging() -> None:
    """进程级初始化 structlog + stdlib logging。幂等（重复调用是 no-op）。"""
    global _configured
    if _configured:
        return

    settings = get_settings()
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)

    # 共享处理器链：contextvars 合并 → 元信息 → 时间戳。最终渲染器按 log_json 切换。
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 让 stdlib logging（uvicorn/sqlalchemy 等第三方库）也走根级别，避免噪音。
    logging.basicConfig(level=level, format="%(message)s")

    _configured = True


def get_logger(name: str | None = None) -> Any:
    """返回一个 structlog logger。``name`` 可选，用于标识来源模块。"""
    return structlog.get_logger(name) if name else structlog.get_logger()


def bind_trace_id(trace_id: str) -> None:
    """把 trace_id 绑到当前 contextvars，本请求协程内所有日志自动携带。"""
    structlog.contextvars.bind_contextvars(trace_id=trace_id)


def clear_context() -> None:
    """清空当前 contextvars 绑定（请求结束时调用，防止跨请求泄漏）。"""
    structlog.contextvars.clear_contextvars()
