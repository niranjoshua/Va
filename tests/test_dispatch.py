from __future__ import annotations

from dataclasses import replace

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


def test_labels_use_the_site_clock_while_timestamps_stay_utc() -> None:
    # 19:00 to 23:00 UTC is 20:00 to midnight in Lagos (UTC+1).
    values = [0.0] * 19 + [60.0] * 4 + [0.0]
    plan = plan_dispatch(
        _series(values), soc_kwh=0.0, assets=ASSETS, display_timezone="Africa/Lagos"
    )
    assert plan.run_window_labels == ["20:00 to midnight"]
    assert "Run the generator 20:00 to midnight" in plan.summary()
    assert str(plan.timestamps.tz) == "UTC"
    assert plan_dispatch(_series(values), soc_kwh=0.0, assets=ASSETS).run_window_labels == [
        "19:00 to 23:00"
    ]


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


def test_minimum_run_time_stops_hour_by_hour_cycling() -> None:
    # 5 kW of net load, an empty battery and a 10 kW minimum stable load: an
    # hour-by-hour planner runs the set one hour (charging 5 kWh), lets the
    # battery cover the next, and starts again. Three starts in six hours.
    hourly = plan_dispatch(_series([5.0] * 6), soc_kwh=0.0, assets=ASSETS)
    assert hourly.genset_on.tolist() == [True, False, True, False, True, False]
    assert hourly.expected.genset_starts == 3

    # A three hour minimum run charges the battery enough for the rest of the
    # day: one start, the same fuel, nothing unserved.
    assets = SiteAssets(**{**ASSETS.__dict__, "genset_min_run_hours": 3.0})
    held = plan_dispatch(_series([5.0] * 6), soc_kwh=0.0, assets=assets)
    assert held.genset_on.tolist() == [True, True, True, False, False, False]
    assert held.expected.genset_starts == 1
    assert held.expected.unserved_kwh == 0.0
    assert held.expected.fuel_l == pytest.approx(hourly.expected.fuel_l)
    assert held.run_window_labels == ["00:00 to 03:00"]


def test_minimum_run_is_cut_short_by_the_end_of_the_plan() -> None:
    assets = SiteAssets(**{**ASSETS.__dict__, "genset_min_run_hours": 4.0})
    plan = plan_dispatch(_series([0.0, 0.0, 5.0, 5.0]), soc_kwh=0.0, assets=assets)
    assert plan.genset_on.tolist() == [False, False, True, True]


def test_minimum_run_counts_whole_steps() -> None:
    quarter_hourly = SiteAssets(**{**ASSETS.__dict__, "step_hours": 0.25})
    assert (
        SiteAssets(**{**quarter_hourly.__dict__, "genset_min_run_hours": 1.5}).genset_min_run_steps
        == 6
    )
    assert SiteAssets(**{**ASSETS.__dict__, "genset_min_run_hours": 1.5}).genset_min_run_steps == 2
    assert ASSETS.genset_min_run_steps == 1
    with pytest.raises(ValueError):
        SiteAssets(**{**ASSETS.__dict__, "genset_min_run_hours": 0.0})


# Run hard, then off (cycle charging), and the look-ahead planner.

CYCLING = SiteAssets(
    battery_kwh=100.0,
    battery_power_kw=50.0,
    genset_kw=40.0,
    charge_efficiency=1.0,
    discharge_efficiency=1.0,
    genset_min_load=0.25,
    genset_charge_setpoint=0.8,  # 32 kW while running
)


def test_cycle_charging_loads_the_generator_and_charges_the_battery() -> None:
    out = _run([4.0], [True], soc=50.0, assets=CYCLING)
    assert out.genset_kw[0] == pytest.approx(32.0)
    assert out.soc_end_kwh == pytest.approx(78.0)  # 28 kW of surplus into the battery


def test_cycle_charging_stops_at_what_the_battery_can_take() -> None:
    nearly_full = _run([4.0], [True], soc=95.0, assets=CYCLING)
    assert nearly_full.genset_kw[0] == pytest.approx(10.0)  # 5 kW of room, but the 10 kW floor
    assert nearly_full.soc_end_kwh == pytest.approx(100.0)
    assert nearly_full.curtailed_kwh == pytest.approx(1.0)
    power_limited = _run([4.0], [True], soc=0.0, assets=replace(CYCLING, battery_power_kw=10.0))
    assert power_limited.genset_kw[0] == pytest.approx(14.0)  # load plus the charger's 10 kW


def test_cycle_charging_still_serves_a_load_above_the_setpoint() -> None:
    out = _run([38.0], [True], soc=50.0, assets=CYCLING)
    assert out.genset_kw[0] == pytest.approx(38.0)
    assert out.soc_end_kwh == pytest.approx(50.0)


