# Treeway on the server (Contabo Mumbai, Ubuntu 22.04/24.04)

Everything here is written to be run by hand, in this order. Nothing runs by itself from this repo.
The server is shared with other pm2 apps, so Treeway gets its own Linux user, its own Redis and modest limits.

| Step | File | What it does |
|---|---|---|
| 1 | `security/setup_server.sh` | Linux user `treeway`, ufw (22/80/443 only), SSH keys only, fail2ban |
| 2 | `postgres/treeway.conf` | Postgres 16 tuning for 11 GB RAM shared with other apps; DB + role |
| 3 | `redis/redis-treeway.conf` + `.service` | Treeway's own Redis on 127.0.0.1:6380 with a password |
| 4 | `env.production.example` | Copy to `/home/treeway/app/config/.env` and fill in |
| 5 | `pm2/ecosystem.config.js` | gunicorn (4 workers) + exactly one scheduler, memory limits, restarts |
| 6 | `nginx/treeway-api.conf` (apitreeway.avigronix.com) + `nginx/treeway-web.conf` (treeway.avigronix.com) + `treeway-headers.conf` + `treeway-proxy.conf` in `/etc/nginx/treeway/` | The app domain serves `web/dist` and passes `/api/` + `/uploads/` to gunicorn (no CORS); HTTPS (certbot), real visitor IP, CSP + security headers, gzip, 6 MB uploads |
| 6b | the app (treeway_frontend) | `git clone <frontend repo> ~/web`: the built app is already in `~/web/dist` (made by release.sh). nginx serves it |
| 7 | `backup/backup.sh` + cron | Daily encrypted Postgres + uploads backup to R2 / Backblaze, 30 days kept |
| 8 | `backup/restore_drill.sh` + cron | Weekly: restore the latest backup into a scratch DB and check it |
| 9 | `monitor/` | Health / disk / RAM checks every 5 min, email alerts, pm2-logrotate |
| 10 | `preflight.py` | Checks all of the above before opening to members |
| 11 | `LAUNCH_CHECKLIST.md` | The last manual steps (admin, SMTP, company details, first OTP test) |

PgBouncer (`pgbouncer/pgbouncer.ini`) is **not needed at launch**: 4 workers × 10 connections + the
scheduler is ~45 connections, which Postgres handles directly. Turn it on only if `perf` shows connection
pressure (see the notes in that file).

## Getting the code on the server

GitHub has only the code the server runs (branch `release` on Vinay's machine, pushed as `main`; made by
`treeway/release.sh`). Tests and docs stay on the development machine.

```bash
sudo -iu treeway
git clone <backend-repo-url> ~/app
git clone <frontend-repo-url> ~/web       # has the built app in ~/web/dist: no npm / build on the server
```

Folder layout on the server:

```
/home/treeway/app            git checkout of main (backend code)
/home/treeway/web            git checkout of the frontend; web/dist is what members download
/home/treeway/app/venv       Python 3.12 virtualenv
/home/treeway/app/config/.env  secrets (chmod 600)
/home/treeway/app/uploads    receipts, health files, product photos (backed up)
/home/treeway/backups        last 3 local backups (also off-server)
/home/treeway/logs           pm2 + job logs (rotated)
```

Deploying a new version:

```bash
sudo -iu treeway
cd ~/app && git pull --ff-only
venv/bin/pip install -r requirements.txt
venv/bin/python seed.py               # applies new migrations once; safe to re-run
pm2 reload treeway-api                # zero-downtime reload of the 4 workers
pm2 restart treeway-scheduler
venv/bin/python deploy/preflight.py

# the app (only when the frontend changed):
cd ~/web && git pull --ff-only   # dist/ comes ready-built; members get the new version on next open ("Update" prompt)
```
