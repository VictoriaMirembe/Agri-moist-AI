# Token counts for every model call: the provider's own prompt/completion/total
# numbers, shown next to the cost on the run page and recorded in the existing log
# entry for that run. Logging only - no draft, decision or safety check depends on
# them. A rule-based stub run has no usage object, so it must report 0, never a guess.
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.agent import model_client
from backend.agent.model_client import ZERO_TOKENS, _usage_tokens


def _usage(prompt: int, completion: int, total: int | None = None) -> SimpleNamespace:
    """A mocked provider usage object, like OpenAI's/Groq's response.usage."""
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total if total is not None else prompt + completion,
    )


# ---------------------------------------------------------------------------
# The numbers themselves
# ---------------------------------------------------------------------------
def test_mocked_usage_object_gives_the_provider_numbers():
    assert _usage_tokens(_usage(1234, 567)) == {
        "prompt_tokens": 1234,
        "completion_tokens": 567,
        "total_tokens": 1801,
    }


def test_the_providers_own_total_is_kept_even_when_it_is_not_the_sum():
    # Some providers count cached or reasoning tokens in the total; report theirs.
    assert _usage_tokens(_usage(100, 20, total=130))["total_tokens"] == 130


def test_a_usage_dict_works_too():
    # Sunbird's translation API reports usage as JSON, not an object.
    assert _usage_tokens({"total_tokens": 42}) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 42,
    }


def test_no_usage_is_zero_not_a_guess():
    assert _usage_tokens(None) == ZERO_TOKENS


def test_a_stub_draft_reports_zero_tokens():
    drafted = model_client._stub_draft({})
    assert drafted["model"] == "rule-based-stub"
    assert (drafted["prompt_tokens"], drafted["completion_tokens"], drafted["total_tokens"]) == (0, 0, 0)
    assert drafted["cost"] == 0.0


def test_drafting_with_a_mocked_model_returns_tokens_and_cost(monkeypatch):
    from backend.core.config import settings

    monkeypatch.setattr(settings, "groq_model", "openai/gpt-oss-120b")
    reply = json.dumps({"advice": "Water the sorghum.", "why": "Little rain fell."})
    fake = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_: SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
                    usage=_usage(2000, 500),
                )
            )
        )
    )
    monkeypatch.setattr(model_client, "_get_client", lambda: fake)
    drafted = model_client.draft_advice_text({"weather": {}})
    assert drafted["model"] == "openai/gpt-oss-120b"
    assert drafted["prompt_tokens"] == 2000
    assert drafted["completion_tokens"] == 500
    assert drafted["total_tokens"] == 2500
    # $0.15/M in + $0.60/M out, unchanged by this feature.
    assert drafted["cost"] == pytest.approx(2000 / 1e6 * 0.15 + 500 / 1e6 * 0.60)


def test_a_failed_model_call_falls_back_to_the_stub_with_zero_tokens(monkeypatch):
    def boom(**_):
        raise RuntimeError("no network")

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
    monkeypatch.setattr(model_client, "_get_client", lambda: fake)
    drafted = model_client.draft_advice_text({"weather": {}})
    assert drafted["model"].startswith("rule-based-stub")
    assert drafted["total_tokens"] == 0


def test_translation_tokens_add_up_across_attempts(monkeypatch):
    # Sunbird is tried twice and changes the number both times, then Groq succeeds:
    # every attempt was paid for, so every attempt's tokens count.
    from backend.core.config import settings

    monkeypatch.setattr(settings, "sunbird_api_key", "test-key")
    monkeypatch.setattr(settings, "groq_model", "openai/gpt-oss-120b")

    def fake_post(url, headers, json, timeout):
        body = {"output": {"translated_text": "Kicakak 5."}, "usage": {"total_tokens": 30}}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    monkeypatch.setattr(model_client.httpx, "post", fake_post)
    fake = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_: SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="Kicakak 7."))],
                    usage=_usage(60, 15),
                )
            )
        )
    )
    monkeypatch.setattr(model_client, "_get_client", lambda: fake)

    result = model_client.translate_text("Water the sorghum within 7 days.", "Ngakarimojong")
    assert result["translated_text"] == "Kicakak 7."
    assert result["model"] == "openai/gpt-oss-120b"
    assert result["total_tokens"] == 30 + 30 + 75  # two rejected Sunbird tries, then Groq
    assert result["prompt_tokens"] == 60 and result["completion_tokens"] == 15


