"""Free doctor advice (SRS 6, Vinay 15 Oct 2026).

- A member writes the problem (+ photos / reports). Max 3 open (waiting) requests per member.
- A new request goes to the active doctor with the fewest waiting requests; the admin can change it.
- The doctor sees only: name, age (asked with each request; bhaiya 16 Oct), state, problem and files. Never mobile, wallet,
  team or bank. Health files: the member, the assigned doctor and admins only.
- The doctor writes a prescription: shop products (or free text if not in the shop) with dosage and days,
  plus advice. The doctor's BAMS registration number is on it. Never edited: only a new one.
- "Order this medicine": the prescription's shop products go to the cart; the order uses the normal shop
  rules (₹2,000, Product -> Fund -> Income, courier) and is linked to the prescription.
- No reply-time promise: the app says "Doctor usually replies within 24 hours"; admins see older ones apart.
"""

import asyncpg

from base import *
from models import responses as R
from models.model import DoctorAssignModel, DoctorCreateModel, DoctorReassignModel, MemberStatusModel, PrescriptionModel

doctorApi = APIRouter()

MAX_OPEN = 3
OVERDUE_HOURS = 24
REPLY_NOTE = "Doctor usually replies within 24 hours."
MAX_FILES = 5


def age_of(dob: Optional[date]) -> Optional[int]:
    if not dob:
        return None
    t = today_ist()
    return t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day))


async def _least_busy_doctor(conn) -> Optional[int]:
    return await cpo.fetch_val(
        "SELECT u.id FROM users u JOIN doctor_profiles p ON p.user_id = u.id "
        "WHERE u.role = 'DOCTOR' AND u.status = 'active' "
        "ORDER BY (SELECT count(*) FROM doctor_requests r WHERE r.doctor_id = u.id AND r.status = 'waiting'), u.id "
        "LIMIT 1", conn=conn)


async def _prescriptions(request_ids: List[int], conn=None) -> Dict[int, List[Dict]]:
    if not request_ids:
        return {}
    pres = await cpo.fetch_all("SELECT * FROM prescriptions WHERE request_id = ANY($1::bigint[]) ORDER BY id",
                               request_ids, conn=conn)
    items = await cpo.fetch_all(
        "SELECT i.*, p.mrp, p.is_active AND p.stock > 0 AS in_stock FROM prescription_items i "
        "LEFT JOIN products p ON p.id = i.product_id WHERE i.prescription_id = ANY($1::bigint[]) "
        "ORDER BY i.prescription_id, i.line_no", [p["id"] for p in pres], conn=conn)
    out: Dict[int, List[Dict]] = {}
    for p in pres:
        out.setdefault(p["request_id"], []).append({
            "id": p["id"], "doctor_name": p["doctor_name"], "bams_reg_no": p["bams_reg_no"], "advice": p["advice"],
            "created_at": p["created_at"],
            "medicines": [{"product_id": i["product_id"], "name": i["name"], "dosage": i["dosage"], "days": i["days"],
                           "in_shop": i["product_id"] is not None,
                           "mrp": rupees(i["mrp"]) if i["mrp"] is not None else None,
                           "in_stock": bool(i["in_stock"])} for i in items if i["prescription_id"] == p["id"]]})
    return out


