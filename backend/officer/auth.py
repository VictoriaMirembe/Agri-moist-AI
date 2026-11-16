# Sign-in checks for officers and farmers. The only proof of who someone is, is the
# signed session cookie (SessionMiddleware in backend/main.py). It is set only after:
#   - an officer gives their own phone number and password (/officer/login or
#     POST /api/auth/login), checked against OfficerAccount's scrypt hash, or
#   - a farmer gives their registered phone number and 4-digit PIN (/farmer/login),
#     checked against FarmerPin.
# A plain cookie or header saying "officer" proves nothing and is ignored.
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import HTTPException, Request
from sqlmodel import Session, select

from backend.core.config import settings
from backend.core.phones import normalize_phone
from backend.db.models import Contact, FarmerPin, Household, Officer, OfficerAccount
from backend.officer.pins import verify_password, verify_pin


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _locked(row) -> bool:
    until = _aware(row.locked_until)
    return until is not None and until > datetime.now(timezone.utc)


def _record_attempt(session: Session, row, ok: bool) -> None:
    if ok:
        row.failed_attempts = 0
        row.locked_until = None
    else:
        row.failed_attempts += 1
        if row.failed_attempts >= settings.web_login_max_attempts:
            row.failed_attempts = 0
            row.locked_until = datetime.now(timezone.utc) + timedelta(minutes=settings.web_login_lock_minutes)
    session.add(row)
    session.commit()


LOCKED_MESSAGE = "Too many wrong tries. Wait {minutes} minutes, then try again."


# ---------------------------------------------------------------------------
# Officers
# ---------------------------------------------------------------------------
def officer_accounts_exist(session: Session) -> bool:
    return session.exec(select(OfficerAccount)).first() is not None


def check_officer_login(
    session: Session, *, password: str, phone: str | None = None, officer_id: int | None = None
) -> tuple[Officer | None, str | None]:
    """(officer, None) when the phone (or officer id) and password match, else
    (None, a message safe to show). Wrong passwords count towards a lock."""
    account = None
    if phone:
        account = session.exec(select(OfficerAccount).where(OfficerAccount.phone == normalize_phone(phone))).first()
    elif officer_id is not None:
        account = session.get(OfficerAccount, officer_id)
    if account is None:
        return None, "That phone number and password do not match an officer account."
    if _locked(account):
        return None, LOCKED_MESSAGE.format(minutes=settings.web_login_lock_minutes)
    ok = verify_password(password or "", account.password_hash)
    _record_attempt(session, account, ok)
    if not ok:
        return None, "That phone number and password do not match an officer account."
    return session.get(Officer, account.officer_id), None


def get_current_user_role(request: Request) -> str | None:
    """The role stored in the signed server-side session, or None."""
    if "session" not in request.scope:
        return None
    return request.session.get("role")


def signed_in_officer_id(request: Request) -> int | None:
    """The signed-in officer's id from the signed session, or None."""
    if get_current_user_role(request) != "officer":
        return None
    officer_id = request.session.get("officer_id")
    return officer_id if isinstance(officer_id, int) else None


def signed_in_officer(request: Request) -> dict | None:
    """{"officer_id", "officer_name", "is_admin"} from the signed session, or None."""
    officer_id = signed_in_officer_id(request)
    if officer_id is None:
        return None
    return {
        "officer_id": officer_id,
        "officer_name": request.session.get("officer_name"),
        "is_admin": bool(request.session.get("is_admin")),
    }


def sign_in(
    request: Request, officer_id: int, officer_name: str, is_admin: bool = False, district_name: str | None = None
) -> None:
    request.session.clear()
    request.session.update(
        role="officer", officer_id=officer_id, officer_name=officer_name, is_admin=is_admin,
        district_name=f"{district_name} District extension office" if district_name else None,
    )


def require_officer(request: Request) -> dict:
    """For the JSON API: 403 unless an officer is signed in."""
    officer = signed_in_officer(request)
    if officer is None:
        raise HTTPException(status_code=403, detail="Access denied: Officers only.")
    return officer


def require_admin(request: Request) -> dict:
    officer = require_officer(request)
    if not officer["is_admin"]:
        raise HTTPException(status_code=403, detail="Only an admin officer can do this.")
    return officer


def require_officer_page(request: Request) -> dict:
    """For officer screens: send anyone not signed in to the sign-in page, then back."""
    officer = signed_in_officer(request)
    if officer is None:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise HTTPException(status_code=303, headers={"Location": f"/officer/login?next={quote(target)}"})
    return officer


# ---------------------------------------------------------------------------
# Farmers
# ---------------------------------------------------------------------------
def check_farmer_login(session: Session, *, phone: str, pin: str) -> tuple[dict | None, str | None]:
    """({"household_id", "phone", "name"}, None) when the number is registered (with
    consent) and the PIN matches, else (None, a message safe to show)."""
    wrong = "That phone number and PIN do not match. Ask your extension officer if you forgot your PIN."
    normalised = normalize_phone(phone)
    contact = next((c for c in session.exec(select(Contact)).all() if normalize_phone(c.phone) == normalised), None)
    household = session.get(Household, contact.household_id) if contact else None
    if household is None or not household.consent_given:
        return None, wrong
    pin_row = session.get(FarmerPin, household.id)
    if pin_row is None:
        return None, "No PIN has been set for this number yet. Ask your extension officer to set one."
    if _locked(pin_row):
        return None, LOCKED_MESSAGE.format(minutes=settings.web_login_lock_minutes)
    ok = verify_pin(pin or "", pin_row.pin_hash)
    _record_attempt(session, pin_row, ok)
    if not ok:
        return None, wrong
    return {"household_id": household.id, "phone": normalised, "name": contact.name}, None


def sign_in_farmer(request: Request, farmer: dict) -> None:
    request.session.clear()
    request.session.update(
        role="farmer", household_id=farmer["household_id"], phone=farmer["phone"], farmer_name=farmer["name"]
    )


def signed_in_farmer(request: Request) -> dict | None:
    if get_current_user_role(request) != "farmer":
        return None
    return {
        "household_id": request.session.get("household_id"),
        "phone": request.session.get("phone"),
        "name": request.session.get("farmer_name"),
    }


def farmer_phone_for_request(request: Request, phone: str | None) -> str:
    """The phone a farmer API call may act for. A signed-in farmer acts only for their
    own number (a blank phone means their own); a signed-in officer may look up any
    registered number. Anyone else gets 401."""
    farmer = signed_in_farmer(request)
    if farmer is not None:
        if phone and normalize_phone(phone) != farmer["phone"]:
            raise HTTPException(status_code=403, detail="You can only see your own farm.")
        return farmer["phone"]
    if signed_in_officer(request) is not None:
        if not phone:
            raise HTTPException(status_code=400, detail="phone is required")
        return phone
    raise HTTPException(status_code=401, detail="Please sign in with your phone number and PIN.")


def require_farmer_page(request: Request) -> dict:
    farmer = signed_in_farmer(request)
    if farmer is None:
        target = request.url.path
        raise HTTPException(status_code=303, headers={"Location": f"/farmer/login?next={quote(target)}"})
    return farmer
