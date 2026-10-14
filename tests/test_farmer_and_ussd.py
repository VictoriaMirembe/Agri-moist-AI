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
    assert any(f["reason"] == "H-009 asked for a visit: little food left" and f["raised_by"] == "farmer_ussd" for f in flags)


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


def test_sign_in_and_dashboard_accept_local_and_international_number_forms(client, monkeypatch):
    from backend.farmer import router as farmer_router

    monkeypatch.setattr(
        farmer_router.impl, "get_weather",
        lambda **kwargs: {"past_7_days_rain_mm": 1.0, "next_7_days_rain_mm": 2.0, "source_ref": "demo-override"},
    )
    for phone in ("+256700000003", "256700000003", "0700000003", "0700 000 003"):
        profile = client.get("/api/farmer/profile", params={"phone": phone})
        assert profile.status_code == 200, phone
        assert profile.json()["household_code"] == "H-003"
        dashboard = client.get("/api/farmer/dashboard", params={"phone": phone})
        assert dashboard.status_code == 200, phone
        assert dashboard.json()["profile"]["household_code"] == "H-003"


def test_visit_requests_name_the_household_on_both_channels(client):
    assert client.post("/api/visit-requests", json={"phone": "+256700000003", "reason": "Little food left"}).status_code == 200
    _ussd(client, "visit-h5", "+256700000005", "3*2")
    reasons = [f["reason"] for f in client.get("/api/flags").json()]
    assert "H-003 asked for a visit: little food left" in reasons
    assert "H-005 asked for a visit: little food left" in reasons


def test_ussd_followup_without_approved_advice_saves_nothing(client):
    # Group 2 (H-004) has only an unapproved draft.
    before_logs = len(client.get("/api/logs").json())
    for text in ("4", "4*1", "4*1*1"):
        assert _ussd(client, "no-advice", "+256700000004", text).startswith("END No advice to answer yet")
    assert not [f for f in client.get("/api/followups").json() if f["group_id"] == 2]
    assert len(client.get("/api/logs").json()) == before_logs


def test_ussd_followup_answer_is_logged(client):
    _ussd(client, "log-fu", "+256700000001", "4*2*3")
    rows = [r for r in client.get("/api/logs").json() if r["tool"] == "record_followup"]
    assert rows and rows[0]["source"] == "farmer_ussd" and rows[0]["actor"] == "farmer"
    assert '"followed": "partly"' in rows[0]["outputs"] and '"helped": "too_early"' in rows[0]["outputs"]


def test_followup_answers_from_two_households_are_both_kept(client):
    advice_id = client.get("/api/farmer/advice", params={"phone": "+256700000001"}).json()["advice_id"]
    web = client.post(
        "/api/farmer/followup",
        json={"phone": "+256700000001", "advice_id": advice_id, "followed": "yes", "helped": "yes"},
    )
    assert web.status_code == 200
    _ussd(client, "fu-h2", "+256700000002", "4*3*2")  # H-002, same group: no / no

    row = next(f for f in client.get("/api/followups").json() if f["advice_id"] == advice_id)
    answers = {a["household_code"]: (a["followed"], a["helped"], a["answered_via"]) for a in row["answers"]}
    assert answers == {"H-001": ("yes", "yes", "farmer_web"), "H-002": ("no", "no", "farmer_ussd")}
    # The existing Followup fields still hold the latest answer, as before.
    assert (row["followed"], row["helped"], row["answered_via"]) == ("no", "no", "farmer_ussd")

    # Answering again updates that household's own answer instead of adding a second one.
    _ussd(client, "fu-h2b", "+256700000002", "4*1*1")
    row = next(f for f in client.get("/api/followups").json() if f["advice_id"] == advice_id)
    assert len(row["answers"]) == 2
    assert {a["household_code"]: a["followed"] for a in row["answers"]}["H-002"] == "yes"


