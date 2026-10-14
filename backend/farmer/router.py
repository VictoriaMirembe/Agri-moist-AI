# Everything a farmer touches: reports, visit requests, reading approved advice, and
# the USSD menu (Africa's Talking callback). Farmers never chat with the assistant and
# never see the raw advice table - every read here goes through the
# advice_visible_to_farmer database view, which only ever contains approved and
# edited_and_approved rows (see backend/core/database.py).
from __future__ import annotations

<<<<<<< Updated upstream
import re
from datetime import date, datetime, timezone
=======
import hmac
import logging
from datetime import date
>>>>>>> Stashed changes
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
<<<<<<< Updated upstream
from pydantic import BaseModel, Field, field_validator
=======
from pydantic import BaseModel, Field, ValidationError
>>>>>>> Stashed changes
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
    VisitFlag,
    Village,
    utcnow,
)
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
            "summary": "Visit request: " + visit.reason.removeprefix("Farmer asked for a visit: "),
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
<<<<<<< Updated upstream
NO_ADVICE_TO_ANSWER = "END No advice to answer yet. Your officer will send advice after checking."
=======
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
>>>>>>> Stashed changes


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
<<<<<<< Updated upstream
            return "END No new advice yet. Your officer will send it after checking."
        _log_advice_viewed(session, household, info, "farmer_ussd")
=======
            return "END No new advice yet. Your officer will send it after checking.", False
>>>>>>> Stashed changes
        officer_bit = f" Approved by {info['officer_name']}," if info.get("officer_name") else ""
        return f"END {info['text'][:140]}.{officer_bit} {info['date'][:10]}.", False

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
<<<<<<< Updated upstream
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
            return "END Request sent. Your officer will plan a visit."
        return NOT_UNDERSTOOD
=======
                group_id=household.group_id,
                reason=f"Farmer asked for a visit: {reason}",
                raised_by="farmer_ussd",
            ),
        )
        return "END Request sent. Your officer will plan a visit.", True
>>>>>>> Stashed changes

    if top == "4":
        info = _advice_for_group(session, household.group_id)
        if not info.get("has_advice"):
<<<<<<< Updated upstream
            # Nothing approved to answer about: say so, and save nothing.
            return NO_ADVICE_TO_ANSWER
=======
            return "END You have no approved advice to answer about yet.", False
>>>>>>> Stashed changes
        if len(steps) == 1:
            return f"CON {retry}Did you follow the last advice?\n1. Yes  2. Partly  3. No" + NAV_HINT, False
        if len(steps) == 2:
<<<<<<< Updated upstream
            if steps[1] not in FOLLOWED_MAP:
                return NOT_UNDERSTOOD
            return "CON Did it help?\n1. Yes  2. No  3. Too early to say"
        if len(steps) == 3:
            followed = FOLLOWED_MAP.get(steps[1])
            helped = HELPED_MAP.get(steps[2])
            if followed is None or helped is None:
                return NOT_UNDERSTOOD
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
            return "END Thank you. This helps improve advice."
        return NOT_UNDERSTOOD
=======
            return f"CON {retry}Did it help?\n1. Yes  2. No  3. Too early to say" + NAV_HINT, False
        _save_followup(session, info["advice_id"], FOLLOWED_MAP[steps[1]], HELPED_MAP[steps[2]], "farmer_ussd")
        return "END Thank you. This helps improve advice.", True
>>>>>>> Stashed changes

    return NOT_UNDERSTOOD, False
