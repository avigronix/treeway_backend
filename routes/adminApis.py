import asyncpg

from adminApproval import lock_pending
from base import *
from models import responses as R
from models.model import AdminCreateModel, MemberStatusModel, ReviewModel
from routes.pinApis import do_issue_pins
from routes.reverseApis import do_reverse
from routes.walletApis import approve_payment, do_admin_debit, lock_pending_payment

adminApi = APIRouter()

MAIN_ADMIN_CODE = "TW1000000"


# ================================================================ 2-admin approvals
@adminApi.get("/admin/approvals", response_model=R.Envelope[R.Page[R.Approval]])
async def list_approvals(status: str = Query("pending", pattern="^(pending|approved|rejected|cancelled)$"),
                         cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20, admin=Depends(tokenRequired(["ADMIN"]))):
    data = await cpo.paginate(
        "SELECT a.*, r.full_name AS requested_by_name, d.full_name AS decided_by_name, "
        "a.requested_by <> $2 AS i_can_decide, a.id AS _k "
        "FROM admin_approvals a JOIN users r ON r.id = a.requested_by LEFT JOIN users d ON d.id = a.decided_by "
        "WHERE a.status = $1", status, admin["id"], page=page, limit=limit, cursor=cursor)
    for r in data["rows"]:
        r["amount"] = rupees(r["amount"])
    return respond({"msg": "", "status": 200, "data": data})


async def _run(conn, ap: Dict, admin: Dict) -> tuple[str, Dict]:
    """Runs the waiting action exactly as the first admin asked for it."""
    p, by = ap["payload"], ap["requested_by"]
    if ap["action"] == "payment_approve":
        req = await lock_pending_payment(conn, p["req_id"])
        note = await approve_payment(conn, req, admin["id"], p.get("remark"))
        return f"Request approved.{note}", {"user_id": req["user_id"]}
    if ap["action"] == "wallet_debit":
        bal = await do_admin_debit(conn, p["user_code"], p["wallet"], ap["amount"], p["comment"], admin["id"], by)
        return "Amount taken from the wallet.", {"balance_after": rupees(bal)}
    if ap["action"] == "pins_issue":
        codes = await do_issue_pins(conn, p["user_code"], p["qty"], p["remark"], admin["id"], by)
        return f"{p['qty']} PIN(s) issued.", {"codes": codes}
    if ap["action"] == "reverse_entry":
        return "Reverse entry done.", await do_reverse(conn, p["ledger_id"], p["reason"], admin["id"], by)
    raise RuntimeError(f"Unknown approval action {ap['action']}")


@adminApi.post("/admin/approvals/{approvalId}/decide", response_model=R.Envelope[Dict[str, Any]])
async def decide_approval(approvalId: int, body: ReviewModel, admin=Depends(tokenRequired(["ADMIN"]))):
    async with cpo.transaction() as conn:
        ap = await lock_pending(conn, approvalId)
        if ap["requested_by"] == admin["id"]:
            fail(403, "A different admin must approve or reject this.")
        data: Dict = {}
        if body.action == "approve":
            msg, data = await _run(conn, ap, admin)
        else:
            msg = "Rejected. Nothing was done."
        await cpo.execute(
            "UPDATE admin_approvals SET status = $2, decided_by = $3, remark = $4, decided_at = now() WHERE id = $1",
            approvalId, "approved" if body.action == "approve" else "rejected", admin["id"], body.remark, conn=conn)
        await audit(conn, admin["id"], f"approval_{body.action}", str(approvalId),
                    {"action": ap["action"], "target": ap["target"], "remark": body.remark})
    if data.get("user_id"):
        await forget_auth_user(data.pop("user_id"))
    return respond({"msg": msg, "status": 200, "data": data})


# ================================================================ admins
@adminApi.get("/admin/admins", response_model=R.Envelope[List[R.AdminAccount]])
async def list_admins(admin=Depends(tokenRequired(["ADMIN"]))):
    rows = await cpo.fetch_all("SELECT user_code, full_name, mobile, email, status, created_at FROM users "
                               "WHERE role = 'ADMIN' ORDER BY id")
    return respond({"msg": "", "status": 200, "data": rows})


@adminApi.post("/admin/admins", response_model=R.Envelope[R.UserCodeOnly], status_code=201)
async def create_admin(body: AdminCreateModel, admin=Depends(tokenRequired(["ADMIN"]))):
    """Only the main admin can add admins. A second admin is needed for big-amount approvals."""
    if admin["user_code"] != MAIN_ADMIN_CODE:
        fail(403, "Only the main admin can add admins.")
    pw_hash = await hash_password_async(body.password)
    for _ in range(5):
        code = new_user_code()
        try:
            async with cpo.transaction() as conn:
                uid = await cpo.insertion("users", {
                    "user_code": code, "full_name": body.full_name, "mobile": body.mobile, "email": body.email,
                    "password_hash": pw_hash, "role": "ADMIN", "status": "active", "activated_at": now_utc()}, conn=conn)
                await cpo.insertion("wallets", {"user_id": uid}, conn=conn, returning=None)
                await audit(conn, admin["id"], "admin_create", code, {"name": body.full_name, "mobile": body.mobile})
            break
        except asyncpg.UniqueViolationError as e:
            if e.constraint_name == "users_mobile_uq":
                fail(409, "This mobile number is already registered.")
            if e.constraint_name != "users_user_code_key":
                raise
    else:
        fail(500, "Could not create the admin. Please try again.")
    return respond({"msg": "Admin added.", "status": 201, "data": {"user_code": code}})


@adminApi.post("/admin/admins/{adminCode}/status", response_model=R.Envelope[R.NoData])
async def admin_account_status(adminCode: str, body: MemberStatusModel, admin=Depends(tokenRequired(["ADMIN"]))):
    """Block / unblock another admin. Only the main admin may (like adding admins); the main admin can never be
    blocked. Blocking ends every session of that admin at once (token_version). Audited with the reason."""
    if admin["user_code"] != MAIN_ADMIN_CODE:
        fail(403, "Only the main admin can block or unblock admins.")
    code = adminCode.strip().upper()
    if code == MAIN_ADMIN_CODE:
        fail(400, "The main admin cannot be blocked.")
    async with cpo.transaction() as conn:
        u = await cpo.fetch_one("SELECT id, status, role FROM users WHERE user_code = $1 FOR UPDATE", code, conn=conn)
        if not u or u["role"] != "ADMIN":
            fail(404, "Admin not found.")
        if body.action == "block" and u["status"] == "blocked":
            fail(400, "This admin is already blocked.")
        if body.action == "unblock" and u["status"] != "blocked":
            fail(400, "This admin is not blocked.")
        await cpo.execute("UPDATE users SET status = $2, token_version = token_version + 1 WHERE id = $1",
                          u["id"], "blocked" if body.action == "block" else "active", conn=conn)
        await audit(conn, admin["id"], f"admin_{body.action}", code, {"remark": body.remark})
    await forget_auth_user(u["id"])
    return respond({"msg": f"Admin {body.action}ed.", "status": 200})
