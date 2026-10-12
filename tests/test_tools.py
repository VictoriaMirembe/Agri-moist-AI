# Unit tests for backend/tools/agrimoist_tools.py - the pure functions behind every
# MCP tool. Fast, no subprocess, no network except the one real get_weather call.
from __future__ import annotations

from datetime import date

import pytest

from backend.core.config import settings
from backend.db.models import Household
from backend.tools import agrimoist_tools as impl
from backend.tools.errors import ToolError


def test_record_observation_officer_visit_is_checked(seeded_session):
    result = impl.record_observation(
        seeded_session, household_code="H-001", source="officer_visit", recorded_by=1, soil_wetness="dry"
    )
    assert result["checked"] == "checked"
    assert result["group_id"] == 1


def test_record_observation_farmer_report_is_unchecked(seeded_session):
    result = impl.record_observation(
        seeded_session, household_code="H-007", source="farmer_ussd", pest_seen="fall armyworm"
    )
    assert result["checked"] == "unchecked"


def test_record_observation_unknown_household_raises(seeded_session):
    with pytest.raises(ToolError):
        impl.record_observation(seeded_session, household_code="H-999", source="officer_visit")


def test_record_observation_requires_consent(seeded_session):
    household = seeded_session.get(Household, 1)
    household.consent_given = False
    seeded_session.add(household)
    seeded_session.commit()
    with pytest.raises(ToolError, match="consent"):
        impl.record_observation(seeded_session, household_code=household.code, source="officer_visit")


def test_get_field_records_flags_household_overdue_a_visit(seeded_session):
    # H-003 is seeded 42 days ago - EVALS task 5 ("no field visit in 30 days").
    result = impl.get_field_records(seeded_session, group_id=1, days=60)
    days_since = result["days_since_last_visit_by_household"]
    assert days_since["H-003"] > 30


def test_get_field_records_shows_little_food(seeded_session):
    # H-005 is seeded with food_left=none - EVALS task 6 ("very little food").
    result = impl.get_field_records(seeded_session, group_id=2, days=60)
    food_left = {r["household_code"]: r["food_left"] for r in result["records"]}
    assert food_left["H-005"] == "none"


def test_get_plot_history_flags_missing_planting_date(seeded_session):
    # H-006's plot history has no planted_on on purpose - EVALS task 12 (messy data).
    result = impl.get_plot_history(seeded_session, group_id=2)
    row = next(r for r in result["history"] if r["household_code"] == "H-006")
    assert row["planting_date_missing"] is True


def test_get_crop_calendar_unknown_crop_raises(seeded_session):
    with pytest.raises(ToolError):
        impl.get_crop_calendar(seeded_session, zone="Karamoja agro-pastoral", crop="coffee")


def test_get_crop_calendar_planting_window_closed_in_october(seeded_session):
    # EVALS task 11: the planting window has closed. Both seeded crops' planting
    # windows end in May/June, so this is true for any real run between roughly July
    # and the following February - true on every date this build will be evaluated.
    result = impl.get_crop_calendar(seeded_session, zone="Karamoja agro-pastoral", crop="sorghum")
    if date.today().month in (7, 8, 9, 10, 11, 12, 1, 2):
        assert result["planting_window_closed_this_year"] is True
    assert result["source_ref"].startswith("https://")


def test_get_pest_reports_includes_unchecked_farmer_report(seeded_session):
    # H-007 has a farmer_ussd pest report - EVALS tasks 3 and 14.
    result = impl.get_pest_reports(seeded_session, group_id=3, days=21)
    pest_names = {r["pest"] for r in result["reports"]}
    assert "fall armyworm" in pest_names
    farmer_reported = [r for r in result["reports"] if r["reported_via"] == "farmer_ussd"]
    assert farmer_reported and farmer_reported[0]["checked"] == "unchecked"


def test_draft_advice_blocked_when_a_fact_has_no_source(seeded_session):
    # EVALS task 8: a draft with any fact missing a source is blocked.
    result = impl.draft_advice(
        seeded_session,
        group_id=1,
        run_id=None,
        draft_text="Water this week.",
        why_text="Because it is dry.",
        sources=[{"fact": "soil was dry", "source_type": "visit", "source_ref": ""}],  # missing ref
    )
    assert result["status"] == "blocked"


def test_draft_advice_blocked_when_no_sources_at_all(seeded_session):
    result = impl.draft_advice(
        seeded_session, group_id=1, run_id=None, draft_text="Water this week.", why_text="Just because.", sources=[]
    )
    assert result["status"] == "blocked"


def test_draft_advice_pending_review_when_fully_sourced(seeded_session):
    result = impl.draft_advice(
        seeded_session,
        group_id=1,
        run_id=None,
        draft_text="Water this week.",
        why_text="Soil was dry at the last visit.",
        sources=[{"fact": "soil dry", "source_type": "visit", "source_ref": "observation:1"}],
    )
    assert result["status"] == "pending_review"


def test_flag_for_visit_creates_open_flag(seeded_session):
    result = impl.flag_for_visit(seeded_session, group_id=1, reason="test reason")
    assert result["status"] == "open"


def test_schedule_followup_creates_row(seeded_session):
    # advice_id=1 is the seeded approved advice for group 1.
    result = impl.schedule_followup(seeded_session, advice_id=1, due_on=date(2026, 12, 1))
    assert result["due_on"] == "2026-12-01"


def test_get_weather_failure_switch(monkeypatch):
    # T23 / EVALS task 4: the demo failure switch makes get_weather fail.
    monkeypatch.setattr(settings, "weather_fail", True)
    with pytest.raises(ToolError):
        impl.get_weather(latitude=2.38, longitude=34.33)


def test_get_weather_real_call_has_a_source():
    # Open-Meteo needs no key; this is a real network call.
    result = impl.get_weather(latitude=2.3833, longitude=34.3333)
    assert "past_7_days_rain_mm" in result
    assert "next_7_days_rain_mm" in result
    assert result["source_ref"].startswith("https://api.open-meteo.com/")


def test_draft_advice_blocks_a_number_no_source_contains(seeded_session):
    sources = [{"fact": "Rain last 7 days 3.2mm, next 7 days forecast 18.0mm", "source_type": "weather", "source_ref": "open-meteo"}]
    invented = impl.draft_advice(
        seeded_session, group_id=1, run_id=None,
        draft_text="Expect 40mm of rain this week.", why_text="Forecast.", sources=sources,
    )
    assert invented["status"] == "blocked"
    assert "40" in invented["blocked_reason"]

    sourced = impl.draft_advice(
        seeded_session, group_id=1, run_id=None,
        draft_text="Only 3.2mm fell; 18mm is forecast over the next 7 days.", why_text="Forecast.", sources=sources,
    )
    assert sourced["status"] == "pending_review"
    assert sourced["blocked_reason"] is None


def test_list_numbering_is_not_treated_as_an_invented_number(seeded_session):
    sources = [{"fact": "Rain last 7 days 8.0mm", "source_type": "weather", "source_ref": "open-meteo"}]
    result = impl.draft_advice(
        seeded_session, group_id=1, run_id=None,
        draft_text="1. Check sorghum daily.\n2. Only 8.0mm fell in the last 7 days.", why_text="Rain.", sources=sources,
    )
    assert result["status"] == "pending_review"
