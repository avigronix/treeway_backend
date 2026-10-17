# Treeway backend

FastAPI + PostgreSQL 16 (asyncpg) + Redis. Money is always stored in **paise** (₹987 = 98700).

## Local setup (Mac)

```bash
brew install postgresql@16 redis
brew services start postgresql@16 && brew services start redis

/opt/homebrew/opt/postgresql@16/bin/psql postgres -c "CREATE ROLE treeway LOGIN PASSWORD 'treeway';"
/opt/homebrew/opt/postgresql@16/bin/psql postgres -c "CREATE DATABASE treeway OWNER treeway;"

cp config/.env.example config/.env        # set SECRET_KEY + ADMIN_* values
python3 -m venv venv && venv/bin/pip install -r requirements.txt

venv/bin/python seed.py                   # tables + settings + admin + Super 3 company spots (safe to re-run)
venv/bin/uvicorn main:app --reload --port 8100
venv/bin/python scheduler_runner.py       # background jobs (separate terminal)
```

Tests (need a `treeway_test` DB owned by `treeway`; Redis db 9 is wiped):

```bash
/opt/homebrew/opt/postgresql@16/bin/psql postgres -c "CREATE DATABASE treeway_test OWNER treeway;"
venv/bin/pip install -r requirements-dev.txt
venv/bin/pytest                       # TW_BULK=6000 venv/bin/pytest for a bigger Super 3 run
```

Frontend end-to-end server (throwaway DB `treeway_e2e`, port 8101): `venv/bin/python scripts/e2e_server.py`

Demo server for meetings (sample data in `treeway_demo`, port 8102, every answer marked DEMO): `venv/bin/python scripts/demo_server.py`

API docs (local only): http://localhost:8100/docs. Without SMTP, OTPs print in the uvicorn console.

Admin login: mobile `ADMIN_MOBILE` / `ADMIN_PASSWORD`. Admin ID `TW1000000` is the root sponsor link.

## Folder structure

| File | What it does |
|---|---|
| `config/pg_operation.py` | Common Postgres helper `cpo` (like `cmo` for Mongo): `insertion`, `updating`, `find_one`, `simple_finding`, `paginate`, `transaction()` |
| `config/redisClient.py` | Async Redis (cache, OTP, rate limit) |
| `base.py` | `respond`, `fail`, money helpers, token / `tokenRequired`, OTP, file upload, settings |
| `mlmEngine.py` | Activation, level income, 10% cut, Super 3 boards, PINs |
| `rankStats.py` | Nightly ranks over the full team depth (50:50 rule), month-end snapshot, Super 3 Level reach date |
| `monthlyPayouts.py` | Royalty + Super 3 Level on the 3rd: preview, admin Pay, 18:00 backup, crash-safe |
| `singleLeg.py` | Single Leg nightly job (claim → compute → pay in chunks → done; safe to rerun after a crash) |
| `scripts/import_pincodes.py` | Loads India Post's pincode → state CSV (courier rates); until then a PIN prefix rule is used |
| `adminApproval.py` | 2-admin approval: actions of `two_admin_threshold` (₹10,000) or more wait for a second admin |
| `routes/` | `users`, `walletApis`, `pinApis`, `matrix` (Super 3), `complaintApis`, `dashboardApis` (admin + P&L), `companyApis` (settings), `adminApis` (approvals, admins), `reverseApis` (reverse entry), `videoApis` (YFI daily videos) |
| `tests/` | pytest suite; every file gets a fresh `treeway_test` DB and ends with `assert_invariants()` |
| `migrations/*.sql` | Schema. New change = new file `002_xxx.sql`, `seed.py` applies it once |
| `scheduler_runner.py` | Jobs: remove inactive IDs after N days, clean video logs older than 60 days, Single Leg at 00:00 IST, ranks at 00:20, royalty / S3 Level backup payout at 18:00 |

## Rules followed in code

- Anything that moves money runs inside `cpo.transaction()`: all or nothing.
- Every ledger row has a unique `ref`, so the same income can never be paid twice.
- Wallet balance has `CHECK (>= 0)` in the database: spending more than the balance is impossible.
- Activations run one at a time (Postgres advisory lock), so Super 3 spots never clash and there are no deadlocks.
- Never put user input inside SQL text; always `$1, $2` parameters.

## Server

Everything for the live server is in [deploy/](deploy/README.md): pm2, nginx + Cloudflare, Postgres, PgBouncer,
Redis, security setup, `deploy/preflight.py`, backups + restore drill, monitoring and the
[launch checklist](deploy/LAUNCH_CHECKLIST.md). Only ONE scheduler may run.

## Performance

`scripts/load_seed.py` (1 lakh members in `treeway_load`), `scripts/explain_report.py`, `scripts/loadtest.py`.
Results: [docs/PERFORMANCE.md](docs/PERFORMANCE.md).
