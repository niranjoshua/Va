"""Sites and their energy assets: the domain model every other layer reads.

One engine serves every kind of distributed site. A telecom tower, a bank
branch, a factory, a hospital and a rural mini-grid differ in size and in what
they care about, not in physics: each has demand, maybe solar, a battery, maybe
a generator and maybe an unreliable grid. So they share one model, and a
site_type records what the site is for, which drives reporting and defaults,
never separate code paths.

Every site is scoped by operator_id and site_id, carries its location (for
weather), its local timezone (for display only; data stays in UTC) and the
currency its prices are in. Portfolios load from TOML so an operator's team
can review and edit them without code.

Prices are always the site's own. The example portfolios use illustrative
figures and say so; they must be replaced with the operator's real diesel,
grid and outage costs before any saving is quoted.
"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vaticore.decisions.dispatch import SiteAssets


class SiteType(StrEnum):
    """What a site is for. Drives defaults and reporting, not physics."""

    TELECOM_TOWER = "telecom_tower"
    BANK_BRANCH = "bank_branch"
    COMMERCIAL_INDUSTRIAL = "commercial_industrial"
    INSTITUTION = "institution"  # hospitals, schools, universities, government
    MINI_GRID = "mini_grid"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SolarArray(_Strict):
    kwp: float = Field(gt=0, description="Peak DC capacity")
    tilt_deg: float | None = Field(default=None, ge=0, le=90)
    azimuth_deg: float | None = Field(default=None, ge=0, lt=360)


class Battery(_Strict):
    usable_kwh: float = Field(gt=0)
    power_kw: float = Field(gt=0)
    min_soc_kwh: float = Field(default=0.0, ge=0)
    charge_efficiency: float = Field(default=0.95, gt=0, le=1)
    discharge_efficiency: float = Field(default=0.95, gt=0, le=1)
    chemistry: str | None = None

    @model_validator(mode="after")
    def _floor_below_capacity(self) -> Battery:
        if self.min_soc_kwh >= self.usable_kwh:
            raise ValueError("min_soc_kwh must be below usable_kwh")
        return self


class Generator(_Strict):
    rated_kw: float = Field(gt=0)
    min_load_fraction: float = Field(default=0.3, ge=0, le=1)
    min_run_hours: float = Field(
        default=1.0, gt=0, le=24, description="Shortest run once started, in hours"
    )
    fuel_intercept_l_per_kw_h: float = Field(default=0.08145, ge=0)
    fuel_slope_l_per_kwh: float = Field(default=0.246, ge=0)
    fuel_price_per_l: float = Field(ge=0)


class GridConnection(_Strict):
    capacity_kw: float = Field(gt=0)
    price_per_kwh: float = Field(ge=0)
    charges_battery: bool = True
    reliable: bool = Field(
        default=False,
        description="True only for a grid that is effectively always on; otherwise "
        "the site must record grid on/off hours so availability can be forecast",
    )
    tariff_band: str | None = Field(
        default=None, description="Supply band where published, for example NERC Band A"
    )


class Site(_Strict):
    """One site in an operator's portfolio."""

    operator_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    name: str
    site_type: SiteType
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str = Field(description="IANA name, for display only; data stays UTC")
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217 code")
    value_of_lost_load_per_kwh: float = Field(
        ge=0, description="What an unserved kWh costs this site, in its currency"
    )
    battery: Battery
    solar: SolarArray | None = None
    generator: Generator | None = None
    grid: GridConnection | None = None
    tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _has_a_backup(self) -> Site:
        if self.generator is None and self.grid is None and self.solar is None:
            raise ValueError("a site needs at least one of solar, a generator or a grid")
        return self

    @property
    def key(self) -> tuple[str, str]:
        return (self.operator_id, self.site_id)

    def dispatch_assets(self) -> SiteAssets:
        """The physical and economic view the planner and simulator use."""
        gen, grid, bat = self.generator, self.grid, self.battery
        return SiteAssets(
            battery_kwh=bat.usable_kwh,
            battery_power_kw=bat.power_kw,
            min_soc_kwh=bat.min_soc_kwh,
            charge_efficiency=bat.charge_efficiency,
            discharge_efficiency=bat.discharge_efficiency,
            genset_kw=gen.rated_kw if gen else 0.0,
            genset_min_load=gen.min_load_fraction if gen else 0.3,
            genset_min_run_hours=gen.min_run_hours if gen else 1.0,
            fuel_intercept_l_per_kw_h=gen.fuel_intercept_l_per_kw_h if gen else 0.08145,
            fuel_slope_l_per_kwh=gen.fuel_slope_l_per_kwh if gen else 0.246,
            diesel_price_per_l=gen.fuel_price_per_l if gen else 0.0,
            value_of_lost_load_per_kwh=self.value_of_lost_load_per_kwh,
            grid_kw=grid.capacity_kw if grid else 0.0,
            grid_price_per_kwh=grid.price_per_kwh if grid else 0.0,
            grid_charges_battery=grid.charges_battery if grid else True,
        )


class Portfolio(_Strict):
    """An operator's sites, unique by (operator_id, site_id)."""

    sites: tuple[Site, ...]

    @model_validator(mode="after")
    def _unique(self) -> Portfolio:
        keys = [s.key for s in self.sites]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ValueError(f"duplicate sites: {dupes}")
        return self

    def get(self, operator_id: str, site_id: str) -> Site:
        for site in self.sites:
            if site.key == (operator_id, site_id):
                return site
        raise KeyError(f"no site {site_id!r} for operator {operator_id!r}")

    def by_type(self, site_type: SiteType) -> list[Site]:
        return [s for s in self.sites if s.site_type == site_type]


def load_portfolio(path: str | Path) -> Portfolio:
    """Load sites from a TOML file with one [[site]] table per site."""
    with open(path, "rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)
    return Portfolio(sites=tuple(Site(**entry) for entry in raw.get("site", [])))
