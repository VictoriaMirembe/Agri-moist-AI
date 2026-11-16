# The approval gate. This is the ONLY module in the whole app allowed to set an
# advice status to approved or edited_and_approved - see docs/API_CONTRACT.md.
# Every web route needs a signed-in officer (signed session, backend/officer/auth.py), and
# the deciding officer is always the one in that session - an officer_id in the
# request body is ignored. A rejection always needs a reason, and only a draft that is
# pending_review (never blocked, drafted, expired or already decided) can be acted on.
#
# Every channel decides through the same two functions here: load_decidable (may this
# draft still be decided?) and apply_decision (decide it, once). The web routes below
# and the officer USSD menu (backend/officer/ussd.py) both call them, so a rule added
# here - such as draft expiry - holds on every channel and cannot be skipped.
#
# Irrigation proposals (SIMULATED valve) are queue items too: an Advice row plus an
# IrrigationProposal row. They pass through these same two functions, so sign-in,
# expiry, decide-once and both channels apply to them unchanged. Only after an approval
# is committed does apply_decision hand the proposal to the approval executor
# (irrigation_executor.py), the one piece of code that may open a valve. A proposal
# cannot be edited, and it is never translated or shown to farmers.
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlmodel import select, update

from backend.agent.model_client import translate_text
from backend.approvals.irrigation_executor import execute_approved_proposal, proposal_for
from backend.core.config import settings
from backend.core.database import session_scope
from backend.db.models import Advice, AdviceSource, Approval, Officer, Run
from backend.irrigation_server import device
from backend.logging.tool_log import run_logged
from backend.officer.auth import require_officer, signed_in_officer_id

router = APIRouter(prefix="/api/approvals", tags=["approvals"], dependencies=[Depends(require_officer)])


# officer_id is still accepted in each body so older clients keep working, but it is
# ignored: the deciding officer always comes from the signed session.
class ApproveRequest(BaseModel):
    officer_id: int | None = None


class EditRequest(BaseModel):
    officer_id: int | None = None
    final_text: str

    @field_validator("final_text")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("final_text cannot be blank")
        return v


class RejectRequest(BaseModel):
    officer_id: int | None = None
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason is required to reject advice")
        return v


def _serialize(session, advice: Advice) -> dict:
    sources = session.exec(select(AdviceSource).where(AdviceSource.advice_id == advice.id)).all()
    proposal = proposal_for(session, advice.id)
    return {
        "id": advice.id,
        "group_id": advice.group_id,
        "run_id": advice.run_id,
        "status": advice.status,
        "draft_text": advice.draft_text,
        "final_text": advice.final_text,
        "why_text": advice.why_text,
        "language": advice.language,
        "created_at": advice.created_at.isoformat(),
        "kind": "irrigation" if proposal else "advice",
        "irrigation": _irrigation_details(session, proposal) if proposal else None,
        "sources": [
            {
                "fact": s.fact,
                "source_type": s.source_type,
                "source_ref": s.source_ref,
                "is_gap": s.source_ref.startswith(("unavailable", "missing")),
            }
            for s in sources
        ],
    }


def _irrigation_details(session, proposal) -> dict:
    return {
        "proposal_id": proposal.id,
        "plot": proposal.plot_code,
        "minutes": proposal.minutes,
        "moisture_pct": proposal.moisture_pct,
        "soil_read_at": proposal.soil_read_at.isoformat(),
        "forecast_mm": proposal.forecast_mm,
        "execution": proposal.execution,
        "execution_error": proposal.execution_error,
        "label": f"IRRIGATE {proposal.plot_code} {proposal.minutes} min",
        "valve": device.valve_status(session, plot=proposal.plot_code),
        "simulated": True,
    }


EXPIRED_MESSAGE = "Expired: please re-draft"
DECISION_STATUS = {"approve": "approved", "edit_and_approve": "edited_and_approved", "reject": "rejected"}


