"""Loads India Post's pincode list into the `pincodes` table (courier state for each pincode).

Download the official "All India Pincode Directory" CSV (data.gov.in), then:

    venv/bin/python scripts/import_pincodes.py path/to/pincode.csv

The CSV needs a pincode column and a state column (India Post names them `pincode` and `statename`;
`district`/`Districtname` is used when present). Running it again replaces the table. A pincode that the file
lists under two different states is reported and left out (the shop then uses its default rate and the
admin's "rate not set" list), so a wrong state never lowers a delivery charge silently.
"""

import asyncio
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PIN = re.compile(r"^[1-9]\d{5}$")
SMALL = {"and", "of"}


def state_name(raw: str) -> str:
    words = re.sub(r"\s+", " ", raw.strip().replace("&", " and ")).lower().split(" ")
    return " ".join(w if w in SMALL and i else w.capitalize() for i, w in enumerate(words))


def read(path: Path):
    states, districts = defaultdict(set), {}
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        cols = {c.lower().strip(): c for c in reader.fieldnames or []}
        pin_col = cols.get("pincode")
        state_col = cols.get("statename") or cols.get("state")
        dist_col = cols.get("districtname") or cols.get("district")
        if not pin_col or not state_col:
            sys.exit(f"CSV needs 'pincode' and 'statename' columns; found: {reader.fieldnames}")
        for row in reader:
            pin = (row.get(pin_col) or "").strip()
            st = (row.get(state_col) or "").strip()
            if PIN.match(pin) and st and st.upper() != "NA":
                states[pin].add(state_name(st))
                if dist_col and row.get(dist_col):
                    districts.setdefault(pin, row[dist_col].strip().title())
    return states, districts


async def main(path: Path):
    from base import cpo

    states, districts = read(path)
    clean = [(p, next(iter(s)), districts.get(p)) for p, s in states.items() if len(s) == 1]
    mixed = sorted(p for p, s in states.items() if len(s) > 1)
    async with cpo.transaction() as conn:
        await conn.execute("DELETE FROM pincodes")
        await conn.copy_records_to_table("pincodes", records=clean, columns=["pincode", "state", "district"])
    await cpo.close()
    print(f"imported {len(clean)} pincodes; {len(mixed)} listed under two states were left out"
          + (f": {', '.join(mixed[:20])}{' ...' if len(mixed) > 20 else ''}" if mixed else ""))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(Path(sys.argv[1])))
