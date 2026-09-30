from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vaticore.evaluation.backtest import FoldForecast
from vaticore.evaluation.benchmark import external_forecasts, pooled_scores


def _fold(fold: int, actual: list[float]) -> FoldForecast:
    stamps = pd.date_range("2024-01-01", periods=len(actual), freq="h", tz="UTC") + pd.Timedelta(
        hours=len(actual) * fold
    )
    a = np.asarray(actual, dtype=float)
    return FoldForecast(model="m", fold=fold, timestamps=stamps, actual=a, quantiles={0.5: a})


def test_external_forecast_is_aligned_to_the_same_hours() -> None:
    template = [_fold(0, [1.0, 2.0]), _fold(1, [3.0, 4.0])]
    index = pd.date_range("2024-01-01", periods=3, freq="h", tz="UTC")  # misses the last hour
    frame = pd.DataFrame({"p50": [10.0, 20.0, 30.0]}, index=index)
    aligned = external_forecasts(template, frame, {0.5: "p50"}, model="tso", scale=0.1)
    assert aligned[0].quantiles[0.5].tolist() == pytest.approx([1.0, 2.0])
    assert aligned[1].quantiles[0.5][0] == pytest.approx(3.0)
    assert np.isnan(aligned[1].quantiles[0.5][1])
    assert aligned[1].actual.tolist() == [3.0, 4.0]
    with pytest.raises(ValueError):
        external_forecasts(template, frame, {0.5: "nope"}, model="tso")


def test_pooled_scores_known_answer_and_gap_handling() -> None:
    fold = FoldForecast(
        model="m",
        fold=0,
        timestamps=pd.date_range("2024-01-01", periods=4, freq="h", tz="UTC"),
        actual=np.array([0.0, 10.0, 5.0, np.nan]),
        quantiles={
            0.1: np.array([-1.0, 0.0, 4.0, 0.0]),
            0.5: np.array([0.0, 8.0, 5.0, 0.0]),
            0.9: np.array([1.0, 9.0, 6.0, 0.0]),
        },
    )
    s = pooled_scores([fold])
    assert s.hours == 3
    assert s.mae == pytest.approx(2.0 / 3.0)
    assert s.coverage_80 == pytest.approx(2.0 / 3.0)  # 10 falls above the band
