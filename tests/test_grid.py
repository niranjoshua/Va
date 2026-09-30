from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vaticore.decisions.dispatch import SiteAssets, plan_dispatch, simulate_dispatch
from vaticore.engine import dispatch_plan_for_site, run_value_backtest
from vaticore.evaluation.backtest import FoldForecast
from vaticore.evaluation.value import PERFECT_FORECAST, PolicySource, value_backtest
from vaticore.forecasting.base import InsufficientHistoryError, quantile_column
from vaticore.forecasting.grid_availability import GridAvailabilityForecaster
from vaticore.schemas import GRID_AVAILABLE, TIMESTAMP

# A telecom tower shape: small, lossless battery, 10 kW grid, 15 kW generator.
TOWER = SiteAssets(
    battery_kwh=20.0,
    battery_power_kw=10.0,
    genset_kw=15.0,
    charge_efficiency=1.0,
    discharge_efficiency=1.0,
    genset_min_load=0.25,
    grid_kw=10.0,
    grid_price_per_kwh=0.2,
    diesel_price_per_l=1.0,
    value_of_lost_load_per_kwh=2.0,
)


def _hours(n: int, start: str = "2024-01-01") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq="h", tz="UTC")


def test_grid_serves_first_and_a_scheduled_generator_stays_off() -> None:
    out = simulate_dispatch([4.0], [True], soc_kwh=20.0, assets=TOWER, grid_available=[1.0])
    assert out.grid_kwh == pytest.approx(4.0)
    assert out.genset_hours == 0.0
    assert out.fuel_l == 0.0


def test_spare_grid_capacity_recharges_the_battery() -> None:
    out = simulate_dispatch([4.0], [False], soc_kwh=10.0, assets=TOWER, grid_available=[1.0])
    # 4 kW to the load, the remaining 6 kW of the connection into the battery.
    assert out.grid_kwh == pytest.approx(10.0)
    assert out.soc_end_kwh == pytest.approx(16.0)
    no_charging = SiteAssets(**{**TOWER.__dict__, "grid_charges_battery": False})
    out = simulate_dispatch([4.0], [False], soc_kwh=10.0, assets=no_charging, grid_available=[1.0])
    assert out.soc_end_kwh == pytest.approx(10.0)


def test_grid_limit_then_battery_then_blackout() -> None:
    out = simulate_dispatch([14.0], [False], soc_kwh=2.0, assets=TOWER, grid_available=[1.0])
    assert out.grid_kwh == pytest.approx(10.0)
    assert out.unserved_kwh == pytest.approx(2.0)  # 4 kW short, 2 kWh in the battery
    off = simulate_dispatch([4.0], [False], soc_kwh=0.0, assets=TOWER, grid_available=[0.0])
    assert off.unserved_kwh == pytest.approx(4.0)


def test_grid_sites_must_say_when_the_grid_was_on() -> None:
    with pytest.raises(ValueError, match="grid_available"):
        simulate_dispatch([4.0], [False], soc_kwh=10.0, assets=TOWER)
    unknown = simulate_dispatch(
        [4.0, 4.0], [False, False], soc_kwh=20.0, assets=TOWER, grid_available=[np.nan, 1.0]
    )
    assert unknown.grid_unknown_steps == 1
    # The unknown hour is treated as off (the battery serves it); when the grid
    # returns it serves 4 kWh of load and puts the 4 kWh back in the battery.
    assert unknown.grid_kw.tolist() == pytest.approx([0.0, 8.0])


def test_plan_books_the_generator_only_when_the_grid_is_expected_off() -> None:
    net = pd.Series([4.0] * 6, index=_hours(6))
    grid = [1, 1, 1, 0, 0, 0]
    plan = plan_dispatch(net, soc_kwh=4.0, assets=TOWER, planned_grid_available=grid)
    # Grid hours top the battery up to 20 kWh, more than the 12 kWh the three
    # off hours need, so no generator at all.
    assert not plan.genset_on.any()
    assert plan.grid_window_labels == ["00:00 to 03:00"]
    assert "counts on grid power 00:00 to 03:00" in plan.summary()

    dark = plan_dispatch(net, soc_kwh=0.0, assets=TOWER, planned_grid_available=[0] * 6)
    assert dark.genset_on.all()
    assert "does not count on the grid" in dark.summary()


def _grid_history(days: int, on_hours: range = range(6, 18)) -> pd.DataFrame:
    stamps = _hours(24 * days)
    return pd.DataFrame(
        {TIMESTAMP: stamps, GRID_AVAILABLE: np.isin(stamps.hour, list(on_hours)).astype(float)}
    )


