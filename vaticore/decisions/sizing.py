"""Sizing studies: how much solar and battery should a site add, and what pays?

This is the business case behind every hybrid project: before a panel is
bought, what size of solar array and battery minimises the site's cost of
power, and how quickly does it pay back? It is also how Vaticore works with
installers and solar providers: Vaticore sizes the system, partners build it,
Vaticore runs and measures it.

Method, and why:
  - A year (or more) of the site's hourly load, and solar output per kWp for
    its location (measured if the site has panels, otherwise estimated from
    weather with vaticore.features.solar), give net load for any array size.
  - Each option is simulated hour by hour with the same physics as the daily
    planner (vaticore.decisions.dispatch): grid when on, then the generator,
    then the battery. The generator starts when the site would otherwise go
    short, as a site controller does, so options are compared on equipment,
    not on forecast quality.
  - Money: equipment is annualised with the capital recovery factor at the
    stated discount rate over each component's life, so a battery that lasts
    10 years and panels that last 25 are compared fairly. Running costs are
    diesel, grid energy, maintenance, and the priced cost of unserved energy.
  - The recommendation is the option with the lowest total annual cost, plus
    the lowest-cost option that is at least as reliable as the site today.
    Both are shown, because an operator with uptime penalties may prefer the
    second.

Prices are the site's own and the caller's: capital costs, lifetimes and the
discount rate have no defaults here, so a figure is never quoted from a number
buried in code.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from vaticore.decisions.dispatch import SiteAssets, plan_dispatch

CO2_KG_PER_LITRE_DIESEL = 2.68
_HOURS_PER_YEAR = 8760.0


@dataclass(frozen=True)
class SizingCosts:
    """Capital and financing assumptions, in the site's currency."""

    pv_capex_per_kwp: float
    battery_capex_per_kwh: float
    discount_rate: float
    pv_life_years: float
    battery_life_years: float
    pv_om_per_kwp_year: float = 0.0
    battery_om_per_kwh_year: float = 0.0

    def __post_init__(self) -> None:
        if min(self.pv_capex_per_kwp, self.battery_capex_per_kwh) < 0:
            raise ValueError("capital costs must be zero or positive")
        if not 0.0 <= self.discount_rate < 1.0:
            raise ValueError("discount_rate must be in [0, 1)")
        if min(self.pv_life_years, self.battery_life_years) <= 0:
            raise ValueError("lifetimes must be positive")


def capital_recovery_factor(rate: float, years: float) -> float:
    """Share of an up-front cost to pay each year over `years` at `rate`."""
    if rate == 0:
        return 1.0 / years
    growth = (1.0 + rate) ** years
    return float(rate * growth / (growth - 1.0))


@dataclass(frozen=True)
class OptionResult:
    """One system size, run for the study period and scaled to a year."""

    pv_kwp: float
    battery_kwh: float
    added_pv_kwp: float
    added_battery_kwh: float
    capex: float
    annualised_capex: float
    annual_om: float
    fuel_l: float
    fuel_cost: float
    genset_hours: float
    grid_kwh: float
    grid_cost: float
    unserved_kwh: float
    unserved_hours: float
    unserved_cost: float
    curtailed_kwh: float

    @property
    def operating_cost(self) -> float:
        """Yearly cost of running the site: fuel, grid, upkeep and outages."""
        return self.fuel_cost + self.grid_cost + self.annual_om + self.unserved_cost

    @property
    def total_annual_cost(self) -> float:
        """Operating cost plus equipment paid off over its life."""
        return self.operating_cost + self.annualised_capex


