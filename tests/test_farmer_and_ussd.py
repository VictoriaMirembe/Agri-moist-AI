# Tests for the farmer web API and USSD callback
# (backend/farmer/router.py, backend/ussd_router.py, USSD_MENU.md).
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


def test_ussd_accepts_africas_talking_form_callback(client):
    response = client.post(
        "/ussd",
        data={
            "sessionId": "at-session-1",
            "serviceCode": "*123#",
            "phoneNumber": "+256700000001",
            "text": "",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text.startswith("CON AgriMoist")


def test_ussd_form_callback_keeps_approved_advice_gate(client):
    pending = client.get("/api/approvals", params={"status": "pending_review"}).json()[0]
    approved = client.post(
        f"/api/approvals/{pending['id']}/approve",
        json={"officer_id": 1},
    )
    assert approved.status_code == 200

    response = client.post(
        "/ussd",
        data={
            "sessionId": "at-session-2",
            "serviceCode": "*123#",
            "phoneNumber": "+256700000004",
            "text": "1",
        },
    )

    assert response.status_code == 200
    assert response.text.startswith("END")
    assert "Heavy rain fell at H-004" in response.text


def test_ussd_form_callback_records_farmer_report_for_registered_group(client):
    response = client.post(
        "/ussd",
        data={
            "sessionId": "at-session-3",
            "serviceCode": "*123#",
            "phoneNumber": "+256700000008",
            "text": "2*1*2",
        },
    )

    assert response.status_code == 200
    assert response.text == "END Thank you. Your officer will check this report."
    reports = client.get("/api/farmer-reports", params={"checked": "unchecked"}).json()
    report = next(row for row in reports if row["household_code"] == "H-008")
    assert report["source"] == "farmer_ussd"
    assert report["group_id"] == 3


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


def test_farmer_sign_in_page_uses_demo_login_endpoint(client):
    response = client.get("/farmer")

    assert response.status_code == 200
    assert "TerraMoist AI" in response.text
    assert 'id="sign-in-form"' in response.text
    assert 'fetch("/api/login"' in response.text
    assert "result.redirect_url" in response.text


def test_farmer_dashboard_loads_registered_name_and_approved_advice(client):
    response = client.get("/farmer/home", params={"phone": "+256700000001"})

    assert response.status_code == 200
    assert 'id="farmer-name">farmer</span>' in response.text
    assert 'getJSON("/api/farmer/profile?phone="' in response.text
    assert 'getJSON("/api/farmer/advice?phone="' in response.text
    assert "/farmer/report?phone=%2B256700000001" in response.text


def test_demo_login_resolves_registered_farmer(client):
    response = client.post("/api/login", data={"phone": "+256700000001"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "role": "farmer",
        "phone": "+256700000001",
        "full_name": "Akello Susan",
        "redirect_url": "/farmer/home?phone=%2B256700000001",
    }


def test_demo_login_rejects_unregistered_phone(client):
    response = client.post("/api/login", data={"phone": "+19999999999"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Phone number not registered."


def test_web_report_rejects_unknown_problem_type(client):
    r = client.post("/api/farmer-reports", json={"phone": "+256700000001", "problem": "locusts"})
    assert r.status_code == 400


def test_no_tool_exists_to_order_inputs_or_request_credit(client):
    # EVALS task 9 / RULES.md: the assistant can only draft; it can never act. There
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
