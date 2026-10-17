"""Shop, courier and orders (SRS 6, Vinay 15 Oct 2026).

- Up to ₹2,000 of products (MRP) per order, paid from the Product Wallet; a shortfall comes from the Fund
  Wallet, then the Income Wallet. Not enough in total -> no order.
- Courier: paid separately by UPI to the company QR (UTR + screenshot), once per order. The server works out
  the charge from the pincode: admin's pincode rate, else the state rate (UP ₹90, others ₹150).
  The state comes from the pincode (India Post data if imported, else the PIN prefix rule).
  A pincode the admin has not reviewed is on the "rate not set" list.
- Statuses: placed -> packed -> shipped -> delivered, or cancelled. Member cancels while placed; admin
  before shipped. Product money goes back to the same wallets, stock goes back. If the courier UTR was sent,
  the courier money is marked "refund due" (the admin pays it back from the bank).
- Courier UTR rejected: the order waits; the member sends a new UTR. After 7 days it is cancelled
  (backgroundRunnerFunction.cancel_unpaid_courier_orders).
"""

import asyncpg

from base import *
from mlmEngine import wallet_move
from models import responses as R
from models.model import CourierRateModel, CourierReviewModel, OrderCancelModel, OrderStatusModel, OrderUtrModel

shopApi = APIRouter()

UP = "Uttar Pradesh"
UTTARAKHAND_PREFIXES = {"246", "248", "249", "263"}      # inside the 20-28 (UP circle) range
MIXED_PREFIXES = {"244", "247", "262"}                   # have both UP and Uttarakhand pincodes
COURIER_FIX_DAYS = 7
LOW_STOCK = 10
PIN_RE = re.compile(r"^[1-9]\d{5}$")
WALLET_ORDER = ("product", "fund", "income")


# ================================================================ courier
async def pincode_state(pincode: str, conn=None) -> tuple:
    """(state or None, source). India Post table first, then the PIN prefix rule."""
    row = await cpo.fetch_val("SELECT state FROM pincodes WHERE pincode = $1", pincode, conn=conn)
    if row:
        return row, "india_post"
    if 20 <= int(pincode[:2]) <= 28:
        if pincode[:3] in UTTARAKHAND_PREFIXES:
            return "Uttarakhand", "pin_prefix"
        if pincode[:3] in MIXED_PREFIXES:
            return None, "unknown"
        return UP, "pin_prefix"
    return None, "other_state"                           # outside UP for sure: the default rate


async def courier_quote(pincode: str, conn=None) -> Dict:
    if not PIN_RE.match(pincode or ""):
        fail(400, "Enter a valid 6 digit PIN code.")
    rates = (await get_settings())["shop"]["courier"]
    state, source = await pincode_state(pincode, conn)
    own = await cpo.fetch_one("SELECT rate FROM pincode_rates WHERE pincode = $1", pincode, conn=conn)
    if own and own["rate"] is not None:
        rate, rate_from = own["rate"], "pincode"
    else:
        rate, rate_from = rates.get(state, rates["default"]) if state else rates["default"], "state"
    return {"pincode": pincode, "state": state, "state_source": source, "rate": rate, "rate_from": rate_from,
            "reviewed": own is not None}


async def _utr_used(utr: str, conn) -> bool:
    return await cpo.fetch_val(
        "SELECT EXISTS (SELECT 1 FROM orders WHERE courier_utr = $1 AND courier_status <> 'rejected') "
        "OR EXISTS (SELECT 1 FROM payment_requests WHERE utr = $1 AND status <> 'rejected')", utr, conn=conn)


# ================================================================ member
def _product_out(p: Dict) -> Dict:
    return {"id": p["id"], "name": p["name"], "description": p["description"], "category": p["category"],
            "mrp": rupees(p["mrp"]), "in_stock": p["stock"] > 0, "images": p["images"]}


