"""Single Leg nightly distribution (SRS 4.3, Vinay 14 Oct 2026). Run by scheduler_runner.py at 00:00 IST.

Rules
- The line is every activated ID in activation order (users.single_leg_no). Position = rank in that order,
  so a gap in the number sequence never changes who is "10 above".
- Each pool row (5% of one income): half shared equally by the 10 people above the earner, half by the
  10 below. Per person = floor(floor(amount / 2) / 10). Missing people and rounding -> company.
- A blocked member keeps their place in the line; their share goes to the company.
- These credits are income: 10% cut, and their own 5% goes to the NEXT night's pool.

Safety
- One run per night (single_leg_runs.run_date UNIQUE). Steps: claim -> compute -> pay (chunks) -> done.
  Each step commits on its own; a crash is resumed by running the job again.
- Every payment has the ref slr:{run}:{user}, so nothing is ever paid twice.
- A job lock (session advisory lock) stops two copies running at once.
"""

from datetime import date
from typing import Dict, Optional

from base import cpo, get_settings, logger, today_ist
from mlmEngine import bulk_credit_income, company_add

SIDE = 10           # people above and below
CHUNK = 2000        # receivers paid per transaction
LOCK_KEY = "job:single_leg"


async def run_single_leg(run_date: Optional[date] = None, chunk: int = CHUNK) -> Optional[Dict]:
    """Returns the run, or None if another copy of the job is running."""
    await cpo.connect()
    async with cpo.pool.acquire() as lock_conn:
        if not await lock_conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", LOCK_KEY):
            logger.warning("single leg: another run is in progress")
            return None
        try:
            run = await _resume_or_claim(run_date or today_ist())
            if run and run["status"] == "claimed":
                await _compute(run["id"])
            if run and run["status"] != "done":
                await _pay(run["id"], chunk)
                await _finish(run["id"])
            return await cpo.fetch_one("SELECT * FROM single_leg_runs WHERE id = $1", run["id"]) if run else None
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock(hashtext($1))", LOCK_KEY)


async def _resume_or_claim(run_date: date) -> Optional[Dict]:
    """An unfinished run is always finished first. Otherwise tonight's run takes every waiting pool row."""
    unfinished = await cpo.fetch_one("SELECT * FROM single_leg_runs WHERE status <> 'done' ORDER BY id LIMIT 1")
    if unfinished:
        logger.info("single leg: resuming run %s (%s)", unfinished["id"], unfinished["status"])
        return unfinished
    existing = await cpo.fetch_one("SELECT * FROM single_leg_runs WHERE run_date = $1", run_date)
    if existing:
        return existing                    # already done tonight: running again changes nothing
    async with cpo.transaction() as conn:
        run_id = await cpo.fetch_val("INSERT INTO single_leg_runs (run_date) VALUES ($1) RETURNING id", run_date, conn=conn)
        # Rows from incomes after this moment (including tonight's own Single Leg credits) wait for tomorrow.
        # A reverse entry cannot cancel a row once it is claimed (see routes/reverseApis.py).
        totals = await cpo.fetch_one(
            "WITH c AS (UPDATE single_leg_pool SET run_id = $1 WHERE run_id IS NULL AND distributed_at IS NULL "
            "AND cancelled_at IS NULL RETURNING amount) SELECT count(*) AS n, coalesce(sum(amount), 0) AS total FROM c",
            run_id, conn=conn)
        await cpo.execute("UPDATE single_leg_runs SET pool_rows = $2, pool_total = $3 WHERE id = $1",
                          run_id, totals["n"], totals["total"], conn=conn)
    logger.info("single leg: run %s claimed %s rows, %s paise", run_id, totals["n"], totals["total"])
    return await cpo.fetch_one("SELECT * FROM single_leg_runs WHERE id = $1", run_id)


async def _compute(run_id: int):
    """Each receiver's total for the run, and the company's part (missing people + rounding). One transaction."""
    async with cpo.transaction() as conn:
        await conn.execute(
            "CREATE TEMP TABLE sl_line ON COMMIT DROP AS SELECT id, row_number() OVER (ORDER BY single_leg_no) AS pos "
            "FROM users WHERE single_leg_no IS NOT NULL")
        await conn.execute("CREATE INDEX ON sl_line (pos)")
        await conn.execute("CREATE INDEX ON sl_line (id)")
        await conn.execute("ANALYZE sl_line")
        await cpo.execute(
            """
            INSERT INTO single_leg_shares (run_id, user_id, gross, earners)
            SELECT $1, x.id, sum(p.per), count(DISTINCT p.earner_id)
            FROM (SELECT sp.earner_id, l.pos, (sp.amount / 2) / $2 AS per
                  FROM single_leg_pool sp JOIN sl_line l ON l.id = sp.earner_id
                  WHERE sp.run_id = $1) p
            JOIN sl_line x ON x.pos BETWEEN p.pos - $2 AND p.pos + $2 AND x.pos <> p.pos
            WHERE p.per > 0
            GROUP BY x.id
            """, run_id, SIDE, conn=conn)
        shares = await cpo.fetch_one(
            "SELECT count(*) AS n, coalesce(sum(gross), 0) AS total FROM single_leg_shares WHERE run_id = $1",
            run_id, conn=conn)
        run = await cpo.fetch_one("SELECT pool_total FROM single_leg_runs WHERE id = $1 FOR UPDATE", run_id, conn=conn)
        company = run["pool_total"] - shares["total"]
        await company_add(conn, "single_leg_company", company, f"slc:{run_id}", None, {"run": run_id})
        await cpo.execute("UPDATE single_leg_runs SET status = 'computed', shared = $2, company = $3, receivers = $4 "
                          "WHERE id = $1", run_id, shares["total"], company, shares["n"], conn=conn)


async def _pay(run_id: int, chunk: int):
    s = await get_settings()
    run_date = await cpo.fetch_val("SELECT run_date FROM single_leg_runs WHERE id = $1", run_id)
    while True:
        async with cpo.transaction() as conn:
            # Same lock as activations: wallets are touched one job-chunk or one activation at a time.
            await cpo.advisory_lock(conn, "super3")
            rows = await cpo.fetch_all(
                "SELECT user_id, gross, earners FROM single_leg_shares WHERE run_id = $1 AND paid_at IS NULL "
                "ORDER BY user_id LIMIT $2 FOR UPDATE", run_id, chunk, conn=conn)
            if not rows:
                return
            items = [(r["user_id"], r["gross"], f"slr:{run_id}:{r['user_id']}",
                      {"run": run_id, "date": str(run_date), "earners": r["earners"]}) for r in rows]
            result = await bulk_credit_income(conn, "single_leg", items, s)
            await cpo.execute("UPDATE single_leg_shares SET paid_at = now(), lapsed = user_id = ANY($3::bigint[]) "
                              "WHERE run_id = $1 AND user_id = ANY($2::bigint[])",
                              run_id, [r["user_id"] for r in rows], result.get("lapsed_ids", []), conn=conn)


async def _finish(run_id: int):
    async with cpo.transaction() as conn:
        await cpo.execute("UPDATE single_leg_pool SET distributed_at = now() WHERE run_id = $1", run_id, conn=conn)
        await cpo.execute("UPDATE single_leg_runs SET status = 'done', finished_at = now() WHERE id = $1", run_id, conn=conn)
    logger.info("single leg: run %s done", run_id)
