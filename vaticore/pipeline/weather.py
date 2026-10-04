"""Weather for live forecasts: fetched once a day per site, cached, never blocking.

Each run asks the weather provider for the hours since the site's weather was
last fetched (plus two days, since recent hours are revised) and the coming
days, stores them, and reads the site's cached weather back. If the provider
is down, the cache still serves the history; if the cache does not cover the
plan day, the weather model simply does not run that day and the run says so.
Plans never wait on weather.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Protocol

import pandas as pd

from vaticore.features.weather import MAX_PAST_DAYS
from vaticore.pipeline.store import PlanStore
from vaticore.sites.model import Site

log = logging.getLogger("vaticore.pipeline")

REFETCH = pd.Timedelta(days=2)


class LiveWeather(Protocol):
    def forecast(
        self, latitude: float, longitude: float, *, past_days: int, forecast_days: int
    ) -> pd.DataFrame: ...


def site_weather(
    site: Site,
    store: PlanStore,
    provider: LiveWeather,
    start: pd.Timestamp,
    end: pd.Timestamp,
    now: datetime,
    notes: list[str],
) -> pd.DataFrame | None:
    """Hourly weather for a site from `start` to `end`, or None if there is none.

    Fetch problems are added to `notes` (they appear in the stored run) and
    never raised.
    """
    now_ts = pd.Timestamp(now).tz_convert("UTC")
    # Open-Meteo counts past_days back from, and forecast_days on from, the
    # start of today (UTC). The cache may hold forecast hours beyond now: the
    # recent past is fetched again either way, since it gets revised.
    today = now_ts.floor("D")
    last = store.last_weather_at(site.operator_id, site.site_id)
    since = (min(last, now_ts) - REFETCH) if last is not None else start
    day = pd.Timedelta(days=1)
    past_days = min(MAX_PAST_DAYS, max(1, math.ceil((today - since) / day)))
    ahead = max(1, math.ceil((end - today) / day))
    try:
        fetched = provider.forecast(
            site.latitude, site.longitude, past_days=past_days, forecast_days=ahead
        )
        if not fetched.empty:
            store.save_weather(site.operator_id, site.site_id, fetched, now_ts.to_pydatetime())
    except Exception as exc:  # reported in the run; the cache may still serve
        log.warning("weather for %s/%s failed: %s", site.operator_id, site.site_id, exc)
        notes.append(f"weather fetch failed ({type(exc).__name__}); using cached weather")
    cached = store.weather(
        site.operator_id, site.site_id, start.to_pydatetime(), end.to_pydatetime()
    )
    return None if cached.empty else cached
