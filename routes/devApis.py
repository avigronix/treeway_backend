"""Test helpers for local and e2e mode only (frontend request #3).

main.py includes this router only when APP_ENV is 'local' or 'e2e'. In production the routes do not exist
(404), and base.issue_otp never stores a plain OTP.

OTP purposes: admin_login, reset (forgot password), withdraw, p2p, bank, email_change.
"""

from base import *
from models import responses as R
import json

from models.model import DevCodeModel, DevEmailModel, DevOtpGapModel
from routes.videoApis import complete_video, start_video

devApi = APIRouter(tags=["dev (local / e2e only)"])

if not DEV_MODE:  # a second guard: importing this in production is a bug
    raise RuntimeError("routes.devApis must not be loaded in production")


@devApi.get("/dev/last-otp", response_model=R.Envelope[R.DevOtp])
async def last_otp(email: str = Query(..., max_length=120), purpose: str = Query(..., max_length=30)):
    """The last OTP sent to this email for this purpose (5 minutes, like the OTP itself)."""
    code = await redis_client.get(f"devotp:{email.strip().lower()}:{purpose}")
    if not code:
        fail(404, "No OTP for this email and purpose in the last 5 minutes.")
    return respond({"msg": "ok", "status": 200, "data": {"otp": code}})


@devApi.post("/dev/clear-otp-gap", response_model=R.Envelope[R.GapsCleared])
async def clear_otp_gap(body: DevOtpGapModel):
    """Skips the 60 second wait before a new OTP, for every ID with this email."""
    ids = [r["id"] for r in await cpo.fetch_all("SELECT id FROM users WHERE email = $1", body.email.strip().lower())]
    keys = [f"otp:{body.purpose}:{i}:gap" for i in ids]
    cleared = await redis_client.delete(*keys) if keys else 0
    return respond({"msg": "ok", "status": 200, "data": {"cleared": cleared}})


@devApi.post("/dev/watch-videos", response_model=R.Envelope[R.VideoDone])
async def watch_videos(body: DevCodeModel):
    """Today's remaining videos of one member, done through the REAL start / complete code (same checks, same
    ledger row `yfi:{user}:{date}`, same ₹20 once). Only the 30 s wait is skipped: each start is moved 31 s back.
    For the dev seed and E2E; never for an account someone tests by hand."""
    user = await cpo.fetch_one("SELECT id, user_code, role, status FROM users WHERE user_code = $1", body.user_code)
    if not user:
        fail(404, "Member not found.")
    if user["role"] != "USER":
        fail(400, "Only a member watches the daily videos.")
    if user["status"] != "active":
        fail(403, "Activate your ID first.", "Inactive")
    s = (await get_settings())["video"]
    done = await cpo.fetch_one("SELECT done_slots, rewarded_at FROM video_days WHERE user_id = $1 AND watch_date = $2",
                               user["id"], today_ist())
    if done and done["rewarded_at"]:
        fail(409, "Today's videos are already done.")
    last = None
    for slot in range((done["done_slots"] if done else 0) + 1, s["per_day"] + 1):
        await start_video(slot, user)
        await cpo.execute(
            "UPDATE video_days SET current_started = current_started - make_interval(secs => $3), "
            "log = jsonb_set(log, ARRAY[$4, 'started'], to_jsonb(current_started - make_interval(secs => $3))) "
            "WHERE user_id = $1 AND watch_date = $2", user["id"], today_ist(), s["seconds"] + 1, str(slot))
        last = json.loads((await complete_video(slot, user)).body)
    return respond({"msg": last["msg"], "status": 200, "data": last["data"]})


@devApi.post("/dev/skip-bank-hold", response_model=R.Envelope[R.BankHoldSkipped])
async def skip_bank_hold(body: DevEmailModel):
    """Moves the last bank change 25 hours back, so the 24 h withdrawal hold is over (FE #6: withdraw E2E, dev seed).
    Only IDs that already saved a bank account; nothing else changes."""
    n = await cpo.fetch_val(
        "WITH u AS (UPDATE users SET bank_updated_at = now() - interval '25 hours' "
        "WHERE email = $1 AND bank_updated_at IS NOT NULL RETURNING id) SELECT count(*) FROM u",
        body.email.strip().lower())
    return respond({"msg": "ok", "status": 200, "data": {"updated": n}})
