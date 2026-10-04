"""Weather in live forecasts: the weather model, the cache, and the issued archive."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from vaticore import engine
from vaticore.datasets import make_synthetic_site
from vaticore.forecasting.base import InsufficientHistoryError, quantile_column
from vaticore.pipeline import runner
from vaticore.pipeline.runner import PipelineConfig, plan_window, run_site
from vaticore.pipeline.store import PlanStore
from vaticore.schemas import TIMESTAMP
from vaticore.sites import Site
from vaticore.storage import DuckDBRepository

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(autouse=True)
def no_chronos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these tests offline and fast: Chronos-2 is unavailable here."""
    original = engine.net_load_forecast

    def without_chronos(history: pd.DataFrame, **kwargs: object) -> pd.DataFrame:
        if kwargs.get("model") == runner.CHRONOS:
            raise RuntimeError("chronos_2 is not available in this test")
        return original(history, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner.engine, "net_load_forecast", without_chronos)


FEATURES = engine.WEATHER_FEATURES
DAY = date(2026, 3, 30)


def _site() -> Site:
    return Site(
        operator_id="op", site_id="s1", name="Site One", site_type="telecom_tower",
        latitude=7.8, longitude=6.7, timezone="Africa/Lagos", currency="NGN",
        value_of_lost_load_per_kwh=5000.0,
        battery={"usable_kwh": 30.0, "power_kw": 15.0, "min_soc_kwh": 6.0},
        solar={"kwp": 12.0}, generator={"rated_kw": 16.0, "fuel_price_per_l": 1250.0},
    )  # fmt: skip


