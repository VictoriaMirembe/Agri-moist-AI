# Unit tests for the deterministic rule-based drafting logic in
# backend/agent/model_client.py - this is what runs whenever no GROQ_API_KEY is
# configured (the default in this environment), so it is the logic that actually
# drives every demo run's wording. Pure functions, no network, no database.
from __future__ import annotations

from backend.agent.model_client import _stub_draft


def test_dry_soil_little_rain_advises_watering():
    # EVALS task 1
    facts = {
        "weather": {"past_7_days_rain_mm": 1.0, "next_7_days_rain_mm": 2.0, "source_ref": "x"},
        "field_records": {"records": [{"household_code": "H-002", "soil_wetness": "dry"}]},
    }
    result = _stub_draft(facts)
    assert "water" in result["advice"].lower()
    assert "x" not in result["advice"]  # never leaks the raw source_ref into the text


def test_heavy_rain_forecast_skips_watering():
    # EVALS task 2
    facts = {"weather": {"past_7_days_rain_mm": 5.0, "next_7_days_rain_mm": 40.0, "source_ref": "x"}}
    result = _stub_draft(facts)
    assert "no extra watering" in result["advice"].lower()


def test_pest_report_is_mentioned():
    # EVALS task 3
    facts = {
        "weather": {"past_7_days_rain_mm": 10.0, "next_7_days_rain_mm": 10.0, "source_ref": "x"},
        "pest_reports_list": [{"pest": "fall armyworm", "crop": "maize", "household_code": "H-002"}],
    }
    result = _stub_draft(facts)
    assert "fall armyworm" in result["advice"]


def test_weather_gap_is_marked_not_guessed():
    # EVALS task 4 (decision-logic half; the retry/gap-source half is tested in
    # tests/test_agent_e2e.py::test_weather_failure_switch_marks_a_gap).
    facts = {"weather": {"gap": "weather missing after retry: boom"}}
    result = _stub_draft(facts)
    assert "missing" in result["advice"].lower()
    assert "check" in result["advice"].lower()
    # No invented numbers: the word "mm" (used for rain figures) must not appear.
    assert "mm" not in result["advice"]


def test_closed_planting_window_blocks_planting_advice():
    # EVALS task 11
    facts = {
        "weather": {"past_7_days_rain_mm": 10.0, "next_7_days_rain_mm": 10.0, "source_ref": "x"},
        "crop_calendar": {
            "maize": {
                "plant_from": "03-01",
                "plant_to": "05-31",
                "planting_window_closed_this_year": True,
            }
        },
    }
    result = _stub_draft(facts)
    assert "has closed" in result["advice"]
    assert "do not advise planting maize" in result["advice"].lower()


def test_no_facts_gives_an_honest_fallback_not_a_guess():
    result = _stub_draft({})
    assert result["advice"]
    assert "model" in result and result["model"] == "rule-based-stub"


def test_cost_uses_the_gpt_oss_120b_price(monkeypatch):
    from types import SimpleNamespace

    from backend.agent import model_client
    from backend.core.config import settings

    monkeypatch.setattr(settings, "groq_model", "openai/gpt-oss-120b")
    usage = SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert model_client._estimate_cost(usage) == 0.75  # $0.15 in + $0.60 out


def test_translation_without_a_key_reports_zero_cost():
    from backend.agent.model_client import translate_text

    result = translate_text("Water the sorghum.", "Ngakarimojong")
    assert result["translated_text"] is None
    assert result["cost"] == 0.0


def test_a_refusal_is_never_stored_as_a_translation(monkeypatch):
    from types import SimpleNamespace

    from backend.agent import model_client

    reply = "I’m sorry, but I don’t have enough knowledge of Ngakarimojong to provide an accurate translation."
    fake = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_: SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
                    usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
                )
            )
        )
    )
    monkeypatch.setattr(model_client, "_get_client", lambda: fake)
    result = model_client.translate_text("Water the sorghum.", "Ngakarimojong")
    assert result["translated_text"] is None  # farmer is shown the English text instead
    assert result["model"].startswith("fallback")
    assert result["cost"] > 0  # the tokens were still paid for


def _fake_sunbird(monkeypatch, translated_text, error=None):
    from types import SimpleNamespace

    from backend.agent import model_client
    from backend.core.config import settings

    sent = {}

    def fake_post(url, headers, json, timeout):
        sent.update(url=url, json=json)
        body = {"output": {"translated_text": translated_text, "Error": error}, "status": "COMPLETED"}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    monkeypatch.setattr(settings, "sunbird_api_key", "test-key")
    monkeypatch.setattr(model_client.httpx, "post", fake_post)
    return sent


def test_sunbird_translates_into_karamojong_first(monkeypatch):
    from backend.agent import model_client

    sent = _fake_sunbird(monkeypatch, "Kicakak akimyet ŋina 7.")
    result = model_client.translate_text("Water the sorghum within 7 days.", "Ngakarimojong")
    assert result["translated_text"] == "Kicakak akimyet ŋina 7."
    assert result["model"] == "sunbird/sunflower-9b"
    assert sent["url"].endswith("/tasks/translate")
    assert sent["json"]["target_language"] == "kdj"  # ISO code for Karamojong


def test_a_translation_that_changes_a_number_is_not_shown(monkeypatch):
    from backend.agent import model_client

    _fake_sunbird(monkeypatch, "Kicakak akimyet ŋina 5.")  # 7 became 5
    result = model_client.translate_text("Water the sorghum within 7 days.", "Ngakarimojong")
    assert result["translated_text"] is None  # farmer sees the English text instead
    assert "changed a number" in result["model"]


def test_sunbird_failure_falls_back_to_english_without_groq(monkeypatch):
    from backend.agent import model_client

    _fake_sunbird(monkeypatch, "", error="model loading")
    result = model_client.translate_text("Water the sorghum.", "Ngakarimojong")
    assert result["translated_text"] is None
    assert "sunbird could not translate" in result["model"]
