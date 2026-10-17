"""Run once after deploy (safe to run again): python seed.py

- Applies new files from migrations/ in order (each one only once).
- Adds default plan settings, the first admin, Super 3 company spots and complaint categories.
"""

import asyncio
from pathlib import Path

from base import *

MIGRATIONS = Path(__file__).parent / "migrations"
CATEGORIES = ["Payment / UTR issue", "Withdrawal issue", "Income not received", "PIN issue",
              "Product / order issue", "Login / account issue", "Fraud / cheating", "Other"]


def admin_password_problem(password: str, mobile: str) -> str:
    """The first admin controls all the money: on the live server its password must be strong. "" = fine."""
    import re

    if len(password) < 12:
        return "at least 12 characters"
    if not (re.search(r"[a-z]", password) and re.search(r"[A-Z]", password) and re.search(r"\d", password)
            and re.search(r"[^A-Za-z0-9]", password)):
        return "small and capital letters, a number and a symbol"
    low = password.lower()
    if mobile and mobile in password or any(w in low for w in ("treeway", "admin", "password", "12345", "qwerty")):
        return "no mobile number, 'treeway', 'admin', 'password' or easy sequences"
    return ""


async def run_migrations(conn):
    await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, "
                       "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name in done:
            continue
        async with conn.transaction():
            await conn.execute(f.read_text())
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", f.name)
        logger.info("migration applied: %s", f.name)


async def initialize_database():
    await cpo.connect()
    async with cpo.transaction() as conn:
        # One seed at a time, even if two processes start together.
        await cpo.advisory_lock(conn, "seed")
        await run_migrations(conn)

        for key, value in DEFAULT_SETTINGS.items():
            await conn.execute("INSERT INTO app_settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING",
                               key, value)

        if not await conn.fetchval("SELECT 1 FROM users WHERE role = 'ADMIN' LIMIT 1"):
            mobile, email, password = (os.environ.get("ADMIN_MOBILE"), os.environ.get("ADMIN_EMAIL"),
                                       os.environ.get("ADMIN_PASSWORD"))
            if not (mobile and email and password):
                raise RuntimeError("Set ADMIN_MOBILE, ADMIN_EMAIL and ADMIN_PASSWORD in config/.env")
            if os.environ.get("APP_ENV") == "production" and (why := admin_password_problem(password, mobile)):
                raise RuntimeError(f"ADMIN_PASSWORD is too weak for the live server: use {why}.")
            # Admin is the root sponsor: the first members join with this ID's link.
            admin_id = await conn.fetchval(
                "INSERT INTO users (user_code, full_name, mobile, email, password_hash, role, status, activated_at) "
                "VALUES ('TW1000000', 'Treeway', $1, $2, $3, 'ADMIN', 'active', now()) RETURNING id",
                mobile, email.lower(), hash_password(password))
            await conn.execute("INSERT INTO wallets (user_id) VALUES ($1)", admin_id)
            logger.info("admin created: TW1000000 / %s", mobile)

        boards = DEFAULT_SETTINGS["super3"]["boards"]
        for board in range(1, boards + 1):
            if await conn.fetchval("SELECT 1 FROM super3_counters WHERE board = $1", board):
                continue
            jid = await conn.fetchval("INSERT INTO super3_journeys (user_id, kind, current_board) "
                                      "VALUES (NULL, 'company', $1) RETURNING id", board)
            await conn.execute("INSERT INTO super3_nodes (board, position, journey_id, user_id) VALUES ($1, 1, $2, NULL)",
                               board, jid)
            await conn.execute("INSERT INTO super3_counters (board, next_position) VALUES ($1, 2)", board)

        await conn.execute("INSERT INTO complaint_categories (name) SELECT unnest($1::text[]) ON CONFLICT (name) DO NOTHING",
                           CATEGORIES)
    clear_settings_cache()
    logger.info("seed done")


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    async def _main():
        try:
            await initialize_database()
        finally:
            await cpo.close()
            await redis_client.aclose()

    asyncio.run(_main())
