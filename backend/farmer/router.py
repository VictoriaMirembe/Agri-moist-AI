# Everything a farmer touches: reports, visit requests, reading approved advice, and
# the simulated USSD menu. Farmers never chat with the assistant and
# never see the raw advice table - every read here goes through the
# advice_visible_to_farmer database view, which only ever contains approved and
# edited_and_approved rows (see backend/core/database.py).
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
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
    Household,
    Observation,
    Officer,
    Plot,
    PlotHistory,
    UssdSession,
    Village,
)
from backend.logging.tool_log import run_logged
from backend.tools import agrimoist_tools as impl
from backend.tools.errors import ToolError

router = APIRouter(tags=["farmer"])

PROBLEM_FIELD_MAP = {
    "pests": {"pest_seen": "farmer-reported pest"},
    "no_rain": {"rain_last_7_days": "none"},
    "heavy_rain": {"rain_last_7_days": "heavy"},
    "crop_sick": {"crop_condition": "poor"},
}


def _household_for_phone(session, phone: str) -> Household | None:
    household = session.exec(
        select(Household).join(Contact, Contact.household_id == Household.id).where(Contact.phone == phone)
    ).first()
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


def _save_followup(session, advice_id: int, followed: str, helped: str | None, channel: str) -> None:
    followup = session.exec(select(Followup).where(Followup.advice_id == advice_id)).first()
    if followup is None:
        followup = Followup(advice_id=advice_id, due_on=date.today())
    followup.followed = followed
    followup.helped = helped
    followup.answered_via = channel
    session.add(followup)
    session.commit()


def _touch_ussd_session(session, session_id: str, phone: str, screen: str) -> None:
    row = session.get(UssdSession, session_id)
    if row is None:
        row = UssdSession(session_id=session_id, phone=phone, last_screen=screen)
    else:
        row.phone = phone
        row.last_screen = screen
    session.add(row)
    session.commit()


# ---------------------------------------------------------------------------
# Farmer reports and visit requests (web)
# ---------------------------------------------------------------------------
class FarmerReportRequest(BaseModel):
    phone: str
    problem: str  # pests, no_rain, heavy_rain, crop_sick
    crop: str | None = None


@router.post("/api/farmer-reports")
def create_farmer_report(body: FarmerReportRequest):
    if body.problem not in PROBLEM_FIELD_MAP:
        raise HTTPException(400, f"Unknown problem: {body.problem}")
    with session_scope() as session:
        household = _household_for_phone(session, body.phone)
        if household is None:
            raise HTTPException(404, "This number is not registered. Please ask your extension officer.")
        fields = PROBLEM_FIELD_MAP[body.problem]
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
                reason=f"Farmer asked for a visit: {body.reason}",
                raised_by="farmer_web",
            ),
        )


@router.get("/api/farmer/advice")
def farmer_advice(phone: str):
    with session_scope() as session:
        household = _household_for_phone(session, phone)
        if household is None:
            raise HTTPException(404, "This number is not registered. Please ask your extension officer.")
        return _advice_for_group(session, household.group_id)


@router.get("/api/farmer/profile")
def farmer_profile(phone: str):
    with session_scope() as session:
        household = _household_for_phone(session, phone)
        if household is None:
            raise HTTPException(404, "This number is not registered. Please ask your extension officer.")
        contact = session.exec(
            select(Contact).where(Contact.household_id == household.id, Contact.phone == phone)
        ).first()
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

        contact = session.exec(
            select(Contact).where(Contact.household_id == household.id, Contact.phone == phone)
        ).first()
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
            weather_error = str(exc)

    return {
        "profile": profile,
        "latest_observation": observation,
        "weather": weather,
        "weather_error": weather_error,
        "crop_calendar": crop_calendar,
    }


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
        _save_followup(session, body.advice_id, body.followed, body.helped, "farmer_web")
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
# USSD (simulated) - same request/reply shape as Africa's Talking.
# ---------------------------------------------------------------------------
class UssdRequest(BaseModel):
    session_id: str = Field(validation_alias="sessionId", serialization_alias="sessionId")
    phone: str = Field(validation_alias="phoneNumber", serialization_alias="phoneNumber")
    text: str = ""

    model_config = {"populate_by_name": True}