# ================================================================ member
@doctorApi.post("/doctor/requests", response_model=R.Envelope[R.DoctorRequestCreated], status_code=201)
async def create_request(request: Request, user=Depends(tokenRequired(activeOnly=True))):
    """Form: problem, age (years, 1 to 120); files: files (photos / reports, up to 5)."""
    form = await request.form()
    problem = (form.get("problem") or "").strip()
    if not 10 <= len(problem) <= 2000:
        fail(400, "Tell the doctor about the problem (10 to 2000 letters).")
    age = (form.get("age") or "").strip()
    if not age.isdigit() or not 1 <= int(age) <= 120:
        fail(400, "Enter the age in years (1 to 120).")
    uploads = [f for f in form.getlist("files") if getattr(f, "filename", "")]
    if len(uploads) > MAX_FILES:
        fail(400, f"You can add up to {MAX_FILES} files.")
    paths = [await save_upload(f, "health") for f in uploads]
    try:
        async with cpo.transaction() as conn:
            # Lock the member: 10 taps at once still make at most 3 open requests.
            await cpo.execute("SELECT 1 FROM users WHERE id = $1 FOR UPDATE", user["id"], conn=conn)
            open_now = await cpo.fetch_val("SELECT count(*) FROM doctor_requests WHERE user_id = $1 AND status = 'waiting'",
                                           user["id"], conn=conn)
            if open_now >= MAX_OPEN:
                fail(409, f"You already have {MAX_OPEN} questions waiting for the doctor. Please wait for a reply.")
            await cpo.advisory_lock(conn, "doctor_assign")   # fair spread when many requests come at once
            doctor = await _least_busy_doctor(conn)
            rid = await cpo.insertion("doctor_requests", {
                "user_id": user["id"], "doctor_id": doctor, "problem": problem, "files": paths, "age": int(age),
                "assigned_at": now_utc() if doctor else None}, conn=conn)
    except BaseException:
        for p in paths:
            (UPLOAD_ROOT / p).unlink(missing_ok=True)
        raise
    return respond({"msg": f"Sent to the doctor. {REPLY_NOTE}", "status": 201,
                    "data": {"id": rid, "reply_note": REPLY_NOTE}})


@doctorApi.get("/doctor/requests", response_model=R.Envelope[R.MemberDoctorPage])
async def my_requests(cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20,
                      user=Depends(tokenRequired())):
    data = await cpo.paginate("SELECT id, problem, files, status, created_at, answered_at, id AS _k FROM doctor_requests "
                              "WHERE user_id = $1", user["id"], page=page, limit=limit, cursor=cursor)
    pres = await _prescriptions([r["id"] for r in data["rows"]])
    data["rows"] = [{**r, "prescriptions": pres.get(r["id"], [])} for r in data["rows"]]
    data["reply_note"] = REPLY_NOTE
    data["open"] = await cpo.fetch_val("SELECT count(*) FROM doctor_requests WHERE user_id = $1 AND status = 'waiting'",
                                       user["id"])
    data["max_open"] = MAX_OPEN
    return respond({"msg": "", "status": 200, "data": data})


@doctorApi.get("/doctor/prescriptions/{prescriptionId}/cart", response_model=R.Envelope[R.PrescriptionCart])
async def prescription_cart(prescriptionId: int, user=Depends(tokenRequired(activeOnly=True))):
    """'Order this medicine': the shop products of the prescription, ready for POST /shop/orders
    (send prescription_id with the order). Free-text medicines are listed apart."""
    p = await cpo.fetch_one("SELECT p.id FROM prescriptions p JOIN doctor_requests r ON r.id = p.request_id "
                            "WHERE p.id = $1 AND r.user_id = $2", prescriptionId, user["id"])
    if not p:
        fail(404, "Prescription not found.")
    rows = await cpo.fetch_all(
        "SELECT i.product_id, i.name, pr.name AS shop_name, pr.mrp, pr.is_active AND pr.stock > 0 AS in_stock "
        "FROM prescription_items i LEFT JOIN products pr ON pr.id = i.product_id WHERE i.prescription_id = $1 "
        "ORDER BY i.line_no", prescriptionId)
    items = [{"product_id": r["product_id"], "name": r["shop_name"], "mrp": rupees(r["mrp"]), "qty": 1,
              "in_stock": bool(r["in_stock"])} for r in rows if r["product_id"]]
    return respond({"msg": "", "status": 200, "data": {
        "prescription_id": prescriptionId, "items": items,
        "not_in_shop": [r["name"] for r in rows if not r["product_id"]]}})


