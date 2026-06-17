from arq.cron import cron

cron_jobs = [
    cron("src.tasks.registry.noop", hour=3, minute=0),
]
