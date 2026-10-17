import asyncpg

from adminApproval import ask_second_admin, needs_second_admin, waiting_response
from base import *
from models import responses as R
from mlmEngine import activate_user, company_add, issue_pins, wallet_move
from models.model import AdminDebitModel, P2PModel, PaymentRequestModel, ReviewModel, WithdrawModel, WithdrawReviewModel

walletApi = APIRouter()

MEMBER_LEDGER_COLS = "id, wallet, type, amount, balance_after, gross, tds, service, single_leg, other_cut, meta, created_at"


# ================================================================ balances + history
@walletApi.get("/wallet/summary", response_model=R.Envelope[R.WalletSummary])
async def wallet_summary(user=Depends(tokenRequired())):
    w = await cpo.fetch_one("SELECT income, fund, product, total_earned, total_withdrawn FROM wallets WHERE user_id = $1",
                            user["id"])
    today = await cpo.fetch_val(
        "SELECT coalesce(sum(amount), 0) FROM wallet_ledger WHERE user_id = $1 AND wallet = 'income' AND amount > 0 "
        "AND visible_to_user AND type NOT IN ('p2p_in', 'withdrawal_refund') AND created_at >= $2",
        user["id"], datetime.combine(today_ist(), datetime.min.time(), IST))
    pins = await cpo.count("pins", {"owner_id": user["id"], "status": "ready"})
    data = {k: rupees(v) for k, v in w.items()}
    data.update({"today_income": rupees(today), "ready_pins": pins})
    return respond({"msg": "", "status": 200, "data": data})


@walletApi.get("/wallet/history", response_model=R.Envelope[R.Page[R.LedgerRow]])
async def wallet_history(wallet: Optional[str] = Query(None, pattern="^(income|fund|product)$"),
                         type: Optional[str] = Query(None, max_length=40),
                         group: Optional[str] = Query(None, pattern=f"^({'|'.join(HISTORY_GROUPS)})$"),
                         direction: Optional[str] = Query(None, pattern="^(in|out)$"),
                         cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20, user=Depends(tokenRequired())):
    # Admin debits are saved with visible_to_user = FALSE and never shown here.
    sql = (f"SELECT {', '.join('l.' + c.strip() for c in MEMBER_LEDGER_COLS.split(','))}, l.id AS _k, "
           "w.status AS withdrawal_status, w.utr AS withdrawal_utr "
           "FROM wallet_ledger l LEFT JOIN withdrawals w ON l.type = 'withdrawal' "
           "AND w.id = (l.meta->>'withdrawal_id')::bigint WHERE l.user_id = $1 AND l.visible_to_user")
    args: List[Any] = [user["id"]]
    if wallet:
        args.append(wallet)
        sql += f" AND l.wallet = ${len(args)}"
    if type:
        args.append(type)
        sql += f" AND l.type = ${len(args)}"
    if group:
        args.append(list(HISTORY_GROUPS[group]))
        sql += f" AND l.type = ANY(${len(args)}::text[])"
    if direction:
        sql += " AND l.amount > 0" if direction == "in" else " AND l.amount < 0"
    data = await cpo.paginate(sql, *args, page=page, limit=limit, cursor=cursor)
    for r in data["rows"]:
        for k in ("amount", "balance_after", "gross", "tds", "service", "single_leg", "other_cut"):
            r[k] = rupees(r[k])
    return respond({"msg": "", "status": 200, "data": data})


