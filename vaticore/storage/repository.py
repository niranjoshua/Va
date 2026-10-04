"""The storage contract, scoped by operator and site from day one.

Retrofitting tenant scoping later is painful, so every read and write takes an
operator_id and site_id here at the interface. Both the DuckDB and the
Postgres/TimescaleDB backends implement this Protocol, so callers depend on the
contract, never on a concrete store.
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd

from vaticore.schemas import GRID_AVAILABLE


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


def with_grid_column(frame: pd.DataFrame) -> pd.DataFrame:
    """Add an empty grid_available column if the feed has none.

    A feed without a grid record leaves the stored record untouched (the upsert
    keeps the existing value), so one CSV without the column cannot erase grid
    hours recorded by another.
    """
    if GRID_AVAILABLE in frame.columns:
        values = pd.to_numeric(frame[GRID_AVAILABLE], errors="coerce")
        bad = values.notna() & ~values.isin([0.0, 1.0])
        if bad.any():
            raise ValueError(f"{GRID_AVAILABLE} must be 1 (on), 0 (off) or missing")
        return frame.assign(**{GRID_AVAILABLE: values.astype(float)})
    return frame.assign(**{GRID_AVAILABLE: float("nan")})
