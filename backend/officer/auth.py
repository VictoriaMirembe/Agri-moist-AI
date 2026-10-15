# Officer sign-in checks. The only proof of a signed-in officer is the signed session
# cookie (SessionMiddleware in backend/main.py), set after the demo password is given
# at /officer/login or POST /api/auth/login. A plain cookie or header saying
# "officer" proves nothing and is ignored: anyone can send those.
from __future__ import annotations

import secrets
from urllib.parse import quote

from fastapi import HTTPException, Request

from backend.core.config import settings


def password_ok(password: str | None) -> bool:
    return secrets.compare_digest((password or "").encode(), settings.officer_demo_password.encode())


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
    """{"officer_id", "officer_name"} from the signed session, or None."""
    officer_id = signed_in_officer_id(request)
    if officer_id is None:
        return None
    return {"officer_id": officer_id, "officer_name": request.session.get("officer_name")}


def sign_in(request: Request, officer_id: int, officer_name: str) -> None:
    request.session.clear()
    request.session.update(role="officer", officer_id=officer_id, officer_name=officer_name)


def require_officer(request: Request) -> dict:
    """For the JSON API: 403 unless an officer is signed in."""
    officer = signed_in_officer(request)
    if officer is None:
        raise HTTPException(status_code=403, detail="Access denied: Officers only.")
    return officer


def require_officer_page(request: Request) -> dict:
    """For officer screens: send anyone not signed in to the sign-in page, then back."""
    officer = signed_in_officer(request)
    if officer is None:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise HTTPException(status_code=303, headers={"Location": f"/officer/login?next={quote(target)}"})
    return officer