@walletApi.get("/wallet/income-summary", response_model=R.Envelope[R.IncomeSummary])
async def income_summary(period: str = Query("month", pattern="^(today|week|month|all)$"), user=Depends(tokenRequired())):
    """'My income' screen: per income type for today / this week (from Monday) / this month, all after the cut."""
    rows = await cpo.fetch_all(
        "SELECT type, count(*) AS count, sum(amount) AS net, sum(gross) AS gross, sum(tds) AS tds, "
        "sum(service) AS service, sum(single_leg) AS single_leg, sum(other_cut) AS other_cut "
        "FROM wallet_ledger WHERE user_id = $1 AND type = ANY($2::text[]) AND created_at >= $3 GROUP BY type",
        user["id"], list(INCOME_TYPES), period_start(period))
    by_type = {t: {"count": 0, "net": 0.0, "gross": 0.0, "tds": 0.0} for t in INCOME_TYPES}
    total = {"count": 0, "net": 0, "gross": 0, "tds": 0, "service": 0, "single_leg": 0, "other_cut": 0}
    for r in rows:
        by_type[r["type"]] = {"count": r["count"], "net": rupees(r["net"]), "gross": rupees(r["gross"]),
                              "tds": rupees(r["tds"])}
        for k in total:
            total[k] += r[k]
    return respond({"msg": "", "status": 200, "data": {
        "period": period, "from": period_start(period), "by_type": by_type,
        "total": {"count": total.pop("count"), **{k: rupees(v) for k, v in total.items()}}}})


@walletApi.get("/wallet/level-income", response_model=R.Envelope[R.LevelIncomePage])
async def level_income(level: Optional[int] = Query(None, ge=1, le=25), cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20,
                       user=Depends(tokenRequired())):
    sql = ("SELECT amount, gross, meta->>'from' AS from_code, meta->>'from_name' AS from_name, (meta->>'level')::int AS level, created_at, id AS _k "
           "FROM wallet_ledger WHERE user_id = $1 AND type = 'level_income'")
    args: List[Any] = [user["id"]]
    if level:
        args.append(str(level))
        sql += f" AND meta->>'level' = ${len(args)}"
    data = await cpo.paginate(sql, *args, page=page, limit=limit, cursor=cursor)
    for r in data["rows"]:
        r["amount"], r["gross"] = rupees(r["amount"]), rupees(r["gross"])
    totals = await cpo.fetch_all(
        "SELECT (meta->>'level')::int AS level, count(*) AS count, sum(amount) AS net, "
        "sum(amount) FILTER (WHERE created_at >= $2) AS net_this_month FROM wallet_ledger "
        "WHERE user_id = $1 AND type = 'level_income' GROUP BY 1 ORDER BY 1", user["id"], period_start("month"))
    data["by_level"] = [{"level": t["level"], "count": t["count"], "net": rupees(t["net"]),
                         "this_month": rupees(t["net_this_month"] or 0)} for t in totals]
    data["this_month"] = rupees(sum(t["net_this_month"] or 0 for t in totals))
    data["total_amount"] = rupees(sum(t["net"] for t in totals))   # data["total"] stays the row count
    return respond({"msg": "", "status": 200, "data": data})


# ================================================================ money in (UTR + screenshot)
@walletApi.post("/wallet/payment-request", response_model=R.Envelope[R.Created], status_code=201)
async def payment_request(request: Request, user=Depends(tokenRequired())):
    """One form for: activate my ID, add funds, buy PINs. Admin checks the UTR and approves."""
    data, form = await read_form(request, PaymentRequestModel)
    s = await get_settings()
    purpose = data["purpose"]

    if purpose == "activation":
        if user["status"] != "inactive":
            fail(400, "Your ID is already active.")
        amount = s["activation_amount"]
    else:
        if user["status"] != "active":
            fail(403, "Activate your ID first.")
        if purpose == "pins":
            amount = s["activation_amount"] * data["pin_qty"]   # qty 1..500 is checked by PaymentRequestModel
        else:
            amount = to_paise(data["amount"])
            if amount < 10000:
                fail(400, "Minimum add is ₹100.")
            if amount > 50000000:
                fail(400, "Maximum add is ₹5,00,000 at a time.")

    receipt = await save_upload(form.get("receipt"), "receipts")
    try:
        req_id = await cpo.insertion("payment_requests", {
            "user_id": user["id"], "purpose": purpose, "amount": amount,
            "pin_qty": data["pin_qty"] if purpose == "pins" else 0,
            "utr": data["utr"], "receipt_path": receipt, "note": data["note"],
        })
    except asyncpg.UniqueViolationError as e:
        (UPLOAD_ROOT / receipt).unlink(missing_ok=True)
        if e.constraint_name == "payment_requests_one_activation_uq":
            fail(409, "Your activation request is already waiting for approval.")
        fail(409, "This UTR is already used. Please check the number.")
    return respond({"msg": "Request sent. Our team will check it soon.", "status": 201,
                    "data": {"id": req_id, "amount": rupees(amount)}})


