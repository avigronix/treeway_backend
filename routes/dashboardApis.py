from base import *
from models import responses as R
from common.mailSender import send_mail_or_fail, send_notice_mail
from models.model import AdminEmailModel, AdminEmailOtpModel, AdminMobileModel, AdminRemarkModel, MemberStatusModel
from routes.matrix import super3_status_data
from routes.users import team_page
from routes.videoApis import day_no, missing_video_days

dashboardApi = APIRouter()

KEPT_OUT = ("tds", "self_help", "s3_hold", "s3_level_pool", "gullak_held")


def _month_range(month: Optional[str]):
    """'2026-10' -> (start, end) in IST. Default: this month."""
    try:
        y, m = map(int, (month or f"{today_ist():%Y-%m}").split("-"))
        start = datetime(y, m, 1, tzinfo=IST)
    except ValueError:
        fail(400, "Month must look like 2026-10.")
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=IST)
    return start, end


# ================================================================ member
@dashboardApi.get("/dashboard", response_model=R.Envelope[R.MemberDashboard])
async def member_dashboard(user=Depends(tokenRequired())):
    w = await cpo.fetch_one("SELECT income, fund, product, total_earned, total_withdrawn FROM wallets WHERE user_id = $1",
                            user["id"])
    team = await cpo.fetch_one(
        "SELECT count(*) FILTER (WHERE u.level = 1) AS directs, count(*) AS team, "
        "count(*) FILTER (WHERE m.status = 'active') AS active_team "
        "FROM user_uplines u JOIN users m ON m.id = u.user_id WHERE u.ancestor_id = $1 AND m.status <> 'removed'",
        user["id"])
    by_type = await cpo.fetch_all(
        "SELECT type, sum(amount) AS net, sum(amount) FILTER (WHERE created_at >= $3) AS net_month FROM wallet_ledger "
        "WHERE user_id = $1 AND type = ANY($2::text[]) GROUP BY type",
        user["id"], list(INCOME_TYPES), period_start("month"))
    pending = await cpo.fetch_one(
        "SELECT (SELECT status FROM payment_requests WHERE user_id = $1 AND purpose = 'activation' "
        "        ORDER BY id DESC LIMIT 1) AS activation_request, "
        "(SELECT amount FROM withdrawals WHERE user_id = $1 AND status = 'pending') AS pending_withdrawal, "
        "(SELECT rank FROM user_rank_stats WHERE user_id = $1) AS rank", user["id"])
    rank = pending["rank"] or 0
    return respond({"msg": "", "status": 200, "data": {
        "status": user["status"], "rank": rank, "rank_name": RANK_NAMES[rank - 1] if rank else None,
        "wallet": {k: rupees(v) for k, v in w.items()},
        "team": team,
        "income": {r["type"]: rupees(r["net"]) for r in by_type},
        "income_this_month": rupees(sum(r["net_month"] or 0 for r in by_type)),
        "activation_request": pending["activation_request"],
        "pending_withdrawal": rupees(pending["pending_withdrawal"]) if pending["pending_withdrawal"] else None,
    }})


