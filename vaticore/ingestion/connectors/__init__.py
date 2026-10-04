"""Connectors from monitoring platforms into the observation store.

    sync_sources(load_sources("sources.toml"), repo)

pulls each site's new readings (from just before the last stored one, so late
corrections are picked up) and upserts them. One source failing never stops
the others; each result says what was read and what was fixed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd

from vaticore.ingestion.connectors.base import (
    Connector,
    FetchResult,
    SourceConfig,
    load_sources,
)
from vaticore.ingestion.connectors.csv_export import CSVExportConnector, map_export
from vaticore.ingestion.connectors.victron import VictronVRMConnector
from vaticore.storage import TimeSeriesRepository

__all__ = [
    "CSVExportConnector",
    "Connector",
    "FetchResult",
    "SourceConfig",
    "SyncResult",
    "VictronVRMConnector",
    "load_sources",
    "map_export",
    "sync_sources",
]

log = logging.getLogger("vaticore.ingestion")

# Re-read this much before the last stored reading: platforms correct late data.
OVERLAP = pd.Timedelta(hours=6)
# How far back to fetch for a site with nothing stored yet. A source can set
# backfill_days to load a longer history (an operator's past year of exports).
BACKFILL = pd.Timedelta(days=60)


@dataclass
class SyncResult:
    operator_id: str
    site_id: str
    connector: str
    rows: int = 0
    notes: list[str] = field(default_factory=list)
    error: str | None = None


def default_connectors() -> dict[str, Connector]:
    return {"csv": CSVExportConnector(), "victron_vrm": VictronVRMConnector()}


def sync_sources(
    sources: list[SourceConfig],
    repo: TimeSeriesRepository,
    *,
    now: datetime | None = None,
    connectors: dict[str, Connector] | None = None,
) -> list[SyncResult]:
    """Fetch and store new readings for every source."""
    end = pd.Timestamp(now or datetime.now(tz=UTC))
    registry = connectors or default_connectors()
    stored = repo.list_sites()
    last_seen = {
        (str(r["operator_id"]), str(r["site_id"])): pd.Timestamp(r["last_timestamp"])
        for _, r in stored.iterrows()
    }
    results = []
    for source in sources:
        result = SyncResult(source.operator_id, source.site_id, source.connector)
        try:
            connector = registry.get(source.connector)
            if connector is None:
                raise ValueError(f"unknown connector {source.connector!r}; have {sorted(registry)}")
            last = last_seen.get(source.key)
            backfill = source.option("backfill_days")
            first = end - (pd.Timedelta(days=float(backfill)) if backfill else BACKFILL)
            start = (last - OVERLAP) if last is not None else first
            fetched = connector.fetch(source, _utc(start), _utc(end))
            result.notes = fetched.notes
            if not fetched.frame.empty:
                result.rows = repo.upsert(fetched.frame)
        except Exception as exc:  # reported per source; the others still sync
            log.exception("sync %s/%s failed", source.operator_id, source.site_id)
            result.error = f"{type(exc).__name__}: {exc}"
        results.append(result)
    return results


def _utc(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
