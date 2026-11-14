# SIMULATED irrigation loop (no real hardware): the agent may only PROPOSE irrigation;
# a proposal goes through the same approval gate as advice; only the approval executor
# (backend/approvals/irrigation_executor.py) opens a valve, through the separate
# irrigation gateway MCP server (backend/irrigation_server), after a named officer
# approves. Every valve closes itself, and any error leaves it closed.
from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from backend.db.models import Advice, Approval, IrrigationProposal, Run, ToolLog, Valve
from backend.irrigation_server import device
from backend.tools import agrimoist_tools as impl
from backend.tools.errors import ToolError

OFFICER_CODE = "*384*5952#"
VALVE_TOOLS = ("open_valve", "close_valve", "valve_status")


@pytest.fixture()
def weather(monkeypatch):
    """Force a rain scenario in this process and in any MCP subprocess."""
    from backend.core.config import settings

    def set_rain(past_mm: float, next_mm: float) -> None:
        monkeypatch.setattr(settings, "weather_override_past_mm", past_mm)
        monkeypatch.setattr(settings, "weather_override_next_mm", next_mm)
        monkeypatch.setenv("WEATHER_OVERRIDE_PAST_MM", str(past_mm))
        monkeypatch.setenv("WEATHER_OVERRIDE_NEXT_MM", str(next_mm))

    set_rain(1.0, 2.0)  # light rain unless a test says otherwise
    return set_rain


def _propose(session, plot: str = "H-009", minutes: int = 20) -> dict:
    return impl.propose_irrigation(session, group_id=3, plot=plot, minutes=minutes, run_id=None)


def _scope():
    from backend.core.database import session_scope

    return session_scope()


def _logs(tool: str) -> list[ToolLog]:
    with _scope() as session:
        return session.exec(select(ToolLog).where(ToolLog.tool == tool)).all()


def _valve(plot: str = "H-009") -> dict:
    with _scope() as session:
        return device.valve_status(session, plot=plot)


def _proposal(advice_id: int) -> IrrigationProposal:
    with _scope() as session:
        return session.exec(select(IrrigationProposal).where(IrrigationProposal.advice_id == advice_id)).one()


def _new_proposal(minutes: int = 20) -> int:
    with _scope() as session:
        result = _propose(session, minutes=minutes)
    assert result["proposed"], result
    return result["advice_id"]


def _dial(client, session_id: str, text: str) -> str:
    r = client.post(
        "/ussd",
        data={"sessionId": session_id, "serviceCode": OFFICER_CODE, "phoneNumber": "+256770000001", "text": text},
    )
    assert r.status_code == 200
    return r.text


# --- the rule: when a proposal is made (code, not the model) ----------------------------


def test_dry_current_soil_and_light_rain_makes_a_pending_proposal(seeded_session, weather):
    result = _propose(seeded_session)
    assert result["proposed"] is True and result["minutes"] == 20 and result["status"] == "pending_review"
    advice = seeded_session.get(Advice, result["advice_id"])
    assert advice.status == "pending_review"
    assert advice.draft_text.startswith("IRRIGATE H-009 20 min")
    proposal = seeded_session.exec(select(IrrigationProposal)).one()
    assert (proposal.plot_code, proposal.moisture_pct, proposal.forecast_mm) == ("H-009", 11.2, 2.0)
    assert proposal.execution == "not_started"
    # A proposal never opens a valve by itself.
    assert seeded_session.exec(select(Valve)).all() == []
    assert not [r for r in seeded_session.exec(select(ToolLog)).all() if r.tool in VALVE_TOOLS]


@pytest.mark.parametrize("next_mm", [45.0, 15.0])
def test_no_proposal_when_heavy_rain_is_forecast(seeded_session, weather, next_mm):
    weather(5.0, next_mm)  # 15.0 is exactly HEAVY_RAIN_THRESHOLD_MM: inclusive, as the rain guard
    result = _propose(seeded_session)
    assert result["proposed"] is False
    assert any(r.startswith("heavy rain is forecast") for r in result["reasons"])
    assert seeded_session.exec(select(IrrigationProposal)).all() == []


@pytest.mark.parametrize(
    "plot, reason",
    [
        ("H-008", "soil reading is out of date"),  # last read 30 hours ago
        ("H-010", "no soil reading for this plot"),
        ("H-007", "soil is not dry"),  # 28.4%
    ],
)
def test_no_proposal_when_the_soil_reading_is_stale_missing_or_not_dry(seeded_session, weather, plot, reason):
    result = _propose(seeded_session, plot=plot)
    assert result["proposed"] is False
    assert any(r.startswith(reason) for r in result["reasons"]), result["reasons"]
    assert seeded_session.exec(select(IrrigationProposal)).all() == []


def test_no_proposal_when_the_weather_is_a_gap(seeded_session, weather, monkeypatch):
    from backend.core.config import settings

    monkeypatch.setattr(settings, "weather_fail", True)
    result = _propose(seeded_session)
    assert result["proposed"] is False
    assert any(r.startswith("weather unavailable (a gap)") for r in result["reasons"])


def test_one_open_proposal_per_plot(seeded_session, weather):
    first = _propose(seeded_session)
    second = _propose(seeded_session)
    assert first["proposed"] and second["proposed"] is False
    assert any("open proposal already exists" in r for r in second["reasons"])


@pytest.mark.parametrize("asked, given", [(20, 20), (30, 30), (31, 30), (240, 30)])
def test_minutes_are_capped(seeded_session, weather, asked, given):
    result = _propose(seeded_session, minutes=asked)
    assert result["minutes"] == given and result["minutes_capped"] is (asked != given)
    assert seeded_session.get(Advice, result["advice_id"]).draft_text.startswith(f"IRRIGATE H-009 {given} min")


def test_minutes_must_be_positive(seeded_session, weather):
    with pytest.raises(ToolError):
        _propose(seeded_session, minutes=0)


def test_threshold_comes_from_config(seeded_session, weather, monkeypatch):
    from backend.core.config import settings

    monkeypatch.setattr(settings, "irrigation_dry_threshold_pct", 10.0)  # 11.2% is no longer dry
    assert _propose(seeded_session)["proposed"] is False


# --- no valve action without a named officer's approval ---------------------------------


def test_the_device_refuses_to_open_an_unapproved_proposal(seeded_session, weather):
    result = _propose(seeded_session)
    proposal = seeded_session.exec(select(IrrigationProposal)).one()
    proposal.execution = "opening"  # even if something claimed it, approval is still checked
    seeded_session.add(proposal)
    seeded_session.commit()
    with pytest.raises(ToolError, match="not been approved"):
        device.open_valve(seeded_session, plot="H-009", minutes=20, proposal_id=proposal.id)
    assert device.valve_status(seeded_session, plot="H-009")["state"] == "closed"
    assert seeded_session.get(Advice, result["advice_id"]).status == "pending_review"


@pytest.mark.asyncio
async def test_the_gateway_server_refuses_an_unapproved_proposal_over_mcp(test_db, weather):
    from backend.agent.mcp_client import IrrigationGatewayClient
    from backend.db.seed import seed

    test_db.init_db()
    with test_db.session_scope() as session:
        seed(session)
        _propose(session)
        proposal_id = session.exec(select(IrrigationProposal)).one().id
    async with IrrigationGatewayClient() as gateway:
        with pytest.raises(ToolError, match="not been approved"):
            await gateway.call("open_valve", plot="H-009", minutes=20, proposal_id=proposal_id,
                               officer_id=1, channel="test")
        assert (await gateway.call("valve_status", plot="H-009"))["state"] == "closed"
    rows = _logs("open_valve")
    assert len(rows) == 1 and "not been approved" in rows[0].error


