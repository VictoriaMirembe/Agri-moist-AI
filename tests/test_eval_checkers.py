# The eval harness's task 2 checker (scripts/run_evals.py, task_2_meaning). It must
# recognise correct "don't water" wording, and still fail any draft that tells farmers
# to add water before heavy rain.
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from run_evals import task_2_meaning  # noqa: E402


def _passes(text: str) -> bool:
    heavy, no_water, add_water = task_2_meaning(text)
    return heavy and no_water and not add_water


@pytest.mark.parametrize(
    "text",
    [
        # 2026-11-13 irrigation re-run, task 2 runs 1 and 2
        "Recent rain has been low (5.0 mm) but heavy rain is forecast (45.0 mm) and a heavy‑rain warning "
        "says not to add water, even on dry plots.",
        # task 2 run 3
        "Heavy rain is forecast (45.0 mm) and a heavy‑rain warning says not to add water. You should wait "
        "for the forecast rain rather than irrigate.",
        "Heavy rain is forecast, so wait for it instead of watering.",
    ],
)
def test_correct_dont_water_wording_passes(text):
    assert _passes(text)


@pytest.mark.parametrize(
    "text",
    [
        "Heavy rain is forecast (45 mm); a warning says not to add water, but water the dry plot today.",
        "Heavy rain is coming. Irrigate now rather than wait.",
        "Heavy rain is forecast; keep the soil moist until it arrives.",
        "Heavy rain is forecast, so do not add water. Water the dry plot if it wilts.",
    ],
)
def test_drafts_that_say_to_add_water_still_fail(text):
    assert not _passes(text)


def test_not_waterlogged_is_not_saying_not_to_water():
    assert task_2_meaning("The soil is not waterlogged.")[1] is False
    assert not _passes("Heavy rain is forecast. The soil is not waterlogged.")
