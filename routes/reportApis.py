"""Admin reports (AdminIncome) and CSV exports (Excel buttons). Every export is in the audit log.

TDS (Vinay, 15 Oct 2026): by PAN (all IDs of one PAN together), for a financial year (Apr-Mar) and an
optional quarter (26Q is filed quarterly). TDS comes from the company's TDS entries (a reverse entry lowers
it); gross and net from the income entries (a reversed income is taken out). Members with TDS but no PAN are
listed apart.
"""

from fastapi.responses import StreamingResponse

from base import *
from common.csvExport import CSV_RESPONSE, stream_csv
from models import responses as R

reportApi = APIRouter()

# Income that has TDS: the MLM incomes and Gullak interest (2% TDS only).
TAXED_TYPES = list(INCOME_TYPES) + ["gullak_interest"]

QUARTERS = {"Q1": (4, 7), "Q2": (7, 10), "Q3": (10, 13), "Q4": (13, 16)}   # months from April of the FY's first year
FY_RE = re.compile(r"^(\d{4})-(\d{2})$")


def fy_range(fy: str, quarter: Optional[str]) -> tuple:
    m = FY_RE.match(fy or "")
    if not m or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
        fail(400, "Financial year must look like 2026-27.")
    y = int(m.group(1))
    a, b = QUARTERS[quarter] if quarter else (4, 16)

    def at(month_no: int) -> datetime:
        yy, mm = y + (month_no - 1) // 12, (month_no - 1) % 12 + 1
        return datetime(yy, mm, 1, tzinfo=IST)
    return at(a), at(b)


def day_range(start: Optional[date], end: Optional[date]) -> tuple:
    """Inclusive IST days. Default: this month."""
    start = start or today_ist().replace(day=1)
    end = end or today_ist()
    if end < start or (end - start).days > 400:
        fail(400, "Choose a date range of up to 400 days.")
    return (datetime.combine(start, datetime.min.time(), IST),
            datetime.combine(end + timedelta(days=1), datetime.min.time(), IST))


async def _export_audit(admin: Dict, name: str, filters: Dict):
    async with cpo.transaction() as conn:
        await audit(conn, admin["id"], "export", name, {k: str(v) for k, v in filters.items() if v is not None})


# ================================================================ TDS
TDS_PER_MEMBER = """
WITH inc AS (
    SELECT user_id, sum(gross) AS gross, sum(amount) AS net FROM wallet_ledger
    WHERE type = ANY($3::text[]) AND created_at >= $1 AND created_at < $2 GROUP BY 1
), rev AS (
    SELECT r.user_id, -sum(o.gross) AS gross, sum(r.amount) AS net FROM wallet_ledger r
    JOIN wallet_ledger o ON o.id = (r.meta->>'reverses')::bigint
    WHERE r.type = 'reversal' AND o.type = ANY($3::text[]) AND r.created_at >= $1 AND r.created_at < $2 GROUP BY 1
), tds AS (
    SELECT user_id, sum(amount) AS tds FROM company_ledger
    WHERE type = 'tds' AND user_id IS NOT NULL AND created_at >= $1 AND created_at < $2 GROUP BY 1
)
SELECT u.id, u.user_code, u.full_name, u.mobile, u.pan,
       coalesce(i.gross, 0) + coalesce(v.gross, 0) AS gross, coalesce(t.tds, 0) AS tds,
       coalesce(i.net, 0) + coalesce(v.net, 0) AS net
FROM users u LEFT JOIN inc i ON i.user_id = u.id LEFT JOIN rev v ON v.user_id = u.id LEFT JOIN tds t ON t.user_id = u.id
WHERE i.user_id IS NOT NULL OR v.user_id IS NOT NULL OR t.user_id IS NOT NULL
"""
TDS_BY_PAN = f"""
SELECT pan, (array_agg(full_name ORDER BY id))[1] AS name, string_agg(user_code, ', ' ORDER BY user_code) AS ids,
       count(*) AS id_count, sum(gross)::bigint AS gross, sum(tds)::bigint AS tds, sum(net)::bigint AS net,
       min(id) AS _k
FROM ({TDS_PER_MEMBER}) m WHERE pan IS NOT NULL GROUP BY pan
"""
TDS_NO_PAN = f"SELECT user_code, full_name, mobile, gross, tds, net, id AS _k FROM ({TDS_PER_MEMBER}) m " \
             f"WHERE pan IS NULL AND tds <> 0"


