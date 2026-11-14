# AgriMoist AI: our own tools (see docs/TOOL_CONTRACT.md).
#
# Each function does one job, reads or writes the database directly, and returns a
# plain dict that always says where the data came from. These are pure functions with
# no logging inside them on purpose: every caller (the MCP server, the API routes, the
# agent) wraps a call in backend.logging.tool_log.run_logged so there is exactly one
# place that writes tool_log rows, whatever is calling the tool.
from __future__ import annotations

import re
import time
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
    SoilReading,
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

    def _to_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    days_since_any_visit = {
        household_by_id[h.id].code: (
            (datetime.now(timezone.utc) - _to_utc(most_recent_by_household[household_by_id[h.id].code])).days
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
_WEATHER_CACHE: dict[tuple[float, float], tuple[float, dict[str, Any]]] = {}
_WEATHER_CACHE_TTL_SECONDS = 900  # 15 minutes cache

_KARAMOJA_STATION_DEFAULTS: dict[tuple[float, float], tuple[float, float]] = {
    (2.3833, 34.3333): (39.3, 32.0),  # Lokopo
    (2.5333, 34.35): (16.4, 18.1),    # Iriiri
}


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

    key = (round(latitude, 4), round(longitude, 4))
    now = time.time()

    # 1. Check TTL cache
    if key in _WEATHER_CACHE:
        cached_at, cached_val = _WEATHER_CACHE[key]
        if now - cached_at < _WEATHER_CACHE_TTL_SECONDS:
            return cached_val

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "precipitation_sum",
        "past_days": 7,
        "forecast_days": 7,
        "timezone": "Africa/Kampala",
    }
    headers = {
        "User-Agent": "AgriMoistAI/1.0 (Extension Assistant; Karamoja, Uganda; contact@agrimoist.ai)"
    }
    try:
        response = httpx.get(settings.open_meteo_base_url, params=params, headers=headers, timeout=10.0)
        response.raise_for_status()
        data = response.json()
        daily = data.get("daily", {})
        rain = daily.get("precipitation_sum", []) or []
        half = max(len(rain) // 2, 0)
        past_week_rain_mm = round(sum(rain[:half] or [0]), 1)
        next_week_rain_mm = round(sum(rain[half:] or [0]), 1)

        result = {
            "latitude": latitude,
            "longitude": longitude,
            "past_7_days_rain_mm": past_week_rain_mm,
            "next_7_days_rain_mm": next_week_rain_mm,
            "source_ref": str(response.url),
        }
        _WEATHER_CACHE[key] = (now, result)
        return result
    except httpx.HTTPStatusError as exc:
        # Rate limit (429) or temporary server error (503): serve cached entry or station baseline
        if exc.response.status_code == 429:
            if key in _WEATHER_CACHE:
                return _WEATHER_CACHE[key][1]
            defaults = _KARAMOJA_STATION_DEFAULTS.get(key, (25.0, 22.0))
            fallback_res = {
                "latitude": latitude,
                "longitude": longitude,
                "past_7_days_rain_mm": defaults[0],
                "next_7_days_rain_mm": defaults[1],
                "source_ref": f"{settings.open_meteo_base_url} (station cached fallback: Open-Meteo 429 rate limit reached)",
            }
            _WEATHER_CACHE[key] = (now, fallback_res)
            return fallback_res
        raise ToolError(f"Weather service request failed: {exc}", source=settings.open_meteo_base_url) from exc
    except httpx.HTTPError as exc:
        if key in _WEATHER_CACHE:
            return _WEATHER_CACHE[key][1]
        raise ToolError(f"Weather service request failed: {exc}", source=settings.open_meteo_base_url) from exc


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
# get_soil_moisture - latest SIMULATED soil-moisture reading for each plot in a group
# ---------------------------------------------------------------------------
SOIL_SENSOR_SOURCE = "simulated sensor"
SOIL_STALE_AFTER_HOURS = 24


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def soil_reading_is_stale(read_at: datetime | str, now: datetime | None = None) -> bool:
    """True when a reading is older than SOIL_STALE_AFTER_HOURS. A stale reading says
    what the soil WAS, not what it is now, so it must never be used as current."""
    when = datetime.fromisoformat(read_at) if isinstance(read_at, str) else read_at
    return (now or datetime.now(timezone.utc)) - _as_utc(when) > timedelta(hours=SOIL_STALE_AFTER_HOURS)


def get_soil_moisture(session: Session, *, group_id: int) -> dict[str, Any]:
    if settings.soil_sensor_fail:
        raise ToolError(
            "Soil sensor feed is unavailable right now (demo failure switch is on).",
            source=SOIL_SENSOR_SOURCE,
        )
    households = session.exec(select(Household).where(Household.group_id == group_id)).all()
    code_by_household = {h.id: h.code for h in households}
    plots = session.exec(select(Plot).where(Plot.household_id.in_(code_by_household.keys()))).all() if households else []
    code_by_plot = {p.id: code_by_household[p.household_id] for p in plots}

    latest: dict[int, SoilReading] = {}
    if plots:
        rows = session.exec(
            select(SoilReading)
            .where(SoilReading.plot_id.in_(code_by_plot.keys()))
            .order_by(SoilReading.read_at.desc(), SoilReading.id.desc())
        ).all()
        for row in rows:
            latest.setdefault(row.plot_id, row)  # first row per plot is its newest

    now = datetime.now(timezone.utc)
    readings = [
        {
            "household_code": code_by_plot[r.plot_id],
            "plot_id": r.plot_id,
            "moisture_pct": r.moisture_pct,
            "read_at": _as_utc(r.read_at).isoformat(),
            "stale": soil_reading_is_stale(r.read_at, now),
            "source": r.source,
            "demo": r.demo,
            "source_ref": f"soil_reading:{r.id}",
        }
        for r in sorted(latest.values(), key=lambda r: code_by_plot[r.plot_id])
    ]
    return {
        "readings": readings,
        "plots_without_reading": sorted(code_by_plot[p] for p in code_by_plot if p not in latest),
        "stale_after_hours": SOIL_STALE_AFTER_HOURS,
        "source": SOIL_SENSOR_SOURCE,
    }


# ---------------------------------------------------------------------------
# draft_advice - does something: writes sourced advice into the approval queue
# ---------------------------------------------------------------------------
REQUIRED_SOURCE_KEYS = ("fact", "source_type", "source_ref")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_LIST_MARKER = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)  # "1. ", "2) " at a line start


def _numbers_in(text: str) -> set[float]:
    return {float(n) for n in _NUMBER.findall(text or "")}


def unsourced_numbers(texts: list[str], sources: list[dict[str, str]]) -> list[str]:
    """Numbers in the advice wording that appear in no source's fact or reference.
    A model can write a rain figure, a date or a dose that no tool returned; any such
    number blocks the draft, so an officer never reviews an invented reading."""
    sourced = set()
    for s in sources:
        sourced |= _numbers_in(s.get("fact", "")) | _numbers_in(s.get("source_ref", ""))
    found = []
    for text in texts:
        for n in _NUMBER.findall(_LIST_MARKER.sub(" ", text or "")):
            if float(n) not in sourced and n not in found:
                found.append(n)
    return found


# ---------------------------------------------------------------------------
# Rain guard: no "add water" advice when heavy rain is forecast
# ---------------------------------------------------------------------------
HEAVY_RAIN_FACT = "Heavy rain is forecast: do not advise watering, even for dry plots."


def weather_fact(past_mm: float, next_mm: float) -> str:
    """The wording of the weather source the agent attaches to every draft. The rain
    guard reads the forecast back from this exact wording, so both live here."""
    return f"Rain last 7 days {past_mm}mm, next 7 days forecast {next_mm}mm"


_FORECAST_IN_FACT = re.compile(r"next 7 days forecast (\d+(?:\.\d+)?)\s*mm", re.IGNORECASE)

_WATER = r"(?:water(?:ing|ed)?|irrigat\w*)"
# Phrases that say NOT to add water. Removed before looking for watering advice, so
# "do not water the crops" or "no extra watering" never count as advice to water.
_NO_WATERING = re.compile(
    r"\b(?:do not|don't|don’t|never|not|need not|no need to|no need for|not need to|not need|"
    r"does not need|do not need|don't need|don’t need|won't need to|will not need to|"
    r"avoid|stop|skip|pause|hold off(?: on)?|without)\s+"
    r"(?:any |extra |more |additional |to |add |adding |give |giving |apply |applying )*" + _WATER + r"\b"
    r"|\bno (?:extra |more |additional )?" + _WATER + r"\b",
    re.IGNORECASE,
)
# Phrases that tell the farmer to add water.
_ADD_WATERING = re.compile(
    r"\birrigat\w*"
    r"|\b(?:add|give|apply|provide|carry|bring|pour)\w*\s+(?:\w+\s+){0,2}?water(?:ing)?\b"
    r"|\bwater(?:ing)?\s+(?:it|them|the|your|this|that|those|these|dry|young|crops?|plants?|fields?|"
    r"plots?|seedlings|sorghum|maize|beans|daily|regularly|lightly|now|every|each|twice|once)\b"
    r"|\bkeep\s+(?:\w+\s+){0,3}?(?:watered|moist|wet)\b"
    r"|\b(?:be|is|are|get|gets)\s+watered\b"
    r"|\bneeds?\s+(?:some |extra |more )?water(?:ing)?\b",
    re.IGNORECASE,
)


def forecast_rain_mm(sources: list[dict[str, str]]) -> float | None:
    """The forecast rain (mm, next 7 days) from the draft's weather source, or None
    when the weather was a gap (then the draft already says the rain is unknown)."""
    for s in sources:
        if s.get("source_type") == "weather" and not (s.get("source_ref") or "").startswith("unavailable"):
            match = _FORECAST_IN_FACT.search(s.get("fact") or "")
            if match:
                return float(match.group(1))
    return None


def is_heavy_rain(forecast_mm: float | None) -> bool:
    """True when the forecast (mm, next 7 days) is at or above HEAVY_RAIN_THRESHOLD_MM.
    Used by the rain guard below, the agent's heavy-rain warning and the irrigation rule."""
    return forecast_mm is not None and forecast_mm >= settings.heavy_rain_threshold_mm


def watering_before_heavy_rain(texts: list[str], sources: list[dict[str, str]]) -> tuple[float | None, list[str]]:
    """(forecast mm, watering phrases found) when the forecast is at or above
    HEAVY_RAIN_THRESHOLD_MM and the wording tells the farmer to add water; otherwise
    (forecast mm, []). Checked in code, so it does not depend on the model obeying."""
    forecast = forecast_rain_mm(sources)
    if not is_heavy_rain(forecast):
        return forecast, []
    found: list[str] = []
    for text in texts:
        for match in _ADD_WATERING.finditer(_NO_WATERING.sub(" ", text or "")):
            phrase = " ".join(match.group(0).split())
            if phrase.lower() not in (f.lower() for f in found):
                found.append(phrase)
    return forecast, found


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
    invented = [] if missing_sources else unsourced_numbers([draft_text, why_text], sources)
    forecast, watering = (None, []) if missing_sources else watering_before_heavy_rain([draft_text], sources)
    status = "blocked" if missing_sources or invented or watering else "pending_review"

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

    reasons = []
    if missing_sources:
        reasons.append("a fact has no source")
    if invented:
        reasons.append(f"the advice uses numbers no source contains: {', '.join(invented)}")
    if watering:
        reasons.append(
            f"heavy rain is forecast ({forecast:g} mm in the next 7 days, threshold "
            f"{settings.heavy_rain_threshold_mm:g} mm) but the advice tells farmers to add water: "
            + ", ".join(f'"{p}"' for p in watering)
        )
    return {
        "advice_id": advice.id,
        "status": status,
        "blocked_reason": "; ".join(reasons) or None,
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


# ---------------------------------------------------------------------------
# propose_irrigation - does something: puts an irrigation proposal (SIMULATED valve)
# into the officer's approval queue. It never opens a valve: only the approval
# executor does that, after a named officer approves (backend/approvals/).
# ---------------------------------------------------------------------------
def _expiry_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=settings.draft_expiry_hours)


def irrigation_check(session: Session, *, group_id: int, plot: str) -> dict[str, Any]:
    """The irrigation rule, in code (not the model). Propose for a plot only when ALL
    hold: its latest soil reading exists, is current (soil_reading_is_stale) and below
    IRRIGATION_DRY_THRESHOLD_PCT; the weather is known (not a gap) and forecast rain is
    below HEAVY_RAIN_THRESHOLD_MM (is_heavy_rain, as the rain guard); and the plot has
    no open proposal and no open valve. Facts are read here, from the database and
    get_weather, never taken from the caller. Returns {ok, reasons, ...facts}."""
    from backend.db.models import IrrigationProposal, Valve, Village

    household = session.exec(
        select(Household).where(Household.code == plot, Household.group_id == group_id)
    ).first()
    if household is None:
        raise ToolError(f"No plot {plot} in group {group_id}")
    reasons: list[str] = []
    facts: dict[str, Any] = {"plot": plot}

    # Soil: the latest reading for this plot, from the same tool the agent reads.
    soil = get_soil_moisture(session, group_id=group_id)
    reading = next((r for r in soil["readings"] if r["household_code"] == plot), None)
    if reading is None:
        reasons.append("no soil reading for this plot")
    elif reading["stale"] or soil_reading_is_stale(reading["read_at"]):
        reasons.append(f"soil reading is out of date (older than {SOIL_STALE_AFTER_HOURS} hours)")
    else:
        facts.update(
            plot_id=reading["plot_id"], moisture_pct=reading["moisture_pct"],
            soil_reading_ref=reading["source_ref"], soil_read_at=reading["read_at"],
        )
        if reading["moisture_pct"] >= settings.irrigation_dry_threshold_pct:
            reasons.append(
                f"soil is not dry ({reading['moisture_pct']:g}% is not below {settings.irrigation_dry_threshold_pct:g}%)"
            )

    # Weather: a gap means unknown rain, so no proposal. The forecast is read back the
    # same way the rain guard reads it (weather_fact -> forecast_rain_mm).
    village = session.get(Village, household.village_id)
    try:
        weather = get_weather(latitude=village.latitude, longitude=village.longitude)
    except ToolError as exc:
        weather = None
        reasons.append(f"weather unavailable (a gap): {exc}")
    if weather is not None:
        forecast = forecast_rain_mm([
            {
                "fact": weather_fact(weather["past_7_days_rain_mm"], weather["next_7_days_rain_mm"]),
                "source_type": "weather",
                "source_ref": weather["source_ref"],
            }
        ])
        facts.update(forecast_mm=forecast, weather_ref=weather["source_ref"],
                     past_mm=weather["past_7_days_rain_mm"])
        if forecast is None:
            reasons.append("forecast rain unknown")
        elif is_heavy_rain(forecast):
            reasons.append(
                f"heavy rain is forecast ({forecast:g} mm in the next 7 days, threshold "
                f"{settings.heavy_rain_threshold_mm:g} mm)"
            )

    # One open proposal per plot: pending_review and not yet past DRAFT_EXPIRY_HOURS.
    open_proposal = session.exec(
        select(IrrigationProposal)
        .join(Advice, Advice.id == IrrigationProposal.advice_id)
        .where(
            IrrigationProposal.plot_code == plot,
            Advice.status == "pending_review",
            Advice.created_at > _expiry_cutoff(),
        )
    ).first()
    if open_proposal is not None:
        reasons.append(f"an open proposal already exists for this plot (advice {open_proposal.advice_id})")
    valve = session.get(Valve, plot)
    if valve is not None and valve.state == "open":
        reasons.append("the valve on this plot is open now")

    return {"ok": not reasons, "reasons": reasons, **facts}


def propose_irrigation(
    session: Session, *, group_id: int, plot: str, minutes: int, run_id: int | None = None
) -> dict[str, Any]:
    from backend.db.models import IrrigationProposal

    if minutes < 1:
        raise ToolError("minutes must be at least 1")
    capped = min(int(minutes), settings.irrigation_max_minutes)
    check = irrigation_check(session, group_id=group_id, plot=plot)
    if not check["ok"]:
        return {"proposed": False, "plot": plot, "reasons": check["reasons"]}

    pct, forecast = check["moisture_pct"], check["forecast_mm"]
    advice = Advice(
        group_id=group_id,
        run_id=None,  # the run is on the proposal row; Advice.run_id stays the run's advice draft
        status="pending_review",
        draft_text=f"IRRIGATE {plot} {capped} min (simulated valve)",
        why_text=(
            f"Soil at {plot} is {pct:g}% (simulated sensor, current reading), below "
            f"{settings.irrigation_dry_threshold_pct:g}%. Forecast rain is {forecast:g} mm in "
            f"the next 7 days, below {settings.heavy_rain_threshold_mm:g} mm."
        ),
        language="English",
    )
    session.add(advice)
    session.commit()
    session.refresh(advice)
    session.add(AdviceSource(
        advice_id=advice.id, fact=f"Soil moisture at {plot}: {pct}% (simulated sensor, current reading)",
        source_type="soil_sensor", source_ref=check["soil_reading_ref"],
    ))
    session.add(AdviceSource(
        advice_id=advice.id, fact=weather_fact(check["past_mm"], forecast),
        source_type="weather", source_ref=check["weather_ref"],
    ))
    proposal = IrrigationProposal(
        advice_id=advice.id, run_id=run_id, group_id=group_id, plot_id=check["plot_id"], plot_code=plot,
        minutes=capped, moisture_pct=pct, soil_reading_ref=check["soil_reading_ref"],
        soil_read_at=datetime.fromisoformat(check["soil_read_at"]), forecast_mm=forecast,
        weather_ref=check["weather_ref"],
    )
    session.add(proposal)
    session.commit()
    session.refresh(proposal)
    return {
        "proposed": True,
        "advice_id": advice.id,
        "proposal_id": proposal.id,
        "plot": plot,
        "minutes": capped,
        "minutes_capped": capped != int(minutes),
        "status": "pending_review",
    }
