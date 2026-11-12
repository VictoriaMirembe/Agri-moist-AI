# Officer approvals over USSD (backend/officer/ussd.py), draft expiry and "decide
# once". The USSD tests use a client with NO web sign-in: the officer id and PIN typed
# on the phone are the only sign-in. Demo PINs: officer 1 = 1234, officer 2 = 5678.
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

OFFICER_CODE = "*384*5952#"
FARMER_CODE = "*384*5951#"
EXPIRED = "Expired: please re-draft"


@pytest.fixture(autouse=True)
def offline_settings(monkeypatch):
    """Pin the settings these tests rely on, so a local .env cannot change them."""
    from backend.core.config import settings

    monkeypatch.setattr(settings, "ussd_service_code", FARMER_CODE)
    monkeypatch.setattr(settings, "ussd_officer_service_code", OFFICER_CODE)
    monkeypatch.setattr(settings, "ussd_callback_token", "")
    monkeypatch.setattr(settings, "ussd_pin_max_attempts", 3)
    monkeypatch.setattr(settings, "ussd_pin_lock_minutes", 15)
    monkeypatch.setattr(settings, "draft_expiry_hours", 72)
    return settings


@pytest.fixture()
def phone(unauthenticated_client):
    """A basic phone: no web session at all."""
    return unauthenticated_client


def _dial(client, session_id: str, text: str) -> str:
    r = client.post(
        "/ussd",
        data={"sessionId": session_id, "serviceCode": OFFICER_CODE, "phoneNumber": "+256770000001", "text": text},
    )
    assert r.status_code == 200
    return r.text


def _pending_id() -> int:
    from backend.core.database import session_scope
    from backend.db.models import Advice

    with session_scope() as session:
        return session.exec(select(Advice.id).where(Advice.status == "pending_review")).one()


def _status(advice_id: int) -> str:
    from backend.core.database import session_scope
    from backend.db.models import Advice

    with session_scope() as session:
        return session.get(Advice, advice_id).status


def _approvals(advice_id: int) -> list:
    from backend.core.database import session_scope
    from backend.db.models import Approval

    with session_scope() as session:
        return session.exec(select(Approval).where(Approval.advice_id == advice_id)).all()


def _decision_logs(client, tool: str) -> list[dict]:
    return [row for row in client.get("/api/logs").json() if row["tool"] == tool]


def _make_old(advice_id: int, hours: float) -> None:
    from backend.core.database import session_scope
    from backend.db.models import Advice

    with session_scope() as session:
        advice = session.get(Advice, advice_id)
        advice.created_at = datetime.now(timezone.utc) - timedelta(hours=hours)
        session.add(advice)
        session.commit()


def _new_draft(group_id: int, text: str) -> int:
    from backend.core.database import session_scope
    from backend.db.models import Advice

    with session_scope() as session:
        advice = Advice(group_id=group_id, status="pending_review", draft_text=text, why_text="test")
        session.add(advice)
        session.commit()
        return advice.id


# --- sign-in -----------------------------------------------------------------


def test_officer_menu_asks_for_id_then_pin_then_lists_drafts(phone):
    assert _dial(phone, "o1", "") == "CON AgriMoist officers\nEnter your officer id:"
    assert _dial(phone, "o1", "2") == "CON Enter your 4-digit PIN:"
    listing = _dial(phone, "o1", "2*5678")
    assert listing == (
        "CON Drafts waiting:\n"
        "1. Group 2 Lokopo: Heavy rain fell at H-004 in the last 7 days; hold off on watering this week."
    )


def test_list_shows_at_most_three_drafts_newest_first_and_short(phone):
    long_text = "Long advice " + "x" * 300
    ids = [_new_draft(1, f"Draft number {n}") for n in range(3)]
    newest = _new_draft(1, long_text)
    lines = _dial(phone, "o2", "1*1234").splitlines()[1:]
    assert len(lines) == 3
    assert lines[0].startswith("1. Group 1") and "Long advice" in lines[0]
    assert len(lines[0].split(": ", 1)[1]) <= 100 and lines[0].endswith("...")
    assert "Draft number 2" in lines[1] and "Draft number 1" in lines[2]
    assert newest > ids[-1]