@dataclass(frozen=True)
class SizingReport:
    """Every option, today's system, and the recommendations."""

    current: OptionResult
    options: list[OptionResult]
    currency: str
    hours: int
    filled_load_hours: int
    filled_solar_hours: int

    @property
    def recommended(self) -> OptionResult:
        """Lowest total annual cost, today's system included."""
        return min([self.current, *self.options], key=lambda o: o.total_annual_cost)

    @property
    def recommended_reliable(self) -> OptionResult:
        """Lowest cost among options at least as reliable as the site today."""
        eligible = [
            o
            for o in [self.current, *self.options]
            if o.unserved_kwh <= self.current.unserved_kwh + 1e-6
        ]
        return min(eligible, key=lambda o: o.total_annual_cost)

    def at_search_edge(self, option: OptionResult) -> list[str]:
        """Which limits of the search this option sits on ('solar', 'battery')."""
        if not self.options:
            return []
        edges = []
        if option.pv_kwp >= max(o.pv_kwp for o in self.options) and option.added_pv_kwp > 0:
            edges.append("solar")
        if (
            option.battery_kwh >= max(o.battery_kwh for o in self.options)
            and option.added_battery_kwh > 0
        ):
            edges.append("battery")
        return edges

    def annual_saving(self, option: OptionResult) -> float:
        """Operating cost saved each year against today's system."""
        return self.current.operating_cost - option.operating_cost

    def payback_years(self, option: OptionResult) -> float | None:
        """Simple payback of the added equipment; None if it never pays back."""
        saving = self.annual_saving(option)
        if option.capex == 0:
            return 0.0
        return option.capex / saving if saving > 0 else None

    def co2_avoided_tonnes(self, option: OptionResult) -> float:
        return (self.current.fuel_l - option.fuel_l) * CO2_KG_PER_LITRE_DIESEL / 1000.0

    def to_frame(self) -> pd.DataFrame:
        rows = []
        for o in [self.current, *self.options]:
            payback = self.payback_years(o)
            rows.append(
                {
                    "pv_kwp": o.pv_kwp,
                    "battery_kwh": o.battery_kwh,
                    "capex": o.capex,
                    "fuel_l": o.fuel_l,
                    "genset_hours": o.genset_hours,
                    "grid_kwh": o.grid_kwh,
                    "unserved_kwh": o.unserved_kwh,
                    "operating_cost": o.operating_cost,
                    "total_annual_cost": o.total_annual_cost,
                    "annual_saving": self.annual_saving(o),
                    "payback_years": payback,
                    "co2_avoided_t": self.co2_avoided_tonnes(o),
                    "is_current": o is self.current,
                }
            )
        return pd.DataFrame(rows)

    def summary(self) -> str:
        """The recommendation in plain words."""
        best = self.recommended
        cur = self.currency
        if best is self.current:
            return (
                "Today's system already has the lowest total cost of the options studied. "
                "Adding solar or battery at these prices does not pay back."
            )
        payback = self.payback_years(best)
        payback_txt = f"pays back in about {payback:.1f} years" if payback else "does not pay back"
        text = (
            f"Recommended: {best.pv_kwp:g} kWp of solar and {best.battery_kwh:g} kWh of battery "
            f"(adding {best.added_pv_kwp:g} kWp and {best.added_battery_kwh:g} kWh for "
            f"{cur} {best.capex:,.0f}). It cuts diesel from {self.current.fuel_l:,.0f} to "
            f"{best.fuel_l:,.0f} litres a year, saves {cur} {self.annual_saving(best):,.0f} a "
            f"year in running costs, {payback_txt}, and avoids "
            f"{self.co2_avoided_tonnes(best):,.1f} tonnes of CO2 a year."
        )
        edge = self.at_search_edge(best)
        if edge:
            text += (
                f" The best option is at the largest {' and '.join(edge)} size studied: "
                "widen the search, a larger system may be cheaper still."
            )
        reliable = self.recommended_reliable
        if reliable is not best:
            text += (
                f" If outages must not rise above today's, the cheapest such option is "
                f"{reliable.pv_kwp:g} kWp and {reliable.battery_kwh:g} kWh."
            )
        return text


def _fill_load(load: pd.Series) -> tuple[pd.Series, int]:
    """Fill load gaps with the median for that hour of the week; count them."""
    gaps = int(load.isna().sum())
    if gaps == 0:
        return load, 0
    stamps = pd.DatetimeIndex(load.index)
    how = stamps.dayofweek * 24 + stamps.hour
    typical = load.groupby(how).median()
    filled = load.copy()
    filled[load.isna()] = typical.reindex(how[load.isna()]).to_numpy()
    if filled.isna().any():
        raise ValueError("load has hours of the week with no data at all; cannot size")
    return filled, gaps


def _fill_solar(solar: pd.Series) -> tuple[pd.Series, int]:
    """Fill solar gaps with the mean for that hour of the day; count them."""
    gaps = int(solar.isna().sum())
    if gaps == 0:
        return solar, 0
    hours = pd.DatetimeIndex(solar.index).hour
    typical = solar.groupby(hours).mean()
    filled = solar.copy()
    filled[solar.isna()] = typical.reindex(hours[solar.isna()]).to_numpy()
    return filled.fillna(0.0), gaps