@shopApi.get("/shop/products", response_model=R.Envelope[R.Page[R.Product]])
async def list_products(search: Optional[str] = Query(None, max_length=60), category: Optional[str] = Query(None, max_length=60),
                        cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20,
                        user=Depends(tokenRequired(allowDoctor=True))):
    sql = "SELECT *, id AS _k FROM products WHERE is_active"
    args: List[Any] = []
    if search:
        args.append(f"%{search.strip()}%")
        sql += f" AND name ILIKE ${len(args)}"
    if category:
        args.append(category)
        sql += f" AND category = ${len(args)}"
    data = await cpo.paginate(sql, *args, desc=False, page=page, limit=limit, cursor=cursor)
    data["rows"] = [_product_out(p) for p in data["rows"]]
    return respond({"msg": "", "status": 200, "data": data})


@shopApi.get("/shop/categories", response_model=R.Envelope[List[str]])
async def product_categories(user=Depends(tokenRequired(allowDoctor=True))):
    """Shop filter chips: the categories of products that are on, A-Z."""
    rows = await cpo.fetch_all("SELECT DISTINCT category FROM products WHERE is_active AND category IS NOT NULL "
                               "AND category <> '' ORDER BY category")
    return respond({"msg": "", "status": 200, "data": [r["category"] for r in rows]})


@shopApi.get("/shop/courier", response_model=R.Envelope[R.CourierQuote])
async def courier_charge(pincode: str = Query(..., max_length=6), user=Depends(tokenRequired())):
    """Checkout 'Check' button. The order recomputes this; the app's number is never trusted."""
    q = await courier_quote(pincode.strip())
    return respond({"msg": "", "status": 200, "data": {**q, "rate": rupees(q["rate"])}})


@shopApi.post("/shop/orders", response_model=R.Envelope[R.OrderPlaced], status_code=201)
async def place_order(request: Request, user=Depends(tokenRequired(activeOnly=True))):
    """Form fields: items (JSON [{product_id, qty}]), name, mobile, address, pincode, courier_utr; file: receipt."""
    from models.model import OrderFormModel

    data, form = await read_form(request, OrderFormModel)
    items = data["items"]
    s = await get_settings()
    receipt = await save_upload(form.get("receipt"), "receipts")
    try:
        async with cpo.transaction() as conn:
            await cpo.execute("SELECT 1 FROM wallets WHERE user_id = $1 FOR UPDATE", user["id"], conn=conn)
            if await _utr_used(data["courier_utr"], conn):
                fail(409, "This UTR is already used. Please check the number.")
            ids = sorted(i["product_id"] for i in items)
            products = {p["id"]: p for p in await cpo.fetch_all(
                "SELECT * FROM products WHERE id = ANY($1::bigint[]) ORDER BY id FOR UPDATE", ids, conn=conn)}
            total = 0
            for i in items:
                p = products.get(i["product_id"])
                if not p or not p["is_active"]:
                    fail(400, "A product in your cart is not available any more.")
                if p["stock"] < i["qty"]:
                    fail(409, f"Only {p['stock']} left of {p['name']}." if p["stock"] else f"{p['name']} is out of stock.")
                total += p["mrp"] * i["qty"]
            if data.get("prescription_id"):
                allowed = await cpo.fetch_all(
                    "SELECT i.product_id FROM prescription_items i JOIN prescriptions p ON p.id = i.prescription_id "
                    "JOIN doctor_requests r ON r.id = p.request_id WHERE p.id = $1 AND r.user_id = $2 "
                    "AND i.product_id IS NOT NULL", data["prescription_id"], user["id"], conn=conn)
                if not allowed or {i["product_id"] for i in items} - {a["product_id"] for a in allowed}:
                    fail(400, "These products are not on this prescription.")
            if total > s["shop"]["order_max"]:
                fail(400, f"You can order up to ₹{rupees(s['shop']['order_max']):,.0f} at a time.")

            bal = await cpo.fetch_one("SELECT product, fund, income FROM wallets WHERE user_id = $1", user["id"], conn=conn)
            if bal["product"] + bal["fund"] + bal["income"] < total:
                fail(400, "Not enough money in your wallets for this order.")
            paid, left = {}, total
            for w in WALLET_ORDER:                          # Product first, then Fund, then Income
                paid[w] = min(bal[w], left)
                left -= paid[w]

            quote = await courier_quote(data["pincode"], conn)
            order_no = "TW-ORD-" + str(await cpo.fetch_val("SELECT nextval('order_no_seq')", conn=conn))
            address = {k: data[k] for k in ("name", "mobile", "address", "pincode")}
            order_id = await cpo.insertion("orders", {
                "order_no": order_no, "user_id": user["id"], "items_total": total, "paid_product": paid["product"],
                "paid_fund": paid["fund"], "paid_income": paid["income"], "courier_charge": quote["rate"],
                "courier_state": quote["state"], "courier_utr": data["courier_utr"], "courier_receipt": receipt,
                "address": address, "prescription_id": data.get("prescription_id")}, conn=conn)
            for i in items:
                p = products[i["product_id"]]
                await cpo.execute("INSERT INTO order_items (order_id, product_id, name, mrp, qty) VALUES ($1, $2, $3, $4, $5)",
                                  order_id, p["id"], p["name"], p["mrp"], i["qty"], conn=conn)
                await cpo.execute("UPDATE products SET stock = stock - $2, updated_at = now() WHERE id = $1",
                                  p["id"], i["qty"], conn=conn)
            for w in WALLET_ORDER:
                await wallet_move(conn, user["id"], w, -paid[w], "shop_order", f"order:{order_id}:{w}",
                                  {"order_no": order_no})
    except BaseException:
        (UPLOAD_ROOT / receipt).unlink(missing_ok=True)
        raise
    return respond({"msg": "Order placed. We will pack it after checking the delivery payment.", "status": 201,
                    "data": {"id": order_id, "order_no": order_no, "items_total": rupees(total),
                             "paid": {w: rupees(v) for w, v in paid.items()}, "courier_charge": rupees(quote["rate"])}})


