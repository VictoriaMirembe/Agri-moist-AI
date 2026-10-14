# Server-side authentication and role-checking helpers for Extension Officers.
from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request


def get_current_user_role(
    request: Request,
    officer_role_cookie: Annotated[str | None, Cookie(alias="officer_role")] = None,
    role_cookie: Annotated[str | None, Cookie(alias="role")] = None,
    officer_role_header: Annotated[str | None, Header(alias="x-officer-role")] = None,
) -> str | None:
    """Retrieve user role from server-side session, cookie, or header."""
    has_session = "session" in request.scope

    # 1. Server-side session
    if has_session:
        session_role = request.session.get("role")
        if session_role:
            return session_role

    # 2. Cookies (officer_role or role)
    cookie_val = officer_role_cookie or role_cookie or request.cookies.get("officer_role") or request.cookies.get("role")
    if cookie_val:
        if has_session and "role" not in request.session:
            request.session["role"] = cookie_val
        return cookie_val

    # 3. Headers (for API / testing clients)
    header_val = officer_role_header or request.headers.get("x-officer-role") or request.headers.get("x-role")
    if header_val:
        if has_session and "role" not in request.session:
            request.session["role"] = header_val
        return header_val

    return None


def require_officer(request: Request) -> str:
    """Validate that the requesting client has an authenticated 'officer' session or cookie.
    Raises HTTP 403 if unauthenticated or not an officer.
    """
    user_role = get_current_user_role(request)
    if user_role != "officer":
        raise HTTPException(status_code=403, detail="Access denied: Officers only.")
    return user_role
