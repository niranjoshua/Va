"""Diesel study (research note 4): litres against how sites run today.

Note 1 measured forecasts against a planner that was already sensible. Sites
are not run that way. This study asks the question a customer asks: against
how my sites run today, how many litres does Vaticore save, with no more
outages?

Three sites, shaped from real hourly demand and solar (ENTSO-E Spain):

  minigrid    the solar mini-grid of note 1 (120 kW peak, 200 kWp, 600 kWh,
              100 kW generator), off grid
  tower       an off-grid telecom tower: 3.3 kW average load (the real demand
              shape flattened to +-15%), 8 kWp, 20 kWh, a 16 kW generator
  tower-grid  the same tower on a weak grid. The on/off pattern of the grid is
              SYNTHETIC (a seeded random process, about 15 hours a day, worse
              in the evening): no public hourly record of a Nigerian grid
              exists. Labelled as such in every report.

Every policy, today's and Vaticore's, keeps a real-time backstop: if the
battery is about to run out, the generator is started whatever the plan said,
as any site would. Reliability is therefore the same across policies and the
litres compare fairly.

Today (no forecast):
  - start when the battery runs out, follow the load: a well-run site with an
    automatic start or an attentive technician. The reference, and the
    hardest to beat;
  - an evening timer, 18:00 to midnight local, plus the backstop;
  - on the grid site, the generator whenever the grid is off (an automatic
    transfer switch), the commonest practice at grid-connected towers.

Vaticore, planned the day before from calibrated forecasts (net load and, on
the grid site, grid availability, each at the quantile chosen on the design
year), each day then run on what actually happened:
  - today's planner (load following, hour by hour; the planner of note 1);
  - run hard, then off (generator.charge_setpoint);
  - run hard plus the look-ahead planner (look_ahead).
Plus the charger setting alone with no forecast, to show what part of the
saving the setting brings and what part the forecast brings, and a perfect
forecast bound.

Protocol (docs/research/README.md): the setpoint and the plan quantile are
chosen on the design year (--phase design: 2017), then the test year (--phase
test: 2018) is run once with them.

    uv run python examples/diesel_study.py energy_dataset.csv --phase design --out results/
    uv run python examples/diesel_study.py energy_dataset.csv --phase test --setpoint 0.8 \\
        --plan-quantile 0.5 --out docs/research/results/
"""

from __future__ import annotations

import argparse
import json
import time
import warnings
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from vaticore.decisions.dispatch import SiteAssets, plan_dispatch, simulate_dispatch
from vaticore.engine import _grid_plan_for_folds, run_value_backtest
from vaticore.evaluation.backtest import FoldForecast
from vaticore.evaluation.calibration import conformalize_forecasts
from vaticore.schemas import (
    GENERATION_KW,
    GRID_AVAILABLE,
    LOAD_KW,
    OPERATOR_ID,
    SITE_ID,
    TIMESTAMP,
    with_net_load,
)

PHASES = {
    "design": ("2016-01-01", "2017-12-31T23:00"),
    "test": ("2017-01-01", "2018-12-31T23:00"),
}
CANDIDATE = "quantile_gbm_day_ahead"
REFERENCE = "Today: start when the battery runs out"
TIMER = "Today: evening timer"
ATS = "Today: generator whenever the grid is off"
V_TODAY = "Vaticore plan, today's planner"
V_HARD = "Vaticore plan, run hard"
V_AHEAD = "Vaticore plan, run hard + look ahead"
SETTING_ONLY = "Charger setting alone, no forecast"
BOUND = "Perfect forecast, run hard + look ahead"
EVENING_UTC = range(17, 23)  # 18:00 to midnight in Spain (CET), as the timer's hours


@dataclass(frozen=True)
class StudySite:
    site_id: str
    description: str
    assets: SiteAssets
    peak_load_kw: float | None = None  # mini-grid: real shape scaled to this peak
    mean_load_kw: float | None = None  # tower: real shape flattened around this mean
    swing: float = 0.15
    pv_kwp: float = 0.0
    grid: bool = False