async def _order_rows(where: str, *args, conn=None):
    return await cpo.fetch_all(f"SELECT * FROM order_items WHERE {where} ORDER BY product_id", *args, conn=conn)


def _order_out(o: Dict, items: List[Dict], admin: bool = False) -> Dict:
    out = {
        "id": o["id"], "order_no": o["order_no"], "status": o["status"], "items_total": rupees(o["items_total"]),
        "paid": {"product": rupees(o["paid_product"]), "fund": rupees(o["paid_fund"]), "income": rupees(o["paid_income"])},
        "courier_charge": rupees(o["courier_charge"]), "courier_state": o["courier_state"], "courier_utr": o["courier_utr"],
        "courier_status": o["courier_status"], "courier_remark": o["courier_remark"],
        "courier_refund_due": o["courier_refund_due"], "courier_refunded_at": o["courier_refunded_at"],
        "address": o["address"], "courier_name": o["courier_name"], "tracking_no": o["tracking_no"],
        "cancel_reason": o["cancel_reason"], "created_at": o["created_at"], "packed_at": o["packed_at"],
        "shipped_at": o["shipped_at"], "delivered_at": o["delivered_at"], "cancelled_at": o["cancelled_at"],
        "prescription_id": o["prescription_id"],
        "items": [{"product_id": i["product_id"], "name": i["name"], "mrp": rupees(i["mrp"]), "qty": i["qty"]} for i in items],
    }
    if admin:
        out.update({"user_code": o.get("user_code"), "full_name": o.get("full_name"),
                    "courier_receipt": o["courier_receipt"], "courier_refund_utr": o["courier_refund_utr"]})
    return out


@shopApi.get("/shop/orders", response_model=R.Envelope[R.Page[R.Order]])
async def my_orders(cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20,
                    user=Depends(tokenRequired())):
    data = await cpo.paginate("SELECT *, id AS _k FROM orders WHERE user_id = $1", user["id"],
                              page=page, limit=limit, cursor=cursor)
    items = await _order_rows("order_id = ANY($1::bigint[])", [o["id"] for o in data["rows"]])
    data["rows"] = [_order_out(o, [i for i in items if i["order_id"] == o["id"]]) for o in data["rows"]]
    return respond({"msg": "", "status": 200, "data": data})