def size_site(
    load_kw: pd.Series,
    solar_per_kwp: pd.Series,
    base: SiteAssets,
    *,
    current_pv_kwp: float,
    pv_options_kwp: Sequence[float],
    battery_options_kwh: Sequence[float],
    costs: SizingCosts,
    currency: str,
    battery_c_rate: float = 0.5,
    min_soc_fraction: float = 0.1,
    grid_available: pd.Series | None = None,
) -> SizingReport:
    """Simulate every solar and battery size and price it against today.

    Option sizes are totals (existing plus added). Only the added part costs
    capital. base carries the site's generator, grid, prices and today's
    battery. load_kw and solar_per_kwp are hourly, indexed by UTC timestamp;
    they are aligned on load's timestamps.
    """
    if not isinstance(load_kw.index, pd.DatetimeIndex):
        raise ValueError("load_kw must be indexed by timestamp")
    if battery_c_rate <= 0 or not 0.0 <= min_soc_fraction < 1.0:
        raise ValueError("battery_c_rate must be positive and min_soc_fraction in [0, 1)")
    if base.grid_kw > 0 and grid_available is None:
        raise ValueError("this site has a grid connection: pass grid_available for every hour")

    load, filled_load = _fill_load(load_kw.astype(float))
    solar, filled_solar = _fill_solar(solar_per_kwp.reindex(load.index).astype(float))
    grid = None
    if grid_available is not None:
        grid = grid_available.reindex(load.index).to_numpy(dtype=float)
    scale = _HOURS_PER_YEAR / len(load)

    def run(pv_kwp: float, battery_kwh: float) -> OptionResult:
        if battery_kwh == base.battery_kwh:
            assets = base
        else:
            assets = replace(
                base,
                battery_kwh=battery_kwh,
                battery_power_kw=max(battery_kwh * battery_c_rate, 1e-6),
                min_soc_kwh=battery_kwh * min_soc_fraction,
            )
        net = pd.Series(load.to_numpy() - pv_kwp * solar.to_numpy(), index=load.index)
        soc0 = assets.min_soc_kwh + 0.5 * (assets.battery_kwh - assets.min_soc_kwh)
        # With the actual net load, the planner books the generator exactly
        # when the site would otherwise go short: a site controller's behaviour.
        outcome = plan_dispatch(
            net, soc_kwh=soc0, assets=assets, planned_grid_available=grid
        ).expected
        added_pv = max(pv_kwp - current_pv_kwp, 0.0)
        added_bat = max(battery_kwh - base.battery_kwh, 0.0)
        capex = added_pv * costs.pv_capex_per_kwp + added_bat * costs.battery_capex_per_kwh
        annualised = added_pv * costs.pv_capex_per_kwp * capital_recovery_factor(
            costs.discount_rate, costs.pv_life_years
        ) + added_bat * costs.battery_capex_per_kwh * capital_recovery_factor(
            costs.discount_rate, costs.battery_life_years
        )
        om = pv_kwp * costs.pv_om_per_kwp_year + battery_kwh * costs.battery_om_per_kwh_year
        return OptionResult(
            pv_kwp=pv_kwp,
            battery_kwh=battery_kwh,
            added_pv_kwp=added_pv,
            added_battery_kwh=added_bat,
            capex=capex,
            annualised_capex=annualised,
            annual_om=om,
            fuel_l=outcome.fuel_l * scale,
            fuel_cost=outcome.fuel_l * scale * base.diesel_price_per_l,
            genset_hours=outcome.genset_hours * scale,
            grid_kwh=outcome.grid_kwh * scale,
            grid_cost=outcome.grid_kwh * scale * base.grid_price_per_kwh,
            unserved_kwh=outcome.unserved_kwh * scale,
            unserved_hours=outcome.unserved_hours * scale,
            unserved_cost=outcome.unserved_kwh * scale * base.value_of_lost_load_per_kwh,
            curtailed_kwh=outcome.curtailed_kwh * scale,
        )

    current = run(current_pv_kwp, base.battery_kwh)
    options = [
        run(float(pv), float(bat))
        for pv in sorted(set(pv_options_kwp))
        for bat in sorted(set(battery_options_kwh))
        if not (np.isclose(pv, current_pv_kwp) and np.isclose(bat, base.battery_kwh))
    ]
    return SizingReport(
        current=current,
        options=options,
        currency=currency,
        hours=len(load),
        filled_load_hours=filled_load,
        filled_solar_hours=filled_solar,
    )
