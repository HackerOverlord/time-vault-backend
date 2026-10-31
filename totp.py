"""
TOTP (RFC 6238) for authenticator apps, plus recovery codes.

Implemented directly from the RFC (HMAC-SHA1, 30-second steps, 6 digits),
the defaults every authenticator app uses, so no extra dependency.
Checked against the RFC's published test vectors in tests.

Secrets are stored Fernet-encrypted (see crypto.py) and decrypt strictly:
a damaged value raises instead of silently falling back.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from flask import current_app

from crypto import _fernet

STEP = 30
DIGITS = 6
WINDOW = 1                 # accept one step either side for clock drift
RECOVERY_CODE_COUNT = 10
_RC_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o, 1/l/i lookalikes


# ── Secrets ──────────────────────────────────────────────────────────────────

def new_secret() -> str:
    """160-bit random secret, base32 (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def encrypt_secret(secret: str) -> str:
    return _fernet.encrypt(secret.encode()).decode()


def decrypt_secret(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode()


def provisioning_uri(secret: str, account: str, issuer: str = "Time Vault") -> str:
    """otpauth:// URI encoded in the setup QR code."""
    label = quote(f"{issuer}:{account}")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")


# ── Codes ────────────────────────────────────────────────────────────────────

def _key(secret: str) -> bytes:
    return base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS, digest=hashlib.sha1) -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def _clock() -> float:          # patched in tests
    return time.time()


def current_step(now: float | None = None) -> int:
    return int((_clock() if now is None else now) // STEP)


def code_at(secret: str, now: float | None = None) -> str:
    return hotp(_key(secret), current_step(now))


def verify(secret: str, code: str, last_step: int | None, now: float | None = None) -> int | None:
    """Return the matched time step if `code` is valid and newer than
    `last_step` (so each code works once), else None."""
    code = "".join(c for c in str(code or "") if c.isdigit())
    if len(code) != DIGITS:
        return None
    key, step = _key(secret), current_step(now)
    for s in range(step - WINDOW, step + WINDOW + 1):
        if last_step is not None and s <= last_step:
            continue
        if hmac.compare_digest(hotp(key, s), code):
            return s
    return None


# ── Recovery codes ───────────────────────────────────────────────────────────

def new_recovery_codes(n: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Codes like 'k7m2p-x9qtr' (about 49 bits each)."""
    def one():
        while True:
            raw = "".join(secrets.choice(_RC_ALPHABET) for _ in range(10))
            if not raw.isdigit():        # always tell-apart from app codes
                return f"{raw[:5]}-{raw[5:]}"
    return [one() for _ in range(n)]


def normalize_recovery_code(code: str) -> str:
    return "".join(c for c in str(code or "").lower() if c.isalnum())


def hash_recovery_code(code: str) -> str:
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, normalize_recovery_code(code).encode(), hashlib.sha256).hexdigest()


def looks_like_recovery_code(code: str) -> bool:
    return len(normalize_recovery_code(code)) == 10 and not normalize_recovery_code(code).isdigit()