def test_availability_forecaster_learns_the_supply_pattern() -> None:
    model = GridAvailabilityForecaster().fit(_grid_history(28))
    p = model.predict_probability(24)
    assert p.iloc[8] > 0.9  # 08:00 is always on
    assert p.iloc[2] < 0.1  # 02:00 is always off
    q = model.predict_quantiles(24, (0.1, 0.5, 0.9))
    assert list(q.columns) == [quantile_column(x) for x in (0.1, 0.5, 0.9)]
    assert (q.to_numpy()[:, 0] <= q.to_numpy()[:, 2]).all()  # cautious plan never counts on more
    assert q[quantile_column(0.1)].iloc[8] == 1.0


def test_availability_forecaster_is_cautious_about_an_unreliable_hour() -> None:
    history = _grid_history(28)
    # 12:00 has been on only every other day.
    noon = pd.DatetimeIndex(history[TIMESTAMP]).hour == 12
    history.loc[noon, GRID_AVAILABLE] = np.tile([1.0, 0.0], 14)
    q = GridAvailabilityForecaster().fit(history).predict_quantiles(24, (0.1, 0.9))
    assert q[quantile_column(0.1)].iloc[12] == 0.0  # the cautious plan does not rely on it
    assert q[quantile_column(0.9)].iloc[12] == 1.0


def test_availability_forecaster_errors_are_typed() -> None:
    with pytest.raises(InsufficientHistoryError):
        GridAvailabilityForecaster().fit(_grid_history(3))
    quarter = pd.DataFrame(
        {
            TIMESTAMP: pd.date_range("2024-01-01", periods=2000, freq="15min", tz="UTC"),
            GRID_AVAILABLE: 1.0,
        }
    )
    with pytest.raises(ValueError, match="hourly"):
        GridAvailabilityForecaster().fit(quarter)


def test_value_backtest_counts_grid_energy_and_punishes_trusting_a_dead_grid() -> None:
    stamps = _hours(24)
    fold = FoldForecast(
        model="m",
        fold=0,
        timestamps=stamps,
        actual=np.full(24, 4.0),
        quantiles={0.5: np.full(24, 4.0)},
    )
    grid_actual = pd.Series(np.isin(stamps.hour, range(6, 18)).astype(float), index=stamps)
    trusting = pd.Series(1.0, index=stamps)  # assumes the grid never fails
    cautious = grid_actual.copy()
    report = value_backtest(
        {
            "trusting": PolicySource([fold], 0.5, trusting),
            "cautious": PolicySource([fold], 0.5, cautious),
        },
        assets=TOWER,
        baseline="trusting",
        grid_actual=grid_actual,
    )
    trusting_r, cautious_r = report.results["trusting"], report.results["cautious"]
    assert trusting_r.unserved_kwh > 0 and cautious_r.unserved_kwh < trusting_r.unserved_kwh
    assert cautious_r.grid_kwh > 0
    assert cautious_r.grid_cost == pytest.approx(cautious_r.grid_kwh * 0.2)
    assert cautious_r.total_cost < trusting_r.total_cost
    assert report.results[PERFECT_FORECAST].unserved_kwh == 0.0
    with pytest.raises(ValueError, match="grid plan"):
        value_backtest(
            {"a": PolicySource([fold], 0.5)}, assets=TOWER, baseline="a", grid_actual=grid_actual
        )


def _tower_history(single_site: pd.DataFrame) -> pd.DataFrame:
    site = single_site.copy()
    hours = pd.DatetimeIndex(site[TIMESTAMP]).hour
    site[GRID_AVAILABLE] = np.isin(hours, list(range(6, 18))).astype(float)
    return site


def test_engine_plans_a_grid_site(single_site: pd.DataFrame) -> None:
    assets = SiteAssets(battery_kwh=300.0, battery_power_kw=80.0, genset_kw=80.0, grid_kw=60.0)
    with pytest.raises(ValueError, match="grid_available"):
        dispatch_plan_for_site(single_site, assets=assets, soc_kwh=100.0, model="persistence")
    reliable = dispatch_plan_for_site(
        single_site, assets=assets, soc_kwh=100.0, model="persistence", assume_grid_always_on=True
    )
    assert reliable.planned_grid_on.all()
    plan = dispatch_plan_for_site(
        _tower_history(single_site), assets=assets, soc_kwh=100.0, model="persistence"
    )
    assert plan.planned_grid_on[8] and not plan.planned_grid_on[2]


def test_engine_value_backtest_on_a_grid_site(single_site: pd.DataFrame) -> None:
    assets = SiteAssets(battery_kwh=300.0, battery_power_kw=80.0, genset_kw=80.0, grid_kw=60.0)
    outcome = run_value_backtest(
        _tower_history(single_site), assets=assets, initial=24 * 21, conformal_window=3
    )
    results = outcome.value.results
    assert all(r.grid_kwh > 0 for r in results.values())
    with pytest.raises(ValueError, match="grid_available"):
        run_value_backtest(single_site, assets=assets, initial=24 * 21, conformal_window=3)
