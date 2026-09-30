"""Adapter for Elia open data exports (Belgian transmission system operator).

Elia publishes 15 minute series with its own probabilistic forecasts, which
makes them a rare professional benchmark: we can score Vaticore against a grid
operator's P10, P50 and P90, not only against persistence.

Supported exports (semicolon separated, timestamps with a UTC offset):
  - Total load, dataset ods001: Datetime; Resolution code; Total Load; Most
    recent forecast / P10 / P90; Day-ahead 6PM forecast / P10 / P90;
    Week-ahead forecast.
  - Solar generation by region, for example ods032: Datetime; Resolution code;
    Region; Measured & Upscaled; ...; Day Ahead 11AM forecast / P10 / P90; ...;
    Monitored capacity; Load factor.

Values are in MW. Readers keep MW and Elia's own column meaning; to_site_frame
converts to the internal schema (kW, UTC, hourly) for one site. Adapters live at
the ingestion boundary so the rest of the system only ever sees the internal
schema.

Resampling rule, stated once: an hour's value is the mean of its 15 minute
values when at least `min_readings` of them are present, otherwise it is a gap
(NaN). Averaging Elia's 15 minute quantiles gives an approximate hourly
quantile; because errors within an hour are strongly correlated the
approximation is close, and it errs towards a slightly wider band.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from vaticore.schemas import GENERATION_KW, LOAD_KW, OPERATOR_ID, SITE_ID, TIMESTAMP

# Elia column -> our column. Forecast columns keep an "elia_" prefix so they
# are never mistaken for Vaticore's own forecasts.
_LOAD_COLUMNS = {
    "Total Load": "load_mw",
    "Most recent forecast": "elia_recent_p50_mw",
    "Most recent P10": "elia_recent_p10_mw",
    "Most recent P90": "elia_recent_p90_mw",
    "Day-ahead 6PM forecast": "elia_da_p50_mw",
    "Day-ahead 6PM P10": "elia_da_p10_mw",
    "Day-ahead 6PM P90": "elia_da_p90_mw",
    "Week-ahead forecast": "elia_week_p50_mw",
}

_SOLAR_COLUMNS = {
    "Measured & Upscaled": "solar_mw",
    "Day Ahead 11AM forecast": "elia_da_p50_mw",
    "Day Ahead 11AM P10": "elia_da_p10_mw",
    "Day Ahead 11AM P90": "elia_da_p90_mw",
    "Monitored capacity": "capacity_mw",
}


class EliaFormatError(ValueError):
    """Raised when a file does not look like the expected Elia export."""


def _read(path: str | Path, columns: dict[str, str], region: str | None) -> pd.DataFrame:
    raw = pd.read_csv(path, sep=";")
    required = {"Datetime", *columns}
    if region is not None:
        required.add("Region")
    missing = sorted(required - set(raw.columns))
    if missing:
        raise EliaFormatError(f"{path}: missing Elia columns {missing}")
    if region is not None:
        raw = raw[raw["Region"] == region]
        if raw.empty:
            raise EliaFormatError(f"{path}: no rows for region {region!r}")
    frame = raw[["Datetime", *columns]].rename(columns={"Datetime": TIMESTAMP, **columns})
    frame[TIMESTAMP] = pd.to_datetime(frame[TIMESTAMP], utc=True)
    frame = frame.drop_duplicates(subset=[TIMESTAMP]).sort_values(TIMESTAMP)
    return frame.set_index(TIMESTAMP)


def read_elia_load(path: str | Path) -> pd.DataFrame:
    """Read an Elia total load export, indexed by UTC timestamp, values in MW."""
    return _read(path, _LOAD_COLUMNS, region=None)


def read_elia_solar(path: str | Path, region: str = "Belgium") -> pd.DataFrame:
    """Read an Elia solar export for one region, indexed by UTC timestamp, in MW."""
    return _read(path, _SOLAR_COLUMNS, region=region)


def to_hourly(frame: pd.DataFrame, *, min_readings: int = 3) -> pd.DataFrame:
    """Hourly means of 15 minute values; hours with too few readings become gaps."""
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("frame must be indexed by timestamp")
    grouped = frame.resample("1h")
    means = grouped.mean()
    counts = grouped.count()
    return means.where(counts >= min_readings)


def to_site_frame(
    load: pd.DataFrame | None,
    solar: pd.DataFrame | None,
    *,
    operator_id: str,
    site_id: str,
) -> pd.DataFrame:
    """Internal schema frame (kW, hourly, UTC) from Elia load and/or solar.

    Only the measured series become load_kw and generation_kw. Elia's forecast
    columns are carried through in kW for benchmarking, prefixed load_ or
    solar_ (for example load_elia_da_p90_kw, solar_elia_da_p10_kw). Timestamps
    are the union of both inputs on a regular hourly grid, so a gap in one
    series is a visible NaN rather than a dropped row.
    """
    if load is None and solar is None:
        raise ValueError("provide load, solar or both")
    parts = []
    if load is not None:
        hourly = to_hourly(load) * 1000.0
        parts.append(
            hourly.rename(
                columns={
                    c: LOAD_KW if c == "load_mw" else "load_" + c.replace("_mw", "_kw")
                    for c in hourly.columns
                }
            )
        )
    if solar is not None:
        hourly = to_hourly(solar) * 1000.0
        parts.append(
            hourly.rename(
                columns={
                    c: GENERATION_KW if c == "solar_mw" else "solar_" + c.replace("_mw", "_kw")
                    for c in hourly.columns
                }
            )
        )
    joined = pd.concat(parts, axis=1)
    grid = pd.date_range(joined.index.min(), joined.index.max(), freq="1h", name=TIMESTAMP)
    joined = joined.reindex(grid)
    for col in (LOAD_KW, GENERATION_KW):
        if col not in joined.columns:
            joined[col] = float("nan")
    joined[GENERATION_KW] = joined[GENERATION_KW].clip(lower=0.0)
    out = joined.reset_index()
    out.insert(0, SITE_ID, site_id)
    out.insert(0, OPERATOR_ID, operator_id)
    return out