class GateRefusal(Exception):
    """The gate would not act. `advice_status` is the draft's current status (None if
    there is no such draft), so a caller can show it instead of acting again."""

    def __init__(self, status_code: int, detail: str, advice_status: str | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.advice_status = advice_status


def _expiry_cutoff() -> datetime:
    """Drafts created at or before this moment (UTC) are too old to approve."""
    return datetime.now(timezone.utc) - timedelta(hours=settings.draft_expiry_hours)


def expire_stale_drafts(session) -> int:
    """Move every pending_review draft older than DRAFT_EXPIRY_HOURS to 'expired',
    and log each one. Runs before every decision and every pending list, so no
    channel can show or approve advice written for weather and field facts that
    have since changed. Returns how many drafts expired."""
    stale = session.exec(
        select(Advice).where(Advice.status == "pending_review", Advice.created_at <= _expiry_cutoff())
    ).all()
    expired = 0
    for advice in stale:
        # Only if it is still pending: a decision made a moment ago is never undone.
        changed = session.exec(
            update(Advice).where(Advice.id == advice.id, Advice.status == "pending_review").values(status="expired")
        ).rowcount
        session.commit()
        if changed:
            expired += 1
            run_logged(
                session,
                tool="expire_advice",
                inputs={"advice_id": advice.id, "expiry_hours": settings.draft_expiry_hours},
                actor="system",
                source="system",
                run_id=advice.run_id,
                func=lambda: {"id": advice.id, "status": "expired"},
            )
    return expired


def load_decidable(session, advice_id: int) -> Advice:
    """The draft, if it may still be decided. Otherwise GateRefusal with its current
    status: unknown (404), expired, blocked or already decided (409)."""
    expire_stale_drafts(session)
    advice = session.get(Advice, advice_id)
    if advice is None:
        raise GateRefusal(404, "No such advice")
    if advice.status == "expired":
        raise GateRefusal(409, EXPIRED_MESSAGE, advice.status)
    if advice.status != "pending_review":
        raise GateRefusal(409, f"Advice is '{advice.status}', not pending_review; nothing changed", advice.status)
    return advice


def _pending_or_409(session, advice_id: int) -> Advice:
    try:
        return load_decidable(session, advice_id)
    except GateRefusal as refusal:
        raise HTTPException(refusal.status_code, refusal.detail) from refusal


def _session_officer(session, request: Request) -> Officer:
    """The signed-in officer (require_officer has already checked there is one)."""
    officer_id = signed_in_officer_id(request)
    officer = session.get(Officer, officer_id) if officer_id is not None else None
    if officer is None:
        raise HTTPException(403, "Access denied: Officers only.")
    return officer


def _log_decision(
    session, advice: Advice, officer: Officer, decision: str, inputs: dict, outputs: dict, run_id: int | None = None
) -> None:
    """Every approval decision goes into the same log as tool calls, tied to the
    agent run that drafted the advice (or proposed the irrigation), with the officer
    as the actor."""
    run_logged(
        session,
        tool=f"{decision}_advice",
        inputs={"advice_id": advice.id, "officer_id": officer.id, **inputs},
        actor=str(officer.id),
        source="officer",
        run_id=advice.run_id or run_id,
        func=lambda: outputs,
    )


def apply_decision(
    session,
    advice: Advice,
    officer: Officer,
    decision: str,  # approve, edit_and_approve or reject
    *,
    channel: str,  # web or ussd
    reason: str | None = None,
    final_text: str | None = None,
) -> dict:
    """Decide a draft that load_decidable returned. The one place a decision is made.

    The status change is a single conditional UPDATE (only while the draft is still
    pending_review and not expired), so two requests for the same draft - a double
    tap, a gateway retry, the web and a phone at once - can never both act. The
    loser gets GateRefusal carrying the status the winner set."""
    new_status = DECISION_STATUS[decision]
    values: dict = {"status": new_status}
    translation = None
    proposal = proposal_for(session, advice.id)
    if proposal is not None and decision == "edit_and_approve":
        raise GateRefusal(409, "An irrigation proposal cannot be edited: approve or reject it.", advice.status)
    if proposal is not None and decision == "approve":
        values["final_text"] = advice.draft_text  # an instruction to the valve: not translated
    elif decision != "reject":
        # Translate the final, approved text for farmers. Any translation failure
        # falls back to showing the English text - it never blocks the approval.
        values["final_text"] = final_text if decision == "edit_and_approve" else advice.draft_text
        translation = translate_text(values["final_text"] or "", settings.farmer_language)
        values["translated_text"] = translation["translated_text"]

    claimed = session.exec(
        update(Advice)
        .where(Advice.id == advice.id, Advice.status == "pending_review", Advice.created_at > _expiry_cutoff())
        .values(**values)
    ).rowcount
    if not claimed:
        session.rollback()
        session.refresh(advice)
        if advice.status == "pending_review":  # it passed the expiry time just now
            raise GateRefusal(409, EXPIRED_MESSAGE, "expired")
        raise GateRefusal(409, f"Advice is '{advice.status}', not pending_review; nothing changed", advice.status)

    # A run's cost is everything AI spent on that advice: drafting, then translating.
    run = session.get(Run, advice.run_id) if advice.run_id else None
    if run is not None and translation and translation.get("cost"):
        run.cost = round((run.cost or 0.0) + translation["cost"], 6)
        session.add(run)
    session.add(Approval(advice_id=advice.id, officer_id=officer.id, decision=new_status, reason=reason))
    session.commit()
    session.refresh(advice)

    result = {"id": advice.id, "status": advice.status, "officer": officer.name}
    inputs: dict = {"channel": channel}
    outputs = dict(result)
    if decision == "edit_and_approve":
        inputs["final_text"] = final_text
    if decision == "reject":
        inputs["reason"] = reason
    if translation is not None:
        result["translation_model"] = translation["model"]
        # Logging only: the translator's own token counts next to the cost already logged.
        outputs = {
            **result,
            "translation_cost": translation.get("cost", 0.0),
            "translation_prompt_tokens": int(translation.get("prompt_tokens") or 0),
            "translation_completion_tokens": int(translation.get("completion_tokens") or 0),
            "translation_total_tokens": int(translation.get("total_tokens") or 0),
        }
    if proposal is not None:
        inputs["irrigation_proposal_id"] = proposal.id
    _log_decision(session, advice, officer, decision, inputs, outputs, run_id=proposal.run_id if proposal else None)
    if proposal is not None and new_status == "approved":
        # Only now, with the named officer's approval committed and logged, may a valve open.
        result["irrigation"] = execute_approved_proposal(session, proposal, officer, channel)
    return result


def _decide_or_http(session, advice: Advice, officer: Officer, decision: str, **kwargs) -> dict:
    try:
        return apply_decision(session, advice, officer, decision, channel="web", **kwargs)
    except GateRefusal as refusal:
        raise HTTPException(refusal.status_code, refusal.detail) from refusal


@router.get("")
def list_approvals(status: str | None = None):
    with session_scope() as session:
        expire_stale_drafts(session)
        query = select(Advice)
        if status:
            query = query.where(Advice.status == status)
        advices = session.exec(query.order_by(Advice.created_at.desc())).all()
        return [_serialize(session, a) for a in advices]


@router.post("/{advice_id}/approve")
def approve(advice_id: int, request: Request, body: ApproveRequest | None = None):
    with session_scope() as session:
        advice = _pending_or_409(session, advice_id)
        officer = _session_officer(session, request)
        return _decide_or_http(session, advice, officer, "approve")


@router.post("/{advice_id}/edit")
def edit_and_approve(advice_id: int, body: EditRequest, request: Request):
    with session_scope() as session:
        advice = _pending_or_409(session, advice_id)
        officer = _session_officer(session, request)
        return _decide_or_http(session, advice, officer, "edit_and_approve", final_text=body.final_text)


@router.post("/{advice_id}/reject")
def reject(advice_id: int, body: RejectRequest, request: Request):
    with session_scope() as session:
        advice = _pending_or_409(session, advice_id)
        officer = _session_officer(session, request)
        return _decide_or_http(session, advice, officer, "reject", reason=body.reason)
