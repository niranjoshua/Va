"""Hour by hour battery, grid and generator planning for a site with solar.

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
  - Net load is load minus solar. Surplus charges the battery. A deficit is met
    in merit order: the grid if it is on that hour (and the site has one), then
    the generator if it is scheduled, then the battery; anything left goes
    unserved (a blackout). A generator that was not scheduled does not start:
    unplanned night call-outs are exactly the cost a day-ahead plan exists to
    avoid.
  - The grid is treated as intermittent, as it is for telecom towers, bank
    branches and factories on weak networks: each hour it is on or off. When
    on, it serves demand up to the connection size and, if allowed, recharges
    the battery with any spare capacity. Whether it will be on is itself
    forecast (see vaticore.forecasting.grid_availability); the plan counts on it
    only in the hours it is expected to be reliable.
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
    grid_kw: float = 0.0  # connection size; 0 means no grid
    grid_price_per_kwh: float = 0.0
    grid_charges_battery: bool = True

    def __post_init__(self) -> None:
        # battery_kwh may be 0 (a site with no battery, or a sizing option
        # without one); power and step must still be positive.
        if self.battery_kwh < 0:
            raise ValueError(f"battery_kwh must be zero or positive, got {self.battery_kwh}")
        positive = {
            "battery_power_kw": self.battery_power_kw,
            "step_hours": self.step_hours,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        if self.genset_kw < 0:
            raise ValueError("genset_kw must be zero or positive")
        if self.grid_kw < 0 or self.grid_price_per_kwh < 0:
            raise ValueError("grid_kw and grid_price_per_kwh must be zero or positive")
        for name, eff in (
            ("charge_efficiency", self.charge_efficiency),
            ("discharge_efficiency", self.discharge_efficiency),
        ):
            if not 0.0 < eff <= 1.0:
                raise ValueError(f"{name} must be in (0, 1], got {eff}")
        if self.battery_kwh > 0 and not 0.0 <= self.min_soc_kwh < self.battery_kwh:
            raise ValueError("min_soc_kwh must be at least 0 and below battery_kwh")
        if self.battery_kwh == 0 and self.min_soc_kwh != 0:
            raise ValueError("a site without a battery must have min_soc_kwh 0")
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
    grid_kwh: float
    grid_kw: np.ndarray  # grid import in each step (demand plus battery charging)
    grid_unknown_steps: int  # hours with unknown grid state, treated as off


@dataclass(frozen=True)
class _Step:
    soc: float
    genset_kw: float
    grid_kw: float
    unserved_kwh: float
    curtailed_kwh: float


def _step(
    net_kw: float,
    genset_on: bool,
    grid_on: bool,
    soc: float,
    assets: SiteAssets,
) -> _Step:
    """Advance one step through the merit order: grid, generator, battery."""
    dt = assets.step_hours
    residual = net_kw  # > 0 must be served, < 0 is surplus

    grid = 0.0
    if grid_on and assets.grid_kw > 0 and residual > 0:
        grid = min(residual, assets.grid_kw)
        residual -= grid

    gen = 0.0
    if genset_on and assets.genset_kw > 0 and residual > 0:
        # Load following, never below the set's minimum stable load.
        floor = assets.genset_min_load * assets.genset_kw
        gen = min(assets.genset_kw, max(residual, floor))
        residual -= gen

    unserved = curtailed = charged = 0.0
    if residual < 0:
        room_kw = (assets.battery_kwh - soc) / (assets.charge_efficiency * dt)
        charged = min(-residual, assets.battery_power_kw, max(room_kw, 0.0))
        soc += charged * assets.charge_efficiency * dt
        curtailed = (-residual - charged) * dt
    elif residual > 0:
        avail_kw = (soc - assets.min_soc_kwh) * assets.discharge_efficiency / dt
        discharge = min(residual, assets.battery_power_kw, max(avail_kw, 0.0))
        soc -= discharge / assets.discharge_efficiency * dt
        unserved = (residual - discharge) * dt

    if grid_on and assets.grid_kw > 0 and assets.grid_charges_battery and residual <= 0:
        # Spare grid capacity tops up the battery while the grid is on.
        room_kw = (assets.battery_kwh - soc) / (assets.charge_efficiency * dt)
        extra = min(
            assets.grid_kw - grid,
            max(assets.battery_power_kw - charged, 0.0),
            max(room_kw, 0.0),
        )
        if extra > 0:
            soc += extra * assets.charge_efficiency * dt
            grid += extra
    return _Step(soc, gen, grid, unserved, curtailed)


def _grid_mask(
    grid_available: npt.ArrayLike | None, n: int, assets: SiteAssets
) -> tuple[np.ndarray, int]:
    """Boolean grid-on mask and the count of unknown hours (treated as off)."""
    if assets.grid_kw <= 0:
        return np.zeros(n, dtype=bool), 0
    if grid_available is None:
        raise ValueError(
            "this site has a grid connection: pass grid_available (1 on, 0 off) for every "
            "step, or set grid_kw=0"
        )
    values = np.asarray(grid_available, dtype=float)
    if values.shape != (n,):
        raise ValueError("grid_available must have one value per step")
    unknown = int(np.isnan(values).sum())
    return np.nan_to_num(values, nan=0.0) >= 0.5, unknown


def simulate_dispatch(
    net_load_kw: npt.ArrayLike,
    genset_on: npt.ArrayLike,
    *,
    soc_kwh: float,
    assets: SiteAssets,
    grid_available: npt.ArrayLike | None = None,
) -> DispatchOutcome:
    """Run the site through a series of net load steps with a fixed schedule.

    Steps where net load is missing are simulated as zero net load (the battery
    idles) and counted in missing_steps, so a gap is visible in every report
    rather than silently changing the result. Hours with an unknown grid state
    are treated as grid off and counted in grid_unknown_steps.
    """
    net = np.asarray(net_load_kw, dtype=float)
    on = np.asarray(genset_on, dtype=bool)
    if net.shape != on.shape:
        raise ValueError("net_load_kw and genset_on must have the same length")
    if not assets.min_soc_kwh - _EPS <= soc_kwh <= assets.battery_kwh + _EPS:
        raise ValueError("starting soc_kwh is outside the battery's usable range")
    grid_on, grid_unknown = _grid_mask(grid_available, net.size, assets)

    soc = float(soc_kwh)
    socs = np.empty(net.size)
    gens = np.empty(net.size)
    grids = np.empty(net.size)
    fuel = unserved = curtailed = 0.0
    unserved_steps = starts = 0
    missing = int(np.isnan(net).sum())
    was_on = False
    for i, x in enumerate(np.nan_to_num(net, nan=0.0)):
        step = _step(float(x), bool(on[i]), bool(grid_on[i]), soc, assets)
        soc = step.soc
        socs[i], gens[i], grids[i] = soc, step.genset_kw, step.grid_kw
        if step.genset_kw > 0:
            fuel += assets.fuel_litres(step.genset_kw)
            starts += int(not was_on)
        was_on = step.genset_kw > 0
        unserved += step.unserved_kwh
        unserved_steps += int(step.unserved_kwh > _EPS)
        curtailed += step.curtailed_kwh

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
        grid_kwh=float(grids.sum() * dt),
        grid_kw=grids,
        grid_unknown_steps=grid_unknown,
    )


def _windows(
    timestamps: pd.DatetimeIndex, mask: np.ndarray, step_hours: float
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    step = pd.Timedelta(hours=step_hours)
    windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    start: pd.Timestamp | None = None
    for ts, on in zip(timestamps, mask, strict=True):
        if on and start is None:
            start = ts
        elif not on and start is not None:
            windows.append((start, ts))
            start = None
    if start is not None:
        windows.append((start, timestamps[-1] + step))
    return windows


def _labels(windows: list[tuple[pd.Timestamp, pd.Timestamp]], timezone: str = "UTC") -> list[str]:
    """Windows as clock times in the site's own timezone (data stays UTC)."""
    labels = []
    for start_utc, end_utc in windows:
        if end_utc - start_utc >= pd.Timedelta(days=1):
            labels.append("all day")
            continue
        start, end = start_utc.tz_convert(timezone), end_utc.tz_convert(timezone)
        end_txt = "midnight" if (end.hour, end.minute) == (0, 0) else f"{end:%H:%M}"
        labels.append(f"{start:%H:%M} to {end_txt}")
    return labels


