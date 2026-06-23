from collections.abc import Callable
from typing import Any

from src.channel_health.tasks import probe_degraded_channels
from src.gateway.observability import flush_gateway_logs
from src.gateway.usage_batch import flush_usage_records

TaskFn = Callable[..., Any]

TASKS: list[TaskFn] = [flush_usage_records, flush_gateway_logs, probe_degraded_channels]


async def noop(_ctx: dict[str, object]) -> None:
    """Placeholder task so the worker + cron schedule have a resolvable target.

    Lives here (the dependency-free leaf module) rather than in ``worker`` so the
    cron schedule can reference it by import path without forming a
    ``worker → schedule → worker`` import cycle: ``arq.cron()`` resolves the
    string target eagerly at module load, so the target must sit in a module that
    does not itself import ``schedule``.
    """
    return None
