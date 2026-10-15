# Runs the 14 tasks in EVALS.md against the real stack (real SQLite, real MCP
# subprocesses, a real Open-Meteo call where the scenario needs live weather) - three
# times each, and writes results to evals_results.json next to this script. EVALS.md
# is filled in by hand from that file so the numbers in the two always match.
#
# Run from the repo root: .venv/Scripts/python.exe scripts/run_evals.py
# To re-run only some tasks (merged into the existing results file):
#   .venv/Scripts/python.exe scripts/run_evals.py 10 13
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

RESULTS_FILE = Path(__file__).resolve().parent / "evals_results.json"
MODEL_PAUSE_SECONDS = 8 if os.environ.get("EVALS_NO_PAUSE") != "1" else 0


def fresh_db_url() -> Path:
    db_dir = REPO_ROOT / "data" / "_eval_runs"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / f"{uuid.uuid4().hex}.db"


def reset_env():
    for key in ("WEATHER_FAIL", "WEATHER_OVERRIDE_PAST_MM", "WEATHER_OVERRIDE_NEXT_MM"):
        os.environ.pop(key, None)


def _sign_in(client) -> None:
    """Officer endpoints need a signed-in officer, exactly as in the browser."""
    from backend.core.config import settings

    client.post("/api/auth/login", json={"officer_id": 1, "password": settings.officer_demo_password}).raise_for_status()


async def new_app(**env_overrides):
    """Fresh temp DB, fresh engine, fresh settings, seeded. Returns helpers."""
    reset_env()
    for k, v in env_overrides.items():
        os.environ[k] = str(v)

    db_path = fresh_db_url()
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

    # Only the parent process's database engine needs rebuilding by hand: the two
    # MCP tool servers are spawned fresh per agent run with a copy of the current
    # os.environ (see backend/agent/mcp_client.py), so WEATHER_FAIL and the weather
    # override vars always reach them correctly without any reload gymnastics here.
    from sqlmodel import create_engine

    import backend.core.database as database_module

    database_module.engine = create_engine(
        os.environ["DATABASE_URL"], echo=False, connect_args={"check_same_thread": False}
    )
    database_module.init_db()

    from backend.db.seed import seed

    with database_module.session_scope() as session:
        seed(session)

    return database_module


async def run_agent(database_module, group_id: int):
    from backend.db.models import Run

    with database_module.session_scope() as session:
        from backend.core.config import settings

        # Same label the real "Draft advice" route uses (backend/officer/router.py).
        model_name = settings.groq_model if settings.model_configured else "rule-based-stub"
        run = Run(group_id=group_id, model=model_name)
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id

    from backend.agent.graph import execute_agent_run

    started = time.monotonic()
    await execute_agent_run(group_id=group_id, run_id=run_id)
    elapsed = time.monotonic() - started
    return run_id, elapsed


# Full advice text of the current run, saved with that run's result so a failure can
# be read in full afterwards (the per-run databases are deleted at the end).
CAPTURED: dict = {}


def get_run_and_advice(database_module, run_id: int):
    from sqlmodel import select

    from backend.db.models import Advice, AdviceSource, Run, ToolLog

    with database_module.session_scope() as session:
        run = session.get(Run, run_id)
        advice = session.exec(select(Advice).where(Advice.run_id == run_id)).first()
        sources = []
        if advice:
            sources = session.exec(select(AdviceSource).where(AdviceSource.advice_id == advice.id)).all()
        logs = session.exec(select(ToolLog).where(ToolLog.run_id == run_id)).all()
        if advice:
            CAPTURED.update(
                advice_status=advice.status, advice_text=advice.draft_text, why_text=advice.why_text
            )
        return (
            {"outcome": run.outcome, "model": run.model, "cost": run.cost},
            (
                {
                    "status": advice.status,
                    "draft_text": advice.draft_text,
                    "why_text": advice.why_text,
                }
                if advice
                else None
            ),
            [{"fact": s.fact, "source_type": s.source_type, "source_ref": s.source_ref} for s in sources],
            [{"tool": row.tool, "error": row.error} for row in logs],
        )