async def _load_order(order_id: int, conn=None, lock: bool = False) -> Dict:
    o = await cpo.fetch_one("SELECT o.*, u.user_code, u.full_name FROM orders o JOIN users u ON u.id = o.user_id "
                            "WHERE o.id = $1" + (" FOR UPDATE OF o" if lock else ""), order_id, conn=conn)
    if not o:
        fail(404, "Order not found.")
    return o


@shopApi.get("/shop/orders/{orderId}", response_model=R.Envelope[R.Order])
async def my_order(orderId: int, user=Depends(tokenRequired())):
    o = await _load_order(orderId)
    if o["user_id"] != user["id"]:
        fail(404, "Order not found.")
    return respond({"msg": "", "status": 200, "data": _order_out(o, await _order_rows("order_id = $1", orderId))})


async def cancel_order(conn, o: Dict, by: Optional[int], reason: str):
    """Product money back to the same wallets, stock back. Courier money: refund due if a UTR was accepted
    or still being checked (the admin returns it from the bank)."""
    for w in WALLET_ORDER:
        await wallet_move(conn, o["user_id"], w, o[f"paid_{w}"], "shop_refund", f"order:{o['id']}:refund:{w}",
                          {"order_no": o["order_no"], "reason": reason})
    for i in await _order_rows("order_id = $1", o["id"], conn=conn):
        await cpo.execute("UPDATE products SET stock = stock + $2, updated_at = now() WHERE id = $1",
                          i["product_id"], i["qty"], conn=conn)
    await cpo.execute(
        "UPDATE orders SET status = 'cancelled', cancelled_at = now(), cancelled_by = $2, cancel_reason = $3, "
        "courier_refund_due = courier_status <> 'rejected' AND courier_charge > 0 WHERE id = $1",
        o["id"], by, reason, conn=conn)


@shopApi.post("/shop/orders/{orderId}/cancel", response_model=R.Envelope[R.NoData])
async def member_cancel(orderId: int, body: OrderCancelModel, user=Depends(tokenRequired())):
    async with cpo.transaction() as conn:
        o = await _load_order(orderId, conn, lock=True)
        if o["user_id"] != user["id"]:
            fail(404, "Order not found.")
        if o["status"] != "placed":
            fail(409, "This order is already packed. Please raise a complaint to cancel it.")
        await cancel_order(conn, o, user["id"], body.reason or "Cancelled by member")
    return respond({"msg": "Order cancelled. The money is back in your wallets.", "status": 200})


@shopApi.post("/shop/orders/{orderId}/courier-utr", response_model=R.Envelope[R.NoData])
async def resend_utr(orderId: int, request: Request, user=Depends(tokenRequired())):
    """After the delivery payment was rejected: a new UTR + screenshot."""
    data, form = await read_form(request, OrderUtrModel)
    receipt = await save_upload(form.get("receipt"), "receipts")
    try:
        async with cpo.transaction() as conn:
            o = await _load_order(orderId, conn, lock=True)
            if o["user_id"] != user["id"]:
                fail(404, "Order not found.")
            if o["status"] != "placed" or o["courier_status"] != "rejected":
                fail(409, "This order does not need a new payment.")
            if await _utr_used(data["courier_utr"], conn):
                fail(409, "This UTR is already used. Please check the number.")
            await cpo.execute(
                "UPDATE orders SET courier_utr = $2, courier_receipt = $3, courier_status = 'pending', "
                "courier_rejected_at = NULL, courier_remark = NULL WHERE id = $1",
                orderId, data["courier_utr"], receipt, conn=conn)
    except BaseException:
        (UPLOAD_ROOT / receipt).unlink(missing_ok=True)
        raise
    return respond({"msg": "Payment sent again. Our team will check it.", "status": 200})


# ================================================================ admin: products
@shopApi.get("/admin/products", response_model=R.Envelope[R.Page[R.AdminProduct]])
async def admin_products(cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 50,
                         admin=Depends(tokenRequired(["ADMIN"]))):
    data = await cpo.paginate("SELECT *, id AS _k FROM products", desc=False, page=page, limit=limit, cursor=cursor)
    data["rows"] = [{**_product_out(p), "stock": p["stock"], "is_active": p["is_active"], "low_stock": p["stock"] <= LOW_STOCK,
                     "updated_at": p["updated_at"]} for p in data["rows"]]
    return respond({"msg": "", "status": 200, "data": data})