# ================================================================ admin
@dashboardApi.get("/admin/dashboard", response_model=R.Envelope[R.AdminDashboard])
async def admin_dashboard(admin=Depends(tokenRequired(["ADMIN"]))):
    """Waiting work (queue) is live. Totals that read lakhs of rows are cached for 60 seconds (`totals_as_of`)."""
    queue = await cpo.fetch_one(
        "SELECT (SELECT count(*) FROM payment_requests WHERE status = 'pending') AS payment_requests, "
        "(SELECT count(*) FROM withdrawals WHERE status = 'pending') AS withdrawals, "
        "(SELECT coalesce(sum(amount), 0) FROM withdrawals WHERE status = 'pending') AS withdrawals_amount, "
        "(SELECT count(*) FROM complaints WHERE status IN ('Submitted', 'In Process')) AS open_complaints, "
        # Complaints with a member message the admins have not read yet ("2 with new messages").
        "(SELECT count(*) FROM complaints c WHERE EXISTS (SELECT 1 FROM complaint_messages m WHERE m.complaint_id = c.id "
        "AND m.sender_id = c.user_id AND m.id > coalesce(c.admin_last_read, 0))) AS complaints_new_messages, "
        "(SELECT count(*) FROM orders WHERE status = 'placed' AND courier_status = 'approved') AS orders_to_pack, "
        "(SELECT count(*) FROM orders WHERE status = 'placed' AND courier_status = 'pending') AS order_payments_to_check, "
        "(SELECT count(*) FROM doctor_requests WHERE status = 'waiting') AS doctor_requests_waiting, "
        "(SELECT count(*) FROM doctor_requests WHERE status = 'waiting' AND created_at < now() - interval '24 hours') "
        "AS doctor_requests_overdue, "
        "(SELECT count(*) FROM contact_messages WHERE status = 'new') AS contact_messages_new, "
        "(SELECT min(created_at) FROM withdrawals WHERE status = 'pending') AS oldest_withdrawal")
    by_purpose = {r["purpose"]: r["n"] for r in await cpo.fetch_all(
        "SELECT purpose, count(*) AS n FROM payment_requests WHERE status = 'pending' GROUP BY purpose")}
    queue["payment_requests_by_purpose"] = {p: by_purpose.get(p, 0) for p in ("activation", "fund", "pins")}
    queue["withdrawals_amount"] = rupees(queue["withdrawals_amount"])
    queue["video_days_missing"] = await missing_video_days(2)   # today + tomorrow without all 4 links
    totals, as_of = await cached("admin_dashboard_totals", 60, _dashboard_totals)
    return respond({"msg": "", "status": 200, "data": {"queue": queue, **totals, "totals_as_of": as_of}})


async def _dashboard_totals() -> Dict:
    today = datetime.combine(today_ist(), datetime.min.time(), IST)
    members = await cpo.fetch_one(
        "SELECT count(*) FILTER (WHERE status = 'active') AS active, count(*) FILTER (WHERE status = 'inactive') AS inactive, "
        "count(*) FILTER (WHERE status = 'blocked') AS blocked, count(*) FILTER (WHERE activated_at >= $1) AS activated_today, "
        "count(*) FILTER (WHERE created_at >= $1) AS joined_today FROM users WHERE role = 'USER'", today)
    money = await cpo.fetch_one(
        "SELECT coalesce(sum(income), 0) AS member_income_wallets, coalesce(sum(fund), 0) AS member_fund_wallets, "
        "coalesce(sum(product), 0) AS member_product_wallets FROM wallets")
    funds = await cpo.fetch_all("SELECT type, sum(amount) AS total FROM company_ledger WHERE type = ANY($1::text[]) GROUP BY type",
                                list(KEPT_OUT) + ["turnover"])
    sl = await cpo.fetch_val("SELECT coalesce(sum(amount), 0) FROM single_leg_pool WHERE distributed_at IS NULL AND cancelled_at IS NULL")
    yesterday = today - timedelta(days=1)
    joins = await cpo.fetch_one(
        "SELECT (SELECT count(*) FROM users WHERE role = 'USER' AND created_at >= $1 AND created_at < $2) AS joined_yesterday, "
        "(SELECT count(*) FROM users WHERE role = 'USER' AND activated_at >= $1 AND activated_at < $2) AS activated_yesterday, "
        "(SELECT coalesce(sum(amount), 0)::bigint FROM company_ledger WHERE type = 'turnover' AND created_at >= $3) "
        "AS turnover_this_month, "
        "(SELECT count(*) FROM company_ledger WHERE type = 'turnover' AND created_at >= $3) AS joins_this_month, "
        "(SELECT count(*) FROM super3_nodes WHERE user_id IS NOT NULL) AS ids_in_super3, "
        "(SELECT count(*) FROM super3_nodes WHERE user_id IS NOT NULL AND status = 'full') AS boards_full",
        yesterday, today, period_start("month"))
    joins["turnover_this_month"] = rupees(joins["turnover_this_month"])
    first_day = today_ist() - timedelta(days=29)
    per_day = {r["day"]: r["n"] for r in await cpo.fetch_all(
        "SELECT (activated_at AT TIME ZONE 'Asia/Kolkata')::date AS day, count(*) AS n FROM users "
        "WHERE role = 'USER' AND activated_at >= $1 GROUP BY 1", datetime.combine(first_day, datetime.min.time(), IST))}
    chart = [{"day": first_day + timedelta(days=i), "activated": per_day.get(first_day + timedelta(days=i), 0)}
             for i in range(30)]
    return {"members": members, "joins": joins, "last_30_days": chart,
            "money": {**{k: rupees(v) for k, v in money.items()}, **{r["type"]: rupees(r["total"]) for r in funds},
                      "single_leg_pending": rupees(sl)}}


