# Farmer-facing HTML: sign in, home, my advice, report a problem, ask for a visit,
# did advice help, plus the USSD simulator page. Simple words, large buttons, low
# data (DESIGN.md). All of these pages call the JSON API in backend/farmer/router.py.
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "farmer" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["farmer-pages"])


@router.get("/farmer")
def sign_in(request: Request):
    return templates.TemplateResponse(request, "sign_in.html", {})


@router.get("/farmer/home")
def farmer_home(request: Request, phone: str = ""):
    return templates.TemplateResponse(request, "home.html", {"phone": phone})


@router.get("/farmer/advice")
def farmer_advice_page(request: Request, phone: str = ""):
    return templates.TemplateResponse(request, "advice.html", {"phone": phone})


@router.get("/farmer/report")
def farmer_report_page(request: Request, phone: str = ""):
    return templates.TemplateResponse(request, "report.html", {"phone": phone})


@router.get("/farmer/visit")
def farmer_visit_page(request: Request, phone: str = ""):
    return templates.TemplateResponse(request, "visit.html", {"phone": phone})


@router.get("/farmer/followup")
def farmer_followup_page(request: Request, phone: str = ""):
    return templates.TemplateResponse(request, "followup.html", {"phone": phone})


@router.get("/ussd")
def ussd_simulator(request: Request):
    return templates.TemplateResponse(request, "ussd.html", {})
