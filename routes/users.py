import asyncpg

from base import *
from typing import Union

from botGuard import check_pow, make_challenge

from models import responses as R
from models.model import (
    BankModel,
    ChangePasswordModel,
    ForgotOtpModel,
    LoginModel,
    LinkResetModel,
    LoginOtpModel,
    ProfileModel,
    RegisterModel,
    ResetPasswordModel,
    SignupTicketModel,
    SignupVerifyModel,
)

userApi = APIRouter()

LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 15 * 60
BANK_HOLD_HOURS = 24
ROOT_SPONSOR_CODE = "TW1000000"


async def _find_by_login(login_id: str) -> Optional[Dict]:
    login_id = login_id.strip().upper()
    if MOBILE_RE.match(login_id):
        return await cpo.fetch_one("SELECT * FROM users WHERE mobile = $1 AND status <> 'removed'", login_id)
    return await cpo.fetch_one("SELECT * FROM users WHERE user_code = $1", login_id)


def _can_sponsor(code: str, role: str) -> bool:
    # Members, and the root company ID. Other admins' IDs are not join links (their team would earn nothing).
    return role == "USER" or code == ROOT_SPONSOR_CODE


def _public_user(u: Dict) -> Dict:
    return {
        "id": u["id"], "user_code": u["user_code"], "full_name": u["full_name"], "mobile": u["mobile"],
        "email": u["email"], "role": u["role"], "status": u["status"], "activated_at": u["activated_at"],
        "direct_count": u["direct_count"], "state": u["state"], "district": u["district"],
        "created_at": u["created_at"],
    }


# ================================================================ sponsor + register
@userApi.get("/users/{sponsorCode}/validate-sponsor", response_model=R.Envelope[R.SponsorCheck])
async def validate_sponsor(sponsorCode: str):
    code = sponsorCode.strip().upper()
    sponsor = await cpo.fetch_one("SELECT full_name, status, role FROM users WHERE user_code = $1", code)
    if not sponsor or not _can_sponsor(code, sponsor["role"]):
        return respond({"msg": "Sponsor ID not found.", "status": 404,
                        "data": {"sponsor_code": code, "is_valid": False, "sponsor_name": ""}})
    if sponsor["status"] != "active":
        return respond({"msg": "This sponsor ID is not active yet. Ask them to activate first.", "status": 400,
                        "data": {"sponsor_code": code, "is_valid": False, "sponsor_name": sponsor["full_name"]}})
    return respond({"msg": "", "status": 200,
                    "data": {"sponsor_code": code, "is_valid": True, "sponsor_name": sponsor["full_name"]}})


@userApi.get("/users/register/challenge", response_model=R.Envelope[R.PowChallenge])
async def register_challenge():
    """Bot protection: a fresh proof-of-work challenge for the sign-up form (botGuard.py)."""
    return respond({"msg": "", "status": 200, "data": make_challenge()})


SIGNUP_TTL = 30 * 60        # a sign-up waits this long for its email OTP (and a lost verify answer is repeated)
SIGNUP_MAX_RESENDS = 5
SIGNUP_EXPIRED = "Sign-up has expired. Please fill the form again."


async def _sponsor_ok(code: str) -> Dict:
    sponsor = await cpo.fetch_one("SELECT id, status, role FROM users WHERE user_code = $1", code)
    if not sponsor or sponsor["status"] != "active" or not _can_sponsor(code, sponsor["role"]):
        fail(400, "Sponsor ID is not valid or not active.")
    return sponsor


async def _mobile_taken(mobile: str) -> bool:
    return bool(await cpo.fetch_val("SELECT 1 FROM users WHERE mobile = $1 AND status <> 'removed'", mobile))


def _signup_answer(ticket: str, email: str, expires_in: int) -> JSONResponse:
    return respond({"msg": "OTP sent to your email.", "status": 200, "data": {
        "signup_ticket": ticket, "email": mask_email(email), "resend_in": OTP_RESEND_GAP, "expires_in": expires_in}})


