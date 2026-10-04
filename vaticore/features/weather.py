"""Weather enrichment via Open-Meteo.

Weather is a required feature source: solar generation and load both depend on
it. This is a real client. It calls Open-Meteo (no API key needed to start),
normalises timestamps to UTC, and returns a tidy hourly frame that joins onto a
site's history on the timestamp.

Network note: run this where outbound HTTPS to open-meteo.com is allowed. The
client accepts an injected httpx.Client, so it is fully testable offline with a
mock transport and swappable for a different provider without touching callers.

Licence: Open-Meteo's free API is for non-commercial use. Commercial use (any
paying customer) needs an Open-Meteo API subscription; pass its key as
api_key (VATICORE_WEATHER_API_KEY) and requests go to the customer endpoints.
"""

from __future__ import annotations

from typing import Protocol

import httpx
import pandas as pd

from vaticore.schemas import TIMESTAMP

# Variables we pull. Shortwave radiation is the strongest solar predictor;
# temperature and cloud cover both inform load and generation.
HOURLY_VARIABLES = ("temperature_2m", "shortwave_radiation", "cloud_cover")

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CUSTOMER_ARCHIVE_URL = "https://customer-archive-api.open-meteo.com/v1/archive"
CUSTOMER_FORECAST_URL = "https://customer-api.open-meteo.com/v1/forecast"
# The forecast endpoint serves at most this many past days and days ahead.
MAX_PAST_DAYS = 92
MAX_FORECAST_DAYS = 16


class WeatherProvider(Protocol):
    """Interface every weather source implements."""

    def hourly(
        self,
        latitude: float,
        longitude: float,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        """Return hourly weather indexed by timezone aware UTC timestamp."""
        ...


class OpenMeteoProvider:
    """Open-Meteo client. Needs no API key to start.

    Parameters
    ----------
    client:
        Optional httpx.Client. Inject one in tests (with a mock transport) or to
        share connection pooling. A default client is created if omitted.
    timeout:
        Request timeout in seconds for the default client.
    api_key:
        An Open-Meteo API subscription key, required for commercial use. With
        one, requests go to the customer endpoints.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        api_key: str | None = None,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._api_key = api_key

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def hourly(
        self,
        latitude: float,
        longitude: float,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        """Fetch hourly weather for a location and date range.

        Uses the historical archive endpoint. Times are requested and returned
        in UTC. The result is indexed by timezone aware UTC timestamp with one
        column per weather variable.
        """
        params: dict[str, float | str] = {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": pd.Timestamp(start).strftime("%Y-%m-%d"),
            "end_date": pd.Timestamp(end).strftime("%Y-%m-%d"),
            "hourly": ",".join(HOURLY_VARIABLES),
            "timezone": "UTC",
        }
        url = ARCHIVE_URL
        if self._api_key:
            url, params["apikey"] = CUSTOMER_ARCHIVE_URL, self._api_key
        response = self._get_client().get(url, params=params)
        response.raise_for_status()
        return self._parse(response.json())

    def forecast(
        self,
        latitude: float,
        longitude: float,
        *,
        past_days: int = 7,
        forecast_days: int = 2,
    ) -> pd.DataFrame:
        """Recent and coming hours from the forecast endpoint, in UTC.

        The past hours are Open-Meteo's own short-range forecasts stitched
        together, so a model trained on them sees the same kind of input it
        is given for the coming day (unlike reanalysis, which is smoother
        and more accurate than any forecast).
        """
        params: dict[str, float | str | int] = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": ",".join(HOURLY_VARIABLES),
            "past_days": max(0, min(int(past_days), MAX_PAST_DAYS)),
            "forecast_days": max(1, min(int(forecast_days), MAX_FORECAST_DAYS)),
            "timezone": "UTC",
        }
        url = FORECAST_URL
        if self._api_key:
            url, params["apikey"] = CUSTOMER_FORECAST_URL, self._api_key
        response = self._get_client().get(url, params=params)
        response.raise_for_status()
        return self._parse(response.json())

    @staticmethod
    def _parse(payload: dict) -> pd.DataFrame:
        hourly = payload.get("hourly")
        if not hourly or "time" not in hourly:
            raise ValueError("Open-Meteo response missing hourly.time")
        index = pd.to_datetime(hourly["time"], utc=True)
        frame = pd.DataFrame({TIMESTAMP: index})
        for var in HOURLY_VARIABLES:
            if var in hourly:
                frame[var] = hourly[var]
        return frame

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


def join_weather(site_frame: pd.DataFrame, weather_frame: pd.DataFrame) -> pd.DataFrame:
    """Left join hourly weather onto a site's history on the UTC timestamp.

    Weather columns arrive alongside the site's load and generation, ready for
    feature building. Rows with no matching weather keep NaN, handled downstream.
    """
    merged = site_frame.merge(weather_frame, on=TIMESTAMP, how="left")
    return merged