@walletApi.get("/wallet/payment-requests", response_model=R.Envelope[R.Page[R.MyPaymentRequest]])
async def my_payment_requests(purpose: Optional[str] = None, cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20,
                              user=Depends(tokenRequired())):
    sql = ("SELECT id, purpose, amount, pin_qty, utr, receipt_path, status, admin_remark, created_at, reviewed_at, id AS _k "
           "FROM payment_requests WHERE user_id = $1")
    args: List[Any] = [user["id"]]
    if purpose in ("activation", "fund", "pins"):
        args.append(purpose)
        sql += " AND purpose = $2"
    data = await cpo.paginate(sql, *args, page=page, limit=limit, cursor=cursor)
    for r in data["rows"]:
        r["amount"] = rupees(r["amount"])
    return respond({"msg": "", "status": 200, "data": data})


@walletApi.get("/admin/payment-requests", response_model=R.Envelope[R.AdminPaymentPage])
async def admin_payment_requests(status: str = "pending", purpose: Optional[str] = None,
                                 search: Optional[str] = Query(None, max_length=40),
                                 cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20, user=Depends(tokenRequired(["ADMIN"]))):
    # reviewed_by_name: the admin who approved / rejected stays on record (Vinay, 14 Oct).
    sql = ("SELECT p.*, u.user_code, u.full_name, u.mobile, u.status AS user_status, "
           "s.user_code AS sponsor_code, s.full_name AS sponsor_name, r.full_name AS reviewed_by_name, p.id AS _k "
           "FROM payment_requests p JOIN users u ON u.id = p.user_id LEFT JOIN users s ON s.id = u.sponsor_id "
           "LEFT JOIN users r ON r.id = p.reviewed_by WHERE p.status = $1")
    args: List[Any] = [status]
    if purpose in ("activation", "fund", "pins"):
        args.append(purpose)
        sql += f" AND p.purpose = ${len(args)}"
    if search:
        args.append(search.strip().upper())
        sql += f" AND (p.utr = ${len(args)} OR u.user_code = ${len(args)} OR u.mobile = ${len(args)})"
    data = await cpo.paginate(sql, *args, desc=False, page=page, limit=limit, cursor=cursor)  # oldest first
    for r in data["rows"]:
        r["amount"] = rupees(r["amount"])
    # AdminFunds tabs: Waiting / Approved / Rejected, and All / Activation / Add funds + PINs.
    data["counts"] = await cpo.fetch_one(
        "SELECT count(*) FILTER (WHERE status = 'pending') AS pending, count(*) FILTER (WHERE status = 'approved') AS approved, "
        "count(*) FILTER (WHERE status = 'rejected') AS rejected, "
        "count(*) FILTER (WHERE status = 'pending' AND purpose = 'activation') AS pending_activation, "
        "count(*) FILTER (WHERE status = 'pending' AND purpose <> 'activation') AS pending_funds_pins FROM payment_requests")
    return respond({"msg": "", "status": 200, "data": data})


async def lock_pending_payment(conn, req_id: int) -> Dict:
    req = await cpo.find_one("payment_requests", {"id": req_id}, conn=conn, for_update=True)
    if not req:
        fail(404, "Request not found.")
    if req["status"] != "pending":
        fail(409, f"This request is already {req['status']}.")
    return req


