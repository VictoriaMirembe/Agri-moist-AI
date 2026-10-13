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
    for path in (
        "/",
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


def test_visit_trims_pest_and_notes_and_blank_becomes_null(client):
    r = client.post(
        "/api/visits",
        json={"household_code": "H-008", "officer_id": 1, "crop": "maize", "pest_seen": "   ", "notes": "  dry patch  "},
    )
    assert r.status_code == 200
    observation_ref = r.json()["source_ref"]
    group_id = r.json()["group_id"]

    detail = client.get(f"/api/groups/{group_id}").json()
    record = next(rec for rec in detail["field_records"]["records"] if rec["source_ref"] == observation_ref)
    assert record["pest_seen"] is None  # spaces only must not count as a pest sighting
    assert record["notes"] == "dry patch"

    from backend.core.database import session_scope
    from backend.tools.agrimoist_tools import get_pest_reports

    with session_scope() as session:
        pests = get_pest_reports(session, group_id=group_id)["reports"]
    assert observation_ref not in {p.get("source_ref") for p in pests}


def test_officer_tables_are_wrapped_for_narrow_screens(client):
    assert 'class="table-wrap"' in client.get("/").text
    assert 'class="table-wrap"' in client.get("/log").text
    css = client.get("/static/styles.css").text
    assert ".table-wrap" in css and "@media (max-width: 600px)" in css


# Officer screens that build HTML from API data must escape it. These raw
# interpolations were the injection points found in review; none may come back.
RAW_INTERPOLATIONS = {
    "approvals.html": ["${a.draft_text}", "${a.why_text", "${s.fact}", "${s.source_ref}"],
    "farmer_reports.html": ["${r.notes", '"Pest: " + r.pest_seen', "${r.household_code}"],
    "flags.html": ["${f.reason}", "${f.raised_by}"],
    "group_detail.html": ["${r.pest_seen", "${r.household_code}", 'href="${row.source_ref}"'],
}


def test_officer_templates_do_not_insert_raw_values_as_html():
    from pathlib import Path

    templates = Path(__file__).resolve().parent.parent / "frontend" / "officer" / "templates"
    for name, patterns in RAW_INTERPOLATIONS.items():
        source = (templates / name).read_text()
        for pattern in patterns:
            assert pattern not in source, f"{name} inserts {pattern} without escapeHTML"


def test_escape_html_helper_neutralises_markup():
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    app_js = Path(__file__).resolve().parent.parent / "frontend" / "static" / "app.js"
    script = app_js.read_text() + """
console.log(JSON.stringify([
  escapeHTML('<script>alert(1)</script>'),
  escapeHTML('</textarea><b>"x" & \\'y\\''),
  escapeHTML(null),
  escapeHTML(7),
  safeURL('javascript:alert(1)'),
  safeURL('https://fews.net/a?b=1&c=2'),
]));
"""
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout
    import json

    assert json.loads(out) == [
        "&lt;script&gt;alert(1)&lt;/script&gt;",
        "&lt;/textarea&gt;&lt;b&gt;&quot;x&quot; &amp; &#39;y&#39;",
        "",
        "7",
        "",
        "https://fews.net/a?b=1&amp;c=2",
    ]


def test_log_screen_shows_model_and_cost_per_run(client):
    from backend.core.database import session_scope
    from backend.db.models import Run

    with session_scope() as session:
        session.add(Run(group_id=1, model="openai/gpt-oss-120b", cost=0.000123, outcome="drafted"))
        session.commit()

    page = client.get("/log").text
    assert "openai/gpt-oss-120b" in page
    assert "0.000123" in page


def test_home_counts_match_the_queues_they_summarise(client):
    import re

    page = client.get("/").text

    def count(label):
        return int(re.search(r"<strong[^>]*>(\d+)</strong><div class=\"muted\">" + label, page).group(1))

    drafts = client.get("/api/approvals", params={"status": "pending_review"}).json()
    flags = client.get("/api/flags", params={"status": "open"}).json()
    reports = client.get("/api/farmer-reports", params={"checked": "unchecked"}).json()
    assert count("drafts waiting for approval") == len(drafts)
    assert count("open visit flags") == len(flags)
    assert count("unchecked farmer reports") == len(reports)


def test_every_officer_file_starts_with_a_comment():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for path in (root / "frontend" / "officer" / "templates").glob("*.html"):
        assert path.read_text().startswith("{#"), f"{path.name} has no header comment"
    for folder in ("backend/officer", "backend/approvals"):
        for path in (root / folder).glob("*.py"):
            assert path.read_text().startswith("#"), f"{path} has no header comment"


def test_officer_screens_auto_refresh_from_existing_json_routes(client):
    # Each screen that shows farmer activity re-fetches its data on a timer.
    for path in ("/", "/approvals", "/farmer-reports", "/flags", "/followups"):
        assert "autoRefresh(" in client.get(path).text, f"{path} does not auto-refresh"
    home = client.get("/").text
    for element_id in ("home-drafts", "home-flags", "home-reports"):
        assert f'id="{element_id}"' in home
    assert 'data-group-id="1"' in home
    approvals = client.get("/approvals").text
    assert "replaceCards(listEl" in approvals  # cards being edited are kept


def test_auto_refresh_pauses_while_hidden_and_refreshes_when_visible():
    import json
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    app_js = Path(__file__).resolve().parent.parent / "frontend" / "static" / "app.js"
    script = """
const listeners = {};
const timers = new Map();
let nextId = 1;
global.document = {
  visibilityState: "visible",
  addEventListener: (name, fn) => { listeners[name] = fn; },
};
global.setInterval = (fn, ms) => { const id = nextId++; timers.set(id, { fn, ms }); return id; };
global.clearInterval = (id) => { timers.delete(id); };
""" + app_js.read_text() + """
(async () => {
  const log = [];
  let calls = 0;
  autoRefresh(async () => { calls += 1; }, 30000);
  const flush = () => new Promise((r) => setImmediate(r));

  log.push(["started", timers.size, [...timers.values()][0].ms]);
  for (const t of timers.values()) await t.fn();
  await flush();
  log.push(["after one tick", calls]);

  document.visibilityState = "hidden";
  listeners.visibilitychange();
  log.push(["hidden: timers", timers.size]);

  document.visibilityState = "visible";
  listeners.visibilitychange();
  await flush();
  log.push(["visible again", calls, timers.size]);
  console.log(JSON.stringify(log));
})();
"""
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [
        ["started", 1, 30000],       # one 30-second timer while visible
        ["after one tick", 1],
        ["hidden: timers", 0],       # polling stops while the tab is hidden
        ["visible again", 2, 1],     # refreshes at once and restarts the timer
    ]


def test_mark_visited_records_and_logs_the_officer(client):
    flag = client.get("/api/flags", params={"status": "open"}).json()[0]
    r = client.post(f"/api/flags/{flag['id']}/visited", json={"officer_id": 2})
    assert r.status_code == 200
    assert r.json() == {"id": flag["id"], "status": "visited", "visited_by": "Lomuria Peter"}

    listed = next(f for f in client.get("/api/flags").json() if f["id"] == flag["id"])
    assert listed["visited_by"] == "Lomuria Peter" and listed["visited_at"]
    log = next(row for row in client.get("/api/logs").json() if row["tool"] == "mark_flag_visited")
    assert log["actor"] == "2" and log["source"] == "officer"


def test_mark_visited_still_accepts_the_original_empty_body(client):
    flag = client.get("/api/flags", params={"status": "open"}).json()[0]
    assert client.post(f"/api/flags/{flag['id']}/visited", json={}).status_code == 200
    listed = next(f for f in client.get("/api/flags").json() if f["id"] == flag["id"])
    assert listed["status"] == "visited" and listed["visited_by"] is None
    log = next(row for row in client.get("/api/logs").json() if row["tool"] == "mark_flag_visited")
    assert log["actor"] == "officer (not named)"


def test_mark_visited_rejects_an_unknown_officer(client):
    flag = client.get("/api/flags", params={"status": "open"}).json()[0]
    assert client.post(f"/api/flags/{flag['id']}/visited", json={"officer_id": 999}).status_code == 400
    assert next(f for f in client.get("/api/flags").json() if f["id"] == flag["id"])["status"] == "open"


def test_report_and_flag_actions_use_whilebusy_and_card_errors_not_alerts(client):
    from pathlib import Path

    templates = Path(__file__).resolve().parent.parent / "frontend" / "officer" / "templates"
    for name in ("approvals.html", "farmer_reports.html", "flags.html"):
        source = (templates / name).read_text()
        assert "alert(" not in source, name
    approvals = (templates / "approvals.html").read_text()
    assert "cardAction(card, () => postJSON(`/api/farmer-reports/${id}/check`" in approvals
    assert "cardAction(card, () => postJSON(`/api/flags/${id}/visited`, { officer_id:" in approvals
    app_js = (templates.parent.parent / "static" / "app.js").read_text()
    assert "await whileBusy(buttons, request);" in app_js
    assert 'id="officer"' in client.get("/flags").text  # the Visit flags page now has "Acting as"
