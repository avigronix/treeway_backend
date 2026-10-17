"""First-day data for the live server, from files. Read scripts/prod_setup/README.md first.

    venv/bin/python scripts/prod_setup/setup.py status
    venv/bin/python scripts/prod_setup/setup.py company  company.json   [--apply]
    venv/bin/python scripts/prod_setup/setup.py products products.csv   [--apply]
    venv/bin/python scripts/prod_setup/setup.py videos   videos.csv     [--apply]
    venv/bin/python scripts/prod_setup/setup.py doctor --name "Dr. ..." --mobile 9xxxxxxxxx --email d@x.in --bams UP-123 [--apply]

Without --apply nothing is written: it checks every row and prints what WOULD change (dry run).
With --apply the whole file goes in ONE transaction: all rows or none (never half a file).
Running the same file again changes nothing (rows already there are skipped and listed).
The rules are the same as the admin screens (same checks, same audit log entries, by the main admin TW1000000).
The doctor's password is never typed on the command line: DOCTOR_PASSWORD env or a hidden prompt.
"""

import argparse
import asyncio
import csv
import getpass
import json
import os
import shutil
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from base import (IMAGE_FILES, MAX_FILE_BYTES, UPLOAD_ROOT, audit, clear_settings_cache, cpo, get_settings,  # noqa: E402
                  hash_password_async, looks_like, new_user_code, now_utc, redis_client, to_paise, today_ist)
from models.model import DayVideosModel, DoctorCreateModel, ProductFormModel, VideoItemModel  # noqa: E402
from routes.companyApis import PUBLIC_COMPANY_KEYS, _check_company  # noqa: E402
from routes.doctorApis import _assign_waiting  # noqa: E402
from routes.videoApis import _check_link  # noqa: E402

ROOT_ADMIN = "TW1000000"
YES = {"1", "yes", "y", "true", "live", "on"}
NO = {"0", "no", "n", "false", "hidden", "off"}


class Bad(Exception):
    """The file has mistakes: nothing was written. `errors` lists every one (row numbers from the file)."""

    def __init__(self, errors: List[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class _DryRun(Exception):
    pass


def _why(e: Exception) -> str:
    if isinstance(e, HTTPException):
        return e.detail["msg"] if isinstance(e.detail, dict) else str(e.detail)
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(str(p) for p in x['loc'])}: {x['msg'].removeprefix('Value error, ')}"
                         for x in e.errors())
    return str(e)


def _read_csv(path: Path) -> List[Dict[str, str]]:
    """CSV with a header row (Excel: File → Save as → "CSV UTF-8"). Header names are case/space insensitive."""
    if not path.is_file():
        raise Bad([f"File not found: {path}"])
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise Bad([f"{path.name}: no rows under the header."])
    return [{(k or "").strip().lower().replace(" ", "_"): (v or "").strip() for k, v in r.items()} for r in rows]


def _image(src: Path, label: str) -> bytes:
    """The same checks as an upload from the admin screen (type, real picture, 5 MB)."""
    if not src.is_file():
        raise Bad([f"{label}: picture not found: {src}"])
    if src.suffix.lower() not in IMAGE_FILES:
        raise Bad([f"{label}: only JPG, PNG or WEBP pictures."])
    content = src.read_bytes()
    if not content or len(content) > MAX_FILE_BYTES or not looks_like(src.suffix.lower(), content):
        raise Bad([f"{label}: not a real JPG / PNG / WEBP picture of at most 5 MB: {src.name}"])
    return content


class Files:
    """Pictures are written only on --apply, and removed again if the transaction does not commit."""

    def __init__(self):
        self.written: List[Path] = []

    def put(self, sub: str, ext: str, content: bytes) -> str:
        folder = UPLOAD_ROOT / sub
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}{ext}"
        (folder / name).write_bytes(content)
        self.written.append(folder / name)
        return f"{sub}/{name}"

    def undo(self):
        for p in self.written:
            p.unlink(missing_ok=True)


async def _admin_id(conn) -> int:
    aid = await cpo.fetch_val("SELECT id FROM users WHERE user_code = $1 AND role = 'ADMIN'", ROOT_ADMIN, conn=conn)
    if not aid:
        raise Bad(["The main admin TW1000000 does not exist yet. Run seed.py first."])
    return aid


async def _run(apply: bool, work) -> Dict[str, Any]:
    """One transaction for the whole job; a dry run does the same reads and checks, then rolls back."""
    files = Files()
    report: Dict[str, Any] = {"applied": apply, "changes": [], "skipped": []}
    try:
        async with cpo.transaction() as conn:
            await cpo.advisory_lock(conn, "prod_setup")  # two runs at once: the second waits
            await work(conn, await _admin_id(conn), report, files if apply else None)
            if not apply:
                raise _DryRun()
    except _DryRun:
        pass
    except BaseException:
        files.undo()
        raise
    if apply:
        clear_settings_cache()
    return report