def test_pins_are_stored_only_as_a_hash(phone):
    from backend.core.database import session_scope
    from backend.db.models import OfficerPin
    from backend.officer.pins import verify_pin

    with session_scope() as session:
        rows = session.exec(select(OfficerPin)).all()
    assert {r.officer_id for r in rows} == {1, 2}
    for row, pin in zip(sorted(rows, key=lambda r: r.officer_id), ("1234", "5678")):
        assert row.pin_hash.startswith("scrypt$") and pin not in row.pin_hash
        assert verify_pin(pin, row.pin_hash) and not verify_pin("0000", row.pin_hash)
    assert rows[0].pin_hash != rows[1].pin_hash


def test_the_pin_is_never_saved_with_the_ussd_session(phone):
    from backend.core.database import session_scope
    from backend.db.models import UssdSession

    _dial(phone, "o3", "2*5678")
    _dial(phone, "o3", "2*5678*1")
    with session_scope() as session:
        assert "5678" not in session.get(UssdSession, "o3").last_screen


def test_wrong_pin_is_refused_and_nothing_is_approved(phone):
    advice_id = _pending_id()
    assert _dial(phone, "w1", "2*0000*1*1") == "END Wrong officer id or PIN."
    assert _dial(phone, "w2", "999*1234") == "END Wrong officer id or PIN."  # unknown officer: same words
    assert _dial(phone, "w3", "2*1234") == "END Wrong officer id or PIN."  # another officer's PIN
    assert _status(advice_id) == "pending_review"
    assert _approvals(advice_id) == []


def test_three_wrong_pins_lock_the_officer_even_for_the_right_pin(phone):
    advice_id = _pending_id()
    assert _dial(phone, "l1", "2*0000") == "END Wrong officer id or PIN."
    assert _dial(phone, "l2", "2*1111") == "END Wrong officer id or PIN."
    assert _dial(phone, "l3", "2*2222") == "END Too many wrong PINs. Try again later."
    assert _dial(phone, "l4", "2*5678*1*1") == "END Too many wrong PINs. Try again later."
    assert _status(advice_id) == "pending_review"
    # The other officer is not locked out.
    assert _dial(phone, "l5", "1*1234").startswith("CON Drafts waiting:")


def test_lock_ends_after_the_lock_time(phone):
    from backend.core.database import session_scope
    from backend.db.models import OfficerPin

    for n in range(3):
        _dial(phone, f"t{n}", "2*0000")
    with session_scope() as session:
        record = session.get(OfficerPin, 2)
        assert record.locked_until is not None
        record.locked_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        session.add(record)
        session.commit()
    assert _dial(phone, "t9", "2*5678").startswith("CON Drafts waiting:")


def test_a_resent_wrong_pin_request_counts_once(phone):
    # The gateway resends the same callback after a timeout: one wrong attempt, not three.
    for _ in range(3):
        assert _dial(phone, "r1", "2*0000") == "END Wrong officer id or PIN."
    assert _dial(phone, "r2", "2*5678").startswith("CON Drafts waiting:")


def test_a_correct_pin_clears_earlier_wrong_attempts(phone):
    _dial(phone, "c1", "2*0000")
    _dial(phone, "c2", "2*0000")
    assert _dial(phone, "c3", "2*5678").startswith("CON Drafts waiting:")
    _dial(phone, "c4", "2*0000")
    _dial(phone, "c5", "2*0000")
    assert _dial(phone, "c6", "2*5678").startswith("CON Drafts waiting:")


# --- approve and reject --------------------------------------------------------


def test_ussd_approval_with_correct_pin(phone):
    advice_id = _pending_id()
    draft = _dial(phone, "a1", "2*5678*1")
    assert draft.startswith("CON Group 2") and draft.endswith("1. Approve\n2. Reject\n0. Back")
    reply = _dial(phone, "a1", "2*5678*1*1")
    assert reply.startswith("END Approved by Lomuria Peter.")

    assert _status(advice_id) == "approved"
    decisions = _approvals(advice_id)
    assert [(d.officer_id, d.decision) for d in decisions] == [(2, "approved")]
    # The farmer in group 2 can now read it; final text is the draft, unedited.
    advice = phone.get("/api/farmer/advice", params={"phone": "+256700000004"}).json()
    assert advice["has_advice"] is True and "Heavy rain fell at H-004" in advice["text"]


