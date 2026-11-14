# The SIMULATED gateway's auto-close timer, with logging. Safe across restarts because
# the close time is stored on the valve row: whatever was running (or not) in between,
# the next check closes every valve whose time has passed.
#
# Run (1) by the gateway server before every tool call, (2) by the app on start-up and
# (3) every IRRIGATION_SWEEP_SECONDS by the app while it runs (backend/main.py).
from __future__ import annotations

import asyncio

from backend.core.config import settings
from backend.core.database import session_scope
from backend.irrigation_server import device
from backend.logging.tool_log import run_logged


def close_due_valves_logged(session) -> list[str]:
    closed = device.close_due_valves(session)
    for plot in closed:
        run_logged(
            session, tool="auto_close_valve", inputs={"plot": plot}, actor="system",
            source=device.GATEWAY_SOURCE, run_id=None, func=lambda p=plot: {"plot": p, "state": "closed"},
        )
    return closed


def sweep_once() -> list[str]:
    with session_scope() as session:
        return close_due_valves_logged(session)


async def run_timer() -> None:
    """Background task for the app's lifetime."""
    while True:
        await asyncio.sleep(settings.irrigation_sweep_seconds)
        try:
            await asyncio.to_thread(sweep_once)
        except Exception:  # noqa: BLE001 - try again on the next tick
            pass
