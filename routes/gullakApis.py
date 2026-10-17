"""Gullak savings (SRS 6; Vinay, 15 Oct 2026). Built, switched OFF by the setting gullak.enabled.

- Open once: ₹100 from the Income Wallet (company income).
- Deposit any amount from the Income Wallet; 3% is taken inside it (₹1,000 taken -> ₹970 in the Gullak).
- Interest: simple, per deposit, amount x rate x days / 365 (paise rounded down), only if that deposit is
  withdrawn on or after its 6-month date; earlier = no interest. Rate and 6-month date are fixed per deposit
  on the day it is made, so later setting changes apply only to new deposits.
- Withdraw one deposit in full: money + interest to the Income Wallet. Interest has only TDS (by PAN), at the rate
  in the setting gullak.interest_tds_percent (2% now; the CA confirms it).
- When the Gullak is switched off, opening and depositing stop, but members can always withdraw.

Money held in Gullaks is members' money: company_ledger 'gullak_held' (kept out of profit).
"""

from datetime import date

from base import *
from mlmEngine import company_add, wallet_move
from models import responses as R
from models.model import GullakDepositModel

gullakApi = APIRouter()


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def interest_on(dep: Dict, on: date) -> tuple:
    """(gross interest, days) if the deposit were withdrawn on `on` (IST date)."""
    days = (on - dep["created_at"].astimezone(IST).date()).days
    if on < dep["interest_from"] or days <= 0:
        return 0, max(days, 0)
    gross = int(Decimal(dep["amount"]) * Decimal(dep["interest_percent"]) * days / Decimal(36500))  # rounds down
    return gross, days


async def _settings() -> Dict:
    return (await get_settings())["gullak"]


async def _must_be_on():
    if not (await _settings())["enabled"]:
        fail(403, "Gullak savings are coming soon.", "Coming soon")


def _deposit_out(d: Dict, today: date) -> Dict:
    gross, days = interest_on(d, today) if d["status"] == "active" else (d["interest"] or 0, None)
    return {"id": d["id"], "amount": rupees(d["amount"]), "taken": rupees(d["taken"]), "charge": rupees(d["charge"]),
            "interest_percent": float(d["interest_percent"]), "interest_from": d["interest_from"], "status": d["status"],
            "created_at": d["created_at"], "withdrawn_at": d["withdrawn_at"],
            "interest_if_withdrawn_today": rupees(gross) if d["status"] == "active" else None,
            "days": days, "interest_paid": rupees(d["interest"]) if d["interest"] is not None else None,
            "interest_tds": rupees(d["interest_tds"]) if d["interest_tds"] is not None else None}


@gullakApi.get("/gullak", response_model=R.Envelope[R.GullakView])
async def my_gullak(user=Depends(tokenRequired())):
    s = await _settings()
    acc = await cpo.fetch_one("SELECT * FROM gullak_accounts WHERE user_id = $1", user["id"])
    deps = await cpo.fetch_all("SELECT * FROM gullak_deposits WHERE user_id = $1 ORDER BY id DESC", user["id"]) if acc else []
    today = today_ist()
    active = [d for d in deps if d["status"] == "active"]
    return respond({"msg": "", "status": 200, "data": {
        "enabled": s["enabled"], "opened_at": acc["opened_at"] if acc else None,
        "opening_charge": rupees(s["opening_charge"]), "deposit_charge_percent": s["deposit_charge_percent"],
        "interest_percent": s["interest_percent"], "min_months": s["min_months"],
        "balance": rupees(sum(d["amount"] for d in active)),
        "interest_if_all_withdrawn_today": rupees(sum(interest_on(d, today)[0] for d in active)),
        "deposits": [_deposit_out(d, today) for d in deps]}})


@gullakApi.post("/gullak/open", response_model=R.Envelope[R.NoData], status_code=201)
async def open_gullak(user=Depends(tokenRequired(activeOnly=True))):
    await _must_be_on()
    s = await _settings()
    async with cpo.transaction() as conn:
        if await cpo.fetch_val("SELECT 1 FROM gullak_accounts WHERE user_id = $1", user["id"], conn=conn):
            fail(409, "Your Gullak is already open.")
        await cpo.insertion("gullak_accounts", {"user_id": user["id"], "opening_charge": s["opening_charge"]},
                            conn=conn, returning=None)
        ref = f"gullak:open:{user['id']}"
        await wallet_move(conn, user["id"], "income", -s["opening_charge"], "gullak_open", ref, {})
        await company_add(conn, "gullak_charge", s["opening_charge"], ref, user["id"], {"kind": "opening"})
    return respond({"msg": "Gullak opened.", "status": 201})