def test_a_pin_never_reaches_the_tool_log_or_the_ussd_session(phone):
    """Sign-in, a wrong PIN, a resent wrong PIN, an approval: no PIN is saved anywhere."""
    from backend.core.database import session_scope
    from backend.db.models import OfficerPin, ToolLog, UssdSession
    from backend.officer.pins import hash_pin

    right, wrong = "8642", "9753"  # distinctive, so a match can only be a leak
    with session_scope() as session:
        record = session.get(OfficerPin, 2)
        record.pin_hash = hash_pin(right)
        session.add(record)
        session.commit()

    advice_id = _pending_id()
    assert _dial(phone, "k1", f"2*{wrong}") == "END Wrong officer id or PIN."
    assert _dial(phone, "k1", f"2*{wrong}") == "END Wrong officer id or PIN."  # resent
    assert _dial(phone, "k2", f"2*{right}").startswith("CON Drafts waiting:")
    assert _dial(phone, "k2", f"2*{right}*1*1").startswith("END Approved")
    assert _status(advice_id) == "approved"

    with session_scope() as session:
        logs = session.exec(select(ToolLog)).all()
        sessions = session.exec(select(UssdSession)).all()
    assert any(row.tool == "approve_advice" for row in logs)
    saved = [str(v) for row in logs for v in row.model_dump().values()]
    saved += [str(v) for row in sessions for v in row.model_dump().values()]
    for pin in (right, wrong):
        assert not [value for value in saved if pin in value]


def test_ussd_decision_is_logged_with_channel_ussd_and_the_signed_in_officer(phone):
    advice_id = _pending_id()
    _dial(phone, "g1", "2*5678*1*1")
    logs = _decision_logs(phone, "approve_advice")
    assert len(logs) == 1
    row = logs[0]
    assert row["actor"] == "2" and row["source"] == "officer" and row["error"] is None
    assert '"channel": "ussd"' in row["inputs"]
    assert f'"advice_id": {advice_id}' in row["inputs"] and '"officer_id": 2' in row["inputs"]
    assert "5678" not in row["inputs"] and "5678" not in row["outputs"]


def test_web_decision_is_logged_with_channel_web(client):
    advice_id = _pending_id()
    assert client.post(f"/api/approvals/{advice_id}/approve").status_code == 200
    assert '"channel": "web"' in _decision_logs(client, "approve_advice")[0]["inputs"]


def test_ussd_reject_records_the_reason_from_the_fixed_list(phone):
    advice_id = _pending_id()
    reasons = _dial(phone, "j1", "1*1234*1*2")
    assert reasons.startswith("CON Why reject?\n1. Facts look wrong")
    # A key that is not a listed reason shows the same screen again and saves nothing.
    assert _dial(phone, "j1", "1*1234*1*2*9").startswith("CON That choice is not on the list.\nWhy reject?")
    assert _status(advice_id) == "pending_review"
    reply = _dial(phone, "j1", "1*1234*1*2*9*4")
    assert reply == "END Rejected by Among Betty: Out of date. Nothing is sent to farmers."

    assert _status(advice_id) == "rejected"
    assert [(d.officer_id, d.decision, d.reason) for d in _approvals(advice_id)] == [(1, "rejected", "Out of date")]
    row = _decision_logs(phone, "reject_advice")[0]
    assert row["actor"] == "1" and '"channel": "ussd"' in row["inputs"] and "Out of date" in row["inputs"]
    assert phone.get("/api/farmer/advice", params={"phone": "+256700000004"}).json()["has_advice"] is False


def test_back_returns_to_the_list_without_deciding(phone):
    advice_id = _pending_id()
    assert _dial(phone, "b1", "2*5678*1*0").startswith("CON Drafts waiting:")
    assert _dial(phone, "b1", "2*5678*1*2*0").endswith("1. Approve\n2. Reject\n0. Back")
    assert _dial(phone, "b1", "2*5678*7").startswith("CON That choice is not on the list.\nDrafts waiting:")
    assert _status(advice_id) == "pending_review"


def test_no_drafts_waiting(phone):
    advice_id = _pending_id()
    _dial(phone, "n1", "2*5678*1*1")
    assert _dial(phone, "n2", "2*5678") == "END No drafts are waiting for approval."
    assert _status(advice_id) == "approved"


