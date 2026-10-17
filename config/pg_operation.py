"""Common PostgreSQL helper, used everywhere as `cpo` (same idea as `cmo` for Mongo).

Simple CRUD goes through the helpers (insertion / updating / simple_finding ...).
Anything with joins, locks or money uses raw SQL with $1, $2 parameters.
Never put user input inside an SQL string: always pass it as a parameter.
"""

import base64
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

import asyncpg

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _ident(name: str) -> str:
    """Table / column names cannot be SQL parameters, so we only allow safe names."""
    if not _IDENT.match(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def _where(where: Dict[str, Any], start: int = 1):
    parts, args = [], []
    for col, val in where.items():
        if val is None:
            parts.append(f"{_ident(col)} IS NULL")
        else:
            args.append(val)
            parts.append(f"{_ident(col)} = ${start + len(args) - 1}")
    return (" AND ".join(parts) or "TRUE"), args


class BadCursor(ValueError):
    """The list cursor sent by the app is broken or edited (answered as 400 in main.py)."""


def _encode_cursor(row: Dict[str, Any], with_sort: bool) -> str:
    data: Dict[str, Any] = {"k": row["_k"]}
    if with_sort:
        s = row["_s"]
        data["s"], data["t"] = (s.isoformat(), "ts") if isinstance(s, datetime) else (s, "v")
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, with_sort: bool) -> list:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        k = int(data["k"])
        if not with_sort:
            return [k]
        s = datetime.fromisoformat(data["s"]) if data["t"] == "ts" else data["s"]
        return [s, k]
    except (ValueError, KeyError, TypeError) as e:
        raise BadCursor("Bad cursor") from e


