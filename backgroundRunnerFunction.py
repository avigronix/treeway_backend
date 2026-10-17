"""Jobs run by scheduler_runner.py (a separate pm2 process, never inside the API workers)."""

from base import *

VIDEO_LOG_KEEP_DAYS = 60


async def remove_inactive_ids():
    """Inactive IDs older than N days (admin setting) are removed, so their mobile is free again.
    An ID with an activation request still waiting is never removed (the person already paid)."""
    days = int((await get_settings())["inactive_remove_days"])
    async with cpo.transaction() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_xact_lock(hashtext('job:remove_inactive'))"):
            return
        rows = await cpo.fetch_all(
            """
            UPDATE users u SET status = 'removed', token_version = token_version + 1
            WHERE u.status = 'inactive' AND u.role = 'USER'
              AND u.created_at < now() - make_interval(days => $1)
              AND NOT EXISTS (SELECT 1 FROM payment_requests p WHERE p.user_id = u.id AND p.status = 'pending')
              AND NOT EXISTS (SELECT 1 FROM user_uplines d WHERE d.ancestor_id = u.id)
            RETURNING u.id
            """, days, conn=conn)
    for r in rows:
        await forget_auth_user(r["id"])
    if rows:
        logger.info("removed %s inactive IDs", len(rows))


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