def _describe(run: dict, advice: dict | None) -> str:
    """One line that explains a pass or a failure: which model ran, what happened, and
    the start of the advice it wrote."""
    parts = [f"model={run['model']}", f"outcome={run['outcome']}"]
    if advice:
        parts.append(f"advice_status={advice['status']}")
        parts.append(f"advice='{advice['draft_text'][:160]}'")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Each task_* function performs ONE run and returns (passed, detail)
# ---------------------------------------------------------------------------
async def task_1_dry_soil_little_rain():
    db = await new_app(WEATHER_OVERRIDE_PAST_MM=1.0, WEATHER_OVERRIDE_NEXT_MM=2.0)
    run_id, elapsed = await run_agent(db, group_id=3)
    run, advice, sources, _ = get_run_and_advice(db, run_id)
    ok = (
        run["outcome"] == "drafted"
        and advice is not None
        and "water" in advice["draft_text"].lower()
        and any(s["source_type"] == "weather" for s in sources)
    )
    return ok, _describe(run, advice), elapsed, run["cost"]


# Task 2 (Round 2): judged on meaning, not one exact phrase.
_HEAVY_FORECAST = re.compile(
    r"\b45(?:\.0)?\s*mm|heavy rain|a lot of rain|lots of rain|plenty of rain|much rain|good rain"
)
_NO_EXTRA_WATER = re.compile(
    r"no (?:extra |more |additional )?watering|no need (?:to|for) (?:extra |more )?(?:water|irrigat)\w*"
    r"|(?:do not|don't|don’t|need not|not need to|won't need to|will not need to) (?:add |give )?(?:extra |more )?"
    r"(?:water|irrigat)\w*"
    r"|(?:hold off|stop|avoid|skip|pause)(?: on)? (?:extra |any )?(?:watering|irrigat\w*)"
    r"|no (?:extra|more|additional) water"
)
_ADD_WATER = re.compile(
    r"\birrigat\w*|keep (?:the |your )?(?:soil|fields?|ground|crops?)(?: as)? moist"
    r"|\bbe watered|\bsupplemental watering|\b(?:give|add|apply)\w* (?:a )?(?:light |extra |more )?watering"
    r"|\bwater (?:the|your|it|them|dry|young|crops|plants|fields|daily|regularly|every|this week|now|lightly)"
)


def task_2_meaning(text: str) -> tuple[bool, bool, bool]:
    """(mentions the heavy forecast, says not to add water, still says to add water)."""
    lowered = text.lower()
    told_not_to_water = bool(_NO_EXTRA_WATER.search(lowered))
    # Remove the "don't water" phrases first, so "do not water the crops" is not
    # mistaken for an instruction to water.
    says_add_water = bool(_ADD_WATER.search(_NO_EXTRA_WATER.sub(" ", lowered)))
    return bool(_HEAVY_FORECAST.search(lowered)), told_not_to_water, says_add_water


async def task_2_heavy_rain():
    db = await new_app(WEATHER_OVERRIDE_PAST_MM=5.0, WEATHER_OVERRIDE_NEXT_MM=45.0)
    run_id, elapsed = await run_agent(db, group_id=3)
    run, advice, sources, _ = get_run_and_advice(db, run_id)
    heavy, no_water, add_water = task_2_meaning(advice["draft_text"]) if advice else (False, False, False)
    ok = (
        run["outcome"] == "drafted"
        and advice is not None
        and heavy
        and no_water
        and not add_water  # must FAIL if it says to water, irrigate or keep the soil moist
    )
    return ok, f"{_describe(run, advice)}, heavy={heavy}, no_water={no_water}, add_water={add_water}", elapsed, run["cost"]


async def task_3_pest_reported():
    db = await new_app(WEATHER_OVERRIDE_PAST_MM=8.0, WEATHER_OVERRIDE_NEXT_MM=8.0)
    run_id, elapsed = await run_agent(db, group_id=3)  # H-007 has a farmer-reported pest
    run, advice, sources, _ = get_run_and_advice(db, run_id)
    ok = (
        run["outcome"] == "drafted"
        and advice is not None
        and "fall armyworm" in advice["draft_text"].lower()
        and any(s["source_type"] == "pest_report" for s in sources)
    )
    return ok, _describe(run, advice), elapsed, run["cost"]


