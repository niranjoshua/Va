from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vaticore.evaluation.backtest import FoldForecast
from vaticore.evaluation.calibration import (
    calibration_report,
    conformalize_forecasts,
    recent_coverage,
)


def _fold(fold: int, actual: np.ndarray, lo: float, mid: float, hi: float) -> FoldForecast:
    n = len(actual)
    stamps = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC") + pd.Timedelta(
        hours=24 * fold
    )
    return FoldForecast(
        model="m",
        fold=fold,
        timestamps=stamps,
        actual=np.asarray(actual, dtype=float),
        quantiles={0.1: np.full(n, lo), 0.5: np.full(n, mid), 0.9: np.full(n, hi)},
    )


def test_calibration_report_known_answer() -> None:
    # Actuals 0..9 against a fixed band [2, 7] with median 4.5.
    fold = _fold(0, np.arange(10), lo=2.0, mid=4.5, hi=7.0)
    report = calibration_report([fold])
    by_q = {q.quantile: q.observed for q in report.quantiles}
    assert by_q[0.1] == pytest.approx(0.3)  # 0, 1, 2 are at or below 2
    assert by_q[0.5] == pytest.approx(0.5)
    assert by_q[0.9] == pytest.approx(0.8)
    (band,) = report.intervals
    assert band.observed == pytest.approx(0.6)  # 2..7 inclusive
    assert band.nominal == pytest.approx(0.8)
    assert band.gap == pytest.approx(-0.2)
    assert band.mean_width == pytest.approx(5.0)


def test_calibration_report_ignores_gaps() -> None:
    actual = np.array([0.0, np.nan, 10.0, 5.0])
    report = calibration_report([_fold(0, actual, lo=1.0, mid=5.0, hi=9.0)])
    assert report.intervals[0].n == 3
    assert report.intervals[0].observed == pytest.approx(1 / 3)


def test_recent_coverage_uses_only_the_latest_observed_hours() -> None:
    old = _fold(0, np.full(24, 100.0), lo=0.0, mid=5.0, hi=10.0)  # all outside
    new = _fold(1, np.full(24, 5.0), lo=0.0, mid=5.0, hi=10.0)  # all inside
    assert recent_coverage([new, old], hours=24).observed == pytest.approx(1.0)
    assert recent_coverage([old, new], hours=48).observed == pytest.approx(0.5)


def test_conformalize_widens_a_band_that_was_too_narrow() -> None:
    rng = np.random.default_rng(0)
    folds = [_fold(i, rng.normal(0.0, 3.0, 24), lo=-0.5, mid=0.0, hi=0.5) for i in range(12)]
    result = conformalize_forecasts(folds, window_folds=6, min_scores=48, nonnegative=False)

    # The first two folds have fewer than 48 outcomes behind them.
    assert result.n_passed_through == 2
    assert result.n_calibrated == 10
    assert result.forecasts[0].model == "m+conformal"

    before = calibration_report(folds[2:]).intervals[0].observed
    after = calibration_report(result.forecasts[2:]).intervals[0].observed
    assert before < 0.3
    assert 0.65 <= after <= 0.95


def test_conformalize_never_uses_outcomes_from_the_future() -> None:
    # Fold 1's band can only be set from fold 0. Make fold 0 perfectly inside
    # its band and fold 1 wildly outside: fold 1 must not be widened.
    calm = _fold(0, np.zeros(24), lo=-1.0, mid=0.0, hi=1.0)
    wild = _fold(1, np.full(24, 50.0), lo=-1.0, mid=0.0, hi=1.0)
    result = conformalize_forecasts([calm, wild], window_folds=5, min_scores=24, nonnegative=False)
    adjusted_hi = result.forecasts[1].quantiles[0.9]
    assert np.all(adjusted_hi < 50.0)
