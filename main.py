import logging
from contextlib import asynccontextmanager

import asyncpg
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from base import *
from models import responses as R
from config.pg_operation import BadCursor
from routes.adminApis import adminApi
from routes.companyApis import cmpApi
from routes.contentApis import contentApi
from routes.doctorApis import doctorApi
from routes.gullakApis import gullakApi
from routes.complaintApis import complaintApi
from routes.dashboardApis import dashboardApi
from routes.matrix import matrixApi
from routes.pinApis import pinApi
from routes.rankApis import rankApi
from routes.reportApis import reportApi
from routes.reverseApis import reverseApi
from routes.shopApis import shopApi
from routes.users import userApi
from routes.videoApis import videoApi
from routes.walletApis import walletApi

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await cpo.connect()
    yield
    await cpo.close()
    await redis_client.aclose()


app = FastAPI(title="Treeway API", lifespan=lifespan, docs_url="/docs" if DEV_MODE else None, redoc_url=None)

for router in (userApi, walletApi, pinApi, matrixApi, complaintApi, dashboardApi, cmpApi, adminApi, reverseApi, videoApi, rankApi, shopApi, doctorApi, reportApi, contentApi, gullakApi):
    app.include_router(router, prefix="/api/v1")
if DEV_MODE:
    # Test helpers (OTP reader). In production these routes do not exist at all: 404.
    from routes.devApis import devApi
    app.include_router(devApi, prefix="/api/v1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Only the company QR, product photos and banners are public. Receipts, evidence and chat files go through /api/v1/files (login needed).
(UPLOAD_ROOT / "company").mkdir(parents=True, exist_ok=True)
app.mount("/uploads/company", StaticFiles(directory=UPLOAD_ROOT / "company"), name="company-files")
(UPLOAD_ROOT / "products").mkdir(parents=True, exist_ok=True)
app.mount("/uploads/products", StaticFiles(directory=UPLOAD_ROOT / "products"), name="product-images")
(UPLOAD_ROOT / "banners").mkdir(parents=True, exist_ok=True)
app.mount("/uploads/banners", StaticFiles(directory=UPLOAD_ROOT / "banners"), name="banner-images")


# ================================================================ rate limit
WINDOW = 60
BLOCK_TIME = 300
# Event day (Vinay, 16 Oct night): hundreds of members in one hall share ONE IP (Wi-Fi / Jio NAT), so the
# per-IP numbers are only flood ceilings; the real limits are per logged-in member (from the token) and per
# account / mobile inside the routes (base.hit_limit). The multiplier is x1 in production (base.py).
IP_FLOOD_LIMIT = 3000 * RATE_LIMIT_MULTIPLIER  # per IP per minute, then the IP waits 5 minutes
MEMBER_LIMIT = 300 * RATE_LIMIT_MULTIPLIER     # per logged-in member per minute (any route)
ROUTE_IP_LIMITS = {"/api/v1/users/login": 600, "/api/v1/users/register": 200, "/api/v1/users/forgot-password": 60, "/api/v1/users/password-reset": 60,
                   "/api/v1/users/mobile-check": 600, "/api/v1/contact": 3,
                   "/api/v1/wallet/payment-request": 600, "/api/v1/wallet/withdrawal": 600, "/api/v1/wallet/p2p": 600}
ROUTE_MEMBER_LIMITS = {"/api/v1/wallet/payment-request": 10, "/api/v1/wallet/withdrawal": 10, "/api/v1/wallet/p2p": 10,
                       "/api/v1/shop/orders": 20, "/api/v1/doctor/requests": 10, "/api/v1/complaints": 60}
TOO_MANY = "Too many requests. Please try again after a few minutes."


def _route_limit(limits: Dict[str, int], path: str) -> Optional[tuple]:
    """Exact path or a sub-path ("/wallet/withdrawal/otp"), but not "/wallet/withdrawals" (the list)."""
    return next(((p, n) for p, n in limits.items() if path == p or path.startswith(p + "/")), None)


def _member_of(request: Request) -> Optional[str]:
    """The member id from a valid token (signature + expiry only; no DB). None for guests or a bad token."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        return str(jwt.decode(auth[7:], SECRET_KEY, algorithms=[ALGORITHM])["sub"])
    except Exception:  # noqa: BLE001 - the route answers 401 itself
        return None


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Every answer (API, /uploads pictures, private files): the browser must not guess another file type."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.middleware("http")
async def rate_limiter(request: Request, call_next):
    if request.method == "OPTIONS" or not request.url.path.startswith("/api/"):
        return await call_next(request)
    ip, path, member = client_ip(request), request.url.path, _member_of(request)
    try:
        if await redis_client.exists(f"block:{ip}"):
            return respond({"msg": TOO_MANY, "status": 429})
        keys = [(f"rate:{ip}", IP_FLOOD_LIMIT, True)]
        # Route ceilings count actions only (a GET like the sign-up challenge is not a sign-up), except mobile-check.
        counted = request.method != "GET" or path.startswith("/api/v1/users/mobile-check")
        hit = _route_limit(ROUTE_IP_LIMITS, path) if counted else None
        if hit:
            keys.append((f"rate:{ip}:{hit[0]}", hit[1] * RATE_LIMIT_MULTIPLIER, False))
        if member:
            keys.append((f"rate:m:{member}", MEMBER_LIMIT, False))
            hit = _route_limit(ROUTE_MEMBER_LIMITS, path) if request.method != "GET" else None
            if hit:
                keys.append((f"rate:m:{member}:{hit[0]}", hit[1] * RATE_LIMIT_MULTIPLIER, False))
        pipe = redis_client.pipeline()
        for key, _, _ in keys:
            pipe.incr(key)
            pipe.expire(key, WINDOW, nx=True)
        counts = (await pipe.execute())[::2]
        for (key, limit, flood), count in zip(keys, counts):
            if count > limit:
                if flood:
                    await redis_client.setex(f"block:{ip}", BLOCK_TIME, 1)
                return respond({"msg": TOO_MANY, "status": 429})
    except Exception as e:  # Redis down: keep the app working, just log it.
        logger.warning("rate limiter skipped: %s", e)

    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    if DEMO_MODE:
        response.headers["X-Treeway-Demo"] = "1"     # the app shows a "DEMO" banner
    return response


# ================================================================ errors
@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"msg": str(exc.detail)}
    return respond({"msg": detail.get("msg", ""), "status": exc.status_code, "data": {"error": detail.get("error", "Error")}})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return respond({"msg": format_validation_errors(exc.errors()), "status": 422, "data": {"error": "Validation Error"}})


@app.exception_handler(BadCursor)
async def bad_cursor(request: Request, exc: BadCursor):
    return respond({"msg": "This list link is old. Please refresh.", "status": 400, "data": {"error": "Cursor"}})


@app.exception_handler(asyncpg.CheckViolationError)
async def not_enough_balance(request: Request, exc: asyncpg.CheckViolationError):
    # Wallet balance CHECK (>= 0) failed: someone tried to spend more than they have.
    return respond({"msg": "Not enough balance.", "status": 400, "data": {"error": "Balance"}})


@app.exception_handler(asyncpg.UniqueViolationError)
async def duplicate(request: Request, exc: asyncpg.UniqueViolationError):
    logger.warning("duplicate on %s: %s", request.url.path, exc.constraint_name)
    return respond({"msg": "This was already done. Please refresh.", "status": 409, "data": {"error": "Duplicate"}})


@app.exception_handler(asyncpg.DeadlockDetectedError)
async def busy(request: Request, exc: asyncpg.DeadlockDetectedError):
    # Rare: two money actions touched the same wallets at once. Nothing was saved, safe to retry.
    logger.warning("deadlock on %s", request.url.path)
    return respond({"msg": "Server is busy. Please try again.", "status": 409, "data": {"error": "Busy"}})


@app.exception_handler(Exception)
async def server_error(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return respond({"msg": "Something went wrong. Please try again.", "status": 500, "data": {"error": "Server"}})


# ================================================================ health + private files
@app.get("/api/v1/health", response_model=R.Envelope[R.NoData])
async def health():
    await cpo.fetch_val("SELECT 1")
    await redis_client.ping()
    return respond({"msg": "ok", "status": 200})


@app.get("/api/v1/files/{folder}/{name}", response_class=FileResponse,
         responses={200: {"description": "The file (image or PDF)", "content": {"image/*": {}, "application/pdf": {}}}})
async def private_file(folder: str, name: str, user=Depends(tokenRequired(allowDoctor=True))):
    """Receipts, complaint evidence, chat and health files. Admin sees all; a member only their own; a doctor
    only the health files of requests assigned to them."""
    path = f"{folder}/{name}"
    if folder not in ("receipts", "evidence", "chat", "health") or "/" in name or ".." in name:
        fail(404, "File not found.")
    if user["role"] == "DOCTOR":
        # Health files only, and only for requests assigned to this doctor.
        mine = folder == "health" and await cpo.fetch_val(
            "SELECT EXISTS (SELECT 1 FROM doctor_requests WHERE doctor_id = $2 AND files ? $1)", path, user["id"])
        if not mine:
            fail(404, "File not found.")
    elif user["role"] != "ADMIN":
        mine = await cpo.fetch_val(
            "SELECT EXISTS (SELECT 1 FROM payment_requests WHERE receipt_path = $1 AND user_id = $2) "
            "OR EXISTS (SELECT 1 FROM complaints WHERE user_id = $2 AND evidence ? $1) "
            "OR EXISTS (SELECT 1 FROM complaint_messages m JOIN complaints c ON c.id = m.complaint_id "
            "           WHERE m.file_path = $1 AND c.user_id = $2) "
            "OR EXISTS (SELECT 1 FROM doctor_requests WHERE user_id = $2 AND files ? $1) "
            "OR EXISTS (SELECT 1 FROM orders WHERE user_id = $2 AND courier_receipt = $1)", path, user["id"])
        if not mine:
            fail(404, "File not found.")
    full = UPLOAD_ROOT / folder / name
    if not full.is_file():
        fail(404, "File not found.")
    return FileResponse(full)
