# Everything a farmer touches: reports, visit requests, reading approved advice, and
# the USSD menu (Africa's Talking callback). Farmers never chat with the assistant and
# never see the raw advice table - every read here goes through the
# advice_visible_to_farmer database view, which only ever contains approved and
# edited_and_approved rows (see backend/core/database.py).
from __future__ import annotations

import hmac
import logging
import re
import threading
import time
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlmodel import select

from backend.core.config import settings
from backend.core.database import session_scope
from backend.db.models import (
    Advice,
    Approval,
    Contact,
    CropCalendar,
    District,
    Followup,
    FollowupAnswer,
    Household,
    Observation,
    Officer,
    Plot,
    PlotHistory,
    UssdSession,
    Village,
    VisitFlag,
    utcnow,
)
from backend.agent import model_client
from backend.logging.tool_log import run_logged
from backend.tools import agrimoist_tools as impl
from backend.tools.errors import ToolError

router = APIRouter(tags=["farmer"])
logger = logging.getLogger("agrimoist.ussd")

PROBLEM_FIELD_MAP = {
    "pests": {"pest_seen": "farmer-reported pest"},
    "no_rain": {"rain_last_7_days": "none"},
    "heavy_rain": {"rain_last_7_days": "heavy"},
    "crop_sick": {"crop_condition": "poor"},
}


def _normalize_phone(phone: str) -> str:
    """Normalise a Uganda phone number to E.164 (+256…) for comparison.

    Handles the most common variants that farmers or officers enter:
      * +256700000001  (already canonical)
      * 0700000001     (local format, leading 0)
      * 256700000001   (no leading +)
      * 0772 890 123   (spaces)
      * 07-00-000-001  (dashes)
    """
    cleaned = re.sub(r"[\s\-]", "", phone).strip()
    if cleaned.startswith("0") and len(cleaned) == 10:
        # local Uganda format 07xxxxxxxx → +25607xxxxxxxx (strip leading 0)
        cleaned = "+256" + cleaned[1:]
    elif cleaned.startswith("256") and not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


def _contact_for_phone(session, household: Household, phone: str) -> Contact | None:
    """The household's contact whose number matches `phone` in any accepted form
    (07..., +256..., 256..., with spaces or dashes)."""
    normalised = _normalize_phone(phone)
    contacts = session.exec(select(Contact).where(Contact.household_id == household.id)).all()
    return next((c for c in contacts if _normalize_phone(c.phone) == normalised), None)


def _household_for_phone(session, phone: str) -> Household | None:
    """Look up a household by phone, normalising both the query and stored numbers."""
    normalised = _normalize_phone(phone)
    contacts = session.exec(select(Contact)).all()
    matching_contact = next(
        (c for c in contacts if _normalize_phone(c.phone) == normalised),
        None,
    )
    if matching_contact is None:
        return None
    household = session.get(Household, matching_contact.household_id)
    return household if household and household.consent_given else None


def _advice_for_group(session, group_id: int) -> dict:
    row = session.execute(
        text(
            "SELECT id, status, final_text, translated_text, language, created_at "
            "FROM advice_visible_to_farmer WHERE group_id = :gid ORDER BY created_at DESC LIMIT 1"
        ),
        {"gid": group_id},
    ).mappings().first()
    if row is None:
        return {"has_advice": False}

    officer_name = None
    approval = session.exec(
        select(Approval).where(Approval.advice_id == row["id"]).order_by(Approval.decided_at.desc())
    ).first()
    if approval:
        officer = session.get(Officer, approval.officer_id)
        officer_name = officer.name if officer else None

    shown_text = row["translated_text"] or row["final_text"]
    return {
        "has_advice": True,
        "advice_id": row["id"],
        "status": row["status"],
        "text": shown_text,
        "language": settings.farmer_language if row["translated_text"] else (row["language"] or "English"),
        "officer_name": officer_name,
        "date": str(row["created_at"]),
    }


def _log_advice_viewed(session, household: Household, info: dict, channel: str) -> None:
    """Record that a farmer was shown approved advice. Only a log entry: the advice
    status never changes here (only backend/approvals may change it)."""
    if not info.get("has_advice"):
        return
    run_logged(
        session,
        tool="farmer_viewed_advice",
        inputs={"household_code": household.code, "advice_id": info["advice_id"]},
        actor="farmer",
        source=channel,
        run_id=None,
        func=lambda: {"household_code": household.code, "advice_id": info["advice_id"], "status": info["status"]},
    )


def _save_followup(
    session, advice_id: int, household: Household, followed: str, helped: str | None, channel: str
) -> None:
    # The advice's Followup row keeps the latest answer, as before.
    followup = session.exec(select(Followup).where(Followup.advice_id == advice_id)).first()
    if followup is None:
        followup = Followup(advice_id=advice_id, due_on=date.today())
    followup.followed = followed
    followup.helped = helped
    followup.answered_via = channel
    session.add(followup)
    # Each household's own answer, so households in one group never overwrite each other.
    answer = session.exec(
        select(FollowupAnswer).where(
            FollowupAnswer.advice_id == advice_id, FollowupAnswer.household_id == household.id
        )
    ).first()
    if answer is None:
        answer = FollowupAnswer(advice_id=advice_id, household_id=household.id, followed=followed, answered_via=channel)
    answer.followed = followed
    answer.helped = helped
    answer.answered_via = channel
    answer.answered_at = datetime.now(timezone.utc)
    session.add(answer)
    session.commit()


