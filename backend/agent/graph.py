# The AgriMoist AI agent: plan, gather, check urgent, draft or flag - with one retry
# per tool before a gap is marked (never guessed). Built with LangGraph. Tools are
# called over the real MCP protocol through backend.agent.mcp_client.ToolClient, which
# talks to our own MCP server and the borrowed mcp-server-time server as subprocesses.
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, TypedDict

from langgraph.graph import END, StateGraph
from sqlmodel import select

from backend.agent import events
from backend.agent.mcp_client import ToolClient
from backend.agent.model_client import draft_advice_text
from backend.core.database import session_scope
from backend.db.models import District, Household, Run, Village, VisitFlag
from backend.tools.errors import ToolError

EmitFn = Callable[[int, dict[str, Any]], Awaitable[None]]


class AgentState(TypedDict, total=False):
    group_id: int
    run_id: int
    zone: str
    village_name: str
    village_lat: float
    village_lon: float
    crops: list[str]
    field_records: dict | None
    field_records_error: str | None
    plot_history: dict | None
    plot_history_error: str | None
    pest_reports: dict | None
    pest_reports_error: str | None
    weather: dict | None
    weather_error: str | None
    now_info: dict | None
    now_error: str | None
    crop_calendars: dict[str, dict]
    crop_calendar_errors: dict[str, str]
    flag_reason: str | None
    flag_id: int | None
    outcome: str | None
    advice_id: int | None
    model_cost: float
    model_name: str
    _client: Any
    _emit: Any


async def _call_with_retry(
    client: ToolClient, run_id: int, emit_fn: EmitFn, tool_name: str, **kwargs: Any
) -> tuple[dict | None, str | None]:
    await emit_fn(run_id, {"type": "tool_call", "tool": tool_name, "inputs": kwargs, "attempt": 1})
    try:
        result = await client.call(tool_name, run_id=run_id, **kwargs)
        await emit_fn(run_id, {"type": "tool_result", "tool": tool_name, "outputs": result, "attempt": 1})
        return result, None
    except ToolError as exc:
        await emit_fn(run_id, {"type": "tool_error", "tool": tool_name, "error": str(exc), "attempt": 1})
        await emit_fn(run_id, {"type": "tool_call", "tool": tool_name, "inputs": kwargs, "attempt": 2})
        try:
            result = await client.call(tool_name, run_id=run_id, **kwargs)
            await emit_fn(run_id, {"type": "tool_result", "tool": tool_name, "outputs": result, "attempt": 2})
            return result, None
        except ToolError as exc2:
            await emit_fn(
                run_id,
                {"type": "tool_error", "tool": tool_name, "error": str(exc2), "attempt": 2, "final": True},
            )
            return None, str(exc2)


async def plan_node(state: AgentState) -> dict:
    group_id = state["group_id"]
    with session_scope() as session:
        household = session.exec(select(Household).where(Household.group_id == group_id)).first()
        if household is None:
            raise ToolError(f"No households found for group {group_id}")
        village = session.get(Village, household.village_id)
        district = session.get(District, village.district_id)
    await state["_emit"](
        state["run_id"],
        {"type": "plan", "zone": district.agroclimatic_zone, "village": village.name},
    )
    return {
        "zone": district.agroclimatic_zone,
        "village_name": village.name,
        "village_lat": village.latitude,
        "village_lon": village.longitude,
    }


