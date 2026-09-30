from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vaticore.decisions.dispatch import SiteAssets, plan_dispatch, simulate_dispatch

# Lossless battery so the arithmetic in each test is easy to follow.
ASSETS = SiteAssets(
    battery_kwh=100.0,
    battery_power_kw=50.0,
    genset_kw=40.0,
    charge_efficiency=1.0,
    discharge_efficiency=1.0,
    genset_min_load=0.25,  # 10 kW minimum stable load
)


def _run(net: list[float], on: list[bool], soc: float, assets: SiteAssets = ASSETS):  # type: ignore[no-untyped-def]
    return simulate_dispatch(np.array(net), np.array(on), soc_kwh=soc, assets=assets)


def _series(values: list[float]) -> pd.Series:
    index = pd.date_range("2024-01-01", periods=len(values), freq="h", tz="UTC")
    return pd.Series(values, index=index, dtype=float)


def test_fuel_curve_matches_the_homer_default() -> None:
    # 0.08145 L/h per rated kW * 40 kW + 0.246 L/kWh * 20 kW
    assert ASSETS.fuel_litres(20.0) == pytest.approx(3.258 + 4.92)


def test_asset_validation() -> None:
    with pytest.raises(ValueError):
        SiteAssets(battery_kwh=-1.0, battery_power_kw=10.0, genset_kw=10.0)
    with pytest.raises(ValueError):
        SiteAssets(battery_kwh=0.0, battery_power_kw=10.0, genset_kw=10.0, min_soc_kwh=1.0)
    with pytest.raises(ValueError):
        SiteAssets(battery_kwh=10.0, battery_power_kw=10.0, genset_kw=10.0, min_soc_kwh=10.0)
    with pytest.raises(ValueError):
        SiteAssets(battery_kwh=10.0, battery_power_kw=10.0, genset_kw=10.0, charge_efficiency=0)


def test_surplus_charges_then_curtails() -> None:
    out = _run([-30.0], [False], soc=90.0)
    assert out.soc_end_kwh == pytest.approx(100.0)
    assert out.curtailed_kwh == pytest.approx(20.0)
    assert out.fuel_l == 0.0


def test_deficit_is_met_from_the_battery_within_its_power_limit() -> None:
    covered = _run([30.0], [False], soc=50.0)
    assert covered.soc_end_kwh == pytest.approx(20.0)
    assert covered.unserved_kwh == 0.0
    limited = _run([60.0], [False], soc=100.0)
    assert limited.unserved_kwh == pytest.approx(10.0)


def test_empty_battery_without_a_scheduled_generator_is_a_blackout() -> None:
    out = _run([20.0], [False], soc=0.0)
    assert out.unserved_kwh == pytest.approx(20.0)
    assert out.unserved_hours == pytest.approx(1.0)


def test_generator_never_runs_below_its_minimum_load() -> None:
    out = _run([4.0], [True], soc=50.0)
    assert out.genset_kw[0] == pytest.approx(10.0)
    assert out.soc_end_kwh == pytest.approx(56.0)  # the 6 kW excess charges the battery
    assert out.fuel_l == pytest.approx(3.258 + 2.46)


def test_generator_and_battery_share_a_peak_above_the_generator_rating() -> None:
    out = _run([70.0], [True], soc=50.0)
    assert out.genset_kw[0] == pytest.approx(40.0)
    assert out.soc_end_kwh == pytest.approx(20.0)
    assert out.unserved_kwh == 0.0


def test_scheduled_generator_stays_off_when_solar_is_in_surplus() -> None:
    out = _run([-10.0], [True], soc=50.0)
    assert out.genset_hours == 0.0
    assert out.fuel_l == 0.0


def test_starts_hours_and_gaps_are_counted() -> None:
    out = _run([20.0, 20.0, 20.0, 20.0], [True, True, False, True], soc=0.0)
    assert out.genset_hours == pytest.approx(3.0)
    assert out.genset_starts == 2
    assert out.unserved_kwh == pytest.approx(20.0)
    gaps = _run([np.nan, 10.0], [False, False], soc=50.0)
    assert gaps.missing_steps == 1
    assert gaps.soc_end_kwh == pytest.approx(40.0)


def test_battery_efficiency_is_applied() -> None:
    lossy = SiteAssets(battery_kwh=100.0, battery_power_kw=50.0, genset_kw=0.0)
    out = simulate_dispatch(np.array([-10.0]), np.array([False]), soc_kwh=0.0, assets=lossy)
    assert out.soc_end_kwh == pytest.approx(9.5)


def test_plan_needs_no_generator_when_the_battery_covers_the_day() -> None:
    plan = plan_dispatch(_series([10.0] * 5), soc_kwh=100.0, assets=ASSETS)
    assert not plan.genset_on.any()
    assert plan.run_windows == []
    assert plan.summary().startswith("No generator needed")


def test_plan_schedules_exactly_the_short_hours() -> None:
    # 60 kWh in the battery, 30 kW net load: it runs dry after two hours.
    plan = plan_dispatch(_series([30.0] * 5), soc_kwh=60.0, assets=ASSETS)
    assert plan.genset_on.tolist() == [False, False, True, True, True]
    ((start, end),) = plan.run_windows
    assert (start.hour, end.hour) == (2, 5)
    assert plan.expected.unserved_kwh == 0.0
    assert plan.expected.fuel_l == pytest.approx(3 * (3.258 + 0.246 * 30.0))
    assert "Run the generator 02:00 to 05:00 (3 h" in plan.summary()


def test_a_run_ending_at_midnight_says_midnight() -> None:
    values = [0.0] * 20 + [60.0] * 4
    plan = plan_dispatch(_series(values), soc_kwh=0.0, assets=ASSETS)
    assert plan.run_window_labels == ["20:00 to midnight"]


def test_plan_warns_when_even_the_generator_is_not_enough() -> None:
    plan = plan_dispatch(_series([100.0] * 3), soc_kwh=0.0, assets=ASSETS)
    assert plan.expected.unserved_kwh == pytest.approx(3 * 60.0)
    assert "may go unserved" in plan.summary()


def test_plan_rejects_incomplete_or_unindexed_forecasts() -> None:
    with pytest.raises(ValueError):
        plan_dispatch(_series([10.0, np.nan]), soc_kwh=50.0, assets=ASSETS)
    with pytest.raises(ValueError):
        plan_dispatch(pd.Series([10.0, 10.0]), soc_kwh=50.0, assets=ASSETS)


def test_a_site_without_a_battery_runs_on_generator_and_solar() -> None:
    no_battery = SiteAssets(battery_kwh=0.0, battery_power_kw=1.0, genset_kw=40.0)
    out = simulate_dispatch(
        np.array([-5.0, 20.0, 20.0]), np.array([False, True, False]), soc_kwh=0.0, assets=no_battery
    )
    assert out.curtailed_kwh == pytest.approx(5.0)  # nowhere to store the surplus
    assert out.unserved_kwh == pytest.approx(20.0)  # the unscheduled hour goes dark
    plan = plan_dispatch(_series([20.0, 20.0]), soc_kwh=0.0, assets=no_battery)
    assert plan.genset_on.all()
    assert "Run the generator" in plan.summary()