# ================================================================ doctor
def _for_doctor(r: Dict) -> Dict:
    """Only what the doctor may see (Vinay, 15 Oct): name, age, state, problem, files."""
    # The age written with the request; requests from before 16 Oct show the age from the old date of birth.
    age = r["age"] if r["age"] is not None else age_of(r["dob"])
    return {"id": r["id"], "member_name": r["full_name"], "age": age, "state": r["state"],
            "problem": r["problem"], "files": r["files"], "status": r["status"], "created_at": r["created_at"],
            "waiting_hours": int((now_utc() - r["created_at"]).total_seconds() // 3600) if r["status"] == "waiting" else None}


DOCTOR_VIEW_SQL = ("SELECT r.*, u.full_name, u.dob, u.state, r.id AS _k FROM doctor_requests r "
                   "JOIN users u ON u.id = r.user_id")


@doctorApi.get("/doctor/inbox", response_model=R.Envelope[R.DoctorInboxPage])
async def doctor_inbox(status: str = Query("waiting", pattern="^(waiting|answered)$"),
                       cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20,
                       doctor=Depends(tokenRequired(["DOCTOR"]))):
    data = await cpo.paginate(f"{DOCTOR_VIEW_SQL} WHERE r.doctor_id = $1 AND r.status = $2", doctor["id"], status,
                              desc=status == "answered", page=page, limit=limit, cursor=cursor)
    data["rows"] = [_for_doctor(r) for r in data["rows"]]
    data["counts"] = await cpo.fetch_one(
        "SELECT count(*) FILTER (WHERE status = 'waiting') AS waiting, "
        "count(*) FILTER (WHERE status = 'answered' AND answered_at >= $2) AS answered_this_month "
        "FROM doctor_requests WHERE doctor_id = $1", doctor["id"], period_start("month"))
    return respond({"msg": "", "status": 200, "data": data})


async def _doctor_request(rid: int, doctor_id: int, conn=None, lock: bool = False) -> Dict:
    r = await cpo.fetch_one(f"{DOCTOR_VIEW_SQL} WHERE r.id = $1" + (" FOR UPDATE OF r" if lock else ""), rid, conn=conn)
    if not r or r["doctor_id"] != doctor_id:
        fail(404, "Request not found.")        # another doctor's request looks like it does not exist
    return r


@doctorApi.get("/doctor/inbox/{requestId}", response_model=R.Envelope[R.DoctorRequestView])
async def doctor_request(requestId: int, doctor=Depends(tokenRequired(["DOCTOR"]))):
    r = await _doctor_request(requestId, doctor["id"])
    pres = await _prescriptions([requestId])
    return respond({"msg": "", "status": 200, "data": {**_for_doctor(r), "prescriptions": pres.get(requestId, [])}})


@doctorApi.post("/doctor/inbox/{requestId}/prescriptions", response_model=R.Envelope[R.IdOnly], status_code=201)
async def write_prescription(requestId: int, body: PrescriptionModel, doctor=Depends(tokenRequired(["DOCTOR"]))):
    async with cpo.transaction() as conn:
        r = await _doctor_request(requestId, doctor["id"], conn, lock=True)
        profile = await cpo.fetch_one("SELECT u.full_name, p.bams_reg_no FROM users u JOIN doctor_profiles p "
                                      "ON p.user_id = u.id WHERE u.id = $1", doctor["id"], conn=conn)
        shop = {}
        ids = [m.product_id for m in body.medicines if m.product_id]
        if ids:
            shop = {p["id"]: p["name"] for p in await cpo.fetch_all(
                "SELECT id, name FROM products WHERE id = ANY($1::bigint[]) AND is_active", ids, conn=conn)}
            if set(ids) - set(shop):
                fail(400, "A chosen medicine is not in the shop. Write it as text instead.")
        pid = await cpo.insertion("prescriptions", {
            "request_id": requestId, "doctor_id": doctor["id"], "doctor_name": profile["full_name"],
            "bams_reg_no": profile["bams_reg_no"], "advice": body.advice or ""}, conn=conn)
        for n, m in enumerate(body.medicines, start=1):
            await cpo.insertion("prescription_items", {
                "prescription_id": pid, "line_no": n, "product_id": m.product_id,
                "name": shop.get(m.product_id) or m.name, "dosage": m.dosage, "days": m.days}, conn=conn, returning=None)
        if r["status"] == "waiting":
            await cpo.execute("UPDATE doctor_requests SET status = 'answered', answered_at = now() WHERE id = $1",
                              requestId, conn=conn)
    return respond({"msg": "Prescription sent to the member.", "status": 201, "data": {"id": pid}})


# ================================================================ admin
async def _assign_waiting(conn, request_ids: List[int], to: Optional[int], admin_id: int, why: str) -> int:
    """Moves waiting requests to doctor `to`, or spreads them by the fewest waiting when `to` is None."""
    moved = 0
    for rid in request_ids:
        row = await cpo.fetch_one("SELECT doctor_id, status FROM doctor_requests WHERE id = $1 FOR UPDATE", rid, conn=conn)
        if not row or row["status"] != "waiting":
            continue                                  # answered meanwhile: leave it
        target = to or await _least_busy_doctor(conn)
        if not target:
            fail(409, "There is no active doctor to take these requests.")
        await cpo.execute("UPDATE doctor_requests SET doctor_id = $2, assigned_at = now() WHERE id = $1",
                          rid, target, conn=conn)
        await audit(conn, admin_id, "doctor_request_assign", str(rid), {"from": row["doctor_id"], "to": target, "why": why})
        moved += 1
    return moved


async def _doctor_id(conn, code: str, active: bool = True) -> int:
    d = await cpo.fetch_one("SELECT id, status FROM users WHERE user_code = $1 AND role = 'DOCTOR'", code.upper(), conn=conn)
    if not d:
        fail(404, "Doctor not found.")
    if active and d["status"] != "active":
        fail(409, "This doctor is blocked.")
    return d["id"]


@doctorApi.post("/admin/doctors", response_model=R.Envelope[R.UserCodeOnly], status_code=201)
async def add_doctor(body: DoctorCreateModel, admin=Depends(tokenRequired(["ADMIN"]))):
    pw = await hash_password_async(body.password)
    for _ in range(5):
        code = new_user_code()
        try:
            async with cpo.transaction() as conn:
                uid = await cpo.insertion("users", {
                    "user_code": code, "full_name": body.full_name, "mobile": body.mobile, "email": body.email,
                    "password_hash": pw, "role": "DOCTOR", "status": "active", "activated_at": now_utc()}, conn=conn)
                await cpo.insertion("wallets", {"user_id": uid}, conn=conn, returning=None)
                await cpo.insertion("doctor_profiles", {"user_id": uid, "bams_reg_no": body.bams_reg_no,
                                                        "created_by": admin["id"]}, conn=conn, returning=None)
                await audit(conn, admin["id"], "doctor_add", code, {"name": body.full_name, "bams_reg_no": body.bams_reg_no})
                # Requests that came while there was no active doctor.
                waiting = [r["id"] for r in await cpo.fetch_all(
                    "SELECT id FROM doctor_requests WHERE doctor_id IS NULL AND status = 'waiting' ORDER BY id", conn=conn)]
                await _assign_waiting(conn, waiting, uid, admin["id"], "first doctor")
            break
        except asyncpg.UniqueViolationError as e:
            if e.constraint_name == "users_mobile_uq":
                fail(409, "This mobile number is already registered.")
            if e.constraint_name != "users_user_code_key":
                raise
    else:
        fail(500, "Could not add the doctor. Please try again.")
    return respond({"msg": "Doctor added. They log in with this mobile, the password and an email OTP.", "status": 201,
                    "data": {"user_code": code}})


@doctorApi.get("/admin/doctors", response_model=R.Envelope[List[R.AdminDoctor]])
async def list_doctors(admin=Depends(tokenRequired(["ADMIN"]))):
    rows = await cpo.fetch_all(
        "SELECT u.user_code, u.full_name, u.mobile, u.email, u.status, p.bams_reg_no, u.created_at, "
        "(SELECT count(*) FROM doctor_requests r WHERE r.doctor_id = u.id AND r.status = 'waiting') AS waiting, "
        "(SELECT count(*) FROM doctor_requests r WHERE r.doctor_id = u.id AND r.status = 'answered' "
        " AND r.answered_at >= $1) AS answered_this_month "
        "FROM users u JOIN doctor_profiles p ON p.user_id = u.id WHERE u.role = 'DOCTOR' ORDER BY u.id",
        period_start("month"))
    return respond({"msg": "", "status": 200, "data": rows})


@doctorApi.post("/admin/doctors/{doctorCode}/status", response_model=R.Envelope[R.NoData])
async def doctor_status(doctorCode: str, body: MemberStatusModel, admin=Depends(tokenRequired(["ADMIN"]))):
    """Block / unblock a doctor. Their waiting requests stay with them until moved (reassign-pending)."""
    async with cpo.transaction() as conn:
        did = await _doctor_id(conn, doctorCode, active=False)
        new = "blocked" if body.action == "block" else "active"
        await cpo.execute("UPDATE users SET status = $2, token_version = token_version + 1 WHERE id = $1", did, new, conn=conn)
        await audit(conn, admin["id"], f"doctor_{body.action}", doctorCode.upper(), {"remark": body.remark})
    await forget_auth_user(did)
    return respond({"msg": f"Doctor {body.action}ed.", "status": 200})


@doctorApi.post("/admin/doctors/{doctorCode}/reassign-pending", response_model=R.Envelope[R.Moved])
async def reassign_pending(doctorCode: str, body: DoctorReassignModel, admin=Depends(tokenRequired(["ADMIN"]))):
    """Moves all waiting requests of a doctor (e.g. blocked) to one doctor, or spreads them (no to_doctor)."""
    async with cpo.transaction() as conn:
        await cpo.advisory_lock(conn, "doctor_assign")
        src = await _doctor_id(conn, doctorCode, active=False)
        to = await _doctor_id(conn, body.to_doctor) if body.to_doctor else None
        if to == src:
            fail(400, "Choose another doctor.")
        ids = [r["id"] for r in await cpo.fetch_all(
            "SELECT id FROM doctor_requests WHERE doctor_id = $1 AND status = 'waiting' ORDER BY id", src, conn=conn)]
        # Spreading must not give requests back to the same doctor: block them out first if they are active.
        if to is None and await cpo.fetch_val("SELECT status = 'active' FROM users WHERE id = $1", src, conn=conn):
            fail(400, "Block this doctor first, or choose the doctor to move the requests to.")
        moved = await _assign_waiting(conn, ids, to, admin["id"], body.remark)
    return respond({"msg": f"{moved} request(s) moved.", "status": 200, "data": {"moved": moved}})


@doctorApi.get("/admin/doctor-requests", response_model=R.Envelope[R.AdminDoctorPage])
async def admin_requests(status: Optional[str] = Query(None, pattern="^(waiting|answered)$"), overdue: bool = False,
                         doctor: Optional[str] = Query(None, max_length=12),
                         cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20,
                         admin=Depends(tokenRequired(["ADMIN"]))):
    """overdue=true: waiting more than 24 hours (shown apart on the admin screen)."""
    sql = ("SELECT r.id, r.problem, r.files, r.status, r.created_at, r.answered_at, r.assigned_at, "
           "u.user_code, u.full_name, d.user_code AS doctor_code, d.full_name AS doctor_name, r.id AS _k "
           "FROM doctor_requests r JOIN users u ON u.id = r.user_id LEFT JOIN users d ON d.id = r.doctor_id WHERE TRUE")
    args: List[Any] = []
    if status:
        args.append(status)
        sql += f" AND r.status = ${len(args)}"
    if overdue:
        args.append(now_utc() - timedelta(hours=OVERDUE_HOURS))
        sql += f" AND r.status = 'waiting' AND r.created_at < ${len(args)}"
    if doctor:
        args.append(doctor.upper())
        sql += f" AND d.user_code = ${len(args)}"
    data = await cpo.paginate(sql, *args, desc=False, page=page, limit=limit, cursor=cursor)
    pres = await _prescriptions([r["id"] for r in data["rows"]])
    data["rows"] = [{**r, "prescriptions": pres.get(r["id"], [])} for r in data["rows"]]
    data["counts"] = await cpo.fetch_one(
        "SELECT count(*) FILTER (WHERE status = 'waiting') AS waiting, "
        "count(*) FILTER (WHERE status = 'waiting' AND created_at < $1) AS overdue, "
        "count(*) FILTER (WHERE status = 'waiting' AND doctor_id IS NULL) AS no_doctor, "
        "count(*) FILTER (WHERE status = 'answered') AS answered FROM doctor_requests",
        now_utc() - timedelta(hours=OVERDUE_HOURS))
    return respond({"msg": "", "status": 200, "data": data})


@doctorApi.post("/admin/doctor-requests/{requestId}/assign", response_model=R.Envelope[R.NoData])
async def assign_request(requestId: int, body: DoctorAssignModel, admin=Depends(tokenRequired(["ADMIN"]))):
    async with cpo.transaction() as conn:
        status = await cpo.fetch_val("SELECT status FROM doctor_requests WHERE id = $1 FOR UPDATE", requestId, conn=conn)
        if not status:
            fail(404, "Request not found.")
        if status != "waiting":
            fail(409, "This request is already answered.")
        to = await _doctor_id(conn, body.doctor_code)
        await _assign_waiting(conn, [requestId], to, admin["id"], body.remark or "admin choice")
    return respond({"msg": "Request moved.", "status": 200})
