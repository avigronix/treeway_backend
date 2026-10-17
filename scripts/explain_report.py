"""Phase I1: which API / job is slow on the big database, and why (EXPLAIN ANALYZE of its real queries).

    venv/bin/python scripts/explain_report.py [--slow-ms 20] [--out docs/perf_explain.md]

Calls the real API handlers in-process against treeway_load (after scripts/load_seed.py), records every SQL
statement each one runs, and EXPLAIN (ANALYZE, BUFFERS)es the ones slower than --slow-ms with the same values.
Each endpoint is called twice; the second (warm cache) time is reported, like a busy server.
"""

import argparse
import asyncio
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.update({"APP_ENV": "e2e", "DATABASE_URL": "postgresql://treeway:treeway@localhost:5432/treeway_load",
                   "REDIS_URL": "redis://localhost:6379/6", "SECRET_KEY": "load-test-secret-key-" + "x" * 40,
                   "RATE_LIMIT_MULTIPLIER": "1000", "DB_POOL_MAX": "10"})

import httpx  # noqa: E402

import main  # noqa: E402
from base import cpo, create_token, today_ist  # noqa: E402
from config.pg_operation import PgOperations  # noqa: E402

RECORD: list = []
_orig = {name: getattr(PgOperations, name) for name in ("fetch_one", "fetch_all", "fetch_val", "execute")}


def _wrap(name):
    async def timed(self, sql, *args, conn=None):
        t = time.perf_counter()
        try:
            return await _orig[name](self, sql, *args, conn=conn)
        finally:
            RECORD.append((sql, args, (time.perf_counter() - t) * 1000))
    return timed


for _n in _orig:
    setattr(PgOperations, _n, _wrap(_n))

BIG = ("wallet_ledger", "users", "user_uplines", "company_ledger", "single_leg_pool", "super3_nodes", "orders",
       "withdrawals", "payment_requests", "pins", "complaints", "rank_snapshots", "user_rank_stats")


async def tokens():
    leader = await _orig["fetch_one"](cpo, "SELECT u.* FROM users u JOIN user_rank_stats r ON r.user_id = u.id "
                                           "WHERE u.role = 'USER' ORDER BY r.active_team DESC LIMIT 1")
    typical = await _orig["fetch_one"](cpo, "SELECT u.* FROM users u JOIN user_rank_stats r ON r.user_id = u.id "
                                            "WHERE u.role = 'USER' AND u.status = 'active' AND r.active_directs BETWEEN 2 AND 4 "
                                            "ORDER BY u.id LIMIT 1")
    admin = await _orig["fetch_one"](cpo, "SELECT * FROM users WHERE user_code = 'TW1000000'")
    return {k: (create_token(u), u) for k, u in (("leader", leader), ("member", typical), ("admin", admin))}


def endpoints(t):
    fy = f"{today_ist().year if today_ist().month >= 4 else today_ist().year - 1}-" \
         f"{(today_ist().year + 1 if today_ist().month >= 4 else today_ist().year) % 100:02d}"
    member_urls = ["/dashboard", "/wallet/summary", "/wallet/history", "/wallet/history?group=income",
                   "/wallet/income-summary?period=month", "/wallet/level-income", "/users/team-summary",
                   "/users/team-members", "/users/team-members?status=inactive", "/users/top-sponsors", "/super3/status",
                   "/rank/me", "/videos/today", "/pins?tab=ready", "/shop/orders", "/complaints", "/doctor/requests",
                   "/gullak", "/users/me"]
    code = t["leader"][1]["user_code"]
    admin_urls = ["/admin/dashboard", "/admin/pnl", "/admin/members", "/admin/members?search=Suresh",
                  "/admin/members?status=inactive", f"/admin/members/{code}", f"/admin/members/{code}/history",
                  f"/admin/members/{code}/team", "/admin/payment-requests", "/admin/withdrawals", "/admin/pins",
                  "/admin/orders", "/admin/super3/stats", f"/admin/reports/tds?fy={fy}", "/admin/reports/income",
                  "/admin/reports/joinings?group=month", "/admin/reports/withdrawals", f"/admin/reports/turnover?fy={fy}",
                  "/admin/doctor-requests", "/admin/contact-messages", "/admin/single-leg/runs", "/admin/monthly-payouts",
                  "/admin/audit", "/complaints", "/admin/exports/members.csv"]
    out = [("leader", u) for u in member_urls] + [("member", u) for u in member_urls] + [("admin", u) for u in admin_urls]
    return out