@userApi.post("/users/register", response_model=R.Envelope[R.SignupOtp])
async def register(body: RegisterModel):
    """Sign-up step 1 (email OTP, Q3 yes, 16 Oct): checks everything, keeps the form for 30 minutes (the password
    only as its hash) and mails an OTP. The ID is made by POST /users/register/verify. Old members are not touched."""
    await hit_limit(f"register:{body.mobile}", 5, 600)   # per mobile: 5 tries in 10 minutes
    await check_pow(body.pow_challenge, body.pow_nonce, body.website)
    await _sponsor_ok(body.sponsor_code)
    if await _mobile_taken(body.mobile):
        fail(409, "This mobile number is already registered.")
    ticket = secrets.token_urlsafe(32)
    data = body.model_dump(include={"sponsor_code", "full_name", "mobile", "email", "state", "district"})
    data["password_hash"] = await hash_password_async(body.password)
    await redis_client.setex(f"signup:{ticket}", SIGNUP_TTL, json.dumps(data))
    try:
        await issue_otp("signup", {"id": ticket, "full_name": body.full_name}, body.email)
    except BaseException:
        await redis_client.delete(f"signup:{ticket}")   # mail failed: nothing is kept, the form can be sent again
        raise
    return _signup_answer(ticket, body.email, SIGNUP_TTL)


@userApi.post("/users/register/resend", response_model=R.Envelope[R.SignupOtp])
async def register_resend(body: SignupTicketModel):
    """A new sign-up OTP to the same email (60 s gap, at most 5 per sign-up). To use another email, fill the form
    again (a new sign-up)."""
    raw = await redis_client.get(f"signup:{body.signup_ticket}")
    if not raw:
        fail(400, SIGNUP_EXPIRED, "SignupExpired")
    await hit_limit(f"signup-resend:{body.signup_ticket}", SIGNUP_MAX_RESENDS, SIGNUP_TTL,
                    "Too many OTPs for this sign-up. Please fill the form again.")
    data = json.loads(raw)
    await issue_otp("signup", {"id": body.signup_ticket, "full_name": data["full_name"]}, data["email"])
    return _signup_answer(body.signup_ticket, data["email"], max(1, await redis_client.ttl(f"signup:{body.signup_ticket}")))


async def _signup_done(user_id: int) -> JSONResponse:
    u = await cpo.fetch_one("SELECT * FROM users WHERE id = $1", user_id)
    if u["status"] in ("blocked", "removed"):
        fail(403, "This ID is blocked. Please contact support.", "Blocked")
    return respond({"msg": "Your ID is ready. Now activate it.", "status": 201, "data": {
        "user_code": u["user_code"], "full_name": u["full_name"], "token": create_token(u), "user": _public_user(u)}})


@userApi.post("/users/register/verify", response_model=R.Envelope[R.SignupDone], status_code=201)
async def register_verify(body: SignupVerifyModel):
    """Sign-up step 2: the right OTP makes the ID (free, inactive) and logs the member in. The same ticket again
    (double tap, lost answer) gives the same ID, never a second one."""
    ticket = body.signup_ticket
    done = await redis_client.get(f"signup:{ticket}:done")
    if done:
        return await _signup_done(int(done))
    raw = await redis_client.get(f"signup:{ticket}")
    if not raw:
        fail(400, SIGNUP_EXPIRED, "SignupExpired")
    try:
        await check_otp("signup", ticket, body.otp)
    except HTTPException:
        done = await redis_client.get(f"signup:{ticket}:done")   # the other tap won a moment ago
        if done:
            return await _signup_done(int(done))
        raise
    data = json.loads(raw)
    try:
        sponsor = await _sponsor_ok(data["sponsor_code"])          # may have been blocked meanwhile
        user_id = await _create_member(data, sponsor["id"])
    except HTTPException:
        await redis_client.delete(f"signup:{ticket}")             # 409 / 400: this sign-up cannot finish
        raise
    await redis_client.setex(f"signup:{ticket}:done", SIGNUP_TTL, user_id)
    await redis_client.delete(f"signup:{ticket}")
    return await _signup_done(user_id)


