# Server-side authentication and role-checking helpers for Extension Officers.
# An officer counts as signed in ONLY through the signed session cookie
# (Starlette SessionMiddleware, see backend/main.py), which is set by the officer
# sign-in routes. Plain cookies or headers that claim a role are never trusted:
# anyone can send those.
from __future__ import annotations

from fastapi import HTTPException, Request


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


def require_officer(request: Request) -> str:
    """Validate that the request carries a signed-in officer session.
    Raises HTTP 403 if unauthenticated or not an officer.
    """
    if signed_in_officer_id(request) is None:
        raise HTTPException(status_code=403, detail="Access denied: Officers only.")
    return "officer"
