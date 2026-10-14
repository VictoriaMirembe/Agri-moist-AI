# Tests for server-side role protection (FastAPI session/cookie) and real-time APIs (Open-Meteo).
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app


def test_unauthenticated_followups_denied(unauthenticated_client):
    """Accessing /followups without an authenticated officer session returns 403."""
    res = unauthenticated_client.get("/followups")
    assert res.status_code == 403
    assert res.json().get("detail") == "Access denied: Officers only."


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

        page_res = client.get("/followups")
        assert page_res.status_code == 403


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
    login_res = unauthenticated_client.post("/api/auth/login", json={"officer_id": 1, "role": "officer"})
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
    """The /officer/login page renders successfully with profile selector."""
    res = unauthenticated_client.get("/officer/login")
    assert res.status_code == 200
    assert "Sign in as this officer" in res.text
    assert "Among Betty" in res.text


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