async def _create_member(data: Dict, sponsor_id: int) -> int:
    """The new inactive member + upline rows + wallets. 409 if the mobile was taken meanwhile (two sign-ups with
    the same mobile: the first verified wins)."""
    for _ in range(5):  # retry if the random member ID already exists
        code = new_user_code()
        try:
            async with cpo.transaction() as conn:
                user_id = await cpo.insertion("users", {
                    "user_code": code, "sponsor_id": sponsor_id, "full_name": data["full_name"],
                    "mobile": data["mobile"], "email": data["email"], "password_hash": data["password_hash"],
                    "state": data["state"], "district": data["district"],
                }, conn=conn)
                await cpo.execute(
                    "INSERT INTO user_uplines (user_id, level, ancestor_id) "
                    "SELECT $1::bigint, 1, $2::bigint UNION ALL "
                    "SELECT $1, level + 1, ancestor_id FROM user_uplines WHERE user_id = $2 AND level < 25",
                    user_id, sponsor_id, conn=conn,
                )
                await cpo.insertion("wallets", {"user_id": user_id}, conn=conn, returning=None)
            return user_id
        except asyncpg.UniqueViolationError as e:
            if e.constraint_name == "users_mobile_uq":
                fail(409, "This mobile number is already registered.")
            if e.constraint_name != "users_user_code_key":
                raise
    fail(500, "Could not create your ID. Please try again.")


# ================================================================ login
@userApi.post("/users/login", response_model=R.Envelope[Union[R.LoginToken, R.LoginOtpNeeded]])
async def login(body: LoginModel):
    # Per account, not per IP: a hall of members on one IP can all log in (event day).
    await hit_limit(f"login:{body.login_id.upper()}", 20)
    lock_key = f"loginfail:{body.login_id.upper()}"
    fails = int(await redis_client.get(lock_key) or 0)
    if fails >= LOGIN_MAX_FAILS:
        fail(429, "Too many wrong tries. Please try again after 15 minutes.")

    user = await _find_by_login(body.login_id)
    if not await verify_password_async(body.password, user["password_hash"] if user else None):
        await redis_client.incr(lock_key)
        await redis_client.expire(lock_key, LOGIN_LOCK_SECONDS)
        fail(401, "Wrong ID or password.")
    if user["status"] in ("blocked", "removed"):
        fail(403, "This ID is blocked. Please contact support.", "Blocked")

    await redis_client.delete(lock_key)
    if user["role"] in ("ADMIN", "DOCTOR"):
        # Admins and doctors need a second step: OTP to their email (a stolen password alone is not enough).
        purpose = "admin_login" if user["role"] == "ADMIN" else "doctor_login"
        await issue_otp(purpose, user, user["email"])
        ticket = secrets.token_urlsafe(32)
        await redis_client.setex(f"adminlogin:{ticket}", OTP_TTL, f"{user['id']}:{purpose}")
        return respond({"msg": "OTP sent to your email.", "status": 200,
                        "data": {"otp_required": True, "login_ticket": ticket, "email": mask_email(user["email"])}})
    return respond({"msg": "Logged in.", "status": 200,
                    "data": {"token": create_token(user), "user": _public_user(user)}})


@userApi.post("/users/login/otp", response_model=R.Envelope[R.LoginToken])
async def login_otp(body: LoginOtpModel):
    """Second step of the admin / doctor login."""
    ticket = await redis_client.get(f"adminlogin:{body.login_ticket}")
    if not ticket:
        fail(401, "Login has expired. Please log in again.")
    user_id, _, purpose = ticket.partition(":")
    await check_otp(purpose, int(user_id), body.otp)
    await redis_client.delete(f"adminlogin:{body.login_ticket}")
    user = await cpo.fetch_one("SELECT * FROM users WHERE id = $1", int(user_id))
    if user["status"] in ("blocked", "removed"):
        fail(403, "This ID is blocked. Please contact support.", "Blocked")
    return respond({"msg": "Logged in.", "status": 200,
                    "data": {"token": create_token(user), "user": _public_user(user)}})


@userApi.post("/users/logout-all", response_model=R.Envelope[R.NoData])
async def logout_all(user=Depends(tokenRequired(allowDoctor=True))):
    """Ends the session on every device."""
    await cpo.execute("UPDATE users SET token_version = token_version + 1 WHERE id = $1", user["id"])
    await forget_auth_user(user["id"])
    return respond({"msg": "Logged out from all devices.", "status": 200})


