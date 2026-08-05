"""Quantile gradient boosting forecaster: the workhorse and likely MVP primary.

This trains one gradient boosted model per quantile (LightGBM quantile
objective) on calendar, lag and weather features. It is the next model to build
once the pipeline is proven end to end with the baseline (Golden Rule 1).

The class already conforms to the Forecaster interface so the backtest harness,
ensemble and dashboard can be wired against it before the internals are filled
in. fit and predict_quantiles raise a clear NotImplementedError until then.
"""

from __future__ import annotations

import pandas as pd

from vaticore.forecasting.base import (
    DEFAULT_QUANTILES,
    Forecaster,
)


class QuantileGBMForecaster(Forecaster):
    """One LightGBM model per quantile over engineered features."""

    def __init__(
        self,
        target: str,
        quantiles: tuple[float, ...] = DEFAULT_QUANTILES,
        lags: tuple[int, ...] = (1, 24, 168),
    ) -> None:
        self.target = target
        self.quantiles = quantiles
        self.lags = lags

    def fit(self, history: pd.DataFrame) -> QuantileGBMForecaster:
        # TODO(build task 5): build features (calendar, lags, weather), train one
        # LightGBM quantile regressor per quantile, store models keyed by quantile.
        raise NotImplementedError("QuantileGBMForecaster.fit lands in build task 5")

    def predict_quantiles(
        self,
        horizon: int,
        quantiles: tuple[float, ...] = DEFAULT_QUANTILES,
    ) -> pd.DataFrame:
        # TODO(build task 5): roll features forward over the horizon and predict
        # each quantile, then sort columns per row to prevent quantile crossing.
        raise NotImplementedError("QuantileGBMForecaster.predict_quantiles lands in build task 5")