@dashboardApi.get("/admin/pnl", response_model=R.Envelope[R.Pnl])
async def profit_and_loss(month: Optional[str] = None, admin=Depends(tokenRequired(["ADMIN"]))):
    """Monthly profit. TDS, Self Help Fund, money on hold and pools are kept out (not company money)."""
    s = await get_settings()
    start, end = _month_range(month)
    c = {r["type"]: r["total"] for r in await cpo.fetch_all(
        "SELECT type, sum(amount) AS total, count(*) AS n FROM company_ledger WHERE created_at >= $1 AND created_at < $2 "
        "GROUP BY type", start, end)}
    joins = await cpo.fetch_val("SELECT count(*) FROM company_ledger WHERE type = 'turnover' AND created_at >= $1 "
                                "AND created_at < $2", start, end)
    turnover = c.get("turnover", 0)
    level_pool = sum(level_amount(s["level_income"], lv) for lv in range(1, 26)) * joins
    s3_pool = s["super3"]["share_per_join"] * joins
    # Product cost = 5% of the MRP of products sold this month (orders not cancelled).
    shop = await cpo.fetch_one(
        "SELECT coalesce(sum(items_total), 0)::bigint AS mrp, coalesce(sum(paid_product), 0)::bigint AS product, "
        "coalesce(sum(paid_fund), 0)::bigint AS fund, coalesce(sum(paid_income), 0)::bigint AS income FROM orders "
        "WHERE status <> 'cancelled' AND created_at >= $1 AND created_at < $2", start, end)
    product_cost = percent_of(shop["mrp"], s["product_cost_percent"])
    # Royalty: the 6 rank pools of this month's turnover (paid on the 3rd of next month; empty pools and
    # rounding come back as company income when paid).
    royalty_pools = percent_of(turnover, s["ranks"]["royalty_percent"]) * len(RANK_NAMES)
    lapses = sum(v for k, v in c.items() if k.startswith("lapse_"))
    other_income = (c.get("service", 0) + lapses + c.get("s3_company", 0) + c.get("p2p_fee", 0)
                    + c.get("admin_debit", 0) + c.get("reversal", 0) + c.get("single_leg_company", 0))
    other_income += c.get("royalty_company", 0) + c.get("s3_level_company", 0) + c.get("gullak_charge", 0)
    shop_sales = shop["fund"] + shop["income"]            # Product Wallet spend is not counted as a sale
    if s["shop_sales_in_profit"]:
        other_income += shop_sales
    profit = turnover - level_pool - s3_pool - product_cost - royalty_pools - c.get("gullak_interest", 0) + other_income
    return respond({"msg": "", "status": 200, "data": {
        "month": f"{start:%Y-%m}", "joins": joins,
        "money_in": {"turnover": rupees(turnover)},
        "costs": {"level_income_pool": rupees(level_pool), "super3_share": rupees(s3_pool),
                  "product_cost": rupees(product_cost), "royalty_pools": rupees(royalty_pools),
                  "gullak_interest": rupees(c.get("gullak_interest", 0))},
        "company_income": {"service_charge": rupees(c.get("service", 0)), "unpaid_levels_and_lapses": rupees(lapses),
                           "company_spots_super3": rupees(c.get("s3_company", 0)), "p2p_fee": rupees(c.get("p2p_fee", 0)),
                           "admin_debits": rupees(c.get("admin_debit", 0)),
                           "reversals": rupees(c.get("reversal", 0)),
                           "single_leg_leftover": rupees(c.get("single_leg_company", 0)),
                           "royalty_leftover": rupees(c.get("royalty_company", 0)),
                           "s3_level_leftover": rupees(c.get("s3_level_company", 0)),
                           "gullak_charges": rupees(c.get("gullak_charge", 0))},
        "kept_out": {k: rupees(c.get(k, 0)) for k in KEPT_OUT},
        "profit": rupees(profit),
        # For information (not in the profit above): what the shop sold and which wallets paid for it.
        "shop": {"mrp_sold": rupees(shop["mrp"]), "from_product_wallet": rupees(shop["product"]),
                 "from_fund_wallet": rupees(shop["fund"]), "from_income_wallet": rupees(shop["income"]),
                 "sales": rupees(shop_sales), "in_profit": s["shop_sales_in_profit"]},
    }})