# ================================================================ passwords
@userApi.post("/users/forgot-password/otp", response_model=R.Envelope[R.MaskedEmail])
async def forgot_password_otp(body: ForgotOtpModel):
    """Member ID only (not mobile). Shows the masked email so the member knows where to look (Vinay, 14 Oct).
    Limits: 5 a member ID per 10 minutes, 60s resend gap, 5 min expiry, 5 wrong tries."""
    await hit_limit(f"forgot:{body.user_code}", 5, 600)
    user = await cpo.fetch_one("SELECT id, full_name, email, status FROM users WHERE user_code = $1", body.user_code)
    if not user or user["status"] == "removed":
        fail(404, "Member ID not found.")
    if user["status"] == "blocked":
        fail(403, "This ID is blocked. Please contact support.", "Blocked")
    await issue_otp("reset", user, user["email"])
    return respond({"msg": "OTP sent to your email.", "status": 200, "data": {"email": mask_email(user["email"])}})


@userApi.post("/users/forgot-password/reset", response_model=R.Envelope[R.NoData])
async def reset_password(body: ResetPasswordModel):
    user = await cpo.fetch_one("SELECT id, user_code, mobile FROM users WHERE user_code = $1", body.user_code)
    if not user:
        fail(400, "OTP has expired. Please ask for a new one.")
    await check_otp("reset", user["id"], body.otp)
    await cpo.execute("UPDATE users SET password_hash = $2, token_version = token_version + 1 WHERE id = $1",
                      user["id"], await hash_password_async(body.new_password))
    await forget_auth_user(user["id"])
    await redis_client.delete(f"loginfail:{user['user_code']}", f"loginfail:{user['mobile']}")
    return respond({"msg": "Password changed. Please log in.", "status": 200})


@userApi.post("/users/password-reset/link", response_model=R.Envelope[R.NoData])
async def reset_password_by_link(body: LinkResetModel):
    """The link the admin sent ('Send password reset'): works once, for 24 hours; ends every session."""
    uid = await redis_client.getdel(f"pwreset:{body.token}")
    if not uid:
        fail(400, "This link has expired or was already used. Ask Treeway support for a new one.", "LinkExpired")
    user = await cpo.fetch_one("SELECT id, user_code, mobile, email, status FROM users WHERE id = $1", int(uid))
    if user["status"] in ("blocked", "removed"):
        fail(403, "This ID is blocked. Please contact support.", "Blocked")
    await cpo.execute("UPDATE users SET password_hash = $2, token_version = token_version + 1 WHERE id = $1",
                      user["id"], await hash_password_async(body.new_password))
    await forget_auth_user(user["id"])
    await redis_client.delete(f"pwreset:user:{user['id']}", f"loginfail:{user['user_code']}",
                              f"loginfail:{user['mobile']}", f"devotp:{user['email'].lower()}:reset_link")
    return respond({"msg": "Password changed. Please log in.", "status": 200})


@userApi.post("/users/change-password", response_model=R.Envelope[R.TokenOnly])
async def change_password(body: ChangePasswordModel, user=Depends(tokenRequired(allowDoctor=True))):
    row = await cpo.fetch_one("SELECT * FROM users WHERE id = $1", user["id"])
    if not await verify_password_async(body.old_password, row["password_hash"]):
        fail(400, "Old password is wrong.")
    if body.old_password == body.new_password:
        fail(400, "New password must be different.")
    await cpo.execute("UPDATE users SET password_hash = $2, token_version = token_version + 1 WHERE id = $1",
                      user["id"], await hash_password_async(body.new_password))
    await forget_auth_user(user["id"])
    row["token_version"] += 1
    return respond({"msg": "Password changed.", "status": 200, "data": {"token": create_token(row)}})


# ================================================================ profile + bank
@userApi.get("/users/me", response_model=R.Envelope[R.Me])
async def me(user=Depends(tokenRequired(allowDoctor=True))):
    row = await cpo.fetch_one(
        "SELECT u.*, s.user_code AS sponsor_code, s.full_name AS sponsor_name "
        "FROM users u LEFT JOIN users s ON s.id = u.sponsor_id WHERE u.id = $1", user["id"])
    data = _public_user(row)
    data.update({
        "sponsor_code": row["sponsor_code"], "sponsor_name": row["sponsor_name"],
        "pan": row["pan"], "bank_holder": row["bank_holder"], "bank_account": mask_account(row["bank_account"]),
        "bank_ifsc": row["bank_ifsc"], "bank_branch": row["bank_branch"], "bank_updated_at": row["bank_updated_at"],
        "referral_link": f"{os.environ.get('FRONTEND_URL', '')}/join/{row['user_code']}" if row["status"] == "active" else None,
        "remove_on": None,
    })
    if row["status"] == "inactive" and row["role"] == "USER":
        # "Activate within 30 days, or this ID will be removed" (the night job runs after this date).
        days = int((await get_settings())["inactive_remove_days"])
        data["remove_on"] = (row["created_at"].astimezone(IST) + timedelta(days=days)).date()
    return respond({"msg": "", "status": 200, "data": data})


