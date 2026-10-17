"""Pre-flight check on the server, before opening to members and after every deploy.

    cd /home/treeway/app && venv/bin/python deploy/preflight.py

Prints PASS / WARN / FAIL for each item and exits with 1 if anything FAILs. Read-only: it changes nothing.
"""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values  # noqa: E402

ENV = {**dotenv_values(ROOT / "config" / ".env"), **{k: v for k, v in os.environ.items() if k in ("APP_ENV",)}}
RESULTS = []


def check(name, ok, detail="", warn=False):
    RESULTS.append(("PASS" if ok else ("WARN" if warn else "FAIL"), name, detail))


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20).stdout
    except Exception as e:  # noqa: BLE001
        return f"ERROR {e}"


def http(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        return None


def config_checks():
    g = ENV.get
    check("APP_ENV is production", g("APP_ENV") == "production", f"APP_ENV={g('APP_ENV')}")
    sk = g("SECRET_KEY") or ""
    check("SECRET_KEY strong", len(sk) >= 48 and "CHANGE" not in sk and "change-me" not in sk, f"{len(sk)} characters")
    ap = g("ADMIN_PASSWORD") or ""
    if ap:
        from seed import admin_password_problem

        why = admin_password_problem(ap, g("ADMIN_MOBILE") or "")
        check("ADMIN_PASSWORD strong (seed.py refuses a weak one)", not why and "CHANGE" not in ap, why)
        check("ADMIN_PASSWORD removed from .env after the first login", False,
              "remove it once the admin has logged in and changed the password", warn=True)
    fe = g("FRONTEND_URL") or ""
    check("FRONTEND_URL is the https domain (join + reset links)", fe.startswith("https://") and "localhost" not in fe, fe)
    check("MAIL_FROM on the company domain", bool(g("MAIL_FROM")) and "gmail.com" not in (g("MAIL_FROM") or ""),
          g("MAIL_FROM") or "not set (mails go out from SMTP_USER)", warn=True)
    check("TRUST_PROXY=1 (nginx sets X-Real-IP)", g("TRUST_PROXY") == "1")
    cors = [o.strip() for o in (g("CORS_ORIGINS") or "").split(",") if o.strip()]
    check("CORS only https origins", bool(cors) and all(o.startswith("https://") for o in cors), ", ".join(cors))
    check("SMTP set (OTPs and alerts)", bool(g("SMTP_HOST") and g("SMTP_USER") and g("SMTP_PASSWORD")))
    check("ALERT_EMAIL set", bool(g("ALERT_EMAIL")))
    redis_url = g("REDIS_URL") or ""
    check("Treeway's own Redis on 6380 with a password", ":6380" in redis_url and re.search(r"//:[^@]+@", redis_url) is not None,
          re.sub(r"//:[^@]+@", "//:***@", redis_url))
    db = g("DATABASE_URL") or ""
    check("Postgres on this machine", "127.0.0.1" in db or "localhost" in db)
    env_file = ROOT / "config" / ".env"
    check("config/.env is chmod 600", env_file.exists() and oct(env_file.stat().st_mode)[-3:] == "600",
          oct(env_file.stat().st_mode)[-3:] if env_file.exists() else "missing")


async def db_checks():
    import asyncpg
    os.environ.update({k: v for k, v in ENV.items() if v is not None})
    try:
        conn = await asyncpg.connect(ENV["DATABASE_URL"].replace(":6432/", ":5432/"), timeout=5)
    except Exception as e:  # noqa: BLE001
        check("Postgres reachable", False, str(e))
        return
    check("Postgres reachable", True)
    applied = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
    files = {p.name for p in (ROOT / "migrations").glob("*.sql")}
    check("all migrations applied", files <= applied, f"missing: {sorted(files - applied)}" if files - applied else "")
    check("admin exists", bool(await conn.fetchval("SELECT 1 FROM users WHERE role = 'ADMIN'")))
    company = await conn.fetchval("SELECT value FROM app_settings WHERE key = 'company'") or {}
    if isinstance(company, str):
        import json
        company = json.loads(company)
    check("company payment details filled (UPI, account, IFSC, QR)",
          all(company.get(k) for k in ("upi_id", "account_no", "ifsc", "qr_path")), warn=True)
    check("Gullak still switched off", "true" not in str(await conn.fetchval(
        "SELECT value->'enabled' FROM app_settings WHERE key = 'gullak'")).lower(),
        "turn on only after the CA / lawyer approve", warn=True)
    await conn.close()


def app_checks():
    base = "http://127.0.0.1:8100"
    check("API answers /health", http(f"{base}/api/v1/health") == 200)
    check("dev OTP hook does not exist (404)", http(f"{base}/api/v1/dev/last-otp?email=a@b.c&purpose=p2p") == 404)
    check("/docs is off (404)", http(f"{base}/docs") == 404)
    pm2 = sh("pm2 jlist")
    check("exactly one scheduler running", pm2.count('"name":"treeway-scheduler"') == 1, "pm2 jlist")
    check("API running under pm2", '"name":"treeway-api"' in pm2)
    page = Path("/home/treeway/web/dist/index.html")
    check("app built (web/dist/index.html) with its CSP", page.exists() and "Content-Security-Policy" in page.read_text(),
          "cd ~/web && npm ci && npm run build")


def server_checks():
    ufw = sh("sudo -n ufw status 2>/dev/null || ufw status 2>/dev/null")
    if "Status:" in ufw:
        ports = set(re.findall(r"^(\d+)(?:/tcp)?\s+ALLOW", ufw, re.M))
        check("ufw on, only 22/80/443 open", "Status: active" in ufw and ports <= {"22", "80", "443"}, ", ".join(sorted(ports)))
    else:
        check("ufw on, only 22/80/443 open", False, "could not read ufw (run as root once to see)", warn=True)
    sshd = sh("sshd -T 2>/dev/null")
    if sshd:
        check("SSH: no password login", "passwordauthentication no" in sshd)
        check("SSH: root only with a key (or not at all)", re.search(r"permitrootlogin (no|prohibit-password|without-password)", sshd) is not None)
    else:
        check("SSH settings", False, "run as root to check sshd -T", warn=True)
    check("fail2ban running", sh("systemctl is-active fail2ban").strip() == "active")
    listen = sh("ss -ltnH")
    for port, name in (("6380", "Redis"), ("5432", "Postgres"), ("8100", "API")):
        addrs = re.findall(rf"(\S+):{port}\s", listen)
        check(f"{name} listens on localhost only", bool(addrs) and all(a in ("127.0.0.1", "[::1]") for a in addrs),
              ", ".join(addrs) or "not listening")
    check("nginx config valid", "successful" in sh("sudo -n nginx -t 2>&1 || nginx -t 2>&1"), warn=True)
    du = shutil.disk_usage("/")
    check("disk at least 20% free", du.free / du.total >= 0.2, f"{du.free * 100 // du.total}% free")
    uploads = ROOT / "uploads"
    check("uploads not world-writable", uploads.exists() and not (uploads.stat().st_mode & 0o002))
    last = Path("/home/treeway/backups/last_ok")
    check("a backup has finished", last.exists(), last.read_text().strip() if last.exists() else "run deploy/backup/backup.sh")
    check("restore drill has passed", "restore drill ok" in sh("tail -50 /home/treeway/logs/restore_drill.log 2>/dev/null"),
          "run deploy/backup/restore_drill.sh once", warn=True)


def main():
    config_checks()
    asyncio.run(db_checks())
    app_checks()
    server_checks()
    width = max(len(n) for _, n, _ in RESULTS)
    for status, name, detail in RESULTS:
        print(f"{status:4}  {name.ljust(width)}  {detail}")
    fails = sum(1 for s, _, _ in RESULTS if s == "FAIL")
    warns = sum(1 for s, _, _ in RESULTS if s == "WARN")
    print(f"\n{len(RESULTS) - fails - warns} pass, {warns} warn, {fails} FAIL")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