@dashboardApi.get("/admin/members", response_model=R.Envelope[R.AdminMembersPage])
async def admin_members(status: Optional[str] = None, search: Optional[str] = Query(None, max_length=40),
                        state: Optional[str] = Query(None, max_length=60), rank: Optional[int] = Query(None, ge=0, le=6),
                        cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20, admin=Depends(tokenRequired(["ADMIN"]))):
    sql = ("SELECT u.id, u.user_code, u.full_name, u.mobile, u.email, u.status, u.direct_count, u.created_at, "
           "u.activated_at, s.user_code AS sponsor_code, w.income, w.fund, w.product, u.state, u.district, "
           "coalesce(r.rank, 0) AS rank, coalesce(r.active_team, 0) AS active_team, u.id AS _k "
           "FROM users u LEFT JOIN users s ON s.id = u.sponsor_id LEFT JOIN wallets w ON w.user_id = u.id "
           "LEFT JOIN user_rank_stats r ON r.user_id = u.id WHERE u.role = 'USER'")
    args: List[Any] = []
    if status in ("active", "inactive", "blocked", "removed"):
        args.append(status)
        sql += f" AND u.status = ${len(args)}"
    if state:
        args.append(state)
        sql += f" AND u.state = ${len(args)}"
    if rank is not None:
        args.append(rank)
        sql += f" AND coalesce(r.rank, 0) = ${len(args)}"
    if search:
        q = search.strip()
        args.append(q.upper())
        sql += f" AND (u.user_code = ${len(args)} OR u.mobile = ${len(args)}"
        args.append(f"%{q}%")
        sql += f" OR u.full_name ILIKE ${len(args)})"
    data = await cpo.paginate(sql, *args, page=page, limit=limit, cursor=cursor)
    for r in data["rows"]:
        for k in ("income", "fund", "product"):
            r[k] = rupees(r[k])
        r["rank_name"] = RANK_NAMES[r["rank"] - 1] if r["rank"] else None
        # "Not active · day 3"
        r["days_inactive"] = (today_ist() - r["created_at"].astimezone(IST).date()).days + 1 \
            if r["status"] == "inactive" else None
    data["counts"] = await cpo.fetch_one(
        "SELECT count(*) AS total, count(*) FILTER (WHERE status = 'active') AS active, "
        "count(*) FILTER (WHERE status = 'inactive') AS inactive, count(*) FILTER (WHERE status = 'blocked') AS blocked, "
        "count(*) FILTER (WHERE status = 'removed') AS removed FROM users WHERE role = 'USER'")
    return respond({"msg": "", "status": 200, "data": data})


async def _member_id(userCode: str) -> int:
    uid = await cpo.fetch_val("SELECT id FROM users WHERE user_code = $1 AND role = 'USER'", userCode.strip().upper())
    if not uid:
        fail(404, "Member not found.")
    return uid


