# End-to-end agent tests: the real LangGraph flow, calling tools over the real MCP
# protocol through two subprocesses (our server and the borrowed mcp-server-time).
# Slower than the other tests (each spawns two Python subprocesses and makes one real
# network call to Open-Meteo) but these are the tests that prove the whole pipeline -
# plan, gather, check urgent, draft or flag, retry, log - actually works together.
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
        assert run.model == "none (no model call)" and run.cost == 0.0  # not the configured model
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


async def test_open_food_visit_request_flags_the_group_instead_of_drafting(test_db):
    # Group 3 normally drafts (see the test above). A farmer's open visit request
    # about food must make the agent flag it instead, like food_left "none".
    test_db.init_db()
    with test_db.session_scope() as session:
        from backend.db.seed import seed

        seed(session)
        from backend.db.models import VisitFlag

        session.add(VisitFlag(group_id=3, reason="H-009 asked for a visit: little food left", raised_by="farmer_ussd"))
        session.commit()

    run_id = await _run(test_db, group_id=3)

    with test_db.session_scope() as session:
        run = session.get(Run, run_id)
        assert run.outcome == "flagged"
        assert session.exec(select(Advice).where(Advice.run_id == run_id)).first() is None
        events = [log.tool for log in session.exec(select(ToolLog).where(ToolLog.run_id == run_id)).all()]
        assert "flag_for_visit" in events and "draft_advice" not in events


async def test_check_urgent_step_reasons(seeded_session):
    from backend.agent.graph import check_urgent_node
    from backend.db.models import VisitFlag

    emitted = []

    async def emit(run_id, event):
        emitted.append(event)

    recent = {"records": [], "households_in_group": 3, "days_since_last_visit_by_household": {"H-007": 2}}
    base = {"run_id": 1, "_emit": emit, "field_records": recent}

    # A non-food visit request alone is not urgent.
    seeded_session.add(VisitFlag(group_id=3, reason="H-007 asked for a visit: pests", raised_by="farmer_web"))
    seeded_session.commit()
    assert (await check_urgent_node({**base, "group_id": 3}))["flag_reason"] is None

    seeded_session.add(VisitFlag(group_id=3, reason="H-008 asked for a visit: little food left", raised_by="farmer_web"))
    seeded_session.commit()
    reason = (await check_urgent_node({**base, "group_id": 3}))["flag_reason"]
    assert reason == "Open visit request mentions food: H-008 asked for a visit: little food left"
    assert emitted[-1] == {"type": "check_urgent", "flag_reason": reason}


async def test_borrowed_clock_call_is_logged_with_the_run(test_db):
    test_db.init_db()
    with test_db.session_scope() as session:
        from backend.db.seed import seed

        seed(session)

    run_id = await _run(test_db, group_id=3)

    with test_db.session_scope() as session:
        rows = session.exec(
            select(ToolLog).where(ToolLog.run_id == run_id, ToolLog.tool == "get_current_time")
        ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.source == "mcp-server-time"
        assert row.actor == "assistant"
        assert '"timezone": "Africa/Kampala"' in row.inputs
        assert row.error is None and "Africa/Kampala" in row.outputs


async def test_a_failed_clock_call_is_logged_and_still_raised(test_db):
    from backend.agent.graph import _logged_now
    from backend.tools.errors import ToolError

    test_db.init_db()

    class BrokenClock:
        async def now(self, timezone="Africa/Kampala"):
            raise ToolError("get_current_time failed: time server down")

    with test_db.session_scope() as session:
        run = Run(group_id=1, model="rule-based-stub")
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id

    with pytest.raises(ToolError, match="time server down"):
        await _logged_now(BrokenClock(), run_id)

    with test_db.session_scope() as session:
        row = session.exec(select(ToolLog).where(ToolLog.run_id == run_id)).one()
        assert row.tool == "get_current_time" and row.source == "mcp-server-time"
        assert "time server down" in row.error