async def gather_node(state: AgentState) -> dict:
    client: ToolClient = state["_client"]
    run_id = state["run_id"]
    emit_fn: EmitFn = state["_emit"]
    group_id = state["group_id"]

    field_records, fr_err = await _call_with_retry(client, run_id, emit_fn, "get_field_records", group_id=group_id)
    plot_history, ph_err = await _call_with_retry(client, run_id, emit_fn, "get_plot_history", group_id=group_id)
    pest_reports, pr_err = await _call_with_retry(client, run_id, emit_fn, "get_pest_reports", group_id=group_id)
    weather, w_err = await _call_with_retry(
        client, run_id, emit_fn, "get_weather", latitude=state["village_lat"], longitude=state["village_lon"]
    )

    await emit_fn(run_id, {"type": "tool_call", "tool": "get_current_time", "inputs": {"timezone": "Africa/Kampala"}})
    try:
        now_info = await client.now()
        await emit_fn(run_id, {"type": "tool_result", "tool": "get_current_time", "outputs": now_info})
        now_err = None
    except ToolError as exc:
        await emit_fn(run_id, {"type": "tool_error", "tool": "get_current_time", "error": str(exc)})
        now_info, now_err = None, str(exc)

    crops = sorted(
        {r["crop"] for r in (plot_history or {}).get("history", []) if r.get("crop")}
        | {r["crop"] for r in (field_records or {}).get("records", []) if r.get("crop")}
    )
    crop_calendars: dict[str, dict] = {}
    calendar_errors: dict[str, str] = {}
    for crop in crops:
        calendar, err = await _call_with_retry(
            client, run_id, emit_fn, "get_crop_calendar", zone=state["zone"], crop=crop
        )
        if calendar:
            crop_calendars[crop] = calendar
        elif err:
            calendar_errors[crop] = err

    return {
        "field_records": field_records,
        "field_records_error": fr_err,
        "plot_history": plot_history,
        "plot_history_error": ph_err,
        "pest_reports": pest_reports,
        "pest_reports_error": pr_err,
        "weather": weather,
        "weather_error": w_err,
        "now_info": now_info,
        "now_error": now_err,
        "crops": crops,
        "crop_calendars": crop_calendars,
        "crop_calendar_errors": calendar_errors,
    }


_FOOD_WORDS = re.compile(r"\bfood\b", re.IGNORECASE)


def _open_food_visit_requests(group_id: int) -> list[str]:
    """Reasons of this group's open visit flags that mention food - for example a
    farmer's "H-003 asked for a visit: little food left" sent by USSD or the web."""
    with session_scope() as session:
        flags = session.exec(
            select(VisitFlag).where(VisitFlag.group_id == group_id, VisitFlag.status == "open")
        ).all()
        return [f.reason for f in flags if _FOOD_WORDS.search(f.reason or "")]


async def check_urgent_node(state: AgentState) -> dict:
    """Decide whether this group needs an urgent visit instead of advice. Never
    invents a visit date or a food reading - only acts on what gather_node found and
    on open visit requests that are already recorded."""
    field_records = state.get("field_records") or {"records": [], "households_in_group": 0}
    days_since = field_records.get("days_since_last_visit_by_household", {}) or {}
    no_recent_visit = sorted(code for code, days in days_since.items() if days is None or days > 30)

    records = field_records.get("records", [])
    very_little_food = sorted({r["household_code"] for r in records if r.get("food_left") == "none"})

    food_requests = _open_food_visit_requests(state["group_id"])

    flag_reason = None
    if very_little_food:
        flag_reason = f"Very little food left at: {', '.join(very_little_food)}"
    elif food_requests:
        flag_reason = f"Open visit request mentions food: {'; '.join(food_requests)}"
    elif no_recent_visit and field_records.get("households_in_group", 0) > 0:
        flag_reason = f"No field visit in over 30 days for: {', '.join(no_recent_visit)}"

    await state["_emit"](state["run_id"], {"type": "check_urgent", "flag_reason": flag_reason})
    return {"flag_reason": flag_reason}