@dashboardApi.get("/admin/members/{userCode}", response_model=R.Envelope[R.AdminMemberDetail])
async def admin_member(userCode: str, admin=Depends(tokenRequired(["ADMIN"]))):
    u = await cpo.fetch_one(
        "SELECT u.*, s.user_code AS sponsor_code, s.full_name AS sponsor_name FROM users u "
        "LEFT JOIN users s ON s.id = u.sponsor_id WHERE u.user_code = $1", userCode.strip().upper())
    if not u:
        fail(404, "Member not found.")
    u.pop("password_hash")
    u.pop("dob")   # not asked since 16 Oct (bhaiya); old values stay in the DB only
    w = await cpo.fetch_one("SELECT * FROM wallets WHERE user_id = $1", u["id"])
    for k in ("income", "fund", "product", "total_earned", "total_withdrawn"):
        w[k] = rupees(w[k])
    # Admin sees everything, including hidden admin debits.
    ledger = await cpo.fetch_all(
        "SELECT l.*, EXISTS (SELECT 1 FROM wallet_ledger r WHERE r.ref = 'rev:' || l.id) AS reversed "
        "FROM wallet_ledger l WHERE l.user_id = $1 ORDER BY l.id DESC LIMIT 50", u["id"])
    for r in ledger:
        for k in ("amount", "balance_after", "gross", "tds", "service", "single_leg", "other_cut"):
            r[k] = rupees(r[k])
    videos = {"day_no": day_no(u["activated_at"], today_ist()), "days_total": (await get_settings())["video"]["days"],
              "days_done": await cpo.fetch_val("SELECT count(*) FROM wallet_ledger WHERE user_id = $1 AND type = 'yfi_daily'",
                                               u["id"])}
    uid = u["id"]
    s = await cpo.fetch_one(
        """
        SELECT (SELECT amount FROM withdrawals WHERE user_id = $1 AND status = 'pending') AS pending_withdrawal,
               (SELECT count(*) FROM user_uplines WHERE user_id = $1) AS people_above,
               (SELECT count(*) FROM complaints WHERE user_id = $1 AND status IN ('Submitted', 'In Process')) AS complaints_open,
               (SELECT count(*) FROM complaints WHERE user_id = $1 AND status IN ('Resolved', 'Closed')) AS complaints_solved
        """, uid)
    levels = await cpo.fetch_all(
        "SELECT u.level, count(*) AS people, count(*) FILTER (WHERE m.status = 'active') AS active "
        "FROM user_uplines u JOIN users m ON m.id = u.user_id WHERE u.ancestor_id = $1 AND m.status <> 'removed' "
        "GROUP BY u.level ORDER BY u.level", uid)
    # One pass over the member's level income (a sub-query per level read a big leader's ledger 25 times: 6 s).
    by_level = {r["level"]: r["net"] for r in await cpo.fetch_all(
        "SELECT (meta->>'level')::int AS level, sum(amount)::bigint AS net FROM wallet_ledger "
        "WHERE user_id = $1 AND type = 'level_income' GROUP BY 1", uid)}
    levels = [{**x, "level_income": by_level.get(x["level"], 0)} for x in levels]
    rank = await cpo.fetch_one("SELECT rank, active_team, active_directs, computed_at FROM user_rank_stats WHERE user_id = $1", uid)
    spot = await cpo.fetch_one(
        "SELECT j.current_board AS board, n.position FROM super3_journeys j JOIN super3_nodes n ON n.journey_id = j.id "
        "AND n.board = j.current_board WHERE j.user_id = $1 AND j.kind = 'main'", uid)
    deduction = await cpo.fetch_one(
        "SELECT l.amount, l.created_at, l.meta->>'comment' AS comment, a.full_name AS by_name FROM wallet_ledger l "
        "LEFT JOIN users a ON a.id = (l.meta->>'admin_id')::bigint WHERE l.user_id = $1 AND l.type = 'admin_debit' "
        "ORDER BY l.id DESC LIMIT 1", uid)
    summary = {
        "pending_withdrawal": rupees(s["pending_withdrawal"]) if s["pending_withdrawal"] else None,
        "people_above": s["people_above"],
        "team_by_level": [{**x, "level_income": rupees(x["level_income"])} for x in levels],
        "team_25_levels": {"people": sum(x["people"] for x in levels), "active": sum(x["active"] for x in levels),
                           "directs": next((x["people"] for x in levels if x["level"] == 1), 0)},
        "rank": {"rank": rank["rank"], "rank_name": RANK_NAMES[rank["rank"] - 1] if rank["rank"] else None,
                 "active_team_full": rank["active_team"], "active_directs": rank["active_directs"], "as_of": rank["computed_at"]}
        if rank else None,
        "super3": {"board": spot["board"] if spot else None, "spot": spot["position"] if spot else None,
                   "recycle_ids": await cpo.fetch_val("SELECT count(*) FROM super3_journeys WHERE user_id = $1 "
                                                      "AND kind = 'recycle'", uid)},
        "complaints": {"open": s["complaints_open"], "solved": s["complaints_solved"]},
        "last_deduction": {**deduction, "amount": rupees(-deduction["amount"])} if deduction else None,
    }
    return respond({"msg": "", "status": 200, "data": {"member": u, "wallet": w, "ledger": ledger, "videos": videos,
                                                        "summary": summary}})