def test_translation_without_a_key_reports_zero_tokens():
    result = model_client.translate_text("Water the sorghum.", "Ngakarimojong")
    assert result["translated_text"] is None
    assert (result["prompt_tokens"], result["completion_tokens"], result["total_tokens"]) == (0, 0, 0)


# ---------------------------------------------------------------------------
# On the run page and in the log
# ---------------------------------------------------------------------------
def _sources() -> list[dict]:
    return [
        {
            "fact": "Rain in the last 7 days 2.0 mm, next 7 days 3.0 mm",
            "source_type": "weather",
            "source_ref": "open-meteo:2.72,34.12",
        }
    ]


def _run_with_a_draft(model: str) -> int:
    """One run with a real draft_advice log row, written by the MCP tool itself -
    the row the model's token counts are added to."""
    from backend.core.database import session_scope
    from backend.db.models import Run
    from backend.mcp_server.server import draft_advice as mcp_draft_advice

    with session_scope() as session:
        run = Run(group_id=3, model=model, outcome="drafted")
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id
    mcp_draft_advice(
        group_id=3,
        draft_text="Rain has been light recently (2.0mm past week, 3.0mm forecast); watch soil before watering.",
        why_text="Based on the rain forecast.",
        sources=_sources(),
        run_id=run_id,
    )
    return run_id


def test_mocked_usage_shows_the_right_numbers_on_the_run_page_and_in_the_log(client):
    """A mocked usage object's numbers reach GET /api/runs/{id} (which the run page
    reads), the run's existing draft_advice log entry, and the activity log."""
    from backend.agent import graph

    run_id = _run_with_a_draft("openai/gpt-oss-120b")
    drafted = {
        "advice": "Water the sorghum this week.",
        "why": "Little rain fell in the last 7 days.",
        "model": "openai/gpt-oss-120b",
        "cost": 0.000345,
        **_usage_tokens(_usage(2048, 310)),
    }
    graph._record_model_usage(run_id, drafted)

    run = client.get(f"/api/runs/{run_id}").json()
    assert run["model"] == "openai/gpt-oss-120b"
    assert run["prompt_tokens"] == 2048
    assert run["completion_tokens"] == 310
    assert run["total_tokens"] == 2358
    assert run["model_cost"] == 0.000345

    listed = next(r for r in client.get("/api/runs").json() if r["id"] == run_id)
    assert listed["total_tokens"] == 2358

    row = next(
        r for r in client.get("/api/logs", params={"run_id": run_id}).json() if r["tool"] == "draft_advice"
    )
    outputs = json.loads(row["outputs"])
    assert outputs["model"] == "openai/gpt-oss-120b"
    assert (outputs["prompt_tokens"], outputs["completion_tokens"], outputs["total_tokens"]) == (2048, 310, 2358)
    assert outputs["model_cost"] == 0.000345
    assert outputs["advice_id"] and outputs["status"] == "pending_review"  # the tool's own output, untouched

    log_page = client.get("/log").text
    assert "Tokens (prompt + completion = total)" in log_page
    assert "2048 + 310 = <strong>2358</strong>" in log_page

    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    assert "showUsage" in page.text  # the run page renders the numbers it is given


def test_a_stub_run_shows_zero_tokens_not_a_guess(client):
    """No GROQ_API_KEY in tests (conftest forces stub mode), so a run is rule-based:
    there is no usage object, and every token number must be 0."""
    from backend.agent import graph

    run_id = _run_with_a_draft("rule-based-stub")
    graph._record_model_usage(run_id, model_client._stub_draft({}))

    run = client.get(f"/api/runs/{run_id}").json()
    assert run["model"] == "rule-based-stub"
    assert (run["prompt_tokens"], run["completion_tokens"], run["total_tokens"]) == (0, 0, 0)
    assert run["model_cost"] == 0.0

    row = next(
        r for r in client.get("/api/logs", params={"run_id": run_id}).json() if r["tool"] == "draft_advice"
    )
    outputs = json.loads(row["outputs"])
    assert outputs["model"] == "rule-based-stub"
    assert (outputs["prompt_tokens"], outputs["completion_tokens"], outputs["total_tokens"]) == (0, 0, 0)
    assert "0 + 0 = <strong>0</strong>" in client.get("/log").text


def test_a_run_with_no_usage_recorded_is_not_reported_as_zero_tokens(client):
    """An older run, or one that never drafted, has no usage on its log row: the API
    reports zeros but the activity log says "not recorded" rather than claiming 0."""
    run_id = _run_with_a_draft("rule-based-stub")  # draft row written, usage never added
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["total_tokens"] == 0 and run["model_cost"] == 0.0
    assert "not recorded" in client.get("/log").text


@pytest.mark.asyncio
async def test_the_agent_records_the_drafting_models_tokens_on_its_own_run(test_db):
    """The whole flow, over the real MCP protocol: group 3 drafts, and the run's
    draft_advice log row carries the drafting call's model, tokens and cost. Tests
    run in stub mode, so the honest numbers here are 0."""
    from backend.agent.graph import execute_agent_run
    from backend.core.database import session_scope
    from backend.db.models import Run
    from backend.logging.tool_log import model_usage_for_run

    test_db.init_db()
    with test_db.session_scope() as session:
        from backend.db.seed import seed

        seed(session)
        run = Run(group_id=3, model="rule-based-stub")
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id

    await execute_agent_run(group_id=3, run_id=run_id)

    with session_scope() as session:
        assert session.get(Run, run_id).outcome == "drafted"
        usage = model_usage_for_run(session, run_id)
    assert usage["model"] == "rule-based-stub"
    assert (usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]) == (0, 0, 0)
    assert usage["model_cost"] == 0.0


def test_translation_tokens_are_in_the_translation_log_entry(client, fake_translator):
    fake_translator(
        {
            "translated_text": "(kdj)",
            "model": "sunbird/sunflower-9b",
            "cost": 0.000042,
            "prompt_tokens": 18,
            "completion_tokens": 24,
            "total_tokens": 42,
        }
    )
    pending = client.get("/api/approvals", params={"status": "pending_review"}).json()[0]
    approved = client.post(f"/api/approvals/{pending['id']}/approve", json={"officer_id": 1})
    assert approved.status_code == 200, approved.text

    row = next(r for r in client.get("/api/logs").json() if r["tool"] == "translate_advice")
    outputs = json.loads(row["outputs"])
    assert outputs["translation_model"] == "sunbird/sunflower-9b"
    assert outputs["translation_cost"] == 0.000042  # already logged before this change
    assert outputs["translation_prompt_tokens"] == 18
    assert outputs["translation_completion_tokens"] == 24
    assert outputs["translation_total_tokens"] == 42
    assert "translation_total_tokens" in client.get("/log").text  # visible in the Details fold


def test_an_approval_whose_translator_reports_no_tokens_logs_zeros(client, fake_translator):
    """A translator that sends no usage must log 0, not crash."""
    fake_translator({"translated_text": "(kdj)", "model": "sunbird/sunflower-9b", "cost": 0.0004})
    pending = client.get("/api/approvals", params={"status": "pending_review"}).json()[0]
    assert client.post(f"/api/approvals/{pending['id']}/approve", json={"officer_id": 1}).status_code == 200

    row = next(r for r in client.get("/api/logs").json() if r["tool"] == "translate_advice")
    assert json.loads(row["outputs"])["translation_total_tokens"] == 0
