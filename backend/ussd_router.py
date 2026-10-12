"""Africa's Talking-compatible callback for the existing SQLite-backed USSD flow."""
from __future__ import annotations

from json import JSONDecodeError
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, ValidationError

from backend.core.database import session_scope
from backend.db.models import Household, UssdSession
from backend.farmer.router import (
    PROBLEM_FIELD_MAP,
    _advice_for_group,
    _household_for_phone,
    _save_followup,
)
from backend.logging.tool_log import run_logged
from backend.tools import agrimoist_tools as impl

router = APIRouter(tags=["USSD Channel"])

AFRICAS_TALKING_PROBLEM_MAP = {"1": "pests", "2": "no_rain", "3": "heavy_rain", "4": "crop_sick"}
CROP_MAP = {"1": "sorghum", "2": "maize", "3": "other"}
VISIT_REASON_MAP = {"1": "Pests", "2": "Little food left", "3": "Other"}
FOLLOWED_MAP = {"1": "yes", "2": "partly", "3": "no"}
HELPED_MAP = {"1": "yes", "2": "no", "3": "too_early"}
NOT_UNDERSTOOD = "END Sorry, that was not understood. Please dial again."


class UssdRequest(BaseModel):
    session_id: str = Field(validation_alias="sessionId", serialization_alias="sessionId")
    service_code: str | None = Field(
        default=None, validation_alias="serviceCode", serialization_alias="serviceCode"
    )
    phone: str = Field(validation_alias="phoneNumber", serialization_alias="phoneNumber")
    text: str = ""

    model_config = {"populate_by_name": True}


def _touch_session(session, session_id: str, phone: str, screen: str) -> None:
    row = session.get(UssdSession, session_id)
    if row is None:
        row = UssdSession(session_id=session_id, phone=phone, last_screen=screen)
    else:
        row.phone = phone
        row.last_screen = screen
    session.add(row)
    session.commit()


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


async def _parse_request(request: Request) -> UssdRequest:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        if content_type == "application/json":
            payload: Any = await request.json()
        elif content_type in ("application/x-www-form-urlencoded", "multipart/form-data"):
            payload = dict(await request.form())
        else:
            raise HTTPException(415, "Expected a form-encoded or JSON USSD callback.")
    except JSONDecodeError as exc:
        raise HTTPException(400, "Invalid JSON request body.") from exc

    try:
        return UssdRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, detail=exc.errors()) from exc


@router.post("/ussd", response_class=PlainTextResponse)
async def ussd_callback(request: Request) -> str:
    body = await _parse_request(request)
    with session_scope() as session:
        household = _household_for_phone(session, body.phone)
        _touch_session(session, body.session_id, body.phone, body.text or "home")
        if household is None:
            return "END This number is not registered. Please ask your extension officer."
        return _handle_ussd(session, household, body.phone, body.text)
