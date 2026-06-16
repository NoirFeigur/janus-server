from src.config import get_settings
from src.tasks.registry import TASKS
from src.tasks.schedule import cron_jobs


async def noop(_ctx: dict[str, object]) -> None:
    return None


class WorkerSettings:
    redis_settings = get_settings().redis_url
    functions = [noop, *TASKS]
    cron_jobs = cron_jobs
