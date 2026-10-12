# The approval gate. This is the ONLY module in the whole app allowed to set an
# advice status to approved or edited_and_approved - see docs/API_CONTRACT.md.
# A rejection always needs a reason; approving or editing always needs an officer id,
# and only a draft that is pending_review (never blocked, drafted, or already
# decided) can be acted on.
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator
from sqlmodel import select

from backend.agent.model_client import translate_text
from backend.core.config import settings
from backend.core.database import session_scope
from backend.db.models import Advice, AdviceSource, Approval, Officer
from backend.logging.tool_log import run_logged

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class ApproveRequest(BaseModel):
    officer_id: int


class EditRequest(BaseModel):
    officer_id: int
    final_text: str

    @field_validator("final_text")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("final_text cannot be blank")
        return v


class RejectRequest(BaseModel):
    officer_id: int
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason is required to reject advice")
        return v


def _serialize(session, advice: Advice) -> dict:
    sources = session.exec(select(AdviceSource).where(AdviceSource.advice_id == advice.id)).all()
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


@router.get("")
def list_approvals(status: str | None = None):
    with session_scope() as session:
        query = select(Advice)
        if status:
            query = query.where(Advice.status == status)
        advices = session.exec(query.order_by(Advice.created_at.desc())).all()
        return [_serialize(session, a) for a in advices]


def _pending_or_409(session, advice_id: int) -> Advice:
    advice = session.get(Advice, advice_id)
    if advice is None:
        raise HTTPException(404, "No such advice")
    if advice.status != "pending_review":
        raise HTTPException(409, f"Advice is '{advice.status}', not pending_review; nothing changed")
    return advice


def _officer_or_400(session, officer_id: int) -> Officer:
    officer = session.get(Officer, officer_id)
    if officer is None:
        raise HTTPException(400, f"No officer with id {officer_id}")
    return officer


def _translate_and_store(session, advice: Advice) -> dict:
    """Translate the final, approved text for farmers. Runs only after approval
    (advice.translated_text is set after approval only). Any
    translation failure falls back to showing the English text - never blocks
    the approval itself."""
    result = translate_text(advice.final_text or "", settings.farmer_language)
    advice.translated_text = result["translated_text"]
    session.add(advice)
    return {"model": result["model"], "cost": result.get("cost", 0.0)}


def _log_decision(session, advice: Advice, officer: Officer, decision: str, inputs: dict, outputs: dict) -> None:
    """Every approval decision goes into the same log as tool calls, tied to the
    agent run that drafted the advice, with the officer as the actor."""
    run_logged(
        session,
        tool=f"{decision}_advice",
        inputs={"advice_id": advice.id, "officer_id": officer.id, **inputs},
        actor=str(officer.id),
        source="officer",
        run_id=advice.run_id,
        func=lambda: outputs,
    )


@router.post("/{advice_id}/approve")
def approve(advice_id: int, body: ApproveRequest):
    with session_scope() as session:
        advice = _pending_or_409(session, advice_id)
        officer = _officer_or_400(session, body.officer_id)
        advice.status = "approved"
        advice.final_text = advice.draft_text
        session.add(advice)
        translation = _translate_and_store(session, advice)
        session.add(Approval(advice_id=advice.id, officer_id=officer.id, decision="approved"))
        session.commit()
        result = {"id": advice.id, "status": advice.status, "officer": officer.name, "translation_model": translation["model"]}
        _log_decision(session, advice, officer, "approve", {}, {**result, "translation_cost": translation["cost"]})
        return result


@router.post("/{advice_id}/edit")
def edit_and_approve(advice_id: int, body: EditRequest):
    with session_scope() as session:
        advice = _pending_or_409(session, advice_id)
        officer = _officer_or_400(session, body.officer_id)
        advice.status = "edited_and_approved"
        advice.final_text = body.final_text
        session.add(advice)
        translation = _translate_and_store(session, advice)
        session.add(Approval(advice_id=advice.id, officer_id=officer.id, decision="edited_and_approved"))
        session.commit()
        result = {"id": advice.id, "status": advice.status, "officer": officer.name, "translation_model": translation["model"]}
        _log_decision(
            session, advice, officer, "edit_and_approve", {"final_text": body.final_text},
            {**result, "translation_cost": translation["cost"]},
        )
        return result


@router.post("/{advice_id}/reject")
def reject(advice_id: int, body: RejectRequest):
    with session_scope() as session:
        advice = _pending_or_409(session, advice_id)
        officer = _officer_or_400(session, body.officer_id)
        advice.status = "rejected"
        session.add(advice)
        session.add(
            Approval(advice_id=advice.id, officer_id=officer.id, decision="rejected", reason=body.reason)
        )
        session.commit()
        result = {"id": advice.id, "status": advice.status, "officer": officer.name}
        _log_decision(session, advice, officer, "reject", {"reason": body.reason}, result)
        return result
