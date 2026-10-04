"""Exports from any monitoring platform, read through a column mapping.

Almost every inverter, battery and tower monitoring platform can export CSV
(Victron VRM, Huawei FusionSolar, Deye/Solarman, SMA, tower RMS vendors). The
columns differ, so each source declares how its file maps to Vaticore:

    [[source]]
    operator_id = "example-towerco"
    site_id = "lag-ikd-0142"
    connector = "csv"
    path = "/data/exports/lag-ikd-0142/*.csv"   # a file or a glob
    timezone = "Africa/Lagos"   # the clock the export's timestamps are in
    backfill_days = 365         # first sync: how much history to load (default 60)

    [source.columns]            # internal name = column in the file
    timestamp = "Time"
    load_kw = "Load Power(W)"
    generation_kw = "PV Power(W)"
    battery_soc_pct = "SOC(%)"
    grid_available = "Grid Status"
    genset_kw = "Generator Power(W)"

    [source.scale]              # multiply after reading: W to kW
    load_kw = 0.001
    generation_kw = 0.001
    genset_kw = 0.001

    [source.grid]               # how the grid column says on or off
    on = ["On", "Connected", "1"]
    # or, for a voltage column: min_voltage = 150

The messy parts real exports have are handled explicitly and reported:
timestamps in local time (most exports), duplicate rows (overlapping
exports), units in watts, percentages above 100, text in number columns.
"""

from __future__ import annotations

import glob
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from vaticore.ingestion.connectors.base import FetchResult, SourceConfig
from vaticore.schemas import (
    BATTERY_SOC_PCT,
    GENERATION_KW,
    GRID_AVAILABLE,
    LOAD_KW,
    OPERATOR_ID,
    OPTIONAL_COLUMNS,
    SITE_ID,
    TIMESTAMP,
)

_NUMERIC = (LOAD_KW, GENERATION_KW, *OPTIONAL_COLUMNS)


class CSVExportConnector:
    name = "csv"

    def fetch(self, source: SourceConfig, start: pd.Timestamp, end: pd.Timestamp) -> FetchResult:
        pattern = source.option("path")
        if not pattern:
            raise ValueError(f"source {source.key} needs a path")
        files = sorted(glob.glob(str(pattern)))
        if not files:
            return FetchResult(_empty(source), [f"no files match {Path(str(pattern)).name}"])
        raw = pd.concat(
            [pd.read_csv(f, **(source.option("read_csv") or {})) for f in files],
            ignore_index=True,
        )
        result = map_export(raw, source)
        frame = result.frame
        keep = (frame[TIMESTAMP] >= start) & (frame[TIMESTAMP] < end)
        result.frame = frame[keep].reset_index(drop=True)
        outside = int((~keep).sum())
        if outside:
            result.notes.append(
                f"{outside} row(s) outside {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC "
                "skipped (set backfill_days to load older history)"
            )
        result.notes.insert(0, f"read {len(files)} file(s), {len(raw)} rows")
        return result


def map_export(raw: pd.DataFrame, source: SourceConfig) -> FetchResult:
    """Map a vendor export onto the internal schema, reporting every fix."""
    columns: dict[str, str] = source.option("columns") or {}
    scale: dict[str, float] = source.option("scale") or {}
    grid_rule: dict[str, Any] = source.option("grid") or {}
    timezone = source.option("timezone")
    notes: list[str] = []

    if "timestamp" not in columns:
        raise ValueError(f"source {source.key}: columns.timestamp is required")
    missing = [c for c in columns.values() if c not in raw.columns]
    if missing:
        raise ValueError(f"source {source.key}: export has no column(s) {missing}")

    stamps = pd.to_datetime(raw[columns["timestamp"]], errors="coerce", format="mixed")
    bad_times = int(stamps.isna().sum())
    if bad_times:
        notes.append(f"dropped {bad_times} row(s) with unreadable timestamps")
    if stamps.dt.tz is None:
        if not timezone:
            raise ValueError(
                f"source {source.key}: timestamps have no timezone; set timezone "
                "(exports are usually in the site's local time)"
            )
        stamps = stamps.dt.tz_localize(timezone, ambiguous="NaT", nonexistent="NaT")
    out = pd.DataFrame({TIMESTAMP: stamps.dt.tz_convert("UTC")})

    for internal in _NUMERIC:
        if internal not in columns:
            continue
        column = raw[columns[internal]]
        if internal == GRID_AVAILABLE:
            out[internal] = _grid_state(column, grid_rule)
            continue
        values = pd.to_numeric(column, errors="coerce")
        unreadable = int(values.isna().sum() - column.isna().sum())
        if unreadable:
            notes.append(f"{internal}: {unreadable} unreadable value(s) left missing")
        values = values * float(scale.get(internal, 1.0))
        if internal == BATTERY_SOC_PCT:
            over = int((values > 100.0).sum())
            if over:
                notes.append(f"{internal}: {over} reading(s) above 100% capped")
            values = values.clip(upper=100.0)
        negative = int((values < 0).sum())
        if negative:
            notes.append(f"{internal}: {negative} negative reading(s) left missing")
            values = values.where(values >= 0)
        out[internal] = values

    out = out.dropna(subset=[TIMESTAMP])
    duplicates = int(out.duplicated(subset=[TIMESTAMP]).sum())
    if duplicates:
        notes.append(f"{duplicates} duplicate timestamp(s): kept the last reading")
        out = out.drop_duplicates(subset=[TIMESTAMP], keep="last")
    out.insert(0, SITE_ID, source.site_id)
    out.insert(0, OPERATOR_ID, source.operator_id)
    for internal in (LOAD_KW, GENERATION_KW):
        if internal not in out:
            out[internal] = np.nan
    return FetchResult(out.sort_values(TIMESTAMP).reset_index(drop=True), notes)


def _grid_state(column: pd.Series, rule: dict[str, Any]) -> pd.Series:
    """1 on, 0 off, missing if unknown, from text states or a voltage."""
    if "min_voltage" in rule:
        volts = pd.to_numeric(column, errors="coerce")
        return (volts >= float(rule["min_voltage"])).astype(float).where(volts.notna())
    on = {str(v).strip().lower() for v in rule.get("on", ["1", "on", "true", "connected"])}
    off = {str(v).strip().lower() for v in rule.get("off", ["0", "off", "false", "disconnected"])}
    text = column.astype("string").str.strip().str.lower()
    state = pd.Series(np.nan, index=column.index, dtype=float)
    state[text.isin(on)] = 1.0
    state[text.isin(off)] = 0.0
    return state


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
