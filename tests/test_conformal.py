from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vaticore.forecasting import ConformalQuantileForecaster, Forecaster, PersistenceForecaster
from vaticore.forecasting.base import (
    DEFAULT_QUANTILES,
    InsufficientHistoryError,
    NotFittedError,
    quantile_column,
)
from vaticore.forecasting.conformal import (
    InsufficientCalibrationError,
    apply_adjustments,
    cqr_adjustment,
    symmetric_pairs,
)
from vaticore.schemas import LOAD_KW, TIMESTAMP


class _OverconfidentForecaster(Forecaster):
    """Right shape, band far too narrow: the failure conformal exists to fix."""

    def __init__(self, target: str = LOAD_KW) -> None:
        self.target = target
        self._profile: np.ndarray | None = None
        self._last: pd.Timestamp | None = None

    def fit(self, history: pd.DataFrame) -> _OverconfidentForecaster:
        frame = history.dropna(subset=[self.target])
        by_hour = frame.groupby(pd.DatetimeIndex(frame[TIMESTAMP]).hour)[self.target].mean()
        self._profile = by_hour.reindex(range(24)).to_numpy(dtype=float)
        self._last = pd.Timestamp(history[TIMESTAMP].max())
        return self

    def predict_quantiles(
        self, horizon: int, quantiles: tuple[float, ...] = DEFAULT_QUANTILES
    ) -> pd.DataFrame:
        assert self._profile is not None and self._last is not None
        index = pd.date_range(self._last + pd.Timedelta(hours=1), periods=horizon, freq="h")
        centre = self._profile[index.hour]
        # +/- 0.5 kW around the hourly mean, while the noise is +/- 3 kW.
        cols = {quantile_column(q): centre + (q - 0.5) * 1.25 for q in quantiles}
        return pd.DataFrame(cols, index=index)


def _coverage(forecast: pd.DataFrame, actual: np.ndarray) -> float:
    lo = forecast[quantile_column(0.1)].to_numpy()
    hi = forecast[quantile_column(0.9)].to_numpy()
    return float(np.mean((actual >= lo) & (actual <= hi)))


def test_symmetric_pairs_finds_partners() -> None:
    assert symmetric_pairs((0.1, 0.5, 0.9)) == [(0.1, 0.9)]
    assert symmetric_pairs((0.05, 0.1, 0.5, 0.9, 0.95)) == [(0.05, 0.95), (0.1, 0.9)]
    assert symmetric_pairs((0.1, 0.5)) == []


def test_cqr_adjustment_known_answer() -> None:
    # Band [0, 0] around actuals 0..9: scores are the actuals themselves.
    # n = 10, coverage 0.5 -> level ceil(11 * 0.5) / 10 = 0.6 -> the 7th score.
    actual = np.arange(10, dtype=float)
    zeros = np.zeros(10)
    assert cqr_adjustment(zeros, zeros, actual, 0.5) == pytest.approx(6.0)


def test_cqr_adjustment_narrows_a_band_that_is_too_wide() -> None:
    actual = np.zeros(50)
    adj = cqr_adjustment(np.full(50, -10.0), np.full(50, 10.0), actual, 0.8)
    assert adj == pytest.approx(-10.0)


def test_cqr_adjustment_ignores_gaps_and_refuses_too_few_outcomes() -> None:
    actual = np.array([1.0, np.nan, 2.0])
    lo = np.array([0.0, 0.0, 0.0])
    hi = np.array([1.5, 1.5, 1.5])
    assert cqr_adjustment(lo, hi, actual, 0.5) == pytest.approx(0.5)
    with pytest.raises(InsufficientCalibrationError):
        cqr_adjustment(lo, hi, actual, 0.5, min_scores=3)


