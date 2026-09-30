from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from vaticore.ingestion.elia import (
    EliaFormatError,
    read_elia_load,
    read_elia_solar,
    to_hourly,
    to_site_frame,
)
from vaticore.schemas import GENERATION_KW, LOAD_KW

LOAD_HEADER = (
    "Datetime;Resolution code;Total Load;Most recent forecast;Most recent P10;"
    "Most recent P90;Day-ahead 6PM forecast;Day-ahead 6PM P10;Day-ahead 6PM P90;"
    "Week-ahead forecast"
)
SOLAR_HEADER = (
    "Datetime;Resolution code;Region;Measured & Upscaled;Most recent forecast;"
    "Most recent P10;Most recent P90;Day Ahead 11AM forecast;Day Ahead 11AM P10;"
    "Day Ahead 11AM P90;Day-ahead 6PM forecast;Day-ahead 6PM P10;Day-ahead 6PM P90;"
    "Week-ahead forecast;Week-ahead P10;Week-ahead P90;Monitored capacity;Load factor"
)


def _load_file(tmp_path: Path) -> Path:
    # Elia exports newest first, in local time with an offset. 00:00+02:00 is
    # 22:00 UTC the day before. The 00:30 reading is missing.
    rows = [
        "2025-05-07T00:45:00+02:00;PT15M;4.0;4;3;5;4;3;5;4",
        "2025-05-07T00:15:00+02:00;PT15M;2.0;2;1;3;2;1;3;2",
        "2025-05-07T00:00:00+02:00;PT15M;3.0;3;2;4;3;2;4;3",
        "2025-05-07T01:00:00+02:00;PT15M;8.0;8;7;9;8;7;9;8",
    ]
    path = tmp_path / "load.csv"
    path.write_text(LOAD_HEADER + "\n" + "\n".join(rows) + "\n")
    return path


def _solar_file(tmp_path: Path) -> Path:
    def row(ts: str, region: str, mw: float) -> str:
        return f"{ts};PT15M;{region};{mw};0;0;0;{mw};{mw - 1};{mw + 1};0;0;0;0;0;0;100;0.1"

    rows = [
        row("2025-05-07T00:00:00+02:00", "Belgium", 10.0),
        row("2025-05-07T00:15:00+02:00", "Belgium", 20.0),
        row("2025-05-07T00:30:00+02:00", "Belgium", 30.0),
        row("2025-05-07T00:45:00+02:00", "Belgium", 40.0),
        row("2025-05-07T00:00:00+02:00", "Antwerp", 1.0),
    ]
    path = tmp_path / "solar.csv"
    path.write_text(SOLAR_HEADER + "\n" + "\n".join(rows) + "\n")
    return path


def test_read_elia_load_converts_to_utc_and_sorts(tmp_path: Path) -> None:
    load = read_elia_load(_load_file(tmp_path))
    assert str(load.index.tz) == "UTC"
    assert load.index[0].isoformat() == "2025-05-06T22:00:00+00:00"
    assert load.index.is_monotonic_increasing
    assert {"load_mw", "elia_da_p10_mw", "elia_da_p90_mw"} <= set(load.columns)


def test_hourly_mean_needs_enough_readings(tmp_path: Path) -> None:
    load = read_elia_load(_load_file(tmp_path))
    hourly = to_hourly(load, min_readings=3)
    # 22:00 UTC hour has 3 of 4 readings: 3, 2 and 4 -> mean 3.
    assert hourly.loc["2025-05-06 22:00", "load_mw"].item() == pytest.approx(3.0)
    # 23:00 UTC hour has one reading only -> a visible gap.
    assert np.isnan(hourly.loc["2025-05-06 23:00", "load_mw"].item())


def test_read_elia_solar_picks_one_region(tmp_path: Path) -> None:
    solar = read_elia_solar(_solar_file(tmp_path), region="Belgium")
    assert len(solar) == 4
    with pytest.raises(EliaFormatError):
        read_elia_solar(_solar_file(tmp_path), region="Atlantis")


def test_to_site_frame_builds_the_internal_schema_in_kw(tmp_path: Path) -> None:
    load = read_elia_load(_load_file(tmp_path))
    solar = read_elia_solar(_solar_file(tmp_path))
    site = to_site_frame(load, solar, operator_id="elia", site_id="belgium")
    first = site.iloc[0]
    assert first[LOAD_KW] == pytest.approx(3000.0)
    assert first[GENERATION_KW] == pytest.approx(25000.0)
    assert first["solar_elia_da_p10_kw"] == pytest.approx(24000.0)
    assert "load_elia_da_p90_kw" in site.columns
    assert (site["operator_id"] == "elia").all()


def test_wrong_file_is_a_typed_error(tmp_path: Path) -> None:
    path = tmp_path / "other.csv"
    path.write_text("a;b\n1;2\n")
    with pytest.raises(EliaFormatError):
        read_elia_load(path)