# ================================================================ company
async def company(path: Path, apply: bool) -> Dict[str, Any]:
    """company.json: any of name, upi_id, account_holder, account_no, ifsc, branch, email, phone, whatsapp_group,
    and "qr": a picture file (path next to the JSON). Only the keys given change."""
    try:
        given = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Bad([f"{path}: {e}"])
    if not isinstance(given, dict):
        raise Bad(["company.json must be one { ... } object."])
    unknown = set(given) - set(PUBLIC_COMPANY_KEYS) - {"qr"} | ({"qr_path"} & set(given))
    if unknown:
        raise Bad([f"Unknown keys: {', '.join(sorted(unknown))}. Allowed: {', '.join(k for k in PUBLIC_COMPANY_KEYS if k != 'qr_path')}, qr"])
    qr = _image((path.parent / str(given["qr"])), "qr") if given.get("qr") else None

    async def work(conn, admin_id, report, files):
        from base import DEFAULT_SETTINGS
        from routes.companyApis import _save_setting

        current = await cpo.fetch_val("SELECT value FROM app_settings WHERE key = 'company' FOR UPDATE", conn=conn)
        old = {**DEFAULT_SETTINGS["company"], **(current or {})}
        new = dict(old)
        for k, v in given.items():
            if k == "qr":
                continue
            v = " ".join(str(v).split())[:200]  # same clean-up as the admin screen
            new[k] = v.upper() if k == "ifsc" else v.lower() if k in ("upi_id", "email") else v
        try:
            _check_company(new)
        except HTTPException as e:
            raise Bad([_why(e)])
        report["changes"] = [f"{k}: {old.get(k) or '(empty)'} → {new[k] or '(empty)'}"
                             for k in PUBLIC_COMPANY_KEYS if k != "qr_path" and new.get(k) != old.get(k)]
        if qr is not None:
            report["changes"].append("qr: new picture")
        if not report["changes"]:
            report["skipped"].append("company details are already the same")
            return
        if files is not None:
            if qr is not None:
                new["qr_path"] = files.put("company", Path(str(given["qr"])).suffix.lower(), qr)
            await _save_setting("company", new, admin_id, conn)

    return await _run(apply, work)


# ================================================================ products
async def products(path: Path, apply: bool) -> Dict[str, Any]:
    """products.csv columns: name, mrp (₹), stock, category, description, active (yes/no, default yes),
    image (optional picture file next to the CSV; several separated by |). A name already in the shop is skipped."""
    rows = _read_csv(path)
    errors: List[str] = []
    clean: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for n, r in enumerate(rows, start=2):  # row 1 is the header
        try:
            active = (r.get("active") or "yes").lower()
            if active not in YES | NO:
                raise ValueError("active: write yes or no")
            m = ProductFormModel(name=" ".join(r.get("name", "").split()), mrp=r.get("mrp", ""),
                                 stock=r.get("stock") or 0, category=r.get("category") or None,
                                 description=r.get("description") or None, is_active=active in YES)
            mrp = to_paise(m.mrp, "mrp")
            pics = [_image(path.parent / p.strip(), f"row {n} image") for p in (r.get("image") or "").split("|") if p.strip()]
            exts = [Path(p.strip()).suffix.lower() for p in (r.get("image") or "").split("|") if p.strip()]
        except (ValidationError, HTTPException, ValueError) as e:
            errors.append(f"row {n}: {_why(e)}")
            continue
        except Bad as e:
            errors.extend(e.errors)
            continue
        key = m.name.lower()
        if key in seen:
            errors.append(f"row {n}: '{m.name}' is also on row {seen[key]}")
            continue
        seen[key] = n
        clean.append({"row": n, "m": m, "mrp": mrp, "pics": list(zip(exts, pics))})
    if errors:
        raise Bad(errors)

    async def work(conn, admin_id, report, files):
        have = {r["name"].lower() for r in await cpo.fetch_all("SELECT name FROM products", conn=conn)}
        for p in clean:
            m = p["m"]
            if m.name.lower() in have:
                report["skipped"].append(f"row {p['row']}: '{m.name}' is already in the shop (not changed)")
                continue
            report["changes"].append(f"row {p['row']}: add '{m.name}' ₹{m.mrp}, stock {m.stock}"
                                     f"{'' if m.is_active else ' (hidden)'}{f', {len(p['pics'])} picture(s)' if p['pics'] else ''}")
            if files is None:
                continue
            images = [files.put("products", ext, c) for ext, c in p["pics"]]
            pid = await cpo.insertion("products", {"name": m.name, "description": m.description or "",
                                                   "category": m.category or "", "mrp": p["mrp"], "stock": m.stock,
                                                   "images": images, "is_active": m.is_active}, conn=conn)
            await audit(conn, admin_id, "product_add", str(pid), {"name": m.name, "mrp": m.mrp, "by": "setup script"})

    return await _run(apply, work)


