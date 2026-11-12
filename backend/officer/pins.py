# Officer USSD PINs. A PIN is never stored: only a salted scrypt hash of it
# (scrypt is a slow, memory-hard password hash from Python's standard library, so
# no extra dependency). A 4-digit PIN has only 10,000 possible values, so the hash
# alone cannot protect it from someone who steals the database - the limit on wrong
# attempts (backend/officer/ussd.py) is what stops guessing over the phone.
from __future__ import annotations

import hashlib
import hmac
import secrets

_N, _R, _P, _KEY_LEN = 2**14, 8, 1, 32


def _derive(pin: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_KEY_LEN)


def is_valid_pin(pin: str) -> bool:
    return len(pin) == 4 and pin.isascii() and pin.isdigit()


def hash_pin(pin: str) -> str:
    """'scrypt$n$r$p$salt$hash' with a fresh random salt for every PIN."""
    if not is_valid_pin(pin):
        raise ValueError("A PIN is exactly 4 digits")
    salt = secrets.token_bytes(16)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${_derive(pin, salt, _N, _R, _P).hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    """True only if `pin` matches the stored hash. Compared in constant time."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt" or not is_valid_pin(pin):
            return False
        expected = bytes.fromhex(hash_hex)
        return hmac.compare_digest(_derive(pin, bytes.fromhex(salt_hex), int(n), int(r), int(p)), expected)
    except (ValueError, TypeError):
        return False
