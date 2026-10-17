"""Demo server with realistic sample data, for showing Treeway in meetings (Vinay, 15 Oct 2026).

    venv/bin/python scripts/demo_server.py               # reset treeway_demo, fill it, serve on :8102
    venv/bin/python scripts/demo_server.py --no-serve    # reset + fill only
    venv/bin/python scripts/demo_server.py --members 2000

Safety: it only ever touches the database `treeway_demo` on this machine. It refuses to run when
APP_ENV=production, when the target is not treeway_demo on localhost, and it checks current_database()
after connecting. Every API answer carries the header X-Treeway-Demo: 1 and /config/public has demo=true.

Logins (password Demo@1234 for all; admin and doctor also need the OTP printed in this console, or
GET /api/v1/dev/last-otp):
    admin   mobile 9000000000
    member  mobile 9876543210 (Suresh Yadav, has a team)
    doctor  mobile 9123456780
"""

import argparse
import asyncio
import os
import random
import secrets
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DB_NAME = "treeway_demo"
PORT = 8102
PASSWORD = "Demo@1234"
ENV = {
    "APP_ENV": "demo",
    "DATABASE_URL": f"postgresql://treeway:treeway@localhost:5432/{DB_NAME}",
    "REDIS_URL": "redis://localhost:6379/7",
    "CORS_ORIGINS": "http://localhost:5173,http://localhost:5174",
    "FRONTEND_URL": "http://localhost:5173",
    "SECRET_KEY": secrets.token_urlsafe(48),
    "SMTP_HOST": "",
    "RATE_LIMIT_MULTIPLIER": "5",
    "ADMIN_MOBILE": "9000000000", "ADMIN_EMAIL": "admin@demo.treeway", "ADMIN_PASSWORD": PASSWORD,
}

FIRST = ["Suresh", "Amit", "Neha", "Pooja", "Rakesh", "Kiran", "Vikas", "Sunita", "Ramesh", "Anita", "Deepak", "Priya",
         "Manoj", "Rekha", "Ajay", "Kavita", "Sanjay", "Meena", "Rahul", "Seema", "Arun", "Geeta", "Vijay", "Asha"]
LAST = ["Yadav", "Singh", "Gupta", "Mishra", "Kumar", "Devi", "Tiwari", "Verma", "Pandey", "Sharma", "Chauhan", "Maurya"]
PLACES = [("Uttar Pradesh", "Kushinagar"), ("Uttar Pradesh", "Deoria"), ("Uttar Pradesh", "Gorakhpur"),
          ("Uttar Pradesh", "Maharajganj"), ("Bihar", "Siwan"), ("Bihar", "Gopalganj"), ("Uttar Pradesh", "Basti")]
PRODUCTS = [("Tulsi drops", 1000, "Health"), ("Neem soap", 250, "Personal care"), ("Amla juice", 500, "Health"),
            ("Ashwagandha churna", 750, "Health"), ("Aloe vera gel", 400, "Personal care"), ("Herbal tea", 300, "Home")]


def check_target(dsn: str, app_env: str):
    """Refuses anything but treeway_demo on this machine."""
    if app_env == "production":
        raise SystemExit("[demo] refusing to run with APP_ENV=production")
    u = urlparse(dsn)
    name = (u.path or "").lstrip("/")
    if name != DB_NAME:
        raise SystemExit(f"[demo] refusing: target database is {name!r}, only {DB_NAME!r} is allowed")
    if u.hostname not in (None, "", "localhost", "127.0.0.1", "::1"):
        raise SystemExit(f"[demo] refusing: database host {u.hostname!r} is not this machine")


async def reset_database():
    import asyncpg

    check_target(ENV["DATABASE_URL"], os.environ.get("APP_ENV_BEFORE", ""))
    try:
        conn = await asyncpg.connect(os.environ.get("DEMO_PG_ADMIN_DSN", "postgresql:///postgres"), timeout=5)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"[demo] cannot connect to Postgres: {e}. Is it running?")
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {DB_NAME} OWNER treeway")
    finally:
        await conn.close()