# ---------------------------------------------------------------------------
# Farmer reports and visit requests (web)
# ---------------------------------------------------------------------------
class FarmerReportRequest(BaseModel):
    phone: str
    problem: str  # pests, no_rain, heavy_rain, crop_sick
    crop: str | None = None  # sorghum, maize, or a crop name the farmer typed under "Other"
    pest: str | None = None  # optional: the pest the farmer picked or named (problem "pests" only)

    @field_validator("crop")
    @classmethod
    def _tidy_crop(cls, value: str | None) -> str | None:
        # A typed crop name is saved trimmed, lowercase and at most 40 characters;
        # an empty one means no crop, as before. "sorghum" and "maize" are unchanged.
        if value is None:
            return None
        return " ".join(value.split())[:40].strip().lower() or None


def _report_fields(problem: str, pest: str | None) -> dict:
    """Observation fields for a farmer report. A pest the farmer named replaces the
    generic "farmer-reported pest"; a channel that cannot name one keeps it."""
    fields = dict(PROBLEM_FIELD_MAP[problem])
    name = " ".join((pest or "").split())[:60]
    if problem == "pests" and name:
        fields["pest_seen"] = name.lower()
    return fields


@router.post("/api/farmer-reports")
def create_farmer_report(body: FarmerReportRequest):
    if body.problem not in PROBLEM_FIELD_MAP:
        raise HTTPException(400, f"Unknown problem: {body.problem}")
    with session_scope() as session:
        household = _household_for_phone(session, body.phone)
        if household is None:
            raise HTTPException(404, "This number is not registered. Please ask your extension officer.")
        fields = _report_fields(body.problem, body.pest)
        try:
            return run_logged(
                session,
                tool="record_observation",
                inputs=body.model_dump(),
                actor="farmer",
                source="farmer_web",
                run_id=None,
                func=lambda: impl.record_observation(
                    session, household_code=household.code, source="farmer_web", crop=body.crop, **fields
                ),
            )
        except ToolError as exc:
            raise HTTPException(400, str(exc)) from exc


class VisitRequestBody(BaseModel):
    phone: str
    reason: str


@router.post("/api/visit-requests")
def create_visit_request(body: VisitRequestBody):
    with session_scope() as session:
        household = _household_for_phone(session, body.phone)
        if household is None:
            raise HTTPException(404, "This number is not registered. Please ask your extension officer.")
        return run_logged(
            session,
            tool="flag_for_visit",
            inputs=body.model_dump(),
            actor="farmer",
            source="farmer_web",
            run_id=None,
            func=lambda: impl.flag_for_visit(
                session,
                group_id=household.group_id,
                reason=f"{household.code} asked for a visit: {body.reason.strip().lower()}",
                raised_by="farmer_web",
            ),
        )


@router.get("/api/farmer/advice")
def farmer_advice(phone: str):
    with session_scope() as session:
        household = _household_for_phone(session, phone)
        if household is None:
            raise HTTPException(404, "This number is not registered. Please ask your extension officer.")
        info = _advice_for_group(session, household.group_id)
        _log_advice_viewed(session, household, info, "farmer_web")
        return info


@router.get("/api/farmer/updates")
def farmer_updates(phone: str):
    """What the farmer page polls in the background, so officer decisions (a report
    checked, a visit done, advice approved) appear without a reload. Not logged as
    the farmer reading advice - only opening the page or the advice counts."""
    with session_scope() as session:
        household = _household_for_phone(session, phone)
        if household is None:
            raise HTTPException(404, "This number is not registered. Please ask your extension officer.")
        return {
            "advice": _advice_for_group(session, household.group_id),
            "recent_requests": _recent_farmer_requests(session, household),
        }


@router.get("/api/farmer/profile")
def farmer_profile(phone: str):
    with session_scope() as session:
        household = _household_for_phone(session, phone)
        if household is None:
            raise HTTPException(404, "This number is not registered. Please ask your extension officer.")
        contact = _contact_for_phone(session, household, phone)
        if contact is None:
            raise HTTPException(404, "No registered farmer contact was found for this household.")
        return {
            "full_name": contact.name,
            "household_code": household.code,
            "group_id": household.group_id,
        }


