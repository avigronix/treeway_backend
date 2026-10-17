import re

from adminApproval import cancel_pending
from base import *
from models import responses as R
from models.model import IFSC_RE, SettingModel

cmpApi = APIRouter()

# Vinay, 14 Oct 2026. Changes apply only to new income; nothing already paid changes.
EDITABLE_SETTINGS = {"level_income", "cut_percent", "min_withdraw", "p2p", "video", "shop", "inactive_remove_days",
                     "product_cost_percent", "super3", "activation_amount", "product_wallet_on_activation",
                     "two_admin_threshold", "ranks", "shop_sales_in_profit", "gullak"}
# Locked once the first member is activated (PIN price, turnover and the ₹987 split depend on them).
LOCKED_AFTER_LAUNCH = {"activation_amount", "product_wallet_on_activation"}
# Inside "super3" only these may change once a member is on a board; the board structure is locked.
SUPER3_FREE_FIELDS = {"direct_bonus", "self_help"}
PUBLIC_COMPANY_KEYS = ("name", "upi_id", "account_holder", "account_no", "ifsc", "branch", "qr_path",
                       "email", "phone", "whatsapp_group")


@cmpApi.get("/company/detail", response_model=R.Envelope[R.CompanyDetail])
async def company_detail():
    """Public: bank / UPI / QR shown on the payment screens."""
    company = (await get_settings())["company"]
    return respond({"msg": "", "status": 200, "data": {k: company.get(k, "") for k in PUBLIC_COMPANY_KEYS}})


@cmpApi.get("/config/public", response_model=R.Envelope[R.PublicConfig])
async def public_config():
    """Plan numbers the screens show (₹987 PIN price, minimum withdrawal, P2P limits ...). No secrets here."""
    s = await get_settings()
    p2p, video = s["p2p"], s["video"]
    return respond({"msg": "", "status": 200, "data": {
        "activation_amount": rupees(s["activation_amount"]), "pin_price": rupees(s["activation_amount"]),
        "product_wallet_on_activation": rupees(s["product_wallet_on_activation"]),
        "min_withdraw": rupees(s["min_withdraw"]), "inactive_remove_days": s["inactive_remove_days"],
        "cut_percent": s["cut_percent"],
        "p2p": {"fee_percent": p2p["fee_percent"], "min": rupees(p2p["min"]), "max": rupees(p2p["max"]),
                "daily_max": rupees(p2p["daily_max"])},
        "video": {"reward": rupees(video["reward"]), "days": video["days"], "per_day": video["per_day"],
                  "seconds": video["seconds"]},
        "shop": {"order_max": rupees(s["shop"]["order_max"])},
        "level_income": {k: rupees(v) for k, v in s["level_income"].items()},
        "demo": DEMO_MODE,
    }})


UPI_RE = re.compile(r"^[A-Za-z0-9.\-_]{2,64}@[A-Za-z][A-Za-z0-9]{1,30}$")
ACCOUNT_RE = re.compile(r"^\d{9,18}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")
# Members tap this link, so only real WhatsApp https links (never javascript: or another site).
WHATSAPP_RE = re.compile(r"^https://(chat\.whatsapp\.com|wa\.me|whatsapp\.com/channel)/[A-Za-z0-9/_\-?=&.]+$")


def _check_company(c: Dict[str, str]):
    """Empty = not set (that part is not shown to members). Anything filled must be in the right format."""
    rules = [("upi_id", UPI_RE, "UPI ID looks wrong (example: treeway@sbi)."),
             ("account_no", ACCOUNT_RE, "Account number must be 9 to 18 digits."),
             ("ifsc", IFSC_RE, "Enter a valid IFSC code (example: SBIN0001234)."),
             ("email", EMAIL_RE, "Enter a valid email."),
             ("phone", re.compile(r"^[6-9]\d{9}$"), "Phone must be a 10-digit mobile number."),
             ("whatsapp_group", WHATSAPP_RE, "Use the WhatsApp group link (https://chat.whatsapp.com/...).")]
    for key, rx, msg in rules:
        if c.get(key) and not rx.match(c[key]):
            fail(400, msg)
    if bool(c.get("account_no")) != bool(c.get("ifsc")):
        fail(400, "Fill both the account number and the IFSC, or leave both empty.")
    if c.get("account_no") and not c.get("account_holder"):
        fail(400, "Enter the account holder name.")


@cmpApi.patch("/admin/company/detail", response_model=R.Envelope[R.CompanyDetail])
async def update_company(request: Request, admin=Depends(tokenRequired(["ADMIN"]))):
    """Only the fields sent change. `qr` = a new QR picture; `qr_remove=1` removes it. Audited (old + new)."""
    form = await request.form()
    async with cpo.transaction() as conn:
        # Read under a row lock, not from the 30 s cache: two admins saving at once keep both changes.
        current = await cpo.fetch_val("SELECT value FROM app_settings WHERE key = 'company' FOR UPDATE", conn=conn)
        company = {**DEFAULT_SETTINGS["company"], **(current or {})}
        for k in PUBLIC_COMPANY_KEYS:
            if k != "qr_path" and k in form:
                v = " ".join(str(form[k]).split())[:200]   # one line, no extra spaces
                company[k] = v.upper() if k == "ifsc" else v.lower() if k in ("upi_id", "email") else v
        if str(form.get("qr_remove", "")) == "1":
            company["qr_path"] = ""
        _check_company(company)   # before the file is written: a wrong form leaves no stray picture
        qr = await save_upload(form.get("qr"), "company", required=False, images_only=True)
        if qr:
            company["qr_path"] = qr
        await _save_setting("company", company, admin["id"], conn)
    clear_settings_cache()
    return respond({"msg": "Company details saved. Members see them now.", "status": 200,
                    "data": {k: company.get(k, "") for k in PUBLIC_COMPANY_KEYS}})


async def _locks(conn=None) -> Dict[str, str]:
    """What cannot be changed right now, and why (shown on the Settings screen)."""
    locks: Dict[str, str] = {}
    if await cpo.fetch_val("SELECT EXISTS (SELECT 1 FROM users WHERE role = 'USER' AND activated_at IS NOT NULL)",
                           conn=conn):
        for k in sorted(LOCKED_AFTER_LAUNCH):
            locks[k] = "Members are already active."
    if await cpo.fetch_val("SELECT EXISTS (SELECT 1 FROM super3_nodes WHERE user_id IS NOT NULL)", conn=conn):
        for f in DEFAULT_SETTINGS["super3"]:
            if f not in SUPER3_FREE_FIELDS:
                locks[f"super3.{f}"] = "Members are already on the boards."
    return locks


@cmpApi.get("/admin/settings", response_model=R.Envelope[R.AdminSettings])
async def get_all_settings(admin=Depends(tokenRequired(["ADMIN"]))):
    changed = await cpo.fetch_all(
        "SELECT s.key, s.updated_at AS at, u.full_name AS by_name FROM app_settings s "
        "LEFT JOIN users u ON u.id = s.updated_by WHERE s.updated_by IS NOT NULL")
    return respond({"msg": "", "status": 200, "data": {"settings": await get_settings(),
                                                        "editable": sorted(EDITABLE_SETTINGS),
                                                        "locked": await _locks(),
                                                        "changed": {r["key"]: {"by_name": r["by_name"], "at": r["at"]}
                                                                    for r in changed}}})


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_percent(v) -> bool:
    return (_is_int(v) or isinstance(v, float)) and 0 <= v <= 100


def _check_shape(key: str, value: Any, default: Any) -> Any:
    """Same fields and types as the default. Money is whole paise; percents are 0-100."""
    if isinstance(default, dict):
        if not isinstance(value, dict):
            fail(400, f"{key}: send all of its fields.")
        if key == "shop.courier":
            # State name -> paise; "default" is needed for unknown states.
            # At least ₹1 each: no free delivery for now (Q4, Vinay 16 Oct).
            if "default" not in value or not all(isinstance(k, str) and k.strip() and _is_int(v) and v >= 100
                                                 for k, v in value.items()):
                fail(400, "Delivery charges: every state needs at least ₹1 (in paise), and a 'default'.")
            return value
        if set(value) != set(default):
            fail(400, f"{key}: fields must be exactly {', '.join(default)}.")
        return {k: _check_shape(f"{key}.{k}", value[k], default[k]) for k in default}
    if isinstance(default, bool):
        if not isinstance(value, bool):
            fail(400, f"{key}: true or false.")
        return value
    if "percent" in key:
        if not _is_percent(value):
            fail(400, f"{key}: enter a percent from 0 to 100.")
        return value
    if _is_int(default) and not (_is_int(value) and value >= 0):
        fail(400, f"{key}: enter a whole number (money is in paise).")
    return value


def _check_rules(key: str, value: Any):
    if key == "cut_percent" and sum(value.values()) >= 100:
        fail(400, "The total cut must be less than 100%.")
    if key == "super3" and value["direct_bonus"] + value["self_help"] != value["share_per_join"]:
        fail(400, "Direct bonus + Self Help Fund must equal the Super 3 share of each join.")
    if key == "p2p" and not value["min"] <= value["max"] <= value["daily_max"]:
        fail(400, "P2P: minimum ≤ maximum ≤ daily maximum.")
    if key == "video" and (value["per_day"] < 1 or value["days"] < 1):
        fail(400, "Videos: at least 1 video a day and 1 day.")
    if key == "gullak" and not 1 <= value["min_months"] <= 60:
        fail(400, "Gullak: interest after 1 to 60 months.")
    if key == "ranks":
        needs = [value["need"][r] for r in RANK_NAMES]
        if any(n < 1 for n in needs) or needs[1:] != sorted(needs[1:]):
            fail(400, "Ranks: each team need must be at least 1 and higher ranks need more.")
        if not 1 <= value["pay_day"] <= 28:
            fail(400, "Pay day must be from 1 to 28.")
        if not 1 <= value["leg_cap_percent"] <= 100:
            fail(400, "Team line rule must be from 1% to 100%.")
    if key == "activation_amount" and value <= 0:
        fail(400, "Activation amount must be more than 0.")


@cmpApi.put("/admin/settings", response_model=R.Envelope[Dict[str, Any]])
async def update_setting(body: SettingModel, admin=Depends(tokenRequired(["ADMIN"]))):
    if body.key not in EDITABLE_SETTINGS:
        fail(400, "This setting cannot be changed from the panel.")
    default = DEFAULT_SETTINGS[body.key]
    if isinstance(default, dict) and not isinstance(body.value, dict):
        fail(400, f"{body.key}: send the fields to change.")

    async with cpo.transaction() as conn:
        # Row lock: two admins saving at once cannot overwrite each other's change.
        current = await cpo.fetch_val("SELECT value FROM app_settings WHERE key = $1 FOR UPDATE", body.key, conn=conn)
        current = default if current is None else current
        value = {**current, **body.value} if isinstance(default, dict) else body.value  # only sent fields change
        if body.key == "two_admin_threshold" and value is None:
            value = 0  # null = OFF, stored as 0
        value = _check_shape(body.key, value, default)
        _check_rules(body.key, value)

        locks = await _locks(conn)
        if body.key in locks:
            fail(400, f"This setting is locked: {locks[body.key]}")
        if body.key == "super3":
            blocked = [f for f in value if value[f] != current.get(f) and f"super3.{f}" in locks]
            if blocked:
                fail(400, f"Locked because members are already on the boards: {', '.join(blocked)}")
        await _save_setting(body.key, value, admin["id"], conn)
        cancelled = await cancel_pending(conn, admin["id"]) if body.key == "two_admin_threshold" and value == 0 else 0
    clear_settings_cache()
    if cancelled:
        return respond({"msg": f"Setting saved. 2-admin approval is off; {cancelled} waiting approval(s) were cancelled. "
                               "Do those actions again.", "status": 200, "data": {body.key: value}})
    return respond({"msg": "Setting saved. It applies to new income within 30 seconds.", "status": 200,
                    "data": {body.key: value}})


async def _save_setting(key: str, value: Any, admin_id: int, conn=None):
    """Saves a setting and audits the old and the new value."""
    if conn is None:
        async with cpo.transaction() as c:
            await _save_setting(key, value, admin_id, c)
        clear_settings_cache()
        return
    old = await cpo.fetch_val("SELECT value FROM app_settings WHERE key = $1 FOR UPDATE", key, conn=conn)
    await cpo.execute(
        "INSERT INTO app_settings (key, value, updated_by) VALUES ($1, $2, $3) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()",
        key, value, admin_id, conn=conn)
    await audit(conn, admin_id, "setting_update", key, {"old": old, "new": value})