def _money(r: Dict, *keys) -> Dict:
    return {**r, **{k: rupees(r[k]) for k in keys}}


@reportApi.get("/admin/reports/tds", response_model=R.Envelope[R.TdsReport])
async def tds_report(fy: str, quarter: Optional[str] = Query(None, pattern="^Q[1-4]$"),
                     cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 50,
                     admin=Depends(tokenRequired(["ADMIN"]))):
    start, end = fy_range(fy, quarter)
    args = (start, end, TAXED_TYPES)
    data = await cpo.paginate(TDS_BY_PAN, *args, desc=False, page=page, limit=limit, cursor=cursor)
    data["rows"] = [_money(r, "gross", "tds", "net") for r in data["rows"]]
    totals = await cpo.fetch_one(
        f"SELECT coalesce(sum(gross), 0)::bigint AS gross, coalesce(sum(tds), 0)::bigint AS tds, "
        f"coalesce(sum(net), 0)::bigint AS net, count(DISTINCT pan) AS pans, "
        f"coalesce(sum(tds) FILTER (WHERE pan IS NULL), 0)::bigint AS tds_without_pan, "
        f"count(*) FILTER (WHERE pan IS NULL AND tds <> 0) AS members_without_pan FROM ({TDS_PER_MEMBER}) m", *args)
    data["totals"] = _money(totals, "gross", "tds", "net", "tds_without_pan")
    data["period"] = {"fy": fy, "quarter": quarter, "from": start.date(), "to": (end - timedelta(days=1)).date()}
    return respond({"msg": "", "status": 200, "data": data})


@reportApi.get("/admin/reports/tds-no-pan", response_model=R.Envelope[R.Page[R.TdsNoPan]])
async def tds_no_pan(fy: str, quarter: Optional[str] = Query(None, pattern="^Q[1-4]$"),
                     cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 50,
                     admin=Depends(tokenRequired(["ADMIN"]))):
    start, end = fy_range(fy, quarter)
    data = await cpo.paginate(TDS_NO_PAN, start, end, TAXED_TYPES, desc=False, page=page, limit=limit, cursor=cursor)
    data["rows"] = [_money(r, "gross", "tds", "net") for r in data["rows"]]
    return respond({"msg": "", "status": 200, "data": data})


# ================================================================ income, joinings, withdrawals, turnover
@reportApi.get("/admin/reports/income", response_model=R.Envelope[R.IncomeReport])
async def income_report(start: Optional[date] = Query(None, alias="from"), end: Optional[date] = Query(None, alias="to"),
                        admin=Depends(tokenRequired(["ADMIN"]))):
    a, b = day_range(start, end)
    rows = await cpo.fetch_all(
        "SELECT type, count(*) AS entries, count(DISTINCT user_id) AS members, sum(gross)::bigint AS gross, "
        "sum(tds)::bigint AS tds, sum(service)::bigint AS service, sum(single_leg)::bigint AS single_leg, "
        "sum(other_cut)::bigint AS other_cut, sum(amount)::bigint AS net FROM wallet_ledger "
        "WHERE type = ANY($3::text[]) AND created_at >= $1 AND created_at < $2 GROUP BY type ORDER BY type",
        a, b, TAXED_TYPES)
    lapses = await cpo.fetch_all(
        "SELECT substr(type, 7) AS type, sum(amount)::bigint AS amount FROM company_ledger "
        "WHERE type LIKE 'lapse\\_%' AND created_at >= $1 AND created_at < $2 GROUP BY 1 ORDER BY 1", a, b)
    return respond({"msg": "", "status": 200, "data": {
        "from": a.date(), "to": (b - timedelta(days=1)).date(),
        "by_type": [_money(r, "gross", "tds", "service", "single_leg", "other_cut", "net") for r in rows],
        "lapsed_to_company": [_money(r, "amount") for r in lapses]}})


