from __future__ import annotations

import httpx
import pandas as pd
import pytest

from vaticore.features import OpenMeteoProvider, join_weather
from vaticore.schemas import TIMESTAMP

# A canned Open-Meteo archive response, so the client is tested without network.
_FAKE_RESPONSE = {
    "latitude": 6.45,
    "longitude": 3.40,
    "hourly": {
        "time": ["2024-01-01T00:00", "2024-01-01T01:00", "2024-01-01T02:00"],
        "temperature_2m": [24.1, 23.8, 23.5],
        "shortwave_radiation": [0.0, 0.0, 0.0],
        "cloud_cover": [40, 55, 60],
    },
}


def _mock_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "archive-api.open-meteo.com"
        assert request.url.params["timezone"] == "UTC"
        return httpx.Response(200, json=_FAKE_RESPONSE)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_hourly_parses_to_utc_frame() -> None:
    provider = OpenMeteoProvider(client=_mock_client())
    frame = provider.hourly(6.45, 3.40, pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-01"))
    assert len(frame) == 3
    assert str(frame[TIMESTAMP].dtype) == "datetime64[ns, UTC]"
    assert list(frame.columns) == [
        TIMESTAMP,
        "temperature_2m",
        "shortwave_radiation",
        "cloud_cover",
    ]
    assert frame["temperature_2m"].iloc[0] == pytest.approx(24.1)


def test_http_error_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    provider = OpenMeteoProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(httpx.HTTPStatusError):
        provider.hourly(6.45, 3.40, pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-01"))


def test_join_weather_aligns_on_timestamp() -> None:
    provider = OpenMeteoProvider(client=_mock_client())
    weather = provider.hourly(6.45, 3.40, pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-01"))
    site = pd.DataFrame(
        {
            TIMESTAMP: pd.to_datetime(["2024-01-01T00:00", "2024-01-01T01:00"], utc=True),
            "load_kw": [100.0, 105.0],
        }
    )
    merged = join_weather(site, weather)
    assert len(merged) == 2
    assert "shortwave_radiation" in merged.columns
    assert merged["temperature_2m"].notna().all()


def test_live_forecast_asks_for_past_and_coming_days() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_FAKE_RESPONSE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    frame = OpenMeteoProvider(client=client).forecast(6.45, 3.4, past_days=500, forecast_days=2)
    assert len(frame) == 3
    url = seen[0].url
    assert url.host == "api.open-meteo.com" and url.path == "/v1/forecast"
    assert url.params["past_days"] == "92"  # the endpoint's limit
    assert url.params["forecast_days"] == "2" and "apikey" not in url.params


def test_a_subscription_key_uses_the_commercial_endpoints() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_FAKE_RESPONSE)

    provider = OpenMeteoProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)), api_key="KEY"
    )
    provider.forecast(6.45, 3.4)
    provider.hourly(6.45, 3.4, pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-02"))
    assert [r.url.host for r in seen] == [
        "customer-api.open-meteo.com",
        "customer-archive-api.open-meteo.com",
    ]
    assert all(r.url.params["apikey"] == "KEY" for r in seen)