def test_web_pest_report_saves_the_pest_the_farmer_picked(client):
    for pest, expected in (("Fall armyworm", "fall armyworm"), (None, "farmer-reported pest"), ("   ", "farmer-reported pest")):
        body = {"phone": "+256700000006", "problem": "pests", "crop": "sorghum"}
        if pest is not None:
            body["pest"] = pest
        assert client.post("/api/farmer-reports", json=body).status_code == 200
        latest = client.get("/api/farmer-reports").json()[0]
        assert latest["household_code"] == "H-006" and latest["pest_seen"] == expected
    # A pest name is ignored for other problems.
    client.post("/api/farmer-reports", json={"phone": "+256700000006", "problem": "no_rain", "pest": "aphids"})
    assert client.get("/api/farmer-reports").json()[0]["pest_seen"] is None
    # USSD cannot name a pest, so it keeps the generic text.
    _ussd(client, "pest-u", "+256700000006", "2*1*1")
    assert client.get("/api/farmer-reports").json()[0]["pest_seen"] == "farmer-reported pest"


def test_reading_approved_advice_is_logged_without_changing_its_status(client):
    def viewed():
        return [r for r in client.get("/api/logs").json() if r["tool"] == "farmer_viewed_advice"]

    advice = client.get("/api/farmer/advice", params={"phone": "+256700000001"}).json()
    _ussd(client, "view-1", "+256700000001", "1")
    rows = viewed()
    assert {r["source"] for r in rows} == {"farmer_web", "farmer_ussd"}
    assert all(r["actor"] == "farmer" and '"household_code": "H-001"' in r["inputs"] for r in rows)
    assert all(f'"advice_id": {advice["advice_id"]}' in r["inputs"] for r in rows)
    # Status unchanged.
    assert client.get("/api/farmer/advice", params={"phone": "+256700000001"}).json()["status"] == advice["status"]
    # No approved advice: nothing to log.
    before = len(viewed())
    client.get("/api/farmer/advice", params={"phone": "+256700000004"})
    _ussd(client, "view-4", "+256700000004", "1")
    assert len(viewed()) == before


def test_crop_calendar_source_link_only_allows_http(client):
    page = client.get("/farmer/home", params={"phone": "+256700000001"}).text
    assert "if (safeURL(entry.source_url))" in page
    assert "source.href = entry.source_url;" in page.split("if (safeURL(entry.source_url))", 1)[1]


def _latest_report(client):
    return client.get("/api/farmer-reports").json()[0]


def test_typed_other_crop_is_saved_trimmed_and_lowercase(client):
    r = client.post("/api/farmer-reports", json={"phone": "+256700000006", "problem": "crop_sick", "crop": "  GroundNuts  "})
    assert r.status_code == 200
    assert _latest_report(client)["crop"] == "groundnuts"
    # Limited to 40 characters.
    client.post("/api/farmer-reports", json={"phone": "+256700000006", "problem": "crop_sick", "crop": "x" * 60})
    assert _latest_report(client)["crop"] == "x" * 40


def test_other_crop_with_an_empty_box_saves_no_crop(client):
    for crop in (None, "", "   "):
        body = {"phone": "+256700000006", "problem": "no_rain"}
        if crop is not None:
            body["crop"] = crop
        assert client.post("/api/farmer-reports", json=body).status_code == 200
        assert _latest_report(client)["crop"] is None


def test_sorghum_and_maize_are_saved_as_before(client):
    for crop in ("sorghum", "maize"):
        client.post("/api/farmer-reports", json={"phone": "+256700000006", "problem": "pests", "crop": crop})
        assert _latest_report(client)["crop"] == crop


def test_report_form_shows_a_crop_box_only_for_other(client):
    page = client.get("/farmer/report", params={"phone": "+256700000006"}).text
    assert '<div id="crop-other-field" hidden>' in page
    assert 'id="crop-other" type="text" maxlength="40"' in page
    assert "otherField.hidden = crop.value !== \"\";" in page
