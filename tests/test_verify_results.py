"""The study verifier reruns a study from its recorded settings and spots drift.

Uses a small synthetic stand-in for the ENTSO-E file, so the full round trip
(publish, rerun, compare) is tested without the real data.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))

import verify_results  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore")


def _spain_like(path: Path, days: int = 60) -> None:
    hours = pd.date_range("2017-01-01", periods=24 * days, freq="h", tz="UTC")
    rng = np.random.default_rng(0)
    h = hours.hour.to_numpy()
    load = 28_000 + 6_000 * np.sin((h - 6) / 24 * 2 * np.pi) + rng.normal(0, 600, len(h))
    solar = np.clip(np.sin((h - 6) / 12 * np.pi), 0, None) * 4_000 * rng.uniform(0.5, 1, len(h))
    pd.DataFrame(
        {"time": hours.strftime("%Y-%m-%d %H:%M:%S+00:00"), "total load actual": load,
         "generation solar": solar}
    ).to_csv(path, index=False)  # fmt: skip


def test_a_published_study_reproduces_and_drift_is_reported(tmp_path: Path) -> None:
    csv = tmp_path / "energy_dataset.csv"
    _spain_like(csv)
    published = tmp_path / "published"
    run = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "value_study.py"), str(csv),
         "--site-id", "site-T-300kWh", "--initial-days", "40",
         "--battery-kwh", "300", "--battery-power-kw", "100", "--min-soc-kwh", "30",
         "--out", str(published)],
        cwd=ROOT, capture_output=True, text=True,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr[-500:]
    name = "value_study_site-T-300kWh"
    assert (published / f"{name}.json").exists()

    args = ["--results", str(published), "--spain-csv", str(csv), name]
    assert verify_results.main(args) == 0

    data = json.loads((published / f"{name}.json").read_text())
    data["value"]["persistence P90"]["fuel_l"] *= 1.01
    (published / f"{name}.json").write_text(json.dumps(data))
    assert verify_results.main(args) == 1
