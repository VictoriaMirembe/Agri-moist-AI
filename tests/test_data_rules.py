# Structural checks for the data and consent rules in RULES.md section 3.
from __future__ import annotations

from pathlib import Path

from backend.db.models import Household


def test_household_has_no_exact_location_fields():
    # "Locations are stored at village level only, never a household's exact
    # location." Household must not carry latitude/longitude at all - only Village
    # does (see DATA_SCHEMA.md).
    field_names = set(Household.model_fields.keys())
    assert "latitude" not in field_names
    assert "longitude" not in field_names


def test_contacts_are_a_separate_table_from_farm_data():
    from backend.db.models import Contact, Observation

    observation_fields = set(Observation.model_fields.keys())
    assert "name" not in observation_fields and "phone" not in observation_fields
    assert {"name", "phone"} <= set(Contact.model_fields.keys())


def test_env_is_gitignored():
    repo_root = Path(__file__).resolve().parent.parent
    gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore
