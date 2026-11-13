# The rain guard in draft_advice (backend/tools/agrimoist_tools.py): when the forecast
# for the next 7 days is at or above HEAVY_RAIN_THRESHOLD_MM, a draft that tells farmers
# to add water is blocked before review - in code, like the unsourced-number check, so it
# does not depend on the model following its instructions.
from __future__ import annotations

import json

import pytest
from sqlmodel import select

from backend.db.models import Advice, Run
from backend.tools import agrimoist_tools as impl


def _sources(past_mm: float, next_mm: float) -> list[dict]:
    return [
        {"fact": impl.weather_fact(past_mm, next_mm), "source_type": "weather", "source_ref": "open-meteo:test"},
        {"fact": "Officer visit noted dry soil at H-009", "source_type": "observation", "source_ref": "observation:1"},
    ]


def _draft(session, text: str, past_mm: float = 5.0, next_mm: float = 45.0, why: str = "Based on the rain forecast.") -> dict:
    return impl.draft_advice(
        session, group_id=3, run_id=None, draft_text=text, why_text=why, sources=_sources(past_mm, next_mm)
    )


WATERING_ADVICE = [
    "Water the dry plot now.",
    "Keep the dry plot watered until the rain arrives.",
    "You may need to add water now, but the rain should help soon.",
    "Irrigate the maize if you can.",
    "Give extra water to the dry plot while the rain comes.",
    "Keep an eye on the very dry plot and water it only if the soil stays hard after the rains.",
    "Fields should be watered if they look wilted.",
    "The dry plot needs water this week.",
    "Keep your fields moist this week.",
]

NO_WATERING_ADVICE = [
    "Heavy rain is forecast, so hold off on extra watering and let the rain soak the soil.",
    "Do not water this week; the coming rain is enough.",
    "There is no need to irrigate, even for the dry plot.",
    "Heavy rain is forecast; no extra watering is needed this week.",
    "Rain is coming, so you do not need to add water to the dry plot.",
    # Real drafts from the 2026-11-13 eval run (task 2) that a first version of the
    # guard wrongly blocked: they all say NOT to water.
    "Heavy rain is forecast at 45.0 mm and a heavy-rain warning says not to add water even if soil looks dry.",
    "A heavy rain forecast is coming, and a heavy-rain warning says not to add water even on dry plots.",
    "Monitor the dry plot but do not irrigate.",
    "Check the crop for early signs of pests.",
]


# --- blocks watering advice before heavy rain ----------------------------------------


@pytest.mark.parametrize("text", WATERING_ADVICE)
def test_watering_advice_is_blocked_when_heavy_rain_is_forecast(seeded_session, text):
    result = _draft(seeded_session, text)
    assert result["status"] == "blocked"
    reason = result["blocked_reason"]
    assert reason.startswith("heavy rain is forecast (45 mm in the next 7 days, threshold 15 mm)")
    assert "tells farmers to add water" in reason
    advice = seeded_session.get(Advice, result["advice_id"])
    assert advice.status == "blocked"


def test_blocked_reason_quotes_the_watering_words(seeded_session):
    result = _draft(seeded_session, "Keep the dry plot watered until the rain arrives.")
    assert '"Keep the dry plot watered"' in result["blocked_reason"]


def test_a_blocked_draft_never_reaches_the_queue_and_cannot_be_approved(client):
    from backend.core.database import session_scope

    with session_scope() as session:
        advice_id = _draft(session, "Water the dry plot now.")["advice_id"]
    pending = client.get("/api/approvals", params={"status": "pending_review"}).json()
    assert advice_id not in [a["id"] for a in pending]
    assert client.post(f"/api/approvals/{advice_id}/approve", json={}).status_code == 409


@pytest.mark.parametrize("text", NO_WATERING_ADVICE)
def test_advice_not_to_water_passes_before_heavy_rain(seeded_session, text):
    result = _draft(seeded_session, text)
    assert result["status"] == "pending_review", result["blocked_reason"]
    assert result["blocked_reason"] is None


# --- allows watering advice when rain is light ----------------------------------------


@pytest.mark.parametrize("text", WATERING_ADVICE)
def test_watering_advice_is_allowed_when_rain_is_light(seeded_session, text):
    result = _draft(seeded_session, text, past_mm=1.0, next_mm=2.0)
    assert result["status"] == "pending_review", result["blocked_reason"]


def test_threshold_is_inclusive(seeded_session):
    assert _draft(seeded_session, "Water the dry plot now.", next_mm=15.0)["status"] == "blocked"
    assert _draft(seeded_session, "Water the dry plot now.", next_mm=14.9)["status"] == "pending_review"


def test_threshold_comes_from_config(seeded_session, monkeypatch):
    from backend.core.config import settings

    monkeypatch.setattr(settings, "heavy_rain_threshold_mm", 50.0)
    assert _draft(seeded_session, "Water the dry plot now.", next_mm=45.0)["status"] == "pending_review"


