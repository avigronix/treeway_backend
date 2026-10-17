import asyncio
import logging
import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from fastapi import HTTPException

from common.mailFormat import notice_mail, otp_mail

logger = logging.getLogger("treeway.mail")

DEV_ENVS = ("local", "e2e", "demo")
MAIL_FAIL_MSG = "Could not send the OTP. Please try again."
# Gmail shows an App Password as 4 groups of 4 letters; the spaces are not part of it.
_GMAIL_APP_PW = re.compile(r"^[a-z]{4}( [a-z]{4}){3}$")


def _smtp_password() -> str:
    pw = os.environ.get("SMTP_PASSWORD") or os.environ.get("SMTP_PASS") or ""
    return pw.replace(" ", "") if _GMAIL_APP_PW.match(pw) else pw


def _is_dev() -> bool:
    return os.environ.get("APP_ENV", "local") in DEV_ENVS


def real_mail_allowed(to: str) -> bool:
    """Production: every address. Dev / e2e / demo: only MAIL_ALLOWLIST (Vinay's own inbox), so the seed, test and
    @t.in / .local addresses never get real mail; those are printed to the console instead."""
    if not os.environ.get("SMTP_HOST"):
        return False
    if not _is_dev():
        return True
    allow = {a.strip().lower() for a in os.environ.get("MAIL_ALLOWLIST", "").split(",") if a.strip()}
    return to.strip().lower() in allow


def _safe(err: Exception) -> str:
    """The error text for the log, never with the SMTP password in it."""
    text = f"{type(err).__name__}: {err}"
    for secret in {os.environ.get("SMTP_PASSWORD"), os.environ.get("SMTP_PASS"), _smtp_password()}:
        if secret:
            text = text.replace(secret, "***")
    return text


def _send(to: str, subject: str, text: str, html: str):
    host, user = os.environ.get("SMTP_HOST"), os.environ.get("SMTP_USER", "")
    sender = os.environ.get("MAIL_FROM") or user
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((os.environ.get("MAIL_FROM_NAME", "Treeway"), sender))
    msg["To"] = to
    # Date + Message-ID + a plain-text part: mails without them are more likely to land in spam.
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender.partition("@")[2] or None)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=15) as server:
        server.starttls()
        server.login(user, _smtp_password())
        server.send_message(msg)


async def send_mail(to: str, subject: str, text: str, html: str):
    """SMTP is slow and blocking, so it runs in a thread and never stops other requests."""
    await asyncio.to_thread(_send, to, subject, text, html)


def _console(to: str, what: str):
    logger.warning("Mail to %s not sent (dev, not in MAIL_ALLOWLIST): %s", to, what)
    print(f"[DEV MAIL] {to} {what}", flush=True)


async def send_otp_mail(to: str, name: str, code: str, purpose: str):
    if not real_mail_allowed(to):
        if not _is_dev():
            # Never print OTPs into server logs: anyone reading logs could take over accounts.
            logger.error("SMTP_HOST is not set in production, OTP mail not sent")
            raise HTTPException(503, {"error": "Mail", "msg": MAIL_FAIL_MSG})
        _console(to, f"OTP {purpose}: {code}")
        print(f"[DEV OTP] {to} {purpose}: {code}", flush=True)
        return
    text, html = otp_mail(name, code, purpose)
    try:
        await send_mail(to, "Your Treeway OTP", text, html)
    except Exception as e:  # noqa: BLE001
        logger.error("OTP mail to %s failed: %s", to, _safe(e))
        raise HTTPException(503, {"error": "Mail", "msg": MAIL_FAIL_MSG})


async def send_mail_or_fail(to: str, name: str, subject: str, lines: list[str], link: str = "") -> None:
    """A mail the action depends on (e.g. a password reset link): a failure stops the action with a clear 503."""
    if not real_mail_allowed(to):
        if _is_dev():
            _console(to, f"{subject}: {' '.join(lines)} {link}".strip())
            return
        logger.error("SMTP_HOST is not set in production, mail not sent")
        raise HTTPException(503, {"error": "Mail", "msg": "Could not send the email. Please try again."})
    text, html = notice_mail(name, lines, link)
    try:
        await send_mail(to, subject, text, html)
    except Exception as e:  # noqa: BLE001
        logger.error("Mail to %s failed: %s", to, _safe(e))
        raise HTTPException(503, {"error": "Mail", "msg": "Could not send the email. Please try again."})


async def send_notice_mail(to: str, name: str, subject: str, lines: list[str], link: str = "") -> bool:
    """A notice (e.g. "your email was changed"). A failure is logged and never stops the action."""
    if not to:
        return False
    if not real_mail_allowed(to):
        if _is_dev():
            _console(to, f"{subject}: {' '.join(lines)} {link}".strip())
        else:
            logger.error("SMTP_HOST is not set in production, notice mail not sent")
        return False
    text, html = notice_mail(name, lines, link)
    try:
        await send_mail(to, subject, text, html)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("Notice mail to %s failed: %s", to, _safe(e))
        return False