async def _product_form(request: Request, partial: bool) -> tuple:
    from models.model import ProductFormModel, ProductPatchModel

    data, form = await read_form(request, ProductPatchModel if partial else ProductFormModel)
    images = []
    for f in form.getlist("images"):
        path = await save_upload(f, "products", images_only=True)
        if path:
            images.append(path)
    return {k: v for k, v in data.items() if v is not None}, images


@shopApi.post("/admin/products", response_model=R.Envelope[R.IdOnly], status_code=201)
async def add_product(request: Request, admin=Depends(tokenRequired(["ADMIN"]))):
    """Form: name, description, category, mrp (₹), stock, is_active; files: images (any number)."""
    data, images = await _product_form(request, partial=False)
    async with cpo.transaction() as conn:
        pid = await cpo.insertion("products", {"name": data["name"], "description": data.get("description", ""),
                                               "category": data.get("category", ""), "mrp": to_paise(data["mrp"]),
                                               "stock": data.get("stock", 0), "images": images,
                                               "is_active": data.get("is_active", True)}, conn=conn)
        await audit(conn, admin["id"], "product_add", str(pid), {"name": data["name"], "mrp": data["mrp"]})
    return respond({"msg": "Product added.", "status": 201, "data": {"id": pid}})


@shopApi.patch("/admin/products/{productId}", response_model=R.Envelope[R.NoData])
async def edit_product(productId: int, request: Request, admin=Depends(tokenRequired(["ADMIN"]))):
    """Any of: name, description, category, mrp (₹), is_active, stock_add (+/- count, never below 0),
    keep_images (JSON list of paths to keep, in order); files: images (added at the end)."""
    data, new_images = await _product_form(request, partial=True)
    async with cpo.transaction() as conn:
        p = await cpo.find_one("products", {"id": productId}, conn=conn, for_update=True)
        if not p:
            fail(404, "Product not found.")
        upd: Dict[str, Any] = {k: data[k] for k in ("name", "description", "category", "is_active") if k in data}
        if "mrp" in data:
            upd["mrp"] = to_paise(data["mrp"])
        images = p["images"]
        if "keep_images" in data:
            images = [i for i in data["keep_images"] if i in p["images"]]
        if new_images or "keep_images" in data:
            upd["images"] = images + new_images
        if upd:
            await cpo.updating("products", {"id": productId}, {**upd, "updated_at": now_utc()}, conn=conn)
        if data.get("stock_add"):
            if p["stock"] + data["stock_add"] < 0:
                fail(400, f"Stock cannot go below 0 (now {p['stock']}).")
            await cpo.execute("UPDATE products SET stock = stock + $2 WHERE id = $1", productId, data["stock_add"], conn=conn)
        await audit(conn, admin["id"], "product_edit", str(productId),
                    {"changes": {k: v for k, v in data.items() if k != "keep_images"}, "images": len(upd.get("images", images))})
    return respond({"msg": "Product saved.", "status": 200})