async def _init_connection(conn: asyncpg.Connection):
    # JSONB columns come back as Python dicts and go in as dicts.
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class PgOperations:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        if self.pool is None:
            self.pool = await asyncpg.create_pool(
                dsn=os.environ.get("DATABASE_URL"),
                min_size=int(os.environ.get("DB_POOL_MIN", 2)),
                max_size=int(os.environ.get("DB_POOL_MAX", 10)),
                # API: 30 s. The scheduler sets DB_COMMAND_TIMEOUT higher (night jobs scan lakhs of rows).
                command_timeout=float(os.environ.get("DB_COMMAND_TIMEOUT", 30)),
                # 0 when the API goes through PgBouncer in transaction mode (no prepared statement cache).
                statement_cache_size=int(os.environ.get("DB_STATEMENT_CACHE_SIZE", 100)),
                max_inactive_connection_lifetime=300,
                init=_init_connection,
            )
        return self.pool

    async def close(self):
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    @asynccontextmanager
    async def transaction(self):
        """All-or-nothing block. Use it for anything that moves money."""
        await self.connect()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    @asynccontextmanager
    async def _conn(self, conn=None):
        if conn is not None:
            yield conn
            return
        await self.connect()
        async with self.pool.acquire() as c:
            yield c

    # ------------------------------------------------------------ raw SQL
    async def fetch_one(self, sql: str, *args, conn=None) -> Optional[dict]:
        async with self._conn(conn) as c:
            row = await c.fetchrow(sql, *args)
            return dict(row) if row else None

    async def fetch_all(self, sql: str, *args, conn=None) -> List[dict]:
        async with self._conn(conn) as c:
            rows = await c.fetch(sql, *args)
            return [dict(r) for r in rows]

    async def fetch_val(self, sql: str, *args, conn=None):
        async with self._conn(conn) as c:
            return await c.fetchval(sql, *args)

    async def execute(self, sql: str, *args, conn=None) -> str:
        async with self._conn(conn) as c:
            return await c.execute(sql, *args)

    # ------------------------------------------------------------ helpers
    async def insertion(self, table: str, data: Dict[str, Any], conn=None, returning: str = "id",
                        on_conflict_do_nothing: bool = False):
        cols = [_ident(c) for c in data.keys()]
        params = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
        sql = f"INSERT INTO {_ident(table)} ({', '.join(cols)}) VALUES ({params})"
        if on_conflict_do_nothing:
            sql += " ON CONFLICT DO NOTHING"
        if returning:
            sql += f" RETURNING {_ident(returning)}"
            return await self.fetch_val(sql, *data.values(), conn=conn)
        await self.execute(sql, *data.values(), conn=conn)
        return None

    async def updating(self, table: str, where: Dict[str, Any], data: Dict[str, Any], conn=None) -> int:
        """Returns how many rows changed (0 = nothing matched)."""
        sets = [f"{_ident(c)} = ${i}" for i, c in enumerate(data.keys(), start=1)]
        where_sql, where_args = _where(where, start=len(data) + 1)
        sql = f"UPDATE {_ident(table)} SET {', '.join(sets)} WHERE {where_sql}"
        status = await self.execute(sql, *data.values(), *where_args, conn=conn)
        return int(status.split()[-1])

    async def find_one(self, table: str, where: Dict[str, Any], columns: str = "*", conn=None,
                       for_update: bool = False) -> Optional[dict]:
        where_sql, args = _where(where)
        sql = f"SELECT {columns} FROM {_ident(table)} WHERE {where_sql} LIMIT 1"
        if for_update:
            sql += " FOR UPDATE"
        return await self.fetch_one(sql, *args, conn=conn)

    async def simple_finding(self, table: str, where: Dict[str, Any], columns: str = "*",
                             order_by: str = "id DESC", limit: int = 100, offset: int = 0, conn=None) -> List[dict]:
        where_sql, args = _where(where)
        col, _, direction = order_by.partition(" ")
        direction = "DESC" if direction.upper() == "DESC" else "ASC"
        sql = (f"SELECT {columns} FROM {_ident(table)} WHERE {where_sql} "
               f"ORDER BY {_ident(col)} {direction} LIMIT {int(limit)} OFFSET {int(offset)}")
        return await self.fetch_all(sql, *args, conn=conn)

    async def count(self, table: str, where: Dict[str, Any], conn=None) -> int:
        where_sql, args = _where(where)
        return await self.fetch_val(f"SELECT count(*) FROM {_ident(table)} WHERE {where_sql}", *args, conn=conn)

    async def paginate(self, sql: str, *args, sort: str = "_k", desc: bool = True, page: int = 1, limit: int = 20,
                       cursor: Optional[str] = None, conn=None) -> Dict[str, Any]:
        """Runs a SELECT (no ORDER BY inside) and returns {rows, total, page, limit, next_cursor} in one query.

        The SELECT must give `_k` (a unique id) and, when sort="_s", a sort value `_s`. The order is always
        (sort, _k), so it is the same on every call. Columns starting with "_" are not returned.
        - cursor (from next_cursor): rows after the last row seen. New rows added meanwhile never cause a
          repeated or skipped row. Use it for lists the app scrolls through.
        - page: jump to page N (admin tables). Rows can shift if new ones arrive between calls.
        """
        if sort not in ("_k", "_s"):
            raise ValueError(sort)
        limit = min(max(1, int(limit)), 100)
        page = max(1, int(page))
        direction, cmp = ("DESC", "<") if desc else ("ASC", ">")
        cols = "_s, _k" if sort == "_s" else "_k"
        where, params = "", list(args)
        if cursor:
            values = _decode_cursor(cursor, sort == "_s")
            marks = ", ".join(f"${len(params) + i + 1}" for i in range(len(values)))
            params += values
            where = f"WHERE ({cols}) {cmp} ({marks})"
        order = ", ".join(f"{c} {direction}" for c in cols.split(", "))
        offset = 0 if cursor else (page - 1) * limit
        wrapped = (f"SELECT * FROM (SELECT *, count(*) OVER() AS _total FROM ({sql}) AS q) AS t {where} "
                   f"ORDER BY {order} LIMIT {limit + 1} OFFSET {offset}")
        rows = await self.fetch_all(wrapped, *params, conn=conn)
        if rows and ("_k" not in rows[0] or (sort == "_s" and "_s" not in rows[0])):
            raise ValueError("paginate: the SELECT must return _k (and _s when sort='_s')")
        more = len(rows) > limit
        rows = rows[:limit]
        total = rows[0]["_total"] if rows else 0
        next_cursor = _encode_cursor(rows[-1], sort == "_s") if more else None
        clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
        return {"rows": clean, "total": total, "page": page, "limit": limit, "next_cursor": next_cursor}

    async def advisory_lock(self, conn, key: str):
        """Lock held until the transaction ends. Used to run one Super 3 placement at a time."""
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)


pg_ops = PgOperations()
