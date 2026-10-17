import asyncpg

from base import *
from models import responses as R
from models.model import CategoryModel, ComplaintModel, ComplaintStatusModel

complaintApi = APIRouter()

MEMBER_TRANSITIONS = {"Draft": ["Submitted"], "Resolved": ["Closed"]}
MAX_EVIDENCE = 5


async def _own_complaint(complaint_id: int, user: Dict, conn=None, lock: bool = False) -> Dict:
    """Members see only their own complaints. Admin sees all."""
    c = await cpo.find_one("complaints", {"id": complaint_id}, conn=conn, for_update=lock)
    if not c or (user["role"] != "ADMIN" and c["user_id"] != user["id"]):
        fail(404, "Complaint not found.")
    return c


# ================================================================ categories
@complaintApi.get("/complaints/categories", response_model=R.Envelope[List[R.Category]])
async def categories(user=Depends(tokenRequired())):
    where = {} if user["role"] == "ADMIN" else {"is_active": True}
    rows = await cpo.simple_finding("complaint_categories", where, "id, name, is_active", order_by="name ASC")
    return respond({"msg": "", "status": 200, "data": rows})


@complaintApi.post("/admin/complaints/categories", response_model=R.Envelope[R.IdOnly], status_code=201)
async def add_category(body: CategoryModel, admin=Depends(tokenRequired(["ADMIN"]))):
    try:
        cid = await cpo.insertion("complaint_categories", body.model_dump())
    except asyncpg.UniqueViolationError:
        fail(409, "This category already exists.")
    return respond({"msg": "Category added.", "status": 201, "data": {"id": cid}})


@complaintApi.put("/admin/complaints/categories/{catId}", response_model=R.Envelope[R.NoData])
async def edit_category(catId: int, body: CategoryModel, admin=Depends(tokenRequired(["ADMIN"]))):
    try:
        changed = await cpo.updating("complaint_categories", {"id": catId}, body.model_dump())
    except asyncpg.UniqueViolationError:
        fail(409, "This category already exists.")
    if not changed:
        fail(404, "Category not found.")
    return respond({"msg": "Category saved.", "status": 200})


# ================================================================ complaints
@complaintApi.post("/complaints", response_model=R.Envelope[R.ComplaintCreated], status_code=201)
async def create_complaint(body: ComplaintModel, user=Depends(tokenRequired())):
    if not await cpo.count("complaint_categories", {"id": body.category_id, "is_active": True}):
        fail(400, "Choose a valid category.")
    status = "Submitted" if body.submit else "Draft"
    for _ in range(3):
        no = f"CMP{today_ist():%y%m}{secrets.randbelow(900000) + 100000}"
        try:
            cid = await cpo.insertion("complaints", {
                "complaint_no": no, "user_id": user["id"], "category_id": body.category_id, "status": status,
                "incident": body.incident, "suspect": body.suspect, "complainant": body.complainant,
                "share_with_agencies": body.share_with_agencies,
            })
            break
        except asyncpg.UniqueViolationError:
            continue
    else:
        fail(500, "Please try again.")
    return respond({"msg": "Complaint submitted." if body.submit else "Draft saved.", "status": 201,
                    "data": {"id": cid, "complaint_no": no, "status": status}})


@complaintApi.put("/complaints/{complaintId}", response_model=R.Envelope[R.NoData])
async def edit_draft(complaintId: int, body: ComplaintModel, user=Depends(tokenRequired())):
    async with cpo.transaction() as conn:
        c = await _own_complaint(complaintId, user, conn, lock=True)
        if c["status"] != "Draft":
            fail(400, "Only a draft can be edited.")
        await cpo.updating("complaints", {"id": complaintId}, {
            "category_id": body.category_id, "incident": body.incident, "suspect": body.suspect,
            "complainant": body.complainant, "share_with_agencies": body.share_with_agencies,
            "status": "Submitted" if body.submit else "Draft", "updated_at": now_utc(),
        }, conn=conn)
    return respond({"msg": "Complaint submitted." if body.submit else "Draft saved.", "status": 200})


@complaintApi.post("/complaints/{complaintId}/evidence", response_model=R.Envelope[R.PathOnly])
async def add_evidence(complaintId: int, request: Request, user=Depends(tokenRequired())):
    form = await request.form()
    async with cpo.transaction() as conn:
        c = await _own_complaint(complaintId, user, conn, lock=True)
        if c["status"] not in ("Draft", "Submitted"):
            fail(400, "Evidence can be added only before the team starts working.")
        if len(c["evidence"]) >= MAX_EVIDENCE:
            fail(400, f"You can add up to {MAX_EVIDENCE} files.")
        path = await save_upload(form.get("file"), "evidence")
        await cpo.execute("UPDATE complaints SET evidence = evidence || $2::jsonb, updated_at = now() WHERE id = $1",
                          complaintId, [path], conn=conn)
    return respond({"msg": "File added.", "status": 200, "data": {"path": path}})