SITES = (
    StudySite(
        "minigrid",
        "solar mini-grid, off grid",
        SiteAssets(
            battery_kwh=600.0,
            battery_power_kw=150.0,
            genset_kw=100.0,
            min_soc_kwh=60.0,
            genset_min_run_hours=2.0,
            diesel_price_per_l=1.10,
            value_of_lost_load_per_kwh=1.0,
        ),
        peak_load_kw=120.0,
        pv_kwp=200.0,
    ),
    StudySite(
        "tower",
        "telecom tower with solar, off grid",
        SiteAssets(
            battery_kwh=20.0,
            battery_power_kw=10.0,
            genset_kw=16.0,
            min_soc_kwh=2.0,
            genset_min_run_hours=2.0,
            diesel_price_per_l=1.10,
            value_of_lost_load_per_kwh=1.0,
        ),
        mean_load_kw=3.3,
        pv_kwp=8.0,
    ),
    StudySite(
        "tower-grid",
        "telecom tower with solar on a weak grid (synthetic grid pattern)",
        SiteAssets(
            battery_kwh=20.0,
            battery_power_kw=10.0,
            genset_kw=16.0,
            min_soc_kwh=2.0,
            genset_min_run_hours=2.0,
            diesel_price_per_l=1.10,
            value_of_lost_load_per_kwh=1.0,
            grid_kw=10.0,
            grid_price_per_kwh=0.15,
        ),
        mean_load_kw=3.3,
        pv_kwp=8.0,
        grid=True,
    ),
)


def _series(csv: Path, column: str) -> pd.Series:
    frame = pd.read_csv(csv, usecols=["time", column])
    stamps = pd.to_datetime(frame["time"], utc=True)
    s = pd.Series(frame[column].to_numpy(dtype=float), index=stamps)
    return s[~s.index.duplicated(keep="first")].sort_index()


def synthetic_grid(index: pd.DatetimeIndex, seed: int = 2026) -> np.ndarray:
    """A weak grid: on about 15 hours a day, failing more often in the evening.

    A two-state chain: each hour an 'on' grid fails with probability 0.10
    (0.20 from 17:00 to 23:00 UTC) and an 'off' grid returns with
    probability 0.20. Seeded, so every run sees the same pattern.
    """
    rng = np.random.default_rng(seed)
    on = np.empty(len(index))
    state = 1.0
    for i, stamp in enumerate(index):
        fail = 0.20 if stamp.hour in EVENING_UTC else 0.10
        if state == 1.0 and rng.random() < fail:
            state = 0.0
        elif state == 0.0 and rng.random() < 0.20:
            state = 1.0
        on[i] = state
    return on