@dashboardApi.get("/admin/members/{userCode}/history", response_model=R.Envelope[R.Page[R.AdminLedgerRow]])
async def admin_member_history(userCode: str, wallet: Optional[str] = Query(None, pattern="^(income|fund|product)$"),
                               type: Optional[str] = Query(None, max_length=40),
                               group: Optional[str] = Query(None, pattern=f"^({'|'.join(HISTORY_GROUPS)})$"),
                               cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20,
                               admin=Depends(tokenRequired(["ADMIN"]))):
    """Income / Wallet tabs. Shows hidden admin debits too (admin only)."""
    uid = await _member_id(userCode)
    # reversed: a reverse entry exists (ref rev:<id>), so the screen hides "Reverse" on it.
    sql = ("SELECT l.*, EXISTS (SELECT 1 FROM wallet_ledger r WHERE r.ref = 'rev:' || l.id) AS reversed, l.id AS _k "
           "FROM wallet_ledger l WHERE l.user_id = $1")
    args: List[Any] = [uid]
    for cond, val in (("l.wallet = ${}", wallet), ("l.type = ${}", type)):
        if val:
            args.append(val)
            sql += " AND " + cond.format(len(args))
    if group:
        args.append(list(HISTORY_GROUPS[group]))
        sql += f" AND l.type = ANY(${len(args)}::text[])"
    data = await cpo.paginate(sql, *args, page=page, limit=limit, cursor=cursor)
    for r in data["rows"]:
        for k in ("amount", "balance_after", "gross", "tds", "service", "single_leg", "other_cut"):
            r[k] = rupees(r[k])
    return respond({"msg": "", "status": 200, "data": data})


@dashboardApi.get("/admin/members/{userCode}/team", response_model=R.Envelope[R.Page[R.TeamMember]])
async def admin_member_team(userCode: str, level: Optional[int] = Query(None, ge=1, le=25),
                            status: Optional[str] = Query(None, pattern="^(active|inactive)$"),
                            search: Optional[str] = Query(None, max_length=40),
                            cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20,
                            admin=Depends(tokenRequired(["ADMIN"]))):
    data = await team_page(await _member_id(userCode), level, status, search, cursor, page, limit)
    return respond({"msg": "", "status": 200, "data": data})


@dashboardApi.get("/admin/members/{userCode}/super3", response_model=R.Envelope[R.Super3Status])
async def admin_member_super3(userCode: str, admin=Depends(tokenRequired(["ADMIN"]))):
    return respond({"msg": "", "status": 200, "data": await super3_status_data(await _member_id(userCode))})


@dashboardApi.post("/admin/members/{userCode}/status", response_model=R.Envelope[R.NoData])
async def admin_member_status(userCode: str, body: MemberStatusModel, admin=Depends(tokenRequired(["ADMIN"]))):
    async with cpo.transaction() as conn:
        u = await cpo.fetch_one("SELECT id, status, activated_at, role FROM users WHERE user_code = $1 FOR UPDATE",
                                userCode.strip().upper(), conn=conn)
        if not u or u["role"] != "USER":
            fail(404, "Member not found.")
        if body.action == "block":
            if u["status"] in ("blocked", "removed"):
                fail(400, f"This ID is already {u['status']}.")
            new = "blocked"
        else:
            if u["status"] != "blocked":
                fail(400, "This ID is not blocked.")
            new = "active" if u["activated_at"] else "inactive"
        await cpo.execute("UPDATE users SET status = $2, token_version = token_version + 1 WHERE id = $1",
                          u["id"], new, conn=conn)
        await audit(conn, admin["id"], f"member_{body.action}", userCode, {"remark": body.remark})
    await forget_auth_user(u["id"])
    return respond({"msg": f"Member {body.action}ed.", "status": 200})