def test_cycle_charging_needs_a_valid_setpoint() -> None:
    with pytest.raises(ValueError):
        replace(CYCLING, genset_charge_setpoint=0.2)  # below the 0.25 minimum load
    with pytest.raises(ValueError):
        replace(CYCLING, genset_charge_setpoint=1.2)


def _tower_days(days: int, assets: SiteAssets) -> tuple[float, float]:
    """Fuel and unserved energy over back to back planned days, battery carried."""
    rng = np.random.default_rng(3)
    shape = np.clip(np.sin((np.arange(24) - 6) / 12 * np.pi), 0, None)
    soc = (assets.min_soc_kwh + assets.battery_kwh) / 2
    fuel = unserved = 0.0
    for day in range(days):
        net = 3.3 * (1 + 0.1 * rng.standard_normal(24)) - 8.0 * shape * rng.uniform(0.3, 1.0)
        index = pd.date_range("2024-01-01", periods=24, freq="h", tz="UTC") + pd.Timedelta(days=day)
        plan = plan_dispatch(pd.Series(net, index=index), soc_kwh=soc, assets=assets)
        out = simulate_dispatch(net, plan.genset_on, soc_kwh=soc, assets=assets)
        soc, fuel, unserved = out.soc_end_kwh, fuel + out.fuel_l, unserved + out.unserved_kwh
    return fuel, unserved


TOWER = SiteAssets(
    battery_kwh=20.0,
    battery_power_kw=10.0,
    genset_kw=16.0,  # large for a 3.3 kW tower, as many are
    min_soc_kwh=2.0,
    genset_min_run_hours=2.0,
)


def test_running_hard_then_off_burns_less_at_a_lightly_loaded_tower() -> None:
    following, lost_following = _tower_days(20, TOWER)
    hard, lost_hard = _tower_days(20, replace(TOWER, genset_charge_setpoint=0.8))
    assert lost_following == pytest.approx(0.0) and lost_hard == pytest.approx(0.0)
    assert hard < 0.85 * following


def test_look_ahead_burns_no_more_and_is_as_reliable() -> None:
    hard, lost_hard = _tower_days(20, replace(TOWER, genset_charge_setpoint=0.8))
    ahead, lost_ahead = _tower_days(20, replace(TOWER, genset_charge_setpoint=0.8, look_ahead=True))
    assert lost_ahead <= lost_hard + 1e-6
    assert ahead < hard


@pytest.mark.parametrize("seed", range(8))
def test_look_ahead_plan_is_never_worse_on_its_forecast(seed: int) -> None:
    rng = np.random.default_rng(seed)
    assets = SiteAssets(
        battery_kwh=float(rng.uniform(5, 60)),
        battery_power_kw=float(rng.uniform(3, 20)),
        genset_kw=float(rng.uniform(8, 30)),
        min_soc_kwh=1.0,
        genset_min_run_hours=float(rng.integers(1, 4)),
        genset_charge_setpoint=None if seed % 2 else 0.8,
        grid_kw=20.0 if seed % 3 == 0 else 0.0,
    )
    net = _series(list(rng.uniform(-6, 12, 24)))
    grid = (rng.uniform(size=24) > 0.4).astype(float) if assets.grid_kw else None
    soc = float(rng.uniform(1.0, assets.battery_kwh))
    plain = plan_dispatch(net, soc_kwh=soc, assets=assets, planned_grid_available=grid)
    ahead = plan_dispatch(
        net, soc_kwh=soc, assets=replace(assets, look_ahead=True), planned_grid_available=grid
    )
    assert ahead.expected.unserved_kwh <= plain.expected.unserved_kwh + 1e-6
    assert ahead.expected.soc_end_kwh >= plain.expected.soc_end_kwh - 1e-6
    assert (
        ahead.expected.fuel_l <= plain.expected.fuel_l + 0.01 * plain.expected.genset_starts + 1e-6
    )


@pytest.mark.parametrize("seed", range(6))
def test_the_search_model_matches_the_simulator(seed: int) -> None:
    from vaticore.decisions.dispatch import _step, _step_levels

    rng = np.random.default_rng(seed)
    assets = SiteAssets(
        battery_kwh=50.0,
        battery_power_kw=15.0,
        genset_kw=20.0,
        min_soc_kwh=5.0,
        genset_charge_setpoint=0.8 if seed % 2 else None,
        grid_kw=10.0,
        grid_price_per_kwh=0.1,
    )
    levels = np.linspace(5.0, 50.0, 13)
    for _ in range(20):
        net, on, grid_on = float(rng.uniform(-20, 30)), bool(rng.integers(2)), bool(rng.integers(2))
        socs, _ = _step_levels(net, on, grid_on, levels, assets)
        for level, soc in zip(levels, socs, strict=True):
            assert _step(net, on, grid_on, float(level), assets).soc == pytest.approx(soc)
