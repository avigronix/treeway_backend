"""Home / login banners and the public "Contact us" form (AdminContent screen; Vinay, 15 Oct 2026)."""

from base import *
from models import responses as R
from models.model import BannerFormModel, BannerPatchModel, ContactModel, ContactRepliedModel

contentApi = APIRouter()

CONTACT_PER_IP_DAY = 10
CONTACT_PER_MOBILE_DAY = 5
URL_RE = re.compile(r"https?://|www\.", re.I)


def _banner_out(b: Dict) -> Dict:
    return {"id": b["id"], "title": b["title"], "image": b["image_path"], "link_url": b["link_url"],
            "audience": b["audience"]}


# ================================================================ banners
@contentApi.get("/banners", response_model=R.Envelope[List[R.Banner]])
async def live_banners(audience: str = Query("everyone", pattern="^(everyone|login)$")):
    """Public. Only banners that are on and inside their dates (IST), in the admin's order."""
    rows = await cpo.fetch_all(
        "SELECT * FROM banners WHERE is_active AND audience = $1 AND (starts_at IS NULL OR starts_at <= now()) "
        "AND (ends_at IS NULL OR ends_at > now()) ORDER BY sort_order, id", audience)
    return respond({"msg": "", "status": 200, "data": [_banner_out(b) for b in rows]})


def _banner_state(b: Dict, now: datetime) -> str:
    if b["ends_at"] and b["ends_at"] <= now:
        return "ended"
    if not b["is_active"]:
        return "off"
    if b["starts_at"] and b["starts_at"] > now:
        return "scheduled"
    return "live"


def _admin_banner(b: Dict, now: datetime) -> Dict:
    state = _banner_state(b, now)
    return {**_banner_out(b), "starts_at": b["starts_at"], "ends_at": b["ends_at"], "is_active": b["is_active"],
            "sort_order": b["sort_order"], "updated_at": b["updated_at"], "state": state, "live": state == "live"}


@contentApi.get("/admin/banners", response_model=R.Envelope[List[R.AdminBanner]])
async def all_banners(admin=Depends(tokenRequired(["ADMIN"]))):
    rows = await cpo.fetch_all("SELECT * FROM banners ORDER BY audience, sort_order, id")
    now = now_utc()
    return respond({"msg": "", "status": 200, "data": [_admin_banner(b, now) for b in rows]})


@contentApi.get("/admin/banners/{bannerId}", response_model=R.Envelope[R.AdminBannerDetail])
async def banner_detail(bannerId: int, admin=Depends(tokenRequired(["ADMIN"]))):
    """One banner with who added it and who changed it last (Vinay 21 Oct)."""
    b = await cpo.fetch_one(
        "SELECT b.*, c.full_name AS created_by_name, e.created_at AS last_edit_at, e.full_name AS last_edit_by_name "
        "FROM banners b LEFT JOIN users c ON c.id = b.created_by "
        "LEFT JOIN LATERAL (SELECT a.created_at, u.full_name FROM admin_audit a LEFT JOIN users u ON u.id = a.admin_id "
        "  WHERE a.action = 'banner_edit' AND a.target = b.id::text ORDER BY a.id DESC LIMIT 1) e ON true "
        "WHERE b.id = $1", bannerId)
    if not b:
        fail(404, "Banner not found.")
    return respond({"msg": "", "status": 200, "data": {
        **_admin_banner(b, now_utc()), "created_at": b["created_at"], "created_by_name": b["created_by_name"],
        "last_edit_at": b["last_edit_at"], "last_edit_by_name": b["last_edit_by_name"]}})


def _check_times(starts, ends, ends_changed: bool):
    if ends_changed and ends and ends <= now_utc():
        fail(400, "That end time is already over. Pick a later one, or leave it empty.")
    if starts and ends and ends <= starts:
        fail(400, "The end time must be after the start time.")


@contentApi.post("/admin/banners", response_model=R.Envelope[R.IdOnly], status_code=201)
async def add_banner(request: Request, admin=Depends(tokenRequired(["ADMIN"]))):
    """Form: title, audience (everyone | login), starts_at, ends_at (IST "2026-10-22T18:00"), is_active, sort_order,
    link_url; file: image."""
    data, form = await read_form(request, BannerFormModel)
    _check_times(data["starts_at"], data["ends_at"], ends_changed=True)
    image = await save_upload(form.get("image"), "banners", images_only=True)
    async with cpo.transaction() as conn:
        bid = await cpo.insertion("banners", {**data, "image_path": image, "created_by": admin["id"]}, conn=conn)
        await audit(conn, admin["id"], "banner_add", str(bid), {k: str(v) for k, v in data.items()})
    return respond({"msg": "Banner added.", "status": 201, "data": {"id": bid}})