@router.get("/api/farmer/dashboard")
def farmer_dashboard(phone: str):
    with session_scope() as session:
        household = _household_for_phone(session, phone)
        if household is None:
            raise HTTPException(404, "This number is not registered. Please ask your extension officer.")

        contact = _contact_for_phone(session, household, phone)
        if contact is None:
            raise HTTPException(404, "No registered farmer contact was found for this household.")

        village = session.get(Village, household.village_id)
        district = session.get(District, village.district_id) if village else None
        latest_observation = session.exec(
            select(Observation)
            .where(Observation.household_id == household.id)
            .order_by(Observation.observed_on.desc())
        ).first()
        plots = session.exec(select(Plot).where(Plot.household_id == household.id)).all()
        plot_ids = [plot.id for plot in plots if plot.id is not None]
        histories = (
            session.exec(select(PlotHistory).where(PlotHistory.plot_id.in_(plot_ids))).all()
            if plot_ids
            else []
        )
        calendars = session.exec(
            select(CropCalendar)
            .where(CropCalendar.zone == (district.agroclimatic_zone if district else ""))
            .order_by(CropCalendar.crop)
        ).all()
        recent_requests = _recent_farmer_requests(session, household)
        coordinates = (village.latitude, village.longitude) if village else None
        profile = {
            "full_name": contact.name,
            "household_code": household.code,
            "group_id": household.group_id,
            "village": village.name if village else None,
            "district": district.name if district else None,
            "zone": district.agroclimatic_zone if district else None,
            "crops": sorted({history.crop for history in histories}),
        }
        observation = (
            {
                "soil_wetness": latest_observation.soil_wetness,
                "rain_last_7_days": latest_observation.rain_last_7_days,
                "crop": latest_observation.crop,
                "observed_on": latest_observation.observed_on.date().isoformat(),
                "source": latest_observation.source,
            }
            if latest_observation
            else None
        )
        crop_calendar = [
            {
                "crop": calendar.crop,
                "plant_from": calendar.plant_from,
                "plant_to": calendar.plant_to,
                "harvest_from": calendar.harvest_from,
                "harvest_to": calendar.harvest_to,
                "source_url": calendar.source_url,
            }
            for calendar in calendars
        ]

    weather = None
    weather_error = None
    if coordinates:
        try:
            weather = impl.get_weather(latitude=coordinates[0], longitude=coordinates[1])
        except ToolError as exc:
            err_text = str(exc)
            if "429" in err_text or "Too Many Requests" in err_text:
                weather_error = "Weather service rate limit reached. Retrying automatically..."
            else:
                weather_error = err_text

    return {
        "profile": profile,
        "latest_observation": observation,
        "weather": weather,
        "weather_error": weather_error,
        "crop_calendar": crop_calendar,
        "ussd_service_code": settings.ussd_service_code,
        "recent_requests": recent_requests,
    }


CHANNEL_LABELS = {"farmer_ussd": "USSD", "farmer_web": "Web"}


def _describe_report(observation: Observation) -> str:
    problems = []
    if observation.pest_seen:
        problems.append("Pests")
    if observation.rain_last_7_days == "none":
        problems.append("No rain")
    if observation.rain_last_7_days == "heavy":
        problems.append("Too much rain")
    if observation.crop_condition == "poor":
        problems.append("Crop looks sick")
    summary = ", ".join(problems) or "Field report"
    if observation.crop and observation.crop != "other":
        summary += f" on {observation.crop}"
    return summary


def _visit_reason_for_farmer(reason: str, household_code: str) -> str:
    # Stored as "H-009 asked for a visit: little food left" for the officer; the
    # farmer only needs to see their own reason.
    prefix = f"{household_code} asked for a visit: "
    if reason.startswith(prefix):
        reason = reason[len(prefix):]
        return reason[:1].upper() + reason[1:]
    return reason


def _recent_farmer_requests(session, household: Household, limit: int = 6) -> list[dict]:
    """What the farmer sent from their phone or this site, newest first. Reports
    belong to the household; visit requests are kept per household group."""
    reports = session.exec(
        select(Observation)
        .where(Observation.household_id == household.id, Observation.source.in_(list(CHANNEL_LABELS)))
        .order_by(Observation.observed_on.desc())
        .limit(limit)
    ).all()
    visits = session.exec(
        select(VisitFlag)
        .where(VisitFlag.group_id == household.group_id, VisitFlag.raised_by.in_(list(CHANNEL_LABELS)))
        .order_by(VisitFlag.created_at.desc())
        .limit(limit)
    ).all()
    items = [
        {
            "kind": "report",
            "summary": _describe_report(report),
            "channel": CHANNEL_LABELS[report.source],
            "status": "Checked by officer" if report.checked == "checked" else "Waiting for officer",
            "at": report.observed_on.isoformat(),
        }
        for report in reports
    ] + [
        {
            "kind": "visit",
            "summary": "Visit request: " + _visit_reason_for_farmer(visit.reason, household.code),
            "channel": CHANNEL_LABELS[visit.raised_by],
            "status": {"open": "Waiting for officer", "visited": "Officer visited"}.get(visit.status, "Closed"),
            "at": visit.created_at.isoformat(),
        }
        for visit in visits
    ]
    return sorted(items, key=lambda item: item["at"], reverse=True)[:limit]


class FollowupBody(BaseModel):
    phone: str
    advice_id: int
    followed: Literal["yes", "no", "partly"]
    helped: Literal["yes", "no", "too_early"] | None = None


@router.post("/api/farmer/followup")
def farmer_followup(body: FollowupBody):
    with session_scope() as session:
        household = _household_for_phone(session, body.phone)
        if household is None:
            raise HTTPException(404, "This number is not registered. Please ask your extension officer.")
        advice = session.get(Advice, body.advice_id)
        if (
            advice is None
            or advice.group_id != household.group_id
            or advice.status not in ("approved", "edited_and_approved")
        ):
            raise HTTPException(404, "No approved advice found for this household.")
        _save_followup(session, body.advice_id, household, body.followed, body.helped, "farmer_web")
        run_logged(
            session,
            tool="record_followup",
            inputs=body.model_dump(),
            actor="farmer",
            source="farmer_web",
            run_id=None,
            func=lambda: {"advice_id": body.advice_id, "followed": body.followed, "helped": body.helped},
        )
        return {"advice_id": body.advice_id, "followed": body.followed, "helped": body.helped}


