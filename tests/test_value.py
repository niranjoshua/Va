from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from vaticore.decisions.dispatch import SiteAssets
from vaticore.engine import dispatch_plan_for_site, run_value_backtest
from vaticore.evaluation.backtest import FoldForecast
from vaticore.evaluation.value import PERFECT_FORECAST, PolicySource, value_backtest
from vaticore.tracking import NoOpTracker

ASSETS = SiteAssets(
    battery_kwh=100.0,
    battery_power_kw=50.0,
    genset_kw=40.0,
    charge_efficiency=1.0,
    discharge_efficiency=1.0,
    genset_min_load=0.25,
    diesel_price_per_l=1.0,
    value_of_lost_load_per_kwh=2.0,
)


def _day() -> np.ndarray:
    """20 quiet hours, then a 4 hour evening load of 40 kW (160 kWh)."""
    return np.r_[np.zeros(20), np.full(4, 40.0)]


def _folds(planned: np.ndarray, days: int = 2) -> list[FoldForecast]:
    folds = []
    for d in range(days):
        stamps = pd.date_range("2024-01-01", periods=24, freq="h", tz="UTC") + pd.Timedelta(days=d)
        folds.append(
            FoldForecast(
                model="m", fold=d, timestamps=stamps, actual=_day(), quantiles={0.5: planned}
            )
        )
    return folds


def test_value_backtest_known_answer() -> None:
    report = value_backtest(
        {
            "blind": PolicySource(_folds(np.zeros(24)), 0.5),
            "cautious": PolicySource(_folds(np.full(24, 40.0)), 0.5),
        },
        assets=ASSETS,
        baseline="blind",
    )
    blind = report.results["blind"]
    perfect = report.results[PERFECT_FORECAST]

    # Planning for nothing never schedules the generator. Starting at 50 kWh:
    # day 1 serves 50 of 160 kWh, day 2 starts empty and serves none.
    assert blind.fuel_l == 0.0
    assert blind.unserved_kwh == pytest.approx(110.0 + 160.0)
    # A perfect forecast schedules exactly the short hours: 3 on day 1, 4 on day 2.
    assert perfect.unserved_kwh == 0.0
    assert perfect.genset_hours == pytest.approx(7.0)
    assert perfect.fuel_l == pytest.approx(7 * (0.08145 * 40 + 0.246 * 40))

    assert report.share_of_possible(PERFECT_FORECAST) == pytest.approx(1.0)
    assert report.share_of_possible("blind") == pytest.approx(0.0)
    assert report.results["cautious"].unserved_kwh == 0.0
    assert report.savings("cautious") > 0

    frame = report.to_frame()
    assert set(frame.index) == {"blind", "cautious", PERFECT_FORECAST}
    assert frame.loc["blind", "total_cost"] == pytest.approx(270.0 * 2.0)


def test_sensitivity_shows_when_a_ranking_depends_on_outage_cost() -> None:
    report = value_backtest(
        {"blind": PolicySource(_folds(np.zeros(24)), 0.5)}, assets=ASSETS, baseline="blind"
    )
    table = report.sensitivity([0.0, 5.0])
    fuel_cost = report.results[PERFECT_FORECAST].fuel_cost
    # If outages cost nothing, burning diesel to avoid them is a pure loss.
    assert table.loc[PERFECT_FORECAST, 0.0] == pytest.approx(-fuel_cost)
    assert table.loc[PERFECT_FORECAST, 5.0] == pytest.approx(270.0 * 5.0 - fuel_cost)


def test_value_backtest_rejects_mismatched_or_broken_windows() -> None:
    good = PolicySource(_folds(np.zeros(24)), 0.5)
    short = PolicySource(_folds(np.zeros(24), days=1), 0.5)
    with pytest.raises(ValueError, match="same windows"):
        value_backtest({"a": good, "b": short}, assets=ASSETS, baseline="a")
    with pytest.raises(ValueError, match="baseline"):
        value_backtest({"a": good}, assets=ASSETS, baseline="missing")

    folds = _folds(np.zeros(24), days=2)
    gapped = [folds[0], replace(folds[1], timestamps=folds[1].timestamps + pd.Timedelta(days=1))]
    with pytest.raises(ValueError, match="back to back"):
        value_backtest({"a": PolicySource(gapped, 0.5)}, assets=ASSETS, baseline="a")


class _Recorder(NoOpTracker):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def log_summary(self, **kwargs: Any) -> str | None:  # type: ignore[override]
        self.calls.append(kwargs)
        return "run-1"


def test_engine_value_backtest_end_to_end(single_site: pd.DataFrame) -> None:
    tracker = _Recorder()
    assets = SiteAssets(battery_kwh=300.0, battery_power_kw=80.0, genset_kw=80.0)
    outcome = run_value_backtest(
        single_site,
        assets=assets,
        initial=24 * 21,
        conformal_window=3,
        tracker=tracker,
        operator_id="op1",
        site_id="siteA",
    )
    policies = set(outcome.value.results)
    assert policies == {
        "persistence P50",
        "persistence P90",
        "quantile_gbm_day_ahead P50",
        "quantile_gbm_day_ahead P90",
        "quantile_gbm_day_ahead+conformal P90",
        PERFECT_FORECAST,
    }
    assert outcome.baseline_policy == "persistence P50"
    assert set(outcome.calibration) == {
        "persistence",
        "quantile_gbm_day_ahead",
        "quantile_gbm_day_ahead+conformal",
    }
    # Nine back to back days scored for every policy.
    assert all(r.days == pytest.approx(9.0) for r in outcome.value.results.values())

    (call,) = tracker.calls
    assert call["params"]["site_id"] == "siteA"
    assert "quantile_gbm_day_ahead P90.total_cost" in call["metrics"]
    assert "quantile_gbm_day_ahead+conformal.coverage_0.1_0.9" in call["metrics"]


def test_engine_value_backtest_validates_inputs(single_site: pd.DataFrame) -> None:
    assets = SiteAssets(battery_kwh=300.0, battery_power_kw=80.0, genset_kw=80.0)
    with pytest.raises(ValueError, match="differ"):
        run_value_backtest(single_site, assets=assets, initial=24 * 21, model="persistence")
    with pytest.raises(ValueError, match="plan quantiles"):
        run_value_backtest(single_site, assets=assets, initial=24 * 21, plan_quantiles=(0.95,))


def test_dispatch_plan_for_site_returns_an_hourly_schedule(single_site: pd.DataFrame) -> None:
    assets = SiteAssets(battery_kwh=300.0, battery_power_kw=80.0, genset_kw=80.0)
    plan = dispatch_plan_for_site(
        single_site, assets=assets, soc_kwh=150.0, model="persistence", calibrate=True
    )
    assert len(plan.timestamps) == 24
    assert plan.timestamps[0] > single_site["timestamp"].max()
    assert plan.genset_on.dtype == bool
    assert isinstance(plan.summary(), str)