async def approve_payment(conn, req: Dict, admin_id: int, remark: Optional[str]) -> str:
    """Gives the member what they paid for. Used directly, or by the second admin for big amounts."""
    note = ""
    ref = f"payreq:{req['id']}"
    if req["purpose"] == "activation":
        # Lock first: a PIN activation at the same moment waits, so we never decide on an old status.
        state = await cpo.fetch_val("SELECT status FROM users WHERE id = $1 FOR UPDATE", req["user_id"], conn=conn)
        if state == "inactive":
            await activate_user(conn, req["user_id"], "payment", ref, admin_id)
        elif state != "active":
            fail(409, f"This ID is {state}. Unblock it first, or reject the request.")
        else:
            # Already activated by a PIN meanwhile: keep the money safe in the Fund Wallet.
            await wallet_move(conn, req["user_id"], "fund", req["amount"], "fund_add", ref, {"utr": req["utr"]})
            note = " ID was already active, so the money went to the Fund Wallet."
    elif req["purpose"] == "fund":
        await wallet_move(conn, req["user_id"], "fund", req["amount"], "fund_add", ref, {"utr": req["utr"]})
    else:
        await issue_pins(conn, req["user_id"], req["pin_qty"], "payment")
    await cpo.execute(
        "UPDATE payment_requests SET status = 'approved', admin_remark = $2, reviewed_by = $3, reviewed_at = now() "
        "WHERE id = $1", req["id"], remark, admin_id, conn=conn)
    await audit(conn, admin_id, "payment_approve", str(req["id"]), {"remark": remark})
    return note


@walletApi.post("/admin/payment-requests/{reqId}/review", response_model=R.Envelope[R.NoData], responses=R.WAITING)
async def review_payment_request(reqId: int, body: ReviewModel, admin=Depends(tokenRequired(["ADMIN"]))):
    note = ""
    async with cpo.transaction() as conn:
        req = await lock_pending_payment(conn, reqId)
        if body.action == "approve":
            if await needs_second_admin(req["amount"]):
                approval_id = await ask_second_admin(conn, admin, "payment_approve", str(reqId), req["amount"],
                                                     {"req_id": reqId, "remark": body.remark})
                waiting = waiting_response(approval_id, req["amount"])
            else:
                waiting = None
                note = await approve_payment(conn, req, admin["id"], body.remark)
        else:
            waiting = None
            await cpo.execute(
                "UPDATE payment_requests SET status = 'rejected', admin_remark = $2, reviewed_by = $3, "
                "reviewed_at = now() WHERE id = $1", reqId, body.remark, admin["id"], conn=conn)
            await audit(conn, admin["id"], "payment_reject", str(reqId), {"remark": body.remark})
    if waiting:
        return respond(waiting)
    await forget_auth_user(req["user_id"])
    return respond({"msg": f"Request {body.action}d.{note}", "status": 200})


# ================================================================ withdrawal
async def _withdraw_checks(user_id: int, amount: Optional[int] = None) -> Dict:
    s = await get_settings()
    u = await cpo.fetch_one("SELECT * FROM users WHERE id = $1", user_id)
    if u["status"] != "active":
        fail(403, "Activate your ID first.")
    if not u["pan"] or not u["bank_account"] or not u["bank_ifsc"]:
        fail(400, "Add your PAN and bank details first.")
    if u["bank_updated_at"] and now_utc() - u["bank_updated_at"] < timedelta(hours=24):
        fail(400, "Bank details were changed recently. You can withdraw 24 hours after the change.")
    if await cpo.count("withdrawals", {"user_id": user_id, "status": "pending"}):
        fail(409, "You already have a withdrawal waiting. Please wait for it.")
    if amount is not None:
        if amount < s["min_withdraw"]:
            fail(400, f"Minimum withdrawal is ₹{rupees(s['min_withdraw']):g}.")
        bal = await cpo.fetch_val("SELECT income FROM wallets WHERE user_id = $1", user_id)
        if amount > bal:
            fail(400, "Not enough balance in your Income Wallet.")
    return u


@walletApi.post("/wallet/withdrawal/otp", response_model=R.Envelope[R.MaskedEmail])
async def withdrawal_otp(user=Depends(tokenRequired(activeOnly=True))):
    u = await _withdraw_checks(user["id"])
    await issue_otp("withdraw", u, u["email"])
    return respond({"msg": "OTP sent to your email.", "status": 200, "data": {"email": mask_email(u["email"])}})