def flags(plan: str) -> list:
    out = []
    for m in re.finditer(r"Seq Scan on (\w+).*?actual time=[\d.]+\.\.([\d.]+) rows=(\d+)", plan):
        table, ms, rows = m.group(1), float(m.group(2)), int(m.group(3))
        if table in BIG and ms > 5:
            out.append(f"Seq Scan on {table} ({ms:.0f} ms, {rows} rows)")
    return out


async def explain(sql, args):
    if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
        return "(not a SELECT; not explained)"
    # Only SELECTs are explained: EXPLAIN ANALYZE runs the query, and a SELECT changes nothing.
    rows = await _orig["fetch_all"](cpo, "EXPLAIN (ANALYZE, BUFFERS) " + sql, *args)
    return "\n".join(r["QUERY PLAN"] for r in rows)


async def run(slow_ms: float, out_path: Path):
    await cpo.connect()
    t = await tokens()
    lines = [f"# EXPLAIN report ({time.strftime('%Y-%m-%d %H:%M')}, treeway_load)", ""]
    counts = await _orig["fetch_one"](cpo, "SELECT (SELECT count(*) FROM users WHERE role='USER') AS members, "
                                           "(SELECT count(*) FROM wallet_ledger) AS ledger, (SELECT count(*) FROM company_ledger) AS company, "
                                           "(SELECT count(*) FROM user_uplines) AS uplines, (SELECT count(*) FROM single_leg_pool) AS pool")
    lines += [f"Data: {counts}", "", f"Leader: {t['leader'][1]['user_code']} · member: {t['member'][1]['user_code']}", "",
              "| Who | API | ms (warm) | queries | slowest query ms | problems |", "|---|---|---|---|---|---|"]
    details = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t", timeout=600) as c:
        for who, url in endpoints(t):
            h = {"Authorization": f"Bearer {t[who][0]}"}
            await c.get("/api/v1" + url, headers=h)                       # warm up
            RECORD.clear()
            s = time.perf_counter()
            r = await c.get("/api/v1" + url, headers=h)
            ms = (time.perf_counter() - s) * 1000
            recs = list(RECORD)
            slow = [x for x in recs if x[2] >= slow_ms]
            probs = []
            for sql, args, qms in sorted(slow, key=lambda x: -x[2])[:3]:
                plan = await explain(sql, args)
                probs += flags(plan)
                details += [f"### {who} {url} — {qms:.0f} ms", "```sql", sql.strip()[:1500], "```", "```", plan[:4000], "```", ""]
            status = "" if r.status_code == 200 else f" **{r.status_code}**"
            lines.append(f"| {who} | `{url}`{status} | {ms:.0f} | {len(recs)} | "
                         f"{max((x[2] for x in recs), default=0):.0f} | {'; '.join(sorted(set(probs))) or ''} |")
            print(f"{who:6} {url:55} {ms:7.0f} ms  {len(recs):3} q  {'; '.join(sorted(set(probs)))}", flush=True)

    from rankStats import run_rank_stats
    from singleLeg import run_single_leg
    from backgroundRunnerFunction import cancel_unpaid_courier_orders, clean_video_logs, remove_inactive_ids
    lines += ["", "## Night jobs", "", "| Job | seconds |", "|---|---|"]
    for name, job in (("rank stats (all members)", lambda: run_rank_stats(today_ist())),
                      ("single leg (no new rows)", lambda: run_single_leg(today_ist())),
                      ("remove inactive IDs", remove_inactive_ids), ("clean video logs", clean_video_logs),
                      ("cancel unpaid courier orders", cancel_unpaid_courier_orders)):
        s = time.perf_counter()
        await job()
        lines.append(f"| {name} | {time.perf_counter() - s:.1f} |")
        print(f"job {name}: {time.perf_counter() - s:.1f}s", flush=True)
    out_path.write_text("\n".join(lines + ["", "## Slow query plans", ""] + details))
    print(f"written {out_path}")
    await cpo.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--slow-ms", type=float, default=20)
    ap.add_argument("--out", default=str(ROOT / "docs" / "perf_explain.md"))
    a = ap.parse_args()
    asyncio.run(run(a.slow_ms, Path(a.out)))
