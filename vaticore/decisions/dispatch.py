"""Hour by hour battery and generator planning for a solar mini-grid.

This is where a forecast becomes a schedule an operator can act on: "run the
generator 18:00 to 22:00". It is advisory only. The plan is a recommendation
for a person to approve; nothing here touches real dispatch.

One physical model, used twice:
  - plan_dispatch() runs it on a *forecast* of net load to decide which hours
    the generator should be scheduled (day-ahead commitment).
  - simulate_dispatch() runs it on what *actually* happened, with that schedule
    fixed, to count litres burned and energy not served.

Keeping both on the same model means any difference between plans comes from
the forecast alone, which is what the value backtest measures.

The site model, and why:
  - Net load is load minus solar. Surplus charges the battery; deficit is met
    from the battery, then from the generator if it is scheduled that hour,
    otherwise it goes unserved (a blackout). A generator that was not
    scheduled does not start: unplanned night call-outs are exactly the cost a
    day-ahead plan exists to avoid.
  - The generator follows load when scheduled, never below its minimum stable
    load (running diesel sets lightly loaded damages them); any excess from
    that minimum charges the battery or is curtailed.
  - Fuel use follows the standard linear fuel curve used by HOMER Pro:
    litres per hour = a * rated_kw + b * output_kw, with defaults
    a = 0.08145 L/h per kW rated and b = 0.246 L/kWh.
  - Battery limits: usable capacity, power limit, one way efficiencies and a
    protective minimum state of charge.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd

_EPS = 1e-9


@dataclass(frozen=True)
class SiteAssets:
    """Physical and economic description of one site's battery and generator.

    Costs are in one currency throughout (the defaults are US dollars).
    value_of_lost_load prices a kWh a customer needed but did not get; it is an
    assumption, so reports always show unserved kWh separately as well.
    """

    battery_kwh: float
    battery_power_kw: float
    genset_kw: float
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    min_soc_kwh: float = 0.0
    genset_min_load: float = 0.3
    fuel_intercept_l_per_kw_h: float = 0.08145
    fuel_slope_l_per_kwh: float = 0.246
    diesel_price_per_l: float = 1.10
    value_of_lost_load_per_kwh: float = 1.00
    step_hours: float = 1.0

    def __post_init__(self) -> None:
        positive = {
            "battery_kwh": self.battery_kwh,
            "battery_power_kw": self.battery_power_kw,
            "step_hours": self.step_hours,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        if self.genset_kw < 0:
            raise ValueError("genset_kw must be zero or positive")
        for name, eff in (
            ("charge_efficiency", self.charge_efficiency),
            ("discharge_efficiency", self.discharge_efficiency),
        ):
            if not 0.0 < eff <= 1.0:
                raise ValueError(f"{name} must be in (0, 1], got {eff}")
        if not 0.0 <= self.min_soc_kwh < self.battery_kwh:
            raise ValueError("min_soc_kwh must be at least 0 and below battery_kwh")
        if not 0.0 <= self.genset_min_load <= 1.0:
            raise ValueError("genset_min_load must be a fraction between 0 and 1")

    def fuel_litres(self, output_kw: float) -> float:
        """Fuel burned in one step with the generator running at output_kw."""
        per_hour = (
            self.fuel_intercept_l_per_kw_h * self.genset_kw + self.fuel_slope_l_per_kwh * output_kw
        )
        return per_hour * self.step_hours


@dataclass(frozen=True)
class DispatchOutcome:
    """What happened (or is expected to happen) over a run of steps."""

    fuel_l: float
    genset_kwh: float
    genset_hours: float
    genset_starts: int
    unserved_kwh: float
    unserved_hours: float
    curtailed_kwh: float
    missing_steps: int
    soc_end_kwh: float
    soc_kwh: np.ndarray  # state of charge at the end of each step
    genset_kw: np.ndarray  # generator output in each step


def _step(
    net_kw: float,
    genset_on: bool,
    soc: float,
    assets: SiteAssets,
) -> tuple[float, float, float, float]:
    """Advance one step. Returns (new_soc, genset_kw, unserved_kwh, curtailed_kwh)."""
    dt = assets.step_hours
    gen = 0.0
    if genset_on and assets.genset_kw > 0 and net_kw > 0:
        # Load following, never below the set's minimum stable load.
        floor = assets.genset_min_load * assets.genset_kw
        gen = min(assets.genset_kw, max(net_kw, floor))
    residual = net_kw - gen  # > 0 needs the battery, < 0 is surplus

    unserved = curtailed = 0.0
    if residual < 0:
        room_kw = (assets.battery_kwh - soc) / (assets.charge_efficiency * dt)
        charge = min(-residual, assets.battery_power_kw, max(room_kw, 0.0))
        soc += charge * assets.charge_efficiency * dt
        curtailed = (-residual - charge) * dt
    elif residual > 0:
        avail_kw = (soc - assets.min_soc_kwh) * assets.discharge_efficiency / dt
        discharge = min(residual, assets.battery_power_kw, max(avail_kw, 0.0))
        soc -= discharge / assets.discharge_efficiency * dt
        unserved = (residual - discharge) * dt
    return soc, gen, unserved, curtailed


def simulate_dispatch(
    net_load_kw: npt.ArrayLike,
    genset_on: npt.ArrayLike,
    *,
    soc_kwh: float,
    assets: SiteAssets,
) -> DispatchOutcome:
    """Run the site through a series of net load steps with a fixed schedule.

    Steps where net load is missing are simulated as zero net load (the battery
    idles) and counted in missing_steps, so a gap is visible in every report
    rather than silently changing the result.
    """
    net = np.asarray(net_load_kw, dtype=float)
    on = np.asarray(genset_on, dtype=bool)
    if net.shape != on.shape:
        raise ValueError("net_load_kw and genset_on must have the same length")
    if not assets.min_soc_kwh - _EPS <= soc_kwh <= assets.battery_kwh + _EPS:
        raise ValueError("starting soc_kwh is outside the battery's usable range")

    soc = float(soc_kwh)
    socs = np.empty(net.size)
    gens = np.empty(net.size)
    fuel = unserved = curtailed = 0.0
    unserved_steps = starts = 0
    missing = int(np.isnan(net).sum())
    was_on = False
    for i, (x, sched) in enumerate(zip(np.nan_to_num(net, nan=0.0), on, strict=True)):
        soc, gen, short, spill = _step(float(x), bool(sched), soc, assets)
        socs[i], gens[i] = soc, gen
        if gen > 0:
            fuel += assets.fuel_litres(gen)
            starts += int(not was_on)
        was_on = gen > 0
        unserved += short
        unserved_steps += int(short > _EPS)
        curtailed += spill

    dt = assets.step_hours
    return DispatchOutcome(
        fuel_l=fuel,
        genset_kwh=float(gens.sum() * dt),
        genset_hours=float((gens > 0).sum() * dt),
        genset_starts=starts,
        unserved_kwh=unserved,
        unserved_hours=unserved_steps * dt,
        curtailed_kwh=curtailed,
        missing_steps=missing,
        soc_end_kwh=soc,
        soc_kwh=socs,
        genset_kw=gens,
    )


@dataclass(frozen=True)
class DispatchPlan:
    """A day-ahead generator schedule and what it is expected to achieve."""

    timestamps: pd.DatetimeIndex
    planned_net_load_kw: np.ndarray
    genset_on: np.ndarray
    expected: DispatchOutcome
    assets: SiteAssets

    @property
    def run_windows(self) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        """Contiguous generator runs as (start, end) with end exclusive."""
        step = pd.Timedelta(hours=self.assets.step_hours)
        windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        start: pd.Timestamp | None = None
        for ts, on in zip(self.timestamps, self.genset_on, strict=True):
            if on and start is None:
                start = ts
            elif not on and start is not None:
                windows.append((start, ts))
                start = None
        if start is not None:
            windows.append((start, self.timestamps[-1] + step))
        return windows

    @property
    def run_window_labels(self) -> list[str]:
        """Run windows as operators say them, for example '18:00 to midnight'."""
        labels = []
        for start, end in self.run_windows:
            end_txt = "midnight" if (end.hour, end.minute) == (0, 0) else f"{end:%H:%M}"
            labels.append(f"{start:%H:%M} to {end_txt}")
        return labels

    def summary(self) -> str:
        """The plan in plain words, the way an operator would say it."""
        end_pct = 100.0 * self.expected.soc_end_kwh / self.assets.battery_kwh
        windows = self.run_windows
        if not windows:
            text = (
                f"No generator needed. The battery covers the plan and ends at "
                f"about {end_pct:.0f}% charge."
            )
        else:
            spans = ", ".join(self.run_window_labels)
            text = (
                f"Run the generator {spans} ({self.expected.genset_hours:.0f} h, "
                f"about {self.expected.fuel_l:.0f} L of diesel). "
                f"The battery ends at about {end_pct:.0f}% charge."
            )
        if self.expected.unserved_kwh > 0.5:
            text += (
                f" Even with the generator, about {self.expected.unserved_kwh:.0f} kWh may go "
                "unserved on this plan: consider shifting or shedding flexible load."
            )
        return text


def plan_dispatch(
    planned_net_load_kw: pd.Series,
    *,
    soc_kwh: float,
    assets: SiteAssets,
) -> DispatchPlan:
    """Schedule the generator for exactly the hours the forecast needs it.

    Walks forward through the forecast. An hour gets the generator only if,
    without it, the battery could not serve that hour's forecast net load.
    Plan on a high quantile (P90) of net load to hold on a bad day, or on the
    median to plan for an ordinary one: the value backtest measures which pays.
    """
    if not isinstance(planned_net_load_kw.index, pd.DatetimeIndex):
        raise ValueError("planned_net_load_kw must be indexed by timestamp")
    values = planned_net_load_kw.to_numpy(dtype=float)
    if np.isnan(values).any():
        raise ValueError("planned net load contains gaps; forecasts must be complete")

    on = np.zeros(values.size, dtype=bool)
    soc = float(soc_kwh)
    for i, x in enumerate(values):
        trial_soc, _, short, _ = _step(float(x), False, soc, assets)
        if short > _EPS and assets.genset_kw > 0:
            on[i] = True
            trial_soc, _, _, _ = _step(float(x), True, soc, assets)
        soc = trial_soc

    expected = simulate_dispatch(values, on, soc_kwh=soc_kwh, assets=assets)
    return DispatchPlan(
        timestamps=pd.DatetimeIndex(planned_net_load_kw.index),
        planned_net_load_kw=values,
        genset_on=on,
        expected=expected,
        assets=assets,
    )
