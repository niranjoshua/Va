"""The value backtest: what a forecast is worth in litres, money and outages.

Pinball loss says which forecast is closer. Operators need to know which one
saves diesel and keeps the lights on. This harness answers that directly.

For each policy (a forecast source plus the quantile it plans on) it walks the
backtest day by day, as a real site would:

  1. At the forecast origin, with the battery wherever the previous days left
     it, schedule the generator from the forecast (plan_dispatch).
  2. Run the day against what actually happened, with that schedule fixed
     (simulate_dispatch), and carry the battery state into the next day.

Every policy sees the same days, the same site and the same starting battery,
so differences in fuel, outages and cost come from the forecast alone. A
perfect forecast row, which plans with the same rule on the actual outcome,
bounds what any forecast could achieve under that rule and turns savings into
"share of the possible gain". It is not a globally optimal dispatch.

No look ahead: each plan uses only its own forecast (issued at the origin) and
the battery state at that moment. The perfect forecast row is the one
deliberate exception and is labelled as a bound, not a policy.

The cost of an outage (value of lost load) is an assumption that changes the
ranking, so ValueReport.sensitivity() recomputes total cost across a range of
values rather than hiding the choice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise

import numpy as np
import pandas as pd

from vaticore.decisions.dispatch import SiteAssets, plan_dispatch, simulate_dispatch
from vaticore.evaluation.backtest import FoldForecast

PERFECT_FORECAST = "perfect forecast"


@dataclass(frozen=True)
class PolicySource:
    """A policy: the forecasts it plans from and the quantile it plans on.

    For a site with a grid connection, grid_plan says which hours the policy
    counts on grid power (1 or 0, indexed by timestamp), as forecast at each
    origin. It must never be built from the actual grid record.
    """

    forecasts: Sequence[FoldForecast]
    plan_quantile: float
    grid_plan: pd.Series | None = None


@dataclass(frozen=True)
class PolicyResult:
    """Totals for one policy over the whole backtest."""

    name: str
    days: float
    fuel_l: float
    fuel_cost: float
    genset_hours: float
    genset_starts: int
    unserved_kwh: float
    unserved_hours: float
    unserved_cost: float
    curtailed_kwh: float
    missing_steps: int
    grid_kwh: float = 0.0
    grid_cost: float = 0.0

    @property
    def total_cost(self) -> float:
        """Fuel, grid energy, and the priced cost of energy not served."""
        return self.fuel_cost + self.grid_cost + self.unserved_cost

    def cost_at(self, value_of_lost_load_per_kwh: float) -> float:
        """Total cost if an unserved kWh were priced differently."""
        return self.fuel_cost + self.grid_cost + self.unserved_kwh * value_of_lost_load_per_kwh


@dataclass(frozen=True)
class ValueReport:
    """Policy results side by side, with savings against the baseline."""

    assets: SiteAssets
    results: dict[str, PolicyResult]
    baseline: str

    def savings(self, name: str) -> float:
        """Total cost saved against the baseline policy (positive is better)."""
        return self.results[self.baseline].total_cost - self.results[name].total_cost

    def share_of_possible(self, name: str) -> float | None:
        """Share of the gap between the baseline and a perfect forecast closed.

        1.0 means as good as knowing the future; 0.0 means no better than the
        baseline. None when there is no gap to close.
        """
        if PERFECT_FORECAST not in self.results:
            return None
        possible = self.savings(PERFECT_FORECAST)
        if possible <= 1e-9:
            return None
        return self.savings(name) / possible

    def sensitivity(self, values_of_lost_load: Sequence[float]) -> pd.DataFrame:
        """Savings against the baseline at several prices of an unserved kWh.

        Rows are policies, columns are the prices. Shows whether a ranking holds
        whatever an outage is assumed to cost.
        """
        base = self.results[self.baseline]
        table = {
            voll: {n: base.cost_at(voll) - r.cost_at(voll) for n, r in self.results.items()}
            for voll in values_of_lost_load
        }
        frame = pd.DataFrame(table)
        frame.index.name = "policy"
        frame.columns.name = "value_of_lost_load_per_kwh"
        return frame

    def to_frame(self) -> pd.DataFrame:
        rows = []
        for name, r in self.results.items():
            rows.append(
                {
                    "policy": name,
                    "fuel_l": r.fuel_l,
                    "grid_kwh": r.grid_kwh,
                    "genset_hours": r.genset_hours,
                    "genset_starts": r.genset_starts,
                    "unserved_kwh": r.unserved_kwh,
                    "unserved_hours": r.unserved_hours,
                    "fuel_cost": r.fuel_cost,
                    "unserved_cost": r.unserved_cost,
                    "total_cost": r.total_cost,
                    "savings_vs_baseline": self.savings(name),
                    "share_of_possible": self.share_of_possible(name),
                }
            )
        return pd.DataFrame(rows).set_index("policy")


def _check_back_to_back(folds: Sequence[FoldForecast], step: pd.Timedelta) -> None:
    for prev, nxt in pairwise(folds):
        if nxt.timestamps[0] != prev.timestamps[-1] + step:
            raise ValueError(
                "the value backtest needs back to back windows (backtest step equal to "
                f"horizon); window ending {prev.timestamps[-1]} is followed by one starting "
                f"{nxt.timestamps[0]}"
            )


def simulate_policy(
    name: str,
    windows: Sequence[tuple[pd.DatetimeIndex, np.ndarray, np.ndarray]],
    *,
    assets: SiteAssets,
    initial_soc_kwh: float,
    grid_plan: pd.Series | None = None,
    grid_actual: pd.Series | None = None,
) -> PolicyResult:
    """Plan then operate each window in turn, carrying the battery forward.

    Each window is (timestamps, planned net load, actual net load). For a site
    with a grid, grid_plan and grid_actual (1 on, 0 off, indexed by timestamp)
    give the hours the plan counted on and the hours the grid was really on.
    """
    has_grid = assets.grid_kw > 0
    if has_grid and (grid_plan is None or grid_actual is None):
        raise ValueError(f"policy {name!r}: a grid site needs both a grid plan and the grid record")
    soc = initial_soc_kwh
    fuel = gen_h = unserved = unserved_h = curtailed = grid_kwh = 0.0
    starts = missing = 0
    steps = 0
    for stamps, planned, actual in windows:
        planned_grid = actual_grid = None
        if has_grid:
            assert grid_plan is not None and grid_actual is not None
            planned_grid = grid_plan.reindex(stamps).to_numpy(dtype=float)
            actual_grid = grid_actual.reindex(stamps).to_numpy(dtype=float)
        plan = plan_dispatch(
            pd.Series(planned, index=stamps),
            soc_kwh=soc,
            assets=assets,
            planned_grid_available=planned_grid,
        )
        outcome = simulate_dispatch(
            actual, plan.genset_on, soc_kwh=soc, assets=assets, grid_available=actual_grid
        )
        soc = outcome.soc_end_kwh
        grid_kwh += outcome.grid_kwh
        fuel += outcome.fuel_l
        gen_h += outcome.genset_hours
        starts += outcome.genset_starts
        unserved += outcome.unserved_kwh
        unserved_h += outcome.unserved_hours
        curtailed += outcome.curtailed_kwh
        missing += outcome.missing_steps
        steps += len(stamps)
    return PolicyResult(
        name=name,
        days=steps * assets.step_hours / 24.0,
        fuel_l=fuel,
        fuel_cost=fuel * assets.diesel_price_per_l,
        genset_hours=gen_h,
        genset_starts=starts,
        unserved_kwh=unserved,
        unserved_hours=unserved_h,
        unserved_cost=unserved * assets.value_of_lost_load_per_kwh,
        curtailed_kwh=curtailed,
        missing_steps=missing,
        grid_kwh=grid_kwh,
        grid_cost=grid_kwh * assets.grid_price_per_kwh,
    )


def value_backtest(
    policies: Mapping[str, PolicySource],
    *,
    assets: SiteAssets,
    baseline: str,
    initial_soc_frac: float = 0.5,
    include_perfect_forecast: bool = True,
    grid_actual: pd.Series | None = None,
) -> ValueReport:
    """Price each policy's plans against what actually happened.

    All policies must cover exactly the same back to back windows. Missing
    forecast values cannot be planned on and raise; missing actuals are
    simulated as zero net load and counted in each result's missing_steps.
    For a grid site, pass grid_actual (1 on, 0 off, NaN unknown, by timestamp)
    and give every policy a grid_plan; unknown grid hours count as off.
    """
    if baseline not in policies:
        raise ValueError(f"baseline policy {baseline!r} is not among the policies")
    if not 0.0 <= initial_soc_frac <= 1.0:
        raise ValueError("initial_soc_frac must be between 0 and 1")

    ordered = {
        name: sorted(src.forecasts, key=lambda f: f.timestamps[0]) for name, src in policies.items()
    }
    reference = ordered[baseline]
    if not reference:
        raise ValueError("baseline policy has no forecasts")
    reference_stamps = [tuple(f.timestamps) for f in reference]
    for name, folds in ordered.items():
        if [tuple(f.timestamps) for f in folds] != reference_stamps:
            raise ValueError(f"policy {name!r} does not cover the same windows as the baseline")
    _check_back_to_back(reference, pd.Timedelta(hours=assets.step_hours))

    soc0 = assets.min_soc_kwh + initial_soc_frac * (assets.battery_kwh - assets.min_soc_kwh)
    results: dict[str, PolicyResult] = {}
    for name, src in policies.items():
        windows = [(f.timestamps, f.quantiles[src.plan_quantile], f.actual) for f in ordered[name]]
        results[name] = simulate_policy(
            name,
            windows,
            assets=assets,
            initial_soc_kwh=soc0,
            grid_plan=src.grid_plan,
            grid_actual=grid_actual,
        )

    if include_perfect_forecast:
        oracle = [(f.timestamps, np.nan_to_num(f.actual, nan=0.0), f.actual) for f in reference]
        perfect_grid = None if grid_actual is None else grid_actual.fillna(0.0)
        results[PERFECT_FORECAST] = simulate_policy(
            PERFECT_FORECAST,
            oracle,
            assets=assets,
            initial_soc_kwh=soc0,
            grid_plan=perfect_grid,
            grid_actual=grid_actual,
        )
    return ValueReport(assets=assets, results=results, baseline=baseline)
