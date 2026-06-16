from arq.cron import cron

cron_jobs = [
    cron("src.tasks.worker.noop", hour=3, minute=0),
]