# ================================================================ admin: orders
@shopApi.get("/admin/orders", response_model=R.Envelope[R.AdminOrdersPage])
async def admin_orders(status: Optional[str] = Query(None, pattern="^(placed|packed|shipped|delivered|cancelled)$"),
                       courier_status: Optional[str] = Query(None, pattern="^(pending|approved|rejected)$"),
                       refund_due: Optional[bool] = None, search: Optional[str] = Query(None, max_length=40),
                       cursor: Optional[str] = Query(None, max_length=200), page: int = 1, limit: int = 20,
                       admin=Depends(tokenRequired(["ADMIN"]))):
    sql = "SELECT o.*, u.user_code, u.full_name, o.id AS _k FROM orders o JOIN users u ON u.id = o.user_id WHERE TRUE"
    args: List[Any] = []
    for col, val in (("o.status", status), ("o.courier_status", courier_status)):
        if val:
            args.append(val)
            sql += f" AND {col} = ${len(args)}"
    if refund_due:
        sql += " AND o.courier_refund_due AND o.courier_refunded_at IS NULL"
    if search:
        args.append(search.strip().upper())
        sql += f" AND (o.order_no = ${len(args)} OR u.user_code = ${len(args)} OR o.courier_utr = ${len(args)})"
    data = await cpo.paginate(sql, *args, desc=False, page=page, limit=limit, cursor=cursor)
    items = await _order_rows("order_id = ANY($1::bigint[])", [o["id"] for o in data["rows"]])
    data["rows"] = [_order_out(o, [i for i in items if i["order_id"] == o["id"]], admin=True) for o in data["rows"]]
    data["counts"] = await cpo.fetch_one(
        "SELECT count(*) FILTER (WHERE status = 'placed' AND courier_status = 'approved') AS to_pack, "
        "count(*) FILTER (WHERE status = 'placed' AND courier_status = 'pending') AS payment_to_check, "
        "count(*) FILTER (WHERE status IN ('packed', 'shipped')) AS on_the_way, "
        "count(*) FILTER (WHERE status = 'delivered' AND delivered_at >= $1) AS delivered_this_month, "
        "count(*) FILTER (WHERE courier_refund_due AND courier_refunded_at IS NULL) AS courier_refunds_due, "
        "(SELECT count(*) FROM products WHERE is_active AND stock <= $2) AS low_stock FROM orders",
        period_start("month"), LOW_STOCK)
    return respond({"msg": "", "status": 200, "data": data})


@shopApi.get("/admin/orders/{orderId}", response_model=R.Envelope[R.AdminOrder])
async def admin_order(orderId: int, admin=Depends(tokenRequired(["ADMIN"]))):
    o = await _load_order(orderId)
    return respond({"msg": "", "status": 200, "data": _order_out(o, await _order_rows("order_id = $1", orderId), admin=True)})


@shopApi.post("/admin/orders/{orderId}/courier-review", response_model=R.Envelope[R.NoData])
async def courier_review(orderId: int, body: CourierReviewModel, admin=Depends(tokenRequired(["ADMIN"]))):
    async with cpo.transaction() as conn:
        o = await _load_order(orderId, conn, lock=True)
        if o["status"] != "placed" or o["courier_status"] != "pending":
            fail(409, "This delivery payment is already checked.")
        await cpo.execute(
            "UPDATE orders SET courier_status = $2, courier_remark = $3, courier_reviewed_by = $4, "
            "courier_rejected_at = CASE WHEN $2 = 'rejected' THEN now() END WHERE id = $1",
            orderId, "approved" if body.action == "approve" else "rejected", body.remark, admin["id"], conn=conn)
        await audit(conn, admin["id"], f"courier_{body.action}", o["order_no"], {"utr": o["courier_utr"], "remark": body.remark})
    return respond({"msg": "Delivery payment approved." if body.action == "approve"
                    else f"Rejected. The member has {COURIER_FIX_DAYS} days to send a new payment.", "status": 200})


NEXT_STATUS = {"packed": "placed", "shipped": "packed", "delivered": "shipped"}


@shopApi.post("/admin/orders/{orderId}/status", response_model=R.Envelope[R.NoData])
async def order_status(orderId: int, body: OrderStatusModel, admin=Depends(tokenRequired(["ADMIN"]))):
    async with cpo.transaction() as conn:
        o = await _load_order(orderId, conn, lock=True)
        if body.status == "cancelled":
            if o["status"] not in ("placed", "packed"):
                fail(409, f"A {o['status']} order cannot be cancelled.")
            await cancel_order(conn, o, admin["id"], body.remark)
        else:
            if o["status"] != NEXT_STATUS[body.status]:
                fail(409, f"Order is {o['status']}; it cannot become {body.status}.")
            if body.status == "packed" and o["courier_status"] != "approved":
                fail(409, "Approve the delivery payment first.")
            if body.status == "shipped" and not (body.tracking_no and body.courier_name):
                fail(400, "Enter the courier name and tracking number.")
            await cpo.execute(
                f"UPDATE orders SET status = $2, {body.status}_at = now(), "
                "courier_name = coalesce($3, courier_name), tracking_no = coalesce($4, tracking_no) WHERE id = $1",
                orderId, body.status, body.courier_name, body.tracking_no, conn=conn)
        await audit(conn, admin["id"], f"order_{body.status}", o["order_no"], {"remark": body.remark})
    return respond({"msg": f"Order {body.status}.", "status": 200})


