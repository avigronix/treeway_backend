"""Send an alert email to ALERT_EMAIL using the app's SMTP settings (config/.env).

    venv/bin/python deploy/monitor/notify.py "Subject" "Body text"

Used by backup.sh, restore_drill.sh and healthcheck.sh. Prints instead when SMTP is not set.
"""

import os
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "config" / ".env")

from common.mailSender import _send  # noqa: E402


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    subject, body = f"[Treeway] {sys.argv[1]}", sys.argv[2]
    to = [x.strip() for x in os.environ.get("ALERT_EMAIL", "").split(",") if x.strip()]
    if not to or not os.environ.get("SMTP_HOST"):
        print(f"(no ALERT_EMAIL / SMTP) {subject}: {body}")
        return
    for addr in to:
        _send(addr, subject, body, f"<pre>{escape(body)}</pre>")


if __name__ == "__main__":
    main()