@reportApi.get("/admin/reports/joinings", response_model=R.Envelope[R.JoiningsReport])
async def joinings_report(start: Optional[date] = Query(None, alias="from"), end: Optional[date] = Query(None, alias="to"),
                          group: str = Query("day", pattern="^(day|month)$"), admin=Depends(tokenRequired(["ADMIN"]))):
    a, b = day_range(start, end)
    unit = "day" if group == "day" else "month"
    rows = await cpo.fetch_all(
        f"""
        WITH r AS (SELECT date_trunc('{unit}', created_at AT TIME ZONE 'Asia/Kolkata')::date AS d, count(*) AS n
                   FROM users WHERE role = 'USER' AND created_at >= $1 AND created_at < $2 GROUP BY 1),
             a AS (SELECT date_trunc('{unit}', created_at AT TIME ZONE 'Asia/Kolkata')::date AS d, count(*) AS n,
                          sum(amount)::bigint AS turnover
                   FROM company_ledger WHERE type = 'turnover' AND created_at >= $1 AND created_at < $2 GROUP BY 1)
        SELECT coalesce(r.d, a.d) AS period, coalesce(r.n, 0) AS registered, coalesce(a.n, 0) AS activated,
               coalesce(a.turnover, 0) AS turnover FROM r FULL JOIN a ON a.d = r.d ORDER BY 1
        """, a, b)
    return respond({"msg": "", "status": 200, "data": {
        "from": a.date(), "to": (b - timedelta(days=1)).date(), "group": group,
        "rows": [_money(r, "turnover") for r in rows],
        "totals": {"registered": sum(r["registered"] for r in rows), "activated": sum(r["activated"] for r in rows),
                   "turnover": rupees(sum(r["turnover"] for r in rows))}}})


@reportApi.get("/admin/reports/withdrawals", response_model=R.Envelope[R.WithdrawalsReport])
async def withdrawals_report(start: Optional[date] = Query(None, alias="from"), end: Optional[date] = Query(None, alias="to"),
                             admin=Depends(tokenRequired(["ADMIN"]))):
    a, b = day_range(start, end)
    rows = await cpo.fetch_all(
        """
        WITH e AS (
            SELECT (created_at AT TIME ZONE 'Asia/Kolkata')::date AS day, 1 AS requested, 0 AS paid, 0::bigint AS paid_amount,
                   0 AS rejected FROM withdrawals WHERE created_at >= $1 AND created_at < $2
            UNION ALL
            SELECT (reviewed_at AT TIME ZONE 'Asia/Kolkata')::date, 0, (status = 'paid')::int,
                   CASE WHEN status = 'paid' THEN amount ELSE 0 END, (status = 'rejected')::int
            FROM withdrawals WHERE status IN ('paid', 'rejected') AND reviewed_at >= $1 AND reviewed_at < $2)
        SELECT day, sum(requested)::int AS requested, sum(paid)::int AS paid, sum(paid_amount)::bigint AS paid_amount,
               sum(rejected)::int AS rejected FROM e GROUP BY day ORDER BY day
        """, a, b)
    pending = await cpo.fetch_one("SELECT count(*) AS n, coalesce(sum(amount), 0)::bigint AS amount, min(created_at) AS oldest "
                                  "FROM withdrawals WHERE status = 'pending'")
    return respond({"msg": "", "status": 200, "data": {
        "from": a.date(), "to": (b - timedelta(days=1)).date(), "rows": [_money(r, "paid_amount") for r in rows],
        "pending": _money(pending, "amount")}})


