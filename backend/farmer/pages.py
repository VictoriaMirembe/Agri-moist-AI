# Farmer-facing HTML: sign in, home, my advice, report a problem, ask for a visit,
# did advice help, plus the USSD simulator page. Simple words, large buttons, low
# data. All of these pages call the JSON API in backend/farmer/router.py.
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from backend.core.config import settings
from backend.core.database import session_scope
from backend.db.models import Contact, Household, Officer
from backend.officer.auth import (
    check_farmer_login,
    require_farmer_page,
    require_officer_page,
    sign_in_farmer,
    signed_in_farmer,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "farmer" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["farmer-pages"])


def _safe_next(next_url: str | None) -> str:
    ok = next_url and next_url.startswith("/farmer") and not next_url.startswith("//")
    return next_url if ok and not next_url.startswith("/farmer/login") else "/farmer/home"


def _login_page(request: Request, next_url: str = "", error: str | None = None, phone: str = "", status_code: int = 200):
    return templates.TemplateResponse(
        request,
        "sign_in.html",
        {"next": _safe_next(next_url), "error": error, "phone": phone, "service_code": settings.ussd_service_code},
        status_code=status_code,
    )


@router.get("/farmer")
@router.get("/farmer/login")
def sign_in(request: Request, next: str = ""):
    if signed_in_farmer(request):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    return _login_page(request, next)


@router.post("/farmer/login")
async def sign_in_submit(request: Request):
    form = await request.form()
    phone = str(form.get("phone", "")).strip()
    next_url = str(form.get("next", ""))
    if not phone:
        return _login_page(request, next_url, "Enter your phone number.", phone, 400)
    with session_scope() as session:
        farmer, error = check_farmer_login(session, phone=phone, pin=str(form.get("pin", "")).strip())
    if farmer is None:
        return _login_page(request, next_url, error, phone, 401)
    sign_in_farmer(request, farmer)
    return RedirectResponse(url=_safe_next(next_url), status_code=303)


@router.get("/farmer/logout")
def sign_out(request: Request):
    request.session.clear()
    return RedirectResponse(url="/farmer/login", status_code=303)


@router.get("/farmer/home")
def farmer_home(request: Request, farmer: dict = Depends(require_farmer_page)):
    return _farmer_page(request, farmer, "dashboard")


@router.get("/farmer/advice")
def farmer_advice_page(request: Request, farmer: dict = Depends(require_farmer_page)):
    return _farmer_page(request, farmer, "advice")


@router.get("/farmer/report")
def farmer_report_page(request: Request, farmer: dict = Depends(require_farmer_page)):
    return _farmer_page(request, farmer, "report")


@router.get("/farmer/visit")
def farmer_visit_page(request: Request, farmer: dict = Depends(require_farmer_page)):
    return _farmer_page(request, farmer, "visit")


@router.get("/farmer/followup")
def farmer_followup_page(request: Request, farmer: dict = Depends(require_farmer_page)):
    return _farmer_page(request, farmer, "followup")


def _farmer_page(request: Request, farmer: dict, active_view: str):
    # The phone comes from the signed session, never from the address bar.
    return templates.TemplateResponse(
        request, "home.html", {"phone": farmer["phone"], "active_view": active_view}
    )


@router.get("/ussd")
@router.get("/farmer/ussd")
def ussd_simulator(request: Request, phone: str = "", officer: dict = Depends(require_officer_page)):
    # The registered numbers, so the simulator can pick one instead of guessing - an
    # unregistered number gets "not registered", as on a real phone. These are real
    # farmers' numbers, so the simulator is for signed-in officers only.
    with session_scope() as session:
        rows = session.exec(
            select(Contact, Household).join(Household, Contact.household_id == Household.id).order_by(Household.code)
        ).all()
        phones = [
            {"phone": c.phone, "name": c.name, "household_code": h.code, "group_id": h.group_id} for c, h in rows
        ]
        officers = [{"id": o.id, "name": o.name} for o in session.exec(select(Officer).order_by(Officer.id)).all()]
    return templates.TemplateResponse(
        request,
        "ussd.html",
        {
            "service_code": settings.ussd_service_code,
            "officer_service_code": settings.ussd_officer_service_code,
            "officers": officers,
            "phone": phone or (phones[0]["phone"] if phones else ""),
            "phones": phones,
        },
    )