@contentApi.patch("/admin/banners/{bannerId}", response_model=R.Envelope[R.NoData])
async def edit_banner(bannerId: int, request: Request, admin=Depends(tokenRequired(["ADMIN"]))):
    """Any of the add fields (on/off with is_active, order with sort_order); a new image replaces the old one."""
    data, form = await read_form(request, BannerPatchModel)
    data = {k: v for k, v in data.items() if k in form}            # only the fields sent
    image = await save_upload(form.get("image"), "banners", required=False, images_only=True)
    async with cpo.transaction() as conn:
        old = await cpo.find_one("banners", {"id": bannerId}, conn=conn, for_update=True)
        if not old:
            fail(404, "Banner not found.")
        _check_times(data.get("starts_at", old["starts_at"]), data.get("ends_at", old["ends_at"]),
                     ends_changed="ends_at" in data)
        if image:
            data["image_path"] = image
        if data:
            await cpo.updating("banners", {"id": bannerId}, {**data, "updated_at": now_utc()}, conn=conn)
        await audit(conn, admin["id"], "banner_edit", str(bannerId), {k: str(v) for k, v in data.items()})
    return respond({"msg": "Banner saved.", "status": 200})


@contentApi.delete("/admin/banners/{bannerId}", response_model=R.Envelope[R.NoData])
async def delete_banner(bannerId: int, admin=Depends(tokenRequired(["ADMIN"]))):
    """Gone from Home / login at once. The audit log keeps a full copy. The picture file stays on disk: phones
    that cached the banner list for a few minutes never show a broken picture, and the copy can still be seen."""
    async with cpo.transaction() as conn:
        # One statement: of two deletes at once only one gets the row back (the other answers 404).
        old = await cpo.fetch_one("DELETE FROM banners WHERE id = $1 RETURNING *", bannerId, conn=conn)
        if not old:
            fail(404, "Banner not found. It may be deleted already.")
        await audit(conn, admin["id"], "banner_delete", str(bannerId),
                    {k: str(v) for k, v in old.items() if k not in ("id",)})
    return respond({"msg": "Banner deleted.", "status": 200})


# ================================================================ contact us
@contentApi.post("/contact", response_model=R.Envelope[R.NoData], status_code=201)
async def contact(body: ContactModel, request: Request):
    """Public form. Limits: 3 a minute and 10 a day per IP, 5 a day per mobile; links and bots are refused."""
    if body.website:                                     # hidden field only bots fill in
        return respond({"msg": "Thank you. We will call you soon.", "status": 201})
    if len(URL_RE.findall(body.message)) > 2:
        fail(400, "Please write your message without links.")
    ip = client_ip(request)
    day_key = f"contact:{ip}:{today_ist()}"
    count = await redis_client.incr(day_key)
    await redis_client.expire(day_key, 86400)
    if count > CONTACT_PER_IP_DAY:
        fail(429, "Too many messages today. Please call us instead.")
    recent = await cpo.fetch_one(
        "SELECT count(*) AS n, count(*) FILTER (WHERE message = $2) AS same FROM contact_messages "
        "WHERE mobile = $1 AND created_at > now() - interval '1 day'", body.mobile, body.message)
    if recent["n"] >= CONTACT_PER_MOBILE_DAY:
        fail(429, "Too many messages today. Please call us instead.")
    if not recent["same"]:                               # the same message twice is saved once
        await cpo.insertion("contact_messages", {"name": body.name, "mobile": body.mobile, "message": body.message,
                                                 "ip": ip}, returning=None)
    return respond({"msg": "Thank you. We will call you soon.", "status": 201})


@contentApi.get("/admin/contact-messages", response_model=R.Envelope[R.ContactPage])
async def contact_messages(status: Optional[str] = Query(None, pattern="^(new|replied)$"),
                           cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20,
                           admin=Depends(tokenRequired(["ADMIN"]))):
    sql = ("SELECT c.id, c.name, c.mobile, c.message, c.status, c.reply_note, c.replied_at, c.created_at, "
           "u.full_name AS replied_by_name, c.id AS _k FROM contact_messages c LEFT JOIN users u ON u.id = c.replied_by")
    args: List[Any] = []
    if status:
        args.append(status)
        sql += " WHERE c.status = $1"
    data = await cpo.paginate(sql, *args, page=page, limit=limit, cursor=cursor)
    data["counts"] = await cpo.fetch_one("SELECT count(*) FILTER (WHERE status = 'new') AS new, "
                                         "count(*) FILTER (WHERE status = 'replied') AS replied FROM contact_messages")
    return respond({"msg": "", "status": 200, "data": data})


@contentApi.post("/admin/contact-messages/{msgId}/replied", response_model=R.Envelope[R.NoData])
async def contact_replied(msgId: int, body: ContactRepliedModel, admin=Depends(tokenRequired(["ADMIN"]))):
    async with cpo.transaction() as conn:
        n = await cpo.fetch_val("UPDATE contact_messages SET status = 'replied', reply_note = $2, replied_by = $3, "
                                "replied_at = now() WHERE id = $1 AND status = 'new' RETURNING id",
                                msgId, body.note, admin["id"], conn=conn)
        if not n:
            fail(409, "This message is already marked replied (or does not exist).")
        await audit(conn, admin["id"], "contact_replied", str(msgId), {"note": body.note})
    return respond({"msg": "Marked as replied.", "status": 200})
