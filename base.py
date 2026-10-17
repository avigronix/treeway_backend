import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config/.env"))

import jwt  # noqa: E402
from argon2 import PasswordHasher  # noqa: E402
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError  # noqa: E402
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, UploadFile  # noqa: E402,F401
from fastapi.encoders import jsonable_encoder  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, ValidationError  # noqa: E402

from config.pg_operation import pg_ops as cpo  # noqa: E402
from config.redisClient import redis_client  # noqa: E402

logger = logging.getLogger("treeway")

SECRET_KEY = os.environ.get("SECRET_KEY", "")
ALGORITHM = os.environ.get("ALGORITHM", "HS256")
TOKEN_EXPIRATION_MINUTES = int(os.environ.get("TOKEN_EXPIRATION_MINUTES", 720))
APP_ENV = os.environ.get("APP_ENV", "local")
IS_LOCAL = APP_ENV == "local"
# local = a developer's machine, e2e = the throwaway server for frontend end-to-end tests (scripts/e2e_server.py),
# demo = the sample-data server for meetings (scripts/demo_server.py; every answer carries X-Treeway-Demo: 1).
# Only these modes print OTPs, keep the dev OTP hook and show /docs. Production is anything else.
DEV_MODE = APP_ENV in ("local", "e2e", "demo")
DEMO_MODE = APP_ENV == "demo"
IST = timezone(timedelta(hours=5, minutes=30))
UPLOAD_ROOT = Path(os.path.dirname(os.path.abspath(__file__))) / "uploads"

if len(SECRET_KEY) < 32 and not IS_LOCAL:
    raise RuntimeError("SECRET_KEY must be at least 32 characters in production")

complaintStatus = ["Draft", "Submitted", "In Process", "Resolved", "Closed", "Rejected"]
allowedTransitions = {
    "Draft": ["Submitted"],
    "Submitted": ["In Process", "Rejected", "Closed"],
    "In Process": ["Resolved"],
    "Resolved": ["Closed"],
    "Rejected": [],
    "Closed": [],
}


# ================================================================ responses
def respond(res: Dict[str, Any]) -> JSONResponse:
    res.setdefault("msg", "")
    res.setdefault("status", 400)
    res.setdefault("data", [])
    return JSONResponse(jsonable_encoder(res), status_code=res.get("status"))


def fail(status: int, msg: str, error: str = "Error"):
    """Stop the request with a clear message (handled in main.py)."""
    raise HTTPException(status_code=status, detail={"error": error, "msg": msg})


def format_validation_errors(errors: List[Dict]) -> str:
    out = []
    for e in errors:
        loc = e.get("loc") or ()   # a model-level check (@model_validator) has an empty loc
        msg = e.get("msg", "Invalid value.").replace("Value error, ", "")
        if e.get("type") == "missing":
            msg = "This field is required."
        out.append(f"{loc[-1]}: {msg}" if loc else msg)
    return " | ".join(out)


