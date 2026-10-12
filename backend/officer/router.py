# Everything an extension officer does through the API: record a visit, browse
# household groups, start an agent run and watch it live, manage farmer reports and
# visit flags, and read the log. See docs/API_CONTRACT.md.
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator
from sqlmodel import func, select
from sse_starlette.sse import EventSourceResponse

from backend.agent import events
from backend.agent.graph import execute_agent_run
from backend.core.config import settings
from backend.core.database import session_scope
from backend.db.models import (
    Advice,
    District,
    Followup,
    Household,
    Observation,
    Officer,
    Run,
    ToolLog,
    Village,
    VisitFlag,
)
from backend.logging.tool_log import run_logged
from backend.tools import agrimoist_tools as impl
from backend.tools.errors import ToolError

router = APIRouter(prefix="/api", tags=["officer"])

# asyncio only holds a weak reference to a task once nothing else references it, so a
# fire-and-forget asyncio.create_task() can be garbage-collected mid-run. Keep a
# strong reference here until each run finishes.
_background_runs: set[asyncio.Task] = set()


# ---------------------------------------------------------------------------
# Visits
# ---------------------------------------------------------------------------
class VisitRequest(BaseModel):
    household_code: str
    officer_id: int
    crop: str | None = None
    soil_wetness: str | None = None
    rain_last_7_days: str | None = None
    pest_seen: str | None = None
    crop_condition: str | None = None
    food_left: str | None = None
    notes: str | None = None

    @field_validator("pest_seen", "notes")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        # "  " or "" means nothing was entered; it must not be saved as a pest sighting.
        if value is None:
            return None
        return value.strip() or None


@router.post("/visits")
def record_visit(body: VisitRequest):
    with session_scope() as session:
        try:
            return run_logged(
                session,
                tool="record_observation",
                inputs=body.model_dump(),
                actor=str(body.officer_id),
                source="officer_visit",
                run_id=None,
                func=lambda: impl.record_observation(
                    session,
                    household_code=body.household_code,
                    source="officer_visit",
                    recorded_by=body.officer_id,
                    crop=body.crop,
                    soil_wetness=body.soil_wetness,
                    rain_last_7_days=body.rain_last_7_days,
                    pest_seen=body.pest_seen,
                    crop_condition=body.crop_condition,
                    food_left=body.food_left,
                    notes=body.notes,
                ),
            )
        except ToolError as exc:
            raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# Household groups
# ---------------------------------------------------------------------------
@router.get("/groups")
def list_groups():
    with session_scope() as session:
        group_ids = sorted({g for g in session.exec(select(Household.group_id)).all()})
        groups = []
        for group_id in group_ids:
            households = session.exec(select(Household).where(Household.group_id == group_id)).all()
            village = session.get(Village, households[0].village_id) if households else None
            open_flags = session.exec(
                select(func.count()).select_from(VisitFlag).where(
                    VisitFlag.group_id == group_id, VisitFlag.status == "open"
                )
            ).one()
            drafts_waiting = session.exec(
                select(func.count()).select_from(Advice).where(
                    Advice.group_id == group_id, Advice.status == "pending_review"
                )
            ).one()
            groups.append(
                {
                    "group_id": group_id,
                    "village": village.name if village else None,
                    "household_count": len(households),
                    "household_codes": [h.code for h in households],
                    "open_flags": open_flags,
                    "drafts_waiting": drafts_waiting,
                }
            )
        return groups


@router.get("/groups/{group_id}")
def group_detail(group_id: int):
    with session_scope() as session:
        households = session.exec(select(Household).where(Household.group_id == group_id)).all()
        if not households:
            raise HTTPException(404, "No such group")
        village = session.get(Village, households[0].village_id)
        district = session.get(District, village.district_id) if village else None

        field_records = impl.get_field_records(session, group_id=group_id)
        plot_history = impl.get_plot_history(session, group_id=group_id)

        crops = sorted({r["crop"] for r in plot_history["history"] if r.get("crop")})
        calendars = {}
        for crop in crops:
            try:
                calendars[crop] = impl.get_crop_calendar(
                    session, zone=district.agroclimatic_zone if district else "", crop=crop
                )
            except ToolError:
                calendars[crop] = None

        last_advice = session.exec(
            select(Advice).where(Advice.group_id == group_id).order_by(Advice.created_at.desc())
        ).first()

        return {
            "group_id": group_id,
            "village": village.name if village else None,
            "zone": district.agroclimatic_zone if district else None,
            "households": [h.code for h in households],
            "field_records": field_records,
            "plot_history": plot_history,
            "crop_calendar": calendars,
            "last_advice": (
                {"id": last_advice.id, "status": last_advice.status, "created_at": last_advice.created_at.isoformat()}
                if last_advice
                else None
            ),
        }


# ---------------------------------------------------------------------------
# Agent runs
# ---------------------------------------------------------------------------
@router.post("/groups/{group_id}/draft")
async def start_draft(group_id: int):
    with session_scope() as session:
        households = session.exec(select(Household).where(Household.group_id == group_id)).all()
        if not households:
            raise HTTPException(404, "No such group")
        model_name = settings.groq_model if settings.model_configured else "rule-based-stub"
        run = Run(group_id=group_id, model=model_name)
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id

    task = asyncio.create_task(execute_agent_run(group_id, run_id))
    _background_runs.add(task)
    task.add_done_callback(_background_runs.discard)
    return {"run_id": run_id}


@router.get("/runs/{run_id}")
def run_detail(run_id: int):
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(404, "No such run")
        return {
            "id": run.id,
            "group_id": run.group_id,
            "model": run.model,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "cost": run.cost,
            "outcome": run.outcome,
        }


