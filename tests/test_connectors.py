"""Monitoring connectors: vendor exports, Victron VRM, syncing, and the health report."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from vaticore.ingestion.connectors import (
    CSVExportConnector,
    FetchResult,
    SourceConfig,
    VictronVRMConnector,
    load_sources,
    map_export,
    sync_sources,
)
from vaticore.pipeline.health import HealthStatus, site_health_report
from vaticore.schemas import (
    BATTERY_SOC_PCT,
    GENERATION_KW,
    GRID_AVAILABLE,
    LOAD_KW,
    TIMESTAMP,
)
from vaticore.storage import DuckDBRepository

pytestmark = pytest.mark.filterwarnings("ignore")


def _source(**options: object) -> SourceConfig:
    base: dict[str, object] = {
        "operator_id": "op",
        "site_id": "s1",
        "connector": "csv",
        "timezone": "Africa/Lagos",
        "columns": {
            "timestamp": "Time",
            "load_kw": "Load Power(W)",
            "battery_soc_pct": "SOC(%)",
            "grid_available": "Grid",
        },
        "scale": {"load_kw": 0.001},
        "grid": {"on": ["On"], "off": ["Off"]},
    }
    base.update(options)
    return SourceConfig(**base)  # type: ignore[arg-type]


def _export() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Time": [
                "2026-03-10 06:00",
                "2026-03-10 07:00",
                "2026-03-10 07:00",  # duplicate from overlapping exports
                "2026-03-10 08:00",
                "not a time",
            ],
            "Load Power(W)": ["3200", "3400", "3450", "n/a", "1"],
            "SOC(%)": [64, 101, 101, 70, 1],
            "Grid": ["On", "Off", "Off", "??", "On"],
        }
    )


def test_a_vendor_export_is_mapped_and_every_fix_reported() -> None:
    result = map_export(_export(), _source())
    frame = result.frame
    assert len(frame) == 3
    assert str(frame[TIMESTAMP].iloc[0]) == "2026-03-10 05:00:00+00:00"  # local to UTC
    assert frame[LOAD_KW].tolist()[:2] == [3.2, 3.45]  # watts to kW, last duplicate kept
    assert np.isnan(frame[LOAD_KW].iloc[2])
    assert frame[BATTERY_SOC_PCT].tolist() == [64.0, 100.0, 70.0]
    assert frame[GRID_AVAILABLE].tolist()[:2] == [1.0, 0.0] and np.isnan(frame[GRID_AVAILABLE][2])
    notes = " ".join(result.notes)
    for expected in ("unreadable timestamps", "duplicate", "above 100%", "unreadable value"):
        assert expected in notes


def test_exports_without_a_timezone_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        map_export(_export(), _source(timezone=None))
    with pytest.raises(ValueError, match="no column"):
        map_export(_export(), _source(columns={"timestamp": "When"}))


def test_grid_state_from_a_voltage() -> None:
    raw = pd.DataFrame({"Time": ["2026-03-10T06:00:00Z", "2026-03-10T07:00:00Z"], "V": [228, 12]})
    source = _source(
        columns={"timestamp": "Time", "grid_available": "V"}, grid={"min_voltage": 150}
    )
    assert map_export(raw, source).frame[GRID_AVAILABLE].tolist() == [1.0, 0.0]


def test_csv_files_are_read_by_glob_and_windowed(tmp_path: Path) -> None:
    _export().iloc[:2].to_csv(tmp_path / "a.csv", index=False)
    _export().iloc[3:4].to_csv(tmp_path / "b.csv", index=False)
    source = _source(path=str(tmp_path / "*.csv"))
    start, end = pd.Timestamp("2026-03-10 05:30", tz="UTC"), pd.Timestamp("2026-03-11", tz="UTC")
    result = CSVExportConnector().fetch(source, start, end)
    assert len(result.frame) == 2 and "read 2 file(s)" in result.notes[0]
    assert "1 row(s) outside" in " ".join(result.notes)  # 05:00 UTC is before the window
    missing = CSVExportConnector().fetch(_source(path=str(tmp_path / "none*.csv")), start, end)
    assert missing.frame.empty and "no files" in missing.notes[0]


# -- Victron VRM -----------------------------------------------------------------


def _vrm(handler: object, token: str | None = "abc") -> VictronVRMConnector:
    client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    env = {"VATICORE_VRM_TOKEN": token} if token else {}
    return VictronVRMConnector(client=client, sleep=lambda _: None, env=env)


def test_victron_battery_charge_is_fetched_in_weekly_chunks() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        start = int(request.url.params["start"]) * 1000
        rows = [[start + h * 3_600_000, 50.0 + h, 49.0, 51.0] for h in range(3)]
        return httpx.Response(200, json={"success": True, "records": {"bs": rows}})

    source = SourceConfig(operator_id="op", site_id="s1", connector="victron_vrm",
                          installation_id="123456")  # fmt: skip
    start = pd.Timestamp("2026-03-01", tz="UTC")
    result = _vrm(handler).fetch(source, start, start + pd.Timedelta(days=10))
    assert len(calls) == 2  # ten days in two requests
    request = calls[0]
    assert request.url.path == "/v2/installations/123456/stats"
    assert request.headers["x-authorization"] == "Token abc"
    assert request.url.params["type"] == "custom"
    assert request.url.params.get_list("attributeCodes[]") == ["bs"]
    frame = result.frame
    assert len(frame) == 6 and frame[BATTERY_SOC_PCT].iloc[1] == 51.0
    assert str(frame[TIMESTAMP].iloc[0]) == "2026-03-01 00:00:00+00:00"


def test_victron_needs_a_token_and_reports_rejection() -> None:
    source = SourceConfig(operator_id="op", site_id="s1", connector="victron_vrm",
                          installation_id="1")  # fmt: skip
    start = pd.Timestamp("2026-03-01", tz="UTC")
    with pytest.raises(ValueError, match="VATICORE_VRM_TOKEN"):
        _vrm(lambda r: httpx.Response(200), token=None).fetch(
            source, start, start + pd.Timedelta(hours=5)
        )

    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errors": "bad token"})

    with pytest.raises(RuntimeError, match="rejected the token"):
        _vrm(denied).fetch(source, start, start + pd.Timedelta(hours=5))


# -- syncing ------------------------------------------------------------------------


class _Fixed:
    name = "fixed"

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []

    def fetch(self, source: SourceConfig, start: pd.Timestamp, end: pd.Timestamp) -> FetchResult:
        self.windows.append((start, end))
        return FetchResult(self.frame, ["ok"])


class _Broken:
    name = "broken"

    def fetch(self, source: SourceConfig, start: pd.Timestamp, end: pd.Timestamp) -> FetchResult:
        raise RuntimeError("platform down")


def test_sync_stores_readings_and_isolates_failures() -> None:
    repo = DuckDBRepository(":memory:")
    frame = map_export(_export(), _source()).frame
    fixed = _Fixed(frame)
    sources = [
        SourceConfig(operator_id="op", site_id="s1", connector="fixed"),
        SourceConfig(operator_id="op", site_id="s2", connector="broken"),
        SourceConfig(operator_id="op", site_id="s3", connector="nope"),
    ]
    now = datetime(2026, 3, 11, tzinfo=UTC)
    results = sync_sources(sources, repo, now=now, connectors={"fixed": fixed, "broken": _Broken()})
    assert results[0].rows == 3 and results[0].error is None
    assert "platform down" in (results[1].error or "")
    assert "unknown connector" in (results[2].error or "")
    assert len(repo.read_history("op", "s1")) == 3
    # The first sync backfills; the next starts just before the last stored reading.
    sync_sources(sources[:1], repo, now=now, connectors={"fixed": fixed})
    assert fixed.windows[1][0] == pd.Timestamp("2026-03-10 07:00", tz="UTC") - pd.Timedelta(hours=6)
    assert fixed.windows[0][0] == pd.Timestamp(now) - pd.Timedelta(days=60)
    longer = SourceConfig(operator_id="op", site_id="s9", connector="fixed", backfill_days=365)
    sync_sources([longer], repo, now=now, connectors={"fixed": fixed})
    assert fixed.windows[2][0] == pd.Timestamp(now) - pd.Timedelta(days=365)


def test_sources_file(tmp_path: Path) -> None:
    path = tmp_path / "sources.toml"
    path.write_text(
        '[[source]]\noperator_id = "op"\nsite_id = "s1"\nconnector = "victron_vrm"\n'
        'installation_id = "1"\n'
    )
    (source,) = load_sources(path)
    assert source.option("installation_id") == "1"
    path.write_text(path.read_text() * 2)
    with pytest.raises(ValueError, match="more than one source"):
        load_sources(path)


# -- the site health report ------------------------------------------------------------


def _site_data(days: int, shift_hours: int = 0, longitude: float = 3.4) -> pd.DataFrame:
    end = pd.Timestamp("2026-03-10", tz="UTC")
    index = pd.date_range(end - pd.Timedelta(days=days), end, freq="h", inclusive="left")
    rng = np.random.default_rng(0)
    solar_noon = 12 - longitude / 15
    hours = (index.hour + 0.5).to_numpy()
    solar = np.clip(np.cos((hours - solar_noon) / 12 * np.pi) * 10, 0, None)
    frame = pd.DataFrame(
        {
            TIMESTAMP: index + pd.Timedelta(hours=shift_hours),
            LOAD_KW: 4 + rng.normal(0, 0.3, len(index)),
            GENERATION_KW: solar,
            BATTERY_SOC_PCT: 60.0,
            GRID_AVAILABLE: 1.0,
        }
    )
    return frame


def _report(frame: pd.DataFrame, **kw: object):  # type: ignore[no-untyped-def]
    args = {"operator_id": "op", "site_id": "s1", "end": pd.Timestamp("2026-03-10", tz="UTC"),
            "days": 14, "timezone": "Africa/Lagos", "longitude": 3.4, "has_grid": True}  # fmt: skip
    args.update(kw)
    return site_health_report(frame, **args)  # type: ignore[arg-type]


def test_a_clean_site_reports_ok() -> None:
    report = _report(_site_data(20))
    assert report.status is HealthStatus.OK, report.to_text()
    assert report.coverage[LOAD_KW] == 1.0 and report.resolution == pd.Timedelta(hours=1)
    assert "No problems found" in report.to_text()


def test_local_time_labelled_as_utc_is_caught() -> None:
    report = _report(_site_data(20, shift_hours=1))
    codes = {f.code for f in report.findings}
    assert "timezone" in codes
    message = next(f.message for f in report.findings if f.code == "timezone")
    assert "1 h late" in message


def test_gaps_stuck_meters_and_missing_channels_are_reported() -> None:
    frame = _site_data(20)
    frame.loc[frame.index[-60:-20], LOAD_KW] = np.nan  # a 40 hour gap
    frame.loc[frame.index[-90:-70], LOAD_KW] = 5.0  # stuck for 20 hours
    frame[BATTERY_SOC_PCT] = np.nan
    frame[GRID_AVAILABLE] = np.nan
    report = _report(frame)
    codes = {f.code for f in report.findings}
    assert {"missing", "stuck_meter", "no_battery_charge", "no_grid_record"} <= codes
    assert report.longest_gap_hours == 40 and report.worst_days
    assert report.status is HealthStatus.WARN
    assert _report(frame.iloc[0:0]).status is HealthStatus.FAIL
