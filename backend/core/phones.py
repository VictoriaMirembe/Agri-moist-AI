# Uganda phone numbers, normalised one way everywhere (sign-in, registration, USSD).
from __future__ import annotations

import re


def normalize_phone(phone: str) -> str:
    """E.164 (+256...) for comparison. Accepts +256700000001, 0700000001,
    256700000001, and the same with spaces or dashes."""
    cleaned = re.sub(r"[\s\-]", "", phone or "").strip()
    if cleaned.startswith("0") and len(cleaned) == 10:
        cleaned = "+256" + cleaned[1:]
    elif cleaned.startswith("256") and not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


def is_valid_phone(phone: str) -> bool:
    """A Uganda mobile number once normalised: +256 then 9 digits."""
    return re.fullmatch(r"\+256\d{9}", normalize_phone(phone)) is not None