# ---------------------------------------------------------------------------
# USSD - Africa's Talking callback.
#
# Africa's Talking POSTs application/x-www-form-urlencoded to the callback URL set
# on the USSD channel, once per keypress: sessionId, serviceCode, phoneNumber,
# networkCode and text. `text` is everything the user has typed this session,
# joined by "*" ("" on the first request). The reply is plain text starting "CON "
# (show the screen, wait for input) or "END " (show the screen, close the session).
# When the session closes, Africa's Talking separately POSTs the outcome to the
# channel's events callback URL.
#
# POST /ussd/africastalking         - point the AT USSD channel callback here
# POST /ussd/africastalking/events  - point the AT events callback here
# POST /ussd                        - same menu for the in-app simulator; also takes JSON
#
# Africa's Talking does not sign callbacks, so when USSD_CALLBACK_TOKEN is set the
# two /ussd/africastalking URLs only answer requests carrying ?token=<that value>.
# Put the token in the callback URLs on the AT dashboard. A serviceCode that is
# not ours (USSD_SERVICE_CODE) is refused as well.
# ---------------------------------------------------------------------------
class UssdRequest(BaseModel):
    session_id: str = Field(min_length=1, validation_alias="sessionId", serialization_alias="sessionId")
    phone: str = Field(min_length=1, validation_alias="phoneNumber", serialization_alias="phoneNumber")
    text: str = ""
    service_code: str | None = Field(default=None, validation_alias="serviceCode", serialization_alias="serviceCode")
    network_code: str | None = Field(default=None, validation_alias="networkCode", serialization_alias="networkCode")

    model_config = {"populate_by_name": True}


AFRICAS_TALKING_PROBLEM_MAP = {"1": "pests", "2": "no_rain", "3": "heavy_rain", "4": "crop_sick"}
CROP_MAP = {"1": "sorghum", "2": "maize", "3": "other"}
VISIT_REASON_MAP = {"1": "Pests", "2": "Little food left", "3": "Other"}
FOLLOWED_MAP = {"1": "yes", "2": "partly", "3": "no"}
HELPED_MAP = {"1": "yes", "2": "no", "3": "too_early"}
MAIN_MENU_MAP = {"1": "advice", "2": "report", "3": "visit", "4": "followup"}

# The options each screen accepts, keyed by (first choice, how many choices so far).
MENU_OPTIONS = {
    ("2", 1): AFRICAS_TALKING_PROBLEM_MAP,
    ("2", 2): CROP_MAP,
    ("3", 1): VISIT_REASON_MAP,
    ("4", 1): FOLLOWED_MAP,
    ("4", 2): HELPED_MAP,
}

MAIN_MENU = "AgriMoist\n1. My advice\n2. Report a problem\n3. Ask for officer visit\n4. Did advice help?"
NOT_ON_LIST = "That choice is not on the list.\n"
NOT_UNDERSTOOD = "END Sorry, that was not understood. Please dial again."
NO_ADVICE_TO_ANSWER = "END No advice to answer yet. Your officer will send advice after checking."
NOT_REGISTERED = "END This number is not registered. Please ask your extension officer."
WRONG_SERVICE_CODE = "END This service is not available on this code."
ALREADY_RECEIVED = "END Thank you, we already have this. Your officer will follow up."
NAV_HINT = "\n0. Back  00. Home"
BACK, HOME = "0", "00"
DONE = "done:"  # last_screen prefix once a session has saved something

AT_SESSION_STATUSES = {"Success", "Incomplete", "Failed"}


def _check_callback_token(request: Request) -> None:
    expected = settings.ussd_callback_token
    if expected and not hmac.compare_digest(request.query_params.get("token", ""), expected):
        raise HTTPException(403, "Unknown caller")


def _normalise_code(code: str) -> str:
    return code.replace(" ", "").strip().lower()


async def _read_form_or_json(request: Request) -> dict:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/json":
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


async def _read_ussd_request(request: Request) -> UssdRequest:
    """Africa's Talking sends form data; tests and older clients may send JSON."""
    try:
        return UssdRequest.model_validate(await _read_form_or_json(request))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(400, "Expected sessionId, phoneNumber and text") from exc


def _save_ussd_session(session, session_id: str, phone: str, screen: str) -> None:
    row = session.get(UssdSession, session_id) or UssdSession(session_id=session_id, phone=phone)
    if phone:
        row.phone = phone
    row.last_screen = screen
    row.updated_at = utcnow()
    session.add(row)
    session.commit()


def _ussd_reply(body: UssdRequest) -> str:
    typed = body.text.strip()
    with session_scope() as session:
        row = session.get(UssdSession, body.session_id)
        # Africa's Talking resends a callback it timed out on. If this exact input
        # already saved a report or request in this session, do not save it twice.
        if typed and row is not None and row.last_screen == DONE + typed:
            return ALREADY_RECEIVED

        household = _household_for_phone(session, body.phone)
        if household is None:
            _save_ussd_session(session, body.session_id, body.phone, "not_registered")
            return NOT_REGISTERED

        steps, invalid = _resolve_navigation(typed)
        reply, saved = _handle_ussd(session, household, body.phone, steps, invalid)
        screen = DONE + typed if saved else ("/".join(steps) or "home")
        _save_ussd_session(session, body.session_id, body.phone, screen)
        return reply


@router.post("/ussd", response_class=PlainTextResponse)
async def ussd(request: Request):
    body = await _read_ussd_request(request)
    return await run_in_threadpool(_ussd_reply, body)