async def _send_change_otp(purpose: str, user_id: int):
    """OTP for a sensitive change always goes to the CURRENT email (a stolen login cannot redirect it)."""
    u = await cpo.fetch_one("SELECT id, full_name, email FROM users WHERE id = $1", user_id)
    await issue_otp(purpose, u, u["email"])
    return respond({"msg": "OTP sent to your email.", "status": 200, "data": {"email": mask_email(u["email"])}})


@userApi.post("/users/email-change/otp", response_model=R.Envelope[R.MaskedEmail])
async def email_change_otp(user=Depends(tokenRequired(allowDoctor=True))):
    return await _send_change_otp("email_change", user["id"])


@userApi.patch("/users/me", response_model=R.Envelope[R.NoData])
async def update_profile(body: ProfileModel, user=Depends(tokenRequired(allowDoctor=True))):
    data = {k: v for k, v in body.model_dump(exclude={"otp"}).items() if v is not None}
    if not data:
        fail(400, "Nothing to update.")
    if "email" in data:
        data["email"] = data["email"].lower()
        current = await cpo.fetch_val("SELECT email FROM users WHERE id = $1", user["id"])
        if data["email"] != current:
            if not body.otp:
                fail(400, "Enter the OTP sent to your current email to change the email.")
            await check_otp("email_change", user["id"], body.otp)
    await cpo.updating("users", {"id": user["id"]}, data)
    await forget_auth_user(user["id"])
    return respond({"msg": "Profile updated.", "status": 200})


@userApi.post("/users/bank-detail/otp", response_model=R.Envelope[R.MaskedEmail])
async def bank_detail_otp(user=Depends(tokenRequired())):
    return await _send_change_otp("bank", user["id"])


@userApi.put("/users/bank-detail", response_model=R.Envelope[R.NoData])
async def bank_detail(body: BankModel, user=Depends(tokenRequired())):
    """Needs an email OTP, and stops withdrawals for 24 hours (safety if a password is stolen)."""
    await check_otp("bank", user["id"], body.otp)
    await cpo.updating("users", {"id": user["id"]}, {**body.model_dump(exclude={"otp"}), "bank_updated_at": now_utc()})
    return respond({"msg": f"Bank details saved. Withdrawals open after {BANK_HOLD_HOURS} hours.", "status": 200})


@userApi.get("/users/mobile-check/{mobile}", response_model=R.Envelope[R.MobileCheck])
async def mobile_check(mobile: str):
    """Sign-up form: 'This number is already used' while typing (rate limited in main.py)."""
    if not MOBILE_RE.match(mobile):
        fail(400, "Enter a valid 10 digit mobile number.")
    used = await cpo.fetch_val("SELECT EXISTS (SELECT 1 FROM users WHERE mobile = $1 AND status <> 'removed')", mobile)
    return respond({"msg": "This number is already used." if used else "", "status": 200, "data": {"available": not used}})


# ================================================================ team
PENDING_ACTIVATION = ("EXISTS (SELECT 1 FROM payment_requests p WHERE p.user_id = m.id AND p.purpose = 'activation' "
                      "AND p.status = 'pending')")


@userApi.get("/users/team-summary", response_model=R.Envelope[R.TeamSummary])
async def team_summary(user=Depends(tokenRequired())):
    row = await cpo.fetch_one(
        f"""
        SELECT count(*) FILTER (WHERE u.level = 1)                         AS directs,
               count(*) FILTER (WHERE u.level = 1 AND m.status = 'active') AS active_directs,
               count(*)                                                    AS team,
               count(*) FILTER (WHERE m.status = 'active')                 AS active_team,
               count(*) FILTER (WHERE m.status = 'inactive')               AS inactive_team,
               count(*) FILTER (WHERE m.status = 'inactive' AND {PENDING_ACTIVATION}) AS payment_waiting
        FROM user_uplines u JOIN users m ON m.id = u.user_id
        WHERE u.ancestor_id = $1 AND m.status <> 'removed'
        """, user["id"])
    levels = await cpo.fetch_all(
        "SELECT u.level, count(*) AS total, count(*) FILTER (WHERE m.status = 'active') AS active "
        "FROM user_uplines u JOIN users m ON m.id = u.user_id "
        "WHERE u.ancestor_id = $1 AND m.status <> 'removed' GROUP BY u.level ORDER BY u.level", user["id"])
    return respond({"msg": "", "status": 200, "data": {**row, "levels": levels}})