def test_a_new_draft_mid_session_does_not_change_which_draft_1_means(phone):
    seeded = _pending_id()
    assert "Heavy rain fell" in _dial(phone, "p1", "2*5678")
    newer = _new_draft(1, "A newer draft that arrived while the officer was reading.")
    assert _dial(phone, "p1", "2*5678*1*1").startswith("END Approved")
    assert _status(seeded) == "approved"
    assert _status(newer) == "pending_review"


# --- one gate, decide once -------------------------------------------------------


def test_double_approval_over_ussd_does_nothing_the_second_time(phone):
    advice_id = _pending_id()
    assert _dial(phone, "d1", "2*5678*1*1").startswith("END Approved")
    # The gateway resends the same request, then the officer tries again on a new session
    # that still lists the draft, and the other officer tries to reject it.
    assert _dial(phone, "d1", "2*5678*1*1") == "END Already approved: nothing changed."
    assert _dial(phone, "d1", "2*5678*1*2*1") == "END Already approved: nothing changed."

    assert _status(advice_id) == "approved"
    assert len(_approvals(advice_id)) == 1
    assert len(_decision_logs(phone, "approve_advice")) == 1
    assert _decision_logs(phone, "reject_advice") == []


def test_draft_decided_on_the_web_cannot_be_decided_again_by_phone(client):
    advice_id = _pending_id()
    assert "Heavy rain fell" in _dial(client, "x1", "2*5678")  # the phone has the list open
    r = client.post(f"/api/approvals/{advice_id}/reject", json={"reason": "Needs a recheck"})
    assert r.status_code == 200
    assert _dial(client, "x1", "2*5678*1") == "CON Already rejected: nothing changed.\n0. Back"
    assert _dial(client, "x1", "2*5678*1*1") == "END Already rejected: nothing changed."
    assert _status(advice_id) == "rejected"
    assert [(d.officer_id, d.decision) for d in _approvals(advice_id)] == [(1, "rejected")]


def test_draft_decided_by_phone_cannot_be_decided_again_on_the_web(client):
    advice_id = _pending_id()
    assert _dial(client, "y1", "2*5678*1*1").startswith("END Approved")
    r = client.post(f"/api/approvals/{advice_id}/reject", json={"reason": "Too late"})
    assert r.status_code == 409 and "'approved'" in r.json()["detail"]
    assert len(_approvals(advice_id)) == 1


def test_the_claim_itself_refuses_a_second_decision(client):
    """Two requests that both passed the first check: only one may act. Simulated by
    deciding with a draft object that was loaded before the other decision landed."""
    from backend.approvals.router import GateRefusal, apply_decision, load_decidable
    from backend.core.database import session_scope
    from backend.db.models import Officer

    advice_id = _pending_id()
    with session_scope() as first, session_scope() as second:
        advice_a = load_decidable(first, advice_id)
        advice_b = load_decidable(second, advice_id)
        apply_decision(first, advice_a, first.get(Officer, 1), "approve", channel="web")
        with pytest.raises(GateRefusal) as refused:
            apply_decision(second, advice_b, second.get(Officer, 2), "reject", channel="ussd", reason="Out of date")
    assert refused.value.advice_status == "approved"
    assert _status(advice_id) == "approved"
    assert [(d.officer_id, d.decision) for d in _approvals(advice_id)] == [(1, "approved")]


def test_ussd_uses_the_same_gate_as_the_web(client, monkeypatch):
    """If the gate refuses, USSD cannot approve: there is no second code path."""
    from backend.approvals import router as gate
    from backend.officer import ussd

    def closed_gate(session, advice_id):
        raise gate.GateRefusal(409, "closed for the test", "blocked")

    advice_id = _pending_id()
    _dial(client, "s1", "2*5678")
    monkeypatch.setattr(ussd, "load_decidable", closed_gate)
    assert _dial(client, "s1", "2*5678*1*1") == "END Already blocked: nothing changed."
    assert _status(advice_id) == "pending_review"


# --- expiry ----------------------------------------------------------------------


