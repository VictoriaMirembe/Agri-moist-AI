# Officer-facing HTML screens. Server-rendered Jinja2 for the
# first paint; the small amount of interactivity (saving a visit, starting a draft,
# approving, marking things checked/visited, watching a run live) is plain fetch()
# calls in frontend/static/app.js against the JSON API in backend/officer/router.py,
# backend/approvals/router.py and backend/farmer/router.py.
#
# Also holds the plain-word labels officers read on screen (status names, channels,
# Kampala times) and the Home screen's "This week at a glance" numbers. Those numbers
# are read from existing tables and the log only - nothing here writes data.
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from backend.core.config import settings
from backend.core.database import session_scope
from backend.db.models import (
    Advice,
    Contact,
    District,
    FollowupAnswer,
    Household,
    Observation,
    Officer,
    Run,
    ToolLog,
    Village,
    VisitFlag,
)
from backend.officer.auth import (
    password_ok,
    require_officer,
    require_officer_page,
    sign_in,
    signed_in_officer,
    signed_in_officer_id,
)
from backend.officer.router import fetch_village_weather
from backend.tools import agrimoist_tools as impl

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "officer" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Every officer screen sends a visitor who is not signed in to /officer/login first.
router = APIRouter(tags=["officer-pages"], dependencies=[Depends(require_officer_page)])
# The JSON behind those screens answers 403 instead of redirecting.
api_router = APIRouter(tags=["officer-pages"], dependencies=[Depends(require_officer)])
# Sign in and sign out need no session.
public_router = APIRouter(tags=["officer-auth"])

# ---------------------------------------------------------------------------
# Plain words for officers. The stored values never change; only what is shown.
# ---------------------------------------------------------------------------
KAMPALA = timezone(timedelta(hours=3))  # East Africa Time, no daylight saving

STATUS_WORDS = {
    "pending_review": "Waiting for approval",
    "approved": "Approved",
    "edited_and_approved": "Approved with edits",
    "rejected": "Rejected",
    "blocked": "Blocked: a fact has no source",
    "drafted": "Draft ready for approval",
    "flagged": "Sent for a visit instead",
    "failed": "Did not finish",
    "unchecked": "New",
    "checked": "Checked",
    "open": "Visit needed",
    "visited": "Visited",
    "closed": "Closed",
}
CHANNEL_WORDS = {
    "officer_visit": "Officer visit",
    "farmer_ussd": "Farmer by phone (USSD)",
    "farmer_web": "Farmer web page",
    "assistant": "AI assistant",
    "assistant-flag": "AI assistant",
    "assistant-followup": "AI assistant",
    "officer": "Officer",
    "mcp-server-time": "Time server (borrowed MCP)",
    "seed-data": "Demo data",
}
TOOL_WORDS = {
    "record_observation": "Saved a visit or farmer report",
    "get_field_records": "Read recent visits and reports",
    "get_plot_history": "Read plot history",
    "get_crop_calendar": "Read the crop calendar",
    "get_weather": "Read the weather forecast",
    "get_current_time": "Read today's date",
    "get_pest_reports": "Read pest reports",
    "draft_advice": "Wrote an advice draft",
    "flag_for_visit": "Asked for an officer visit",
    "schedule_followup": "Booked a follow-up date",
    "approve_advice": "Approved advice",
    "edit_and_approve_advice": "Edited and approved advice",
    "reject_advice": "Rejected advice",
    "mark_report_checked": "Checked a farmer message",
    "mark_flag_visited": "Marked a visit done",
    "record_followup": "Farmer answered \"did it help?\"",
    "farmer_viewed_advice": "Farmer read approved advice",
}