# ================================================================ videos
async def videos(path: Path, apply: bool) -> Dict[str, Any]:
    """videos.csv columns: date (YYYY-MM-DD, today or later), slot (1..videos a day), platform
    (youtube / facebook / instagram), url (https), title. A slot already set with the same link is skipped;
    a different link replaces it (like the admin screen)."""
    rows = _read_csv(path)
    per_day = (await get_settings())["video"]["per_day"]
    today = today_ist()
    errors: List[str] = []
    by_day: Dict[date, List[Dict[str, Any]]] = {}
    seen: Dict[tuple, int] = {}
    for n, r in enumerate(rows, start=2):
        try:
            d = date.fromisoformat(r.get("date", ""))
            v = VideoItemModel(slot=r.get("slot") or 0, platform=(r.get("platform") or "").lower(),
                               url=r.get("url", ""), title=r.get("title", ""))
            _check_link(v.platform, v.url)
        except (ValidationError, HTTPException, ValueError) as e:
            errors.append(f"row {n}: {_why(e)}")
            continue
        if d < today:
            errors.append(f"row {n}: {d} is a past day (cannot be changed)")
            continue
        if v.slot > per_day:
            errors.append(f"row {n}: slot {v.slot}, but there are {per_day} videos a day")
            continue
        if (d, v.slot) in seen:
            errors.append(f"row {n}: {d} slot {v.slot} is also on row {seen[(d, v.slot)]}")
            continue
        seen[(d, v.slot)] = n
        by_day.setdefault(d, []).append({"row": n, "v": v})
    if errors:
        raise Bad(errors)

    async def work(conn, admin_id, report, files):
        for d in sorted(by_day):
            DayVideosModel(videos=[x["v"] for x in by_day[d]])  # same limits as the screen (1-10 a day)
            old = {r["slot"]: r for r in await cpo.fetch_all(
                "SELECT slot, platform, url, title FROM daily_videos WHERE video_date = $1", d, conn=conn)}
            changed = []
            for x in by_day[d]:
                v = x["v"]
                o = old.get(v.slot)
                if o and (o["platform"], o["url"], o["title"]) == (v.platform, v.url, v.title):
                    report["skipped"].append(f"row {x['row']}: {d} slot {v.slot} already set")
                    continue
                report["changes"].append(f"row {x['row']}: {d} slot {v.slot} {'replace' if o else 'set'} {v.platform} '{v.title}'")
                changed.append(v)
            if files is None or not changed:
                continue
            for v in changed:
                await cpo.execute(
                    "INSERT INTO daily_videos (video_date, slot, platform, url, title, updated_by) VALUES ($1, $2, $3, $4, $5, $6) "
                    "ON CONFLICT (video_date, slot) DO UPDATE SET platform = EXCLUDED.platform, url = EXCLUDED.url, "
                    "title = EXCLUDED.title, updated_by = EXCLUDED.updated_by, updated_at = now()",
                    d, v.slot, v.platform, v.url, v.title, admin_id, conn=conn)
            await audit(conn, admin_id, "videos_set", str(d), {"old": list(old.values()), "new": [v.model_dump() for v in changed],
                                                              "by": "setup script"})
        missing = []
        for i in range(7):
            d = date.fromordinal(today.toordinal() + i)
            have = await cpo.fetch_val("SELECT count(*) FROM daily_videos WHERE video_date = $1 AND slot <= $2",
                                       d, per_day, conn=conn)
            if have < per_day:
                missing.append(f"{d} ({per_day - have} missing)")
        report["next_7_days_missing"] = missing

    return await _run(apply, work)


