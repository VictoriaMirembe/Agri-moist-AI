# Tests for the officer API: recording visits, browsing groups, and the permanent
# log (backend/officer/router.py).
from __future__ import annotations


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
    for path in ("/", "/visits/new", "/groups/1", "/approvals", "/farmer-reports", "/flags", "/log"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"


def test_farmer_pages_render(client):
    for path in ("/farmer", "/ussd"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"
    for path in ("/farmer/home", "/farmer/advice", "/farmer/report", "/farmer/visit", "/farmer/followup"):
        r = client.get(path, params={"phone": "+256700000001"})
        assert r.status_code == 200, f"{path} returned {r.status_code}"
