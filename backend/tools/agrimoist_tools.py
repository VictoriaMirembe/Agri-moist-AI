# AgriMoist AI: our own tools (see docs/TOOL_CONTRACT.md).
#
# Each function does one job, reads or writes the database directly, and returns a
# plain dict that always says where the data came from. These are pure functions with
# no logging inside them on purpose: every caller (the MCP server, the API routes, the
# agent) wraps a call in backend.logging.tool_log.run_logged so there is exactly one
# place that writes tool_log rows, whatever is calling the tool.
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from sqlmodel import Session, select

from backend.core.config import settings
from backend.db.models import (
    Advice,
    AdviceSource,
    CropCalendar,
    Followup,
    Household,
    Observation,
    Plot,
    PlotHistory,
    VisitFlag,
)
from backend.tools.errors import ToolError


# ---------------------------------------------------------------------------
# record_observation - does something: saves a field visit or a farmer report
# ---------------------------------------------------------------------------
def record_observation(
    session: Session,
    *,
    household_code: str,
    source: str,  # officer_visit, farmer_ussd, farmer_web
    recorded_by: int | None = None,
    crop: str | None = None,
    soil_wetness: str | None = None,
    rain_last_7_days: str | None = None,
    pest_seen: str | None = None,
    crop_condition: str | None = None,
    food_left: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    if source not in ("officer_visit", "farmer_ussd", "farmer_web"):
        raise ToolError(f"Unknown observation source: {source}")

    household = session.exec(select(Household).where(Household.code == household_code)).first()
    if household is None:
        raise ToolError(f"No household with code {household_code}")
    if not household.consent_given:
        raise ToolError(f"Household {household_code} has not given consent; nothing can be recorded")

    checked = "checked" if source == "officer_visit" else "unchecked"
    observation = Observation(
        household_id=household.id,
        source=source,
        checked=checked,
        recorded_by=recorded_by,
        observed_on=datetime.now(timezone.utc),
        crop=crop,
        soil_wetness=soil_wetness,
        rain_last_7_days=rain_last_7_days,
        pest_seen=pest_seen,
        crop_condition=crop_condition,
        food_left=food_left,
        notes=notes,
    )
    session.add(observation)
    session.commit()
    session.refresh(observation)
    return {
        "observation_id": observation.id,
        "household_code": household_code,
        "group_id": household.group_id,
        "checked": checked,
        "source_ref": f"observation:{observation.id}",
    }


# ---------------------------------------------------------------------------
# get_field_records - reads recent visits and farmer reports for a group
# ---------------------------------------------------------------------------
def get_field_records(session: Session, *, group_id: int, days: int = 60) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    households = session.exec(select(Household).where(Household.group_id == group_id)).all()
    household_by_id = {h.id: h for h in households}
    if not households:
        return {"records": [], "households_in_group": 0}

    observations = session.exec(
        select(Observation)
        .where(Observation.household_id.in_(household_by_id.keys()))
        .where(Observation.observed_on >= cutoff)
        .order_by(Observation.observed_on.desc())
    ).all()

    records = [
        {
            "household_code": household_by_id[o.household_id].code,
            "source": o.source,
            "checked": o.checked,
            "observed_on": o.observed_on.isoformat(),
            "crop": o.crop,
            "soil_wetness": o.soil_wetness,
            "rain_last_7_days": o.rain_last_7_days,
            "pest_seen": o.pest_seen,
            "crop_condition": o.crop_condition,
            "food_left": o.food_left,
            "notes": o.notes,
            "source_ref": f"observation:{o.id}",
        }
        for o in observations
    ]
    most_recent_by_household = {}
    for o in observations:
        code = household_by_id[o.household_id].code
        most_recent_by_household.setdefault(code, o.observed_on)

    days_since_any_visit = {
        household_by_id[h.id].code: (
            (datetime.now(timezone.utc) - most_recent_by_household[household_by_id[h.id].code]).days
            if household_by_id[h.id].code in most_recent_by_household
            else None
        )
        for h in households
    }

    return {
        "records": records,
        "households_in_group": len(households),
        "days_since_last_visit_by_household": days_since_any_visit,
    }


# ---------------------------------------------------------------------------
# get_plot_history - reads a group's plots' past crops, plantings and harvests
# ---------------------------------------------------------------------------
def get_plot_history(session: Session, *, group_id: int) -> dict[str, Any]:
    households = session.exec(select(Household).where(Household.group_id == group_id)).all()
    household_by_id = {h.id: h for h in households}
    plots = session.exec(select(Plot).where(Plot.household_id.in_(household_by_id.keys()))).all()
    plot_household = {p.id: household_by_id[p.household_id].code for p in plots}
    if not plots:
        return {"history": []}

    rows = session.exec(select(PlotHistory).where(PlotHistory.plot_id.in_(plot_household.keys()))).all()
    history = [
        {
            "household_code": plot_household[r.plot_id],
            "season": r.season,
            "crop": r.crop,
            "planted_on": r.planted_on.isoformat() if r.planted_on else None,
            "harvested_on": r.harvested_on.isoformat() if r.harvested_on else None,
            "yield_note": r.yield_note,
            "planting_date_missing": r.planted_on is None,
            "source_ref": f"plot_history:{r.id}",
        }
        for r in rows
    ]
    return {"history": history}


# ---------------------------------------------------------------------------
# get_crop_calendar - planting and harvest windows for a crop in a zone
# ---------------------------------------------------------------------------
def get_crop_calendar(session: Session, *, zone: str, crop: str) -> dict[str, Any]:
    row = session.exec(
        select(CropCalendar).where(CropCalendar.zone == zone).where(CropCalendar.crop == crop)
    ).first()
    if row is None:
        raise ToolError(f"No crop calendar entry for {crop} in {zone}")

    today = date.today()
    plant_from = _month_day_to_date(row.plant_from, today.year)
    plant_to = _month_day_to_date(row.plant_to, today.year)
    window_open = plant_from <= today <= plant_to
    window_closed_this_year = today > plant_to

    return {
        "zone": row.zone,
        "crop": row.crop,
        "plant_from": row.plant_from,
        "plant_to": row.plant_to,
        "harvest_from": row.harvest_from,
        "harvest_to": row.harvest_to,
        "planting_window_open_today": window_open,
        "planting_window_closed_this_year": window_closed_this_year,
        "source_ref": row.source_url,
    }


def _month_day_to_date(month_day: str, year: int) -> date:
    month, day = (int(p) for p in month_day.split("-"))
    return date(year, month, day)


# ---------------------------------------------------------------------------
# get_weather - rain forecast and recent rain from Open-Meteo (no key needed)
# ---------------------------------------------------------------------------
def get_weather(*, latitude: float, longitude: float) -> dict[str, Any]:
    if settings.weather_fail:
        raise ToolError(
            "Weather service is unavailable right now (demo failure switch is on).",
            source=settings.open_meteo_base_url,
        )
    if settings.weather_override_past_mm is not None and settings.weather_override_next_mm is not None:
        return {
            "latitude": latitude,
            "longitude": longitude,
            "past_7_days_rain_mm": settings.weather_override_past_mm,
            "next_7_days_rain_mm": settings.weather_override_next_mm,
            "source_ref": "demo-override (WEATHER_OVERRIDE_PAST_MM/WEATHER_OVERRIDE_NEXT_MM set, not a live reading)",
        }
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "precipitation_sum",
        "past_days": 7,
        "forecast_days": 7,
        "timezone": "Africa/Kampala",
    }
    try:
        response = httpx.get(settings.open_meteo_base_url, params=params, timeout=10.0)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise ToolError(f"Weather service request failed: {exc}", source=settings.open_meteo_base_url) from exc

    daily = data.get("daily", {})
    rain = daily.get("precipitation_sum", []) or []
    half = max(len(rain) // 2, 0)
    past_week_rain_mm = round(sum(rain[:half] or [0]), 1)
    next_week_rain_mm = round(sum(rain[half:] or [0]), 1)

    return {
        "latitude": latitude,
        "longitude": longitude,
        "past_7_days_rain_mm": past_week_rain_mm,
        "next_7_days_rain_mm": next_week_rain_mm,
        "source_ref": str(response.url),
    }


# ---------------------------------------------------------------------------
# get_pest_reports - pest sightings from recent visits and farmer reports nearby
# ---------------------------------------------------------------------------
def get_pest_reports(session: Session, *, group_id: int, days: int = 21) -> dict[str, Any]:
    group_households = session.exec(select(Household).where(Household.group_id == group_id)).all()
    if not group_households:
        return {"reports": []}
    village_id = group_households[0].village_id

    # "Nearby villages": every household in the same village as this group, plus the
    # group's own households, so a pest seen next door still surfaces as a signal.
    village_households = session.exec(select(Household).where(Household.village_id == village_id)).all()
    household_by_id = {h.id: h for h in village_households}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.exec(
        select(Observation)
        .where(Observation.household_id.in_(household_by_id.keys()))
        .where(Observation.pest_seen.is_not(None))
        .where(Observation.observed_on >= cutoff)
        .order_by(Observation.observed_on.desc())
    ).all()

    reports = [
        {
            "household_code": household_by_id[o.household_id].code,
            "pest": o.pest_seen,
            "crop": o.crop,
            "reported_via": o.source,
            "checked": o.checked,
            "observed_on": o.observed_on.isoformat(),
            "source_ref": f"observation:{o.id}",
        }
        for o in rows
    ]
    return {"reports": reports}


# ---------------------------------------------------------------------------
# draft_advice - does something: writes sourced advice into the approval queue
# ---------------------------------------------------------------------------
REQUIRED_SOURCE_KEYS = ("fact", "source_type", "source_ref")


def draft_advice(
    session: Session,
    *,
    group_id: int,
    run_id: int | None,
    draft_text: str,
    why_text: str,
    sources: list[dict[str, str]],
    language: str = "English",
) -> dict[str, Any]:
    missing_sources = not sources or any(
        not all(s.get(k) for k in REQUIRED_SOURCE_KEYS) for s in sources
    )
    status = "blocked" if missing_sources else "pending_review"

    advice = Advice(
        group_id=group_id,
        run_id=run_id,
        status=status,
        draft_text=draft_text,
        why_text=why_text,
        language=language,
    )
    session.add(advice)
    session.commit()
    session.refresh(advice)

    for s in sources:
        session.add(
            AdviceSource(
                advice_id=advice.id,
                fact=s.get("fact", ""),
                source_type=s.get("source_type", ""),
                source_ref=s.get("source_ref", ""),
            )
        )
    session.commit()

    return {
        "advice_id": advice.id,
        "status": status,
        "blocked_reason": "a fact has no source" if missing_sources else None,
    }


# ---------------------------------------------------------------------------
# flag_for_visit - does something: flags a group for an urgent officer visit
# ---------------------------------------------------------------------------
def flag_for_visit(session: Session, *, group_id: int, reason: str, raised_by: str = "assistant") -> dict[str, Any]:
    if raised_by not in ("assistant", "farmer_ussd", "farmer_web"):
        raise ToolError(f"Unknown raised_by value: {raised_by}")
    flag = VisitFlag(group_id=group_id, reason=reason, raised_by=raised_by, status="open")
    session.add(flag)
    session.commit()
    session.refresh(flag)
    return {"flag_id": flag.id, "group_id": group_id, "reason": reason, "status": "open"}


# ---------------------------------------------------------------------------
# schedule_followup - does something: books a check-back date for advice
# ---------------------------------------------------------------------------
def schedule_followup(session: Session, *, advice_id: int, due_on: date) -> dict[str, Any]:
    advice = session.get(Advice, advice_id)
    if advice is None:
        raise ToolError(f"No advice with id {advice_id}")
    followup = Followup(advice_id=advice_id, due_on=due_on)
    session.add(followup)
    session.commit()
    session.refresh(followup)
    return {"followup_id": followup.id, "advice_id": advice_id, "due_on": due_on.isoformat()}