@router.post("/ussd/africastalking", response_class=PlainTextResponse)
async def ussd_africastalking(request: Request):
    _check_callback_token(request)
    body = await _read_ussd_request(request)
    expected_code = settings.ussd_service_code
    if body.service_code and expected_code and _normalise_code(body.service_code) != _normalise_code(expected_code):
        logger.warning(
            "USSD callback for service code %s, but USSD_SERVICE_CODE is %s", body.service_code, expected_code
        )
        return WRONG_SERVICE_CODE
    return await run_in_threadpool(_ussd_reply, body)


def _record_session_end(session_id: str, status: str) -> bool:
    with session_scope() as session:
        if session.get(UssdSession, session_id) is None:
            return False  # only sessions our callback actually served are recorded
        _save_ussd_session(session, session_id, "", f"ended:{status}")
        return True


@router.post("/ussd/africastalking/events")
async def ussd_session_event(request: Request):
    """End-of-session notification from Africa's Talking (status Success, Incomplete
    or Failed). Saved on the session row; Africa's Talking only needs a 200."""
    _check_callback_token(request)
    try:
        data = await _read_form_or_json(request)
    except ValueError:
        data = {}
    session_id = str(data.get("sessionId") or "")
    status = data.get("status") if data.get("status") in AT_SESSION_STATUSES else "unknown"
    recorded = bool(session_id) and await run_in_threadpool(_record_session_end, session_id, status)
    return {"received": True, "recorded": recorded}


def _resolve_navigation(raw_text: str) -> tuple[list[str], bool]:
    """Turn the typed input into the menu path the farmer is on.

    Africa's Talking never rewrites `text`, so every keypress of the session is in
    it. "0" goes back a screen and "00" goes home. A choice that is not on the
    screen's list is dropped, so the same screen is shown again. The second value
    is True when the last thing typed was such a dropped choice."""
    steps: list[str] = []
    invalid = False
    for part in raw_text.split("*") if raw_text else []:
        part = part.strip()
        invalid = False
        if part == HOME:
            steps.clear()
        elif part == BACK:
            if steps:
                steps.pop()
        else:
            options = MAIN_MENU_MAP if not steps else MENU_OPTIONS.get((steps[0], len(steps)))
            if options is not None and part in options:
                steps.append(part)
            else:
                invalid = True
    return steps, invalid


def _handle_ussd(session, household: Household, phone: str, steps: list[str], invalid: bool) -> tuple[str, bool]:
    """Return the reply and whether this step saved something."""
    retry = NOT_ON_LIST if invalid else ""

    if not steps:
        return f"CON {retry}{MAIN_MENU}", False

    top = steps[0]

    if top == "1":
        info = _advice_for_group(session, household.group_id)
        if not info.get("has_advice"):
            return "END No new advice yet. Your officer will send it after checking.", False
        _log_advice_viewed(session, household, info, "farmer_ussd")
        officer_bit = f" Approved by {info['officer_name']}," if info.get("officer_name") else ""
        return f"END {info['text'][:140].rstrip('.')}.{officer_bit} {info['date'][:10]}.", False

    if top == "2":
        if len(steps) == 1:
            return (
                f"CON {retry}What did you see?\n1. Pests  2. No rain  3. Too much rain  4. Crop looks sick" + NAV_HINT,
                False,
            )
        if len(steps) == 2:
            return f"CON {retry}Which crop?\n1. Sorghum  2. Maize  3. Other" + NAV_HINT, False
        problem = AFRICAS_TALKING_PROBLEM_MAP[steps[1]]
        crop = CROP_MAP[steps[2]]
        fields = PROBLEM_FIELD_MAP[problem]
        run_logged(
            session,
            tool="record_observation",
            inputs={"phone": phone, "problem": problem, "crop": crop},
            actor="farmer",
            source="farmer_ussd",
            run_id=None,
            func=lambda: impl.record_observation(
                session, household_code=household.code, source="farmer_ussd", crop=crop, **fields
            ),
        )
        return "END Thank you. Your officer will check this report.", True

    if top == "3":
        if len(steps) == 1:
            return f"CON {retry}Why?\n1. Pests  2. Little food left  3. Other" + NAV_HINT, False
        reason = VISIT_REASON_MAP[steps[1]]
        run_logged(
            session,
            tool="flag_for_visit",
            inputs={"phone": phone, "reason": reason},
            actor="farmer",
            source="farmer_ussd",
            run_id=None,
            func=lambda: impl.flag_for_visit(
                session,
                group_id=household.group_id,
                reason=f"{household.code} asked for a visit: {reason.lower()}",
                raised_by="farmer_ussd",
            ),
        )
        return "END Request sent. Your officer will plan a visit.", True

    if top == "4":
        info = _advice_for_group(session, household.group_id)
        if not info.get("has_advice"):
            # Nothing approved to answer about: say so, and save nothing.
            return NO_ADVICE_TO_ANSWER, False
        if len(steps) == 1:
            return f"CON {retry}Did you follow the last advice?\n1. Yes  2. Partly  3. No" + NAV_HINT, False
        if len(steps) == 2:
            return f"CON {retry}Did it help?\n1. Yes  2. No  3. Too early to say" + NAV_HINT, False
        followed, helped = FOLLOWED_MAP[steps[1]], HELPED_MAP[steps[2]]
        _save_followup(session, info["advice_id"], household, followed, helped, "farmer_ussd")
        run_logged(
            session,
            tool="record_followup",
            inputs={"phone": phone, "advice_id": info["advice_id"], "followed": followed, "helped": helped},
            actor="farmer",
            source="farmer_ussd",
            run_id=None,
            func=lambda: {"advice_id": info["advice_id"], "followed": followed, "helped": helped},
        )
        return "END Thank you. This helps improve advice.", True

    return NOT_UNDERSTOOD, False


