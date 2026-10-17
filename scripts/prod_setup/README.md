# First-day data on the live server

Everything here can also be done from the admin screens. These scripts are for putting in many rows at once
(products, a month of videos) and for the very first setup before anyone logs in.

**Safety, for every command:**
- Without `--apply` it is a **dry run**: it checks every row and prints what would change. Nothing is saved.
- With `--apply` the whole file is saved in **one transaction**. If any row is wrong, or the server stops half way,
  nothing is saved (and no picture is left behind).
- Running the same file again changes nothing: rows already there are listed with `=` and skipped.
- The checks are the same as the admin screens. Every change goes into the **Audit log** as the main admin
  (TW1000000), marked "setup script".
- Two runs at the same time: the second waits for the first.

Run on the server as the `treeway` user, from `~/app`:

```bash
venv/bin/python scripts/prod_setup/setup.py status          # what is still missing (read only)
```

## Order on the first day

| # | What | How |
|---|---|---|
| 1 | First admin (TW1000000) | `config/.env`: `ADMIN_MOBILE`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, then `venv/bin/python seed.py`. On the live server (`APP_ENV=production`) seed refuses a weak password: at least 12 characters with small + capital letters, a number and a symbol, and no mobile number / "treeway" / "admin" in it. After the first login, remove `ADMIN_PASSWORD` from `.env`. |
| 2 | Plan settings, complaint types, Super 3 company spots | `seed.py` adds the defaults (signed-off SRS numbers). Change later only from Admin → Settings. |
| 3 | Company bank / UPI / QR | `setup.py company company.json` |
| 4 | Products | `setup.py products products.csv` |
| 5 | Daily videos | `setup.py videos videos.csv` |
| 6 | Doctor | `setup.py doctor ...` |
| 7 | PIN codes (delivery) | `venv/bin/python scripts/import_pincodes.py` (see that file) |
| 8 | Check | `setup.py status` → nothing missing |

Examples of every file: `scripts/prod_setup/examples/`.

## company.json

```json
{ "name": "...", "upi_id": "treeway@sbi", "account_holder": "...", "account_no": "123456789012",
  "ifsc": "SBIN0001234", "branch": "...", "email": "...", "phone": "9876543210",
  "whatsapp_group": "https://chat.whatsapp.com/...", "qr": "qr.png" }
```
Only the keys you give change. `qr` is a JPG/PNG/WEBP next to the JSON file (max 5 MB).

```bash
venv/bin/python scripts/prod_setup/setup.py company company.json            # dry run
venv/bin/python scripts/prod_setup/setup.py company company.json --apply
```

## products.csv

Make it in Excel, then **File → Save as → CSV UTF-8**. Columns (header row needed, any order):

| Column | Needed | Example | Rule |
|---|---|---|---|
| name | yes | Tulsi Drops | 2 to 120 letters; a name already in the shop is skipped |
| mrp | yes | 250 or 120.50 | rupees, more than 0, at most 2 decimals |
| stock | no (0) | 50 | 0 or more |
| category | no | Health | |
| description | no | | up to 2000 letters |
| active | no (yes) | yes / no | "no" = hidden from members |
| image | no | tulsi.jpg or a.jpg\|b.jpg | pictures next to the CSV |

To change price or stock later, use Admin → Products and orders → Edit (the script only adds).

## videos.csv

| Column | Example | Rule |
|---|---|---|
| date | 2026-10-20 | today or later (past days cannot change) |
| slot | 1 | 1 to "videos a day" (Settings, 4 now) |
| platform | youtube / facebook / instagram | |
| url | https://www.youtube.com/watch?v=... | https and the right site |
| title | Video 1 | |

A slot that already has the same link is skipped; a different link replaces it (as on the admin screen).
The output ends with the next 7 days that still miss videos.

## doctor

```bash
venv/bin/python scripts/prod_setup/setup.py doctor --name "Dr. Sharma" --mobile 98xxxxxxxx \
    --email doctor@example.com --bams UP-12345 --apply
```
The password is asked hidden (or `DOCTOR_PASSWORD=... ` in front); it is never on the command line or in the logs.
Requests that came while there was no doctor go to this doctor. The doctor logs in with mobile + password + email OTP.

Tests: `tests/test_prod_setup.py`.
