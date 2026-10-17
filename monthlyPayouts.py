"""Royalty and Super 3 Level, paid on the 3rd (setting ranks.pay_day) for the previous month.

Royalty (SRS 4.4, Vinay 14 Oct 2026)
- Turnover of the month = its activations x ₹987 (company_ledger 'turnover').
- Each of the 6 rank pools = royalty_percent (2%) of turnover, shared equally by members whose rank on the
  month's last day (rank_snapshots) is that rank. A member gets only their own top rank's pool.
- Empty pool and rounding -> company (royalty_company). Blocked on payout day -> their share to the company.

Super 3 Level (SRS 4.6, Vinay 14 Oct 2026)
- Pool = the month's ₹100s from board 3+ incomes (company_ledger 's3_level_pool').
- Shared equally by members who reached 10 active directs in that month or the 5 before it
  (6 payouts, starting on the pay day after the month they reached 10). Fixed once reached.
- No one eligible and rounding -> company (s3_level_company). Blocked on payout day -> company.

Both are income (10% cut, 5% to the Single Leg pool). Admin presses Pay on/after the pay day; a backup run
pays at 18:00 IST on any day from the pay day if the admin has not. Same safety as Single Leg: one run per
month and kind, chunks, unique refs (roy:/s3l:{run}:{user}), resume after a crash, a job lock.
"""

from datetime import date, datetime, timedelta
from typing import Dict, Optional

from base import IST, RANK_NAMES, cpo, fail, get_settings, logger, percent_of, today_ist
from mlmEngine import bulk_credit_income, company_add
from rankStats import ensure_snapshot

KINDS = ("royalty", "s3_level")
CHUNK = 2000
LOCK_KEY = "job:monthly_payouts"


def next_month(m: date) -> date:
    return (m.replace(day=28) + timedelta(days=4)).replace(day=1)


def add_months(m: date, n: int) -> date:
    y, mo = divmod(m.month - 1 + n, 12)
    return date(m.year + y, mo + 1, 1)


def month_bounds(m: date) -> tuple:
    return (datetime.combine(m, datetime.min.time(), IST), datetime.combine(next_month(m), datetime.min.time(), IST))


async def pay_date(m: date) -> date:
    return next_month(m).replace(day=(await get_settings())["ranks"]["pay_day"])


# ================================================================ plan (preview and payout use the same numbers)
async def plan(m: date, conn=None) -> Dict:
    """Who gets how much for month `m`. Reads only; the payout saves exactly this."""
    s = await get_settings()
    start, end = month_bounds(m)
    turnover = await cpo.fetch_val("SELECT coalesce(sum(amount), 0)::bigint FROM company_ledger WHERE type = 'turnover' "
                                   "AND created_at >= $1 AND created_at < $2", start, end, conn=conn)
    snap = await cpo.fetch_one("SELECT as_of FROM rank_snapshot_months WHERE month = $1", m, conn=conn)
    pool_each = percent_of(turnover, s["ranks"]["royalty_percent"])
    members = await cpo.fetch_all("SELECT user_id, rank FROM rank_snapshots WHERE month = $1", m, conn=conn) if snap else []
    ranks, royalty_shares, royalty_company = [], [], 0
    for r, name in enumerate(RANK_NAMES, start=1):
        ids = [x["user_id"] for x in members if x["rank"] == r]
        each = pool_each // len(ids) if ids else 0
        left = pool_each - each * len(ids)
        royalty_company += left
        ranks.append({"rank": r, "name": name, "pool": pool_each, "members": len(ids), "each": each, "to_company": left})
        royalty_shares += [(uid, each, r) for uid in ids if each > 0]

    s3l_pool = await cpo.fetch_val(
        "SELECT coalesce(sum(amount), 0)::bigint FROM company_ledger WHERE type = 's3_level_pool' AND amount > 0 "
        "AND created_at >= $1 AND created_at < $2", start, end, conn=conn)
    months = s["s3_level"]["months"]
    eligible = [r["id"] for r in await cpo.fetch_all(
        "SELECT id FROM users WHERE s3l_reached_on >= $1 AND s3l_reached_on < $2 ORDER BY id",
        add_months(m, -(months - 1)), next_month(m), conn=conn)]
    s3l_each = s3l_pool // len(eligible) if eligible else 0
    return {
        "month": m, "pay_from": await pay_date(m),
        "royalty": {"turnover": turnover, "pool_each_rank": pool_each, "pool_total": pool_each * len(RANK_NAMES),
                    "ranks": ranks, "to_company": royalty_company, "snapshot_as_of": snap["as_of"] if snap else None,
                    "shares": royalty_shares},
        "s3_level": {"pool_total": s3l_pool, "members": len(eligible), "each": s3l_each,
                     "to_company": s3l_pool - s3l_each * len(eligible),
                     "shares": [(uid, s3l_each, None) for uid in eligible if s3l_each > 0]},
    }


