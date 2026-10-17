"""Sends ONE test OTP mail with the SMTP details from config/.env, to check that mail arrives (and not in spam).

    venv/bin/python scripts/send_test_mail.py you@example.com

In local / e2e / demo the address must be in MAIL_ALLOWLIST. The SMTP password is never printed.
"""

import asyncio
import logging
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import base  # noqa: E402,F401  (loads config/.env)
from common import mailSender as mail  # noqa: E402


async def main(to: str):
    if not os.environ.get("SMTP_HOST"):
        sys.exit("SMTP_HOST is empty in config/.env")
    if not mail.real_mail_allowed(to):
        sys.exit(f"{to} is not in MAIL_ALLOWLIST (APP_ENV={os.environ.get('APP_ENV')}); nothing sent")
    code = f"{secrets.randbelow(900000) + 100000}"
    await mail.send_otp_mail(to, "Vinay", code, "signup")
    print(f"Sent via {os.environ['SMTP_HOST']} as {os.environ.get('MAIL_FROM_NAME', 'Treeway')} "
          f"<{os.environ.get('MAIL_FROM') or os.environ.get('SMTP_USER')}> to {to}. Look for OTP {code}.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