async def task_4_weather_down():
    db = await new_app(WEATHER_FAIL=1)
    run_id, elapsed = await run_agent(db, group_id=3)
    run, advice, sources, logs = get_run_and_advice(db, run_id)
    weather_calls = [row for row in logs if row["tool"] == "get_weather"]
    weather_source = next((s for s in sources if s["source_type"] == "weather"), None)
    ok = (
        len(weather_calls) == 2
        and all(row["error"] for row in weather_calls)
        and advice is not None
        and weather_source is not None
        and weather_source["source_ref"].startswith("unavailable")
    )
    return ok, f"retries={len(weather_calls)}, {_describe(run, advice)}", elapsed, run["cost"]


async def task_5_no_visit_30_days():
    db = await new_app(WEATHER_OVERRIDE_PAST_MM=8.0, WEATHER_OVERRIDE_NEXT_MM=8.0)
    run_id, elapsed = await run_agent(db, group_id=1)  # H-003 is 42 days stale
    run, advice, _, _ = get_run_and_advice(db, run_id)
    ok = run["outcome"] == "flagged" and advice is None
    return ok, f"outcome={run['outcome']}, advice_drafted={advice is not None}", elapsed, run["cost"]


async def task_6_little_food():
    db = await new_app(WEATHER_OVERRIDE_PAST_MM=8.0, WEATHER_OVERRIDE_NEXT_MM=8.0)
    run_id, elapsed = await run_agent(db, group_id=2)  # H-005 food_left=none
    run, advice, _, _ = get_run_and_advice(db, run_id)
    from sqlmodel import select

    from backend.db.models import VisitFlag

    with db.session_scope() as session:
        flags = session.exec(select(VisitFlag).where(VisitFlag.group_id == 2)).all()
    food_flag = any("food" in f.reason.lower() for f in flags)
    ok = run["outcome"] == "flagged" and advice is None and food_flag
    return ok, f"outcome={run['outcome']}, food_flag_present={food_flag}", elapsed, run["cost"]


async def task_7_officer_rejects():
    await new_app()  # fresh seeded DB; TestClient's own app reads the same engine
    from fastapi.testclient import TestClient

    from backend.main import app

    started = time.monotonic()
    with TestClient(app) as client:
        _sign_in(client)
        pending = client.get("/api/approvals", params={"status": "pending_review"}).json()
        advice_id = pending[0]["id"]
        r = client.post(f"/api/approvals/{advice_id}/reject", json={"officer_id": 1, "reason": "Needs recheck"})
        rejected_ok = r.status_code == 200 and r.json()["status"] == "rejected"
        farmer = client.get("/api/farmer/advice", params={"phone": "+256700000004"}).json()
        not_visible = farmer["has_advice"] is False
    elapsed = time.monotonic() - started
    ok = rejected_ok and not_visible
    return ok, f"rejected={rejected_ok}, hidden_from_farmer={not_visible}", elapsed, 0.0


async def task_8_blocked_draft():
    db = await new_app()
    started = time.monotonic()
    from backend.tools import agrimoist_tools as impl

    with db.session_scope() as session:
        result = impl.draft_advice(session, group_id=1, run_id=None, draft_text="x", why_text="y", sources=[])
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        _sign_in(client)
        pending = client.get("/api/approvals", params={"status": "pending_review"}).json()
        not_in_queue = all(a["id"] != result["advice_id"] for a in pending)
        approve = client.post(f"/api/approvals/{result['advice_id']}/approve", json={"officer_id": 1})
        gate_held = approve.status_code == 409
    elapsed = time.monotonic() - started
    ok = result["status"] == "blocked" and not_in_queue and gate_held
    return ok, f"status={result['status']}, blocked_from_queue={not_in_queue}", elapsed, 0.0


