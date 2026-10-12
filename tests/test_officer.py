# Tests for the officer API: recording visits, browsing groups, and the permanent
# log (backend/officer/router.py).
from __future__ import annotations

from backend.core.database import session_scope
from backend.db.models import Officer


def test_record_visit_shows_up_through_get_field_records(client):
    r = client.post(
        "/api/visits",
        json={
            "household_code": "H-008",
            "officer_id": 1,
            "crop": "maize",
            "soil_wetness": "dry",
            "rain_last_7_days": "none",
            "crop_condition": "fair",
            "food_left": "enough",
            "notes": "pytest visit",
        },
    )
    assert r.status_code == 200
    group_id = r.json()["group_id"]

    detail = client.get(f"/api/groups/{group_id}").json()
    notes = [rec["notes"] for rec in detail["field_records"]["records"]]
    assert "pytest visit" in notes


def test_visit_for_unknown_household_is_rejected(client):
    r = client.post("/api/visits", json={"household_code": "H-999", "officer_id": 1})
    assert r.status_code == 400


def test_every_recorded_visit_is_logged(client):
    r = client.post(
        "/api/visits",
        json={"household_code": "H-001", "officer_id": 1, "soil_wetness": "moist"},
    )
    observation_id = r.json()["observation_id"]

    logs = client.get("/api/logs").json()
    matching = [
        row
        for row in logs
        if row["tool"] == "record_observation" and f'"observation_id": {observation_id}' in row["outputs"]
    ]
    assert matching
    assert matching[0]["actor"] == "1"
    assert matching[0]["source"] == "officer_visit"
    assert matching[0]["error"] is None


def test_groups_list_shows_counts(client):
    groups = client.get("/api/groups").json()
    assert len(groups) == 3  # seeded: groups 1, 2, 3
    group1 = next(g for g in groups if g["group_id"] == 1)
    assert group1["household_count"] == 3
    assert "H-001" in group1["household_codes"]


def test_officer_pages_render(client):
    for path in (
        "/officer/dashboard",
        "/visits/new",
        "/groups/1",
        "/approvals",
        "/farmer-reports",
        "/followups",
        "/flags",
        "/log",
    ):
        r = client.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"


def test_officer_routes_require_a_signed_in_officer(client):
    client.cookies.clear()

    assert client.get("/followups").status_code == 403
    assert client.get("/api/followups").status_code == 403
    assert client.get("/officer/dashboard").status_code == 403

    landing = client.get("/", follow_redirects=False)
    assert landing.status_code == 303
    assert landing.headers["location"] == "/farmer"
    assert client.get(landing.headers["location"]).status_code == 200

    login_page = client.get("/officer/login")
    assert login_page.status_code == 200
    assert 'id="officer-login-form"' in login_page.text


def test_officer_login_creates_session_and_logout_clears_it(client):
    client.cookies.clear()

    invalid = client.post(
        "/api/officer/login",
        data={"username": "test-officer", "password": "wrong"},
    )
    assert invalid.status_code == 401

    login = client.post(
        "/api/officer/login",
        data={"username": "test-officer", "password": "test-officer-password"},
    )
    assert login.status_code == 200
    assert login.json() == {
        "status": "success",
        "role": "officer",
        "redirect_url": "/officer/dashboard",
    }
    assert client.get("/api/followups").status_code == 200
    officer_landing = client.get("/", follow_redirects=False)
    assert officer_landing.status_code == 303
    assert officer_landing.headers["location"] == "/officer/dashboard"

    with session_scope() as session:
        officer = session.get(Officer, 1)
        assert officer is not None
        assert officer.login_username == "test-officer"
        assert officer.password_hash is not None
        assert officer.password_hash.startswith("pbkdf2_sha256$")
        assert officer.password_hash != "test-officer-password"

    logout = client.post("/api/officer/logout")
    assert logout.status_code == 200
    assert client.get("/api/followups").status_code == 403


def test_officer_actions_are_bound_to_signed_in_officer(client):
    officers = client.get("/api/officers").json()
    assert len(officers) == 1
    assert officers[0]["id"] == 1

    pending = client.get("/api/approvals", params={"status": "pending_review"}).json()[0]
    approve = client.post(
        f"/api/approvals/{pending['id']}/approve",
        json={"officer_id": 2},
    )
    assert approve.status_code == 403

    visit = client.post(
        "/api/visits",
        json={"household_code": "H-001", "officer_id": 2},
    )
    assert visit.status_code == 403


def test_farmer_pages_render(client):
    for path in ("/farmer", "/ussd"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"
    for path in ("/farmer/home", "/farmer/advice", "/farmer/report", "/farmer/visit", "/farmer/followup"):
        r = client.get(path, params={"phone": "+256700000001"})
        assert r.status_code == 200, f"{path} returned {r.status_code}"


def test_farmer_followup_is_visible_to_officers(client):
    advice = client.get("/api/farmer/advice", params={"phone": "+256700000001"}).json()
    assert advice["has_advice"] is True

    response = client.post(
        "/api/farmer/followup",
        json={
            "phone": "+256700000001",
            "advice_id": advice["advice_id"],
            "followed": "partly",
            "helped": "yes",
        },
    )
    assert response.status_code == 200

    followups = client.get("/api/followups")
    assert followups.status_code == 200
    result = next(row for row in followups.json() if row["advice_id"] == advice["advice_id"])
    assert result["group_id"] == 1
    assert result["followed"] == "partly"
    assert result["helped"] == "yes"
    assert result["answered_via"] == "farmer_web"


def test_farmer_cannot_answer_unapproved_advice(client):
    pending_advice = client.get("/api/approvals", params={"status": "pending_review"}).json()[0]

    response = client.post(
        "/api/farmer/followup",
        json={
            "phone": "+256700000004",
            "advice_id": pending_advice["id"],
            "followed": "yes",
            "helped": "no",
        },
    )
    assert response.status_code == 404


def test_farmer_cannot_answer_approved_advice_for_another_group(client):
    pending_advice = client.get("/api/approvals", params={"status": "pending_review"}).json()[0]
    approval = client.post(
        f"/api/approvals/{pending_advice['id']}/approve",
        json={"officer_id": 1},
    )
    assert approval.status_code == 200

    response = client.post(
        "/api/farmer/followup",
        json={
            "phone": "+256700000001",
            "advice_id": pending_advice["id"],
            "followed": "yes",
            "helped": "no",
        },
    )
    assert response.status_code == 404