async def seed_demo(members: int = 500, seed: int = 7) -> dict:
    """Fills the CURRENT database (already migrated + seeded) with realistic data through the real engine."""
    from base import cpo, hash_password_async, new_user_code, today_ist
    from mlmEngine import activate_user, issue_pins, wallet_move
    from singleLeg import run_single_leg
    from rankStats import run_rank_stats

    rnd = random.Random(seed)
    pw = await hash_password_async(PASSWORD)
    root = await cpo.fetch_val("SELECT id FROM users WHERE user_code = 'TW1000000'")
    ids, used_mobiles = [], set()

    async def new_member(sponsor_id, name=None, mobile=None, active=True, days_ago=0):
        while True:
            mob = mobile or f"{rnd.choice('6789')}{rnd.randint(0, 999999999):09d}"
            if mob not in used_mobiles:
                used_mobiles.add(mob)
                break
        state, district = rnd.choice(PLACES)
        async with cpo.transaction() as conn:
            uid = await cpo.insertion("users", {
                "user_code": new_user_code(), "sponsor_id": sponsor_id,
                "full_name": name or f"{rnd.choice(FIRST)} {rnd.choice(LAST)}", "mobile": mob,
                "email": f"{mob}@demo.treeway", "password_hash": pw, "state": state, "district": district}, conn=conn)
            await conn.execute("INSERT INTO user_uplines (user_id, level, ancestor_id) SELECT $1::bigint, 1, $2::bigint "
                               "UNION ALL SELECT $1, level + 1, ancestor_id FROM user_uplines WHERE user_id = $2 AND level < 25",
                               uid, sponsor_id)
            await cpo.insertion("wallets", {"user_id": uid}, conn=conn, returning=None)
            if active:
                await activate_user(conn, uid, "pin", f"demo:{uid}")
        return uid

    suresh = await new_member(root, "Suresh Yadav", "9876543210")
    ids.append(suresh)
    for i in range(members - 1):
        # Bushy near the top, deeper further down: 40% under Suresh's recent team, the rest anywhere.
        sponsor = rnd.choice(ids[-60:]) if rnd.random() < 0.6 else rnd.choice(ids)
        ids.append(await new_member(sponsor, active=rnd.random() > 0.06))
    # Spread joining dates over the last 60 days (newest last), so charts and "joined today" look real.
    await cpo.execute(
        "UPDATE users u SET created_at = now() - make_interval(days => (60 * (1 - x.r))::int), "
        "activated_at = CASE WHEN activated_at IS NULL THEN NULL ELSE now() - make_interval(days => (60 * (1 - x.r))::int) END "
        "FROM (SELECT id, row_number() OVER (ORDER BY id)::float / count(*) OVER () AS r FROM users WHERE role = 'USER') x "
        "WHERE x.id = u.id")

    # Money activity: PINs, add-fund and activation requests, withdrawals, P2P-free extras.
    async with cpo.transaction() as conn:
        await issue_pins(conn, suresh, 3, "payment")
    some = rnd.sample(ids[1:], min(40, len(ids) - 1))
    for n, uid in enumerate(some[:6]):
        await cpo.insertion("payment_requests", {"user_id": uid, "purpose": "fund", "amount": 200000, "utr": f"DEMOUTR{n:05d}",
                                                 "receipt_path": "receipts/demo.png", "note": "Paid with PhonePe"}, returning=None)
    for n, uid in enumerate(some[6:16]):
        bank = {"holder": "Demo Member", "account": f"30210045{n:06d}", "ifsc": "SBIN0001234", "branch": "Padrauna",
                "pan": f"ABCPY{1000 + n}K"}
        await cpo.execute("UPDATE users SET pan = $2, bank_holder = 'Demo Member', bank_account = $3, bank_ifsc = 'SBIN0001234' "
                          "WHERE id = $1", uid, bank["pan"], bank["account"])
        bal = await cpo.fetch_val("SELECT income FROM wallets WHERE user_id = $1", uid)
        if bal >= 20000:
            async with cpo.transaction() as conn:
                wid = await cpo.insertion("withdrawals", {"user_id": uid, "amount": 20000, "bank": bank,
                                                          "status": "paid" if n % 2 else "pending",
                                                          "utr": f"DEMOBANK{n:04d}" if n % 2 else None}, conn=conn)
                await wallet_move(conn, uid, "income", -20000, "withdrawal", f"wd:{wid}", {"withdrawal_id": wid})
                if n % 2:
                    await cpo.execute("UPDATE withdrawals SET reviewed_at = now() WHERE id = $1", wid, conn=conn)
                    await cpo.execute("UPDATE wallets SET total_withdrawn = total_withdrawn + 20000 WHERE user_id = $1",
                                      uid, conn=conn)

    # Shop, doctor, complaints, videos.
    pids = [await cpo.insertion("products", {"name": n, "mrp": m * 100, "stock": rnd.randint(5, 120), "category": c})
            for n, m, c in PRODUCTS]
    buyers = [r["user_id"] for r in await cpo.fetch_all(
        "SELECT user_id FROM wallets w JOIN users u ON u.id = w.user_id WHERE u.status = 'active' AND u.role = 'USER' "
        "AND w.product >= 50000 ORDER BY user_id LIMIT 200")]
    for n, uid in enumerate(rnd.sample(buyers, min(10, len(buyers)))):
        p = rnd.choice(pids[1:3])          # ₹250 or ₹500: inside the ₹987 Product Wallet
        prod = await cpo.fetch_one("SELECT name, mrp FROM products WHERE id = $1", p)
        mrp = prod["mrp"]
        async with cpo.transaction() as conn:
            oid = await cpo.insertion("orders", {
                "order_no": "TW-ORD-" + str(await cpo.fetch_val("SELECT nextval('order_no_seq')", conn=conn)),
                "user_id": uid, "items_total": mrp, "paid_product": mrp, "courier_charge": 9000, "courier_state": "Uttar Pradesh",
                "courier_utr": f"DEMOCOUR{n:04d}", "courier_receipt": "receipts/demo.png",
                "courier_status": "approved" if n % 3 else "pending",
                "status": ("placed", "packed", "shipped", "delivered")[n % 4],
                "address": {"name": "Demo", "mobile": "9876543210", "address": "Ward 4, Padrauna", "pincode": "274304"}},
                conn=conn)
            await cpo.insertion("order_items", {"order_id": oid, "product_id": p, "name": prod["name"], "mrp": mrp, "qty": 1},
                                conn=conn, returning=None)
            await cpo.execute("UPDATE products SET stock = stock - 1 WHERE id = $1", p, conn=conn)
            await wallet_move(conn, uid, "product", -mrp, "shop_order", f"order:{oid}:product", {})

    doctor = await cpo.insertion("users", {"user_code": new_user_code(), "full_name": "Dr. Asha Verma", "mobile": "9123456780",
                                           "email": "doctor@demo.treeway", "password_hash": pw, "role": "DOCTOR",
                                           "status": "active", "activated_at": today_ist()})
    await cpo.insertion("wallets", {"user_id": doctor}, returning=None)
    await cpo.insertion("doctor_profiles", {"user_id": doctor, "bams_reg_no": "BAMS-UP-DEMO-4521"}, returning=None)
    for n, uid in enumerate(some[26:31]):
        rid = await cpo.insertion("doctor_requests", {"user_id": uid, "doctor_id": doctor, "problem": rnd.choice([
            "Headache for 3 days, worse in the evening.", "Knee pain when climbing stairs.", "Cannot sleep well at night.",
            "Stomach burning after meals.", "Dry cough for a week."]), "age": rnd.randint(22, 65)})
        if n % 2 == 0:
            pres = await cpo.insertion("prescriptions", {"request_id": rid, "doctor_id": doctor, "doctor_name": "Dr. Asha Verma",
                                                         "bams_reg_no": "BAMS-UP-DEMO-4521", "advice": "Drink more water, sleep early."})
            await cpo.insertion("prescription_items", {"prescription_id": pres, "line_no": 1, "product_id": pids[0],
                                                       "name": "Tulsi drops", "dosage": "morning and evening", "days": 15},
                                returning=None)
            await cpo.execute("UPDATE doctor_requests SET status = 'answered', answered_at = now() WHERE id = $1", rid)
    cat = await cpo.fetch_val("SELECT id FROM complaint_categories ORDER BY id LIMIT 1")
    for n, uid in enumerate(some[31:36]):
        cid = await cpo.insertion("complaints", {"complaint_no": f"CMP{today_ist():%y%m}{900000 + n}", "user_id": uid,
                                                 "category_id": cat, "status": ("Submitted", "In Process", "Resolved")[n % 3],
                                                 "incident": {"detail": "Withdrawal is late"}})
        await cpo.insertion("complaint_messages", {"complaint_id": cid, "sender_id": uid,
                                                   "message": "Please check, my money has not come."}, returning=None)
    for day in (today_ist() - timedelta(days=1), today_ist(), today_ist() + timedelta(days=1)):
        for slot, (platform, url, title) in enumerate((
                ("youtube", "https://www.youtube.com/watch?v=demo1", "Benefits of Tulsi"),
                ("facebook", "https://www.facebook.com/watch/?v=demo2", "Treeway product introduction"),
                ("instagram", "https://www.instagram.com/reel/demo3", "Daily health tip"),
                ("youtube", "https://youtu.be/demo4", "Ayurvedic daily routine")), start=1):
            await cpo.execute("INSERT INTO daily_videos (video_date, slot, platform, url, title) VALUES ($1, $2, $3, $4, $5) "
                              "ON CONFLICT DO NOTHING", day, slot, platform, url, title)

    sl = await run_single_leg(today_ist())
    await run_rank_stats(today_ist() + timedelta(days=1))
    return {"members": len(ids), "active": await cpo.fetch_val("SELECT count(*) FROM users WHERE status = 'active' AND role = 'USER'"),
            "single_leg_rows": sl["pool_rows"] if sl else 0,
            "on_boards": await cpo.fetch_val("SELECT count(*) FROM super3_nodes WHERE user_id IS NOT NULL")}