# ================================================================ doctor
async def doctor(name: str, mobile: str, email: str, bams: str, password: str, apply: bool) -> Dict[str, Any]:
    """Adds a doctor like Admin → Doctors → Add. A mobile that is already a doctor is skipped."""
    try:
        body = DoctorCreateModel(full_name=name, mobile=mobile, email=email, password=password, bams_reg_no=bams)
    except ValidationError as e:
        raise Bad([_why(e)])

    async def work(conn, admin_id, report, files):
        u = await cpo.fetch_one("SELECT user_code, role FROM users WHERE mobile = $1", body.mobile, conn=conn)
        if u and u["role"] == "DOCTOR":
            report["skipped"].append(f"{body.mobile} is already doctor {u['user_code']} (not changed)")
            return
        if u:
            raise Bad([f"{body.mobile} is already used by {u['user_code']} ({u['role']}). Use another mobile."])
        report["changes"].append(f"add doctor {body.full_name} ({body.mobile}, {body.email}, BAMS {body.bams_reg_no})")
        if files is None:
            return
        for _ in range(5):
            code = new_user_code()
            if not await cpo.fetch_val("SELECT 1 FROM users WHERE user_code = $1", code, conn=conn):
                break
        uid = await cpo.insertion("users", {
            "user_code": code, "full_name": body.full_name, "mobile": body.mobile, "email": body.email,
            "password_hash": await hash_password_async(body.password), "role": "DOCTOR", "status": "active",
            "activated_at": now_utc()}, conn=conn)
        await cpo.insertion("wallets", {"user_id": uid}, conn=conn, returning=None)
        await cpo.insertion("doctor_profiles", {"user_id": uid, "bams_reg_no": body.bams_reg_no, "created_by": admin_id},
                            conn=conn, returning=None)
        await audit(conn, admin_id, "doctor_add", code, {"name": body.full_name, "bams_reg_no": body.bams_reg_no,
                                                        "by": "setup script"})
        waiting = [r["id"] for r in await cpo.fetch_all(
            "SELECT id FROM doctor_requests WHERE doctor_id IS NULL AND status = 'waiting' ORDER BY id", conn=conn)]
        await _assign_waiting(conn, waiting, uid, admin_id, "first doctor")
        report["doctor_code"] = code

    return await _run(apply, work)


# ================================================================ status
async def status() -> Dict[str, Any]:
    """What is still missing before members come (read only)."""
    s = await get_settings()
    c = s["company"]
    today = today_ist()
    per_day = s["video"]["per_day"]
    video_days = await cpo.fetch_all(
        "SELECT d::date AS d, (SELECT count(*) FROM daily_videos v WHERE v.video_date = d::date AND v.slot <= $2) AS n "
        "FROM generate_series($1::date, $1::date + 6, interval '1 day') d", today, per_day)
    return {
        "main_admin": bool(await cpo.fetch_val("SELECT 1 FROM users WHERE user_code = $1 AND role = 'ADMIN'", ROOT_ADMIN)),
        "admins": await cpo.fetch_val("SELECT count(*) FROM users WHERE role = 'ADMIN' AND status = 'active'"),
        "company_missing": [k for k in ("name", "upi_id", "account_holder", "account_no", "ifsc", "qr_path", "phone")
                            if not c.get(k)],
        "complaint_categories": await cpo.fetch_val("SELECT count(*) FROM complaint_categories"),
        "products_live": await cpo.fetch_val("SELECT count(*) FROM products WHERE is_active AND stock > 0"),
        "doctors_active": await cpo.fetch_val("SELECT count(*) FROM users WHERE role = 'DOCTOR' AND status = 'active'"),
        "video_days_missing_next_7": [str(r["d"]) for r in video_days if r["n"] < per_day],
        "pincodes": await cpo.fetch_val("SELECT count(*) FROM pincodes"),
        "smtp_set": bool(os.environ.get("SMTP_HOST") and (os.environ.get("SMTP_PASSWORD") or os.environ.get("SMTP_PASS"))),
        "app_env": os.environ.get("APP_ENV", "local"),
    }


def _print(report: Dict[str, Any]):
    if "applied" in report:
        print("APPLIED (saved)" if report["applied"] else "DRY RUN: nothing saved. Add --apply to save.")
        for line in report["changes"]:
            print("  +", line)
        for line in report["skipped"]:
            print("  =", line)
        if not report["changes"]:
            print("  nothing to change")
        extra = {k: v for k, v in report.items() if k not in ("applied", "changes", "skipped")}
        if extra:
            print(json.dumps(extra, indent=2, default=str))
    else:
        print(json.dumps(report, indent=2, default=str))


async def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for name in ("company", "products", "videos"):
        p = sub.add_parser(name)
        p.add_argument("file", type=Path)
        p.add_argument("--apply", action="store_true")
    p = sub.add_parser("doctor")
    for a in ("--name", "--mobile", "--email", "--bams"):
        p.add_argument(a, required=True)
    p.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            report = await status()
        elif a.cmd == "doctor":
            pw = os.environ.get("DOCTOR_PASSWORD") or getpass.getpass("Doctor's password (hidden): ")
            report = await doctor(a.name, a.mobile, a.email, a.bams, pw, a.apply)
        else:
            report = await {"company": company, "products": products, "videos": videos}[a.cmd](a.file, a.apply)
    except Bad as e:
        print("NOT SAVED. Fix these and run again:")
        for line in e.errors:
            print("  -", line)
        return 1
    finally:
        await cpo.close()
        await redis_client.aclose()
    _print(report)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
