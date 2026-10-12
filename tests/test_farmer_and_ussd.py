# Tests for the farmer web API and the simulated USSD menu
# (backend/farmer/router.py).
from __future__ import annotations


def _ussd(client, session_id: str, phone: str, text: str) -> str:
    r = client.post("/ussd", json={"sessionId": session_id, "phoneNumber": phone, "text": text})
    assert r.status_code == 200
    return r.text


def test_unregistered_number_is_rejected(client):
    reply = _ussd(client, "s1", "+10000000000", "")
    assert reply == "END This number is not registered. Please ask your extension officer."


def test_main_menu(client):
    reply = _ussd(client, "s1", "+256700000001", "")
    assert reply.startswith("CON AgriMoist")
    assert "1. My advice" in reply


def test_my_advice_before_approval_shows_no_new_advice(client):
    # EVALS task 13: an unapproved draft never appears, even to a farmer who checks
    # before the officer approves it. H-004 is in group 2, which has a seeded
    # pending_review draft that has NOT been approved yet.
    reply = _ussd(client, "s1", "+256700000004", "1")
    assert reply == "END No new advice yet. Your officer will send it after checking."


def test_my_advice_after_approval_shows_the_advice(client):
    pending = client.get("/api/approvals", params={"status": "pending_review"}).json()
    advice_id = pending[0]["id"]
    approve = client.post(f"/api/approvals/{advice_id}/approve", json={"officer_id": 1})
    assert approve.status_code == 200

    reply = _ussd(client, "s1", "+256700000004", "1")
    assert reply.startswith("END")
    assert "No new advice yet" not in reply


def test_report_pest_via_ussd_is_saved_unchecked(client):
    # EVALS task 14: farmer reports armyworm by USSD - saved as farmer-reported and
    # unchecked, and shows in the officer queue.
    r1 = _ussd(client, "s2", "+256700000008", "2")
    assert "What did you see?" in r1
    r2 = _ussd(client, "s2", "+256700000008", "2*1")
    assert "Which crop?" in r2
    r3 = _ussd(client, "s2", "+256700000008", "2*1*2")
    assert r3 == "END Thank you. Your officer will check this report."

    reports = client.get("/api/farmer-reports", params={"checked": "unchecked"}).json()
    report = next(r for r in reports if r["household_code"] == "H-008")
    assert report["pest_seen"] == "farmer-reported pest"
    assert report["source"] == "farmer_ussd"
    assert report["group_id"] == 3

    # And an officer can mark it checked.
    check = client.post(f"/api/farmer-reports/{report['id']}/check", json={"officer_id": 1})
    assert check.status_code == 200
    assert check.json()["checked"] == "checked"


def test_visit_request_via_ussd_creates_an_open_flag(client):
    r1 = _ussd(client, "s3", "+256700000009", "3")
    assert "Why?" in r1
    r2 = _ussd(client, "s3", "+256700000009", "3*2")
    assert r2 == "END Request sent. Your officer will plan a visit."

    flags = client.get("/api/flags", params={"status": "open"}).json()
    assert any("Little food left" in f["reason"] and f["raised_by"] == "farmer_ussd" for f in flags)


def test_followup_flow_via_ussd(client):
    # Group 1's household H-001 has a seeded, already-approved advice.
    r1 = _ussd(client, "s4", "+256700000001", "4")
    assert "Did you follow" in r1
    r2 = _ussd(client, "s4", "+256700000001", "4*1")
    assert "Did it help" in r2
    r3 = _ussd(client, "s4", "+256700000001", "4*1*1")
    assert r3 == "END Thank you. This helps improve advice."


def test_web_report_rejects_unregistered_phone(client):
    r = client.post("/api/farmer-reports", json={"phone": "+19999999999", "problem": "pests"})
    assert r.status_code == 404


