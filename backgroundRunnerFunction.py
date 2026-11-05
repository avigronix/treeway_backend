"""Jobs run by scheduler_runner.py (a separate pm2 process, never inside the API workers)."""

from base import *

VIDEO_LOG_KEEP_DAYS = 60


# Inactive IDs are never removed any more (Vinay, 4 Nov): the 30-day "remove inactive IDs" job is gone. An ID
# removed before that stays removed.


async def clean_video_logs():
    """Daily watch logs older than 60 days are removed (they are not money: the ₹20 credits stay in the ledger)."""
    n = await cpo.execute("DELETE FROM video_days WHERE watch_date < $1",
                          today_ist() - timedelta(days=VIDEO_LOG_KEEP_DAYS))
    logger.info("video logs cleaned: %s", n)


async def cancel_unpaid_courier_orders():
    """Orders whose delivery payment was rejected and not fixed in 7 days: cancelled, product money back."""
    from routes.shopApis import COURIER_FIX_DAYS, cancel_order

    ids = [r["id"] for r in await cpo.fetch_all(
        "SELECT id FROM orders WHERE status = 'placed' AND courier_status = 'rejected' "
        "AND courier_rejected_at < now() - make_interval(days => $1)", COURIER_FIX_DAYS)]
    for oid in ids:
        async with cpo.transaction() as conn:
            o = await cpo.fetch_one("SELECT * FROM orders WHERE id = $1 FOR UPDATE", oid, conn=conn)
            if o["status"] == "placed" and o["courier_status"] == "rejected":   # still true after the lock
                await cancel_order(conn, o, None, f"Delivery payment not fixed in {COURIER_FIX_DAYS} days")
    if ids:
        logger.info("cancelled %s orders with an unpaid delivery charge", len(ids))
    return len(ids)
