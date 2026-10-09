# End-to-end agent tests: the real LangGraph flow, calling tools over the real MCP
# protocol through two subprocesses (our server and the borrowed mcp-server-time).
# Slower than the other tests (each spawns two Python subprocesses and makes one real
# network call to Open-Meteo) but these are the tests that prove the whole pipeline -
# plan, gather, check sources, draft or flag, retry, log - actually works together.
from __future__ import annotations

import pytest
from sqlmodel import select

from backend.db.models import Advice, AdviceSource, Run, ToolLog

pytestmark = pytest.mark.asyncio


async def _run(test_db, group_id: int) -> int:
    from backend.agent.graph import execute_agent_run

    with test_db.session_scope() as session:
        run = Run(group_id=group_id, model="rule-based-stub")
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id

    await execute_agent_run(group_id=group_id, run_id=run_id)
    return run_id


async def test_group_with_stale_visit_and_low_food_is_flagged_not_drafted(test_db):
    # EVALS tasks 5 and 6: group 2 has H-005 with food_left=none, so it must be
    # flagged for an urgent visit, never drafted - the assistant must not invent
    # soil or weather advice for a household it knows is short on food.
    test_db.init_db()
    with test_db.session_scope() as session:
        from backend.db.seed import seed

        seed(session)

    run_id = await _run(test_db, group_id=2)

    with test_db.session_scope() as session:
        run = session.get(Run, run_id)
        assert run.outcome == "flagged"
        drafted = session.exec(select(Advice).where(Advice.run_id == run_id)).first()
        assert drafted is None  # never drafted advice for a flagged group


async def test_group_with_recent_visits_drafts_sourced_advice(test_db):
    # EVALS tasks 1, 2, 3, 11 (decision wording is unit-tested in
    # tests/test_model_client.py; this test proves the full pipeline wires the real
    # facts into a real sourced draft). Group 3 (Iriiri) has no stale-visit or
    # low-food households, so it should draft rather than flag.
    test_db.init_db()
    with test_db.session_scope() as session:
        from backend.db.seed import seed

        seed(session)

    run_id = await _run(test_db, group_id=3)

    with test_db.session_scope() as session:
        run = session.get(Run, run_id)
        assert run.outcome == "drafted"
        advice = session.exec(select(Advice).where(Advice.run_id == run_id)).first()
        assert advice is not None
        assert advice.status == "pending_review"
        assert advice.why_text

        sources = session.exec(select(AdviceSource).where(AdviceSource.advice_id == advice.id)).all()
        assert sources, "a drafted advice must always carry at least one source"
        source_types = {s.source_type for s in sources}
        assert "weather" in source_types
        assert "crop_calendar" in source_types  # both seeded crops' windows are closed in October

        logs = session.exec(select(ToolLog).where(ToolLog.run_id == run_id)).all()
        tool_names = {log.tool for log in logs}
        assert {"get_field_records", "get_plot_history", "get_pest_reports", "get_weather", "draft_advice"} <= tool_names
        assert all(log.error is None for log in logs), "a healthy run should have no failed tool calls"


async def test_weather_failure_switch_marks_a_gap_and_still_drafts(test_db, monkeypatch):
    # EVALS task 4: weather service down -> retries, then marks "weather missing"
    # instead of guessing. The draft still reaches the approval queue (an officer can
    # see and judge the gap), it is just honestly marked.
    monkeypatch.setenv("WEATHER_FAIL", "1")
    test_db.init_db()
    with test_db.session_scope() as session:
        from backend.db.seed import seed

        seed(session)

    run_id = await _run(test_db, group_id=3)

    with test_db.session_scope() as session:
        weather_calls = session.exec(
            select(ToolLog).where(ToolLog.run_id == run_id, ToolLog.tool == "get_weather")
        ).all()
        assert len(weather_calls) == 2, "must retry exactly once before giving up"
        assert all(c.error for c in weather_calls)

        advice = session.exec(select(Advice).where(Advice.run_id == run_id)).first()
        assert advice is not None
        weather_source = next(
            s
            for s in session.exec(select(AdviceSource).where(AdviceSource.advice_id == advice.id)).all()
            if s.source_type == "weather"
        )
        assert weather_source.source_ref.startswith("unavailable")
        assert "missing" in advice.draft_text.lower() or "missing" in weather_source.fact.lower()