def _world(end: pd.Timestamp, days: int = 40) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Readings up to `end` and weather running two days past it."""
    frame = make_synthetic_site(
        "op", "s1", days=days + 2, seed=4, gap_fraction=0.0, with_weather=True,
        base_load_kw=4.0, solar_peak_kw=9.0,
    )  # fmt: skip
    shift = end + pd.Timedelta(days=2) - pd.Timestamp(frame[TIMESTAMP].max())
    frame[TIMESTAMP] = frame[TIMESTAMP] + pd.Timedelta(hours=round(shift / pd.Timedelta(hours=1)))
    weather = frame[[TIMESTAMP, *FEATURES]].copy()
    readings = frame[frame[TIMESTAMP] < end].drop(columns=list(FEATURES))
    return readings, weather


def test_the_weather_model_forecasts_net_load_from_weather() -> None:
    end = pd.Timestamp("2026-03-30 05:00", tz="UTC")
    readings, weather = _world(end)
    fc = engine.net_load_forecast(
        readings, horizon=24, model=engine.WEATHER_MODEL, weather=weather, calibrate=True
    )
    assert len(fc) == 24 and fc.index[0] == end
    assert list(fc.columns) == [quantile_column(q) for q in (0.1, 0.5, 0.9)]

    # Weather drives it: an overcast day ahead means more net load in daylight
    # than a clear one.
    past = weather[weather[TIMESTAMP] < end]
    future = weather[weather[TIMESTAMP] >= end].copy()
    day = future["shortwave_radiation"] > 0
    clear = future.copy()
    clear.loc[day, "shortwave_radiation"] = future["shortwave_radiation"].max()
    clear.loc[day, "cloud_cover"] = 0.0
    overcast = future.copy()
    overcast.loc[day, "shortwave_radiation"] = 20.0
    overcast.loc[day, "cloud_cover"] = 100.0
    kwargs = {"horizon": 24, "model": engine.WEATHER_MODEL, "calibrate": False}
    sunny = engine.net_load_forecast(readings, weather=pd.concat([past, clear]), **kwargs)  # type: ignore[arg-type]
    gloomy = engine.net_load_forecast(readings, weather=pd.concat([past, overcast]), **kwargs)  # type: ignore[arg-type]
    hours = pd.DatetimeIndex(future.loc[day, TIMESTAMP]).intersection(sunny.index)
    median = quantile_column(0.5)
    assert gloomy.loc[hours, median].sum() > sunny.loc[hours, median].sum() + 5.0


def test_the_weather_model_refuses_to_guess() -> None:
    end = pd.Timestamp("2026-03-30 05:00", tz="UTC")
    readings, weather = _world(end)
    with pytest.raises(ValueError, match="needs the site's weather"):
        engine.net_load_forecast(readings, horizon=24, model=engine.WEATHER_MODEL)
    no_forecast = weather[weather[TIMESTAMP] < end + pd.Timedelta(hours=10)]
    with pytest.raises(ValueError, match="no weather forecast for 14 of the 24"):
        engine.net_load_forecast(
            readings, horizon=24, model=engine.WEATHER_MODEL, weather=no_forecast
        )
    recent = weather[weather[TIMESTAMP] >= end - pd.Timedelta(days=5)]
    with pytest.raises(InsufficientHistoryError, match="14 days"):
        engine.net_load_forecast(readings, horizon=24, model=engine.WEATHER_MODEL, weather=recent)


class FakeWeather:
    def __init__(self, weather: pd.DataFrame, fail: bool = False) -> None:
        self.weather, self.fail = weather, fail
        self.calls: list[tuple[int, int]] = []

    def forecast(
        self, latitude: float, longitude: float, *, past_days: int, forecast_days: int
    ) -> pd.DataFrame:
        self.calls.append((past_days, forecast_days))
        if self.fail:
            raise RuntimeError("weather service down")
        return self.weather


def test_the_daily_run_fetches_caches_and_archives_weather() -> None:
    site = _site()
    start, _ = plan_window(site, DAY)
    readings, weather = _world(start, days=60)  # over 8 weeks: the GBM plans
    repo, store = DuckDBRepository(":memory:"), PlanStore("duckdb:///:memory:")
    repo.upsert(readings)
    provider = FakeWeather(weather)
    now = (start - pd.Timedelta(hours=6)).to_pydatetime()  # 18:00 the evening before

    run = run_site(site, repo, store, plan_date=DAY, now=now, weather=provider)
    assert run.status == "planned" and (run.model or "").startswith(runner.GBM)
    assert provider.calls == [(92, 2)]  # nothing cached yet: as far back as allowed
    forecasts = store.forecasts("op", "s1", DAY)
    shadow = forecasts[forecasts["role"] == "shadow"]
    assert f"{engine.WEATHER_MODEL}+conformal" in set(shadow["model"])
    issued = store.issued_weather("op", "s1", DAY)
    assert len(issued) == 24 and issued[TIMESTAMP].min() == start
    assert np.allclose(
        issued["shortwave_radiation"],
        weather.set_index(TIMESTAMP).loc[issued[TIMESTAMP], "shortwave_radiation"],
    )

    # The next day the service is down: the cache still serves, and the run says so.
    down = FakeWeather(weather, fail=True)
    again = run_site(site, repo, store, plan_date=DAY, now=now, weather=down)
    assert down.calls == [(2, 2)]  # only the recent days are asked for again
    assert any("weather fetch failed" in note for note in again.fallback)
    forecasts = store.forecasts("op", "s1", DAY)
    assert f"{engine.WEATHER_MODEL}+conformal" in set(forecasts["model"])


def test_without_weather_the_run_carries_on() -> None:
    site = _site()
    start, _ = plan_window(site, DAY)
    readings, weather = _world(start)
    repo, store = DuckDBRepository(":memory:"), PlanStore("duckdb:///:memory:")
    repo.upsert(readings)
    config = PipelineConfig(shadow=True)
    run = run_site(site, repo, store, plan_date=DAY, config=config,
                   weather=FakeWeather(weather, fail=True))  # fmt: skip
    assert run.status == "planned"
    assert any("weather fetch failed" in note for note in run.fallback)
    assert engine.WEATHER_MODEL not in " ".join(store.forecasts("op", "s1", DAY)["model"])