@walletApi.post("/wallet/withdrawal", response_model=R.Envelope[R.Created], status_code=201)
async def withdrawal_request(body: WithdrawModel, user=Depends(tokenRequired(activeOnly=True))):
    amount = to_paise(body.amount)
    u = await _withdraw_checks(user["id"], amount)
    await check_otp("withdraw", user["id"], body.otp)
    bank = {"holder": u["bank_holder"], "account": u["bank_account"], "ifsc": u["bank_ifsc"],
            "branch": u["bank_branch"], "pan": u["pan"]}
    try:
        async with cpo.transaction() as conn:
            wd_id = await cpo.insertion("withdrawals", {"user_id": user["id"], "amount": amount, "bank": bank}, conn=conn)
            # Money is held right away, so it cannot be spent twice.
            await wallet_move(conn, user["id"], "income", -amount, "withdrawal", f"wd:{wd_id}", {"withdrawal_id": wd_id})
    except asyncpg.UniqueViolationError:
        fail(409, "You already have a withdrawal waiting. Please wait for it.")
    return respond({"msg": "Withdrawal requested. Our team will pay it to your bank.", "status": 201,
                    "data": {"id": wd_id, "amount": rupees(amount)}})


@walletApi.get("/wallet/withdrawals", response_model=R.Envelope[R.Page[R.MyWithdrawal]])
async def my_withdrawals(cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20,
                        user=Depends(tokenRequired())):
    data = await cpo.paginate(
        "SELECT id, amount, status, utr, admin_remark, bank->>'account' AS account, created_at, reviewed_at, id AS _k "
        "FROM withdrawals WHERE user_id = $1", user["id"], page=page, limit=limit, cursor=cursor)
    for r in data["rows"]:
        r["amount"], r["account"] = rupees(r["amount"]), mask_account(r["account"])
    return respond({"msg": "", "status": 200, "data": data})


@walletApi.get("/admin/withdrawals", response_model=R.Envelope[R.AdminWithdrawalsPage])
async def admin_withdrawals(status: str = "pending", search: Optional[str] = Query(None, max_length=40), cursor: Optional[str] = Query(None, max_length=200), 
                            page: int = 1, limit: int = 20, user=Depends(tokenRequired(["ADMIN"]))):
    # reviewed_by_name: which admin paid / rejected it stays on record (Vinay, 15 Oct).
    sql = ("SELECT w.*, u.user_code, u.full_name, u.mobile, r.full_name AS reviewed_by_name, w.id AS _k "
           "FROM withdrawals w JOIN users u ON u.id = w.user_id LEFT JOIN users r ON r.id = w.reviewed_by "
           "WHERE w.status = $1")
    args: List[Any] = [status]
    if search:
        args.append(search.strip().upper())
        sql += " AND (u.user_code = $2 OR u.mobile = $2 OR w.utr = $2)"
    data = await cpo.paginate(sql, *args, desc=False, page=page, limit=limit, cursor=cursor)  # oldest first
    for r in data["rows"]:
        r["amount"] = rupees(r["amount"])
    c = await cpo.fetch_one(
        "SELECT count(*) FILTER (WHERE status = 'pending') AS pending, "
        "coalesce(sum(amount) FILTER (WHERE status = 'pending'), 0)::bigint AS pending_amount, "
        "count(*) FILTER (WHERE status = 'paid' AND reviewed_at >= $1) AS paid_today, "
        "coalesce(sum(amount) FILTER (WHERE status = 'paid' AND reviewed_at >= $1), 0)::bigint AS paid_today_amount, "
        "count(*) FILTER (WHERE status = 'paid' AND reviewed_at >= $2) AS paid_this_month, "
        "coalesce(sum(amount) FILTER (WHERE status = 'paid' AND reviewed_at >= $2), 0)::bigint AS paid_this_month_amount, "
        "count(*) FILTER (WHERE status = 'rejected' AND reviewed_at >= $2) AS rejected_this_month FROM withdrawals",
        period_start("today"), period_start("month"))
    data["counts"] = {k: rupees(v) if k.endswith("amount") else v for k, v in c.items()}
    return respond({"msg": "", "status": 200, "data": data})