@gullakApi.post("/gullak/deposits", response_model=R.Envelope[R.GullakDeposited], status_code=201)
async def deposit(body: GullakDepositModel, user=Depends(tokenRequired(activeOnly=True))):
    await _must_be_on()
    s = await _settings()
    taken = to_paise(body.amount)
    if taken < s["min_deposit"]:
        fail(400, f"Put at least ₹{rupees(s['min_deposit']):g}.")
    charge = percent_of(taken, s["deposit_charge_percent"])
    async with cpo.transaction() as conn:
        if not await cpo.fetch_val("SELECT 1 FROM gullak_accounts WHERE user_id = $1", user["id"], conn=conn):
            fail(409, "Open your Gullak first.")
        did = await cpo.insertion("gullak_deposits", {
            "user_id": user["id"], "taken": taken, "charge": charge, "amount": taken - charge,
            "interest_percent": Decimal(str(s["interest_percent"])),
            "interest_from": add_months(today_ist(), s["min_months"])}, conn=conn)
        ref = f"gullak:dep:{did}"
        await wallet_move(conn, user["id"], "income", -taken, "gullak_deposit", ref, {"deposit_id": did, "charge": charge})
        await company_add(conn, "gullak_charge", charge, ref, user["id"], {"kind": "deposit", "deposit_id": did})
        await company_add(conn, "gullak_held", taken - charge, f"gullak:held:{did}", user["id"], {"deposit_id": did})
    return respond({"msg": "Added to your Gullak.", "status": 201, "data": {
        "id": did, "taken": rupees(taken), "charge": rupees(charge), "amount": rupees(taken - charge),
        "interest_from": add_months(today_ist(), s["min_months"])}})


@gullakApi.post("/gullak/deposits/{depositId}/withdraw", response_model=R.Envelope[R.GullakWithdrawn])
async def withdraw(depositId: int, user=Depends(tokenRequired())):
    """Always allowed, even when the Gullak is switched off: it is the member's money."""
    pct = (await _settings())["interest_tds_percent"]
    today = today_ist()
    async with cpo.transaction() as conn:
        d = await cpo.fetch_one("SELECT * FROM gullak_deposits WHERE id = $1 FOR UPDATE", depositId, conn=conn)
        if not d or d["user_id"] != user["id"]:
            fail(404, "Deposit not found.")
        if d["status"] != "active":
            fail(409, "This deposit is already withdrawn.")
        gross, days = interest_on(d, today)
        tds = percent_of(gross, pct)
        meta = {"deposit_id": depositId, "days": days, "interest_percent": float(d["interest_percent"])}
        await wallet_move(conn, user["id"], "income", d["amount"], "gullak_withdraw", f"gullak:wd:{depositId}", meta)
        await company_add(conn, "gullak_held", -d["amount"], f"gullak:unheld:{depositId}", user["id"], meta)
        if gross:
            ref = f"gullak:int:{depositId}"
            await wallet_move(conn, user["id"], "income", gross - tds, "gullak_interest", ref, meta,
                              cuts={"gross": gross, "tds": tds}, count_as_earned=True)
            await company_add(conn, "gullak_interest", gross, ref, user["id"], meta)   # the company's cost
            await company_add(conn, "tds", tds, f"tds:{ref}", user["id"], meta)
        await cpo.execute("UPDATE gullak_deposits SET status = 'withdrawn', withdrawn_at = now(), interest = $2, "
                          "interest_tds = $3 WHERE id = $1", depositId, gross, tds, conn=conn)
    msg = "Money is back in your Income Wallet." + (
        "" if gross else f" No interest: it was withdrawn before {d['interest_from']:%d %b %Y}.")
    return respond({"msg": msg, "status": 200, "data": {
        "amount": rupees(d["amount"]), "interest": rupees(gross), "tds": rupees(tds),
        "to_income_wallet": rupees(d["amount"] + gross - tds), "days": days}})


@gullakApi.get("/admin/gullak", response_model=R.Envelope[R.AdminGullak])
async def admin_gullak(admin=Depends(tokenRequired(["ADMIN"]))):
    s = await _settings()
    row = await cpo.fetch_one(
        "SELECT (SELECT count(*) FROM gullak_accounts) AS accounts, "
        "(SELECT count(*) FROM gullak_deposits WHERE status = 'active') AS active_deposits, "
        "(SELECT coalesce(sum(amount), 0)::bigint FROM gullak_deposits WHERE status = 'active') AS held, "
        "(SELECT coalesce(sum(interest), 0)::bigint FROM gullak_deposits WHERE status = 'withdrawn') AS interest_paid, "
        "(SELECT coalesce(sum(amount), 0)::bigint FROM company_ledger WHERE type = 'gullak_charge') AS charges")
    today = today_ist()
    owed = sum(interest_on(d, today)[0] for d in await cpo.fetch_all(
        "SELECT amount, interest_percent, interest_from, created_at FROM gullak_deposits WHERE status = 'active'"))
    return respond({"msg": "", "status": 200, "data": {
        "enabled": s["enabled"], "accounts": row["accounts"], "active_deposits": row["active_deposits"],
        "held": rupees(row["held"]), "interest_paid": rupees(row["interest_paid"]), "charges": rupees(row["charges"]),
        "interest_if_all_withdrawn_today": rupees(owed)}})
