# The approval executor: the ONLY code that opens or closes a (SIMULATED) valve. It is
# called by apply_decision in backend/approvals/router.py, right after a named officer's
# approval of an irrigation proposal has been committed - on any channel (web, USSD,
# offline sync), because they all decide through that one function.
#
# It reaches the irrigation gateway over MCP (IrrigationGatewayClient), the same way the
# agent reaches its own tools; the agent itself is never connected to that server.
#
# Fail closed: if anything goes wrong while opening, it closes the valve through the
# gateway, and if even that fails it marks the valve closed directly. The approval
# itself stands either way (the officer's decision is recorded); only the valve stays shut.
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable

from sqlmodel import select, update

from backend.agent.mcp_client import IrrigationGatewayClient
from backend.db.models import Advice, IrrigationProposal, Officer
from backend.irrigation_server import device
from backend.logging.tool_log import run_logged
from backend.tools.errors import ToolError


def proposal_for(session, advice_id: int) -> IrrigationProposal | None:
    """The irrigation proposal behind this queue item, or None for ordinary advice."""
    return session.exec(select(IrrigationProposal).where(IrrigationProposal.advice_id == advice_id)).first()


def _run(make: Callable[[], Awaitable[Any]]) -> Any:
    """Run an async MCP call from sync code (the approval routes and the USSD menu),
    in a fresh thread with its own event loop, so it works whether or not the caller is
    already inside one."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(make())).result()


async def _gateway_call(tool: str, **arguments: Any) -> dict:
    # The gateway's refusal is raised after the client has closed, so it reaches the log
    # as itself, not wrapped in the transport's task-group error.
    error: ToolError | None = None
    async with IrrigationGatewayClient() as gateway:
        try:
            return await gateway.call(tool, **arguments)
        except ToolError as exc:
            error = exc
    raise error


def _fail_closed(session, proposal: IrrigationProposal, officer: Officer, channel: str, error: str) -> dict:
    """Leave the valve CLOSED after an error, whatever state the gateway got to."""
    try:
        _run(lambda: _gateway_call(
            "close_valve", plot=proposal.plot_code, reason=f"fail closed after error: {error}",
            officer_id=officer.id, channel=channel, run_id=proposal.run_id,
        ))
    except Exception:  # noqa: BLE001 - the direct close below still runs
        pass
    session.expire_all()  # the gateway wrote from another process
    status = device.valve_status(session, plot=proposal.plot_code)
    if status["state"] != "closed":
        status = run_logged(
            session, tool="force_close_valve",
            inputs={"plot": proposal.plot_code, "proposal_id": proposal.id, "officer_id": officer.id, "channel": channel},
            actor="system", source="approval-executor", run_id=proposal.run_id,
            func=lambda: device.force_closed(session, plot=proposal.plot_code),
        )
    session.exec(
        update(IrrigationProposal).where(IrrigationProposal.id == proposal.id)
        .values(execution="failed", execution_error=error)
    )
    session.commit()
    return status


def execute_approved_proposal(session, proposal: IrrigationProposal, officer: Officer, channel: str) -> dict:
    """Open the valve for an approved proposal. Runs once per proposal: the claim
    (not_started -> opening) is a conditional UPDATE, like the gate's decide-once."""
    advice = session.get(Advice, proposal.advice_id)
    if advice is None or advice.status != "approved":
        raise ToolError("Refused: the proposal is not approved")
    claimed = session.exec(
        update(IrrigationProposal)
        .where(IrrigationProposal.id == proposal.id, IrrigationProposal.execution == "not_started")
        .values(execution="opening")
    ).rowcount
    session.commit()
    if not claimed:
        session.refresh(proposal)
        return {"valve": device.valve_status(session, plot=proposal.plot_code), "opened": False,
                "error": f"already '{proposal.execution}'; nothing done"}

    inputs = {
        "proposal_id": proposal.id, "advice_id": proposal.advice_id, "plot": proposal.plot_code,
        "minutes": proposal.minutes, "officer_id": officer.id, "channel": channel,
    }

    def open_it() -> dict:
        try:
            status = _run(lambda: _gateway_call(
                "open_valve", plot=proposal.plot_code, minutes=proposal.minutes, proposal_id=proposal.id,
                officer_id=officer.id, channel=channel, run_id=proposal.run_id,
            ))
        except Exception as exc:  # noqa: BLE001 - every failure ends with the valve closed
            _fail_closed(session, proposal, officer, channel, str(exc))
            raise ToolError(f"Valve left CLOSED after an error: {exc}") from exc
        session.exec(
            update(IrrigationProposal)
            .where(IrrigationProposal.id == proposal.id, IrrigationProposal.execution == "opening")
            .values(execution="opened")
        )
        session.commit()
        return {"valve": status, "opened": True, "error": None}

    try:
        return run_logged(session, tool="execute_irrigation", inputs=inputs, actor=str(officer.id),
                          source="approval-executor", run_id=proposal.run_id, func=open_it)
    except Exception as exc:  # noqa: BLE001 - logged by run_logged; the approval stands
        session.rollback()
        session.expire_all()
        status = device.valve_status(session, plot=proposal.plot_code)
        if status["state"] != "closed":  # failed after the valve opened: close it too
            status = _fail_closed(session, proposal, officer, channel, str(exc))
        return {"valve": status, "opened": False, "error": str(exc)}
