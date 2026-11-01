"""Ranks over the FULL team depth (SRS 4.4, Vinay 14 Oct 2026). Run every night by scheduler_runner.py.

- Active = activated before tonight's midnight (IST) and status 'active' (blocked is not counted).
- Distributor: 5 active directs. Silver..Diamond: active team (full depth) with the 50:50 rule:
  counted = sum over direct lines of min(line's active IDs, need x 50%).
- The member's rank is the highest one they qualify for, and every rank below it too (Vinay 28 Oct): Silver and
  up need the Distributor 5 active directs as well.
- On the 1st of a month the night's result is also saved as last month's snapshot (royalty uses it).
- Super 3 Level: the first night a member has 10 active directs sets users.s3l_reached_on (the day that ended).
- Directs count at once (Vinay 1 Nov, bhaiya: "turant reflect hona chahiye"): every activation recounts the
  sponsor's active directs (count_directs_now), so Distributor and Super 3 Level (reached today) show at once.
  A rank only goes up there; the night count still decides team ranks, drops and the month-end snapshot.

All members are loaded into memory once (5 lakh rows is fine). A sponsor always has a smaller id than
the people they sponsor, so one pass from the highest id down adds every line into its sponsor.
"""

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from base import IST, RANK_NAMES, cpo, get_settings, logger, today_ist

LOCK_KEY = "job:rank_stats"
RUN_AT = (0, 20)          # IST hour, minute (scheduler_runner.py); the screens say when the next count is


