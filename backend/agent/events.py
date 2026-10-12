# Per-run event queues, so the Agent run screen (officer screen 4) can watch tool
# calls as they happen over server-sent events, while tool_log keeps the permanent
# record. Events are best-effort and in memory only; the log table is the source of
# truth if a browser tab misses them.
from __future__ import annotations

import asyncio
from typing import Any

_queues: dict[int, asyncio.Queue] = {}


def create_run_queue(run_id: int) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    _queues[run_id] = queue
    return queue


def get_run_queue(run_id: int) -> asyncio.Queue | None:
    return _queues.get(run_id)


async def emit(run_id: int, event: dict[str, Any]) -> None:
    queue = _queues.get(run_id)
    if queue is not None:
        await queue.put(event)


def close_run_queue(run_id: int) -> None:
    queue = _queues.pop(run_id, None)
    if queue is not None:
        queue.put_nowait(None)  # sentinel: tells the SSE route the run is over