@router.get("/runs/{run_id}/events")
async def run_events(run_id: int):
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(404, "No such run")

    queue = events.get_run_queue(run_id)

    async def event_generator():
        if queue is None:
            # The run already finished (or the server restarted) - replay from the
            # permanent log instead of a live stream.
            with session_scope() as session:
                rows = session.exec(
                    select(ToolLog).where(ToolLog.run_id == run_id).order_by(ToolLog.at)
                ).all()
                for row in rows:
                    yield {
                        "event": "tool_error" if row.error else "tool_result",
                        "data": json.dumps(
                            {"tool": row.tool, "outputs": row.outputs, "error": row.error, "source": row.source}
                        ),
                    }
                run = session.get(Run, run_id)
            yield {"event": "run_finished", "data": json.dumps({"outcome": run.outcome if run else None})}
            return

        while True:
            item = await queue.get()
            if item is None:
                break
            yield {"event": item.get("type", "message"), "data": json.dumps(item, default=str)}

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# Farmer reports (officer side: list and mark checked)
# ---------------------------------------------------------------------------
@router.get("/farmer-reports")
def list_farmer_reports(checked: str | None = None):
    with session_scope() as session:
        query = (
            select(Observation)
            .where(Observation.source.in_(["farmer_ussd", "farmer_web"]))
            .order_by(Observation.observed_on.desc())
        )
        if checked:
            query = query.where(Observation.checked == checked)
        rows = session.exec(query).all()
        out = []
        for o in rows:
            household = session.get(Household, o.household_id)
            out.append(
                {
                    "id": o.id,
                    "household_code": household.code if household else None,
                    "group_id": household.group_id if household else None,
                    "source": o.source,
                    "checked": o.checked,
                    "observed_on": o.observed_on.isoformat(),
                    "crop": o.crop,
                    "pest_seen": o.pest_seen,
                    "rain_last_7_days": o.rain_last_7_days,
                    "notes": o.notes,
                }
            )
        return out


class CheckReportRequest(BaseModel):
    officer_id: int


@router.post("/farmer-reports/{observation_id}/check")
def check_farmer_report(observation_id: int, body: CheckReportRequest):
    with session_scope() as session:
        observation = session.get(Observation, observation_id)
        if observation is None:
            raise HTTPException(404, "No such report")
        observation.checked = "checked"
        session.add(observation)
        session.commit()
        run_logged(
            session,
            tool="mark_report_checked",
            inputs={"observation_id": observation_id},
            actor=str(body.officer_id),
            source="officer",
            run_id=None,
            func=lambda: {"observation_id": observation_id, "checked": True},
        )
        return {"id": observation.id, "checked": observation.checked}


# ---------------------------------------------------------------------------
# Farmer follow-ups
# ---------------------------------------------------------------------------
@router.get("/followups")
def list_followups():
    with session_scope() as session:
        rows = session.exec(
            select(Followup, Advice)
            .join(Advice, Followup.advice_id == Advice.id)
            .where(Advice.status.in_(("approved", "edited_and_approved")))
            .order_by(Followup.due_on.desc(), Followup.advice_id.desc())
        ).all()
        return [
            {
                "advice_id": advice.id,
                "group_id": advice.group_id,
                "advice_text": advice.final_text,
                "due_on": followup.due_on.isoformat(),
                "followed": followup.followed,
                "helped": followup.helped,
                "answered_via": followup.answered_via,
            }
            for followup, advice in rows
        ]


# ---------------------------------------------------------------------------
# Visit flags (officer side: list and mark visited)
# ---------------------------------------------------------------------------
@router.get("/flags")
def list_flags(status: str | None = None):
    with session_scope() as session:
        query = select(VisitFlag).order_by(VisitFlag.created_at.desc())
        if status:
            query = query.where(VisitFlag.status == status)
        rows = session.exec(query).all()
        return [
            {
                "id": f.id,
                "group_id": f.group_id,
                "reason": f.reason,
                "raised_by": f.raised_by,
                "status": f.status,
                "created_at": f.created_at.isoformat(),
            }
            for f in rows
        ]


@router.post("/flags/{flag_id}/visited")
def mark_flag_visited(flag_id: int):
    with session_scope() as session:
        flag = session.get(VisitFlag, flag_id)
        if flag is None:
            raise HTTPException(404, "No such flag")
        flag.status = "visited"
        session.add(flag)
        session.commit()
        return {"id": flag.id, "status": flag.status}


# ---------------------------------------------------------------------------
# Log and officer list
# ---------------------------------------------------------------------------
@router.get("/logs")
def list_logs(run_id: int | None = None):
    with session_scope() as session:
        query = select(ToolLog).order_by(ToolLog.at.desc())
        if run_id:
            query = query.where(ToolLog.run_id == run_id)
        rows = session.exec(query.limit(500)).all()
        return [
            {
                "id": r.id,
                "run_id": r.run_id,
                "tool": r.tool,
                "inputs": r.inputs,
                "outputs": r.outputs,
                "source": r.source,
                "actor": r.actor,
                "at": r.at.isoformat(),
                "error": r.error,
            }
            for r in rows
        ]


@router.get("/runs")
def list_runs():
    with session_scope() as session:
        rows = session.exec(select(Run).order_by(Run.started_at.desc()).limit(100)).all()
        return [
            {
                "id": r.id,
                "group_id": r.group_id,
                "model": r.model,
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "cost": r.cost,
                "outcome": r.outcome,
            }
            for r in rows
        ]


@router.get("/officers")
def list_officers():
    with session_scope() as session:
        rows = session.exec(select(Officer)).all()
        return [{"id": o.id, "name": o.name, "role": o.role} for o in rows]
