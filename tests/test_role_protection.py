# Tests for server-side role protection (FastAPI session/cookie) and real-time APIs (Open-Meteo).
from __future__ import annotations

from backend.db.seed import DEMO_OFFICER_PASSWORD
import pytest
from fastapi.testclient import TestClient

from backend.core.config import settings
from backend.main import app


def test_unauthenticated_followups_denied(unauthenticated_client):
    """Opening /followups without a signed-in officer goes to the sign-in page first."""
    res = unauthenticated_client.get("/followups", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/officer/login?next=/followups"


def test_unauthenticated_api_followups_denied(unauthenticated_client):
    """Accessing /api/followups without officer role returns 403 Forbidden."""
    res = unauthenticated_client.get("/api/followups")
    assert res.status_code == 403
    assert res.json().get("detail") == "Access denied: Officers only."


def test_farmer_role_cannot_access_officer_followups(test_db):
    """A user with role='farmer' receives 403 when accessing officer routes."""
    with TestClient(app, cookies={"role": "farmer"}) as client:
        res = client.get("/api/followups")
        assert res.status_code == 403
        assert res.json().get("detail") == "Access denied: Officers only."

        page_res = client.get("/followups", follow_redirects=False)
        assert page_res.status_code == 303


def test_authenticated_officer_can_access_followups(client):
    """An authenticated officer with valid session/cookie gets 200 OK."""
    res = client.get("/api/followups")
    assert res.status_code == 200
    assert isinstance(res.json(), list)

    page_res = client.get("/followups")
    assert page_res.status_code == 200


def test_officer_login_and_logout_flow(unauthenticated_client):
    """Logging in sets the session/cookie; logging out clears it."""
    # 1. Unauthenticated initially
    me_before = unauthenticated_client.get("/api/auth/me").json()
    assert me_before["authenticated"] is False

    # 2. Log in as Among Betty (officer_id 1)
    login_res = unauthenticated_client.post(
        "/api/auth/login", json={"officer_id": 1, "password": DEMO_OFFICER_PASSWORD}
    )
    assert login_res.status_code == 200
    assert login_res.json()["status"] == "authenticated"
    assert login_res.json()["role"] == "officer"

    # 3. /api/auth/me confirms authenticated officer
    me_after = unauthenticated_client.get("/api/auth/me").json()
    assert me_after["authenticated"] is True
    assert me_after["role"] == "officer"

    # 4. Officer can now access /api/followups
    followups_res = unauthenticated_client.get("/api/followups")
    assert followups_res.status_code == 200

    # 5. Log out
    logout_res = unauthenticated_client.post("/api/auth/logout")
    assert logout_res.status_code == 200
    assert logout_res.json()["status"] == "logged_out"

    # 6. Subsequent request without session is denied again
    after_logout = unauthenticated_client.get("/api/followups")
    assert after_logout.status_code == 403


def test_officer_login_page_renders(unauthenticated_client):
    """/officer/login is a full sign-in screen: phone and password, no list of
    officers, no shared password, and none of the officer app around it."""
    res = unauthenticated_client.get("/officer/login")
    assert res.status_code == 200
    assert 'name="phone"' in res.text and 'name="password"' in res.text
    assert "Among Betty" not in res.text
    assert DEMO_OFFICER_PASSWORD not in res.text
    assert "o-sidebar" not in res.text and "Approve advice" not in res.text


@pytest.mark.parametrize(
    "path", ["/", "/approvals", "/farmer-reports", "/flags", "/visits/new", "/log", "/mcp", "/groups/1", "/runs/1"]
)
def test_officer_screens_redirect_to_sign_in(unauthenticated_client, path):
    res = unauthenticated_client.get(path, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/officer/login?next=")


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/approvals"),
        ("post", "/api/approvals/1/approve"),
        ("post", "/api/approvals/1/reject"),
        ("post", "/api/visits"),
        ("post", "/api/groups/1/draft"),
        ("get", "/api/farmer-reports"),
        ("post", "/api/flags/1/visited"),
        ("get", "/api/logs"),
        ("get", "/api/week-at-a-glance"),
    ],
)
def test_officer_api_needs_sign_in(unauthenticated_client, method, path):
    res = getattr(unauthenticated_client, method)(path, **({"json": {}} if method == "post" else {}))
    assert res.status_code == 403


