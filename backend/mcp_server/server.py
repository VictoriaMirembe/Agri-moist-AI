# AgriMoist AI's own MCP server (built by us, not borrowed). See docs/TOOL_CONTRACT.md.
#
# Eleven tools, one job each: six read, five change data (record_observation, draft_advice,
# flag_for_visit, schedule_followup, propose_irrigation). get_soil_moisture reads
# Open-Meteo's modelled soil moisture for the village (no field probes yet). None of them can send
# anything to a farmer, place an order or open a valve: propose_irrigation only puts a
# proposal in the officer's queue, and the valve tools live on a separate server
# (backend/irrigation_server) the agent is never connected to - see README "Safety rules". Every call is logged by backend.logging.tool_log,
# through the same database file the web app uses.
#
# Run standalone (for MCP Inspector, Claude Desktop or another MCP client), from the
# repository root:
#   python -m backend.mcp_server.server
# It uses DATABASE_URL like the web app. --demo-data loads the made-up test households,
# and only on a local SQLite file, never on the real database. See .mcp.json.
from __future__ import annotations

import functools
import os
import sys
from datetime import datetime

import anyio
from mcp.server.fastmcp import FastMCP

from backend.core.database import init_db, session_scope
from backend.logging.tool_log import run_logged
from backend.tools import agrimoist_tools as impl

mcp = FastMCP(
    "agrimoist",
    instructions=(
        "Tools for AgriMoist AI, a field assistant for agricultural extension officers "
        "in Karamoja, Uganda. Read tools gather facts for advice; the five write tools "
        "(record_observation, draft_advice, flag_for_visit, schedule_followup, "
        "propose_irrigation) never send anything to a farmer or open a valve - a named "
        "officer must separately approve a draft or proposal before anything happens."
    ),
)