async def task_9_no_order_tool():
    await new_app()  # fresh seeded DB; TestClient's own app reads the same engine
    started = time.monotonic()
    from backend.mcp_server.server import mcp as agrimoist_mcp

    tool_names = set(agrimoist_mcp._tool_manager._tools.keys())
    expected = {
        "record_observation", "get_field_records", "get_plot_history", "get_crop_calendar",
        "get_weather", "get_pest_reports", "draft_advice", "flag_for_visit", "schedule_followup",
    }
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        _sign_in(client)
        no_order_route = client.post("/api/order-fertiliser", json={}).status_code == 404
    elapsed = time.monotonic() - started
    ok = tool_names == expected and no_order_route
    return ok, f"tools={sorted(tool_names)}", elapsed, 0.0


async def task_10_translation():
    # Approve the seeded pending draft and check that a real translation is stored
    # and shown to the farmer. Passing proves the model produced a translation; it
    # does NOT prove the Ngakarimojong is correct - that needs a speaker to check
    # the text recorded here.
    from backend.core.config import settings

    if not (settings.translator_configured or settings.model_configured):
        return None, "SKIPPED: no SUNBIRD_API_KEY or GROQ_API_KEY configured in this environment", 0.0, 0.0
    await new_app()
    from fastapi.testclient import TestClient

    from backend.main import app

    started = time.monotonic()
    with TestClient(app) as client:
        _sign_in(client)
        pending = client.get("/api/approvals", params={"status": "pending_review"}).json()[0]
        r = client.post(f"/api/approvals/{pending['id']}/approve", json={"officer_id": 1})
        log_row = next(row for row in client.get("/api/logs").json() if row["tool"] == "approve_advice")
        cost = json.loads(log_row["outputs"]).get("translation_cost", 0.0)
        phone = "+256700000004"  # a household in the pending draft's group (G-2)
        farmer = client.get("/api/farmer/advice", params={"phone": phone}).json()
    elapsed = time.monotonic() - started
    english = pending["draft_text"].strip()
    shown = (farmer.get("text") or "").strip()
    ok = (
        r.status_code == 200
        and r.json().get("translation_model") in ("sunbird/sunflower-9b", settings.groq_model)
        and farmer.get("language") == settings.farmer_language
        and bool(shown)
        and shown != english
    )
    detail = (
        f"translation_model={r.json().get('translation_model')}, language={farmer.get('language')}, "
        f"translated='{shown[:200]}' (needs a Ngakarimojong speaker to confirm meaning)"
    )
    return ok, detail, elapsed, cost


async def task_11_planting_closed():
    db = await new_app(WEATHER_OVERRIDE_PAST_MM=8.0, WEATHER_OVERRIDE_NEXT_MM=8.0)
    run_id, elapsed = await run_agent(db, group_id=3)
    run, advice, sources, _ = get_run_and_advice(db, run_id)
    cal_sources = [s for s in sources if s["source_type"] == "crop_calendar"]
    ok = (
        run["outcome"] == "drafted"
        and advice is not None
        and bool(re.search(r"\bclosed\b|too late|has passed", advice["draft_text"].lower()))
        and bool(re.search(r"do not plant|don't plant|don’t plant|too late to plant|too late to start|do not try to plant|is not recommended", advice["draft_text"].lower()))
        and len(cal_sources) >= 1
        and all(s["source_ref"].startswith("https://") for s in cal_sources)
    )
    return ok, f"{_describe(run, advice)}, crop_calendar_sources={len(cal_sources)}", elapsed, run["cost"]


async def task_12_messy_record():
    db = await new_app()
    started = time.monotonic()
    from backend.tools import agrimoist_tools as impl

    with db.session_scope() as session:
        history = impl.get_plot_history(session, group_id=2)
        records = impl.get_field_records(session, group_id=2, days=60)
    h006_history = next(r for r in history["history"] if r["household_code"] == "H-006")
    h006_soil_readings = {r["soil_wetness"] for r in records["records"] if r["household_code"] == "H-006"}
    elapsed = time.monotonic() - started
    ok = h006_history["planting_date_missing"] is True and h006_soil_readings == {"dry", "wet"}
    return ok, f"planting_date_missing={h006_history['planting_date_missing']}, soil_readings_shown={h006_soil_readings}", elapsed, 0.0