def test_expired_draft_is_refused_on_the_web(client):
    advice_id = _pending_id()
    _make_old(advice_id, hours=73)
    for path, body in (("approve", {}), ("edit", {"final_text": "Edited"}), ("reject", {"reason": "Old"})):
        r = client.post(f"/api/approvals/{advice_id}/{path}", json=body)
        assert r.status_code == 409 and r.json()["detail"] == EXPIRED
    assert _status(advice_id) == "expired"
    assert _approvals(advice_id) == []
    assert client.get("/api/approvals", params={"status": "pending_review"}).json() == []
    assert [a["id"] for a in client.get("/api/approvals", params={"status": "expired"}).json()] == [advice_id]
    assert client.get("/api/farmer/advice", params={"phone": "+256700000004"}).json()["has_advice"] is False
    # Expiring is logged once, however many times it is refused afterwards.
    logs = _decision_logs(client, "expire_advice")
    assert len(logs) == 1 and logs[0]["actor"] == "system"


def test_expired_draft_is_refused_over_ussd(phone):
    advice_id = _pending_id()
    assert "Heavy rain fell" in _dial(phone, "e1", "2*5678")  # listed while still fresh
    _make_old(advice_id, hours=73)
    assert _dial(phone, "e1", "2*5678*1") == f"CON {EXPIRED}\n0. Back"
    assert _dial(phone, "e1", "2*5678*1*1") == f"END {EXPIRED}"
    assert _dial(phone, "e1", "2*5678*1*2*1") == f"END {EXPIRED}"
    assert _status(advice_id) == "expired"
    assert _approvals(advice_id) == []
    # A new session no longer lists it.
    assert _dial(phone, "e2", "2*5678") == "END No drafts are waiting for approval."


def test_draft_just_inside_the_expiry_time_can_still_be_approved(client):
    advice_id = _pending_id()
    _make_old(advice_id, hours=71)
    assert client.post(f"/api/approvals/{advice_id}/approve").status_code == 200
    assert _status(advice_id) == "approved"


def test_expiry_hours_come_from_config(client, offline_settings):
    advice_id = _pending_id()  # seeded one day old
    offline_settings.draft_expiry_hours = 12
    r = client.post(f"/api/approvals/{advice_id}/approve")
    assert r.status_code == 409 and r.json()["detail"] == EXPIRED


def test_the_claim_itself_refuses_a_draft_that_expired_after_it_was_loaded(client):
    from backend.approvals.router import GateRefusal, apply_decision, load_decidable
    from backend.core.database import session_scope
    from backend.db.models import Officer

    advice_id = _pending_id()
    with session_scope() as session:
        advice = load_decidable(session, advice_id)
        _make_old(advice_id, hours=73)
        with pytest.raises(GateRefusal) as refused:
            apply_decision(session, advice, session.get(Officer, 1), "approve", channel="web")
    assert refused.value.detail == EXPIRED
    assert _status(advice_id) != "approved" and _approvals(advice_id) == []


def test_already_decided_advice_never_expires(client):
    advice_id = _pending_id()
    assert client.post(f"/api/approvals/{advice_id}/approve").status_code == 200
    _make_old(advice_id, hours=500)
    client.get("/api/approvals")
    assert _status(advice_id) == "approved"


# --- routing ---------------------------------------------------------------------


def test_africastalking_callback_routes_the_officer_code_to_the_officer_menu(phone):
    form = {"sessionId": "AT_o1", "phoneNumber": "+256770000001", "serviceCode": OFFICER_CODE, "text": ""}
    assert phone.post("/ussd/africastalking", data=form).text == "CON AgriMoist officers\nEnter your officer id:"
    # The farmer code still gets the farmer menu, and an unknown code is still refused.
    farmer = {**form, "sessionId": "AT_f1", "phoneNumber": "+256700000001", "serviceCode": FARMER_CODE}
    assert phone.post("/ussd/africastalking", data=farmer).text.startswith("CON AgriMoist\n1. My advice")
    other = {**form, "sessionId": "AT_x1", "serviceCode": "*384*0000#"}
    assert phone.post("/ussd/africastalking", data=other).text == "END This service is not available on this code."


def test_officer_ids_and_pins_do_nothing_on_the_farmer_code(phone):
    advice_id = _pending_id()
    r = phone.post("/ussd", json={"sessionId": "f9", "phoneNumber": "+256700000004", "text": "2*5678*1*1"})
    assert not r.text.startswith("END Approved")
    assert _status(advice_id) == "pending_review"


def test_simulator_page_offers_the_officer_code(phone):
    page = phone.get("/ussd").text
    assert OFFICER_CODE in page and "officer-dial-btn" in page