@walletApi.post("/admin/withdrawals/{wdId}/review", response_model=R.Envelope[R.NoData])
async def review_withdrawal(wdId: int, body: WithdrawReviewModel, admin=Depends(tokenRequired(["ADMIN"]))):
    try:
        async with cpo.transaction() as conn:
            wd = await cpo.find_one("withdrawals", {"id": wdId}, conn=conn, for_update=True)
            if not wd:
                fail(404, "Withdrawal not found.")
            if wd["status"] != "pending":
                fail(409, f"This withdrawal is already {wd['status']}.")
            if body.action == "paid":
                await cpo.execute(
                    "UPDATE withdrawals SET status = 'paid', utr = $2, admin_remark = $3, reviewed_by = $4, "
                    "reviewed_at = now() WHERE id = $1", wdId, body.utr, body.remark, admin["id"], conn=conn)
                await cpo.execute("UPDATE wallets SET total_withdrawn = total_withdrawn + $2 WHERE user_id = $1",
                                  wd["user_id"], wd["amount"], conn=conn)
            else:
                await cpo.execute(
                    "UPDATE withdrawals SET status = 'rejected', admin_remark = $2, reviewed_by = $3, "
                    "reviewed_at = now() WHERE id = $1", wdId, body.remark, admin["id"], conn=conn)
                await wallet_move(conn, wd["user_id"], "income", wd["amount"], "withdrawal_refund", f"wdref:{wdId}",
                                  {"withdrawal_id": wdId, "reason": body.remark})
            await audit(conn, admin["id"], f"withdrawal_{body.action}", str(wdId), {"utr": body.utr, "remark": body.remark})
    except asyncpg.UniqueViolationError:
        fail(409, "This UTR is already used for another withdrawal.")
    return respond({"msg": "Marked as paid." if body.action == "paid" else "Rejected. Money is back in the wallet.",
                    "status": 200})


# ================================================================ P2P
async def _p2p_target(user: Dict, to_code: str) -> Dict:
    to = await cpo.fetch_one("SELECT id, user_code, full_name, status FROM users WHERE user_code = $1", to_code)
    if not to or to["status"] != "active":
        fail(404, "Member not found or not active.")
    if to["id"] == user["id"]:
        fail(400, "You cannot send to yourself.")
    # My downline at ANY depth (not only 25 levels), or my own sponsor. (bhaiya, 16 Oct: whole downline.)
    if to["id"] != user["sponsor_id"] and await downline_level(user["id"], to["id"]) is None:
        fail(403, "You can send only to your team or your sponsor.")
    return to


@walletApi.post("/wallet/p2p/otp", response_model=R.Envelope[R.MaskedEmail])
async def p2p_otp(user=Depends(tokenRequired(activeOnly=True))):
    u = await cpo.fetch_one("SELECT id, full_name, email FROM users WHERE id = $1", user["id"])
    await issue_otp("p2p", u, u["email"])
    return respond({"msg": "OTP sent to your email.", "status": 200, "data": {"email": mask_email(u["email"])}})


async def _p2p_sent_today(user_id: int, conn=None) -> int:
    return await cpo.fetch_val(
        "SELECT coalesce(sum(-amount), 0) FROM wallet_ledger WHERE user_id = $1 AND type = 'p2p_out' "
        "AND created_at >= $2", user_id, period_start("today"), conn=conn)


@walletApi.get("/wallet/p2p/limits", response_model=R.Envelope[R.P2PLimits])
async def p2p_limits(user=Depends(tokenRequired(activeOnly=True))):
    """P2P screen: fee, min/max and how much is left today."""
    p2p = (await get_settings())["p2p"]
    sent = await _p2p_sent_today(user["id"])
    return respond({"msg": "", "status": 200, "data": {
        "fee_percent": p2p["fee_percent"], "min": rupees(p2p["min"]), "max": rupees(p2p["max"]),
        "daily_max": rupees(p2p["daily_max"]), "sent_today": rupees(sent),
        "left_today": rupees(max(0, p2p["daily_max"] - sent))}})


