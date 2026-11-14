# A SIMULATED irrigation gateway as its own MCP server (FastMCP, stdio). There is no
# real valve hardware: valve state is a row in the database (see device.py).
#
# Three tools: open_valve, close_valve, valve_status. The AI agent is never connected
# to this server (backend/agent/mcp_client.ToolClient opens only our AgriMoist server
# and the time server). Only the approval executor (backend/approvals/irrigation_executor.py)
# opens it, through IrrigationGatewayClient, after a named officer approves a proposal.
# open_valve also checks that approval itself. Every call is logged with run_logged.
#
#   python -m backend.irrigation_server.server
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from backend.core.database import init_db, session_scope
from backend.irrigation_server import device
from backend.irrigation_server.timer import close_due_valves_logged
from backend.logging.tool_log import run_logged

mcp = FastMCP(
    "irrigation-gateway",
    instructions=(
        "A SIMULATED irrigation gateway for AgriMoist AI (no real hardware). open_valve "
        "only works for an irrigation proposal a named officer has approved, and every "
        "valve closes itself when its minutes are up."
    ),
)


def _logged(tool: str, inputs: dict, actor: str, run_id: int | None, func):
    with session_scope() as session:
        # The gateway's own timer runs first, so a valve whose time passed while nothing
        # was running is closed before anything else happens.
        close_due_valves_logged(session)
        return run_logged(session, tool=tool, inputs=inputs, actor=actor, source=device.GATEWAY_SOURCE,
                          run_id=run_id, func=lambda: func(session))


@mcp.tool()
def open_valve(plot: str, minutes: int, proposal_id: int, officer_id: int, channel: str,
               run_id: int | None = None) -> dict:
    """Open the SIMULATED valve on a plot (household code, e.g. 'H-009') for `minutes`.
    Refused unless `proposal_id` was approved by a named officer and is waiting to open,
    and `minutes` matches it (maximum IRRIGATION_MAX_MINUTES). Closes itself after `minutes`."""
    return _logged(
        "open_valve",
        {"plot": plot, "minutes": minutes, "proposal_id": proposal_id, "officer_id": officer_id, "channel": channel},
        str(officer_id), run_id,
        lambda s: device.open_valve(s, plot=plot, minutes=minutes, proposal_id=proposal_id),
    )


@mcp.tool()
def close_valve(plot: str, reason: str, officer_id: int | None = None, channel: str | None = None,
                run_id: int | None = None) -> dict:
    """Close the SIMULATED valve on a plot now. Closing is always allowed."""
    return _logged(
        "close_valve",
        {"plot": plot, "reason": reason, "officer_id": officer_id, "channel": channel},
        str(officer_id) if officer_id is not None else "system", run_id,
        lambda s: device.close_valve(s, plot=plot),
    )


@mcp.tool()
def valve_status(plot: str, run_id: int | None = None) -> dict:
    """Whether the SIMULATED valve on a plot is closed or open, and the seconds left if open."""
    return _logged("valve_status", {"plot": plot}, "system", run_id, lambda s: device.valve_status(s, plot=plot))


if __name__ == "__main__":
    init_db()
    mcp.run()