def test_forged_role_cookie_or_header_is_not_a_sign_in(test_db):
    """A plain cookie or header saying "officer" is not the signed session."""
    with TestClient(app, cookies={"officer_role": "officer", "role": "officer"}) as client:
        assert client.get("/api/approvals").status_code == 403
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.get("/api/auth/me").json()["authenticated"] is False
    with TestClient(app, headers={"x-officer-role": "officer", "x-role": "officer"}) as client:
        assert client.get("/api/approvals").status_code == 403
        assert client.post("/api/approvals/1/approve", json={"officer_id": 1}).status_code == 403


def test_wrong_password_is_refused(unauthenticated_client):
    res = unauthenticated_client.post("/api/auth/login", json={"officer_id": 1, "password": "guess"})
    assert res.status_code == 401
    assert unauthenticated_client.get("/api/approvals").status_code == 403

    page = unauthenticated_client.post(
        "/officer/login", data={"phone": "+256780000001", "password": "guess", "next": "/approvals"}, follow_redirects=False
    )
    assert page.status_code == 401
    assert "do not match an officer account" in page.text


def test_sign_in_form_returns_to_the_page_asked_for(unauthenticated_client):
    res = unauthenticated_client.post(
        "/officer/login",
        data={"phone": "0780 000 002", "password": DEMO_OFFICER_PASSWORD, "next": "/approvals"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/approvals"
    assert unauthenticated_client.get("/approvals").status_code == 200
    assert unauthenticated_client.get("/api/auth/me").json()["officer_id"] == 2


def test_sign_in_never_redirects_off_site(unauthenticated_client):
    res = unauthenticated_client.post(
        "/officer/login",
        data={"phone": "+256780000001", "password": DEMO_OFFICER_PASSWORD, "next": "//evil.example"},
        follow_redirects=False,
    )
    assert res.headers["location"] == "/"


def test_farmer_screens_need_the_farmers_pin(unauthenticated_client):
    c = unauthenticated_client
    assert c.get("/farmer/login").status_code == 200
    # Farmer pages and farmer data are closed until the farmer signs in.
    r = c.get("/farmer/home", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/farmer/login")
    assert c.get("/api/farmer/profile", params={"phone": "+256700000001"}).status_code == 401
    # The USSD simulator lists real farmers' numbers, so it is for officers only.
    assert c.get("/ussd", follow_redirects=False).status_code == 303

    bad = c.post("/farmer/login", data={"phone": "0700000001", "pin": "0000"}, follow_redirects=False)
    assert bad.status_code == 401
    ok = c.post("/farmer/login", data={"phone": "0700000001", "pin": "2580"}, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/farmer/home"
    assert c.get("/farmer/home").status_code == 200
    assert c.get("/api/farmer/profile").json()["household_code"] == "H-001"
    # A farmer sees only their own farm, and is not an officer.
    assert c.get("/api/farmer/profile", params={"phone": "+256700000002"}).status_code == 403
    assert c.get("/api/approvals").status_code == 403
    assert c.get("/", follow_redirects=False).status_code == 303


def test_farmer_pin_locks_after_repeated_wrong_tries(unauthenticated_client):
    c = unauthenticated_client
    for _ in range(5):
        assert c.post("/farmer/login", data={"phone": "0700000003", "pin": "9999"}).status_code == 401
    locked = c.post("/farmer/login", data={"phone": "0700000003", "pin": "2580"})
    assert locked.status_code == 401 and "Too many wrong tries" in locked.text


def test_live_weather_api_returns_open_meteo_data(client):
    """The /api/weather endpoint fetches live data for villages from Open-Meteo."""
    res = client.get("/api/weather")
    assert res.status_code == 200
    data = res.json()
    assert "Open-Meteo" in data["source"]
    assert len(data["villages"]) >= 2

    # Check village weather structure
    for v in data["villages"]:
        assert "village" in v
        assert "latitude" in v
        assert "longitude" in v
        assert "past_7_days_rain_mm" in v
        assert "next_7_days_rain_mm" in v
        assert "condition" in v
        assert "source" in v
        assert v["status"] in ("live", "override", "unavailable", "error")


def test_live_dashboard_api_aggregates_weather_and_approvals(client):
    """The /api/dashboard/live endpoint integrates Open-Meteo weather and enforces
    that all recommendations require officer approval."""
    res = client.get("/api/dashboard/live")
    assert res.status_code == 200
    data = res.json()

    # Human in the loop safeguards
    assert data["human_in_the_loop"] is True
    assert "Officer" in data["officer_approval_policy"]

    # Counts and sections
    assert "counts" in data
    assert "drafts_waiting" in data["counts"]
    assert "open_flags" in data["counts"]
    assert "unchecked_reports" in data["counts"]
    assert "weather" in data
    assert len(data["weather"]) >= 2
    assert "sync_time" in data
