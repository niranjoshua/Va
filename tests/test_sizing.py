from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vaticore.decisions.dispatch import SiteAssets
from vaticore.decisions.sizing import (
    CO2_KG_PER_LITRE_DIESEL,
    SizingCosts,
    capital_recovery_factor,
    size_site,
)
from vaticore.engine import size_registered_site
from vaticore.features.solar import pv_output_per_kwp
from vaticore.schemas import GRID_AVAILABLE, LOAD_KW, TIMESTAMP
from vaticore.sites import Site

# An off-grid, diesel-only tower drawing a flat 4 kW.
TOWER = SiteAssets(
    battery_kwh=0.0,
    battery_power_kw=1.0,
    genset_kw=16.0,
    genset_min_load=0.25,
    diesel_price_per_l=1000.0,
    value_of_lost_load_per_kwh=5000.0,
)
COSTS = SizingCosts(
    pv_capex_per_kwp=800_000.0,
    battery_capex_per_kwh=500_000.0,
    discount_rate=0.15,
    pv_life_years=25,
    battery_life_years=10,
)


def _hours(days: int) -> pd.DatetimeIndex:
    return pd.date_range("2025-01-01", periods=24 * days, freq="h", tz="UTC")


def _sun(index: pd.DatetimeIndex) -> pd.Series:
    """Clear-sky bell: 0.8 kW per kWp at noon, zero at night."""
    h = index.hour.to_numpy()
    shape = np.clip(np.sin((h - 6) / 12 * np.pi), 0, None) * 0.8
    return pd.Series(shape, index=index)


def test_capital_recovery_factor_known_answers() -> None:
    assert capital_recovery_factor(0.0, 10) == pytest.approx(0.1)
    assert capital_recovery_factor(0.10, 10) == pytest.approx(0.16275, abs=1e-5)


def test_pv_output_model_known_answer() -> None:
    weather = pd.DataFrame(
        {
            TIMESTAMP: _hours(1)[:2],
            "shortwave_radiation": [1000.0, np.nan],
            "temperature_2m": [25.0, 25.0],
        }
    )
    out = pv_output_per_kwp(weather)
    # Cell at 25 + 25/800*1000 = 56.25 C, derate 1 - 0.004*31.25 = 0.875.
    assert out.iloc[0] == pytest.approx(0.80 * 0.875)
    assert np.isnan(out.iloc[1])


def test_diesel_only_tower_known_fuel_and_solar_cuts_it() -> None:
    index = _hours(14)
    load = pd.Series(4.0, index=index)
    report = size_site(
        load,
        _sun(index),
        TOWER,
        current_pv_kwp=0.0,
        pv_options_kwp=[0, 10, 20],
        battery_options_kwh=[0, 40],
        costs=COSTS,
        currency="NGN",
    )
    # Today: the generator runs every hour at its 4 kW floor.
    per_hour = 0.08145 * 16 + 0.246 * 4
    assert report.current.fuel_l == pytest.approx(per_hour * 8760)
    assert report.current.unserved_kwh == 0.0

    frame = report.to_frame()
    assert len(frame) == 6  # today plus five new combinations
    best = report.recommended
    assert best.pv_kwp > 0 and best.battery_kwh > 0
    assert best.fuel_l < report.current.fuel_l
    saving = report.current.operating_cost - best.operating_cost
    assert report.annual_saving(best) == pytest.approx(saving)
    assert report.payback_years(best) == pytest.approx(best.capex / saving)
    assert report.co2_avoided_tonnes(best) == pytest.approx(
        (report.current.fuel_l - best.fuel_l) * CO2_KG_PER_LITRE_DIESEL / 1000
    )
    assert report.recommended_reliable.unserved_kwh <= report.current.unserved_kwh
    assert "Recommended:" in report.summary()
    # With cheap equipment the biggest option studied wins: the report says so.
    assert report.at_search_edge(best)
    assert "widen the search" in report.summary()


def test_expensive_equipment_is_not_recommended() -> None:
    index = _hours(7)
    costly = SizingCosts(
        pv_capex_per_kwp=1e12,
        battery_capex_per_kwh=1e12,
        discount_rate=0.15,
        pv_life_years=25,
        battery_life_years=10,
    )
    report = size_site(
        pd.Series(4.0, index=index),
        _sun(index),
        TOWER,
        current_pv_kwp=0.0,
        pv_options_kwp=[10],
        battery_options_kwh=[40],
        costs=costly,
        currency="NGN",
    )
    assert report.recommended is report.current
    assert "does not pay back" in report.summary()


def test_gaps_are_filled_and_counted() -> None:
    index = _hours(14)
    load = pd.Series(4.0, index=index)
    load.iloc[[5, 30]] = np.nan
    sun = _sun(index)
    sun.iloc[12] = np.nan
    report = size_site(
        load,
        sun,
        TOWER,
        current_pv_kwp=0.0,
        pv_options_kwp=[10],
        battery_options_kwh=[0],
        costs=COSTS,
        currency="NGN",
    )
    assert (report.filled_load_hours, report.filled_solar_hours) == (2, 1)


def test_grid_sites_need_the_grid_record() -> None:
    index = _hours(7)
    grid_site = SiteAssets(battery_kwh=10.0, battery_power_kw=5.0, genset_kw=16.0, grid_kw=10.0)
    with pytest.raises(ValueError, match="grid_available"):
        size_site(
            pd.Series(4.0, index=index),
            _sun(index),
            grid_site,
            current_pv_kwp=0.0,
            pv_options_kwp=[5],
            battery_options_kwh=[10],
            costs=COSTS,
            currency="NGN",
        )


def test_registered_site_study_uses_its_record() -> None:
    index = _hours(14)
    site = Site(
        operator_id="op",
        site_id="t1",
        name="Tower",
        site_type="telecom_tower",
        latitude=6.6,
        longitude=3.5,
        timezone="Africa/Lagos",
        currency="NGN",
        value_of_lost_load_per_kwh=5000.0,
        battery={"usable_kwh": 20.0, "power_kw": 10.0, "min_soc_kwh": 2.0},
        generator={"rated_kw": 16.0, "fuel_price_per_l": 1000.0},
        grid={"capacity_kw": 10.0, "price_per_kwh": 70.0},
    )
    history = pd.DataFrame(
        {
            TIMESTAMP: index,
            LOAD_KW: 4.0,
            GRID_AVAILABLE: np.isin(index.hour, list(range(8, 16))).astype(float),
        }
    )
    report = size_registered_site(
        site,
        history,
        _sun(index),
        pv_options_kwp=[10.0],
        battery_options_kwh=[20.0, 40.0],
        costs=COSTS,
    )
    assert report.currency == "NGN"
    assert report.current.battery_kwh == 20.0 and report.current.grid_kwh > 0
    with pytest.raises(ValueError, match="grid_available"):
        size_registered_site(
            site,
            history.drop(columns=[GRID_AVAILABLE]),
            _sun(index),
            pv_options_kwp=[10.0],
            battery_options_kwh=[20.0],
            costs=COSTS,
        )