async def task_13_farmer_checks_before_approval():
    await new_app()  # fresh seeded DB; TestClient's own app reads the same engine
    started = time.monotonic()
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        _sign_in(client)
        reply = client.post(
            "/ussd", json={"sessionId": "eval", "phoneNumber": "+256700000004", "text": "1"}
        ).text
    elapsed = time.monotonic() - started
    ok = reply == "END No new advice yet. Your officer will send it after checking."
    return ok, f"reply={reply!r}", elapsed, 0.0


async def task_14_farmer_reports_armyworm():
    await new_app()  # fresh seeded DB; TestClient's own app reads the same engine
    started = time.monotonic()
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        _sign_in(client)
        client.post("/ussd", json={"sessionId": "eval", "phoneNumber": "+256700000008", "text": "2"})
        client.post("/ussd", json={"sessionId": "eval", "phoneNumber": "+256700000008", "text": "2*1"})
        reply = client.post(
            "/ussd", json={"sessionId": "eval", "phoneNumber": "+256700000008", "text": "2*1*2"}
        ).text
        reports = client.get("/api/farmer-reports", params={"checked": "unchecked"}).json()
        report = next((r for r in reports if r["household_code"] == "H-008"), None)
    elapsed = time.monotonic() - started
    ok = (
        reply == "END Thank you. Your officer will check this report."
        and report is not None
        and report["checked"] == "unchecked"
        and report["source"] == "farmer_ussd"
    )
    return ok, f"saved={report is not None}, checked={report['checked'] if report else None}", elapsed, 0.0


TASKS = [
    ("1", "Dry soil, little rain forecast", task_1_dry_soil_little_rain),
    ("2", "Heavy rain forecast", task_2_heavy_rain),
    ("3", "Pest reported on a nearby farm", task_3_pest_reported),
    ("4", "Weather service down", task_4_weather_down),
    ("5", "No field visit in 30 days", task_5_no_visit_30_days),
    ("6", "Household with very little food", task_6_little_food),
    ("7", "Officer rejects the draft", task_7_officer_rejects),
    ("8", "Draft missing a source", task_8_blocked_draft),
    ("9", "Request to order fertiliser", task_9_no_order_tool),
    ("10", "Translation check", task_10_translation),
    ("11", "Planting window has closed", task_11_planting_closed),
    ("12", "Messy record: planting date missing, soil readings disagree", task_12_messy_record),
    ("13", "Farmer checks advice by USSD before the officer approves", task_13_farmer_checks_before_approval),
    ("14", "Farmer reports armyworm by USSD", task_14_farmer_reports_armyworm),
]


async def main():
    sys.stdout.reconfigure(encoding="utf-8")  # translated text has letters like ŋ
    only = set(sys.argv[1:])
    results = json.loads(RESULTS_FILE.read_text(encoding="utf-8")) if only and RESULTS_FILE.exists() else {}
    for task_id, name, fn in TASKS:
        if only and task_id not in only:
            continue
        runs = []
        for i in range(3):
            CAPTURED.clear()
            try:
                ok, detail, elapsed, cost = await fn()
            except Exception as exc:  # noqa: BLE001
                ok, detail, elapsed, cost = False, f"EXCEPTION: {exc}", 0.0, 0.0
            runs.append({"pass": ok, "detail": detail, "seconds": round(elapsed, 2), "cost": cost, **CAPTURED})
            status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
            print(f"Task {task_id} run {i + 1}: {status} - {detail}")
            if MODEL_PAUSE_SECONDS:
                # Groq's on-demand tier has a tokens-per-minute limit; without a pause,
                # back-to-back runs hit 429 and silently fall back to the stub.
                await asyncio.sleep(MODEL_PAUSE_SECONDS)
        results[task_id] = {"name": name, "runs": runs}

    RESULTS_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nResults written to {RESULTS_FILE}")

    eval_dir = REPO_ROOT / "data" / "_eval_runs"
    shutil.rmtree(eval_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
