# Every tool call is logged here: inputs, outputs, source, actor, time and any error.
# This is the single place that writes to tool_log, so every tool goes through it.
from __future__ import annotations

import json
from typing import Any, Callable

from sqlmodel import Session

from backend.db.models import ToolLog


def _json(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except TypeError:
        return json.dumps(str(value))


def run_logged(
    session: Session,
    *,
    tool: str,
    inputs: dict,
    actor: str,
    source: str | None,
    run_id: int | None,
    func: Callable[[], Any],
) -> Any:
    """Run `func`, write one tool_log row, and re-raise any error after logging it."""
    try:
        outputs = func()
        session.add(
            ToolLog(
                run_id=run_id,
                tool=tool,
                inputs=_json(inputs),
                outputs=_json(outputs),
                source=source,
                actor=actor,
                error=None,
            )
        )
        session.commit()
        return outputs
    except Exception as exc:  # noqa: BLE001 - we want to log and re-raise every failure
        session.add(
            ToolLog(
                run_id=run_id,
                tool=tool,
                inputs=_json(inputs),
                outputs=_json(None),
                source=source,
                actor=actor,
                error=str(exc),
            )
        )
        session.commit()
        raise
