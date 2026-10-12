# Tests for the approval gate (backend/approvals/router.py) - the only code allowed
# to move advice to approved or edited_and_approved. Uses the TestClient so the real
# HTTP layer, validation and status rules are all exercised.
from __future__ import annotations


def test_seeded_pending_draft_exists_for_group_2(client):
    r = client.get("/api/approvals", params={"status": "pending_review"})
    assert r.status_code == 200
    ids = [a["id"] for a in r.json()]
    assert ids, "expected the seeded pending_review draft for group 2"


def test_approve_requires_a_real_officer(client):
    pending = client.get("/api/approvals", params={"status": "pending_review"}).json()
    advice_id = pending[0]["id"]
    r = client.post(f"/api/approvals/{advice_id}/approve", json={"officer_id": 9999})
    assert r.status_code == 403


def test_approve_then_cannot_approve_again(client):
    pending = client.get("/api/approvals", params={"status": "pending_review"}).json()
    advice_id = pending[0]["id"]
    r1 = client.post(f"/api/approvals/{advice_id}/approve", json={"officer_id": 1})
    assert r1.status_code == 200
    assert r1.json()["status"] == "approved"

    r2 = client.post(f"/api/approvals/{advice_id}/approve", json={"officer_id": 1})
    assert r2.status_code == 409  # gate: cannot re-decide an already-decided advice


def test_reject_requires_a_reason(client):
    pending = client.get("/api/approvals", params={"status": "pending_review"}).json()
    advice_id = pending[0]["id"]
    r = client.post(f"/api/approvals/{advice_id}/reject", json={"officer_id": 1, "reason": "   "})
    assert r.status_code == 422


def test_reject_is_logged_and_never_reaches_a_farmer(client):
    # EVALS task 7: officer rejects the draft - nothing is sent, rejection is logged.
    pending = client.get("/api/approvals", params={"status": "pending_review"}).json()
    advice_id = pending[0]["id"]

    r = client.post(f"/api/approvals/{advice_id}/reject", json={"officer_id": 1, "reason": "Needs a recheck"})
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"

    # H-004 is in group 2, the seeded group with the pending draft.
    advice_resp = client.get("/api/farmer/advice", params={"phone": "+256700000004"})
    assert advice_resp.status_code == 200
    assert advice_resp.json()["has_advice"] is False


def test_edit_and_approve_stores_the_officers_final_text(client):
    pending = client.get("/api/approvals", params={"status": "pending_review"}).json()
    advice_id = pending[0]["id"]
    r = client.post(
        f"/api/approvals/{advice_id}/edit",
        json={"officer_id": 1, "final_text": "Edited: water twice this week."},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "edited_and_approved"

    approvals = client.get("/api/approvals").json()
    edited = next(a for a in approvals if a["id"] == advice_id)
    assert edited["final_text"] == "Edited: water twice this week."


def test_blocked_advice_can_never_be_approved(client):
    # A blocked draft never enters pending_review, so approving it 404/409s.
    from backend.core.database import session_scope
    from backend.tools import agrimoist_tools as impl

    with session_scope() as session:
        result = impl.draft_advice(
            session, group_id=1, run_id=None, draft_text="x", why_text="y", sources=[]
        )
    assert result["status"] == "blocked"
    r = client.post(f"/api/approvals/{result['advice_id']}/approve", json={"officer_id": 1})
    assert r.status_code == 409