@complaintApi.get("/complaints", response_model=R.Envelope[R.Page[R.ComplaintListRow]])
async def list_complaints(status: Optional[str] = None, search: Optional[str] = Query(None, max_length=40),
                          cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20, user=Depends(tokenRequired())):
    # unread = messages from the other side after this side's last read message.
    if user["role"] == "ADMIN":
        unread = "m.sender_id = c.user_id AND m.id > c.admin_last_read"
    else:
        unread = "m.sender_id <> c.user_id AND m.id > c.member_last_read"
    sql = ("SELECT c.id, c.complaint_no, c.status, c.created_at, c.updated_at, k.name AS category, "
           "u.user_code, u.full_name, c.incident->>'date' AS happened_on, c.id AS _k, "
           f"(SELECT count(*) FROM complaint_messages m WHERE m.complaint_id = c.id AND {unread}) AS unread "
           "FROM complaints c JOIN complaint_categories k ON k.id = c.category_id "
           "JOIN users u ON u.id = c.user_id WHERE TRUE")
    args: List[Any] = []
    if user["role"] != "ADMIN":
        args.append(user["id"])
        sql += f" AND c.user_id = ${len(args)}"
    else:
        sql += " AND c.status <> 'Draft'"   # admin does not see unsent drafts
    if status in complaintStatus:
        args.append(status)
        sql += f" AND c.status = ${len(args)}"
    elif status == "open":   # still waiting on the team: Submitted + In Process
        sql += " AND c.status IN ('Submitted', 'In Process')"
    if search:
        args.append(search.strip().upper())
        sql += f" AND (c.complaint_no = ${len(args)} OR u.user_code = ${len(args)})"
    data = await cpo.paginate(sql, *args, page=page, limit=limit, cursor=cursor)
    return respond({"msg": "", "status": 200, "data": data})


@complaintApi.get("/complaints/matrix", response_model=R.Envelope[Dict[str, int]])
async def complaint_matrix(user=Depends(tokenRequired())):
    if user["role"] == "ADMIN":
        rows = await cpo.fetch_all("SELECT status, count(*) AS n FROM complaints GROUP BY status")
    else:
        rows = await cpo.fetch_all("SELECT status, count(*) AS n FROM complaints WHERE user_id = $1 GROUP BY status",
                                   user["id"])
    data = {s: 0 for s in complaintStatus}
    data.update({r["status"]: r["n"] for r in rows})
    return respond({"msg": "", "status": 200, "data": data})


@complaintApi.get("/complaints/{complaintId}", response_model=R.Envelope[R.ComplaintDetail])
async def complaint_detail(complaintId: int, user=Depends(tokenRequired())):
    c = await _own_complaint(complaintId, user)
    extra = await cpo.fetch_one(
        "SELECT k.name AS category, u.user_code, u.full_name, u.mobile FROM complaint_categories k, users u "
        "WHERE k.id = $1 AND u.id = $2", c["category_id"], c["user_id"])
    return respond({"msg": "", "status": 200, "data": {**c, **extra}})


@complaintApi.patch("/complaints/{complaintId}/status", response_model=R.Envelope[R.NoData])
async def change_status(complaintId: int, body: ComplaintStatusModel, user=Depends(tokenRequired())):
    async with cpo.transaction() as conn:
        c = await _own_complaint(complaintId, user, conn, lock=True)
        rules = allowedTransitions if user["role"] == "ADMIN" else MEMBER_TRANSITIONS
        if body.status not in rules.get(c["status"], []):
            fail(400, f"Cannot move from {c['status']} to {body.status}.")
        data = {"status": body.status, "updated_at": now_utc()}
        if body.remarks is not None:
            data["remarks"] = body.remarks
        await cpo.updating("complaints", {"id": complaintId}, data, conn=conn)
        if user["role"] == "ADMIN":
            await audit(conn, user["id"], "complaint_status", c["complaint_no"], {"to": body.status})
    return respond({"msg": "Status updated.", "status": 200})


# ================================================================ chat
@complaintApi.post("/complaints/{complaintId}/messages", response_model=R.Envelope[R.IdOnly], status_code=201)
async def send_message(complaintId: int, request: Request, user=Depends(tokenRequired())):
    form = await request.form()
    message = (form.get("message") or "").strip()[:2000] or None
    c = await _own_complaint(complaintId, user)
    if c["status"] in ("Draft", "Closed", "Rejected"):
        fail(400, "Chat is closed for this complaint.")
    path = await save_upload(form.get("file"), "chat", required=False)
    if not message and not path:
        fail(400, "Write a message or attach a file.")
    mid = await cpo.insertion("complaint_messages", {"complaint_id": complaintId, "sender_id": user["id"],
                                                     "message": message, "file_path": path})
    await cpo.execute("UPDATE complaints SET updated_at = now() WHERE id = $1", complaintId)
    return respond({"msg": "", "status": 201, "data": {"id": mid}})


@complaintApi.get("/complaints/{complaintId}/messages", response_model=R.Envelope[List[R.ChatMessage]])
async def get_messages(complaintId: int, after_id: int = 0, user=Depends(tokenRequired())):
    await _own_complaint(complaintId, user)
    rows = await cpo.fetch_all(
        "SELECT m.id, m.message, m.file_path, m.created_at, m.sender_id = $2 AS mine, "
        "CASE WHEN u.role = 'ADMIN' THEN 'Support team' ELSE u.full_name END AS sender "
        "FROM complaint_messages m JOIN users u ON u.id = m.sender_id "
        "WHERE m.complaint_id = $1 AND m.id > $3 ORDER BY m.id LIMIT 200", complaintId, user["id"], after_id)
    if rows:
        side = "admin_last_read" if user["role"] == "ADMIN" else "member_last_read"
        await cpo.execute(f"UPDATE complaints SET {side} = GREATEST({side}, $2) WHERE id = $1",
                          complaintId, rows[-1]["id"])
    return respond({"msg": "", "status": 200, "data": rows})