def build(
    site: StudySite, csv: Path, start: str, end: str
) -> tuple[pd.DataFrame, dict[str, object]]:
    load = _series(csv, "total load actual")
    solar = _series(csv, "generation solar")
    index = pd.date_range(pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"), freq="h")
    load, solar = load.reindex(index), solar.reindex(index)
    if site.peak_load_kw is not None:
        load_kw = load / load.max() * site.peak_load_kw
    else:
        assert site.mean_load_kw is not None
        z = (load - load.mean()) / (load.max() - load.min())
        load_kw = site.mean_load_kw * (1 + 2 * site.swing * z)
    pv_kw = (solar / solar.max()).clip(lower=0.0) * site.pv_kwp
    frame = pd.DataFrame(
        {
            OPERATOR_ID: "study",
            SITE_ID: site.site_id,
            TIMESTAMP: index,
            LOAD_KW: load_kw.to_numpy(),
            GENERATION_KW: pv_kw.to_numpy(),
        }
    )
    meta: dict[str, object] = {
        "period_utc": [str(index[0]), str(index[-1])],
        "mean_load_kw": float(load_kw.mean()),
        "mean_daily_pv_kwh": float(pv_kw.mean() * 24),
        "missing_load_hours": int(load.isna().sum()),
        "missing_solar_hours": int(solar.isna().sum()),
    }
    if site.grid:
        frame[GRID_AVAILABLE] = synthetic_grid(index)
        meta["grid_on_share"] = float(frame[GRID_AVAILABLE].mean())
    return frame, meta


@dataclass
class Totals:
    fuel_l: float = 0.0
    genset_hours: float = 0.0
    genset_starts: int = 0
    unserved_kwh: float = 0.0
    unserved_hours: float = 0.0
    grid_kwh: float = 0.0
    days: int = 0


def operate(
    net: np.ndarray,
    planned_on: np.ndarray,
    soc: float,
    assets: SiteAssets,
    grid: np.ndarray | None,
) -> tuple[np.ndarray, float]:
    """Run one day as planned, with the real-time backstop; the schedule actually run.

    Hour by hour: the generator runs in planned hours and for its minimum run
    after any start; otherwise, if the battery (and grid) would not cover the
    hour, it is started anyway.
    """
    on = np.zeros(net.size, dtype=bool)
    owed = 0  # hours the generator must still run after a backstop start
    level = soc
    for i in range(net.size):
        g = None if grid is None else grid[i : i + 1]
        if owed > 0:
            run, owed = True, owed - 1
        elif planned_on[i]:
            run = True
        else:
            trial = simulate_dispatch(
                net[i : i + 1], np.array([False]), soc_kwh=level, assets=assets, grid_available=g
            )
            run = trial.unserved_kwh > 1e-9 and assets.genset_kw > 0
            if run:
                owed = assets.genset_min_run_steps - 1
        on[i] = run
        level = simulate_dispatch(
            net[i : i + 1], np.array([run]), soc_kwh=level, assets=assets, grid_available=g
        ).soc_end_kwh
    return on, level


def run_policy(
    windows: list[tuple[pd.DatetimeIndex, np.ndarray | None, np.ndarray]],
    assets: SiteAssets,
    *,
    rule: str,
    grid_plan: pd.Series | None,
    grid_actual: pd.Series | None,
) -> Totals:
    """Plan (or apply today's rule to) each day, operate it, carry the battery."""
    soc = assets.min_soc_kwh + 0.5 * (assets.battery_kwh - assets.min_soc_kwh)
    out = Totals()
    for stamps, planned, actual in windows:
        net = np.nan_to_num(actual, nan=0.0)
        grid = None if grid_actual is None else grid_actual.reindex(stamps).fillna(0.0).to_numpy()
        if rule == "plan":
            assert planned is not None
            gplan = None if grid_plan is None else grid_plan.reindex(stamps).to_numpy(dtype=float)
            planned_on = plan_dispatch(
                pd.Series(planned, index=stamps),
                soc_kwh=soc,
                assets=assets,
                planned_grid_available=gplan,
            ).genset_on
        elif rule == "timer":
            planned_on = np.array([s.hour in EVENING_UTC for s in stamps])
        elif rule == "ats":
            assert grid is not None
            planned_on = grid < 0.5
        else:  # "reactive": nothing planned; the backstop starts it when needed
            planned_on = np.zeros(len(stamps), dtype=bool)
        on, _ = operate(net, planned_on, soc, assets, grid)
        day = simulate_dispatch(net, on, soc_kwh=soc, assets=assets, grid_available=grid)
        soc = day.soc_end_kwh
        out.fuel_l += day.fuel_l
        out.genset_hours += day.genset_hours
        out.genset_starts += day.genset_starts
        out.unserved_kwh += day.unserved_kwh
        out.unserved_hours += day.unserved_hours
        out.grid_kwh += day.grid_kwh
        out.days += 1
    return out


def evaluate(
    site: StudySite,
    csv: Path,
    phase: str,
    setpoints: list[float],
    plan_quantiles: list[float],
    initial_days: int,
    grid_quantile: float = 0.5,
) -> dict[str, object]:
    start, end = PHASES[phase]
    frame, meta = build(site, csv, start, end)
    t0 = time.time()
    outcome = run_value_backtest(
        frame, assets=site.assets, initial=24 * initial_days, conformal_window=28
    )
    forecasts: list[FoldForecast] = sorted(
        conformalize_forecasts(
            outcome.backtest.forecasts_for(CANDIDATE), window_folds=28, nonnegative=False
        ).forecasts,
        key=lambda f: f.timestamps[0],
    )
    grid_plan = grid_actual = None
    if site.grid:
        grid_plan, grid_actual = _grid_plan_for_folds(
            with_net_load(frame), outcome.backtest.forecasts_for(CANDIDATE), grid_quantile
        )

    def planned_on(q: float) -> list[tuple[pd.DatetimeIndex, np.ndarray | None, np.ndarray]]:
        return [(f.timestamps, f.quantiles[q], f.actual) for f in forecasts]

    oracle = [(f.timestamps, np.nan_to_num(f.actual, nan=0.0), f.actual) for f in forecasts]
    today_only = [(f.timestamps, None, f.actual) for f in forecasts]

    base = site.assets
    policies: dict[str, Totals] = {}
    common = {"grid_actual": grid_actual}
    policies[REFERENCE] = run_policy(today_only, base, rule="reactive", grid_plan=None, **common)
    if phase == "design":
        for q in plan_quantiles:
            for sp in setpoints:
                hard_ahead = replace(base, genset_charge_setpoint=sp, look_ahead=True)
                policies[f"P{100 * q:.0f} plan, setpoint {sp:.1f}"] = run_policy(
                    planned_on(q), hard_ahead, rule="plan", grid_plan=grid_plan, **common
                )
    else:
        sp, q = setpoints[0], plan_quantiles[0]
        planned = planned_on(q)
        hard = replace(base, genset_charge_setpoint=sp)
        hard_ahead = replace(hard, look_ahead=True)
        policies[TIMER] = run_policy(today_only, base, rule="timer", grid_plan=None, **common)
        if site.grid:
            policies[ATS] = run_policy(today_only, base, rule="ats", grid_plan=None, **common)
        policies[V_TODAY] = run_policy(planned, base, rule="plan", grid_plan=grid_plan, **common)
        policies[V_HARD] = run_policy(planned, hard, rule="plan", grid_plan=grid_plan, **common)
        policies[V_AHEAD] = run_policy(
            planned, hard_ahead, rule="plan", grid_plan=grid_plan, **common
        )
        policies[SETTING_ONLY] = run_policy(
            today_only, hard, rule="reactive", grid_plan=None, **common
        )
        perfect_grid = None if grid_actual is None else grid_actual.fillna(0.0)
        policies[BOUND] = run_policy(
            oracle, hard_ahead, rule="plan", grid_plan=perfect_grid, **common
        )

    reference = REFERENCE
    ref_fuel = policies[reference].fuel_l
    value = {
        name: {
            **asdict(t),
            "fuel_saved_vs_reference": 1 - t.fuel_l / ref_fuel,
        }
        for name, t in policies.items()
    }
    return {
        "meta": {
            **meta,
            "site_id": site.site_id,
            "description": site.description,
            "phase": phase,
            "setpoints": setpoints,
            "plan_quantiles": plan_quantiles,
            "initial_days": initial_days,
            "grid_quantile": grid_quantile if site.grid else None,
            "scored_days": len(forecasts),
            "seconds": time.time() - t0,
        },
        "assets": asdict(site.assets),
        "reference": reference,
        "value": value,
        "calibration": {
            name: json.loads(report.to_frame().to_json(orient="records"))
            for name, report in outcome.calibration.items()
        },
    }


def render(result: dict[str, object]) -> str:
    meta = result["meta"]
    assets = result["assets"]
    value = result["value"]
    assert isinstance(meta, dict) and isinstance(assets, dict) and isinstance(value, dict)
    lines = [
        f"# Diesel study: {meta['description']}",
        "",
        f"Site `{meta['site_id']}`, {meta['phase']} run. Period (UTC) {meta['period_utc'][0]} to "
        f"{meta['period_utc'][1]}; scored days: {meta['scored_days']}.",
        "",
        "## Site",
        "",
        f"- Average load {meta['mean_load_kw']:.1f} kW; solar {meta['mean_daily_pv_kwh']:.0f} kWh "
        "on an average day (real ENTSO-E Spain shapes).",
        f"- Battery {assets['battery_kwh']:.0f} kWh usable, {assets['battery_power_kw']:.0f} kW, "
        f"floor {assets['min_soc_kwh']:.0f} kWh. Generator {assets['genset_kw']:.0f} kW, minimum "
        f"load {100 * assets['genset_min_load']:.0f}%, minimum run "
        f"{assets['genset_min_run_hours']:.0f} hours, HOMER default fuel curve.",
    ]
    if "grid_on_share" in meta:
        lines.append(
            f"- Grid {assets['grid_kw']:.0f} kW, on {100 * meta['grid_on_share']:.0f}% of hours. "
            "**The grid's on/off pattern is synthetic** (a seeded random process); no public "
            "hourly record of a Nigerian grid exists."
        )
    lines += [
        f"- Missing hours: load {meta['missing_load_hours']}, solar {meta['missing_solar_hours']}.",
        "",
        "## Litres against how sites run today",
        "",
        "Every policy keeps the real-time backstop (the generator is started if the battery is "
        "about to run out), so outages are comparable. Savings are against "
        f'"{result["reference"]}", the best-run practice.',
        "",
        "| Policy | Diesel (L) | Saved vs today | Generator hours | Starts | Unserved (kWh) | Outage hours |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, row in value.items():
        lines.append(
            f"| {name} | {row['fuel_l']:,.0f} | {100 * row['fuel_saved_vs_reference']:.1f}% | "
            f"{row['genset_hours']:,.0f} | {row['genset_starts']:,} | "
            f"{row['unserved_kwh']:,.0f} | {row['unserved_hours']:,.0f} |"
        )
    lines += [
        "",
        "## Method",
        "",
        "- Day-ahead net load forecasts (quantile GBM, conformal calibration over the previous "
        "28 days), rolling origin at 00:00 UTC, trained only on data before each day. Vaticore "
        f"plans on P{'/P'.join(f'{100 * q:.0f}' for q in meta['plan_quantiles'])} net load"
        + (
            f" and P{100 * meta['grid_quantile']:.0f} grid availability."
            if meta.get("grid_quantile") is not None
            else "."
        ),
        "- Each day is run on what actually happened; the battery is carried from day to day; "
        "every policy starts half full.",
        f"- Runtime {meta['seconds'] / 60:.1f} minutes.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("csv", type=Path)
    parser.add_argument("--phase", choices=sorted(PHASES), required=True)
    parser.add_argument("--setpoint", type=float, nargs="+", default=[0.6, 0.7, 0.8, 0.9, 1.0])
    parser.add_argument("--plan-quantile", type=float, nargs="+", default=[0.5, 0.9])
    parser.add_argument(
        "--grid-quantile",
        type=float,
        default=0.5,
        help="grid availability quantile a plan counts on (grid site); chosen on the design year",
    )
    parser.add_argument("--sites", nargs="+", default=[s.site_id for s in SITES])
    parser.add_argument("--initial-days", type=int, default=365)
    parser.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args()
    warnings.filterwarnings("ignore", category=UserWarning)
    if args.phase == "test" and (len(args.setpoint) != 1 or len(args.plan_quantile) != 1):
        parser.error(
            "the test run uses the one setpoint and plan quantile chosen on the design year"
        )

    args.out.mkdir(parents=True, exist_ok=True)
    for site in SITES:
        if site.site_id not in args.sites:
            continue
        result = evaluate(
            site,
            args.csv,
            args.phase,
            args.setpoint,
            args.plan_quantile,
            args.initial_days,
            args.grid_quantile,
        )
        stem = f"diesel_{args.phase}_{site.site_id}"
        if site.grid and args.phase == "design":
            stem += f"-gridP{100 * args.grid_quantile:.0f}"
        report = render(result)
        (args.out / f"{stem}.md").write_text(report)
        (args.out / f"{stem}.json").write_text(
            json.dumps(
                result,
                indent=2,
                default=lambda o: float(o) if isinstance(o, np.floating) else str(o),
            )
        )
        print(report)


if __name__ == "__main__":
    main()
