# Farmer <-> officer sync: everything a farmer sends (web or USSD) shows on the officer
# screens' APIs straight away, and every officer decision shows back on the farmer's
# dashboard, web advice and USSD menu. One shared database, no copies to drift apart.


def _ussd(client, session_id: str, phone: str, text: str) -> str:
    return client.post("/ussd", data={"sessionId": session_id, "phoneNumber": phone, "text": text}).text


def _farmer_requests(client, phone: str) -> dict[str, str]:
    dashboard = client.get("/api/farmer/dashboard", params={"phone": phone}).json()
    return {item["summary"]: item["status"] for item in dashboard["recent_requests"]}


def test_farmer_reports_from_web_and_ussd_reach_the_officer_and_back(client):
    before = client.get("/api/pending-summary").json()["unchecked_reports"]

    assert client.post(
        "/api/farmer-reports", json={"phone": "+256700000004", "problem": "no_rain", "crop": "sorghum"}
    ).status_code in (200, 201)
    assert _ussd(client, "sync-1", "+256700000005", "2*1*2").startswith("END Thank you")

    reports = client.get("/api/farmer-reports", params={"checked": "unchecked"}).json()
    by_household = {r["household_code"]: r for r in reports}
    assert by_household["H-004"]["source"] == "farmer_web"
    assert by_household["H-005"]["source"] == "farmer_ussd"
    assert client.get("/api/pending-summary").json()["unchecked_reports"] == before + 2

    # Officer checks the USSD report -> the farmer sees it as checked.
    assert "Waiting for officer" in _farmer_requests(client, "+256700000005").values()
    client.post(f"/api/farmer-reports/{by_household['H-005']['id']}/check", json={"officer_id": 1})
    assert "Checked by officer" in _farmer_requests(client, "+256700000005").values()
    assert client.get("/api/pending-summary").json()["unchecked_reports"] == before + 1


def test_visit_requests_reach_the_officer_and_visits_show_to_the_farmer(client):
    client.post("/api/visit-requests", json={"phone": "+256700000007", "reason": "Pests"})
    assert _ussd(client, "sync-2", "+256700000008", "3*2").startswith("END Request sent")

    flags = client.get("/api/flags", params={"status": "open"}).json()
    reasons = {f["reason"]: f for f in flags}
    assert "H-007 asked for a visit: pests" in reasons
    assert "H-008 asked for a visit: little food left" in reasons
    assert reasons["H-008 asked for a visit: little food left"]["raised_by"] == "farmer_ussd"

    flag_id = reasons["H-008 asked for a visit: little food left"]["id"]
    client.post(f"/api/flags/{flag_id}/visited", json={"officer_id": 1})
    assert _farmer_requests(client, "+256700000008")["Visit request: Little food left"] == "Officer visited"


def test_approval_reaches_web_and_ussd_and_answers_reach_the_officer(client):
    pending = client.get("/api/approvals", params={"status": "pending_review"}).json()[0]
    phone = "+256700000004"  # household H-004, in the pending draft's group
    assert _ussd(client, "sync-3", phone, "1").startswith("END No new advice yet")

    client.post(f"/api/approvals/{pending['id']}/approve", json={"officer_id": 1})
    advice = client.get("/api/farmer/advice", params={"phone": phone}).json()
    assert advice["advice_id"] == pending["id"] and advice["officer_name"]
    assert _ussd(client, "sync-4", phone, "1").startswith("END " + advice["text"][:20])

    # One household answers on the web, another by USSD: the officer sees both.
    client.post("/api/farmer/followup", json={"phone": phone, "advice_id": pending["id"], "followed": "yes", "helped": "yes"})
    assert _ussd(client, "sync-5", "+256700000005", "4*3*2").startswith("END Thank you")
    followup = next(f for f in client.get("/api/followups").json() if f["advice_id"] == pending["id"])
    answers = {a["household_code"]: a for a in followup["answers"]}
    assert answers["H-004"]["answered_via"] == "farmer_web" and answers["H-004"]["helped"] == "yes"
    assert answers["H-005"]["answered_via"] == "farmer_ussd" and answers["H-005"]["followed"] == "no"


def test_farmer_updates_endpoint_does_not_count_as_a_read(client):
    phone = "+256700000001"
    def reads():
        return sum(1 for r in client.get("/api/logs").json() if r["tool"] == "farmer_viewed_advice")
    start = reads()
    updates = client.get("/api/farmer/updates", params={"phone": phone}).json()
    assert {"advice", "recent_requests"} <= set(updates)
    assert reads() == start  # background refreshes are not the farmer reading advice


def test_translation_cost_is_added_to_the_run_and_the_week(client, monkeypatch):
    from backend.approvals import router as approvals_router

    monkeypatch.setattr(
        approvals_router,
        "translate_text",
        lambda text, lang: {"translated_text": "(kdj)", "model": "sunbird/sunflower-9b", "cost": 0.0004},
    )
    pending = client.get("/api/approvals", params={"status": "pending_review"}).json()[0]
    run_id = pending["run_id"]
    before = client.get(f"/api/runs/{run_id}").json()["cost"]
    week_before = client.get("/api/week-at-a-glance").json()

    client.post(f"/api/approvals/{pending['id']}/approve", json={"officer_id": 1})

    assert abs(client.get(f"/api/runs/{run_id}").json()["cost"] - (before + 0.0004)) < 1e-9
    week = client.get("/api/week-at-a-glance").json()
    assert abs(week["ai_cost_translation"] - (week_before["ai_cost_translation"] + 0.0004)) < 1e-9


def test_week_numbers_move_when_a_farmer_sends_something(client):
    before = client.get("/api/week-at-a-glance").json()
    _ussd(client, "sync-6", "+256700000006", "2*1*1")
    after = client.get("/api/week-at-a-glance").json()
    assert after["messages_total"] == before["messages_total"] + 1
    assert after["messages_unchecked"] == before["messages_unchecked"] + 1


def test_simulator_lists_registered_numbers(client):
    page = client.get("/ussd").text
    assert "+256700000004" in page and "H-004" in page
    assert "Menu map" in page and 'id="sent-to-officer"' in page