async def draft_or_flag_node(state: AgentState) -> dict:
    client: ToolClient = state["_client"]
    run_id = state["run_id"]
    emit_fn: EmitFn = state["_emit"]
    group_id = state["group_id"]

    if state.get("flag_reason"):
        result, _err = await _call_with_retry(
            client, run_id, emit_fn, "flag_for_visit", group_id=group_id, reason=state["flag_reason"]
        )
        await emit_fn(run_id, {"type": "run_finished", "outcome": "flagged"})
        return {"outcome": "flagged", "flag_id": (result or {}).get("flag_id")}

    facts: dict[str, Any] = {"group_id": group_id, "zone": state.get("zone"), "crops": state.get("crops")}
    sources: list[dict[str, str]] = []

    weather = state.get("weather")
    if weather:
        facts["weather"] = weather
        sources.append(
            {
                "fact": f"Rain last 7 days {weather['past_7_days_rain_mm']}mm, next 7 days forecast {weather['next_7_days_rain_mm']}mm",
                "source_type": "weather",
                "source_ref": weather["source_ref"],
            }
        )
    else:
        facts["weather"] = {"gap": f"weather missing after retry: {state.get('weather_error')}"}
        sources.append(
            {
                "fact": "Weather data unavailable after one retry",
                "source_type": "weather",
                "source_ref": f"unavailable: {state.get('weather_error')}",
            }
        )

    field_records = state.get("field_records") or {}
    facts["field_records"] = field_records
    for rec in field_records.get("records", [])[:10]:
        sources.append(
            {
                "fact": f"{rec['household_code']}: soil {rec.get('soil_wetness')}, crop {rec.get('crop')}, "
                f"condition {rec.get('crop_condition')} ({rec['source']}, {rec['checked']})",
                "source_type": "farmer_report" if rec["source"] != "officer_visit" else "visit",
                "source_ref": rec["source_ref"],
            }
        )

    pest_list = (state.get("pest_reports") or {}).get("reports", [])
    facts["pest_reports_list"] = pest_list
    for rep in pest_list:
        sources.append(
            {
                "fact": f"{rep['pest']} seen on {rep['crop']} at {rep['household_code']} ({rep['reported_via']}, {rep['checked']})",
                "source_type": "pest_report",
                "source_ref": rep["source_ref"],
            }
        )

    crop_calendars = state.get("crop_calendars") or {}
    facts["crop_calendar"] = crop_calendars
    for crop, cal in crop_calendars.items():
        sources.append(
            {
                "fact": f"{crop} planting window {cal['plant_from']} to {cal['plant_to']} in {state.get('zone')}",
                "source_type": "crop_calendar",
                "source_ref": cal["source_ref"],
            }
        )

    now_info = state.get("now_info")
    if now_info:
        facts["today"] = now_info["datetime"]
        sources.append(
            {
                "fact": f"Current date/time {now_info['datetime']} ({now_info['timezone']})",
                "source_type": "clock",
                "source_ref": "mcp-server-time:Africa/Kampala",
            }
        )

    await emit_fn(run_id, {"type": "model_call"})
    drafted = draft_advice_text(facts)
    await emit_fn(run_id, {"type": "model_result", "model": drafted.get("model"), "advice_preview": drafted["advice"][:160]})

    result, _err = await _call_with_retry(
        client,
        run_id,
        emit_fn,
        "draft_advice",
        group_id=group_id,
        draft_text=drafted["advice"],
        why_text=drafted["why"],
        sources=sources,
        language="English",
    )

    outcome = "failed"
    advice_id = None
    if result:
        advice_id = result.get("advice_id")
        outcome = "drafted" if result.get("status") == "pending_review" else "blocked"
        if advice_id and outcome == "drafted":
            due_on = (date.today() + timedelta(days=14)).isoformat()
            await _call_with_retry(client, run_id, emit_fn, "schedule_followup", advice_id=advice_id, due_on=due_on)

    await emit_fn(run_id, {"type": "run_finished", "outcome": outcome})
    return {
        "outcome": outcome,
        "advice_id": advice_id,
        "model_cost": drafted.get("cost", 0.0),
        "model_name": drafted.get("model"),
    }


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("plan", plan_node)
    graph.add_node("gather", gather_node)
    graph.add_node("check_urgent", check_urgent_node)
    graph.add_node("draft_or_flag", draft_or_flag_node)
    graph.set_entry_point("plan")
    graph.add_edge("plan", "gather")
    graph.add_edge("gather", "check_urgent")
    graph.add_edge("check_urgent", "draft_or_flag")
    graph.add_edge("draft_or_flag", END)
    return graph.compile()


async def execute_agent_run(group_id: int, run_id: int) -> None:
    """Runs the whole agent flow for one household group, streaming events for the
    Agent run screen, and always finalizing the Run row even if something fails."""
    events.create_run_queue(run_id)

    async def emit_fn(r_id: int, event: dict[str, Any]) -> None:
        await events.emit(r_id, event)

    final_state: dict[str, Any] = {}
    error: str | None = None
    try:
        async with ToolClient() as client:
            graph = build_graph()
            initial_state: AgentState = {
                "group_id": group_id,
                "run_id": run_id,
                "_client": client,
                "_emit": emit_fn,
            }
            final_state = await graph.ainvoke(initial_state)
    except Exception as exc:  # noqa: BLE001 - a failed run must still be recorded and closed
        error = str(exc)
        await emit_fn(run_id, {"type": "run_error", "error": error})
        final_state = {"outcome": "failed"}
    finally:
        _finalize_run(run_id, final_state, error=error)
        events.close_run_queue(run_id)


def _finalize_run(run_id: int, state: dict[str, Any], error: str | None) -> None:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.finished_at = datetime.now(timezone.utc)
        run.outcome = state.get("outcome", "failed")
        run.cost = state.get("model_cost") or 0.0
        if state.get("model_name"):
            run.model = state["model_name"]
        session.add(run)
        session.commit()
