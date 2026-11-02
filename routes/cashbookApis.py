"""Cash Book (Vinay, 2 Nov): every logged-in member and admin keeps their own businesses -> books -> cash in / out
entries. It is a notebook, NOT Treeway money: no wallet, no ledger, entries can be edited and deleted.

- Everything is tied to the user's 10-digit mobile (models.model.cb_mobile), never to users.id.
- Owner (made the business): full control. Member (a mobile the owner added to ONE book): view only.
- A member is matched by mobile at query time: no account needed when added, no invite to accept.
- No access to a book = 404. A view-only member trying to change something = 403.
- Totals are summed in SQL (one query for a whole list).
"""

from base import *
from models import responses as R
from models.model import CashBookEntryModel, CashBookMemberModel, CashBookNameModel, cb_mobile

cashbookApi = APIRouter(prefix="/cashbook")

NOT_FOUND = "This book was not found or is not shared with you."
VIEW_ONLY = "This book is view only for you."
SUMS = ("coalesce(sum(CASE WHEN e.type = 'in' THEN e.amount_paise ELSE 0 END), 0)::bigint AS t_in, "
        "coalesce(sum(CASE WHEN e.type = 'out' THEN e.amount_paise ELSE 0 END), 0)::bigint AS t_out")


def _totals(r: Dict) -> Dict:
    return {"total_in": rupees(r["t_in"]), "total_out": rupees(r["t_out"]), "balance": rupees(r["t_in"] - r["t_out"])}


async def _my_mobile(user: Dict) -> str:
    mobile = cb_mobile(await cpo.fetch_val("SELECT mobile FROM users WHERE id = $1", user["id"]))
    if not mobile:
        fail(403, "Your account has no valid mobile number.", "Forbidden")
    return mobile


async def _book(book_id: int, me: str, owner: bool = False, conn=None) -> Dict:
    """The book if I own it or it is shared with me (404 otherwise); owner=True: 403 for a view-only member.
    Inside a transaction (conn) the book row stays locked, so it cannot be deleted halfway through a change."""
    b = await cpo.fetch_one(
        "SELECT b.*, z.name AS business_name, b.owner_mobile = $2 AS is_owner FROM cb_books b "
        "JOIN cb_businesses z ON z.id = b.business_id WHERE b.id = $1 AND (b.owner_mobile = $2 OR EXISTS "
        "(SELECT 1 FROM cb_book_members m WHERE m.book_id = b.id AND m.mobile = $2))"
        + (" FOR UPDATE OF b" if conn else ""), book_id, me, conn=conn)
    if not b:
        fail(404, NOT_FOUND, "Not found")
    if owner and not b["is_owner"]:
        fail(403, VIEW_ONLY, "Forbidden")
    return b


async def _business(business_id: int, me: str, conn=None) -> Dict:
    """My business. 403 if one of its books is shared with me (I know it exists), else 404."""
    z = await cpo.fetch_one("SELECT * FROM cb_businesses WHERE id = $1" + (" FOR UPDATE" if conn else ""),
                            business_id, conn=conn)
    if z and z["owner_mobile"] == me:
        return z
    if z and await cpo.fetch_val("SELECT 1 FROM cb_book_members m JOIN cb_books b ON b.id = m.book_id "
                                 "WHERE b.business_id = $1 AND m.mobile = $2 LIMIT 1", business_id, me, conn=conn):
        fail(403, VIEW_ONLY, "Forbidden")
    fail(404, "Business not found.", "Not found")


async def _businesses(me: str, business_id: Optional[int] = None) -> List[Dict]:
    rows = await cpo.fetch_all(
        f"""
        SELECT z.id, z.name, z.updated_at, count(b.id) AS book_count,
               coalesce(sum(b.t_in), 0)::bigint AS t_in, coalesce(sum(b.t_out), 0)::bigint AS t_out
        FROM cb_businesses z
        LEFT JOIN (SELECT bk.id, bk.business_id, {SUMS} FROM cb_books bk LEFT JOIN cb_entries e ON e.book_id = bk.id
                   WHERE bk.owner_mobile = $1 GROUP BY bk.id) b ON b.business_id = z.id
        WHERE z.owner_mobile = $1 AND ($2::bigint IS NULL OR z.id = $2)
        GROUP BY z.id ORDER BY z.created_at, z.id
        """, me, business_id)
    return [{"id": r["id"], "name": r["name"], "updated_at": r["updated_at"], "book_count": r["book_count"],
             **_totals(r)} for r in rows]


