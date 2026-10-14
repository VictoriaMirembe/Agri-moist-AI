# Officer-facing HTML screens. Server-rendered Jinja2 for the
# first paint; the small amount of interactivity (saving a visit, starting a draft,
# approving, marking things checked/visited, watching a run live) is plain fetch()
# calls in frontend/static/app.js against the JSON API in backend/officer/router.py,
# backend/approvals/router.py and backend/farmer/router.py.
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from backend.core.database import session_scope
from backend.db.models import (
    Advice,
    District,
    Household,
    Observation,
    Officer,
    Run,
    ToolLog,
    Village,
    VisitFlag,
)
from backend.officer.router import fetch_village_weather

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "officer" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["officer-pages"])


@router.get("/")
def home(request: Request):
    user_role = request.session.get("role") if "session" in request.scope else None
    if not user_role:
        user_role = request.cookies.get("officer_role") or request.cookies.get("role")

    with session_scope() as session:
        villages = session.exec(select(Village)).all()
        if not villages:
            villages = [
                Village(name="Lokopo", latitude=2.3833, longitude=34.3333),
                Village(name="Iriiri", latitude=2.5333, longitude=34.3500),
            ]
        weather_list = [fetch_village_weather(v.name, v.latitude, v.longitude) for v in villages]

        pending_advices = session.exec(
            select(Advice)
            .where(Advice.status == "pending_review")
            .order_by(Advice.created_at.desc())
        ).all()

        group_ids = sorted({g for g in session.exec(select(Household.group_id)).all()})
        groups = []
        for group_id in group_ids:
            households = session.exec(select(Household).where(Household.group_id == group_id)).all()
            village = session.get(Village, households[0].village_id) if households else None
            open_flags = len(
                [
                    f
                    for f in session.exec(
                        select(VisitFlag).where(VisitFlag.group_id == group_id, VisitFlag.status == "open")
                    ).all()
                ]
            )
            drafts_waiting = len(
                [
                    a
                    for a in session.exec(
                        select(Advice).where(Advice.group_id == group_id, Advice.status == "pending_review")
                    ).all()
                ]
            )
            groups.append(
                {
                    "group_id": group_id,
                    "village": village.name if village else "-",
                    "household_count": len(households),
                    "open_flags": open_flags,
                    "drafts_waiting": drafts_waiting,
                }
            )
        unchecked_reports = len(
            session.exec(
                select(Observation).where(
                    Observation.source.in_(["farmer_ussd", "farmer_web"]), Observation.checked == "unchecked"
                )
            ).all()
        )
        open_flags_total = len(session.exec(select(VisitFlag).where(VisitFlag.status == "open")).all())
        drafts_total = len(pending_advices)
        officers = session.exec(select(Officer)).all()

    officer_name = request.session.get("officer_name") if "session" in request.scope else None

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "groups": groups,
            "unchecked_reports": unchecked_reports,
            "open_flags_total": open_flags_total,
            "drafts_total": drafts_total,
            "weather_list": weather_list,
            "pending_advices": pending_advices,
            "officers": officers,
            "officer_name": officer_name,
            "user_role": user_role,
        },
    )


@router.get("/visits/new")
def visit_form(request: Request):
    with session_scope() as session:
        households = session.exec(select(Household).order_by(Household.code)).all()
        officers = session.exec(select(Officer)).all()
    return templates.TemplateResponse(
        request, "visit_form.html", {"households": households, "officers": officers}
    )


@router.get("/groups/{group_id}")
def group_detail_page(request: Request, group_id: int):
    with session_scope() as session:
        households = session.exec(select(Household).where(Household.group_id == group_id)).all()
        village = session.get(Village, households[0].village_id) if households else None
        district = session.get(District, village.district_id) if village else None
        advices = session.exec(
            select(Advice).where(Advice.group_id == group_id).order_by(Advice.created_at.desc())
        ).all()
        flags = session.exec(
            select(VisitFlag).where(VisitFlag.group_id == group_id).order_by(VisitFlag.created_at.desc())
        ).all()
    return templates.TemplateResponse(
        request,
        "group_detail.html",
        {
            "group_id": group_id,
            "households": households,
            "village": village,
            "zone": district.agroclimatic_zone if district else None,
            "advices": advices,
            "flags": flags,
        },
    )


@router.get("/runs/{run_id}")
def run_page(request: Request, run_id: int):
    with session_scope() as session:
        run = session.get(Run, run_id)
    return templates.TemplateResponse(request, "run.html", {"run": run, "run_id": run_id})


@router.get("/approvals")
def approvals_page(request: Request):
    with session_scope() as session:
        officers = session.exec(select(Officer)).all()
    return templates.TemplateResponse(request, "approvals.html", {"officers": officers})


@router.get("/farmer-reports")
def farmer_reports_page(request: Request):
    with session_scope() as session:
        officers = session.exec(select(Officer)).all()
    return templates.TemplateResponse(request, "farmer_reports.html", {"officers": officers})


@router.get("/followups")
def followups_page(request: Request):
    user_role = request.session.get("role") if "session" in request.scope else None
    if not user_role:
        user_role = request.cookies.get("officer_role") or request.cookies.get("role")
    if user_role != "officer":
        raise HTTPException(status_code=403, detail="Access denied: Officers only.")
    return templates.TemplateResponse(request, "followups.html", {})


@router.get("/officer/login")
def officer_login_page(request: Request, next: str = "/"):
    with session_scope() as session:
        officers = session.exec(select(Officer)).all()
    return templates.TemplateResponse(request, "officer_login.html", {"officers": officers, "next": next})


@router.post("/officer/login")
async def officer_login_submit(request: Request):
    form_data = await request.form()
    try:
        officer_id = int(form_data.get("officer_id", 1))
    except (ValueError, TypeError):
        officer_id = 1
    next_url = form_data.get("next", "/")
    with session_scope() as session:
        officer = session.get(Officer, officer_id)
        officer_name = officer.name if officer else "Extension Officer"

    if "session" in request.scope:
        request.session["role"] = "officer"
        request.session["officer_id"] = officer_id
        request.session["officer_name"] = officer_name

    resp = RedirectResponse(url=next_url, status_code=303)
    resp.set_cookie(key="officer_role", value="officer", max_age=86400 * 7, httponly=False, samesite="lax")
    return resp


@router.get("/officer/logout")
def officer_logout_page(request: Request):
    if "session" in request.scope:
        request.session.clear()
    resp = RedirectResponse(url="/officer/login", status_code=303)
    resp.delete_cookie("officer_role")
    resp.delete_cookie("agrimoist_session")
    return resp


@router.get("/flags")
def flags_page(request: Request):
    return templates.TemplateResponse(request, "flags.html", {})


@router.get("/log")
def log_page(request: Request, run_id: int | None = None):
    with session_scope() as session:
        query = select(ToolLog).order_by(ToolLog.at.desc())
        if run_id:
            query = query.where(ToolLog.run_id == run_id)
        rows = session.exec(query.limit(300)).all()
        runs = session.exec(select(Run).order_by(Run.started_at.desc()).limit(50)).all()
    return templates.TemplateResponse(
        request, "log.html", {"rows": rows, "runs": runs, "selected_run_id": run_id}
    )
