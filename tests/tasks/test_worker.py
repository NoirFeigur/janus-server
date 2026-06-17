"""Import + minimal-exercise coverage for the arq task scaffolding.

These modules are wiring scaffolds (a task registry, a cron schedule, and the
arq ``WorkerSettings``) with no behaviour yet beyond the ``noop`` placeholder.
The tests import them (covering the module bodies) and exercise the one callable
(``noop``) plus assert the structural invariants the worker depends on, so the
scaffolding stays internally consistent as real tasks are added.
"""

from __future__ import annotations

import pytest

from src.tasks import registry, schedule, worker


def test_registry_tasks_is_a_list() -> None:
    assert isinstance(registry.TASKS, list)


def test_schedule_defines_cron_jobs() -> None:
    assert isinstance(schedule.cron_jobs, list)
    assert len(schedule.cron_jobs) >= 1


def test_worker_settings_wires_noop_and_cron() -> None:
    # noop is always registered; any registry tasks are appended after it.
    assert registry.noop in worker.WorkerSettings.functions
    assert worker.WorkerSettings.cron_jobs is schedule.cron_jobs
    # redis_settings is sourced from config (a url string in this scaffold).
    assert worker.WorkerSettings.redis_settings


@pytest.mark.asyncio
async def test_noop_returns_none() -> None:
    assert await registry.noop({}) is None