# ---------------------------------------------------------------- businesses
@cashbookApi.get("/businesses", response_model=R.Envelope[R.CashBookHome])
async def home(user=Depends(tokenRequired())):
    """My businesses (with totals) and the books other people shared with my mobile."""
    me = await _my_mobile(user)
    shared = await cpo.fetch_all(
        f"""
        SELECT bk.id, bk.name, bk.updated_at, z.name AS business_name, z.owner_mobile, {SUMS}
        FROM cb_book_members m
        JOIN cb_books bk ON bk.id = m.book_id
        JOIN cb_businesses z ON z.id = bk.business_id
        LEFT JOIN cb_entries e ON e.book_id = bk.id
        WHERE m.mobile = $1
        GROUP BY bk.id, z.id ORDER BY bk.updated_at DESC, bk.id DESC
        """, me)
    return respond({"msg": "", "status": 200, "data": {
        "businesses": await _businesses(me),
        "shared": [{"id": r["id"], "name": r["name"], "business_name": r["business_name"],
                    "owner_mobile": r["owner_mobile"], "updated_at": r["updated_at"], **_totals(r)} for r in shared]}})


@cashbookApi.post("/businesses", response_model=R.Envelope[R.IdOnly], status_code=201)
async def add_business(body: CashBookNameModel, user=Depends(tokenRequired())):
    me = await _my_mobile(user)
    bid = await cpo.insertion("cb_businesses", {"name": body.name, "owner_mobile": me})
    return respond({"msg": "Business added.", "status": 201, "data": {"id": bid}})


@cashbookApi.put("/businesses/{businessId}", response_model=R.Envelope[R.NoData])
async def rename_business(businessId: int, body: CashBookNameModel, user=Depends(tokenRequired())):
    me = await _my_mobile(user)
    await _business(businessId, me)
    await cpo.execute("UPDATE cb_businesses SET name = $2, updated_at = now() WHERE id = $1 AND owner_mobile = $3",
                      businessId, body.name, me)
    return respond({"msg": "Business renamed.", "status": 200})


@cashbookApi.delete("/businesses/{businessId}", response_model=R.Envelope[R.NoData])
async def delete_business(businessId: int, user=Depends(tokenRequired())):
    """Its books, members and entries go too (ON DELETE CASCADE)."""
    me = await _my_mobile(user)
    await _business(businessId, me)
    await cpo.execute("DELETE FROM cb_businesses WHERE id = $1 AND owner_mobile = $2", businessId, me)
    return respond({"msg": "Business deleted.", "status": 200})


# ---------------------------------------------------------------- books
async def _book_row(book_id: int, conn=None) -> Dict:
    r = await cpo.fetch_one(f"SELECT bk.id, bk.name, bk.created_at, bk.updated_at, {SUMS} FROM cb_books bk "
                            "LEFT JOIN cb_entries e ON e.book_id = bk.id WHERE bk.id = $1 GROUP BY bk.id",
                            book_id, conn=conn)
    return {"id": r["id"], "name": r["name"], "created_at": r["created_at"], "updated_at": r["updated_at"],
            **_totals(r)}


@cashbookApi.get("/businesses/{businessId}/books", response_model=R.Envelope[R.CashBookBusinessBooks])
async def books(businessId: int, user=Depends(tokenRequired())):
    me = await _my_mobile(user)
    await _business(businessId, me)
    rows = await cpo.fetch_all(
        f"SELECT bk.id, bk.name, bk.created_at, bk.updated_at, {SUMS} FROM cb_books bk "
        "LEFT JOIN cb_entries e ON e.book_id = bk.id WHERE bk.business_id = $1 "
        "GROUP BY bk.id ORDER BY bk.updated_at DESC, bk.id DESC", businessId)
    return respond({"msg": "", "status": 200, "data": {
        "business": (await _businesses(me, businessId))[0],
        "books": [{"id": r["id"], "name": r["name"], "created_at": r["created_at"], "updated_at": r["updated_at"],
                   **_totals(r)} for r in rows]}})


