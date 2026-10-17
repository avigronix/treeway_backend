"""2-admin approval for big amounts (switched OFF since 15 Oct 2026, bhaiya's decision).

When `two_admin_threshold` is more than 0, an admin action of that amount or more does not run right away. It is
saved in `admin_approvals` and runs only when a DIFFERENT admin approves it (routes/adminApis.py).
0 (the default) or null = OFF: every admin action runs at once, whatever the amount. Switching it OFF cancels the
waiting approvals (`cancel_pending`), so nothing stays stuck; the request itself is untouched and can be done again.
"""

from typing import Any, Dict, Optional

import asyncpg

from base import audit, cpo, fail, get_settings, rupees


async def needs_second_admin(amount: int) -> bool:
    threshold = (await get_settings()).get("two_admin_threshold") or 0
    return int(threshold) > 0 and amount >= int(threshold)


CANCEL_REMARK = "2-admin approval was switched off. Nothing was done; do it again from its own screen."


async def cancel_pending(conn, admin_id: int) -> int:
    """Cancels every waiting approval (when the feature is switched off). Audited. Returns how many."""
    rows = await cpo.fetch_all(
        "UPDATE admin_approvals SET status = 'cancelled', remark = $1, decided_at = now() "
        "WHERE status = 'pending' RETURNING id, action, target, amount", CANCEL_REMARK, conn=conn)
    for r in rows:
        await audit(conn, admin_id, "approval_cancelled", str(r["id"]),
                    {"action": r["action"], "target": r["target"], "amount": r["amount"], "reason": CANCEL_REMARK})
    return len(rows)


async def ask_second_admin(conn, admin: Dict, action: str, target: str, amount: int, payload: Dict[str, Any]) -> int:
    """Saves the action as waiting. Returns the approval id."""
    try:
        async with conn.transaction():  # savepoint, so a duplicate does not break the outer transaction
            approval_id = await cpo.insertion("admin_approvals", {
                "action": action, "target": target, "amount": amount, "payload": payload,
                "requested_by": admin["id"]}, conn=conn)
    except asyncpg.UniqueViolationError:
        fail(409, "This is already waiting for a second admin.")
    await audit(conn, admin["id"], f"{action}_requested", target, {"approval_id": approval_id, "amount": amount, **payload})
    return approval_id


def waiting_response(approval_id: int, amount: int) -> Dict[str, Any]:
    return {"msg": f"₹{rupees(amount):,.2f} needs a second admin. Sent for approval.", "status": 202,
            "data": {"approval_id": approval_id, "waiting_for_second_admin": True}}


async def lock_pending(conn, approval_id: int) -> Optional[Dict]:
    row = await cpo.find_one("admin_approvals", {"id": approval_id}, conn=conn, for_update=True)
    if not row:
        fail(404, "Approval not found.")
    if row["status"] != "pending":
        fail(409, f"This is already {row['status']}.")
    return row
