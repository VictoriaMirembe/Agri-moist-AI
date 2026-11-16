# The SIMULATED soil-moisture sensor: the SoilReading table and its demo seed, the
# get_soil_moisture MCP tool, how the agent uses (and refuses to use) readings, and the
# officer group page. There is no real sensor hardware.
#
# Seeded group 3 (see backend/db/seed.py DEMO_SOIL_READINGS): H-007 moist 28.4% (an
# older 22.0% reading too), H-008 stale 31.0% (30 hours old), H-009 dry 11.2%, H-010 none.
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from backend.db.models import Advice, AdviceSource, Run, SoilReading, ToolLog


def _readings_by_code(result: dict) -> dict[str, dict]:
    return {r["household_code"]: r for r in result["readings"]}


# --- data and seed -----------------------------------------------------------------


def test_demo_readings_cover_a_dry_a_moist_and_a_stale_plot(seeded_session):
    from backend.tools import agrimoist_tools as impl

    latest = []
    for group_id in (1, 2, 3):
        latest += impl.get_soil_moisture(seeded_session, group_id=group_id)["readings"]
    current = [r for r in latest if not r["stale"]]
    assert any(r["moisture_pct"] < 15 for r in current), "needs a dry plot"
    assert any(15 <= r["moisture_pct"] < 35 for r in current), "needs a moist plot"
    assert any(r["stale"] for r in latest), "needs a plot whose latest reading is over 24 hours old"

    rows = seeded_session.exec(select(SoilReading)).all()
    assert rows and all(r.demo and r.source == "simulated sensor" for r in rows)


def test_seeding_never_overwrites_existing_readings(seeded_session):
    from backend.db.models import Household, Plot
    from backend.db.seed import seed

    def plot_of(code: str) -> int:
        return seeded_session.exec(
            select(Plot.id).join(Household, Plot.household_id == Household.id).where(Household.code == code)
        ).one()

    # A real-looking reading on a plot the demo seed would otherwise fill, and a
    # changed value on a seeded plot.
    seeded_session.add(SoilReading(plot_id=plot_of("H-010"), moisture_pct=40.0, source="simulated sensor"))
    h009 = seeded_session.exec(select(SoilReading).where(SoilReading.plot_id == plot_of("H-009"))).one()
    h009.moisture_pct = 20.0
    seeded_session.add(h009)
    seeded_session.commit()
    before = len(seeded_session.exec(select(SoilReading)).all())

    seed(seeded_session)  # the start-up path for an already-seeded database
    seed(seeded_session)

    seeded_session.expire_all()
    assert len(seeded_session.exec(select(SoilReading)).all()) == before
    assert seeded_session.get(SoilReading, h009.id).moisture_pct == 20.0
    h010 = seeded_session.exec(select(SoilReading).where(SoilReading.plot_id == plot_of("H-010"))).all()
    assert [(r.moisture_pct, r.demo) for r in h010] == [(40.0, False)]


def test_a_database_seeded_before_the_sensor_gets_the_demo_readings(seeded_session):
    from backend.db.seed import DEMO_SOIL_READINGS, seed

    for row in seeded_session.exec(select(SoilReading)).all():
        seeded_session.delete(row)
    seeded_session.commit()
    seed(seeded_session)
    count = len(seeded_session.exec(select(SoilReading)).all())
    assert count == sum(len(v) for v in DEMO_SOIL_READINGS.values())


# --- the tool -----------------------------------------------------------------------


def test_tool_returns_the_latest_reading_per_plot_with_time_and_source(seeded_session):
    from backend.tools import agrimoist_tools as impl

    result = impl.get_soil_moisture(seeded_session, group_id=3)
    by_code = _readings_by_code(result)
    assert set(by_code) == {"H-007", "H-008", "H-009"}
    assert by_code["H-007"]["moisture_pct"] == 28.4  # the newer of its two readings
    assert by_code["H-009"]["moisture_pct"] == 11.2 and by_code["H-009"]["stale"] is False
    assert by_code["H-008"]["stale"] is True
    for r in by_code.values():
        assert r["source"] == "simulated sensor"
        assert r["source_ref"].startswith("soil_reading:")
        assert datetime.fromisoformat(r["read_at"]).tzinfo is not None
    assert result["plots_without_reading"] == ["H-010"]
    assert result["source"] == "simulated sensor" and result["stale_after_hours"] == 24


def test_mcp_tool_call_is_logged(seeded_session):
    from backend.mcp_server.server import get_soil_moisture

    result = get_soil_moisture(group_id=3, run_id=None)
    assert len(result["readings"]) == 3
    rows = seeded_session.exec(select(ToolLog).where(ToolLog.tool == "get_soil_moisture")).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.error is None and row.actor == "assistant" and row.source == "soil-moisture"
    assert '"group_id": 3' in row.inputs and "simulated sensor" in row.outputs


