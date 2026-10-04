"""Victron VRM: battery charge (and, if configured, power flows) from the VRM API.

Victron inverters and battery monitors are common in Nigerian solar and hybrid
systems, and VRM (vrm.victronenergy.com) exposes their history over an API:

    GET https://vrmapi.victronenergy.com/v2/installations/{id}/stats
        ?type=custom&interval=hours&start=<unix s>&end=<unix s>
        &attributeCodes[]=bs
    X-Authorization: Token <access token>

Each record is [timestamp in ms, mean, min, max]. The attribute code "bs" is
the battery state of charge in percent, which is all the daily plan needs to
start from the battery's real charge. Codes for load, solar, grid and
generator power vary by installation; map them per source after checking the
installation in VRM, with a scale to convert watts to kW:

    [[source]]
    operator_id = "example-towerco"
    site_id = "lag-ikd-0142"
    connector = "victron_vrm"
    installation_id = "123456"
    token_env = "VATICORE_VRM_TOKEN"
    [source.attributes]         # internal name = VRM attribute code
    battery_soc_pct = "bs"      # the default if attributes is not given
    [source.scale]
    # load_kw = 0.001

Create a VRM access token under Preferences > Integrations > Access tokens,
and set auth = "Bearer" instead for a token from the login endpoint.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx
import numpy as np
import pandas as pd

from vaticore.ingestion.connectors.base import FetchResult, SourceConfig
from vaticore.schemas import (
    BATTERY_SOC_PCT,
    GENERATION_KW,
    LOAD_KW,
    OPERATOR_ID,
    SITE_ID,
    TIMESTAMP,
)

VRM_URL = "https://vrmapi.victronenergy.com/v2"
DEFAULT_ATTRIBUTES = {BATTERY_SOC_PCT: "bs"}
# One request covers at most this much history; longer ranges are chunked.
CHUNK = pd.Timedelta(days=7)


class VictronVRMConnector:
    name = "victron_vrm"

    def __init__(
        self,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        env: dict[str, str] | None = None,
    ) -> None:
        self._client = client or httpx.Client(timeout=30.0)
        self._sleep = sleep
        self._env = env if env is not None else dict(os.environ)

    def fetch(self, source: SourceConfig, start: pd.Timestamp, end: pd.Timestamp) -> FetchResult:
        installation = source.option("installation_id")
        token_env = source.option("token_env", "VATICORE_VRM_TOKEN")
        token = self._env.get(str(token_env))
        if not installation:
            raise ValueError(f"source {source.key} needs installation_id")
        if not token:
            raise ValueError(f"source {source.key}: set the VRM token in ${token_env}")
        attributes: dict[str, str] = source.option("attributes") or DEFAULT_ATTRIBUTES
        scale: dict[str, float] = source.option("scale") or {}
        interval = source.option("interval", "hours")
        headers = {"X-Authorization": f"{source.option('auth', 'Token')} {token}"}

        series: dict[str, list[pd.Series]] = {name: [] for name in attributes}
        notes: list[str] = []
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + CHUNK, end)
            params: list[tuple[str, str | int | float | bool | None]] = [
                ("type", "custom"),
                ("interval", interval),
                ("start", int(chunk_start.timestamp())),
                ("end", int(chunk_end.timestamp())),
                *[("attributeCodes[]", code) for code in attributes.values()],
            ]
            data = self._get(f"{VRM_URL}/installations/{installation}/stats", headers, params)
            records: dict[str, Any] = data.get("records") or {}
            for name, code in attributes.items():
                rows = records.get(code) or []
                if not rows:
                    continue
                stamps = pd.to_datetime([r[0] for r in rows], unit="ms", utc=True)
                values = pd.Series([r[1] for r in rows], index=stamps, dtype=float)
                series[name].append(values * float(scale.get(name, 1.0)))
            chunk_start = chunk_end

        frames = {
            name: pd.concat(parts).groupby(level=0).last()
            for name, parts in series.items()
            if parts
        }
        for name in attributes:
            if name not in frames:
                notes.append(f"no {name} readings returned for code {attributes[name]!r}")
        if not frames:
            return FetchResult(_empty(source), notes)
        frame = pd.DataFrame(frames).sort_index()
        frame = frame[(frame.index >= start) & (frame.index < end)]
        if BATTERY_SOC_PCT in frame:
            frame[BATTERY_SOC_PCT] = frame[BATTERY_SOC_PCT].clip(0.0, 100.0)
        frame.index.name = TIMESTAMP
        out = frame.reset_index()
        out.insert(0, SITE_ID, source.site_id)
        out.insert(0, OPERATOR_ID, source.operator_id)
        for column in (LOAD_KW, GENERATION_KW):
            if column not in out:
                out[column] = np.nan
        notes.insert(0, f"{len(out)} readings from VRM installation {installation}")
        return FetchResult(out, notes)

    def _get(
        self,
        url: str,
        headers: dict[str, str],
        params: list[tuple[str, str | int | float | bool | None]],
    ) -> dict[str, Any]:
        last = "no response"
        for attempt in range(1, 4):
            try:
                response = self._client.get(url, headers=headers, params=params)
            except httpx.HTTPError as exc:
                last = f"network error: {exc}"
            else:
                if response.status_code == 200:
                    data: dict[str, Any] = response.json()
                    if data.get("success") is False:
                        raise RuntimeError(f"VRM refused the request: {data.get('errors')}")
                    return data
                if response.status_code in (401, 403):
                    raise RuntimeError("VRM rejected the token (401/403): check it and its access")
                last = f"HTTP {response.status_code}: {response.text[:200]}"
                if response.status_code < 500 and response.status_code != 429:
                    raise RuntimeError(f"VRM request failed: {last}")
            if attempt < 3:
                self._sleep(2.0 ** (attempt - 1))
        raise RuntimeError(f"VRM unavailable after 3 attempts: {last}")


def _empty(source: SourceConfig) -> pd.DataFrame:
    return pd.DataFrame(
        {
            OPERATOR_ID: pd.Series([], dtype="string"),
            SITE_ID: pd.Series([], dtype="string"),
            TIMESTAMP: pd.Series([], dtype="datetime64[ns, UTC]"),
            LOAD_KW: pd.Series([], dtype=float),
            GENERATION_KW: pd.Series([], dtype=float),
        }
    )