# ================================================================ payout
async def pay_month(m: date, admin_id: Optional[int] = None, chunk: int = CHUNK) -> Optional[Dict]:
    """Pays royalty and S3 Level for month `m` (both, each once). None = another payout is running."""
    if today_ist() < await pay_date(m):
        fail(400, f"This month can be paid from {await pay_date(m):%d %b %Y}.")
    await cpo.connect()
    async with cpo.pool.acquire() as lock_conn:
        if not await lock_conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", LOCK_KEY):
            return None
        try:
            await ensure_snapshot(m)
            if not await cpo.fetch_val("SELECT 1 FROM rank_snapshot_months WHERE month = $1", m):
                fail(409, "Month-end ranks are being counted right now. Please try again in a minute.")
            out = {}
            for kind in KINDS:
                run = await cpo.fetch_one("SELECT * FROM monthly_runs WHERE month = $1 AND kind = $2", m, kind)
                if not run:
                    run = await _create(m, kind, admin_id)
                if run["status"] != "done":
                    await _pay(run, chunk)
                    await cpo.execute("UPDATE monthly_runs SET status = 'done', finished_at = now() WHERE id = $1",
                                      run["id"])
                out[kind] = await cpo.fetch_one("SELECT * FROM monthly_runs WHERE id = $1", run["id"])
            return out
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock(hashtext($1))", LOCK_KEY)


async def _create(m: date, kind: str, admin_id: Optional[int]) -> Dict:
    """Saves the plan (shares + company part) in one transaction."""
    async with cpo.transaction() as conn:
        p = (await plan(m, conn))[kind]
        shares = p["shares"]
        detail = {k: v for k, v in p.items() if k != "shares"}
        if detail.get("snapshot_as_of"):
            detail["snapshot_as_of"] = detail["snapshot_as_of"].isoformat()
        shared = sum(g for _, g, _ in shares)
        run_id = await cpo.fetch_val(
            "INSERT INTO monthly_runs (month, kind, pool_total, shared, company, receivers, detail, paid_by) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
            m, kind, p["pool_total"], shared, p["pool_total"] - shared, len(shares), detail, admin_id, conn=conn)
        if shares:
            cols = list(zip(*shares))
            await cpo.execute("INSERT INTO monthly_shares (run_id, user_id, gross, rank) "
                              "SELECT $1, * FROM unnest($2::bigint[], $3::bigint[], $4::smallint[])",
                              run_id, list(cols[0]), list(cols[1]), list(cols[2]), conn=conn)
        meta = {"run": run_id, "month": f"{m:%Y-%m}"}
        if kind == "royalty":
            await company_add(conn, "royalty_company", p["pool_total"] - shared, f"royc:{run_id}", None, meta)
        else:
            # The month's S3 Level pool is used up: members' part is paid below, the rest is company income.
            await company_add(conn, "s3_level_pool", -p["pool_total"], f"s3lp:{run_id}", None, meta)
            await company_add(conn, "s3_level_company", p["pool_total"] - shared, f"s3lc:{run_id}", None, meta)
    logger.info("%s %s: run %s, %s receivers", kind, m, run_id, len(shares))
    return await cpo.fetch_one("SELECT * FROM monthly_runs WHERE id = $1", run_id)


async def _pay(run: Dict, chunk: int):
    s = await get_settings()
    prefix = "roy" if run["kind"] == "royalty" else "s3l"
    while True:
        async with cpo.transaction() as conn:
            await cpo.advisory_lock(conn, "super3")          # one wallet writer at a time with activations
            rows = await cpo.fetch_all(
                "SELECT user_id, gross, rank FROM monthly_shares WHERE run_id = $1 AND paid_at IS NULL "
                "ORDER BY user_id LIMIT $2 FOR UPDATE", run["id"], chunk, conn=conn)
            if not rows:
                return
            items = [(r["user_id"], r["gross"], f"{prefix}:{run['id']}:{r['user_id']}",
                      {"run": run["id"], "month": f"{run['month']:%Y-%m}",
                       **({"rank": RANK_NAMES[r["rank"] - 1]} if r["rank"] else {})}) for r in rows]
            result = await bulk_credit_income(conn, run["kind"], items, s)
            await cpo.execute("UPDATE monthly_shares SET paid_at = now(), lapsed = user_id = ANY($3::bigint[]) "
                              "WHERE run_id = $1 AND user_id = ANY($2::bigint[])",
                              run["id"], [r["user_id"] for r in rows], result.get("lapsed_ids", []), conn=conn)


async def pay_due_months() -> Optional[Dict]:
    """Backup (18:00 IST daily): pays last month if its pay day has come and the admin has not paid it."""
    m = (today_ist().replace(day=1) - timedelta(days=1)).replace(day=1)
    if today_ist() < await pay_date(m):
        return None
    done = await cpo.fetch_val("SELECT count(*) FROM monthly_runs WHERE month = $1 AND status = 'done'", m)
    if done == len(KINDS):
        return None
    logger.warning("monthly payout for %s was not paid by an admin: paying automatically", m)
    return await pay_month(m)