def test_a_failing_sensor_raises_and_the_failure_is_logged(seeded_session, monkeypatch):
    from backend.core.config import settings
    from backend.mcp_server.server import get_soil_moisture
    from backend.tools.errors import ToolError

    monkeypatch.setattr(settings, "soil_sensor_fail", True)
    with pytest.raises(ToolError, match="Soil sensor feed is unavailable"):
        get_soil_moisture(group_id=3, run_id=None)
    row = seeded_session.exec(select(ToolLog).where(ToolLog.tool == "get_soil_moisture")).one()
    assert "unavailable" in row.error


# --- how the agent uses readings ------------------------------------------------------


def _reading(code: str, pct: float, hours_old: float, stale_flag: bool, ref: int) -> dict:
    return {
        "household_code": code,
        "moisture_pct": pct,
        "read_at": (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat(),
        "stale": stale_flag,
        "source": "simulated sensor",
        "source_ref": f"soil_reading:{ref}",
    }


def test_the_agent_rechecks_age_and_does_not_trust_a_stale_flag_alone():
    from backend.agent.graph import split_soil_readings

    soil = {
        "readings": [
            _reading("H-007", 28.4, 3, False, 1),
            _reading("H-008", 31.0, 30, True, 2),
            _reading("H-009", 13.7, 25, False, 3),  # flag says fresh, but it is 25 hours old
        ]
    }
    current, stale = split_soil_readings(soil)
    assert [r["household_code"] for r in current] == ["H-007"]
    assert [r["household_code"] for r in stale] == ["H-008", "H-009"]


def test_stale_values_never_reach_the_model_and_current_ones_are_cited():
    from backend.agent.graph import _soil_facts_and_sources

    soil = {
        "readings": [_reading("H-009", 11.2, 2, False, 7), _reading("H-008", 31.0, 30, True, 8)],
        "plots_without_reading": ["H-010"],
        "source": "simulated sensor",
    }
    facts, sources = _soil_facts_and_sources({"soil_moisture": soil})
    assert facts["current_readings"] == [{"household_code": "H-009", "moisture_pct": 11.2}]
    assert facts["stale_readings_not_used"] == [{"household_code": "H-008", "note": "out of date; not a current reading"}]
    assert "simulated" in facts["source"]
    assert "31" not in str(facts) and "31" not in str(sources)  # the stale value is nowhere
    cited = {s["source_ref"]: s for s in sources}
    assert cited["soil_reading:7"]["source_type"] == "soil_sensor"
    assert "11.2%" in cited["soil_reading:7"]["fact"] and "simulated sensor" in cited["soil_reading:7"]["fact"]
    assert "out of date" in cited["soil_reading:8"]["fact"]


def test_a_missing_sensor_becomes_a_gap_not_a_guess():
    from backend.agent.graph import _soil_facts_and_sources
    from backend.agent.model_client import _stub_draft

    facts, sources = _soil_facts_and_sources(
        {"soil_moisture": None, "soil_moisture_error": "get_soil_moisture failed: sensor down"}
    )
    assert facts == {"gap": "soil sensor readings missing after retry: get_soil_moisture failed: sensor down"}
    assert sources == [
        {
            "fact": "Soil sensor readings unavailable after one retry",
            "source_type": "soil_sensor",
            "source_ref": "unavailable: get_soil_moisture failed: sensor down",
        }
    ]
    draft = _stub_draft({"weather": {"past_7_days_rain_mm": 3, "next_7_days_rain_mm": 2}, "soil_moisture": facts})
    assert "Soil sensor readings are missing after a retry" in draft["advice"]
    assert "%" not in draft["advice"]


# --- the unsourced-number check still applies ------------------------------------------


def test_a_moisture_number_that_matches_no_source_is_blocked(seeded_session):
    from backend.agent.graph import _soil_facts_and_sources
    from backend.tools import agrimoist_tools as impl

    soil = impl.get_soil_moisture(seeded_session, group_id=3)
    _facts, sources = _soil_facts_and_sources({"soil_moisture": soil})

    sourced = impl.draft_advice(
        seeded_session, group_id=3, run_id=None, draft_text="The sensor shows dry soil at H-009 (11.2%).",
        why_text="Simulated soil sensor reading.", sources=sources,
    )
    assert sourced["status"] == "pending_review"

    invented = impl.draft_advice(
        seeded_session, group_id=3, run_id=None, draft_text="The sensor shows dry soil at H-009 (9.5%).",
        why_text="Simulated soil sensor reading.", sources=sources,
    )
    assert invented["status"] == "blocked" and "9.5" in invented["blocked_reason"]

    # The stale H-008 value (31.0%) is not in any source, so using it as current is blocked too.
    stale_used = impl.draft_advice(
        seeded_session, group_id=3, run_id=None, draft_text="Soil at H-008 is moist (31.0%); no watering needed.",
        why_text="Simulated soil sensor reading.", sources=sources,
    )
    assert stale_used["status"] == "blocked" and "31.0" in stale_used["blocked_reason"]


# --- end to end: the real agent over MCP -------------------------------------------------


async def _run(test_db, group_id: int) -> int:
    from backend.agent.graph import execute_agent_run
    from backend.db.seed import seed

    test_db.init_db()
    with test_db.session_scope() as session:
        seed(session)
        run = Run(group_id=group_id, model="rule-based-stub")
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id
    await execute_agent_run(group_id=group_id, run_id=run_id)
    return run_id


@pytest.mark.asyncio
async def test_agent_gathers_soil_moisture_and_the_draft_cites_the_sensor(test_db):
    run_id = await _run(test_db, group_id=3)
    with test_db.session_scope() as session:
        calls = session.exec(select(ToolLog).where(ToolLog.run_id == run_id, ToolLog.tool == "get_soil_moisture")).all()
        assert len(calls) == 1 and calls[0].error is None

        advice = session.exec(select(Advice).where(Advice.run_id == run_id)).one()
        assert advice.status == "pending_review"
        soil_sources = [
            s for s in session.exec(select(AdviceSource).where(AdviceSource.advice_id == advice.id)).all()
            if s.source_type == "soil_sensor"
        ]
        facts = " | ".join(s.fact for s in soil_sources)
        assert "Soil moisture at H-009: 11.2% (simulated sensor, current reading)" in facts
        assert "Soil moisture at H-007: 28.4%" in facts
        assert "Soil sensor reading at H-008 is out of date and was not used" in facts
        assert all(s.source_ref.startswith("soil_reading:") for s in soil_sources)

        # The dry current reading is used; the stale 31.0% is not used as current.
        assert "11.2%" in advice.draft_text
        for stale_value in ("31.0", "31%"):
            assert stale_value not in advice.draft_text and stale_value not in facts
        assert "H-008 is out of date" in advice.draft_text


@pytest.mark.asyncio
async def test_failing_sensor_retries_once_then_marks_a_gap_and_still_drafts(test_db, monkeypatch):
    monkeypatch.setenv("SOIL_SENSOR_FAIL", "1")  # read by the MCP server subprocess
    run_id = await _run(test_db, group_id=3)
    with test_db.session_scope() as session:
        calls = session.exec(select(ToolLog).where(ToolLog.run_id == run_id, ToolLog.tool == "get_soil_moisture")).all()
        assert len(calls) == 2, "must retry exactly once before giving up"
        assert all(c.error for c in calls)

        advice = session.exec(select(Advice).where(Advice.run_id == run_id)).one()
        soil_sources = [
            s for s in session.exec(select(AdviceSource).where(AdviceSource.advice_id == advice.id)).all()
            if s.source_type == "soil_sensor"
        ]
        assert len(soil_sources) == 1 and soil_sources[0].source_ref.startswith("unavailable")
        assert "Soil sensor readings are missing" in advice.draft_text
        assert "%" not in advice.draft_text  # no guessed moisture figure


# --- officer group page -------------------------------------------------------------------


def test_group_page_shows_latest_readings_with_a_stale_label(client):
    data = client.get("/api/groups/3").json()
    by_code = _readings_by_code(data["soil_moisture"])
    assert by_code["H-009"]["moisture_pct"] == 11.2 and by_code["H-009"]["stale"] is False
    assert by_code["H-008"]["stale"] is True
    assert by_code["H-007"]["read_at"]
    assert data["soil_moisture"]["plots_without_reading"] == ["H-010"]

    page = client.get("/groups/3").text
    assert "<h2>Soil moisture</h2>" in page and "Stale" in page


def test_group_page_readings_need_an_officer_sign_in(unauthenticated_client):
    assert unauthenticated_client.get("/api/groups/3").status_code == 403
    r = unauthenticated_client.get("/groups/3", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/officer/login")


def test_group_page_says_when_the_sensor_is_unavailable(client, monkeypatch):
    from backend.core.config import settings

    monkeypatch.setattr(settings, "soil_sensor_fail", True)
    r = client.get("/api/groups/3")
    assert r.status_code == 200
    assert r.json()["soil_moisture"]["readings"] == [] and "unavailable" in r.json()["soil_moisture"]["error"]