def next_run(now: datetime) -> datetime:
    """The next night count after `now` (IST 12:20 AM)."""
    t = now.astimezone(IST).replace(hour=RUN_AT[0], minute=RUN_AT[1], second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def compute_ranks(users: List[Dict], s: Dict) -> Dict[int, Dict]:
    """users: [{id, sponsor_id, active}] for every member. Returns {id: stats} for every member."""
    ranks, cap_pct = s["ranks"]["need"], s["ranks"]["leg_cap_percent"]
    by_id = {u["id"]: u for u in users}
    sub = {u["id"]: 1 if u["active"] else 0 for u in users}     # active IDs in the line starting at u
    legs: Dict[int, List[tuple]] = {u["id"]: [] for u in users}  # (line size, direct id) per direct
    directs = {u["id"]: 0 for u in users}
    for u in sorted(users, key=lambda x: x["id"], reverse=True):
        p = u["sponsor_id"]
        if p in by_id:
            sub[p] += sub[u["id"]]
            legs[p].append((sub[u["id"]], u["id"]))
            directs[p] += 1 if u["active"] else 0

    out = {}
    for uid in by_id:
        lines = legs[uid]
        team = sum(n for n, _ in lines)

        def counted(rank_no: int) -> int:
            if rank_no == 1:
                return directs[uid]
            need = ranks[RANK_NAMES[rank_no - 1]]
            cap = need * cap_pct // 100
            return sum(min(n, cap) for n, _ in lines)

        rank = 0
        for r in range(1, len(RANK_NAMES) + 1):     # each rank needs the one before it (Vinay 28 Oct)
            if counted(r) < ranks[RANK_NAMES[r - 1]]:
                break
            rank = r
        big = max(lines, default=(0, None))
        out[uid] = {"active_directs": directs[uid], "active_team": team, "biggest_leg": big[0],
                    "biggest_leg_user": big[1], "rank": rank,
                    "next_counted": counted(rank + 1) if rank < len(RANK_NAMES) else 0}
    return out


async def run_rank_stats(run_day: Optional[date] = None, snapshot_only: bool = False) -> Optional[Dict]:
    """Computes everyone's stats as of midnight starting `run_day` (default: today, IST).
    snapshot_only: only save the month-end snapshot (run_day must be a 1st); live stats are not touched."""
    run_day = run_day or today_ist()
    as_of = datetime.combine(run_day, datetime.min.time(), IST)
    s = await get_settings()
    await cpo.connect()
    async with cpo.pool.acquire() as lock_conn:
        if not await lock_conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", LOCK_KEY):
            return None
        try:
            rows = await cpo.fetch_all(
                "SELECT id, sponsor_id, (status = 'active' AND activated_at < $1) AS active FROM users "
                "WHERE role = 'USER' AND status <> 'removed'", as_of)
            stats = compute_ranks(rows, s)
            async with cpo.transaction() as conn:
                await conn.execute("CREATE TEMP TABLE rs (LIKE user_rank_stats) ON COMMIT DROP")
                await conn.copy_records_to_table("rs", records=[
                    (uid, v["active_directs"], v["active_team"], v["biggest_leg"], v["biggest_leg_user"], v["rank"],
                     v["next_counted"], as_of) for uid, v in stats.items()],
                    columns=["user_id", "active_directs", "active_team", "biggest_leg", "biggest_leg_user", "rank",
                             "next_counted", "computed_at"])
                if not snapshot_only:
                    await _save_live(conn, s, run_day)
                    # IDs activated after midnight (before this run) were counted at once; keep that.
                    late = [r["sponsor_id"] for r in await cpo.fetch_all(
                        "SELECT DISTINCT sponsor_id FROM users WHERE activated_at >= $1 AND sponsor_id IS NOT NULL",
                        as_of, conn=conn)]
                    if late:
                        await count_directs_now(conn, late, s)
                if run_day.day == 1:
                    await _snapshot(conn, (run_day - timedelta(days=1)).replace(day=1), as_of)
            ranked = sum(1 for v in stats.values() if v["rank"])
            logger.info("rank stats: %s members, %s with a rank, as of %s", len(stats), ranked, as_of)
            return {"members": len(stats), "ranked": ranked, "as_of": as_of}
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock(hashtext($1))", LOCK_KEY)


async def _save_live(conn, s: Dict, run_day: date):
    await conn.execute(
        "INSERT INTO user_rank_stats SELECT * FROM rs ON CONFLICT (user_id) DO UPDATE SET "
        "active_directs = EXCLUDED.active_directs, active_team = EXCLUDED.active_team, "
        "biggest_leg = EXCLUDED.biggest_leg, biggest_leg_user = EXCLUDED.biggest_leg_user, "
        "rank = EXCLUDED.rank, next_counted = EXCLUDED.next_counted, computed_at = EXCLUDED.computed_at")
    # Super 3 Level: first time with 10 active directs -> reached on the day that just ended.
    await cpo.execute(
        "UPDATE users u SET s3l_reached_on = $2 FROM rs WHERE rs.user_id = u.id "
        "AND rs.active_directs >= $1 AND u.s3l_reached_on IS NULL",
        s["s3_level"]["directs"], run_day - timedelta(days=1), conn=conn)


async def count_directs_now(conn, sponsor_ids: List[int], s: Dict):
    """These sponsors' active directs right now: Distributor (rank 1, only up) and Super 3 Level (reached today).
    Called inside the activation (activations run one at a time). Team numbers stay from the night count; a new
    row takes the last night's time, so the admin's "ranks last counted" stays true."""
    need = s["ranks"]["need"][RANK_NAMES[0]]
    await cpo.execute(
        "INSERT INTO user_rank_stats AS r (user_id, active_directs, rank, next_counted, computed_at) "
        "SELECT p.id, d.n, (d.n >= $2)::int, d.n, "
        "       coalesce((SELECT computed_at FROM user_rank_stats ORDER BY user_id LIMIT 1), $3) "
        "FROM unnest($1::bigint[]) AS p(id) CROSS JOIN LATERAL ("
        "  SELECT count(*)::int AS n FROM users c WHERE c.sponsor_id = p.id AND c.role = 'USER' AND c.status = 'active'"
        ") d "
        "ON CONFLICT (user_id) DO UPDATE SET active_directs = EXCLUDED.active_directs, "
        "rank = GREATEST(r.rank, EXCLUDED.rank), "
        # rank 0: the next rank is Distributor, counted by directs; just became Distributor: Silver is a team
        # number, known after the night count
        "next_counted = CASE WHEN GREATEST(r.rank, EXCLUDED.rank) = 0 THEN EXCLUDED.active_directs "
        "                    WHEN r.rank = 0 THEN 0 ELSE r.next_counted END",
        sponsor_ids, need, datetime.combine(today_ist(), datetime.min.time(), IST), conn=conn)
    await cpo.execute(
        "UPDATE users u SET s3l_reached_on = $3 FROM user_rank_stats r "
        "WHERE r.user_id = u.id AND u.id = ANY($1::bigint[]) AND r.active_directs >= $2 AND u.s3l_reached_on IS NULL",
        sponsor_ids, s["s3_level"]["directs"], today_ist(), conn=conn)


async def _snapshot(conn, month: date, as_of: datetime):
    """Last month's ranks (as of its last midnight). The first snapshot of a month is kept."""
    if not await cpo.fetch_val("INSERT INTO rank_snapshot_months (month, as_of) VALUES ($1, $2) "
                               "ON CONFLICT DO NOTHING RETURNING month", month, as_of, conn=conn):
        return
    await cpo.execute("INSERT INTO rank_snapshots (month, user_id, rank, active_directs, active_team) "
                      "SELECT $1, user_id, rank, active_directs, active_team FROM rs WHERE rank > 0", month, conn=conn)
    logger.info("rank snapshot saved for %s", month)


async def ensure_snapshot(month: date):
    """Used by the payout if the 1st-of-month night run was missed: ranks as of the month's end from
    activation times (blocks are as of now, the best we know). Live stats are not touched."""
    if await cpo.fetch_val("SELECT 1 FROM rank_snapshot_months WHERE month = $1", month):
        return
    first_of_next = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    await run_rank_stats(first_of_next, snapshot_only=True)