def test_farmer_profile_resolves_the_registered_household(client):
    r = client.get("/api/farmer/profile", params={"phone": "+256700000001"})
    assert r.status_code == 200
    assert r.json() == {
        "full_name": "Akello Susan",
        "household_code": "H-001",
        "group_id": 1,
    }

    unknown = client.get("/api/farmer/profile", params={"phone": "+19999999999"})
    assert unknown.status_code == 404


def test_maria_akol_is_the_last_demo_farmer(client):
    response = client.get("/api/farmer/profile", params={"phone": "+256700000010"})

    assert response.status_code == 200
    assert response.json()["full_name"] == "Maria Akol"


def test_farmer_dashboard_returns_recorded_farm_data(client, monkeypatch):
    from backend.farmer import router as farmer_router

    monkeypatch.setattr(
        farmer_router.impl,
        "get_weather",
        lambda **kwargs: {
            "past_7_days_rain_mm": 12.5,
            "next_7_days_rain_mm": 18.0,
            "source_ref": "https://weather.example/forecast",
        },
    )

    response = client.get("/api/farmer/dashboard", params={"phone": "+256700000010"})

    assert response.status_code == 200
    dashboard = response.json()
    assert dashboard["profile"]["full_name"] == "Maria Akol"
    assert dashboard["profile"]["household_code"] == "H-010"
    assert dashboard["profile"]["village"] == "Iriiri"
    assert dashboard["profile"]["crops"] == ["sorghum"]
    assert dashboard["latest_observation"]["soil_wetness"] is None
    assert dashboard["weather"]["past_7_days_rain_mm"] == 12.5
    assert {crop["crop"] for crop in dashboard["crop_calendar"]} == {"maize", "sorghum"}
    assert all(crop["source_url"].startswith("https://") for crop in dashboard["crop_calendar"])


def test_farmer_dashboard_loads_registered_profile_and_approved_advice(client):
    response = client.get("/farmer/home", params={"phone": "+256700000001"})

    assert response.status_code == 200
    assert '<span id="farmer-name">farmer</span>' in response.text
    assert 'getJSON("/api/farmer/dashboard?phone="' in response.text
    assert 'getJSON("/api/farmer/advice?phone="' in response.text
    assert 'data-view-link="report"' in response.text
    assert 'data-view-link="visit"' in response.text
    assert "{%" not in response.text
    assert "{{" not in response.text
    assert response.text.index('<script src="../../static/app.js"></script>') < response.text.index(
        "async function loadDashboard()"
    )


def test_farmer_actions_share_the_dashboard_template(client):
    action_forms = {
        "/farmer/report": 'id="report-form"',
        "/farmer/visit": 'id="visit-form"',
        "/farmer/followup": 'id="followup-form"',
        "/farmer/advice": 'id="advice-detail"',
    }

    for path, expected in action_forms.items():
        response = client.get(path, params={"phone": "+256700000001"})
        assert response.status_code == 200
        assert expected in response.text
        assert "farmer-action-page" in response.text


def test_web_report_rejects_unknown_problem_type(client):
    r = client.post("/api/farmer-reports", json={"phone": "+256700000001", "problem": "locusts"})
    assert r.status_code == 400


def test_no_tool_exists_to_order_inputs_or_request_credit(client):
    # EVALS task 9 / README "Safety rules": the assistant can only draft; it can never act. There
    # is no endpoint of any kind that could order fertiliser, seed or credit.
    for path in ("/api/order", "/api/order-fertiliser", "/api/credit", "/api/request-credit", "/api/sms/send"):
        r = client.post(path, json={})
        assert r.status_code == 404

    from backend.mcp_server.server import mcp as agrimoist_mcp

    tool_names = set(agrimoist_mcp._tool_manager._tools.keys())
    assert tool_names == {
        "record_observation",
        "get_field_records",
        "get_plot_history",
        "get_crop_calendar",
        "get_weather",
        "get_pest_reports",
        "draft_advice",
        "flag_for_visit",
        "schedule_followup",
    }