def test_apply_adjustments_moves_edges_and_keeps_median() -> None:
    frame = pd.DataFrame({"q0.1": [1.0, 2.0], "q0.5": [2.0, 3.0], "q0.9": [3.0, 4.0]})
    wider = apply_adjustments(frame, {(0.1, 0.9): 0.5}, nonnegative=True)
    assert wider["q0.1"].tolist() == [0.5, 1.5]
    assert wider["q0.9"].tolist() == [3.5, 4.5]
    assert wider["q0.5"].tolist() == [2.0, 3.0]
    # A large negative adjustment would cross the edges; rows are re-sorted.
    crossed = apply_adjustments(frame, {(0.1, 0.9): -5.0}, nonnegative=False)
    assert np.all(np.diff(crossed.to_numpy(), axis=1) >= 0)


def test_apply_adjustments_can_keep_negative_values_for_net_load() -> None:
    frame = pd.DataFrame({"q0.1": [-5.0], "q0.5": [0.0], "q0.9": [5.0]})
    signed = apply_adjustments(frame, {(0.1, 0.9): 1.0}, nonnegative=False)
    clipped = apply_adjustments(frame, {(0.1, 0.9): 1.0}, nonnegative=True)
    assert signed["q0.1"].iloc[0] == -6.0
    assert clipped["q0.1"].iloc[0] == 0.0


def test_wrapper_fixes_an_overconfident_model(single_site: pd.DataFrame) -> None:
    train, future = single_site.iloc[: 24 * 22], single_site.iloc[24 * 22 : 24 * 29]
    actual = future[LOAD_KW].to_numpy()

    raw = _OverconfidentForecaster().fit(train).predict_quantiles(horizon=len(future))
    model = ConformalQuantileForecaster(
        _OverconfidentForecaster, target=LOAD_KW, calibration_windows=7
    ).fit(train)
    calibrated = model.predict_quantiles(horizon=len(future))

    assert _coverage(raw, actual) < 0.4
    assert 0.7 <= _coverage(calibrated, actual) <= 0.95
    assert model.adjustments_[(0.1, 0.9)] > 0
    assert model.n_scores_ == 7 * 24


def test_wrapper_satisfies_the_forecaster_interface(single_site: pd.DataFrame) -> None:
    model = ConformalQuantileForecaster(
        lambda: PersistenceForecaster(target=LOAD_KW), target=LOAD_KW
    ).fit(single_site)
    forecast = model.predict_quantiles(horizon=24, quantiles=(0.1, 0.5, 0.9))
    assert isinstance(model, Forecaster)
    assert len(forecast) == 24
    assert list(forecast.columns) == ["q0.1", "q0.5", "q0.9"]
    assert str(forecast.index.dtype) == "datetime64[ns, UTC]"
    assert np.all(np.diff(forecast.to_numpy(), axis=1) >= -1e-9)


def test_wrapper_errors_are_typed(single_site: pd.DataFrame) -> None:
    model = ConformalQuantileForecaster(
        lambda: PersistenceForecaster(target=LOAD_KW), target=LOAD_KW
    )
    with pytest.raises(NotFittedError):
        model.predict_quantiles(horizon=24)
    with pytest.raises(InsufficientHistoryError):
        model.fit(single_site.iloc[: 24 * 7])
    with pytest.raises(ValueError):
        ConformalQuantileForecaster(PersistenceForecaster, target=LOAD_KW, quantiles=(0.5,))


def test_wrapper_refuses_a_feed_with_too_few_outcomes(single_site: pd.DataFrame) -> None:
    site = single_site.copy()
    # A feed that reports only one hour in six over the last week leaves 28
    # outcomes, too few to calibrate on.
    last_week = site.index[-24 * 7 :]
    site.loc[last_week[np.arange(len(last_week)) % 6 != 0], LOAD_KW] = np.nan
    model = ConformalQuantileForecaster(
        lambda: PersistenceForecaster(target=LOAD_KW), target=LOAD_KW, min_scores=72
    )
    with pytest.raises(InsufficientCalibrationError):
        model.fit(site)
