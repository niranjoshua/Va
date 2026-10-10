"""The efficiency review: rules replayed on a site's own history."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from vaticore.decisions.efficiency import (
    BATTERY_FIRST,
    GRID_OFF,
    RECORDED,
    RUN_HARD,
    review_efficiency,
)
from vaticore.sites import Site

END = pd.Timestamp("2026-10-01", tz="UTC")
START = END - pd.Timedelta(days=30)


def _site(**overrides: object) -> Site:
    base: dict[str, object] = {
        "operator_id": "op",
        "site_id": "t1",
        "name": "Tower",
        "site_type": "telecom_tower",
        "latitude": 6.6,
        "longitude": 3.5,
        "timezone": "Africa/Lagos",
        "currency": "NGN",
        "value_of_lost_load_per_kwh": 5000.0,
        "battery": {"usable_kwh": 20.0, "power_kw": 10.0, "min_soc_kwh": 2.0},
        "solar": {"kwp": 8.0},
        "generator": {"rated_kw": 16.0, "fuel_price_per_l": 1250.0, "min_run_hours": 2},
        "grid": {"capacity_kw": 10.0, "price_per_kwh": 225.0},
    }
    base.update(overrides)
    return Site(**base)  # type: ignore[arg-type]


def _readings(*, grid: bool = True, practice: str = "grid_off", seed: int = 4) -> pd.DataFrame:
    """A lightly loaded tower: 3.3 kW, 8 kWp, a 16 kW generator run by habit."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(START, END, freq="1h", inclusive="left")
    h = idx.hour.to_numpy()
    load = 3.3 * (1 + 0.1 * rng.standard_normal(len(idx)))
    solar = np.clip(np.sin((h - 6) / 12 * np.pi), 0, None) * 8 * rng.uniform(0.3, 1, len(idx))
    on = np.empty(len(idx))
    state = 1.0
    for i in range(len(idx)):
        if state and rng.random() < 0.1:
            state = 0.0
        elif not state and rng.random() < 0.2:
            state = 1.0
        on[i] = state
    deficit = np.maximum(load - solar, 0.0)
    if practice == "grid_off":  # an automatic transfer switch: generator on every grid failure
        genset = np.where((on < 0.5) & (deficit > 0), np.maximum(deficit, 4.8), 0.0)
    elif practice == "always_on_deficit":  # off grid: the generator covers every shortfall
        genset = np.where(deficit > 0, np.maximum(deficit, 4.8), 0.0)
    else:  # runs at night regardless of the grid
        genset = np.where((h >= 18) | (h < 6), np.maximum(deficit, 4.8), 0.0)
    frame = pd.DataFrame(
        {"timestamp": idx, "load_kw": load, "generation_kw": solar, "genset_kw": genset}
    )
    if grid:
        frame["grid_available"] = on
    return frame


def test_a_grid_site_that_starts_on_every_grid_failure_should_go_battery_first() -> None:
    review = review_efficiency(_site(), _readings(), start=START, end=END)
    assert not review.problems
    assert [r.name for r in review.results] == [RECORDED, GRID_OFF, BATTERY_FIRST, RUN_HARD]
    assert review.reference == RECORDED and review.recommended == BATTERY_FIRST
    assert review.share_saved is not None and review.share_saved > 0.5
    assert any("grid failed" in a for a in review.advice)
    assert any("grid refills the battery" in a for a in review.advice)
    assert [f.code for f in review.findings] == ["light_load"]
    text = review.to_text("Africa/Lagos")
    assert "Battery first:" in text and "NGN" in text
    json.dumps(review.to_dict())


def test_running_while_the_grid_is_on_is_flagged() -> None:
    review = review_efficiency(_site(), _readings(practice="nightly"), start=START, end=END)
    codes = [f.code for f in review.findings]
    assert "ran_with_grid" in codes
    flag = next(f for f in review.findings if f.code == "ran_with_grid")
    assert flag.litres > 0


def test_an_off_grid_tower_at_light_load_should_run_hard() -> None:
    review = review_efficiency(
        _site(grid=None),
        _readings(grid=False, practice="always_on_deficit"),
        start=START,
        end=END,
    )
    assert review.recommended == RUN_HARD
    run_hard = review.result(RUN_HARD)
    first = review.result(BATTERY_FIRST)
    assert run_hard is not None and first is not None and run_hard.fuel_l < first.fuel_l
    assert run_hard.unserved_hours == 0
    assert any("charge_setpoint = 0.8" in a for a in review.advice)


def test_without_generator_readings_the_common_practice_is_the_reference() -> None:
    readings = _readings().drop(columns=["genset_kw"])
    review = review_efficiency(_site(), readings, start=START, end=END)
    assert review.reference == GRID_OFF and review.result(RECORDED) is None
    assert review.findings == ()
    off_grid = review_efficiency(
        _site(grid=None), readings.drop(columns=["grid_available"]), start=START, end=END
    )
    assert off_grid.reference is None and off_grid.litres_saved is None
    assert "Battery first" in off_grid.to_text()


@pytest.mark.parametrize(
    ("site", "readings", "problem"),
    [
        ({"generator": None}, None, "no generator"),
        ({}, "sparse", "load (and solar) readings cover"),
        ({}, "no_grid_record", "grid on/off is recorded"),
    ],
)
def test_what_it_refuses_to_review(
    site: dict[str, object], readings: str | None, problem: str
) -> None:
    frame = _readings()
    if readings == "sparse":
        frame = frame.iloc[::3]
    elif readings == "no_grid_record":
        frame = frame.drop(columns=["grid_available"])
    review = review_efficiency(_site(**site), frame, start=START, end=END)
    assert review.recommended is None
    assert any(problem in p for p in review.problems)
    assert "Cannot review" in review.to_text()


def test_a_window_shorter_than_a_week_is_refused() -> None:
    review = review_efficiency(_site(), _readings(), start=END - pd.Timedelta(days=3), end=END)
    assert review.problems and review.recommended is None