@dataclass(frozen=True)
class DispatchPlan:
    """A day-ahead generator schedule and what it is expected to achieve."""

    timestamps: pd.DatetimeIndex
    planned_net_load_kw: np.ndarray
    genset_on: np.ndarray
    expected: DispatchOutcome
    assets: SiteAssets
    planned_grid_on: np.ndarray  # hours the plan counts on grid power
    display_timezone: str = "UTC"  # IANA name; labels and summary use the site's clock

    @property
    def run_windows(self) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        """Contiguous generator runs as (start, end) with end exclusive."""
        return _windows(self.timestamps, self.genset_on, self.assets.step_hours)

    @property
    def run_window_labels(self) -> list[str]:
        """Run windows as operators say them, for example '18:00 to midnight'."""
        return _labels(self.run_windows, self.display_timezone)

    @property
    def grid_window_labels(self) -> list[str]:
        """Hours the plan counts on the grid, for example '06:00 to 14:00'."""
        return _labels(
            _windows(self.timestamps, self.planned_grid_on, self.assets.step_hours),
            self.display_timezone,
        )

    def summary(self) -> str:
        """The plan in plain words, the way an operator would say it."""
        cap = self.assets.battery_kwh
        end_pct = 100.0 * self.expected.soc_end_kwh / cap if cap > 0 else 0.0
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
        if self.assets.grid_kw > 0:
            grid = self.grid_window_labels
            text += (
                f" The plan counts on grid power {', '.join(grid)}."
                if grid
                else " The plan does not count on the grid today."
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
    planned_grid_available: npt.ArrayLike | None = None,
    display_timezone: str = "UTC",
) -> DispatchPlan:
    """Schedule the generator for exactly the hours the forecast needs it.

    Walks forward through the forecast. An hour gets the generator only if,
    without it, the grid (where the plan counts on it) and the battery could
    not serve that hour's forecast net load. Plan on a high quantile (P90) of
    net load and a low quantile (P10) of grid availability to hold on a bad day;
    the value backtest measures which choices pay.
    """
    if not isinstance(planned_net_load_kw.index, pd.DatetimeIndex):
        raise ValueError("planned_net_load_kw must be indexed by timestamp")
    values = planned_net_load_kw.to_numpy(dtype=float)
    if np.isnan(values).any():
        raise ValueError("planned net load contains gaps; forecasts must be complete")

    grid_on, _ = _grid_mask(planned_grid_available, values.size, assets)

    on = np.zeros(values.size, dtype=bool)
    soc = float(soc_kwh)
    for i, x in enumerate(values):
        trial = _step(float(x), False, bool(grid_on[i]), soc, assets)
        if trial.unserved_kwh > _EPS and assets.genset_kw > 0:
            on[i] = True
            trial = _step(float(x), True, bool(grid_on[i]), soc, assets)
        soc = trial.soc

    expected = simulate_dispatch(
        values,
        on,
        soc_kwh=soc_kwh,
        assets=assets,
        grid_available=grid_on.astype(float) if assets.grid_kw > 0 else None,
    )
    return DispatchPlan(
        timestamps=pd.DatetimeIndex(planned_net_load_kw.index),
        planned_net_load_kw=values,
        genset_on=on,
        expected=expected,
        assets=assets,
        planned_grid_on=grid_on,
        display_timezone=display_timezone,
    )
