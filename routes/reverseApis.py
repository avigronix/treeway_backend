"""Reverse entry for admin mistakes. Nothing is ever deleted: an opposite row is added.

Vinay, 14 Oct 2026:
- reversing an income also reverses its TDS and service rows,
- its Single Leg pool row is cancelled if not shared yet; if already shared, the company absorbs it,
- not enough balance in the wallet -> the whole reverse fails (never partial).
"""

from adminApproval import ask_second_admin, needs_second_admin, waiting_response
from base import *
from models import responses as R
from mlmEngine import company_add, wallet_move
from models.model import ReverseModel

reverseApi = APIRouter()

INCOME_TYPES = {"level_income", "direct_bonus"}
REVERSIBLE = INCOME_TYPES | {"fund_add", "admin_debit"}
NOT_REVERSIBLE_MSG = {
    "super3_income": "Super 3 income cannot be reversed (the board moves and recycle IDs stay).",
    "p2p_out": "P2P cannot be reversed here.", "p2p_in": "P2P cannot be reversed here.",
    "withdrawal": "Reject the withdrawal instead.", "withdrawal_refund": "Refunds cannot be reversed.",
    "reversal": "A reverse entry cannot be reversed.",
}


async def _lock_entry(conn, ledger_id: int) -> Dict:
    row = await cpo.fetch_one("SELECT * FROM wallet_ledger WHERE id = $1 FOR UPDATE", ledger_id, conn=conn)
    if not row:
        fail(404, "Entry not found.")
    if row["type"] not in REVERSIBLE:
        fail(400, NOT_REVERSIBLE_MSG.get(row["type"], "This entry cannot be reversed."))
    if await cpo.fetch_val("SELECT 1 FROM wallet_ledger WHERE ref = $1", f"rev:{ledger_id}", conn=conn):
        fail(409, "This entry is already reversed.")
    return row


async def do_reverse(conn, ledger_id: int, reason: str, admin_id: int, requested_by: Optional[int] = None) -> Dict:
    row = await _lock_entry(conn, ledger_id)
    by = {"admin_id": requested_by or admin_id, "approved_by": admin_id if requested_by else None}
    meta = {"reverses": ledger_id, "of_type": row["type"], "reason": reason, **by}
    # Opposite amount in the same wallet. If the balance is too small, CHECK (>= 0) fails and nothing is saved.
    bal = await wallet_move(conn, row["user_id"], row["wallet"], -row["amount"], "reversal", f"rev:{ledger_id}", meta,
                            visible=row["visible_to_user"])
    ref = row["ref"]
    if row["type"] in INCOME_TYPES:
        await cpo.execute("UPDATE wallets SET total_earned = total_earned - $2 WHERE user_id = $1",
                          row["user_id"], row["amount"], conn=conn)
        await company_add(conn, "tds", -row["tds"], f"rev:tds:{ref}", row["user_id"], meta)
        await company_add(conn, "service", -row["service"], f"rev:svc:{ref}", row["user_id"], meta)
        cancelled = await cpo.fetch_val(
            "UPDATE single_leg_pool SET cancelled_at = now() WHERE ref = $1 AND distributed_at IS NULL "
            "AND cancelled_at IS NULL AND run_id IS NULL RETURNING amount", f"sl:{ref}", conn=conn) or 0
        # Already taken by a night run (even if not finished): it gets shared, so the company absorbs it.
        # Company gets back: the member's net + TDS no longer owed + service + an unshared Single Leg part.
        back = row["amount"] + row["tds"] + row["service"] + cancelled
        await company_add(conn, "reversal", back, f"rev:{ref}", row["user_id"], {**meta, "single_leg_cancelled": cancelled})
    elif row["type"] == "admin_debit":
        await company_add(conn, "admin_debit", row["amount"], f"rev:{ref}", row["user_id"], meta)  # amount is negative
    await audit(conn, admin_id, "reverse_entry", str(ledger_id),
                {"user_id": row["user_id"], "type": row["type"], "amount": -row["amount"], "reason": reason, **by})
    return {"ledger_id": ledger_id, "reversed": rupees(-row["amount"]), "balance_after": rupees(bal)}


@reverseApi.post("/admin/reverse", response_model=R.Envelope[R.Reversed], responses=R.WAITING)
async def reverse_entry(body: ReverseModel, admin=Depends(tokenRequired(["ADMIN"]))):
    async with cpo.transaction() as conn:
        row = await _lock_entry(conn, body.ledger_id)
        amount = abs(row["amount"])
        if await needs_second_admin(amount):
            approval_id = await ask_second_admin(conn, admin, "reverse_entry", str(body.ledger_id), amount,
                                                 {"ledger_id": body.ledger_id, "reason": body.reason})
            return respond(waiting_response(approval_id, amount))
        data = await do_reverse(conn, body.ledger_id, body.reason, admin["id"])
    await forget_auth_user(row["user_id"])
    return respond({"msg": "Reverse entry done.", "status": 200, "data": data})
