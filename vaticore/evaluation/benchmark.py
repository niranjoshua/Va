"""Score outside forecasts (a grid operator's, a vendor's) on Vaticore's terms.

A fair comparison scores every forecast on exactly the same hours with the
same metrics. external_forecasts() aligns an outside forecast to the folds of
a Vaticore backtest, and pooled_scores() scores any set of fold forecasts over
all their hours at once, so a professional benchmark and our own models sit in
one table.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from vaticore.evaluation.backtest import FoldForecast
from vaticore.evaluation.metrics import coverage, mae, mean_pinball_loss, rmse


def external_forecasts(
    template: Sequence[FoldForecast],
    frame: pd.DataFrame,
    columns: Mapping[float, str],
    *,
    model: str,
    scale: float = 1.0,
) -> list[FoldForecast]:
    """Align an outside forecast to the same folds (and actuals) as template.

    frame is indexed by timestamp; columns maps each quantile to the column
    holding it. Hours the outside forecast does not cover stay NaN and are left
    out of scoring for that forecast only.
    """
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("frame must be indexed by timestamp")
    missing = [c for c in columns.values() if c not in frame.columns]
    if missing:
        raise ValueError(f"columns {missing} are not in the frame")
    out = []
    for fold in template:
        quantiles = {
            q: frame[col].reindex(fold.timestamps).to_numpy(dtype=float) * scale
            for q, col in columns.items()
        }
        out.append(
            FoldForecast(
                model=model,
                fold=fold.fold,
                timestamps=fold.timestamps,
                actual=fold.actual,
                quantiles=quantiles,
            )
        )
    return out


@dataclass(frozen=True)
class PooledScores:
    """Scores over every hour of a set of folds."""

    model: str
    pinball: float
    mae: float
    rmse: float
    coverage_80: float | None
    hours: int


def pooled_scores(forecasts: Sequence[FoldForecast]) -> PooledScores:
    """Pinball, MAE and RMSE (on the median) and P10-P90 coverage, pooled."""
    if not forecasts:
        raise ValueError("no forecasts to score")
    actual = np.concatenate([f.actual for f in forecasts])
    qs = sorted(forecasts[0].quantiles)
    by_q = {q: np.concatenate([f.quantiles[q] for f in forecasts]) for q in qs}
    # Score only hours every quantile covers, so no forecast is judged on a
    # subset of hours chosen by its own gaps.
    keep = ~np.isnan(actual)
    for values in by_q.values():
        keep &= ~np.isnan(values)
    a = actual[keep]
    by_q = {q: v[keep] for q, v in by_q.items()}
    median = by_q[min(qs, key=lambda q: abs(q - 0.5))]
    cov = coverage(a, by_q[0.1], by_q[0.9]) if 0.1 in by_q and 0.9 in by_q else None
    return PooledScores(
        model=forecasts[0].model,
        pinball=mean_pinball_loss(a, by_q),
        mae=mae(a, median),
        rmse=rmse(a, median),
        coverage_80=cov,
        hours=int(keep.sum()),
    )
