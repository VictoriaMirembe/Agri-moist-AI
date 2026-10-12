# Everything a farmer touches: reports, visit requests, reading approved advice, and
# the simulated USSD menu (USSD_MENU.md). Farmers never chat with the assistant and
# never see the raw advice table - every read here goes through the
# advice_visible_to_farmer database view, which only ever contains approved and
# edited_and_approved rows (see backend/core/database.py).
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import select

from backend.core.config import settings
from backend.core.database import session_scope
from backend.db.models import Advice, Approval, Contact, Followup, Household, Officer
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
        return {"household_code": household.code, "group_id": household.group_id}


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