@cashbookApi.post("/businesses/{businessId}/books", response_model=R.Envelope[R.CashBookBook], status_code=201)
async def add_book(businessId: int, body: CashBookNameModel, user=Depends(tokenRequired())):
    me = await _my_mobile(user)
    async with cpo.transaction() as conn:
        await _business(businessId, me, conn=conn)
        bid = await cpo.insertion("cb_books", {"business_id": businessId, "name": body.name, "owner_mobile": me},
                                  conn=conn)
        await cpo.execute("UPDATE cb_businesses SET updated_at = now() WHERE id = $1", businessId, conn=conn)
        book = await _book_row(bid, conn=conn)
    return respond({"msg": "Book added.", "status": 201, "data": book})


@cashbookApi.put("/books/{bookId}", response_model=R.Envelope[R.NoData])
async def rename_book(bookId: int, body: CashBookNameModel, user=Depends(tokenRequired())):
    me = await _my_mobile(user)
    await _book(bookId, me, owner=True)
    await cpo.execute("UPDATE cb_books SET name = $2, updated_at = now() WHERE id = $1 AND owner_mobile = $3",
                      bookId, body.name, me)
    return respond({"msg": "Book renamed.", "status": 200})


@cashbookApi.delete("/books/{bookId}", response_model=R.Envelope[R.NoData])
async def delete_book(bookId: int, user=Depends(tokenRequired())):
    """Its members and entries go too (ON DELETE CASCADE)."""
    me = await _my_mobile(user)
    await _book(bookId, me, owner=True)
    await cpo.execute("DELETE FROM cb_books WHERE id = $1 AND owner_mobile = $2", bookId, me)
    return respond({"msg": "Book deleted.", "status": 200})


@cashbookApi.get("/books/{bookId}", response_model=R.Envelope[R.CashBookBookDetail])
async def book_detail(bookId: int, user=Depends(tokenRequired())):
    me = await _my_mobile(user)
    b = await _book(bookId, me)
    members = await cpo.fetch_all(
        "SELECT m.mobile, m.added_at, EXISTS (SELECT 1 FROM users u WHERE u.mobile = m.mobile "
        "AND u.status <> 'removed') AS joined FROM cb_book_members m WHERE m.book_id = $1 ORDER BY m.id",
        bookId) if b["is_owner"] else []   # a view-only member never sees the other members' numbers
    t = await _book_row(bookId)
    return respond({"msg": "", "status": 200, "data": {
        "id": b["id"], "name": b["name"], "business_id": b["business_id"], "business_name": b["business_name"],
        "owner_mobile": b["owner_mobile"], "is_owner": b["is_owner"], "updated_at": b["updated_at"],
        "total_in": t["total_in"], "total_out": t["total_out"], "balance": t["balance"], "members": members}})


# ---------------------------------------------------------------- entries
@cashbookApi.get("/books/{bookId}/entries", response_model=R.Envelope[R.CashBookEntriesPage])
async def entries(bookId: int, search: str = Query("", max_length=100), page: int = 1, limit: int = 50,
                  user=Depends(tokenRequired())):
    """Newest first. search = words in the remark."""
    me = await _my_mobile(user)
    await _book(bookId, me)
    like = "%" + search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    data = await cpo.paginate(
        "SELECT id AS _k, entry_date AS _s, id, type, amount_paise, entry_date, remark, created_at, updated_at "
        "FROM cb_entries WHERE book_id = $1 AND ($2 = '%%' OR remark ILIKE $2)",
        bookId, like, sort="_s", page=page, limit=limit)
    for r in data["rows"]:
        r["amount"] = rupees(r.pop("amount_paise"))
    return respond({"msg": "", "status": 200, "data": data})