AFRICAS_TALKING_PROBLEM_MAP = {"1": "pests", "2": "no_rain", "3": "heavy_rain", "4": "crop_sick"}
CROP_MAP = {"1": "sorghum", "2": "maize", "3": "other"}
VISIT_REASON_MAP = {"1": "Pests", "2": "Little food left", "3": "Other"}
FOLLOWED_MAP = {"1": "yes", "2": "partly", "3": "no"}
HELPED_MAP = {"1": "yes", "2": "no", "3": "too_early"}

NOT_UNDERSTOOD = "END Sorry, that was not understood. Please dial again."


@router.post("/ussd", response_class=PlainTextResponse)
def ussd(body: UssdRequest):
    with session_scope() as session:
        household = _household_for_phone(session, body.phone)
        _touch_ussd_session(session, body.session_id, body.phone, body.text or "home")
        if household is None:
            return "END This number is not registered. Please ask your extension officer."
        return _handle_ussd(session, household, body.phone, body.text)


def _handle_ussd(session, household: Household, phone: str, raw_text: str) -> str:
    steps = raw_text.split("*") if raw_text else []

    if not steps:
        return (
            "CON AgriMoist\n1. My advice\n2. Report a problem\n3. Ask for officer visit\n4. Did advice help?"
        )

    top = steps[0]

    if top == "1":
        info = _advice_for_group(session, household.group_id)
        if not info.get("has_advice"):
            return "END No new advice yet. Your officer will send it after checking."
        officer_bit = f" Approved by {info['officer_name']}," if info.get("officer_name") else ""
        return f"END {info['text'][:140]}.{officer_bit} {info['date'][:10]}."

    if top == "2":
        if len(steps) == 1:
            return "CON What did you see?\n1. Pests  2. No rain  3. Too much rain  4. Crop looks sick"
        if len(steps) == 2:
            if steps[1] not in AFRICAS_TALKING_PROBLEM_MAP:
                return NOT_UNDERSTOOD
            return "CON Which crop?\n1. Sorghum  2. Maize  3. Other"
        if len(steps) == 3:
            problem = AFRICAS_TALKING_PROBLEM_MAP.get(steps[1])
            if problem is None or steps[2] not in CROP_MAP:
                return NOT_UNDERSTOOD
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
            return "END Thank you. Your officer will check this report."
        return NOT_UNDERSTOOD

    if top == "3":
        if len(steps) == 1:
            return "CON Why?\n1. Pests  2. Little food left  3. Other"
        if len(steps) == 2:
            reason = VISIT_REASON_MAP.get(steps[1])
            if reason is None:
                return NOT_UNDERSTOOD
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
                    reason=f"Farmer asked for a visit: {reason}",
                    raised_by="farmer_ussd",
                ),
            )
            return "END Request sent. Your officer will plan a visit."
        return NOT_UNDERSTOOD

    if top == "4":
        if len(steps) == 1:
            return "CON Did you follow the last advice?\n1. Yes  2. Partly  3. No"
        if len(steps) == 2:
            if steps[1] not in FOLLOWED_MAP:
                return NOT_UNDERSTOOD
            return "CON Did it help?\n1. Yes  2. No  3. Too early to say"
        if len(steps) == 3:
            followed = FOLLOWED_MAP.get(steps[1])
            helped = HELPED_MAP.get(steps[2])
            if followed is None or helped is None:
                return NOT_UNDERSTOOD
            info = _advice_for_group(session, household.group_id)
            if info.get("has_advice"):
                _save_followup(session, info["advice_id"], followed, helped, "farmer_ussd")
            return "END Thank you. This helps improve advice."
        return NOT_UNDERSTOOD

    return NOT_UNDERSTOOD