@reportApi.get("/admin/reports/turnover", response_model=R.Envelope[R.TurnoverReport])
async def turnover_report(fy: str, admin=Depends(tokenRequired(["ADMIN"]))):
    a, b = fy_range(fy, None)
    rows = await cpo.fetch_all(
        "SELECT date_trunc('month', created_at AT TIME ZONE 'Asia/Kolkata')::date AS month, count(*) AS joins, "
        "sum(amount)::bigint AS turnover FROM company_ledger WHERE type = 'turnover' AND created_at >= $1 "
        "AND created_at < $2 GROUP BY 1 ORDER BY 1", a, b)
    total = sum(r["turnover"] for r in rows)
    return respond({"msg": "", "status": 200, "data": {
        "fy": fy, "rows": [_money(r, "turnover") for r in rows],
        "totals": {"joins": sum(r["joins"] for r in rows), "turnover": rupees(total)},
        # SRS: GST registration may be needed once a year's turnover passes ₹40 lakh (talk to the CA).
        "gst_threshold": 4000000.0, "over_gst_threshold": total > 400000000}})


# ================================================================ CSV exports
EXPORTS = ("tds", "tds-no-pan", "members", "payment-requests", "withdrawals", "pins", "income-entries", "orders")


@reportApi.get("/admin/exports/{name}.csv", response_class=StreamingResponse, responses=CSV_RESPONSE)
async def export_csv(name: str, fy: Optional[str] = None, quarter: Optional[str] = Query(None, pattern="^Q[1-4]$"),
                     status: Optional[str] = Query(None, max_length=20),
                     start: Optional[date] = Query(None, alias="from"), end: Optional[date] = Query(None, alias="to"),
                     admin=Depends(tokenRequired(["ADMIN"]))):
    """tds, tds-no-pan (fy, quarter) · members, payment-requests, withdrawals, pins, orders (status) ·
    income-entries (from, to). Money is in rupees."""
    if name not in EXPORTS:
        fail(404, "Unknown export.")
    st = {"status": status}
    rs = "round({}/100.0, 2)"
    if name in ("tds", "tds-no-pan"):
        if not fy:
            fail(400, "Choose the financial year (fy=2026-27).")
        a, b = fy_range(fy, quarter)
        if name == "tds":
            header, sql = ["PAN", "Name", "IDs", "Gross income", "TDS", "Net"], \
                f"SELECT pan, name, ids, {rs.format('gross')}, {rs.format('tds')}, {rs.format('net')} FROM ({TDS_BY_PAN}) x ORDER BY pan"
        else:
            header, sql = ["ID", "Name", "Mobile", "Gross income", "TDS", "Net"], \
                f"SELECT user_code, full_name, mobile, {rs.format('gross')}, {rs.format('tds')}, {rs.format('net')} " \
                f"FROM ({TDS_NO_PAN}) x ORDER BY user_code"
        args: tuple = (a, b, TAXED_TYPES)
        st = {"fy": fy, "quarter": quarter}
    elif name == "members":
        header = ["ID", "Name", "Mobile", "Email", "Status", "Sponsor", "State", "District", "Joined", "Activated",
                  "Directs", "Rank", "Income wallet", "Fund wallet", "Product wallet", "PAN"]
        sql = (f"SELECT u.user_code, u.full_name, u.mobile, u.email, u.status, s.user_code, u.state, u.district, u.created_at, "
               f"u.activated_at, u.direct_count, r.rank, {rs.format('w.income')}, {rs.format('w.fund')}, "
               f"{rs.format('w.product')}, u.pan FROM users u LEFT JOIN users s ON s.id = u.sponsor_id "
               f"LEFT JOIN wallets w ON w.user_id = u.id LEFT JOIN user_rank_stats r ON r.user_id = u.id "
               f"WHERE u.role = 'USER' AND ($1::text IS NULL OR u.status = $1) ORDER BY u.id")
        args = (status,)
    elif name == "payment-requests":
        header = ["Request", "ID", "Name", "For", "Amount", "PINs", "UTR", "Status", "Remark", "Sent", "Checked", "Checked by"]
        sql = (f"SELECT p.id, u.user_code, u.full_name, p.purpose, {rs.format('p.amount')}, p.pin_qty, p.utr, p.status, "
               f"p.admin_remark, p.created_at, p.reviewed_at, a.full_name FROM payment_requests p "
               f"JOIN users u ON u.id = p.user_id LEFT JOIN users a ON a.id = p.reviewed_by "
               f"WHERE ($1::text IS NULL OR p.status = $1) ORDER BY p.id")
        args = (status,)
    elif name == "withdrawals":
        # Bank upload format first: name, account, IFSC, amount.
        header = ["Account holder", "Account number", "IFSC", "Amount", "ID", "Member", "PAN", "Status", "UTR",
                  "Requested", "Paid / rejected"]
        sql = (f"SELECT w.bank->>'holder', w.bank->>'account', w.bank->>'ifsc', {rs.format('w.amount')}, u.user_code, "
               f"u.full_name, w.bank->>'pan', w.status, w.utr, w.created_at, w.reviewed_at FROM withdrawals w "
               f"JOIN users u ON u.id = w.user_id WHERE ($1::text IS NULL OR w.status = $1) ORDER BY w.id")
        args = (status,)
    elif name == "pins":
        header = ["PIN", "Status", "Bought by", "Now with", "Used for", "Used by", "Source", "Created", "Used"]
        sql = ("SELECT p.code, p.status, b.user_code, o.user_code, f.user_code, x.user_code, p.source, p.created_at, p.used_at "
               "FROM pins p JOIN users b ON b.id = p.bought_by JOIN users o ON o.id = p.owner_id "
               "LEFT JOIN users f ON f.id = p.used_for LEFT JOIN users x ON x.id = p.used_by "
               "WHERE ($1::text IS NULL OR p.status = $1) ORDER BY p.id")
        args = (status,)
    elif name == "orders":
        header = ["Order", "ID", "Member", "Status", "Products (₹)", "From product wallet", "From fund wallet",
                  "From income wallet", "Delivery (₹)", "Delivery UTR", "Delivery payment", "PIN code", "Courier",
                  "Tracking", "Placed", "Delivered", "Cancelled"]
        sql = (f"SELECT o.order_no, u.user_code, u.full_name, o.status, {rs.format('o.items_total')}, "
               f"{rs.format('o.paid_product')}, {rs.format('o.paid_fund')}, {rs.format('o.paid_income')}, "
               f"{rs.format('o.courier_charge')}, o.courier_utr, o.courier_status, o.address->>'pincode', o.courier_name, "
               f"o.tracking_no, o.created_at, o.delivered_at, o.cancelled_at FROM orders o JOIN users u ON u.id = o.user_id "
               f"WHERE ($1::text IS NULL OR o.status = $1) ORDER BY o.id")
        args = (status,)
    else:   # income-entries
        a, b = day_range(start, end)
        header = ["Date", "ID", "Member", "Income", "Gross", "TDS", "Service", "Single Leg", "Other cut", "Net", "From"]
        sql = (f"SELECT l.created_at, u.user_code, u.full_name, l.type, {rs.format('l.gross')}, {rs.format('l.tds')}, "
               f"{rs.format('l.service')}, {rs.format('l.single_leg')}, {rs.format('l.other_cut')}, {rs.format('l.amount')}, "
               f"l.meta->>'from' FROM wallet_ledger l JOIN users u ON u.id = l.user_id "
               f"WHERE l.type = ANY($3::text[]) AND l.created_at >= $1 AND l.created_at < $2 ORDER BY l.id")
        args = (a, b, TAXED_TYPES)
        st = {"from": start, "to": end}
    await _export_audit(admin, name, st)
    return stream_csv(f"treeway-{name}-{today_ist():%Y%m%d}.csv", header, sql, *args)
