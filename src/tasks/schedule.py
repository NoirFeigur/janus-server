from arq.cron import cron

_EVERY_5S = {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}
_EVERY_5S_OFFSET = {2, 7, 12, 17, 22, 27, 32, 37, 42, 47, 52, 57}

cron_jobs = [
    # Flush pending usage records from Redis queue → DB every 5 seconds.
    cron("src.gateway.usage_batch.flush_usage_records", second=_EVERY_5S),
    # Flush pending gateway request logs from Redis queue → DB every 5 seconds.
    cron("src.gateway.observability.flush_gateway_logs", second=_EVERY_5S_OFFSET),
    # Probe degraded channels every minute.
    cron("src.channel_health.tasks.probe_degraded_channels", minute=None, second={30}),
]
