# Officer-facing HTML screens (DESIGN.md screens 1-8). Server-rendered Jinja2 for the
# first paint; the small amount of interactivity (saving a visit, starting a draft,
# approving, marking things checked/visited, watching a run live) is plain fetch()
# calls in frontend/static/app.js against the JSON API in backend/officer/router.py,
# backend/approvals/router.py and backend/farmer/router.py.
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
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

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "officer" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["officer-pages"])


@router.get("/")
def home(request: Request):
    with session_scope() as session:
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
        drafts_total = len(session.exec(select(Advice).where(Advice.status == "pending_review")).all())

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "groups": groups,
            "unchecked_reports": unchecked_reports,
            "open_flags_total": open_flags_total,
            "drafts_total": drafts_total,
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
    return templates.TemplateResponse(request, "followups.html", {})


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
