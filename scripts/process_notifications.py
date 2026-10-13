"""Send queued e-mails and queue meeting reminders.

Run on a schedule (e.g. a Render Cron Job every 5 minutes):
    python -m scripts.process_notifications

It is safe to run concurrently with the web app: rows are claimed with
SKIP LOCKED, so no notification is processed by two workers at once.
Exit code 0 = ran (even if some e-mails failed and were scheduled for retry).
"""
from __future__ import annotations

import logging
import sys

from config import ConfigError, load_config
from core.database import create_service_client
from core.notifications import NotificationWorker, SmtpMailer

log = logging.getLogger("wealthy.notifications")


def run(max_batches: int = 20, batch_size: int = 50) -> dict:
    cfg = load_config()
    if not cfg.supabase_service_role_key:
        raise ConfigError("SUPABASE_SERVICE_ROLE_KEY is required for the notification worker")
    if cfg.notifications_enabled and not cfg.smtp.configured:
        raise ConfigError("SMTP_HOST and SMTP_FROM are required when NOTIFICATIONS_ENABLED=true")
    worker = NotificationWorker(create_service_client(cfg.supabase_url, cfg.supabase_service_role_key),
                                SmtpMailer(cfg.smtp, cfg.app_name), enabled=cfg.notifications_enabled)
    totals = {"reminders_queued": worker.queue_reminders(), "sent": 0, "failed": 0, "skipped": 0}
    for _ in range(max_batches):
        result = worker.process(limit=batch_size)
        for key in ("sent", "failed", "skipped"):
            totals[key] += result[key]
        if sum(result.values()) < batch_size:
            break
    return totals


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        totals = run()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    log.info("notifications: %s", totals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