def test_unknown_weather_does_not_trigger_the_guard(seeded_session):
    sources = [
        {"fact": "Weather data unavailable after one retry", "source_type": "weather", "source_ref": "unavailable: down"},
        {"fact": "Officer visit noted dry soil at H-009", "source_type": "observation", "source_ref": "observation:1"},
    ]
    result = impl.draft_advice(
        seeded_session, group_id=3, run_id=None, draft_text="Weather is missing. Water the dry plot if it stays dry.",
        why_text="Weather missing.", sources=sources,
    )
    assert result["status"] == "pending_review"


def test_the_unsourced_number_check_still_applies_alongside(seeded_session):
    result = _draft(seeded_session, "Water the dry plot now; it is about 11% moisture.")
    assert result["status"] == "blocked"
    assert "numbers no source contains: 11" in result["blocked_reason"]
    assert "heavy rain is forecast" in result["blocked_reason"]


# --- end to end: the real agent over MCP ------------------------------------------------


async def _run(test_db, monkeypatch, past_mm: float, next_mm: float, advice: str | None) -> tuple[int, list[dict]]:
    """Run the real agent with a forced weather scenario (read by the MCP subprocess).
    `advice` is a fixed draft standing in for the model; None uses the real rule-based
    drafting. Returns the run id and the facts the drafting step was given."""
    from backend.agent import graph
    from backend.agent.model_client import _stub_draft
    from backend.db.seed import seed

    monkeypatch.setenv("WEATHER_OVERRIDE_PAST_MM", str(past_mm))
    monkeypatch.setenv("WEATHER_OVERRIDE_NEXT_MM", str(next_mm))
    seen: list[dict] = []

    def drafter(facts):
        seen.append(facts)
        if advice is None:
            return _stub_draft(facts)
        return {"advice": advice, "why": "Based on the rain forecast.", "model": "test", "cost": 0.0}

    monkeypatch.setattr(graph, "draft_advice_text", drafter)
    test_db.init_db()
    with test_db.session_scope() as session:
        seed(session)
        run = Run(group_id=3, model="test")
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id
    await graph.execute_agent_run(group_id=3, run_id=run_id)
    return run_id, seen


@pytest.mark.asyncio
async def test_agent_tells_the_model_and_blocks_watering_before_heavy_rain(test_db, monkeypatch):
    run_id, seen = await _run(test_db, monkeypatch, 5.0, 45.0, "Keep the dry plot watered until the rain arrives.")
    assert seen[0]["heavy_rain_warning"] == "Heavy rain is forecast: do not advise watering, even for dry plots."
    with test_db.session_scope() as session:
        assert session.get(Run, run_id).outcome == "blocked"
        advice = session.exec(select(Advice).where(Advice.run_id == run_id)).one()
        assert advice.status == "blocked"


@pytest.mark.asyncio
async def test_agent_drafts_watering_advice_when_rain_is_light(test_db, monkeypatch):
    run_id, seen = await _run(test_db, monkeypatch, 1.0, 2.0, "Keep the dry plot watered this week.")
    assert "heavy_rain_warning" not in seen[0]
    with test_db.session_scope() as session:
        assert session.get(Run, run_id).outcome == "drafted"


@pytest.mark.asyncio
async def test_rule_based_draft_passes_the_guard_before_heavy_rain(test_db, monkeypatch):
    run_id, _ = await _run(test_db, monkeypatch, 5.0, 45.0, None)
    with test_db.session_scope() as session:
        advice = session.exec(select(Advice).where(Advice.run_id == run_id)).one()
        assert advice.status == "pending_review"
        assert "no extra watering" in advice.draft_text


def test_run_page_replay_shows_the_blocked_reason(client, monkeypatch):
    """After the run, the run page replays from the log; the officer sees why it was blocked."""
    from backend.core.database import session_scope
    from backend.mcp_server.server import draft_advice as mcp_draft_advice

    with session_scope() as session:
        run = Run(group_id=3, model="test", outcome="blocked")
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id
    mcp_draft_advice(
        group_id=3, draft_text="Water the dry plot now.", why_text="Rain forecast.",
        sources=_sources(5.0, 45.0), run_id=run_id,
    )
    body = client.get(f"/api/runs/{run_id}/events").text
    finished = [line for line in body.splitlines() if line.startswith("data:") and "blocked_reason" in line][-1]
    data = json.loads(finished[len("data:"):].strip())
    assert data["outcome"] == "blocked"
    assert data["blocked_reason"].startswith("heavy rain is forecast (45 mm")
    page = client.get(f"/runs/{run_id}").text
    assert "Blocked before review" in page and "d.blocked_reason" in page
