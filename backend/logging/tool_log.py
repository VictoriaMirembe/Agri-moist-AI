# Every tool call is logged here: inputs, outputs, source, actor, time and any error.
# This is the single place that writes to tool_log, so every tool goes through it.
from __future__ import annotations

import json
from typing import Any, Callable

from sqlmodel import Session, select

from backend.db.models import ToolLog

# The drafting model's name, token counts and cost live as extra fields on each run's
# draft_advice log row (written by backend.agent.graph._record_model_usage). These
# helpers read them back for the run page, the activity log and GET /api/runs.
ZERO_MODEL_USAGE: dict[str, Any] = {
    "model": None,
    "model_cost": 0.0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
}


def model_usage_by_run(session: Session, run_ids: list[int]) -> dict[int, dict]:
    """Model name, tokens and cost for each of these runs, keyed by run id. A run
    whose log row carries no usage (an older run, or one that never drafted) is left
    out rather than reported as zero tokens it never spent."""
    if not run_ids:
        return {}
    rows = session.exec(
        select(ToolLog)
        .where(ToolLog.tool == "draft_advice", ToolLog.run_id.in_(run_ids))
        .order_by(ToolLog.id)
    ).all()
    usage: dict[int, dict] = {}
    for row in rows:
        try:
            outputs = json.loads(row.outputs) if row.outputs else {}
        except ValueError:
            continue
        if not isinstance(outputs, dict) or "total_tokens" not in outputs:
            continue
        usage[row.run_id] = {name: outputs.get(name, default) for name, default in ZERO_MODEL_USAGE.items()}
    return usage


def model_usage_for_run(session: Session, run_id: int | None) -> dict:
    """One run's model usage, or zeros when the run has none recorded."""
    if run_id is None:
        return dict(ZERO_MODEL_USAGE)
    return model_usage_by_run(session, [run_id]).get(run_id, dict(ZERO_MODEL_USAGE))


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