@walletApi.post("/wallet/p2p", response_model=R.Envelope[R.P2PSent])
async def p2p_send(body: P2PModel, user=Depends(tokenRequired(activeOnly=True))):
    p2p = (await get_settings())["p2p"]
    amount = to_paise(body.amount)
    if amount < p2p["min"] or amount > p2p["max"]:
        fail(400, f"Send between ₹{rupees(p2p['min']):g} and ₹{rupees(p2p['max']):g}.")
    to = await _p2p_target(user, body.to_code)
    # Early answers BEFORE the OTP is used up (the member keeps the OTP and can fix the amount). The same checks
    # run again inside the transaction below, under the wallet lock; those are the ones that count.
    left = p2p["daily_max"] - await _p2p_sent_today(user["id"])
    if amount > left:
        fail(400, f"Daily limit is ₹{float(rupees(p2p['daily_max'])):g}. You can send ₹{float(rupees(max(0, left))):g} more today.")
    if amount > await cpo.fetch_val("SELECT income FROM wallets WHERE user_id = $1", user["id"]):
        fail(400, "Not enough balance in your Income Wallet.", "Balance")
    await check_otp("p2p", user["id"], body.otp)

    fee = percent_of(amount, p2p["fee_percent"])
    ref = new_ref("p2p")
    async with cpo.transaction() as conn:
        # Lock both wallets (always in id order, so A->B and B->A at once cannot deadlock).
        # Holding my wallet also stops two sends at once from crossing the daily limit.
        await cpo.execute("SELECT 1 FROM wallets WHERE user_id = ANY($1::bigint[]) ORDER BY user_id FOR UPDATE",
                          [user["id"], to["id"]], conn=conn)
        sent_today = await _p2p_sent_today(user["id"], conn)
        if sent_today + amount > p2p["daily_max"]:
            fail(400, f"Daily limit is ₹{float(rupees(p2p['daily_max'])):g}. You can send ₹{float(rupees(max(0, p2p['daily_max'] - sent_today))):g} more today.")
        await wallet_move(conn, user["id"], "income", -amount, "p2p_out", f"{ref}:out",
                          {"to": to["user_code"], "to_name": to["full_name"], "fee": fee})
        await wallet_move(conn, to["id"], "income", amount - fee, "p2p_in", f"{ref}:in",
                          {"from": user["user_code"], "from_name": user["full_name"]})
        await company_add(conn, "p2p_fee", fee, f"{ref}:fee", user["id"])
    return respond({"msg": f"Sent. {to['full_name']} got ₹{rupees(amount - fee):g}.", "status": 200,
                    "data": {"sent": rupees(amount), "fee": rupees(fee), "received": rupees(amount - fee)}})


# ================================================================ admin debit (hidden from member)
async def do_admin_debit(conn, user_code: str, wallet: str, amount: int, comment: str, admin_id: int,
                         requested_by: Optional[int] = None) -> int:
    """Hidden debit (the member never sees it). Returns the new balance."""
    target = await cpo.fetch_one("SELECT id FROM users WHERE user_code = $1", user_code, conn=conn)
    if not target:
        fail(404, "Member not found.")
    ref = new_ref("admindebit")
    by = {"admin_id": requested_by or admin_id, "approved_by": admin_id if requested_by else None}
    bal = await wallet_move(conn, target["id"], wallet, -amount, "admin_debit", ref, {"comment": comment, **by},
                            visible=False)
    await company_add(conn, "admin_debit", amount, ref, target["id"], {"wallet": wallet, "comment": comment})
    await audit(conn, admin_id, "wallet_debit", user_code, {"wallet": wallet, "amount": amount, "comment": comment, **by})
    return bal


@walletApi.post("/admin/wallet/debit", response_model=R.Envelope[R.BalanceAfter], responses=R.WAITING)
async def admin_debit(body: AdminDebitModel, admin=Depends(tokenRequired(["ADMIN"]))):
    amount = to_paise(body.amount)
    async with cpo.transaction() as conn:
        if await needs_second_admin(amount):
            if not await cpo.fetch_val("SELECT 1 FROM users WHERE user_code = $1", body.user_code, conn=conn):
                fail(404, "Member not found.")
            approval_id = await ask_second_admin(conn, admin, "wallet_debit", f"{body.user_code}:{body.wallet}", amount,
                                                 {"user_code": body.user_code, "wallet": body.wallet, "comment": body.comment})
            return respond(waiting_response(approval_id, amount))
        bal = await do_admin_debit(conn, body.user_code, body.wallet, amount, body.comment, admin["id"])
    return respond({"msg": "Amount taken from the wallet.", "status": 200, "data": {"balance_after": rupees(bal)}})
