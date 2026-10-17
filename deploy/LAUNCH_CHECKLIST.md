# Launch checklist

Tick each line on the server, in order. `deploy/preflight.py` checks most of them by itself.

## Before the first start
- [ ] `security/setup_server.sh` done; logged in again as `treeway` with the SSH key (second window)
- [ ] Postgres tuned (`postgres/treeway.conf`), role + database `treeway` created, `pg_hba` local only
- [ ] Redis for Treeway running on 127.0.0.1:6380 with a long password
- [ ] `config/.env` from `env.production.example`, `chmod 600`:
  - [ ] `APP_ENV=production`
  - [ ] `SECRET_KEY` = 64 random characters (never reuse the local one)
  - [ ] `ADMIN_MOBILE`, `ADMIN_EMAIL` (a real inbox you can read), long `ADMIN_PASSWORD`
  - [ ] `SMTP_*`: send one test mail (`venv/bin/python deploy/monitor/notify.py "Test" "SMTP works"`)
  - [ ] `ALERT_EMAIL`, `FRONTEND_URL` (the real domain: join + reset links), `CORS_ORIGINS` (https only), `TRUST_PROXY=1`
- [ ] `venv/bin/pip install -r requirements.txt` and `venv/bin/python seed.py` (migrations + first admin + settings)
- [ ] pm2: `pm2 start deploy/pm2/ecosystem.config.js && pm2 save && pm2 startup`; pm2-logrotate set
- [ ] DNS: A records `treeway.avigronix.com` and `apitreeway.avigronix.com` → server IP
- [ ] nginx: copy `nginx/treeway-headers.conf` + `nginx/treeway-proxy.conf` to `/etc/nginx/treeway/`, enable
      `treeway-api.conf` and `treeway-web.conf`, certificates with `sudo certbot --nginx -d treeway.avigronix.com -d apitreeway.avigronix.com`
      (behind Cloudflare instead: origin certificate + `scripts/update_cloudflare_ips.sh`, SSL "Full (strict)")
- [ ] Cloudflare rate-limiting rule: `/api/v1/users/login*`, more than 30 requests / minute / IP -> block 10 min
- [ ] Backup: `backup.env`, `rclone config`, age key pair (public key on the server, private key with Vinay +
      one more safe copy); run `backup/backup.sh` once by hand
- [ ] Restore drill: run `backup/restore_drill.sh` once by hand; it must say "restore drill ok"
- [ ] Cron: healthcheck, backup, restore drill, Cloudflare IPs (see `monitor/README.md`)
- [ ] App (frontend): `cd ~/web && git pull --ff-only && npm ci && npm run build` → nginx serves `~/web/dist` (see README step 6b)
- [ ] Open `https://treeway.avigronix.com` on a phone: login screen loads; `https://treeway.avigronix.com/api/v1/health` and `https://apitreeway.avigronix.com/api/v1/health` say ok
- [ ] Outside uptime monitor on `https://apitreeway.avigronix.com/api/v1/health` with SMS

## First login and company setup
- [ ] Admin login with the password; the OTP arrives at `ADMIN_EMAIL` (this tests SMTP for real)
- [ ] Change the admin password in the app (the one in `.env` is only for the first start)
- [ ] Settings -> company details: account holder, account no., IFSC, UPI ID, **QR photo**, phone, WhatsApp group
      (or from a file: `scripts/prod_setup/setup.py company company.json --apply`, see scripts/prod_setup/README.md)
- [ ] Check plan numbers in Settings (₹987, level income, Super 3, limits) once more with bhaiya
- [ ] Add a second admin (backup person; 2-admin approval is OFF since 15 Oct, one admin approves any amount)
- [ ] Add doctor(s) with BAMS numbers; tomorrow's 4 videos; at least one shop product with stock
      (many at once: `scripts/prod_setup/setup.py products|videos|doctor`; then `setup.py status` shows nothing missing)
- [ ] Remove `ADMIN_PASSWORD` from `config/.env` after the first login
- [ ] Import the India Post pincode CSV: `venv/bin/python scripts/import_pincodes.py <file.csv>`
- [ ] Gullak stays OFF (CA / lawyer first); `shop_sales_in_profit` stays false until the CA confirms

## Go / no-go
- [ ] `venv/bin/python deploy/preflight.py` -> 0 FAIL
- [ ] One real end-to-end run by Vinay: register a test member with the admin's link, pay ₹987 by UPI,
      approve, see the videos lock, ₹20, withdraw ₹200 with OTP, pay it from the bank, mark paid
- [ ] Remove / block that test member, or keep it as the company's demo ID
