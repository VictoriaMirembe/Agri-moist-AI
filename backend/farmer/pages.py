# Farmer-facing HTML: sign in, home, my advice, report a problem, ask for a visit,
# did advice help, plus the USSD simulator page. Simple words, large buttons, low
# data. All of these pages call the JSON API in backend/farmer/router.py.
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from backend.core.config import settings
from backend.core.database import session_scope
from backend.db.models import Contact, Household

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "farmer" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["farmer-pages"])


@router.get("/farmer")
def sign_in(request: Request):
    return templates.TemplateResponse(request, "sign_in.html", {})


@router.get("/farmer/home")
def farmer_home(request: Request, phone: str = ""):
    return _farmer_page(request, phone, "dashboard")


@router.get("/farmer/advice")
def farmer_advice_page(request: Request, phone: str = ""):
    return _farmer_page(request, phone, "advice")


@router.get("/farmer/report")
def farmer_report_page(request: Request, phone: str = ""):
    return _farmer_page(request, phone, "report")


@router.get("/farmer/visit")
def farmer_visit_page(request: Request, phone: str = ""):
    return _farmer_page(request, phone, "visit")


@router.get("/farmer/followup")
def farmer_followup_page(request: Request, phone: str = ""):
    return _farmer_page(request, phone, "followup")


def _farmer_page(request: Request, phone: str, active_view: str):
    return templates.TemplateResponse(
        request, "home.html", {"phone": phone, "active_view": active_view}
    )


@router.get("/ussd")
@router.get("/farmer/ussd")
def ussd_simulator(request: Request, phone: str = ""):
    # The registered (made-up demo) numbers, so the simulator can pick one instead of
    # guessing - an unregistered number gets "not registered", as on a real phone.
    with session_scope() as session:
        rows = session.exec(
            select(Contact, Household).join(Household, Contact.household_id == Household.id).order_by(Household.code)
        ).all()
        phones = [
            {"phone": c.phone, "name": c.name, "household_code": h.code, "group_id": h.group_id} for c, h in rows
        ]
    return templates.TemplateResponse(
        request,
        "ussd.html",
        {"service_code": settings.ussd_service_code, "phone": phone or (phones[0]["phone"] if phones else ""), "phones": phones},
    )
