"""Bot protection for sign-up (SRS §7 "bot registration: OTP, captcha"), with no paid service and nothing for the
member to solve.

Proof of work: the app gets a signed challenge, its phone finds a nonce so that sha256("<salt>:<nonce>") starts
with POW_BITS zero bits (about 65,000 tries at 16 bits = 1–2 s on a cheap Android, done in the background while
the form is filled), and the server checks it once. One sign-up costs a real device a moment; a bot making
thousands pays thousands of times. Together with the honeypot field and the rate limits.
"""

import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

from base import SECRET_KEY, fail, logger, redis_client

# Tests use fewer bits (fast); production and the local / e2e servers use the default.
POW_BITS = int(os.environ.get("REGISTER_POW_BITS", 16))
POW_TTL = 600  # a challenge works for 10 minutes, once


def _sign(body: str) -> str:
    return hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]


def make_challenge() -> dict:
    salt = secrets.token_hex(16)
    body = f"{salt}.{int(time.time()) + POW_TTL}.{POW_BITS}"
    return {"challenge": f"{body}.{_sign(body)}", "bits": POW_BITS, "expires_in": POW_TTL}


def leading_zero_bits_ok(salt: str, nonce: str, bits: int) -> bool:
    digest = hashlib.sha256(f"{salt}:{nonce}".encode()).digest()
    return bits <= 0 or int.from_bytes(digest, "big") >> (256 - bits) == 0


def solve(challenge: str) -> str:
    """For tests and scripts: the nonce a phone would find."""
    salt, _, bits, _ = challenge.split(".")
    n = 0
    while not leading_zero_bits_ok(salt, str(n), int(bits)):
        n += 1
    return str(n)


async def check_pow(challenge: Optional[str], nonce: Optional[str], honeypot: Optional[str]):
    """Stops the sign-up with 400 "Captcha" unless the proof of work is right, fresh and not used before."""
    again = "Please try again. (Your phone checks itself in a moment.)"
    if honeypot:
        fail(400, again, "Captcha")   # a person never fills the hidden field
    if POW_BITS <= 0:
        return
    if not challenge or not nonce or len(nonce) > 20 or not nonce.isdigit():
        fail(400, again, "Captcha")
    parts = challenge.split(".")
    if len(parts) != 4:
        fail(400, again, "Captcha")
    salt, exp, bits, sig = parts
    if not hmac.compare_digest(sig, _sign(f"{salt}.{exp}.{bits}")):
        fail(400, again, "Captcha")
    if not exp.isdigit() or int(exp) < time.time():
        fail(400, "The sign-up form was open too long. Please press the button again.", "Captcha")
    if int(bits) < POW_BITS or not leading_zero_bits_ok(salt, nonce, int(bits)):
        fail(400, again, "Captcha")
    try:
        if not await redis_client.set(f"pow:{salt}", 1, ex=POW_TTL, nx=True):
            fail(400, again, "Captcha")   # the same challenge again (a replay)
    except ConnectionError as e:  # Redis down: keep sign-up working (the signature + expiry still hold)
        logger.warning("pow replay check skipped: %s", e)
