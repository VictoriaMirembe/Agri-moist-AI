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