async def build(members: int):
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    from base import cpo, redis_client
    from seed import initialize_database

    if await cpo.fetch_val("SELECT current_database()") != DB_NAME:      # last check, after connecting
        raise SystemExit("[demo] refusing: connected to the wrong database")
    await redis_client.flushdb()
    await initialize_database()
    out = await seed_demo(members)
    await cpo.close()
    await redis_client.aclose()
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-serve", action="store_true")
    parser.add_argument("--members", type=int, default=500)
    args = parser.parse_args()
    os.environ["APP_ENV_BEFORE"] = os.environ.get("APP_ENV", "")
    check_target(ENV["DATABASE_URL"], os.environ["APP_ENV_BEFORE"])
    os.environ.update(ENV)
    asyncio.run(reset_database())
    out = asyncio.run(build(args.members))
    print(f"[demo] {DB_NAME} ready: {out}. Logins: admin 9000000000, member 9876543210, doctor 9123456780 / {PASSWORD}")
    if args.no_serve:
        return
    uvicorn = ROOT / "venv" / "bin" / "uvicorn"
    print(f"[demo] serving http://localhost:{PORT} (APP_ENV=demo, header X-Treeway-Demo: 1)", flush=True)
    os.execve(str(uvicorn), [str(uvicorn), "main:app", "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
              os.environ.copy())


if __name__ == "__main__":
    main()
