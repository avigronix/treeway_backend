"""Rank and Royalty screen (member) and the monthly payouts (admin)."""

from base import *
from models import responses as R
from monthlyPayouts import pay_month, plan

rankApi = APIRouter()


@rankApi.get("/rank/me", response_model=R.Envelope[R.MyRank])
async def my_rank(user=Depends(tokenRequired())):
    """'Rank and Royalty' screen. Numbers are from last night's count (`as_of`)."""
    s = await get_settings()
    need, cap_pct = s["ranks"]["need"], s["ranks"]["leg_cap_percent"]
    st = await cpo.fetch_one(
        "SELECT r.*, b.full_name AS biggest_leg_name, b.user_code AS biggest_leg_code FROM user_rank_stats r "
        "LEFT JOIN users b ON b.id = r.biggest_leg_user WHERE r.user_id = $1", user["id"])
    st = st or {"active_directs": 0, "active_team": 0, "biggest_leg": 0, "rank": 0, "next_counted": 0,
                "computed_at": None, "biggest_leg_name": None, "biggest_leg_code": None}
    rank = st["rank"]
    ranks = []
    for i, name in enumerate(RANK_NAMES, start=1):
        kind = "active directs" if i == 1 else "active team"
        ranks.append({"rank": i, "name": name, "need": need[name], "counts": kind,
                      "status": "done" if i <= rank else ("next" if i == rank + 1 else "locked")})
    nxt = None
    if rank < len(RANK_NAMES):
        name = RANK_NAMES[rank]
        cap = need[name] * cap_pct // 100
        nxt = {"name": name, "need": need[name], "counted": st["next_counted"],
               "one_line_max": cap if rank >= 1 else None,
               "biggest_line": {"name": st["biggest_leg_name"], "user_code": st["biggest_leg_code"],
                                "active": st["biggest_leg"], "counted": min(st["biggest_leg"], cap)}
               if rank >= 1 and st["biggest_leg_code"] else None}
    last = await cpo.fetch_one("SELECT amount, meta, created_at FROM wallet_ledger WHERE user_id = $1 AND type = 'royalty' "
                               "ORDER BY id DESC LIMIT 1", user["id"])
    me = await cpo.fetch_one("SELECT s3l_reached_on FROM users WHERE id = $1", user["id"])
    s3l = s["s3_level"]
    first_pay = None
    if me["s3l_reached_on"]:
        first_pay = (me["s3l_reached_on"].replace(day=28) + timedelta(days=4)).replace(day=s["ranks"]["pay_day"])
    return respond({"msg": "", "status": 200, "data": {
        "as_of": st["computed_at"], "rank": rank, "rank_name": RANK_NAMES[rank - 1] if rank else None,
        "active_directs": st["active_directs"], "active_team": st["active_team"], "ranks": ranks, "next": nxt,
        "leg_cap_percent": cap_pct, "pay_day": s["ranks"]["pay_day"],
        "last_royalty": {"amount": rupees(last["amount"]), "month": last["meta"].get("month"), "paid_at": last["created_at"]}
        if last else None,
        "s3_level": {"directs_needed": s3l["directs"], "active_directs": st["active_directs"],
                     "reached_on": me["s3l_reached_on"], "first_payout": first_pay, "payouts": s3l["months"]},
    }})


# ================================================================ admin: monthly payouts
def _month(month: str) -> date:
    try:
        y, m = map(int, month.split("-"))
        return date(y, m, 1)
    except ValueError:
        fail(400, "Month must look like 2026-10.")


def _money(d: Dict) -> Dict:
    out = {}
    for k, v in d.items():
        if k == "shares":
            continue
        if k in ("turnover", "pool_each_rank", "pool_total", "to_company", "each", "pool", "shared", "company"):
            out[k] = rupees(v)
        elif k == "detail":
            out[k] = _money(v)
        elif k == "ranks":
            out[k] = [{**r, **{f: rupees(r[f]) for f in ("pool", "each", "to_company")}} for r in v]
        else:
            out[k] = v
    return out


@rankApi.get("/admin/monthly-payouts/{month}/preview", response_model=R.Envelope[R.PayoutPreview])
async def payout_preview(month: str, admin=Depends(tokenRequired(["ADMIN"]))):
    """AdminIncome 'Royalty · October (pays on 3 Nov) Preview' and 'Super 3 Level pool · October'."""
    m = _month(month)
    p = await plan(m)
    runs = {r["kind"]: r for r in await cpo.fetch_all("SELECT kind, status, finished_at, paid_by FROM monthly_runs "
                                                      "WHERE month = $1", m)}
    return respond({"msg": "", "status": 200, "data": {
        "month": f"{m:%Y-%m}", "pay_from": p["pay_from"], "can_pay": today_ist() >= p["pay_from"],
        "royalty": _money(p["royalty"]), "s3_level": _money(p["s3_level"]), "runs": runs,
        "note": None if p["royalty"]["snapshot_as_of"] else "Month-end ranks are taken on the 1st; until then "
                                                            "royalty members show as 0."}})


@rankApi.post("/admin/monthly-payouts/{month}/pay", response_model=R.Envelope[Dict[str, R.MonthlyRun]])
async def payout_pay(month: str, admin=Depends(tokenRequired(["ADMIN"]))):
    m = _month(month)
    out = await pay_month(m, admin["id"])
    if out is None:
        fail(409, "A payout is running right now. Please refresh in a minute.")
    async with cpo.transaction() as conn:
        await audit(conn, admin["id"], "monthly_payout", f"{m:%Y-%m}",
                    {k: {"run": v["id"], "shared": v["shared"], "company": v["company"]} for k, v in out.items()})
    return respond({"msg": "Royalty and Super 3 Level paid.", "status": 200,
                    "data": {k: _money(dict(v)) for k, v in out.items()}})


@rankApi.get("/admin/monthly-payouts", response_model=R.Envelope[R.Page[R.MonthlyRunRow]])
async def payout_runs(cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20,
                      admin=Depends(tokenRequired(["ADMIN"]))):
    data = await cpo.paginate("SELECT r.*, u.full_name AS paid_by_name, r.id AS _k FROM monthly_runs r "
                              "LEFT JOIN users u ON u.id = r.paid_by", page=page, limit=limit, cursor=cursor)
    data["rows"] = [_money(r) for r in data["rows"]]
    return respond({"msg": "", "status": 200, "data": data})