def _aware(value: datetime) -> datetime:
    # SQLite gives back naive datetimes; they were stored in UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def when(value) -> str:
    """'12 Oct, 11:01' in Kampala time; dates alone as '12 Oct'."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        local = _aware(value).astimezone(KAMPALA)
        return f"{local.day} {local:%b}, {local:%H:%M}"
    if isinstance(value, date):
        return f"{value.day} {value:%b}"
    return str(value)


def plain_status(value: str | None) -> str:
    return STATUS_WORDS.get(value or "", (value or "").replace("_", " ").capitalize())


def plain_channel(value: str | None) -> str:
    return CHANNEL_WORDS.get(value or "", (value or "-").replace("_", " "))


def plain_tool(value: str | None) -> str:
    return TOOL_WORDS.get(value or "", (value or "").replace("_", " ").capitalize())


def status_tone(value: str | None) -> str:
    if value in ("approved", "edited_and_approved", "checked", "visited", "drafted"):
        return "green"
    if value in ("blocked", "rejected", "failed", "open"):
        return "red"
    return "amber"


templates.env.filters.update(
    when=when, plain_status=plain_status, plain_channel=plain_channel, plain_tool=plain_tool, tone=status_tone
)


def _officer_names(session) -> dict[str, str]:
    return {str(o.id): o.name for o in session.exec(select(Officer)).all()}


def _is_demo_data(session) -> bool:
    """True while every registered phone is one of the made-up seed numbers."""
    phones = [c.phone for c in session.exec(select(Contact)).all()]
    return bool(phones) and all(re.fullmatch(r"\+2567000000\d\d", p or "") for p in phones)


# ---------------------------------------------------------------------------
# "This week at a glance" - last 7 days, from existing tables and the log
# ---------------------------------------------------------------------------
def week_at_a_glance(session, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=7)

    def recent(value: datetime | None) -> bool:
        return value is not None and _aware(value) >= since

    observations = [o for o in session.exec(select(Observation)).all() if recent(o.observed_on)]
    visits = [o for o in observations if o.source == "officer_visit"]
    messages = [o for o in observations if o.source in ("farmer_ussd", "farmer_web")]

    advices = [a for a in session.exec(select(Advice)).all() if recent(a.created_at)]
    drafts = {status: sum(1 for a in advices if a.status == status) for status in
              ("approved", "edited_and_approved", "rejected", "pending_review", "blocked")}

    farmer_flags = [
        f for f in session.exec(select(VisitFlag)).all()
        if recent(f.created_at) and f.raised_by in ("farmer_ussd", "farmer_web")
    ]

    answers = [a for a in session.exec(select(FollowupAnswer)).all() if recent(a.answered_at)]
    helped = {key: sum(1 for a in answers if a.helped == key) for key in ("yes", "no", "too_early")}

    reads = [
        r for r in session.exec(select(ToolLog).where(ToolLog.tool == "farmer_viewed_advice")).all()
        if recent(r.at) and r.error is None
    ]

    runs = [r for r in session.exec(select(Run)).all() if recent(r.started_at)]
    ai_cost = sum(r.cost or 0.0 for r in runs)  # drafting + translation (backend/approvals)
    decisions = session.exec(
        select(ToolLog).where(ToolLog.tool.in_(["approve_advice", "edit_and_approve_advice"]))
    ).all()
    translation_cost = sum(
        json.loads(r.outputs or "{}").get("translation_cost") or 0.0 for r in decisions if recent(r.at)
    )
    drafted_runs = sum(1 for r in runs if r.outcome == "drafted")

    # Same rule the agent uses to flag a group (backend/agent/graph.py check_urgent):
    # any household with no visit or report in over 30 days, or never.
    overdue_groups = 0
    group_ids = sorted({g for g in session.exec(select(Household.group_id)).all()})
    for group_id in group_ids:
        days = impl.get_field_records(session, group_id=group_id).get("days_since_last_visit_by_household", {})
        if any(d is None or d > 30 for d in days.values()):
            overdue_groups += 1

    return {
        "since": since,
        "field_visits": len(visits),
        "drafts_total": len(advices),
        "drafts": drafts,
        "messages_total": len(messages),
        "messages_unchecked": sum(1 for o in messages if o.checked == "unchecked"),
        "visit_requests_total": len(farmer_flags),
        "visit_requests_open": sum(1 for f in farmer_flags if f.status == "open"),
        "visit_requests_visited": sum(1 for f in farmer_flags if f.status == "visited"),
        "answers_total": len(answers),
        "helped": helped,
        "reads_total": len(reads),
        "reads_web": sum(1 for r in reads if r.source == "farmer_web"),
        "reads_ussd": sum(1 for r in reads if r.source == "farmer_ussd"),
        "ai_cost": round(ai_cost, 6),
        "ai_cost_translation": round(translation_cost, 6),
        "ai_cost_per_draft": (ai_cost / drafted_runs) if drafted_runs else 0.0,
        "overdue_groups": overdue_groups,
        "groups_total": len(group_ids),
    }


@router.get("/")
def home(request: Request):
    officer = signed_in_officer(request)

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
        week = week_at_a_glance(session)
        demo = _is_demo_data(session)

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
            "officer_name": officer["officer_name"],
            "user_role": "officer",
            "week": week,
            "demo": demo,
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
    signed_in = signed_in_officer_id(request) is not None
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
            "signed_in": signed_in,  # drafting needs a signed-in officer
        },
    )


@router.get("/runs/{run_id}")
def run_page(request: Request, run_id: int):
    with session_scope() as session:
        run = session.get(Run, run_id)
    return templates.TemplateResponse(request, "run.html", {"run": run, "run_id": run_id})


def _observation_labels(session) -> dict[str, dict]:
    """For showing sources in plain words: each observation's household, channel and
    Kampala date, keyed by the source reference the drafts cite ("observation:9")."""
    households = {h.id: h.code for h in session.exec(select(Household)).all()}
    return {
        f"observation:{o.id}": {
            "household": households.get(o.household_id, ""),
            "channel": o.source,
            "date": when(o.observed_on).split(",")[0],
        }
        for o in session.exec(select(Observation)).all()
    }


def _group_villages(session) -> dict[int, str]:
    villages = {v.id: v.name for v in session.exec(select(Village)).all()}
    out: dict[int, str] = {}
    for h in session.exec(select(Household)).all():
        out.setdefault(h.group_id, villages.get(h.village_id, ""))
    return out


@router.get("/approvals")
def approvals_page(request: Request):
    # The approval API needs a signed-in officer, so a signed-out visitor is sent to
    # sign in first rather than shown an empty or broken list.
    officer_id = signed_in_officer_id(request)
    if officer_id is None:
        return RedirectResponse(url="/officer/login?next=/approvals", status_code=303)
    with session_scope() as session:
        observation_labels = _observation_labels(session)
        group_villages = _group_villages(session)
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "officer_id": officer_id,
            "officer_name": request.session.get("officer_name"),
            "observation_labels": observation_labels,
            "group_villages": group_villages,
        },
    )


@router.get("/farmer-reports")
def farmer_reports_page(request: Request):
    with session_scope() as session:
        officers = session.exec(select(Officer)).all()
        group_villages = _group_villages(session)
    return templates.TemplateResponse(
        request, "farmer_reports.html", {"officers": officers, "group_villages": group_villages}
    )


@router.get("/followups")
def followups_page(request: Request):
    with session_scope() as session:
        group_villages = _group_villages(session)
    return templates.TemplateResponse(request, "followups.html", {"group_villages": group_villages})


def _safe_next(next_url: str | None) -> str:
    # Only send people back to a page on this site, never to another host.
    return next_url if next_url and next_url.startswith("/") and not next_url.startswith("//") else "/"


def _login_page(request: Request, next_url: str, error: str | None = None, officer_id: int = 1, status_code: int = 200):
    with session_scope() as session:
        officers = session.exec(select(Officer)).all()
    return templates.TemplateResponse(
        request,
        "officer_login.html",
        {
            "officers": officers,
            "next": _safe_next(next_url),
            "error": error,
            "selected_id": officer_id,
            "demo_password": settings.officer_demo_password,
        },
        status_code=status_code,
    )


@public_router.get("/officer/login")
def officer_login_page(request: Request, next: str = "/"):
    if signed_in_officer(request):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    return _login_page(request, next)


@public_router.post("/officer/login")
async def officer_login_submit(request: Request):
    form_data = await request.form()
    try:
        officer_id = int(form_data.get("officer_id"))
    except (ValueError, TypeError):
        officer_id = 1
    next_url = str(form_data.get("next", "/"))
    if not password_ok(str(form_data.get("password", ""))):
        return _login_page(request, next_url, "That password is not right. Try again.", officer_id, 401)
    with session_scope() as session:
        officer = session.get(Officer, officer_id)
        if officer is None:
            return _login_page(request, next_url, "Choose an officer from the list.", 1, 400)
        officer_name = officer.name
    sign_in(request, officer_id, officer_name)
    return RedirectResponse(url=_safe_next(next_url), status_code=303)


@public_router.get("/officer/logout")
def officer_logout_page(request: Request):
    request.session.clear()
    return RedirectResponse(url="/officer/login", status_code=303)


@router.get("/flags")
def flags_page(request: Request):
    with session_scope() as session:
        officers = session.exec(select(Officer)).all()
        group_villages = _group_villages(session)
    return templates.TemplateResponse(
        request, "flags.html", {"officers": officers, "group_villages": group_villages}
    )


@router.get("/log")
def log_page(request: Request, run_id: int | None = None):
    with session_scope() as session:
        query = select(ToolLog).order_by(ToolLog.at.desc())
        if run_id:
            query = query.where(ToolLog.run_id == run_id)
        rows = session.exec(query.limit(300)).all()
        runs = session.exec(select(Run).order_by(Run.started_at.desc()).limit(50)).all()
        officer_names = _officer_names(session)
    return templates.TemplateResponse(
        request,
        "log.html",
        {"rows": rows, "runs": runs, "selected_run_id": run_id, "officer_names": officer_names},
    )


# ---------------------------------------------------------------------------
# AI tools (MCP): proof that the assistant's tools are real MCP servers. Each load
# starts both servers over stdio, exactly as an agent run does, and lists what they
# report over the protocol - nothing on this screen is hard-coded.
# ---------------------------------------------------------------------------
MCP_WRITE_TOOLS = {"record_observation", "draft_advice", "flag_for_visit", "schedule_followup"}


async def _mcp_status() -> dict:
    from backend.agent.mcp_client import ToolClient

    try:
        async with ToolClient() as client:
            servers = await client.list_servers()
        error = None
    except Exception as exc:  # shown on screen, never hidden
        servers, error = [], f"{type(exc).__name__}: {exc}"
    for server in servers:
        for tool in server["tools"]:
            tool["writes"] = tool["name"] in MCP_WRITE_TOOLS
    with session_scope() as session:
        calls = session.exec(
            select(ToolLog).where(ToolLog.actor == "assistant", ToolLog.run_id.is_not(None))
        ).all()
    ai_services = [
        {
            "name": f"Groq ({settings.groq_model})",
            "job": "Drafts advice; backup translator",
            "linked": settings.model_configured,
            "without": "Rule-based drafting, marked as such in the log",
        },
        {
            "name": "Sunbird AI (sunflower-9b)",
            "job": f"Translates approved advice into {settings.farmer_language}",
            "linked": settings.translator_configured,
            "without": "Groq tries instead; if that fails the farmer sees English",
        },
        {
            "name": "Sunbird AI (text-to-speech)",
            "job": "Reads the farmer page aloud in English, Swahili and Karamojong",
            "linked": settings.translator_configured,
            "without": "The farmer's browser reads it in its own voice",
        },
        {"name": "Open-Meteo", "job": "Rain in the last 7 days and the next 7", "linked": True, "without": "No key needed"},
    ]
    return {
        "servers": servers,
        "ai_services": ai_services,
        "error": error,
        "assistant_calls": len(calls),
        "assistant_call_errors": sum(1 for c in calls if c.error),
    }


@api_router.get("/api/mcp")
async def mcp_status_api():
    return await _mcp_status()


@router.get("/mcp")
async def mcp_page(request: Request):
    return templates.TemplateResponse(request, "mcp.html", await _mcp_status())


@api_router.get("/api/week-at-a-glance")
def week_at_a_glance_api():
    """The Home screen's "This week at a glance" numbers, polled so farmer reports,
    answers and reads show there without a reload."""
    with session_scope() as session:
        return week_at_a_glance(session)
