"""Background jobs. Run as ONE separate process: python scheduler_runner.py

Night jobs read lakhs of rows, so their database statements may run up to 30 minutes (the API keeps 30 s).
The scheduler connects to Postgres directly, not through PgBouncer: the jobs use session advisory locks.
If a job fails or is missed, an email goes to ALERT_EMAIL (needs SMTP).
"""

import asyncio
import logging
import os

os.environ.setdefault("DB_COMMAND_TIMEOUT", "1800")   # before base.py creates the pool

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED  # noqa: E402
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402

from backgroundRunnerFunction import cancel_unpaid_courier_orders, clean_video_logs, remove_inactive_ids  # noqa: E402
from base import IST, cpo, redis_client  # noqa: E402
from monthlyPayouts import pay_due_months  # noqa: E402
from rankStats import run_rank_stats  # noqa: E402
from seed import initialize_database  # noqa: E402
from singleLeg import run_single_leg  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("scheduler")


async def alert(subject: str, body: str):
    """Email the team (ALERT_EMAIL, comma separated) when a night job fails. Never raises."""
    logger.error("%s: %s", subject, body)
    to = [x.strip() for x in os.environ.get("ALERT_EMAIL", "").split(",") if x.strip()]
    if not to or not os.environ.get("SMTP_HOST"):
        return
    from common.mailSender import send_mail
    for addr in to:
        try:
            await send_mail(addr, f"[Treeway] {subject}", f"<pre>{body}</pre>")
        except Exception:  # noqa: BLE001
            logger.exception("alert mail failed")


def on_job_event(event):
    if event.code == EVENT_JOB_MISSED:
        subject, body = f"job missed: {event.job_id}", f"Scheduled for {event.scheduled_run_time}; it did not run."
    else:
        subject, body = f"job failed: {event.job_id}", f"{event.exception!r}\n\n{event.traceback}"
    asyncio.get_event_loop().create_task(alert(subject, body))


async def main():
    await initialize_database()
    scheduler = AsyncIOScheduler(timezone=IST)
    scheduler.add_job(remove_inactive_ids, CronTrigger(hour=2, minute=30, timezone=IST), id="remove_inactive_ids",
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(clean_video_logs, CronTrigger(hour=3, minute=15, timezone=IST), id="clean_video_logs",
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(cancel_unpaid_courier_orders, CronTrigger(hour=3, minute=45, timezone=IST), id="courier_orders",
                      replace_existing=True, max_instances=1, coalesce=True)
    # Single Leg at 12 midnight IST. If the server was down at 12, it still runs up to 6 hours late.
    scheduler.add_job(run_single_leg, CronTrigger(hour=0, minute=0, timezone=IST), id="single_leg",
                      replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=6 * 3600)
    # Ranks after Single Leg; on the 1st it also saves last month's ranks for royalty.
    scheduler.add_job(run_rank_stats, CronTrigger(hour=0, minute=20, timezone=IST), id="rank_stats",
                      replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=6 * 3600)
    # Royalty + Super 3 Level: the admin pays from the pay day (3rd); this backup pays at 18:00 if not.
    scheduler.add_job(pay_due_months, CronTrigger(hour=18, minute=0, timezone=IST), id="monthly_payouts",
                      replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=4 * 3600)
    scheduler.add_listener(on_job_event, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    scheduler.start()
    logger.info("scheduler started")
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown()
        await cpo.close()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
