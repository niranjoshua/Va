"""The storage contract, scoped by operator and site from day one.

Retrofitting tenant scoping later is painful, so every read and write takes an
operator_id and site_id here at the interface. Both the DuckDB and the
Postgres/TimescaleDB backends implement this Protocol, so callers depend on the
contract, never on a concrete store.
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd

from vaticore.schemas import GRID_AVAILABLE, OPTIONAL_COLUMNS


class TimeSeriesRepository(Protocol):
    """Storage contract for validated site time series."""

    def upsert(self, frame: pd.DataFrame) -> int:
        """Insert or update rows for one or more sites. Returns rows written."""
        ...

    def read_history(
        self,
        operator_id: str,
        site_id: str,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """Return one site's validated history within an optional time range."""
        ...

    def read_fleet(self) -> pd.DataFrame:
        """Return all stored observations across every operator and site."""
        ...

    def list_sites(self) -> pd.DataFrame:
        """Summarise stored sites: row count and time span per operator/site."""
        ...

    def count(self) -> int:
        """Total number of stored observations."""
        ...

    def close(self) -> None:
        """Release the underlying connection."""
        ...


def with_optional_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Add the optional monitoring columns a feed does not carry, as missing.

    The stores keep an existing value when an incoming one is missing, so a
    feed without, say, a grid record cannot erase grid hours recorded by
    another feed.
    """
    out = frame.copy()
    for column in OPTIONAL_COLUMNS:
        if column not in out.columns:
            out[column] = float("nan")
        else:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype(float)
    bad = out[GRID_AVAILABLE].notna() & ~out[GRID_AVAILABLE].isin([0.0, 1.0])
    if bad.any():
        raise ValueError(f"{GRID_AVAILABLE} must be 1 (on), 0 (off) or missing")
    return out
