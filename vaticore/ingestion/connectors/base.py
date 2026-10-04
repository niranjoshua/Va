"""Connectors: how readings get from a site's monitoring system into Vaticore.

Every connector turns one outside source (a vendor API, an export file) into
the internal schema: operator_id, site_id, UTC timestamps, kW, and the
optional monitoring columns (grid on/off, battery charge, generator output,
fuel level). What each site uses is declared in a sources file:

    [[source]]
    operator_id = "example-towerco"
    site_id = "lag-ikd-0142"
    connector = "victron_vrm"          # or "csv"
    installation_id = "123456"
    token_env = "VATICORE_VRM_TOKEN"   # the secret stays in the environment

Credentials are never written in the sources file: it names the environment
variable that holds them.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field


class SourceConfig(BaseModel):
    """One site's data source, from the sources file."""

    model_config = ConfigDict(extra="allow", frozen=True)

    operator_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    connector: str = Field(min_length=1)

    @property
    def key(self) -> tuple[str, str]:
        return self.operator_id, self.site_id

    def option(self, name: str, default: Any = None) -> Any:
        extra = self.model_extra or {}
        return extra.get(name, default)


@dataclass
class FetchResult:
    """Readings from one source, in the internal schema, and what was fixed."""

    frame: pd.DataFrame
    notes: list[str] = field(default_factory=list)


class Connector(Protocol):
    name: str

    def fetch(self, source: SourceConfig, start: pd.Timestamp, end: pd.Timestamp) -> FetchResult:
        """Readings from start (inclusive) to end (exclusive), UTC."""
        ...


def load_sources(path: str | Path) -> list[SourceConfig]:
    data: dict[str, Any] = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    sources = [SourceConfig(**item) for item in data.get("source", [])]
    keys = [s.key for s in sources]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ValueError(f"more than one source for {dupes}")
    return sources