# ================================================================ money
def to_paise(value: Any, field: str = "amount") -> int:
    """₹ from the user (int, float or text) to paise. Rejects junk and more than 2 decimals."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        fail(400, f"{field}: enter a valid amount")
    if d != d.quantize(Decimal("0.01")):
        fail(400, f"{field}: use at most 2 decimals")
    if d <= 0:
        fail(400, f"{field}: must be more than 0")
    return int(d * 100)


def rupees(paise: Optional[int]) -> float:
    return round((paise or 0) / 100, 2)


def percent_of(amount: int, percent: float) -> int:
    """Percent in paise, always rounded down so we never pay more than we have."""
    return int((Decimal(amount) * Decimal(str(percent)) / 100).to_integral_value(rounding=ROUND_DOWN))


# ================================================================ ids and codes
def new_user_code() -> str:
    # 7 digits = 90 lakh codes, room for 5 lakh+ members with few clashes.
    return f"TW{secrets.randbelow(9_000_000) + 1_000_000}"


def new_pin_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid confusion
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"TWP-{raw[:4]}-{raw[4:]}"


def new_ref(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_ist() -> date:
    return datetime.now(IST).date()


def period_start(period: str) -> datetime:
    """Start (IST) of 'today', 'week' (from Monday), 'month' or 'all'."""
    d = today_ist()
    if period == "week":
        d -= timedelta(days=d.weekday())
    elif period == "month":
        d = d.replace(day=1)
    elif period == "all":
        d = date(2000, 1, 1)
    return datetime.combine(d, datetime.min.time(), IST)


# Income credited to the Income Wallet after the 10% cut.
INCOME_TYPES = ("level_income", "direct_bonus", "super3_income", "single_leg", "royalty", "s3_level")
RANK_NAMES = ("Distributor", "Silver", "Gold", "Platinum", "Emerald", "Diamond")   # rank 1..6
# Wallet history filter groups (Wallet screen).
HISTORY_GROUPS = {
    "income": INCOME_TYPES,
    "funds": ("fund_add", "pin_buy"),
    "p2p": ("p2p_in", "p2p_out"),
    "withdrawal": ("withdrawal", "withdrawal_refund"),
    "product": ("activation_product", "yfi_daily"),
    "shop": ("shop_order", "shop_refund"),
    "gullak": ("gullak_open", "gullak_deposit", "gullak_withdraw", "gullak_interest"),
}


# ================================================================ passwords
_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# argon2 takes ~50 ms of CPU and 64 MB of RAM per call. Run it in a thread so the server keeps answering
# other requests, and at most 4 at a time per process so a login rush cannot eat the server's memory.
_hash_slots = asyncio.Semaphore(4)
_DUMMY_HASH = _ph.hash("not-a-real-password")


async def hash_password_async(password: str) -> str:
    async with _hash_slots:
        return await asyncio.to_thread(_ph.hash, password)


async def verify_password_async(password: str, password_hash: Optional[str]) -> bool:
    """With no hash (unknown ID) it still does the work, so the answer time does not reveal which IDs exist."""
    async with _hash_slots:
        ok = await asyncio.to_thread(verify_password, password, password_hash or _DUMMY_HASH)
    return ok and password_hash is not None


# ================================================================ settings
DEFAULT_SETTINGS: Dict[str, Any] = {
    "activation_amount": 98700,
    "product_wallet_on_activation": 98700,
    "video": {"reward": 2000, "days": 150, "per_day": 4, "seconds": 30},
    # Level -> paise. Ranges are inclusive.
    "level_income": {"1": 10000, "2": 5000, "3": 2500, "4-10": 1000, "11-15": 500, "16-25": 300},
    "cut_percent": {"tds": 2, "service": 3, "single_leg": 5},
    "min_withdraw": 20000,
    "p2p": {"fee_percent": 3, "min": 10000, "max": 300000, "daily_max": 300000},
    "super3": {
        "boards": 8,
        "share_per_join": 30000,     # ₹300 of every ₹987
        "board1_entry": 60000,       # ₹300 + ₹300 from the first two directs
        "direct_bonus": 20000,       # 3rd direct onwards: ₹200 to sponsor
        "self_help": 10000,          # 3rd direct onwards: ₹100 to Self Help Fund
        "recycle": 60000,            # boards 2-8
        "s3_level_cut": 10000,       # boards 3-8
        "s3_level_from_board": 3,
    },
    # Ranks: Distributor counts active directs, the others the full active team with the 50:50 rule.
    "ranks": {
        "need": {"Distributor": 5, "Silver": 50, "Gold": 500, "Platinum": 5000, "Emerald": 20000, "Diamond": 100000},
        "royalty_percent": 2,        # of the month's turnover, for EACH rank pool
        "leg_cap_percent": 50,       # at most this % of the need may come from one direct's line
        "pay_day": 3,
    },
    "s3_level": {"directs": 10, "months": 6},
    "inactive_remove_days": 30,
    # 2-admin approval: an action of this amount (paise) or more waits for a second admin. 0 = OFF.
    # OFF by default: any one admin approves any amount (bhaiya, 15 Oct 2026). The code stays, switched off.
    "two_admin_threshold": 0,
    # Until the CA confirms: shop purchases paid from the Fund / Income wallets are shown in the P&L for
    # information only. True = count them as sales in the profit (Product Wallet spend never is).
    "shop_sales_in_profit": False,
    # Gullak savings: built, OFF until the CA / lawyer approve (Vinay, 15 Oct).
    "gullak": {"enabled": False, "opening_charge": 10000, "deposit_charge_percent": 3, "interest_percent": 10,
               "min_months": 6, "min_deposit": 10000,
               "interest_tds_percent": 2},   # TDS on Gullak interest (CA to confirm the rate)

    "product_cost_percent": 5,
    "shop": {"order_max": 200000, "courier": {"Uttar Pradesh": 9000, "default": 15000}},
    "company": {
        "name": "Treeway", "upi_id": "", "account_holder": "", "account_no": "", "ifsc": "",
        "branch": "", "qr_path": "", "email": "", "phone": "", "whatsapp_group": "",
    },
}

_settings_cache: Dict[str, Any] = {}
_settings_cache_at = 0.0


async def get_settings(conn=None) -> Dict[str, Any]:
    """All plan numbers come from the database, cached for 30 seconds."""
    global _settings_cache, _settings_cache_at
    if _settings_cache and time.monotonic() - _settings_cache_at < 30:
        return _settings_cache
    rows = await cpo.fetch_all("SELECT key, value FROM app_settings", conn=conn)
    merged = dict(DEFAULT_SETTINGS)
    for r in rows:
        default = DEFAULT_SETTINGS.get(r["key"])
        # Fields added to a settings group later are filled from the defaults (old databases do not have them).
        merged[r["key"]] = {**default, **r["value"]} if isinstance(default, dict) and isinstance(r["value"], dict) \
            else r["value"]
    _settings_cache, _settings_cache_at = merged, time.monotonic()
    return merged


async def cached(key: str, seconds: int, make):
    """Admin totals that read lakhs of rows: kept in Redis for `seconds` (the same answer for every admin and
    worker). Returns (data, as_of). If Redis is down, it just computes."""
    k = f"cache:{key}"
    try:
        hit = await redis_client.get(k)
        if hit:
            d = json.loads(hit)
            return d["data"], d["as_of"]
    except Exception:  # noqa: BLE001
        hit = None
    data, as_of = jsonable_encoder(await make()), now_utc().isoformat()
    try:
        await redis_client.setex(k, seconds, json.dumps({"data": data, "as_of": as_of}))
    except Exception:  # noqa: BLE001
        pass
    return data, as_of


async def downline_level(ancestor_id: int, user_id: int, conn=None) -> Optional[int]:
    """How many levels below `ancestor_id` the member `user_id` is, at ANY depth (None = not in that downline).
    user_uplines keeps 25 levels per member, so we jump 25 at a time from each 25th ancestor: a member 1,000 levels
    deep costs ~40 index lookups (see docs/PERFORMANCE.md, P2P full downline)."""
    return await cpo.fetch_val(
        """
        WITH RECURSIVE j(id, base) AS (
            SELECT $1::bigint, 0
            UNION ALL
            SELECT u.ancestor_id, j.base + 25 FROM user_uplines u JOIN j ON u.user_id = j.id AND u.level = 25
        )
        SELECT min(j.base + x.level) FROM j CROSS JOIN LATERAL (
            -- "+ 0": look each jump up by user_id (<= 25 rows), never by ancestor: the company root is the ancestor
            -- of everyone, and a generic plan through that index took 471 ms (now 0.3 ms) on 1 lakh members.
            SELECT u.level FROM user_uplines u WHERE u.user_id = j.id AND u.ancestor_id + 0 = $2 LIMIT 1) x
        """, user_id, ancestor_id, conn=conn)


def clear_settings_cache():
    global _settings_cache_at
    _settings_cache_at = 0.0


def level_amount(level_table: Dict[str, int], level: int) -> int:
    for key, amount in level_table.items():
        low, _, high = key.partition("-")
        if int(low) <= level <= int(high or low):
            return int(amount)
    return 0


# ================================================================ auth
def create_token(user: Dict[str, Any]) -> str:
    payload = {
        "sub": str(user["id"]),
        "role": user["role"],
        "tv": user["token_version"],
        "exp": datetime.now(timezone.utc) + timedelta(minutes=TOKEN_EXPIRATION_MINUTES),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


AUTH_CACHE_SECONDS = 60


async def load_auth_user(user_id: int) -> Optional[Dict[str, Any]]:
    key = f"auth:user:{user_id}"
    cached = await redis_client.get(key)
    if cached:
        return json.loads(cached)
    user = await cpo.fetch_one(
        "SELECT id, user_code, full_name, role, status, token_version, sponsor_id FROM users WHERE id = $1",
        user_id,
    )
    if user:
        await redis_client.setex(key, AUTH_CACHE_SECONDS, json.dumps(user))
    return user


async def forget_auth_user(user_id: int):
    await redis_client.delete(f"auth:user:{user_id}")


def tokenRequired(accessRole: List[str] = [], activeOnly: bool = False, allowDoctor: bool = False):
    """Checks the token. accessRole=['ADMIN'] for admin APIs, activeOnly=True for APIs that need an active ID.
    Doctor accounts can use only routes that list 'DOCTOR' or set allowDoctor=True (privacy: a doctor never
    reaches member wallets, teams, lookups ...)."""

    async def verify(request: Request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            fail(401, "Please log in again.", "Unauthorized")
        try:
            payload = jwt.decode(header[7:], SECRET_KEY, algorithms=[ALGORITHM])
        except jwt.ExpiredSignatureError:
            fail(401, "Your session has ended. Please log in again.", "Unauthorized")
        except jwt.InvalidTokenError:
            fail(401, "Please log in again.", "Unauthorized")

        user = await load_auth_user(int(payload["sub"]))
        if not user:
            fail(401, "Please log in again.", "Unauthorized")
        # Blocked first: blocking also ends the old tokens, and the member must learn WHY (frontend request #4).
        if user["status"] in ("blocked", "removed"):
            fail(403, "This ID is blocked. Please contact support.", "Blocked")
        if user["token_version"] != payload.get("tv"):
            fail(401, "Please log in again.", "Unauthorized")
        if accessRole and user["role"] not in accessRole:
            fail(403, "You do not have access to this.", "Forbidden")
        if user["role"] == "DOCTOR" and "DOCTOR" not in accessRole and not allowDoctor:
            fail(403, "Doctor accounts cannot open this.", "Forbidden")
        if activeOnly and user["status"] != "active":
            fail(403, "Activate your ID first.", "Inactive")
        request.state.role = user["role"]
        return user

    return verify


async def audit(conn, admin_id: int, action: str, target: str = None, detail: Dict = None):
    await cpo.insertion("admin_audit", {"admin_id": admin_id, "action": action, "target": target,
                                        "detail": detail or {}}, conn=conn, returning=None)


# ================================================================ OTP (email)
OTP_TTL = 300
OTP_MAX_TRIES = 5
OTP_RESEND_GAP = 60


async def issue_otp(purpose: str, user: Dict[str, Any], email: str) -> None:
    from common.mailSender import send_otp_mail

    key = f"otp:{purpose}:{user['id']}"
    # SET NX: two taps at the same moment cannot both send an OTP.
    if not await redis_client.set(f"{key}:gap", 1, ex=OTP_RESEND_GAP, nx=True):
        fail(429, "Please wait a minute before asking for a new OTP.")
    code = f"{secrets.randbelow(900000) + 100000}"
    await redis_client.hset(key, mapping={"hash": _otp_hash(key, code), "tries": 0})
    await redis_client.expire(key, OTP_TTL)
    if DEV_MODE:
        # Dev / e2e only: the plain OTP for GET /api/v1/dev/last-otp (Playwright cannot read the console).
        await redis_client.setex(f"devotp:{email.lower()}:{purpose}", OTP_TTL, code)
    try:
        await send_otp_mail(email, user.get("full_name", ""), code, purpose)
    except Exception:
        # Mail failed: the member can try again right away.
        await redis_client.delete(key, f"{key}:gap", f"devotp:{email.lower()}:{purpose}")
        raise


def _otp_hash(key: str, code: str) -> str:
    # HMAC is enough for a 6-digit code that allows only 5 tries, and it does not block the server like argon2.
    return hmac.new(f"{SECRET_KEY}:{key}".encode(), code.encode(), hashlib.sha256).hexdigest()


# Check + use the OTP in ONE Redis step. With 10 requests at the same moment, the first right one
# deletes the key and wins; the rest see "expired". Only WRONG codes count as tries.
_OTP_USE = """
local stored = redis.call('HGET', KEYS[1], 'hash')
if not stored then return -1 end
if stored == ARGV[1] then redis.call('DEL', KEYS[1]) return 1 end
local tries = redis.call('HINCRBY', KEYS[1], 'tries', 1)
if tries >= tonumber(ARGV[2]) then redis.call('DEL', KEYS[1]) return -2 end
return 0
"""


async def check_otp(purpose: str, user_id: int, code: str) -> None:
    """Raises if the OTP is wrong, expired or tried too many times. Each OTP works exactly once."""
    key = f"otp:{purpose}:{user_id}"
    result = await redis_client.eval(_OTP_USE, 1, key, _otp_hash(key, str(code or "").strip()), OTP_MAX_TRIES)
    if result == 1:
        return
    if result == -1:
        fail(400, "OTP has expired or was already used. Please ask for a new one.")
    if result == -2:
        fail(400, "Too many wrong tries. Please ask for a new OTP.")
    fail(400, "Wrong OTP. Please check and try again.")


# ================================================================ files
ALLOWED_FILES = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
IMAGE_FILES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_FILE_BYTES = 5 * 1024 * 1024
# The first bytes of each allowed type: a renamed .exe / .html cannot pass as a picture or a PDF.
FILE_SIGNATURES = {".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",), ".png": (b"\x89PNG\r\n\x1a\n",),
                   ".pdf": (b"%PDF-",)}


def looks_like(ext: str, content: bytes) -> bool:
    if ext == ".webp":
        return content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return any(content.startswith(sig) for sig in FILE_SIGNATURES.get(ext, ()))


async def save_upload(file: Optional[UploadFile], sub_folder: str, required: bool = True,
                      images_only: bool = False) -> Optional[str]:
    """Saves an image or PDF (max 5 MB) with a random name. Returns the relative path.
    images_only: for public pictures (banners, products, QR)."""
    if file is None or not getattr(file, "filename", ""):
        if required:
            fail(400, "Please attach the file.")
        return None
    ext = Path(file.filename).suffix.lower()
    if images_only and ext not in IMAGE_FILES:
        fail(400, "Only JPG, PNG or WEBP images are allowed.")
    if ext not in ALLOWED_FILES:
        fail(400, "Only JPG, PNG, WEBP or PDF files are allowed.")
    content = await file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        fail(400, "File is too big. Maximum size is 5 MB.")
    if not content:
        fail(400, "The file is empty.")
    if not looks_like(ext, content):
        fail(400, "This file is not a real JPG, PNG, WEBP or PDF. Please choose the photo or PDF again.")
    folder = UPLOAD_ROOT / sub_folder
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    (folder / name).write_bytes(content)
    return f"{sub_folder}/{name}"


async def read_form(request: Request, model: type[BaseModel]) -> tuple[Dict[str, Any], Any]:
    """Reads multipart form data into a pydantic model. Returns (data, form)."""
    form = await request.form()
    raw = {k: v for k, v in form.items() if not hasattr(v, "filename")}
    try:
        data = model(**raw).model_dump()
    except ValidationError as e:
        fail(400, format_validation_errors(e.errors()), "Validation Error")
    return data, form


def page_args(page: int, limit: int) -> tuple[int, int]:
    return max(1, page), min(max(1, limit), 100)


def mask_account(acc: Optional[str]) -> Optional[str]:
    if not acc:
        return acc
    return f"{'*' * max(0, len(acc) - 4)}{acc[-4:]}"


def mask_email(email: Optional[str]) -> str:
    """s****@gmail.com"""
    if not email or "@" not in email:
        return ""
    name, _, domain = email.partition("@")
    return f"{name[:1]}****@{domain}"


# ================================================================ rate limits
# Per-IP limits only stop floods: on an event day a whole hall shares one Wi-Fi / Jio NAT IP. The real limits
# are per account / mobile / logged-in member (hit_limit and the per-member limits in main.py).
# The e2e and local dev servers may raise every limit (RATE_LIMIT_MULTIPLIER); PRODUCTION ALWAYS USES x1.
RATE_LIMIT_MULTIPLIER = max(1, int(os.environ.get("RATE_LIMIT_MULTIPLIER", 1))) if DEV_MODE else 1
LIMIT_MSG = "Too many tries. Please wait a minute and try again."


async def hit_limit(key: str, limit: int, window: int = 60, msg: str = LIMIT_MSG) -> None:
    """Counts one try for `key` (an account, mobile or member) and answers 429 above `limit` per `window` s.
    Redis down: the app keeps working (logged)."""
    try:
        pipe = redis_client.pipeline()
        pipe.incr(f"rate:id:{key}")
        pipe.expire(f"rate:id:{key}", window, nx=True)
        count = (await pipe.execute())[0]
    except Exception as e:  # noqa: BLE001
        logger.warning("hit_limit skipped: %s", e)
        return
    if count > limit * RATE_LIMIT_MULTIPLIER:
        fail(429, msg)


def client_ip(request: Request) -> str:
    # On the server nginx sets X-Real-IP (the real visitor IP from Cloudflare). Never trust
    # X-Forwarded-For from the browser: anyone can fake it to dodge the limits.
    if os.environ.get("TRUST_PROXY") == "1" and request.headers.get("x-real-ip"):
        return request.headers["x-real-ip"]
    return request.client.host if request.client else "unknown"


MOBILE_RE = re.compile(r"^[6-9]\d{9}$")
