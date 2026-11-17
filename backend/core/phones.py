# Uganda phone numbers, normalised one way everywhere (sign-in, registration, USSD).
from __future__ import annotations

import re


def normalize_phone(phone: str) -> str:
    """E.164 (+256...) for comparison. Accepts +256700000001, 0700000001,
    256700000001, 700000001, +2560700000001, and the same with spaces, dashes,
    dots or brackets."""
    cleaned = re.sub(r"[\s\-.()]", "", phone or "").strip()
    if re.fullmatch(r"\+?2560\d{9}", cleaned):  # country code plus the local 0
        cleaned = "+256" + cleaned[-9:]
    elif re.fullmatch(r"7\d{8}", cleaned):  # local number without the leading 0
        cleaned = "+256" + cleaned
    elif cleaned.startswith("0") and len(cleaned) == 10:
        cleaned = "+256" + cleaned[1:]
    elif cleaned.startswith("256") and not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


def is_valid_phone(phone: str) -> bool:
    """A Uganda mobile number once normalised: +256 then 9 digits."""
    return re.fullmatch(r"\+256\d{9}", normalize_phone(phone)) is not None