@shopApi.post("/admin/orders/{orderId}/courier-refunded", response_model=R.Envelope[R.NoData])
async def courier_refunded(orderId: int, body: OrderUtrModel, admin=Depends(tokenRequired(["ADMIN"]))):
    """The admin sent the delivery money back from the bank: record its UTR."""
    async with cpo.transaction() as conn:
        o = await _load_order(orderId, conn, lock=True)
        if not o["courier_refund_due"] or o["courier_refunded_at"]:
            fail(409, "No delivery refund is due for this order.")
        await cpo.execute("UPDATE orders SET courier_refunded_at = now(), courier_refund_utr = $2 WHERE id = $1",
                          orderId, body.courier_utr, conn=conn)
        await audit(conn, admin["id"], "courier_refunded", o["order_no"], {"utr": body.courier_utr})
    return respond({"msg": "Delivery refund saved.", "status": 200})


# ================================================================ admin: courier rates
@shopApi.get("/admin/courier/rates", response_model=R.Envelope[R.CourierRates])
async def courier_rates(admin=Depends(tokenRequired(["ADMIN"]))):
    """Pincode rates the admin set, and the 'rate not set' list (pincodes from orders never reviewed)."""
    s = (await get_settings())["shop"]["courier"]
    own = await cpo.fetch_all("SELECT r.pincode, r.rate, r.updated_at, u.full_name AS updated_by_name FROM pincode_rates r "
                              "LEFT JOIN users u ON u.id = r.updated_by ORDER BY r.pincode")
    unset = await cpo.fetch_all(
        "SELECT o.address->>'pincode' AS pincode, count(*) AS orders, max(o.created_at) AS last_order, "
        "max(o.courier_state) AS state FROM orders o LEFT JOIN pincode_rates r ON r.pincode = o.address->>'pincode' "
        "WHERE r.pincode IS NULL GROUP BY 1 ORDER BY 3 DESC LIMIT 500")
    return respond({"msg": "", "status": 200, "data": {
        "state_rates": {k: rupees(v) for k, v in s.items()},
        "pincode_rates": [{**r, "rate": rupees(r["rate"]) if r["rate"] is not None else None} for r in own],
        "rate_not_set": unset}})


@shopApi.put("/admin/courier/rates/{pincode}", response_model=R.Envelope[R.NoData])
async def set_courier_rate(pincode: str, body: CourierRateModel, admin=Depends(tokenRequired(["ADMIN"]))):
    """rate in ₹, or null = use the state rate (and take it off the 'rate not set' list)."""
    if not PIN_RE.match(pincode):
        fail(400, "Enter a valid 6 digit PIN code.")
    if body.rate is not None and float(body.rate) == 0:
        # Q4 (Vinay, 16 Oct): no free delivery for now; checkout always asks for the delivery UTR.
        fail(400, "Delivery charge must be at least ₹1. Free delivery is not allowed yet.")
    rate = to_paise(body.rate) if body.rate is not None else None
    async with cpo.transaction() as conn:
        old = await cpo.fetch_one("SELECT rate FROM pincode_rates WHERE pincode = $1", pincode, conn=conn)
        await cpo.execute("INSERT INTO pincode_rates (pincode, rate, updated_by) VALUES ($1, $2, $3) ON CONFLICT (pincode) "
                          "DO UPDATE SET rate = EXCLUDED.rate, updated_by = EXCLUDED.updated_by, updated_at = now()",
                          pincode, rate, admin["id"], conn=conn)
        await audit(conn, admin["id"], "courier_rate", pincode, {"old": old["rate"] if old else "not set", "new": rate})
    return respond({"msg": "Delivery charge saved.", "status": 200})
