"""Request and response models for the API.

These are the public contract of the service. They are deliberately separate
from the internal pandas/dataclass types so the wire format can stay stable
while internals evolve.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from vaticore.schemas import GENERATION_KW, LOAD_KW

Target = str  # constrained in the request model below


class ForecastRequest(BaseModel):
    operator_id: str
    site_id: str
    target: str = Field(default=LOAD_KW, description=f"{LOAD_KW} or {GENERATION_KW}")
    horizon: int = Field(default=24, ge=1, le=168)
    model: str = Field(default="quantile_gbm")
    quantiles: list[float] = Field(default=[0.1, 0.5, 0.9])


class ForecastPoint(BaseModel):
    timestamp: datetime
    quantiles: dict[str, float]


class ForecastResponse(BaseModel):
    operator_id: str
    site_id: str
    target: str
    model: str
    points: list[ForecastPoint]


class AdvisoryRequest(BaseModel):
    operator_id: str
    site_id: str
    horizon: int = Field(default=24, ge=1, le=168)
    usable_battery_kwh: float = Field(gt=0.0)
    step_hours: float = Field(default=1.0, gt=0.0)
    model: str = Field(default="quantile_gbm")


class AdvisoryResponse(BaseModel):
    operator_id: str
    site_id: str
    horizon_hours: float
    expected_net_load_kwh: float
    conservative_net_load_kwh: float
    recommended_reserve_kwh: float
    genset_recommended: bool
    note: str


class SiteInfo(BaseModel):
    operator_id: str
    site_id: str
    n_observations: int
    first_timestamp: datetime
    last_timestamp: datetime


class PlanRequest(BaseModel):
    """Ask for today's hour by hour generator schedule for one site."""

    operator_id: str
    site_id: str
    horizon: int = Field(default=24, ge=1, le=72)
    battery_kwh: float = Field(gt=0.0, description="Usable battery capacity")
    battery_power_kw: float = Field(gt=0.0)
    genset_kw: float = Field(ge=0.0)
    soc_kwh: float = Field(ge=0.0, description="Battery charge right now")
    min_soc_kwh: float = Field(default=0.0, ge=0.0)
    genset_min_run_hours: float = Field(
        default=1.0, gt=0.0, le=24.0, description="Shortest generator run once started"
    )
    diesel_price_per_l: float = Field(default=1.10, ge=0.0)
    plan_quantile: float = Field(
        default=0.9, description="Net load quantile to plan on; 0.9 holds on a bad day"
    )
    calibrate: bool = Field(default=True, description="Conformal calibration on the last week")
    model: str = Field(default="quantile_gbm_day_ahead")
    grid_kw: float = Field(default=0.0, ge=0.0, description="Grid connection size; 0 for none")
    grid_price_per_kwh: float = Field(default=0.0, ge=0.0)
    assume_grid_always_on: bool = Field(
        default=False, description="For a reliable grid with no on/off record"
    )
    timezone: str = Field(
        default="UTC",
        description="Site's IANA timezone, for example Africa/Lagos; the summary and window "
        "labels use the site's clock, timestamps stay UTC",
    )

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value


class PlanHour(BaseModel):
    timestamp: datetime
    planned_net_load_kw: float
    genset_on: bool
    planned_genset_kw: float
    planned_soc_kwh: float
    planned_grid_on: bool
    planned_grid_kw: float


class RunWindow(BaseModel):
    start: datetime
    end: datetime


class PlanResponse(BaseModel):
    operator_id: str
    site_id: str
    plan_quantile: float
    summary: str
    run_windows: list[RunWindow]
    grid_windows: list[str]
    expected_fuel_l: float
    expected_genset_hours: float
    expected_genset_starts: int
    expected_unserved_kwh: float
    hours: list[PlanHour]
    advisory_only: bool = True


class Reading(BaseModel):
    """One timestamped reading. Units: kW, percent, litres; grid 1 on, 0 off."""

    timestamp: str
    load_kw: float | None = Field(default=None, ge=0.0)
    generation_kw: float | None = Field(default=None, ge=0.0)
    grid_available: float | None = Field(default=None, ge=0.0, le=1.0)
    battery_soc_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    genset_kw: float | None = Field(default=None, ge=0.0)
    fuel_level_l: float | None = Field(default=None, ge=0.0)


class IngestBatch(BaseModel):
    readings: list[Reading] = Field(max_length=10_000)
    timezone: str | None = Field(
        default=None, description="For timestamps without an offset, e.g. Africa/Lagos"
    )

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value


class Delivery(BaseModel):
    """One diesel delivery to a site."""

    delivered_at: str = Field(description="ISO 8601 with a UTC offset, e.g. 2026-10-06T10:30+01:00")
    litres: float = Field(gt=0, le=100_000)
    reference: str | None = Field(default=None, max_length=200)


class DeliveryBatch(BaseModel):
    deliveries: list[Delivery] = Field(min_length=1, max_length=1_000)
