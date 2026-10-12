"""Demo farmer sign-in lookup using the application's existing SQLite records."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from sqlmodel import select

from backend.core.config import settings
from backend.core.database import session_scope
from backend.db.models import Contact, Household, Officer

router = APIRouter(tags=["authentication"])
PASSWORD_HASH_ITERATIONS = 600_000


def require_officer(request: Request) -> int:
    officer_id = request.session.get("officer_id")
    if request.session.get("role") != "officer" or not isinstance(officer_id, int):
        raise HTTPException(status_code=403, detail="Access denied: Officers only.")
    return officer_id


def require_session_officer(request: Request, requested_id: int) -> None:
    officer_id = require_officer(request)
    if requested_id != officer_id:
        raise HTTPException(status_code=403, detail="Action must be performed as the signed-in officer.")


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS)
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_text = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt_text}${digest_text}"


def _verify_password(password: str, encoded_hash: str | None) -> bool:
    if not encoded_hash:
        return False
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        if not 100_000 <= iterations <= 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected_digest = base64.urlsafe_b64decode(digest_text.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False
    actual_digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual_digest, expected_digest)


def provision_officer_login(session) -> None:
    username = settings.officer_login_username
    password = settings.officer_login_password
    officer_id = settings.officer_login_id
    if not username and not password and officer_id is None:
        for officer in session.exec(select(Officer)).all():
            if officer.login_username is not None or officer.password_hash is not None:
                officer.login_username = None
                officer.password_hash = None
                session.add(officer)
        session.commit()
        return
    if not username or not password or officer_id is None:
        raise RuntimeError(
            "Set OFFICER_LOGIN_ID, OFFICER_LOGIN_USERNAME, and OFFICER_LOGIN_PASSWORD together."
        )
    if len(password) < 12:
        raise RuntimeError("OFFICER_LOGIN_PASSWORD must contain at least 12 characters.")

    officer = session.get(Officer, officer_id)
    if officer is None:
        raise RuntimeError(f"No officer with id {officer_id} for the configured login.")
    for current_officer in session.exec(select(Officer)).all():
        if current_officer.id == officer_id:
            current_officer.login_username = username
            current_officer.password_hash = _hash_password(password)
        else:
            current_officer.login_username = None
            current_officer.password_hash = None
        session.add(current_officer)
    session.commit()


@router.post("/api/officer/login")
def officer_login(request: Request, username: str = Form(...), password: str = Form(...)):
    with session_scope() as session:
        officer = session.exec(select(Officer).where(Officer.login_username == username)).first()
        if officer is None or not _verify_password(password, officer.password_hash):
            raise HTTPException(status_code=401, detail="Invalid officer username or password.")
        officer_id = officer.id
        officer_name = officer.name

    request.session.clear()
    request.session.update(
        {
            "role": "officer",
            "officer_id": officer_id,
            "officer_name": officer_name,
        }
    )
    return {"status": "success", "role": "officer", "redirect_url": "/officer/dashboard"}


@router.post("/api/officer/logout")
def officer_logout(request: Request):
    request.session.clear()
    return {"status": "success", "redirect_url": "/officer/login"}


@router.post("/api/login")
def login(phone: str = Form(...)):
    with session_scope() as session:
        contact = session.exec(
            select(Contact)
            .join(Household, Contact.household_id == Household.id)
            .where(Contact.phone == phone, Household.consent_given.is_(True))
        ).first()
        if contact is None:
            raise HTTPException(status_code=401, detail="Phone number not registered.")

        return {
            "status": "success",
            "role": "farmer",
            "phone": contact.phone,
            "full_name": contact.name,
            "redirect_url": "/farmer/home?phone=" + quote(contact.phone, safe=""),
        }
