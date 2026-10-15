# Officer sign-in is the signed server-side session only (backend/officer/auth.py).
# The approval gate and the paid drafting route refuse requests without it, plain
# role cookies or headers are not trusted, and the deciding officer is always the
# one in the session - never an officer_id sent in the request body.
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app

DECISIONS = [
    ("approve", {"officer_id": 1}),
    ("edit", {"officer_id": 1, "final_text": "Edited text."}),
    ("reject", {"officer_id": 1, "reason": "Needs a recheck"}),
]


def _pending_id(client) -> int:
    return client.get("/api/approvals", params={"status": "pending_review"}).json()[0]["id"]


def _status(client, advice_id: int) -> str:
    return next(a for a in client.get("/api/approvals").json() if a["id"] == advice_id)["status"]


@pytest.mark.parametrize("decision,body", DECISIONS)
def test_decisions_need_a_signed_in_officer(client, unauthenticated_client, decision, body):
    advice_id = _pending_id(client)
    r = unauthenticated_client.post(f"/api/approvals/{advice_id}/{decision}", json=body)
    assert r.status_code in (401, 403)
    assert _status(client, advice_id) == "pending_review"  # nothing changed


def test_draft_queue_and_drafting_need_a_signed_in_officer(unauthenticated_client):
    runs_before = len(unauthenticated_client.get("/api/runs").json())
    assert unauthenticated_client.get("/api/approvals").status_code in (401, 403)
    assert unauthenticated_client.post("/api/groups/1/draft").status_code in (401, 403)
    assert len(unauthenticated_client.get("/api/runs").json()) == runs_before  # no run was started


@pytest.mark.parametrize(
    "cookies,headers",
    [
        ({"officer_role": "officer"}, {}),
        ({"role": "officer"}, {}),
        ({}, {"x-officer-role": "officer"}),
        ({}, {"x-role": "officer"}),
    ],
)
def test_fake_role_cookie_or_header_is_rejected(client, test_db, cookies, headers):
    advice_id = _pending_id(client)
    with TestClient(app, cookies=cookies) as faker:
        for decision, body in DECISIONS:
            r = faker.post(f"/api/approvals/{advice_id}/{decision}", json=body, headers=headers)
            assert r.status_code in (401, 403), (decision, r.status_code)
        assert faker.post("/api/groups/1/draft", headers=headers).status_code in (401, 403)
        assert faker.get("/api/followups", headers=headers).status_code == 403
        assert faker.get("/api/auth/me", headers=headers).json()["authenticated"] is False
    assert _status(client, advice_id) == "pending_review"


def test_sign_in_refuses_other_roles_and_unknown_officers(unauthenticated_client):
    assert unauthenticated_client.post("/api/auth/login", json={"officer_id": 1, "role": "admin"}).status_code == 400
    assert unauthenticated_client.post("/api/auth/login", json={"officer_id": 9999}).status_code == 400
    assert unauthenticated_client.post("/officer/login", data={"officer_id": "9999"}).status_code == 400
    assert unauthenticated_client.get("/api/auth/me").json()["authenticated"] is False


def test_approval_is_logged_with_the_session_officer_not_the_body(unauthenticated_client):
    client = unauthenticated_client
    # Sign in through the officer sign-in form, as the web page does.
    r = client.post("/officer/login", data={"officer_id": "2", "next": "/approvals"}, follow_redirects=False)
    assert r.status_code == 303
    assert "officer_role" not in r.cookies  # no plain role cookie is set any more

    advice_id = _pending_id(client)
    r = client.post(f"/api/approvals/{advice_id}/approve", json={"officer_id": 1})  # body says officer 1
    assert r.status_code == 200
    assert r.json()["officer"] == "Lomuria Peter"  # officer 2, from the session

    row = next(x for x in client.get("/api/logs").json() if x["tool"] == "approve_advice")
    assert row["actor"] == "2"
    assert '"officer_id": 2' in row["inputs"]


def test_signed_in_officer_can_draft_and_decide(client):
    assert client.post("/api/groups/1/draft").status_code == 200
    pending = client.get("/api/approvals", params={"status": "pending_review"}).json()
    assert client.post(f"/api/approvals/{pending[0]['id']}/reject", json={"reason": "Check again"}).status_code == 200


def test_signed_out_visitor_is_sent_to_sign_in_from_approvals(unauthenticated_client):
    r = unauthenticated_client.get("/approvals", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/officer/login?next=/approvals"


def test_approvals_page_shows_the_signed_in_officer_and_no_officer_picker(unauthenticated_client):
    client = unauthenticated_client
    assert client.post("/api/auth/login", json={"officer_id": 2}).status_code == 200
    page = client.get("/approvals").text
    assert "Signed in as <strong>Lomuria Peter</strong>" in page
    assert "Acting as" not in page
    assert 'id="officer"' not in page
    assert "officerSelect" not in page
    # Approval decisions send no officer id; the server takes it from the session.
    assert 'decide(id, "approve", {});' in page
    assert 'decide(id, "edit", { final_text: text });' in page
    assert 'decide(id, "reject", { reason });' in page


def test_group_page_asks_signed_out_visitors_to_sign_in_before_drafting(client, unauthenticated_client):
    signed_out = unauthenticated_client.get("/groups/1")
    assert signed_out.status_code == 200
    assert 'id="draft-btn"' not in signed_out.text
    assert 'Please <a href="/officer/login?next=/groups/1">sign in</a> to draft advice.' in signed_out.text
    assert 'id="draft-btn"' in client.get("/groups/1").text


def test_home_still_opens_for_signed_out_visitors(unauthenticated_client):
    r = unauthenticated_client.get("/")
    assert r.status_code == 200
    assert 'class="table-wrap"' in r.text