# ---------------------------------------------------------------- admin fixes a member's mobile / email, sends a reset
# Vinay, 16 Oct: audit log + a notice to the old email; a new email is confirmed with an OTP sent to it; the admin
# never sees a password (or the reset link); every change ends all the member's sessions.
RESET_LINK_TTL = 24 * 3600


def _mask_mobile(m: str) -> str:
    return f"******{m[-4:]}"


async def _member_row(conn, code: str) -> Dict:
    u = await cpo.fetch_one("SELECT id, user_code, full_name, mobile, email, role FROM users WHERE user_code = $1 "
                            "FOR UPDATE", code.strip().upper(), conn=conn)
    if not u or u["role"] != "USER":
        fail(404, "Member not found.")
    return u


def _when() -> str:
    return f"{datetime.now(IST):%d %b %Y, %I:%M %p} IST"


@dashboardApi.post("/admin/members/{userCode}/mobile", response_model=R.Envelope[R.NoData])
async def admin_member_mobile(userCode: str, body: AdminMobileModel, admin=Depends(tokenRequired(["ADMIN"]))):
    async with cpo.transaction() as conn:
        u = await _member_row(conn, userCode)
        if u["mobile"] == body.mobile:
            fail(400, "This is already the member's mobile number.")
        if await cpo.fetch_val("SELECT 1 FROM users WHERE mobile = $1 AND status <> 'removed' AND id <> $2",
                               body.mobile, u["id"], conn=conn):
            fail(409, "This mobile number is already registered.")
        await cpo.execute("UPDATE users SET mobile = $2, token_version = token_version + 1 WHERE id = $1",
                          u["id"], body.mobile, conn=conn)
        await audit(conn, admin["id"], "member_mobile_change", u["user_code"],
                    {"from": u["mobile"], "to": body.mobile, "remark": body.remark})
    await forget_auth_user(u["id"])
    await redis_client.delete(f"loginfail:{u['mobile']}")
    await send_notice_mail(u["email"], u["full_name"], "Your Treeway mobile number was changed", [
        f"Treeway support changed the mobile number of your ID {u['user_code']} to {_mask_mobile(body.mobile)} "
        f"on {_when()}.",
        "You have been logged out on every phone. Log in again with your member ID or the new number.",
        "If you did not ask for this, call Treeway support at once."])
    return respond({"msg": "Mobile number changed. The member must log in again.", "status": 200})


@dashboardApi.post("/admin/members/{userCode}/email/otp", response_model=R.Envelope[R.MaskedEmail])
async def admin_member_email_otp(userCode: str, body: AdminEmailOtpModel, admin=Depends(tokenRequired(["ADMIN"]))):
    """Step 1: an OTP to the NEW email (the member reads it out to the admin). It works only for that email."""
    async with cpo.transaction() as conn:
        u = await _member_row(conn, userCode)
    if u["email"] == body.email:
        fail(400, "This is already the member's email.")
    await issue_otp("admin_email", u, body.email)
    await redis_client.setex(f"adminemail:{u['id']}", OTP_TTL, body.email)
    return respond({"msg": "OTP sent to the new email.", "status": 200, "data": {"email": mask_email(body.email)}})


