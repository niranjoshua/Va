"""Conformal calibration: make the forecast range mean what it says.

A P10 to P90 band is a promise that about 80% of outcomes land inside it. Raw
quantile models often break that promise (usually too narrow), and an operator
who sizes a battery on a band that is secretly too narrow gets caught out.

This module applies conformalized quantile regression (CQR; Romano, Patterson
and Candes, 2019). For each symmetric pair of quantiles (for example 0.1 and
0.9, nominal coverage 0.8) it measures how far past actuals fell outside the
band on held out data,

    score_i = max(lower_i - y_i, y_i - upper_i),

and widens (or, if the band was too wide, narrows) the band by the finite
sample quantile of those scores. Under exchangeability the adjusted band covers
at the nominal rate. Time series are not exchangeable, so we calibrate on the
most recent windows only and always report observed coverage alongside.

Two ways to use it:
  - ConformalQuantileForecaster wraps any Forecaster and calibrates itself on
    its own recent history during fit (production, API, dashboard).
  - The pure functions (cqr_adjustment, symmetric_pairs, apply_adjustments)
    let the backtest harness calibrate each fold on the folds before it,
    without refitting (see vaticore.evaluation.calibration).

Leakage: calibration only ever uses forecasts whose outcomes were known before
the forecast being adjusted was issued.
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Mapping

import numpy as np
import numpy.typing as npt
import pandas as pd

from vaticore.forecasting.base import (
    DEFAULT_QUANTILES,
    Forecaster,
    ForecasterError,
    InsufficientHistoryError,
    NotFittedError,
    quantile_column,
)
from vaticore.schemas import TIMESTAMP

QuantilePair = tuple[float, float]


class InsufficientCalibrationError(ForecasterError):
    """Raised when there are too few scored outcomes to calibrate a band."""


def symmetric_pairs(quantiles: tuple[float, ...]) -> list[QuantilePair]:
    """Return (lower, upper) pairs such as (0.1, 0.9) found in quantiles."""
    qs = sorted(quantiles)
    pairs: list[QuantilePair] = []
    for q in qs:
        if q >= 0.5:
            continue
        partner = next((p for p in qs if math.isclose(p, 1.0 - q, abs_tol=1e-9)), None)
        if partner is not None:
            pairs.append((q, partner))
    return pairs


def cqr_adjustment(
    lower: npt.ArrayLike,
    upper: npt.ArrayLike,
    actual: npt.ArrayLike,
    coverage: float,
    *,
    min_scores: int = 1,
) -> float:
    """How far to move each edge of a band so it covers at the nominal rate.

    Returns the ceil((n + 1) * coverage) / n empirical quantile of the CQR
    scores. Positive widens the band, negative narrows it. Positions where the
    actual or either edge is missing are ignored (a gap is not an outcome).
    When n is so small that the level exceeds 1 the largest score is used,
    which is the conservative choice.
    """
    if not 0.0 < coverage < 1.0:
        raise ValueError("coverage must be strictly between 0 and 1")
    lo = np.asarray(lower, dtype=float)
    hi = np.asarray(upper, dtype=float)
    y = np.asarray(actual, dtype=float)
    if not lo.shape == hi.shape == y.shape:
        raise ValueError("lower, upper and actual must have the same shape")
    mask = ~(np.isnan(lo) | np.isnan(hi) | np.isnan(y))
    n = int(mask.sum())
    if n < max(1, min_scores):
        raise InsufficientCalibrationError(
            f"need at least {max(1, min_scores)} scored outcomes to calibrate, got {n}"
        )
    scores = np.maximum(lo[mask] - y[mask], y[mask] - hi[mask])
    level = min(1.0, math.ceil((n + 1) * coverage) / n)
    return float(np.quantile(scores, level, method="higher"))


def apply_adjustments(
    forecast: pd.DataFrame,
    adjustments: Mapping[QuantilePair, float],
    *,
    nonnegative: bool,
) -> pd.DataFrame:
    """Move each calibrated band's edges outward (or inward) by its adjustment.

    Quantiles without a calibrated partner (for example the median) are left
    as they are. Rows are re-sorted afterwards so quantiles never cross.
    """
    out = forecast.copy()
    for (q_lo, q_hi), adj in adjustments.items():
        lo_col, hi_col = quantile_column(q_lo), quantile_column(q_hi)
        if lo_col not in out.columns or hi_col not in out.columns:
            raise ValueError(f"forecast has no columns for the calibrated band {q_lo}-{q_hi}")
        out[lo_col] = out[lo_col] - adj
        out[hi_col] = out[hi_col] + adj
    values = np.sort(out.to_numpy(dtype=float), axis=1)
    if nonnegative:
        values = np.clip(values, 0.0, None)
    out.loc[:, :] = values
    return out


def _predict(
    model: Forecaster,
    horizon: int,
    quantiles: tuple[float, ...],
    future_exog: pd.DataFrame | None,
) -> pd.DataFrame:
    """Call predict_quantiles, passing future_exog only to models that take it."""
    takes_exog = "future_exog" in inspect.signature(model.predict_quantiles).parameters
    if future_exog is not None and takes_exog:
        return model.predict_quantiles(  # type: ignore[call-arg]
            horizon=horizon, quantiles=quantiles, future_exog=future_exog
        )
    return model.predict_quantiles(horizon=horizon, quantiles=quantiles)


class ConformalQuantileForecaster(Forecaster):
    """Wrap any Forecaster so its bands cover at their nominal rate.

    During fit the base model is refitted at `calibration_windows` recent
    origins, each forecasting the following `window_horizon` steps from data
    strictly before that origin. The misses on those windows set each band's
    adjustment. The base model is then fitted once more on the full history
    for the live forecast.

    Parameters
    ----------
    make_base:
        Zero argument factory returning a fresh, unfitted base model.
    target:
        The column the base model forecasts, used to read calibration outcomes.
    quantiles:
        Quantiles to forecast. Symmetric pairs (0.1 and 0.9) are calibrated;
        unpaired ones (0.5) pass through.
    calibration_windows, window_horizon:
        How many recent windows to calibrate on, and their length. The default
        is the last seven days at hourly resolution.
    min_scores:
        Minimum number of observed (non missing) outcomes required. Fewer
        raises InsufficientCalibrationError rather than trusting a noisy band.
    nonnegative:
        Clip at zero. Set False for signed targets such as net load.
    """

    def __init__(
        self,
        make_base: Callable[[], Forecaster],
        *,
        target: str,
        quantiles: tuple[float, ...] = DEFAULT_QUANTILES,
        calibration_windows: int = 7,
        window_horizon: int = 24,
        min_scores: int = 72,
        nonnegative: bool = True,
    ) -> None:
        if calibration_windows < 1 or window_horizon < 1:
            raise ValueError("calibration_windows and window_horizon must be positive")
        if not symmetric_pairs(quantiles):
            raise ValueError("quantiles contain no symmetric pair (such as 0.1 and 0.9)")
        self.make_base = make_base
        self.target = target
        self.quantiles = tuple(sorted(quantiles))
        self.calibration_windows = calibration_windows
        self.window_horizon = window_horizon
        self.min_scores = min_scores
        self.nonnegative = nonnegative

        self.adjustments_: dict[QuantilePair, float] = {}
        self.n_scores_: int = 0
        self._base: Forecaster | None = None

    def fit(self, history: pd.DataFrame) -> ConformalQuantileForecaster:
        if self.target not in history.columns:
            raise ValueError(f"target column {self.target!r} not present in history")
        data = history.dropna(subset=[TIMESTAMP]).sort_values(TIMESTAMP).reset_index(drop=True)
        n = len(data)
        span = self.calibration_windows * self.window_horizon
        if n <= span:
            raise InsufficientHistoryError(
                f"need more than {span} rows to calibrate on {self.calibration_windows} "
                f"windows of {self.window_horizon}, got {n}"
            )

        pairs = symmetric_pairs(self.quantiles)
        lows: dict[QuantilePair, list[np.ndarray]] = {p: [] for p in pairs}
        highs: dict[QuantilePair, list[np.ndarray]] = {p: [] for p in pairs}
        actuals: list[np.ndarray] = []

        for k in range(self.calibration_windows):
            origin = n - span + k * self.window_horizon
            train = data.iloc[:origin]
            window = data.iloc[origin : origin + self.window_horizon]
            # The window's own target is never shown to the model; only its
            # exogenous columns (weather forecasts) are, as in production.
            future_exog = window.drop(columns=[self.target])
            model = self.make_base().fit(train)
            fc = _predict(model, self.window_horizon, self.quantiles, future_exog)
            observed = (
                window.set_index(TIMESTAMP)[self.target]
                .reindex(pd.DatetimeIndex(fc.index))
                .to_numpy(dtype=float)
            )
            actuals.append(observed)
            for pair in pairs:
                lows[pair].append(fc[quantile_column(pair[0])].to_numpy(dtype=float))
                highs[pair].append(fc[quantile_column(pair[1])].to_numpy(dtype=float))

        y = np.concatenate(actuals)
        self.n_scores_ = int((~np.isnan(y)).sum())
        self.adjustments_ = {
            pair: cqr_adjustment(
                np.concatenate(lows[pair]),
                np.concatenate(highs[pair]),
                y,
                pair[1] - pair[0],
                min_scores=self.min_scores,
            )
            for pair in pairs
        }
        self._base = self.make_base().fit(data)
        return self

    def predict_quantiles(
        self,
        horizon: int,
        quantiles: tuple[float, ...] = DEFAULT_QUANTILES,
        future_exog: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        if self._base is None:
            raise NotFittedError("call fit before predict_quantiles")
        requested = symmetric_pairs(quantiles)
        missing = [p for p in requested if p not in self.adjustments_]
        if missing:
            raise ValueError(f"bands {missing} were not calibrated; fit with those quantiles")
        forecast = _predict(self._base, horizon, quantiles, future_exog)
        adjustments = {p: self.adjustments_[p] for p in requested}
        return apply_adjustments(forecast, adjustments, nonnegative=self.nonnegative)
