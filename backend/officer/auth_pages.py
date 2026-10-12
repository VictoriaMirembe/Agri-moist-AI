"""Public officer sign-in page."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "officer" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["officer-auth"])


@router.get("/officer/login")
def officer_login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})
