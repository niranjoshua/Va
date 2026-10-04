from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from vaticore.engine import plan_for_site
from vaticore.schemas import GRID_AVAILABLE, TIMESTAMP
from vaticore.sites import Site, SiteType, load_portfolio

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sites" / "nigeria_portfolio.toml"


def _tower(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "operator_id": "op",
        "site_id": "t1",
        "name": "Tower",
        "site_type": "telecom_tower",
        "latitude": 6.6,
        "longitude": 3.5,
        "timezone": "Africa/Lagos",
        "currency": "ngn",
        "value_of_lost_load_per_kwh": 5000.0,
        "battery": {"usable_kwh": 24.0, "power_kw": 12.0, "min_soc_kwh": 4.8},
        "generator": {"rated_kw": 16.0, "fuel_price_per_l": 1200.0},
        "grid": {"capacity_kw": 10.0, "price_per_kwh": 70.0},
    }
    base.update(overrides)
    return base


def test_example_portfolio_loads_one_of_each_site_type() -> None:
    portfolio = load_portfolio(EXAMPLE)
    assert {s.site_type for s in portfolio.sites} == set(SiteType)
    tower = portfolio.get("example-towerco", "lag-ikd-0142")
    assert tower.grid is not None and tower.grid.tariff_band == "C"
    assert len(portfolio.by_type(SiteType.TELECOM_TOWER)) == 2
    with pytest.raises(KeyError):
        portfolio.get("example-towerco", "nope")


def test_site_maps_to_dispatch_assets() -> None:
    assets = Site(**_tower()).dispatch_assets()  # type: ignore[arg-type]
    assert assets.battery_kwh == 24.0 and assets.min_soc_kwh == 4.8
    assert assets.genset_kw == 16.0 and assets.diesel_price_per_l == 1200.0
    assert assets.grid_kw == 10.0 and assets.grid_price_per_kwh == 70.0
    off_grid = Site(**_tower(grid=None)).dispatch_assets()  # type: ignore[arg-type]
    assert off_grid.grid_kw == 0.0
    assert assets.genset_min_run_hours == 1.0
    held = Site(  # type: ignore[arg-type]
        **_tower(generator={"rated_kw": 16.0, "fuel_price_per_l": 1200.0, "min_run_hours": 2})
    ).dispatch_assets()
    assert held.genset_min_run_hours == 2.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"latitude": 95.0},
        {"timezone": "Mars/Olympus"},
        {"battery": {"usable_kwh": 10.0, "power_kw": 5.0, "min_soc_kwh": 10.0}},
        {"generator": None, "grid": None, "solar": None},
        {"unexpected_field": 1},
        {"site_type": "spaceport"},
        {"generator": {"rated_kw": 16.0, "fuel_price_per_l": 1200.0, "min_run_hours": 0}},
    ],
)
def test_bad_sites_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Site(**_tower(**overrides))  # type: ignore[arg-type]


def test_currency_is_normalised() -> None:
    assert Site(**_tower()).currency == "NGN"  # type: ignore[arg-type]


def test_plan_for_a_registered_tower(single_site: pd.DataFrame) -> None:
    # Scale the fixture down to tower size and give it a daytime grid.
    history = single_site.copy()
    history["load_kw"] = history["load_kw"] / 30.0
    history["generation_kw"] = history["generation_kw"] / 10.0
    hours = pd.DatetimeIndex(history[TIMESTAMP]).hour
    history[GRID_AVAILABLE] = np.isin(hours, list(range(8, 16))).astype(float)
    site = Site(**_tower())  # type: ignore[arg-type]
    plan = plan_for_site(site, history, soc_kwh=12.0)
    assert len(plan.timestamps) == 24
    assert plan.planned_grid_on[10] and not plan.planned_grid_on[2]
    with pytest.raises(ValueError, match="no history"):
        plan_for_site(site, history.iloc[0:0], soc_kwh=12.0)