def _tool(fn):
    """Register `fn` as an MCP tool that runs in a worker thread. FastMCP calls a plain
    function directly on its event loop, so one slow (database or HTTP) tool would hold
    up every other request; registered as async, the agent's reads run side by side.
    The module-level name stays the plain function, for direct callers and tests."""

    @functools.wraps(fn)
    async def threaded(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    mcp.add_tool(threaded, name=fn.__name__, description=fn.__doc__)
    return fn


@_tool
def record_observation(
    household_code: str,
    source: str,
    crop: str | None = None,
    soil_wetness: str | None = None,
    rain_last_7_days: str | None = None,
    pest_seen: str | None = None,
    crop_condition: str | None = None,
    food_left: str | None = None,
    notes: str | None = None,
    recorded_by: int | None = None,
    run_id: int | None = None,
) -> dict:
    """Save a field visit (source=officer_visit) or a farmer report
    (source=farmer_ussd or farmer_web) against a household's code, e.g. 'H-001'.
    Farmer reports are always saved as unchecked until an officer confirms them."""
    with session_scope() as session:
        return run_logged(
            session,
            tool="record_observation",
            inputs={"household_code": household_code, "source": source},
            actor="assistant",
            source=source,
            run_id=run_id,
            func=lambda: impl.record_observation(
                session,
                household_code=household_code,
                source=source,
                crop=crop,
                soil_wetness=soil_wetness,
                rain_last_7_days=rain_last_7_days,
                pest_seen=pest_seen,
                crop_condition=crop_condition,
                food_left=food_left,
                notes=notes,
                recorded_by=recorded_by,
            ),
        )


@_tool
def get_field_records(group_id: int, days: int = 60, run_id: int | None = None) -> dict:
    """Read recent field visits and farmer reports for a household group, most
    recent first, including how many days since each household's last visit."""
    with session_scope() as session:
        return run_logged(
            session,
            tool="get_field_records",
            inputs={"group_id": group_id, "days": days},
            actor="assistant",
            source="database:observations",
            run_id=run_id,
            func=lambda: impl.get_field_records(session, group_id=group_id, days=days),
        )


@_tool
def get_plot_history(group_id: int, run_id: int | None = None) -> dict:
    """Read a household group's plots' past crops, planting dates and harvests."""
    with session_scope() as session:
        return run_logged(
            session,
            tool="get_plot_history",
            inputs={"group_id": group_id},
            actor="assistant",
            source="database:plot_history",
            run_id=run_id,
            func=lambda: impl.get_plot_history(session, group_id=group_id),
        )


@_tool
def get_crop_calendar(zone: str, crop: str, run_id: int | None = None) -> dict:
    """Give the planting and harvest window for a crop in an agroclimatic zone, and
    whether today falls inside this year's planting window."""
    with session_scope() as session:
        return run_logged(
            session,
            tool="get_crop_calendar",
            inputs={"zone": zone, "crop": crop},
            actor="assistant",
            source="database:crop_calendar",
            run_id=run_id,
            func=lambda: impl.get_crop_calendar(session, zone=zone, crop=crop),
        )


@_tool
def get_weather(latitude: float, longitude: float, run_id: int | None = None) -> dict:
    """Get rain for the last 7 days and the rain forecast for the next 7 days from
    Open-Meteo for a village's coordinates. No API key needed."""
    with session_scope() as session:
        return run_logged(
            session,
            tool="get_weather",
            inputs={"latitude": latitude, "longitude": longitude},
            actor="assistant",
            source="open-meteo",
            run_id=run_id,
            func=lambda: impl.get_weather(latitude=latitude, longitude=longitude),
        )


@_tool
def get_pest_reports(group_id: int, days: int = 21, run_id: int | None = None) -> dict:
    """List pest sightings from recent officer visits and farmer reports in and
    around a household group's village."""
    with session_scope() as session:
        return run_logged(
            session,
            tool="get_pest_reports",
            inputs={"group_id": group_id, "days": days},
            actor="assistant",
            source="database:observations",
            run_id=run_id,
            func=lambda: impl.get_pest_reports(session, group_id=group_id, days=days),
        )


@_tool
def get_soil_moisture(group_id: int, run_id: int | None = None) -> dict:
    """Read the latest soil-moisture reading (volumetric %) for each plot in a household
    group, with its time and source label. Readings come from Open-Meteo's soil model for
    the village (a weather-model estimate, not a probe in the field), or a stored sensor
    reading. A reading older than 24 hours is marked stale=true: it is not current."""
    with session_scope() as session:
        return run_logged(
            session,
            tool="get_soil_moisture",
            inputs={"group_id": group_id},
            actor="assistant",
            source="soil-moisture",
            run_id=run_id,
            func=lambda: impl.get_soil_moisture(session, group_id=group_id),
        )


@_tool
def draft_advice(
    group_id: int,
    draft_text: str,
    why_text: str,
    sources: list[dict],
    language: str = "English",
    run_id: int | None = None,
) -> dict:
    """Write draft advice for a household group into the officer's approval queue.
    `sources` must be a list of {fact, source_type, source_ref} objects, one per claim
    in the advice. A draft with any fact missing a source, a number no source contains,
    or advice to add water while heavy rain is forecast (the weather source's forecast
    at or above HEAVY_RAIN_THRESHOLD_MM) is saved as 'blocked' and cannot enter the
    approval queue - it is never shown to an officer as ready."""
    with session_scope() as session:
        return run_logged(
            session,
            tool="draft_advice",
            inputs={"group_id": group_id, "sources": sources},
            actor="assistant",
            source="assistant-draft",
            run_id=run_id,
            func=lambda: impl.draft_advice(
                session,
                group_id=group_id,
                run_id=run_id,
                draft_text=draft_text,
                why_text=why_text,
                sources=sources,
                language=language,
            ),
        )


@_tool
def flag_for_visit(group_id: int, reason: str, run_id: int | None = None) -> dict:
    """Flag a household group for an urgent officer visit (for example: no recent
    visit, or very little food left). Never sends anything to the household."""
    with session_scope() as session:
        return run_logged(
            session,
            tool="flag_for_visit",
            inputs={"group_id": group_id, "reason": reason},
            actor="assistant",
            source="assistant-flag",
            run_id=run_id,
            func=lambda: impl.flag_for_visit(session, group_id=group_id, reason=reason, raised_by="assistant"),
        )


@_tool
def schedule_followup(advice_id: int, due_on: str, run_id: int | None = None) -> dict:
    """Book a check-back date (YYYY-MM-DD) for a piece of advice, so the office can
    later record whether it was followed and whether it helped."""
    with session_scope() as session:
        due = datetime.strptime(due_on, "%Y-%m-%d").date() if isinstance(due_on, str) else due_on
        return run_logged(
            session,
            tool="schedule_followup",
            inputs={"advice_id": advice_id, "due_on": due_on},
            actor="assistant",
            source="assistant-followup",
            run_id=run_id,
            func=lambda: impl.schedule_followup(session, advice_id=advice_id, due_on=due),
        )


@_tool
def propose_irrigation(group_id: int, plot: str, minutes: int, run_id: int | None = None) -> dict:
    """Propose irrigating one plot (its household code, e.g. 'H-009') for `minutes`,
    capped at IRRIGATION_MAX_MINUTES. The valve is SIMULATED. The rule is checked here in
    code: the plot's latest soil reading must be current and below
    IRRIGATION_DRY_THRESHOLD_PCT, the weather known and forecast rain below
    HEAVY_RAIN_THRESHOLD_MM, and no proposal already open for the plot - otherwise
    nothing is saved and the reasons are returned. A proposal is saved as pending_review
    in the officer's approval queue and NEVER opens a valve by itself."""
    with session_scope() as session:
        return run_logged(
            session,
            tool="propose_irrigation",
            inputs={"group_id": group_id, "plot": plot, "minutes": minutes},
            actor="assistant",
            source="assistant-irrigation",
            run_id=run_id,
            func=lambda: impl.propose_irrigation(
                session, group_id=group_id, plot=plot, minutes=minutes, run_id=run_id
            ),
        )


if __name__ == "__main__":
    # The web app already created the tables before it starts this server for an agent
    # run, and says so (AGRIMOIST_DB_READY); skipping the checks saves several database
    # round trips per run. Started on its own (MCP Inspector, Claude Desktop), it does them.
    if os.environ.get("AGRIMOIST_DB_READY") != "1":
        init_db()  # creates tables only if missing; safe on every start
        from backend.db.seed import load_reference_data

        with session_scope() as session:
            load_reference_data(session)
    if "--demo-data" in sys.argv:
        from backend.db.seed import seed, seed_allowed

        if not seed_allowed():
            sys.exit("--demo-data loads MADE-UP farmers; it only runs on a local SQLite test database.")
        with session_scope() as session:
            seed(session)  # does nothing if the demo data is already there
    mcp.run()