# ---------------------------------------------------------------------------
# Farmer voice audio scripts (English, Karamojong, Swahili)
# ---------------------------------------------------------------------------
VOICE_LANG_LABELS = {
    "en": "English",
    "kdj": "Ngakarimojong",
    "karamojong": "Ngakarimojong",
    "sw": "Kiswahili",
    "swahili": "Kiswahili",
}


def _canonical_voice_lang(lang: str) -> str:
    cleaned = (lang or "").strip().lower()
    if cleaned in ("sw", "swahili", "kiswahili"):
        return "sw"
    if cleaned in ("kdj", "karamojong", "ngakarimojong", "ngakarimonjong"):
        return "kdj"
    return "en"


@router.get("/api/farmer/voice-script")
def get_farmer_voice_script(phone: str = "", lang: str = "en", view: str = "dashboard"):
    """Returns a structured voice narration script for the farmer page in English,
    Karamojong (Ngakarimojong), or Swahili (Kiswahili). Suitable for browser Web Speech
    Synthesis and audio visualizers."""
    norm_lang = _canonical_voice_lang(lang)
    lang_name = VOICE_LANG_LABELS.get(norm_lang, "English")
    sentences: list[dict[str, str]] = []

    if view == "signin" or (view == "dashboard" and not phone):
        if norm_lang == "sw":
            sentences.append({
                "label": "Karibu",
                "text": "Karibu kwenye ukurasa wa wakulima wa AgriMoist AI. Tafadhali ingiza nambari yako ya simu iliyosajiliwa ili kupakia taarifa za shamba lako.",
            })
        elif norm_lang == "kdj":
            sentences.append({
                "label": "Ejok nooi",
                "text": "Ejok nooi amana kon aloma AgriMoist AI. Tolim namba kon naka simu na egeritai ka ekaunan aanyun eboyer loka amana kon.",
            })
        else:
            sentences.append({
                "label": "Welcome",
                "text": "Welcome to the AgriMoist AI farmer portal. Please sign in with your registered phone number to view your farm details, weather forecast, and extension officer advice.",
            })
        full_text = " ".join(s["text"] for s in sentences)
        return {"language": norm_lang, "language_label": lang_name, "view": view, "sentences": sentences, "full_script": full_text}

    if view == "report":
        if norm_lang == "sw":
            sentences.append({"label": "Ripoti tatizo", "text": "Ripoti tatizo shambani. Taarifa yako itatumwa kwa afisa kilimo wako kuikagua."})
            sentences.append({"label": "Maelezo", "text": "Chagua kama umeona wadudu waharibifu, ukame na ukosefu wa mvua, mvua nyingi kupita kiasi, au mmea unaugua. Kisha chagua aina ya mmea na ubonyeze Tuma ripoti."})
        elif norm_lang == "kdj":
            sentences.append({"label": "Elimo aroniar", "text": "Elimo ngikaat na aroniar amana. Ekaunan ekoniite ripoot kon."})
            sentences.append({"label": "Akisil", "text": "Tooseu arai ngikuruk, akamu na emam akiru, akiru na alal nooi, kori ekaat lo ekae. Kosodi tooseu ngimomwa kon kosodi tojuka."})
        else:
            sentences.append({"label": "Report issue", "text": "Report an issue. Your report will be shared with your extension officer for review."})
            sentences.append({"label": "Options", "text": "Select what you observed: pests, no rain, too much rain, or sick crops. Choose your crop and click Send report."})
        full_text = " ".join(s["text"] for s in sentences)
        return {"language": norm_lang, "language_label": lang_name, "view": view, "sentences": sentences, "full_script": full_text}

    if view == "visit":
        if norm_lang == "sw":
            sentences.append({"label": "Omba ziara", "text": "Omba afisa kilimo akutembelee shambani."})
            sentences.append({"label": "Sababu", "text": "Eleza afisa wako sababu ya kuhitaji ukaguzi, kama vile wadudu shambani au chakula kidogo kilichobaki nyumbani."})
        elif norm_lang == "kdj":
            sentences.append({"label": "Ilipe ekaunan", "text": "Ilipe ekaunan ebu amana kon."})
            sentences.append({"label": "Eboyer", "text": "Kitatam ekaunan nyo ebunere amana, kotere ngikuruk aloma amana kori edis akimuj na edongit."})
        else:
            sentences.append({"label": "Request visit", "text": "Request an extension officer visit. Let your officer know why you need a visit to your farm, such as pests or food security concerns."})
        full_text = " ".join(s["text"] for s in sentences)
        return {"language": norm_lang, "language_label": lang_name, "view": view, "sentences": sentences, "full_script": full_text}

    if view == "followup":
        if norm_lang == "sw":
            sentences.append({"label": "Tathmini ya ushauri", "text": "Tathmini ya ushauri wa kilimo. Eleza kama ulifuata ushauri wa mwisho ulioidhinishwa, na kama ulikusaidia kuboresha mavuno yako."})
        elif norm_lang == "kdj":
            sentences.append({"label": "Akiwotokin", "text": "Akiwotokin naka akisil. Kitatam arai itupit akisil na sek, ka arai kitiakite amana kon."})
        else:
            sentences.append({"label": "Advice follow-up", "text": "Advice follow-up. Share whether you followed the approved advice from your extension officer, and whether it helped your field."})
        full_text = " ".join(s["text"] for s in sentences)
        return {"language": norm_lang, "language_label": lang_name, "view": view, "sentences": sentences, "full_script": full_text}

    with session_scope() as session:
        household = _household_for_phone(session, phone)
        if household is None:
            if norm_lang == "sw":
                text = "Nambari hii ya simu haijasajiliwa. Tafadhali wasiliana na afisa wako wa kilimo."
            elif norm_lang == "kdj":
                text = "Namba kon na emam na egeritai. Inyono ekaunan kon."
            else:
                text = "This phone number is not registered. Please contact your extension officer."
            return {"language": norm_lang, "language_label": lang_name, "view": view, "sentences": [{"label": "Notice", "text": text}], "full_script": text}

        contact = _contact_for_phone(session, household, phone)
        farmer_name = contact.name if contact else "Farmer"
        village = session.get(Village, household.village_id)
        district = session.get(District, village.district_id) if village else None
        loc_str = ", ".join(filter(None, [village.name if village else None, district.name if district else None]))

        advice_info = _advice_for_group(session, household.group_id)

        if view == "advice":
            if advice_info.get("has_advice"):
                raw_text = advice_info.get("text", "")
                officer = advice_info.get("officer_name") or "Extension officer"
                if norm_lang == "sw":
                    sentences.append({"label": "Ushauri wako", "text": f"Ushauri kutoka kwa afisa kilimo {officer}. {raw_text}"})
                elif norm_lang == "kdj":
                    sentences.append({"label": "Akisil kon", "text": f"Akisil naka ekaunan {officer}. {raw_text}"})
                else:
                    sentences.append({"label": "My advice", "text": f"Advice from extension officer {officer}. {raw_text}"})
            else:
                if norm_lang == "sw":
                    sentences.append({"label": "Ushauri", "text": "Bado huna ushauri mpya kutoka kwa afisa wako. Utaonekana hapa baada ya kukaguliwa."})
                elif norm_lang == "kdj":
                    sentences.append({"label": "Akisil", "text": "Emam akisil na ngitet naka ekaunan sek. Ebuni aanyun na erikuno akisil."})
                else:
                    sentences.append({"label": "Advice", "text": "You have no new approved advice yet. Your officer will send it after checking your farm."})
            full_text = " ".join(s["text"] for s in sentences)
            return {"language": norm_lang, "language_label": lang_name, "view": view, "sentences": sentences, "full_script": full_text}

        # Dashboard View:
        if norm_lang == "sw":
            sentences.append({"label": "Karibu", "text": f"Karibu kwenye shamba lako, {farmer_name}. Shamba limesajiliwa {loc_str}." if loc_str else f"Karibu kwenye shamba lako, {farmer_name}."})
        elif norm_lang == "kdj":
            sentences.append({"label": "Ejok nooi", "text": f"Ejok nooi amana kon, {farmer_name}. Amana na egeritai aloma {loc_str}." if loc_str else f"Ejok nooi amana kon, {farmer_name}."})
        else:
            sentences.append({"label": "Welcome", "text": f"Welcome to your farm dashboard, {farmer_name}. Registered at {loc_str}." if loc_str else f"Welcome to your farm dashboard, {farmer_name}."})

        coordinates = (village.latitude, village.longitude) if village else None
        weather = None
        if coordinates:
            try:
                weather = impl.get_weather(latitude=coordinates[0], longitude=coordinates[1])
            except Exception:
                pass

        latest_obs = session.exec(
            select(Observation)
            .where(Observation.household_id == household.id)
            .order_by(Observation.observed_on.desc())
        ).first()

        past_rain = f"{weather['past_7_days_rain_mm']:.1f}" if weather and isinstance(weather.get("past_7_days_rain_mm"), (int, float)) else None
        next_rain = f"{weather['next_7_days_rain_mm']:.1f}" if weather and isinstance(weather.get("next_7_days_rain_mm"), (int, float)) else None
        soil_raw = (latest_obs.soil_wetness if latest_obs else "") or "not recorded"

        soil_map = {
            "wet": {"en": "Wet", "sw": "Unyevu mwingi", "kdj": "Adelunit nooi"},
            "moist": {"en": "Moist", "sw": "Unyevu mzuri wa kutosha", "kdj": "Edol alupok akipi"},
            "dry": {"en": "Dry", "sw": "Kavu", "kdj": "Ekatung"},
        }
        soil_text = soil_map.get(soil_raw.lower(), {"en": soil_raw, "sw": soil_raw, "kdj": soil_raw})[norm_lang]

        if past_rain and next_rain:
            if norm_lang == "sw":
                sentences.append({
                    "label": "Hali ya hewa",
                    "text": f"Hali ya hewa na udongo. Mvua siku saba zilizopita: milimita {past_rain}. Utabiri wa mvua siku saba zijazo: milimita {next_rain}. Hali ya udongo: {soil_text}."
                })
            elif norm_lang == "kdj":
                sentences.append({
                    "label": "Eboyer loka amana",
                    "text": f"Eboyer loka akiru ka alupok. Akiru napaarisyet 7 naka alotung: milimita {past_rain}. Etebito akiru napaarisyet 7 nukebunit: milimita {next_rain}. Eboyer loka alupok: {soil_text}."
                })
            else:
                sentences.append({
                    "label": "Field conditions",
                    "text": f"Field conditions: Rain in the past 7 days: {past_rain} millimeters. Rain forecast for the next 7 days: {next_rain} millimeters. Latest soil condition: {soil_text}."
                })
        else:
            if norm_lang == "sw":
                sentences.append({"label": "Hali ya udongo", "text": f"Hali ya udongo shambani ni {soil_text}."})
            elif norm_lang == "kdj":
                sentences.append({"label": "Eboyer loka alupok", "text": f"Eboyer loka alupok amana kon erai {soil_text}."})
            else:
                sentences.append({"label": "Soil condition", "text": f"Recorded field soil condition is {soil_text}."})

        if advice_info.get("has_advice"):
            raw_text = advice_info.get("text", "")
            officer = advice_info.get("officer_name") or "extension officer"
            if norm_lang == "sw":
                sentences.append({"label": "Ushauri wa afisa", "text": f"Ushauri kutoka kwa afisa kilimo {officer}: {raw_text}"})
            elif norm_lang == "kdj":
                sentences.append({"label": "Akisil naka ekaunan", "text": f"Akisil naka ekaunan {officer}: {raw_text}"})
            else:
                sentences.append({"label": "Officer advice", "text": f"Advice from extension officer {officer}: {raw_text}"})
        else:
            if norm_lang == "sw":
                sentences.append({"label": "Ushauri", "text": "Bado huna ushauri mpya kutoka kwa afisa wako. Utaonekana hapa baada ya kukaguliwa."})
            elif norm_lang == "kdj":
                sentences.append({"label": "Akisil", "text": "Emam akisil na ngitet naka ekaunan sek. Ebuni aanyun na erikuno akisil."})
            else:
                sentences.append({"label": "Officer advice", "text": "You have no new advice yet. Your extension officer will send advice after review."})

        zone = district.agroclimatic_zone if district else ""
        calendars = session.exec(
            select(CropCalendar).where(CropCalendar.zone == zone).order_by(CropCalendar.crop)
        ).all()
        if calendars:
            crop_names = ", ".join(c.crop.capitalize() for c in calendars)
            if norm_lang == "sw":
                sentences.append({"label": "Msimu wa kupanda", "text": f"Mwongozo wa msimu wa kupanda kwa eneo lako unajumuisha mazao ya {crop_names}."})
            elif norm_lang == "kdj":
                sentences.append({"label": "Esaawa loka arowot", "text": f"Apak naka arowot ngimomwa nuka {zone} erai {crop_names}."})
            else:
                sentences.append({"label": "Planting guidance", "text": f"Planting calendar guidance for your zone covers {crop_names}."})

        if norm_lang == "sw":
            sentences.append({"label": "Huduma", "text": "Unaweza kutumia huduma za ukurasa huu kuripoti tatizo shambani, au kuomba afisa akutembelee."})
        elif norm_lang == "kdj":
            sentences.append({"label": "Aswamisiot", "text": "Ipedori iyon elimun aroniar amana, kori alip ekaunan ebu amana kon."})
        else:
            sentences.append({"label": "Services", "text": "You can use this page to report an issue with your crop, or request an extension officer visit."})

    full_text = " ".join(s["text"] for s in sentences)
    return {
        "language": norm_lang,
        "language_label": lang_name,
        "view": view,
        "sentences": sentences,
        "full_script": full_text,
    }


class VoiceAudioRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    lang: str = "en"


# (lang, text) -> (stored_at, result). Sunbird's signed audio URLs last 30 minutes,
# so a cached one is reused for 25. After that the stored audio is re-signed (~2s)
# instead of generated again (10-30s), so a sentence is only ever generated once.
_VOICE_AUDIO_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_VOICE_AUDIO_TTL_SECONDS = 25 * 60
# One lock per sentence: a page-load warm-up and a Listen press asking for the same
# sentence at once share one Sunbird call instead of paying for two.
_VOICE_AUDIO_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_VOICE_AUDIO_LOCKS_GUARD = threading.Lock()


@router.post("/api/farmer/voice-audio")
def get_farmer_voice_audio(body: VoiceAudioRequest):
    """Speech for one short piece of the voice script, from Sunbird AI's Ugandan TTS
    voices. 503 when Sunbird is not configured or fails; the page then uses the
    browser's own voice instead."""
    lang = _canonical_voice_lang(body.lang)
    key = (lang, body.text.strip())
    with _VOICE_AUDIO_LOCKS_GUARD:
        lock = _VOICE_AUDIO_LOCKS.setdefault(key, threading.Lock())
    with lock:
        now = time.monotonic()
        cached = _VOICE_AUDIO_CACHE.get(key)
        if cached and now - cached[0] < _VOICE_AUDIO_TTL_SECONDS:
            return cached[1]
        result = None
        if cached and cached[1].get("gcs_object"):
            url = model_client.refresh_speech_url(cached[1]["gcs_object"])
            if url:
                result = {**cached[1], "audio_url": url}
        if result is None:
            result = model_client.synthesize_speech(key[1], lang)
        if result is None:
            raise HTTPException(503, "Voice audio is unavailable; use the browser voice.")
        if len(_VOICE_AUDIO_CACHE) > 2000:
            _VOICE_AUDIO_CACHE.clear()
        _VOICE_AUDIO_CACHE[key] = (now, result)
        return result
