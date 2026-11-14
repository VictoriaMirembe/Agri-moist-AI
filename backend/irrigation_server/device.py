# The SIMULATED irrigation gateway's behaviour. There is no real valve: the state of
# each plot's valve is a row in the Valve table. Pure functions with no logging inside,
# like backend/tools/agrimoist_tools.py - the MCP server (server.py) and the app's timer
# wrap every call in run_logged.
#
# Safety, in the device itself (not only in the code that calls it):
# - open_valve refuses unless the proposal's draft was approved by a named officer
#   (an Approval row) and the approval executor has claimed it (execution "opening").
# - minutes are capped at IRRIGATION_MAX_MINUTES and must match the approved proposal.
# - every open valve has a close time. close_due_valves closes any valve whose time has
#   passed; it runs before every gateway call, on app start and on the app's timer, so
#   a restart never leaves a valve open past its time.
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, select

from backend.core.config import settings
from backend.db.models import Advice, Approval, IrrigationProposal, Valve
from backend.tools.errors import ToolError

GATEWAY_SOURCE = "irrigation-gateway (simulated)"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _status(valve: Valve | None, plot: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    if valve is None or valve.state != "open":
        return {"plot": plot, "state": "closed", "seconds_left": 0, "close_at": None, "simulated": True}
    close_at = _aware(valve.close_at)
    return {
        "plot": plot,
        "state": "open",
        "opened_at": _aware(valve.opened_at).isoformat() if valve.opened_at else None,
        "close_at": close_at.isoformat() if close_at else None,
        "seconds_left": max(0, int((close_at - now).total_seconds())) if close_at else 0,
        "proposal_id": valve.proposal_id,
        "simulated": True,
    }


def _set_closed(session: Session, valve: Valve, execution: str = "closed") -> None:
    proposal_id = valve.proposal_id
    valve.state = "closed"
    valve.close_at = None
    valve.updated_at = _now()
    session.add(valve)
    if proposal_id is not None:
        proposal = session.get(IrrigationProposal, proposal_id)
        if proposal is not None and proposal.execution in ("opening", "opened"):
            proposal.execution = execution
            session.add(proposal)
    session.commit()


def close_due_valves(session: Session, now: datetime | None = None) -> list[str]:
    """The gateway's timer: close every open valve whose close time has passed (or that
    has no close time at all). Returns the plots it closed."""
    now = now or _now()
    closed = []
    for valve in session.exec(select(Valve).where(Valve.state == "open")).all():
        close_at = _aware(valve.close_at)
        if close_at is None or close_at <= now:
            _set_closed(session, valve)
            closed.append(valve.plot_code)
    return closed


def valve_status(session: Session, *, plot: str) -> dict[str, Any]:
    return _status(session.get(Valve, plot), plot)


def all_valves(session: Session) -> list[dict[str, Any]]:
    now = _now()
    return [_status(v, v.plot_code, now) for v in session.exec(select(Valve).order_by(Valve.plot_code)).all()]


def open_valve(session: Session, *, plot: str, minutes: int, proposal_id: int) -> dict[str, Any]:
    if settings.irrigation_gateway_fault == "open_fails":
        raise ToolError("Irrigation gateway did not respond; the valve was not opened (simulated fault).",
                        source=GATEWAY_SOURCE)
    proposal = session.get(IrrigationProposal, proposal_id)
    if proposal is None or proposal.plot_code != plot:
        raise ToolError(f"No irrigation proposal {proposal_id} for plot {plot}")
    advice = session.get(Advice, proposal.advice_id)
    approval = session.exec(
        select(Approval).where(Approval.advice_id == proposal.advice_id, Approval.decision == "approved")
    ).first()
    if advice is None or advice.status != "approved" or approval is None:
        raise ToolError("Refused: this proposal has not been approved by a named officer")
    if proposal.execution != "opening":
        raise ToolError(f"Refused: proposal {proposal_id} is '{proposal.execution}', not waiting to open")
    if minutes < 1 or minutes > settings.irrigation_max_minutes or minutes != proposal.minutes:
        raise ToolError(
            f"Refused: {minutes} min is not the approved {proposal.minutes} min "
            f"(maximum {settings.irrigation_max_minutes})"
        )
    valve = session.get(Valve, plot) or Valve(plot_code=plot)
    if valve.state == "open":
        raise ToolError(f"Refused: the valve on {plot} is already open")

    now = _now()
    valve.state = "open"
    valve.opened_at = now
    valve.close_at = now + timedelta(minutes=minutes)
    valve.proposal_id = proposal_id
    valve.updated_at = now
    session.add(valve)
    session.commit()
    if settings.irrigation_gateway_fault == "no_confirmation":
        # The valve opened, but the answer was lost on the way back.
        raise ToolError("Irrigation gateway opened the valve but sent no confirmation (simulated fault).",
                        source=GATEWAY_SOURCE)
    return _status(valve, plot, now)


def close_valve(session: Session, *, plot: str) -> dict[str, Any]:
    valve = session.get(Valve, plot)
    if valve is not None and valve.state == "open":
        _set_closed(session, valve)
    return _status(session.get(Valve, plot), plot)


def force_closed(session: Session, *, plot: str, execution: str = "failed") -> dict[str, Any]:
    """Last resort for the approval executor when the gateway cannot be reached even to
    close: mark the valve closed directly. Closing is always the safe direction."""
    valve = session.get(Valve, plot)
    if valve is not None and valve.state == "open":
        _set_closed(session, valve, execution=execution)
    return _status(session.get(Valve, plot), plot)
