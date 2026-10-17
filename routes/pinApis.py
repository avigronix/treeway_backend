from adminApproval import ask_second_admin, needs_second_admin, waiting_response
from base import *
from models import responses as R
from mlmEngine import activate_user, issue_pins, wallet_move
from models.model import AdminPinIssueModel, PinActivateModel, PinBuyModel, PinTransferModel

pinApi = APIRouter()


@pinApi.get("/pins", response_model=R.Envelope[R.MyPinsPage])
async def my_pins(tab: str = Query("ready", pattern="^(ready|received|sent|used)$"), cursor: Optional[str] = Query(None, max_length=200), page: int = 1,
                  limit: int = 20, user=Depends(tokenRequired())):
    """Ready = mine to use, Received = sent to me, Sent = I gave away, Used = IDs activated with my PINs."""
    if tab == "ready":
        sql = ("SELECT p.code, p.source, p.created_at, p.id AS _k FROM pins p "
               "WHERE p.owner_id = $1 AND p.status = 'ready'")
        sort, desc = "_k", False   # oldest PIN first (used first)
    elif tab == "received":
        sql = ("SELECT p.code, p.status, f.user_code AS from_code, f.full_name AS from_name, t.created_at, t.id AS _k "
               "FROM pin_transfers t JOIN pins p ON p.id = t.pin_id JOIN users f ON f.id = t.from_user "
               "WHERE t.to_user = $1")
        sort, desc = "_k", True
    elif tab == "sent":
        sql = ("SELECT p.code, p.status, r.user_code AS to_code, r.full_name AS to_name, t.created_at, t.id AS _k "
               "FROM pin_transfers t JOIN pins p ON p.id = t.pin_id JOIN users r ON r.id = t.to_user "
               "WHERE t.from_user = $1")
        sort, desc = "_k", True
    else:
        sql = ("SELECT p.code, a.user_code AS activated_code, a.full_name AS activated_name, p.used_at, "
               "p.used_at AS _s, p.id AS _k FROM pins p JOIN users a ON a.id = p.used_for WHERE p.used_by = $1")
        sort, desc = "_s", True
    data = await cpo.paginate(sql, user["id"], sort=sort, desc=desc, page=page, limit=limit, cursor=cursor)
    data["counts"] = await cpo.fetch_one(
        "SELECT (SELECT count(*) FROM pins WHERE owner_id = $1 AND status = 'ready') AS ready, "
        "(SELECT count(*) FROM pin_transfers WHERE to_user = $1) AS received, "
        "(SELECT count(*) FROM pin_transfers WHERE from_user = $1) AS sent, "
        "(SELECT count(*) FROM pins WHERE used_by = $1) AS used", user["id"])
    return respond({"msg": "", "status": 200, "data": data})


@pinApi.post("/pins/activate", response_model=R.Envelope[R.PinActivated])
async def activate_with_pin(body: PinActivateModel, user=Depends(tokenRequired())):
    """Use one of my PINs to activate my own ID or any inactive ID in my team."""
    async with cpo.transaction() as conn:
        target = await cpo.fetch_one("SELECT id, status, full_name FROM users WHERE user_code = $1",
                                     body.user_code, conn=conn)
        if not target:
            fail(404, "Member not found.")
        # My own ID, or my team at ANY depth (bhaiya, 16 Oct: whole downline).
        if target["id"] != user["id"] and await downline_level(user["id"], target["id"], conn) is None:
            fail(403, "You can activate only your own ID or an ID in your team.")
        if target["status"] != "inactive":
            fail(409, "This ID is already active." if target["status"] == "active" else "This ID cannot be activated.")
        # SKIP LOCKED: two taps at once take two different PINs, never the same one.
        pin = await cpo.fetch_one(
            "SELECT id, code FROM pins WHERE owner_id = $1 AND status = 'ready' ORDER BY id LIMIT 1 "
            "FOR UPDATE SKIP LOCKED", user["id"], conn=conn)
        if not pin:
            fail(400, "You have no ready PIN. Buy PINs first.")
        await cpo.execute("UPDATE pins SET status = 'used', used_for = $2, used_by = $3, used_at = now() WHERE id = $1",
                          pin["id"], target["id"], user["id"], conn=conn)
        await activate_user(conn, target["id"], "pin", f"pin:{pin['code']}", user["id"])
    await forget_auth_user(target["id"])
    return respond({"msg": f"{target['full_name']}'s ID is active now.", "status": 200, "data": {"pin": pin["code"]}})


@pinApi.post("/pins/transfer", response_model=R.Envelope[R.NoData])
async def transfer_pins(body: PinTransferModel, user=Depends(tokenRequired(activeOnly=True))):
    async with cpo.transaction() as conn:
        to = await cpo.fetch_one("SELECT id, full_name, status FROM users WHERE user_code = $1", body.to_code, conn=conn)
        if not to or to["status"] in ("blocked", "removed"):
            fail(404, "Member not found.")
        if to["id"] == user["id"]:
            fail(400, "You cannot send PINs to yourself.")
        pins = await cpo.fetch_all(
            "SELECT id FROM pins WHERE owner_id = $1 AND status = 'ready' ORDER BY id LIMIT $2 FOR UPDATE SKIP LOCKED",
            user["id"], body.qty, conn=conn)
        if len(pins) < body.qty:
            fail(400, f"You have only {len(pins)} ready PINs.")
        ids = [p["id"] for p in pins]
        await cpo.execute("UPDATE pins SET owner_id = $2 WHERE id = ANY($1::bigint[])", ids, to["id"], conn=conn)
        await cpo.execute(
            "INSERT INTO pin_transfers (pin_id, from_user, to_user) SELECT unnest($1::bigint[]), $2, $3",
            ids, user["id"], to["id"], conn=conn)
    return respond({"msg": f"{body.qty} PIN(s) sent to {to['full_name']}.", "status": 200})