@userApi.get("/users/team-members", response_model=R.Envelope[R.Page[R.TeamMember]])
async def team_members(level: Optional[int] = Query(None, ge=1, le=25),
                       status: Optional[str] = Query(None, pattern="^(active|inactive)$"),
                       search: Optional[str] = Query(None, max_length=40),
                       cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20, user=Depends(tokenRequired())):
    """No level = all 25 levels (e.g. 'my inactive team' on the PIN screen).
    team_size = that member's active team at full depth, from last night's rank job (a live count per row was
    too slow for big teams: 2 s for a leader with 33,000 people)."""
    data = await team_page(user["id"], level, status, search, cursor, page, limit)
    return respond({"msg": "", "status": 200, "data": data})


async def team_page(uid: int, level: Optional[int], status: Optional[str], search: Optional[str],
                    cursor: Optional[str], page: int, limit: int) -> Dict:
    """Also used by the admin's member page (Team tab)."""
    sql = ("SELECT m.user_code, m.full_name, m.status, m.direct_count, m.created_at, m.activated_at, u.level, "
           "m.id AS _k, "
           "s.user_code AS sponsor_code, "
           "coalesce(rs.active_team, 0) AS team_size, "
           f"{PENDING_ACTIVATION} AS payment_pending "
           "FROM user_uplines u JOIN users m ON m.id = u.user_id LEFT JOIN users s ON s.id = m.sponsor_id "
           "LEFT JOIN user_rank_stats rs ON rs.user_id = m.id "
           "WHERE u.ancestor_id = $1 AND m.status <> 'removed'")
    args: List[Any] = [uid]
    if level:
        args.append(level)
        sql += f" AND u.level = ${len(args)}"
    if status:
        args.append(status)
        sql += f" AND m.status = ${len(args)}"
    if search:
        args.append(f"%{search.strip()}%")
        sql += f" AND (m.user_code ILIKE ${len(args)} OR m.full_name ILIKE ${len(args)})"
    return await cpo.paginate(sql, *args, page=page, limit=limit, cursor=cursor)  # newest first (id order = join order)


@userApi.get("/users/top-sponsors", response_model=R.Envelope[List[R.TopSponsor]])
async def top_sponsors(user=Depends(tokenRequired())):
    """Team screen 'Top sponsors · most joins this month' (Vinay, 15 Oct): only people in MY downline
    (25 levels), top 5 by IDs they got activated this month. Never a company-wide list."""
    rows = await cpo.fetch_all(
        "SELECT s.user_code, s.full_name, count(*) AS joins FROM users j "
        "JOIN user_uplines d ON d.user_id = j.sponsor_id AND d.ancestor_id = $1 "
        "JOIN users s ON s.id = j.sponsor_id "
        "WHERE j.activated_at >= $2 AND j.role = 'USER' GROUP BY s.id, s.user_code, s.full_name "
        "ORDER BY joins DESC, s.id LIMIT 5", user["id"], period_start("month"))
    return respond({"msg": "", "status": 200, "data": rows})


@userApi.get("/users/member/{userCode}", response_model=R.Envelope[R.MemberLookup])
async def member_lookup(userCode: str, user=Depends(tokenRequired())):
    """Name check before sending P2P / PINs / PIN activation."""
    row = await cpo.fetch_one(
        "SELECT m.id, m.user_code, m.full_name, m.status, m.created_at FROM users m "
        "WHERE m.user_code = $1 AND m.status <> 'removed'", userCode.strip().upper())
    if not row:
        fail(404, "Member not found.")
    row["my_level"] = await downline_level(user["id"], row["id"])   # any depth (bhaiya, 16 Oct)
    row["is_my_sponsor"] = row["id"] == user["sponsor_id"]
    row.pop("id")
    return respond({"msg": "", "status": 200, "data": row})