def test_web_approval_opens_the_valve_and_logs_every_step(client, weather):
    advice_id = _new_proposal()
    r = client.post(f"/api/approvals/{advice_id}/approve", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved" and body["officer"] == "Among Betty"
    assert body["irrigation"]["opened"] is True and body["irrigation"]["valve"]["state"] == "open"
    assert "translation_model" not in body  # an instruction to the valve is never translated

    valve = _valve()
    assert valve["state"] == "open" and 19 * 60 <= valve["seconds_left"] <= 20 * 60
    assert _proposal(advice_id).execution == "opened"

    decision = json.loads(_logs("approve_advice")[0].inputs)
    assert decision["channel"] == "web" and decision["officer_id"] == 1
    executed = _logs("execute_irrigation")
    assert len(executed) == 1 and executed[0].actor == "1" and executed[0].error is None
    assert json.loads(executed[0].inputs)["channel"] == "web"
    opened = _logs("open_valve")
    assert len(opened) == 1 and opened[0].actor == "1" and opened[0].source == "irrigation-gateway (simulated)"
    assert json.loads(opened[0].inputs)["channel"] == "web"

    # The panel shows it, and farmers never see the proposal as advice.
    valves = client.get("/api/irrigation/valves").json()
    assert valves["simulated"] is True and valves["valves"][0]["state"] == "open"
    farmer = client.get("/api/farmer/advice", params={"phone": "+256700000009"}).json()
    assert farmer.get("advice_id") != advice_id


def test_rejection_needs_a_reason_and_leaves_the_valve_closed(client, weather):
    advice_id = _new_proposal()
    assert client.post(f"/api/approvals/{advice_id}/reject", json={"reason": "  "}).status_code == 422
    r = client.post(f"/api/approvals/{advice_id}/reject", json={"reason": "Rain expected locally"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert _valve()["state"] == "closed"
    assert _logs("execute_irrigation") == [] and _logs("open_valve") == []
    assert json.loads(_logs("reject_advice")[0].inputs)["reason"] == "Rain expected locally"


def test_a_proposal_cannot_be_edited(client, weather):
    advice_id = _new_proposal()
    r = client.post(f"/api/approvals/{advice_id}/edit", json={"final_text": "IRRIGATE H-009 300 min"})
    assert r.status_code == 409
    with _scope() as session:
        assert session.get(Advice, advice_id).status == "pending_review"
    assert _valve()["state"] == "closed"


def test_an_expired_proposal_cannot_open_a_valve(client, weather):
    from backend.core.config import settings

    advice_id = _new_proposal()
    with _scope() as session:
        advice = session.get(Advice, advice_id)
        advice.created_at = datetime.now(timezone.utc) - timedelta(hours=settings.draft_expiry_hours + 1)
        session.add(advice)
        session.commit()
    r = client.post(f"/api/approvals/{advice_id}/approve", json={})
    assert r.status_code == 409 and r.json()["detail"] == "Expired: please re-draft"
    assert _valve()["state"] == "closed"
    assert _logs("open_valve") == [] and _logs("execute_irrigation") == []
    with _scope() as session:
        assert session.get(Advice, advice_id).status == "expired"
        # Once expired it is no longer open, so a fresh proposal may be made.
        assert _propose(session)["proposed"] is True


def test_decide_once_opens_the_valve_once(client, weather):
    advice_id = _new_proposal()
    assert client.post(f"/api/approvals/{advice_id}/approve", json={}).status_code == 200
    again = client.post(f"/api/approvals/{advice_id}/approve", json={})
    assert again.status_code == 409
    with _scope() as session:
        assert len(session.exec(select(Approval).where(Approval.advice_id == advice_id)).all()) == 1
    assert len(_logs("open_valve")) == 1 and len(_logs("execute_irrigation")) == 1


def test_the_executor_runs_once_per_proposal(client, weather):
    from backend.approvals.irrigation_executor import execute_approved_proposal
    from backend.db.models import Officer

    advice_id = _new_proposal()
    client.post(f"/api/approvals/{advice_id}/approve", json={})
    with _scope() as session:
        proposal = session.exec(select(IrrigationProposal)).one()
        second = execute_approved_proposal(session, proposal, session.get(Officer, 1), "web")
    assert second["opened"] is False and "nothing done" in second["error"]
    assert len(_logs("open_valve")) == 1


# --- the simulated timer --------------------------------------------------------------


def _open_then_age(client, minutes_ago: int = 1) -> int:
    advice_id = _new_proposal()
    assert client.post(f"/api/approvals/{advice_id}/approve", json={}).json()["irrigation"]["opened"]
    with _scope() as session:
        valve = session.get(Valve, "H-009")
        valve.close_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        session.add(valve)
        session.commit()
    return advice_id


def test_the_valve_closes_itself_when_its_time_is_up(client, weather):
    from backend.irrigation_server.timer import sweep_once

    advice_id = _open_then_age(client)
    assert sweep_once() == ["H-009"]
    assert _valve()["state"] == "closed"
    assert _proposal(advice_id).execution == "closed"
    auto = _logs("auto_close_valve")
    assert len(auto) == 1 and auto[0].actor == "system"
    assert sweep_once() == []  # nothing left to close


def test_a_valve_still_open_is_not_closed_early(client, weather):
    from backend.irrigation_server.timer import sweep_once

    advice_id = _new_proposal()
    client.post(f"/api/approvals/{advice_id}/approve", json={})
    assert sweep_once() == [] and _valve()["state"] == "open"


def test_a_restart_closes_a_valve_whose_time_passed_while_down(test_db, weather):
    """The close time is stored on the valve, so starting the app again closes it."""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as first:
        first.post("/api/auth/login", json={"officer_id": 1, "password": "napak2026"})
        _open_then_age(first, minutes_ago=5)
        assert _valve()["state"] == "open"  # the app's timer has not run yet
    with TestClient(app):  # start-up
        assert _valve()["state"] == "closed"
    assert len(_logs("auto_close_valve")) == 1


def test_the_valve_panel_closes_due_valves_before_answering(client, weather):
    _open_then_age(client)
    assert client.get("/api/irrigation/valves").json()["valves"][0]["state"] == "closed"


# --- fail closed --------------------------------------------------------------------


@pytest.mark.parametrize("fault", ["open_fails", "no_confirmation"])
def test_any_gateway_error_leaves_the_valve_closed(client, weather, monkeypatch, fault):
    monkeypatch.setenv("IRRIGATION_GATEWAY_FAULT", fault)  # read by the gateway subprocess
    advice_id = _new_proposal()
    r = client.post(f"/api/approvals/{advice_id}/approve", json={})
    assert r.status_code == 200  # the officer's approval is recorded...
    body = r.json()
    assert body["status"] == "approved"
    assert body["irrigation"]["opened"] is False  # ...but the valve stays shut
    assert body["irrigation"]["valve"]["state"] == "closed"
    assert _valve()["state"] == "closed"
    proposal = _proposal(advice_id)
    assert proposal.execution == "failed" and "simulated fault" in proposal.execution_error
    executed = _logs("execute_irrigation")
    assert len(executed) == 1 and "Valve left CLOSED" in executed[0].error
    if fault == "no_confirmation":  # it really had opened: the executor closed it again
        assert len(_logs("close_valve")) == 1


def test_valve_is_forced_closed_when_the_gateway_cannot_even_close(client, weather, monkeypatch):
    from backend.approvals import irrigation_executor

    async def broken_gateway(tool, **arguments):
        if tool == "open_valve":  # it opens, then the gateway goes silent
            with _scope() as session:
                device.open_valve(session, plot=arguments["plot"], minutes=arguments["minutes"],
                                  proposal_id=arguments["proposal_id"])
        raise ToolError(f"{tool}: gateway unreachable")

    monkeypatch.setattr(irrigation_executor, "_gateway_call", broken_gateway)
    advice_id = _new_proposal()
    body = client.post(f"/api/approvals/{advice_id}/approve", json={}).json()
    assert body["irrigation"]["opened"] is False
    assert _valve()["state"] == "closed"
    forced = _logs("force_close_valve")
    assert len(forced) == 1 and json.loads(forced[0].inputs)["channel"] == "web"
    assert _proposal(advice_id).execution == "failed"


# --- USSD ---------------------------------------------------------------------------


def test_ussd_lists_the_proposal_and_approval_opens_the_valve(unauthenticated_client, weather):
    phone = unauthenticated_client
    advice_id = _new_proposal()
    listing = ""
    for text in ("", "1", "1*1234"):
        listing = _dial(phone, "irr-1", text)
    assert listing.startswith("CON Drafts waiting:\n1. IRRIGATE H-009 20 min")
    shown = _dial(phone, "irr-1", "1*1234*1")
    assert "IRRIGATE H-009 20 min" in shown and "1. Approve" in shown
    reply = _dial(phone, "irr-1", "1*1234*1*1")
    assert reply == "END Approved by Among Betty. Valve H-009 open for 20 min (simulated), then it closes."
    assert _valve()["state"] == "open"
    assert json.loads(_logs("approve_advice")[0].inputs)["channel"] == "ussd"
    assert json.loads(_logs("open_valve")[0].inputs)["channel"] == "ussd"
    # The gateway resends the same request: decided once, the valve opened once.
    assert _dial(phone, "irr-1", "1*1234*1*1") == "END Already approved: nothing changed."
    assert len(_logs("open_valve")) == 1
    with _scope() as session:
        assert session.get(Advice, advice_id).status == "approved"


def test_ussd_rejection_leaves_the_valve_closed(unauthenticated_client, weather):
    phone = unauthenticated_client
    _new_proposal()
    reply = ""
    for text in ("", "1", "1*1234", "1*1234*1", "1*1234*1*2", "1*1234*1*2*4"):
        reply = _dial(phone, "irr-2", text)
    assert reply == "END Rejected by Among Betty: Out of date. The valve stays closed."
    assert _valve()["state"] == "closed" and _logs("open_valve") == []


def test_ussd_cannot_open_an_expired_proposal(unauthenticated_client, weather):
    from backend.core.config import settings

    phone = unauthenticated_client
    advice_id = _new_proposal()
    for text in ("", "1", "1*1234", "1*1234*1"):
        _dial(phone, "irr-3", text)
    with _scope() as session:
        advice = session.get(Advice, advice_id)
        advice.created_at = datetime.now(timezone.utc) - timedelta(hours=settings.draft_expiry_hours + 1)
        session.add(advice)
        session.commit()
    assert _dial(phone, "irr-3", "1*1234*1*1") == "END Expired: please re-draft"
    assert _valve()["state"] == "closed" and _logs("open_valve") == []


# --- the agent cannot reach the valve tools -----------------------------------------


async def _agent_run(test_db, group_id: int = 3) -> int:
    from backend.agent.graph import execute_agent_run
    from backend.db.seed import seed

    test_db.init_db()
    with test_db.session_scope() as session:
        seed(session)
        run = Run(group_id=group_id, model="rule-based-stub")
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id
    await execute_agent_run(group_id=group_id, run_id=run_id)
    return run_id


@pytest.mark.asyncio
async def test_agent_run_proposes_but_never_calls_a_valve_tool(test_db, weather):
    run_id = await _agent_run(test_db)
    with test_db.session_scope() as session:
        assert session.get(Run, run_id).outcome == "drafted"
        proposal = session.exec(select(IrrigationProposal)).one()
        assert proposal.plot_code == "H-009" and proposal.run_id == run_id and proposal.minutes == 20
        assert session.get(Advice, proposal.advice_id).status == "pending_review"
        tools = [row.tool for row in session.exec(select(ToolLog)).all()]
        assert tools.count("propose_irrigation") == 3  # H-007, H-008, H-009: the tool decides
        assert not set(tools) & {*VALVE_TOOLS, "execute_irrigation", "auto_close_valve"}
        assert session.exec(select(Valve)).all() == []


@pytest.mark.asyncio
async def test_agent_run_with_heavy_rain_makes_no_proposal(test_db, weather):
    weather(5.0, 45.0)
    run_id = await _agent_run(test_db)
    with test_db.session_scope() as session:
        assert session.exec(select(IrrigationProposal)).all() == []
        outputs = [json.loads(r.outputs) for r in session.exec(
            select(ToolLog).where(ToolLog.run_id == run_id, ToolLog.tool == "propose_irrigation")).all()]
        h009 = next(o for o in outputs if o["plot"] == "H-009")
        assert h009["proposed"] is False and h009["reasons"][0].startswith("heavy rain is forecast (45 mm")


@pytest.mark.asyncio
async def test_agent_run_with_a_weather_gap_proposes_nothing(test_db, weather, monkeypatch):
    monkeypatch.setenv("WEATHER_FAIL", "1")
    run_id = await _agent_run(test_db)
    with test_db.session_scope() as session:
        assert session.exec(select(IrrigationProposal)).all() == []
        assert not session.exec(
            select(ToolLog).where(ToolLog.run_id == run_id, ToolLog.tool == "propose_irrigation")).all()


@pytest.mark.asyncio
async def test_the_agents_client_has_no_valve_tools(test_db):
    from backend.agent.mcp_client import ToolClient
    from backend.db.seed import seed

    test_db.init_db()
    with test_db.session_scope() as session:
        seed(session)
    async with ToolClient() as client:
        names = {t["name"] for server in await client.list_servers() for t in server["tools"]}
        assert "propose_irrigation" in names and not names & set(VALVE_TOOLS)
        with pytest.raises(ToolError):
            await client.call("open_valve", plot="H-009", minutes=20, proposal_id=1, officer_id=1, channel="agent")
    assert _logs("open_valve") == []


def test_the_agent_code_never_names_the_gateway():
    from backend.agent import graph, mcp_client

    source = inspect.getsource(graph)
    assert "IrrigationGatewayClient" not in source and "irrigation_executor" not in source
    for tool in ("open_valve", "close_valve"):
        assert f'"{tool}"' not in source
    assert "_irrigation_server_params" not in inspect.getsource(mcp_client.ToolClient)


@pytest.mark.asyncio
async def test_the_gateway_offers_exactly_three_tools(test_db):
    from backend.agent.mcp_client import IrrigationGatewayClient

    test_db.init_db()
    async with IrrigationGatewayClient() as gateway:
        described = await gateway.describe()
    assert described["name"] == "irrigation-gateway"
    assert {t["name"] for t in described["tools"]} == set(VALVE_TOOLS)


def test_the_valve_panel_needs_an_officer(unauthenticated_client):
    assert unauthenticated_client.get("/api/irrigation/valves").status_code == 403


def test_the_approvals_page_shows_proposals_and_the_valve_panel(client, weather):
    _new_proposal()
    item = next(a for a in client.get("/api/approvals", params={"status": "pending_review"}).json()
                if a["kind"] == "irrigation")
    assert item["irrigation"]["label"] == "IRRIGATE H-009 20 min"
    assert item["irrigation"]["moisture_pct"] == 11.2 and item["irrigation"]["forecast_mm"] == 2.0
    assert item["irrigation"]["valve"]["state"] == "closed"
    page = client.get("/approvals").text
    assert "renderIrrigation" in page and "/api/irrigation/valves" in page and "Simulated" in page