def _entry_data(body: CashBookEntryModel) -> Dict:
    return {"type": body.type, "amount_paise": to_paise(body.amount), "entry_date": body.entry_date,
            "remark": body.remark}


async def _entry_book(entry_id: int, me: str, conn) -> Dict:
    """The entry's book (locked) if I own it: 403 view only, 404 not mine / not found."""
    book_id = await cpo.fetch_val("SELECT book_id FROM cb_entries WHERE id = $1", entry_id, conn=conn)
    if book_id is None:
        fail(404, "Entry not found.", "Not found")
    return await _book(book_id, me, owner=True, conn=conn)


@cashbookApi.post("/books/{bookId}/entries", response_model=R.Envelope[R.IdOnly], status_code=201)
async def add_entry(bookId: int, body: CashBookEntryModel, user=Depends(tokenRequired())):
    me = await _my_mobile(user)
    data = _entry_data(body)
    async with cpo.transaction() as conn:
        await _book(bookId, me, owner=True, conn=conn)
        eid = await cpo.insertion("cb_entries", {**data, "book_id": bookId, "created_by_mobile": me}, conn=conn)
        await cpo.execute("UPDATE cb_books SET updated_at = now() WHERE id = $1", bookId, conn=conn)
    return respond({"msg": "Cash in added." if body.type == "in" else "Cash out added.", "status": 201,
                    "data": {"id": eid}})


@cashbookApi.put("/entries/{entryId}", response_model=R.Envelope[R.NoData])
async def edit_entry(entryId: int, body: CashBookEntryModel, user=Depends(tokenRequired())):
    me = await _my_mobile(user)
    data = _entry_data(body)
    async with cpo.transaction() as conn:
        b = await _entry_book(entryId, me, conn)
        await cpo.updating("cb_entries", {"id": entryId}, {**data, "updated_at": now_utc()}, conn=conn)
        await cpo.execute("UPDATE cb_books SET updated_at = now() WHERE id = $1", b["id"], conn=conn)
    return respond({"msg": "Entry saved.", "status": 200})


@cashbookApi.delete("/entries/{entryId}", response_model=R.Envelope[R.NoData])
async def delete_entry(entryId: int, user=Depends(tokenRequired())):
    me = await _my_mobile(user)
    async with cpo.transaction() as conn:
        b = await _entry_book(entryId, me, conn)
        await cpo.execute("DELETE FROM cb_entries WHERE id = $1", entryId, conn=conn)
        await cpo.execute("UPDATE cb_books SET updated_at = now() WHERE id = $1", b["id"], conn=conn)
    return respond({"msg": "Entry deleted.", "status": 200})


# ---------------------------------------------------------------- members
@cashbookApi.post("/books/{bookId}/members", response_model=R.Envelope[R.NoData], status_code=201)
async def add_member(bookId: int, body: CashBookMemberModel, user=Depends(tokenRequired())):
    """Any valid mobile, with or without a Treeway account: they see the book as soon as they log in."""
    me = await _my_mobile(user)
    async with cpo.transaction() as conn:
        await _book(bookId, me, owner=True, conn=conn)
        if body.mobile == me:
            fail(400, "This is your own number. You already own this book.")
        added = await cpo.insertion("cb_book_members", {"book_id": bookId, "mobile": body.mobile},
                                    conn=conn, on_conflict_do_nothing=True)
    if added is None:
        fail(409, "This number is already added to this book.")
    return respond({"msg": "Member added. They can see this book now.", "status": 201})


@cashbookApi.delete("/books/{bookId}/members/{mobile}", response_model=R.Envelope[R.NoData])
async def remove_member(bookId: int, mobile: str, user=Depends(tokenRequired())):
    me = await _my_mobile(user)
    await _book(bookId, me, owner=True)
    status = await cpo.execute("DELETE FROM cb_book_members WHERE book_id = $1 AND mobile = $2",
                               bookId, cb_mobile(mobile) or "")
    if status.endswith(" 0"):
        fail(404, "This number is not a member of this book.", "Not found")
    return respond({"msg": "Member removed.", "status": 200})
