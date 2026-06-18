from arq.connections import RedisSettings

from src.config import get_settings
from src.tasks.registry import TASKS, noop
from src.tasks.schedule import cron_jobs


class WorkerSettings:
    # ARQ task queue lives on the dedicated ARQ Redis db (redis_arq_url, db 1),
    # kept separate from the business cache / quota counters (redis_url, db 0) so
    # a queue backlog or FLUSHDB never collides with live auth/quota state. ARQ
    # wants a RedisSettings, not a raw DSN string — parse the configured ARQ url.
    redis_settings = RedisSettings.from_dsn(get_settings().redis_arq_url)
    functions = [noop, *TASKS]
    cron_jobs = cron_jobs
