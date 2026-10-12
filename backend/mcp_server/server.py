# AgriMoist AI's own MCP server (built by us, not borrowed). See docs/TOOL_CONTRACT.md.
#
# Nine tools, one job each. Four change data (record_observation, draft_advice,
# flag_for_visit, schedule_followup); none of them can send anything to a farmer or
# place an order - see README "Safety rules". Every call is logged by backend.logging.tool_log,
# through the same database file the web app uses.
#
# Run standalone (for MCP Inspector or another MCP client):
#   python -m backend.mcp_server.server
from __future__ import annotations

from datetime import datetime

from mcp.server.fastmcp import FastMCP

from backend.core.database import session_scope
from backend.logging.tool_log import run_logged
from backend.tools import agrimoist_tools as impl

mcp = FastMCP(
    "agrimoist",
    instructions=(
        "Tools for AgriMoist AI, a field assistant for agricultural extension officers "
        "in Karamoja, Uganda. Read tools gather facts for advice; the four write tools "
        "(record_observation, draft_advice, flag_for_visit, schedule_followup) never "
        "send anything to a farmer - a named officer must separately approve a draft "
        "before it can ever reach one."
    ),
)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
    in the advice. A draft with any fact missing a source is saved as 'blocked' and
    cannot enter the approval queue - it is never shown to an officer as ready."""
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


@mcp.tool()
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


@mcp.tool()
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


if __name__ == "__main__":
    mcp.run()
