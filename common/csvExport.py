"""CSV downloads for the admin (Vinay, 15 Oct 2026).

- Streamed: rows come from a Postgres server-side cursor 1,000 at a time and go straight out, so even
  lakhs of rows never sit in memory.
- Safe in Excel: a text cell that starts with = + - @ (or tab / carriage return) gets a leading ' so Excel
  never runs it as a formula. Numbers stay numbers.
- UTF-8 with a BOM so Excel shows ₹ and Hindi names correctly.
"""

import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from fastapi.responses import StreamingResponse

from base import IST, cpo

DANGEROUS_START = ("=", "+", "-", "@", "\t", "\r")
BATCH = 500


def safe_cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float, Decimal)):
        return v
    if isinstance(v, datetime):
        return v.astimezone(IST).strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    return "'" + s if s.startswith(DANGEROUS_START) else s


def _lines(rows: List[List[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow([safe_cell(c) for c in r])
    return buf.getvalue()


def stream_csv(filename: str, header: List[str], sql: str, *args,
               row: Optional[Callable[[Dict], List[Any]]] = None) -> StreamingResponse:
    """`sql` columns are written in order (or `row(record)` builds the line)."""

    async def gen():
        yield "﻿" + _lines([header])
        await cpo.connect()
        async with cpo.pool.acquire() as conn:
            async with conn.transaction():              # a server-side cursor lives inside a transaction
                batch: List[List[Any]] = []
                async for rec in conn.cursor(sql, *args, prefetch=1000):
                    # list(rec) keeps every column even when two have the same name (dict() would merge them).
                    batch.append(row(dict(rec)) if row else list(rec))
                    if len(batch) >= BATCH:
                        yield _lines(batch)
                        batch = []
                if batch:
                    yield _lines(batch)

    return StreamingResponse(gen(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                      "Cache-Control": "no-store"})


CSV_RESPONSE = {200: {"description": "CSV file (UTF-8, opens in Excel)", "content": {"text/csv": {}}}}