@dashboardApi.post("/admin/members/{userCode}/email", response_model=R.Envelope[R.NoData])
async def admin_member_email(userCode: str, body: AdminEmailModel, admin=Depends(tokenRequired(["ADMIN"]))):
    """Step 2: the OTP from the new email confirms it."""
    async with cpo.transaction() as conn:
        u = await _member_row(conn, userCode)
        if await redis_client.get(f"adminemail:{u['id']}") != body.email:
            fail(400, "Send the OTP to this email first.")
        await check_otp("admin_email", u["id"], body.otp)
        await cpo.execute("UPDATE users SET email = $2, token_version = token_version + 1 WHERE id = $1",
                          u["id"], body.email, conn=conn)
        await audit(conn, admin["id"], "member_email_change", u["user_code"],
                    {"from": u["email"], "to": body.email, "remark": body.remark})
    await redis_client.delete(f"adminemail:{u['id']}")
    await forget_auth_user(u["id"])
    await send_notice_mail(u["email"], u["full_name"], "Your Treeway email was changed", [
        f"Treeway support changed the email of your ID {u['user_code']} to {mask_email(body.email)} on {_when()}.",
        "From now on OTPs go to the new email. You have been logged out on every phone.",
        "If you did not ask for this, call Treeway support at once."])
    return respond({"msg": "Email changed. The member must log in again.", "status": 200})


@dashboardApi.post("/admin/members/{userCode}/password-reset", response_model=R.Envelope[R.ResetLinkSent])
async def admin_member_password_reset(userCode: str, body: AdminRemarkModel, admin=Depends(tokenRequired(["ADMIN"]))):
    """Mails the member a link to set a new password (24 hours, works once; a new one replaces the old one).
    The admin never sees the link or any password. All the member's sessions end when it is sent."""
    async with cpo.transaction() as conn:
        u = await _member_row(conn, userCode)
    token = secrets.token_urlsafe(32)
    old = await redis_client.get(f"pwreset:user:{u['id']}")
    link = f"{os.environ.get('FRONTEND_URL', '')}/reset-password?t={token}"
    await send_mail_or_fail(u["email"], u["full_name"], "Set a new Treeway password", [
        f"Treeway support sent you this link to set a new password for your ID {u['user_code']}.",
        "It works once, for 24 hours. If you did not ask for it, ignore this mail and call Treeway support."], link)
    pipe = redis_client.pipeline()
    if old:
        pipe.delete(f"pwreset:{old}")
    pipe.setex(f"pwreset:{token}", RESET_LINK_TTL, u["id"])
    pipe.setex(f"pwreset:user:{u['id']}", RESET_LINK_TTL, token)
    if DEV_MODE:
        pipe.setex(f"devotp:{u['email'].lower()}:reset_link", RESET_LINK_TTL, token)   # e2e / dev seed only
    await pipe.execute()
    async with cpo.transaction() as conn:
        await cpo.execute("UPDATE users SET token_version = token_version + 1 WHERE id = $1", u["id"], conn=conn)
        await audit(conn, admin["id"], "member_password_reset_sent", u["user_code"], {"remark": body.remark})
    await forget_auth_user(u["id"])
    return respond({"msg": "Password reset link sent to the member's email.", "status": 200,
                    "data": {"email": mask_email(u["email"]), "expires_in": RESET_LINK_TTL}})


@dashboardApi.get("/admin/single-leg/runs", response_model=R.Envelope[R.Page[R.SingleLegRun]])
async def single_leg_runs(cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20,
                          admin=Depends(tokenRequired(["ADMIN"]))):
    """Night runs: pool, shared with the line, company part (missing people + rounding), status."""
    data = await cpo.paginate("SELECT *, id AS _k FROM single_leg_runs", page=page, limit=limit, cursor=cursor)
    for r in data["rows"]:
        for k in ("pool_total", "shared", "company"):
            r[k] = rupees(r[k])
    return respond({"msg": "", "status": 200, "data": data})


@dashboardApi.get("/admin/audit", response_model=R.Envelope[R.Page[R.AuditRow]])
async def admin_audit(cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 50, admin=Depends(tokenRequired(["ADMIN"]))):
    data = await cpo.paginate(
        "SELECT a.*, u.full_name AS admin_name, a.id AS _k FROM admin_audit a JOIN users u ON u.id = a.admin_id",
        page=page, limit=limit, cursor=cursor)
    return respond({"msg": "", "status": 200, "data": data})