@pinApi.post("/pins/buy-from-fund", response_model=R.Envelope[R.PinsBought])
async def buy_pins_from_fund(body: PinBuyModel, user=Depends(tokenRequired(activeOnly=True))):
    s = await get_settings()
    cost = s["activation_amount"] * body.qty
    async with cpo.transaction() as conn:
        ref = new_ref("pinbuy")
        await wallet_move(conn, user["id"], "fund", -cost, "pin_buy", ref, {"qty": body.qty})
        codes = await issue_pins(conn, user["id"], body.qty, "fund_wallet")
    return respond({"msg": f"{body.qty} PIN(s) added.", "status": 200, "data": {"codes": codes, "paid": rupees(cost)}})


# ================================================================ admin
@pinApi.get("/admin/pins", response_model=R.Envelope[R.AdminPinsPage])
async def admin_pins(status: Optional[str] = None, search: Optional[str] = Query(None, max_length=40),
                     cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20, user=Depends(tokenRequired(["ADMIN"]))):
    sql = ("SELECT p.code, p.status, p.source, p.created_at, p.used_at, o.user_code AS owner_code, "
           "o.full_name AS owner_name, a.user_code AS used_for_code, b.user_code AS bought_by_code, b.full_name AS bought_by_name, "
           "t.last_sent_at, p.id AS _k FROM pins p JOIN users o ON o.id = p.owner_id JOIN users b ON b.id = p.bought_by "
           "LEFT JOIN users a ON a.id = p.used_for "
           "LEFT JOIN LATERAL (SELECT max(created_at) AS last_sent_at FROM pin_transfers WHERE pin_id = p.id) t ON TRUE "
           "WHERE TRUE")
    args: List[Any] = []
    if status in ("ready", "used"):
        args.append(status)
        sql += f" AND p.status = ${len(args)}"
    elif status == "sent":   # AdminPins "Sent" tab: given by one member to another at least once
        sql += " AND t.last_sent_at IS NOT NULL"
    if search:
        args.append(search.strip().upper())
        sql += f" AND (p.code = ${len(args)} OR o.user_code = ${len(args)} OR a.user_code = ${len(args)})"
    data = await cpo.paginate(sql, *args, page=page, limit=limit, cursor=cursor)
    c = await cpo.fetch_one(
        "SELECT count(*) FILTER (WHERE status = 'ready') AS ready, count(*) FILTER (WHERE status = 'used') AS used, "
        "count(*) AS total, count(*) FILTER (WHERE source IN ('payment', 'fund_wallet')) AS sold, "
        "count(DISTINCT owner_id) FILTER (WHERE status = 'ready') AS members_holding, "
        "(SELECT count(*) FROM pin_transfers) AS sent_between_members FROM pins")
    c["sold_amount"] = rupees(c["sold"] * (await get_settings())["activation_amount"])
    data["counts"] = c
    return respond({"msg": "", "status": 200, "data": data})


async def do_issue_pins(conn, user_code: str, qty: int, remark: str, admin_id: int,
                        requested_by: Optional[int] = None) -> List[str]:
    to = await cpo.fetch_one("SELECT id FROM users WHERE user_code = $1 AND status <> 'removed'", user_code, conn=conn)
    if not to:
        fail(404, "Member not found.")
    codes = await issue_pins(conn, to["id"], qty, "admin")
    await audit(conn, admin_id, "pins_issue", user_code,
                {"qty": qty, "remark": remark, "requested_by": requested_by or admin_id})
    return codes


@pinApi.post("/admin/pins/issue", response_model=R.Envelope[R.PinCodes], responses=R.WAITING)
async def admin_issue_pins(body: AdminPinIssueModel, admin=Depends(tokenRequired(["ADMIN"]))):
    """For cash payments taken in person. Every issue is saved in the audit log."""
    value = (await get_settings())["activation_amount"] * body.qty
    async with cpo.transaction() as conn:
        if await needs_second_admin(value):
            if not await cpo.fetch_val("SELECT 1 FROM users WHERE user_code = $1 AND status <> 'removed'",
                                       body.user_code, conn=conn):
                fail(404, "Member not found.")
            approval_id = await ask_second_admin(conn, admin, "pins_issue", body.user_code, value,
                                                 {"user_code": body.user_code, "qty": body.qty, "remark": body.remark})
            return respond(waiting_response(approval_id, value))
        codes = await do_issue_pins(conn, body.user_code, body.qty, body.remark, admin["id"])
    return respond({"msg": f"{body.qty} PIN(s) issued.", "status": 200, "data": {"codes": codes}})
