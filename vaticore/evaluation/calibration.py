"""Calibration: does the forecast range mean what it says?

A P90 should be exceeded about 10% of the time and a P10 to P90 band should
hold about 80% of outcomes. This module measures that on backtest forecasts,
and can calibrate each fold on the folds before it (rolling conformal), so the
effect of calibration is measured out of sample, exactly as it would run live.

The headline an operator sees ("last 30 days: 81% of hours inside the range,
target 80%") comes from recent_coverage().
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from vaticore.evaluation.backtest import FoldForecast
from vaticore.forecasting.base import quantile_column
from vaticore.forecasting.conformal import (
    InsufficientCalibrationError,
    apply_adjustments,
    cqr_adjustment,
    symmetric_pairs,
)


@dataclass(frozen=True)
class QuantileCalibration:
    """Share of outcomes at or below a quantile forecast. Ideal: equal to quantile."""

    quantile: float
    observed: float
    n: int


@dataclass(frozen=True)
class IntervalCalibration:
    """Share of outcomes inside a band. Ideal: equal to nominal."""

    lower: float
    upper: float
    nominal: float
    observed: float
    mean_width: float
    n: int

    @property
    def gap(self) -> float:
        """Observed minus nominal coverage. Negative means the band is too narrow."""
        return self.observed - self.nominal


@dataclass(frozen=True)
class CalibrationReport:
    """Quantile and band calibration for one model over a set of folds."""

    model: str
    quantiles: tuple[QuantileCalibration, ...]
    intervals: tuple[IntervalCalibration, ...]

    def to_frame(self) -> pd.DataFrame:
        rows = [
            {"model": self.model, "kind": "quantile", "nominal": q.quantile, "observed": q.observed}
            for q in self.quantiles
        ] + [
            {
                "model": self.model,
                "kind": f"band {i.lower:g}-{i.upper:g}",
                "nominal": i.nominal,
                "observed": i.observed,
                "mean_width": i.mean_width,
            }
            for i in self.intervals
        ]
        return pd.DataFrame(rows)


def _stack(forecasts: Sequence[FoldForecast]) -> tuple[np.ndarray, dict[float, np.ndarray]]:
    if not forecasts:
        raise ValueError("no forecasts to evaluate")
    actual = np.concatenate([f.actual for f in forecasts])
    qs = sorted(forecasts[0].quantiles)
    by_q = {q: np.concatenate([f.quantiles[q] for f in forecasts]) for q in qs}
    return actual, by_q


def _interval(
    actual: np.ndarray, lo: np.ndarray, hi: np.ndarray, q_lo: float, q_hi: float
) -> IntervalCalibration:
    mask = ~(np.isnan(actual) | np.isnan(lo) | np.isnan(hi))
    n = int(mask.sum())
    if n == 0:
        raise ValueError("no observed outcomes to evaluate")
    a, lo_m, hi_m = actual[mask], lo[mask], hi[mask]
    return IntervalCalibration(
        lower=q_lo,
        upper=q_hi,
        nominal=q_hi - q_lo,
        observed=float(np.mean((a >= lo_m) & (a <= hi_m))),
        mean_width=float(np.mean(hi_m - lo_m)),
        n=n,
    )


def calibration_report(forecasts: Sequence[FoldForecast]) -> CalibrationReport:
    """Measure quantile and band calibration across a model's fold forecasts."""
    actual, by_q = _stack(forecasts)
    quantile_rows = []
    for q, pred in by_q.items():
        mask = ~(np.isnan(actual) | np.isnan(pred))
        n = int(mask.sum())
        if n == 0:
            raise ValueError("no observed outcomes to evaluate")
        quantile_rows.append(
            QuantileCalibration(
                quantile=q, observed=float(np.mean(actual[mask] <= pred[mask])), n=n
            )
        )
    intervals = tuple(
        _interval(actual, by_q[lo], by_q[hi], lo, hi) for lo, hi in symmetric_pairs(tuple(by_q))
    )
    return CalibrationReport(
        model=forecasts[0].model, quantiles=tuple(quantile_rows), intervals=intervals
    )


def recent_coverage(
    forecasts: Sequence[FoldForecast],
    *,
    hours: int = 24 * 30,
    lower: float = 0.1,
    upper: float = 0.9,
) -> IntervalCalibration:
    """Band coverage over the most recent `hours` of observed outcomes.

    This is the trust badge an operator sees: "last 30 days, 81% of hours
    inside the range (target 80%)".
    """
    ordered = sorted(forecasts, key=lambda f: f.timestamps[0])
    actual = np.concatenate([f.actual for f in ordered])
    lo = np.concatenate([f.quantiles[lower] for f in ordered])
    hi = np.concatenate([f.quantiles[upper] for f in ordered])
    observed = np.flatnonzero(~np.isnan(actual))
    keep = observed[-hours:]
    return _interval(actual[keep], lo[keep], hi[keep], lower, upper)


@dataclass(frozen=True)
class ConformalResult:
    """Fold forecasts after rolling calibration, plus how many folds it touched."""

    forecasts: list[FoldForecast]
    n_calibrated: int
    n_passed_through: int


def conformalize_forecasts(
    forecasts: Sequence[FoldForecast],
    *,
    window_folds: int = 28,
    min_scores: int = 72,
    nonnegative: bool = True,
    suffix: str = "+conformal",
) -> ConformalResult:
    """Calibrate each fold's bands on the misses of the folds just before it.

    Only folds whose whole window ended before this fold began are used, so no
    outcome is seen before it happened. Early folds without `min_scores`
    observed outcomes behind them pass through unchanged and are counted, so
    every policy is still compared on the same days.
    """
    ordered = sorted(forecasts, key=lambda f: f.timestamps[0])
    if not ordered:
        raise ValueError("no forecasts to calibrate")
    pairs = symmetric_pairs(tuple(ordered[0].quantiles))
    if not pairs:
        raise ValueError("forecasts contain no symmetric band to calibrate")

    out: list[FoldForecast] = []
    calibrated = passed = 0
    for i, fold in enumerate(ordered):
        start = fold.timestamps[0]
        prior = [f for f in ordered[max(0, i - window_folds) : i] if f.timestamps[-1] < start]
        frame = pd.DataFrame({quantile_column(q): v for q, v in fold.quantiles.items()})
        adjusted = None
        if prior:
            y = np.concatenate([f.actual for f in prior])
            try:
                adjustments = {
                    (lo, hi): cqr_adjustment(
                        np.concatenate([f.quantiles[lo] for f in prior]),
                        np.concatenate([f.quantiles[hi] for f in prior]),
                        y,
                        hi - lo,
                        min_scores=min_scores,
                    )
                    for lo, hi in pairs
                }
                adjusted = apply_adjustments(frame, adjustments, nonnegative=nonnegative)
            except InsufficientCalibrationError:
                adjusted = None
        if adjusted is None:
            passed += 1
            new_q = dict(fold.quantiles)
        else:
            calibrated += 1
            new_q = {q: adjusted[quantile_column(q)].to_numpy(dtype=float) for q in fold.quantiles}
        out.append(replace(fold, model=fold.model + suffix, quantiles=new_q))
    return ConformalResult(forecasts=out, n_calibrated=calibrated, n_passed_through=passed)
