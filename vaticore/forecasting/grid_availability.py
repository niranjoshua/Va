"""Forecasting whether the grid will be on, hour by hour.

On weak networks (much of Nigeria, and many emerging markets) a site's grid
supply comes and goes: rationed by feeder, scheduled by distribution company,
cut by faults. Knowing which hours the grid can be counted on decides whether
the battery is charged from the grid, and whether a generator must be booked.

The target is grid_available: 1 when the grid supplied the site in an
interval, 0 when it did not, missing when unknown. This baseline estimates the
chance the grid is on for each hour of the week from the recent past, with
more weight on recent weeks. Each hour of the week borrows strength from the
same hour on every day (rationing is mostly a daily pattern), which in turn
borrows from the site's overall availability, so a rarely seen hour is not
trusted blindly. It implements the standard Forecaster interface:
the q quantile of an on/off outcome with probability p of being on is 1 when
p > 1 - q, else 0. So the P10 plan counts on the grid only in hours it has
been on at least 90% of the time: a cautious plan for the bad day.

It is deliberately simple and explainable; it is the persistence of grid
forecasting. Richer models (feeder schedules, published load shedding bands,
weather-driven faults) must beat it in the value backtest to earn their place.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from vaticore.forecasting.base import (
    DEFAULT_QUANTILES,
    Forecaster,
    InsufficientHistoryError,
    NotFittedError,
    quantile_column,
)
from vaticore.schemas import GRID_AVAILABLE, TIMESTAMP

_HOURS_PER_WEEK = 24 * 7


class GridAvailabilityForecaster(Forecaster):
    """Hour-of-week grid availability with recency weighting.

    Parameters
    ----------
    window_days:
        How much recent history to learn from.
    half_life_days:
        Weight halves every half_life_days back, so a new supply pattern is
        picked up within a couple of weeks.
    prior_strength:
        Weight, in (recency weighted) observations, with which each hour of the
        week is pulled towards its hour of day, and each hour of day towards the
        site's overall availability.
    min_observations:
        Minimum known on/off observations required to fit.
    """

    def __init__(
        self,
        target: str = GRID_AVAILABLE,
        *,
        window_days: int = 28,
        half_life_days: float = 7.0,
        prior_strength: float = 1.0,
        min_observations: int = 24 * 7,
    ) -> None:
        self.target = target
        self.window_days = window_days
        self.half_life_days = half_life_days
        self.prior_strength = prior_strength
        self.min_observations = min_observations
        self.probability_: np.ndarray | None = None  # by hour of week, Monday 00:00 first
        self._last: pd.Timestamp | None = None
        self._freq: pd.Timedelta | None = None

    def fit(self, history: pd.DataFrame) -> GridAvailabilityForecaster:
        if self.target not in history.columns:
            raise ValueError(f"target column {self.target!r} not present in history")
        frame = history[[TIMESTAMP, self.target]].dropna(subset=[TIMESTAMP])
        frame = frame.sort_values(TIMESTAMP)
        stamps = pd.DatetimeIndex(frame[TIMESTAMP])
        self._last = stamps[-1]
        self._freq = pd.Timedelta(stamps.to_series().diff().dropna().median())
        if self._freq != pd.Timedelta(hours=1):
            raise ValueError("grid availability is modelled at hourly resolution; resample first")

        recent = stamps >= self._last - pd.Timedelta(days=self.window_days)
        values = frame[self.target].to_numpy(dtype=float)[recent]
        stamps = stamps[recent]
        known = ~np.isnan(values)
        if known.sum() < self.min_observations:
            raise InsufficientHistoryError(
                f"need at least {self.min_observations} known grid observations, got {known.sum()}"
            )
        values, stamps = (values[known] >= 0.5).astype(float), stamps[known]
        age_days = (self._last - stamps) / pd.Timedelta(days=1)
        weights = 0.5 ** (np.asarray(age_days, dtype=float) / self.half_life_days)
        hod = np.asarray(stamps.hour)
        how = np.asarray(stamps.dayofweek * 24 + stamps.hour)
        k = self.prior_strength

        overall = float(np.average(values, weights=weights))
        on_d = np.bincount(hod, weights=weights * values, minlength=24)
        tot_d = np.bincount(hod, weights=weights, minlength=24)
        by_hour_of_day = (on_d + k * overall) / (tot_d + k)

        on_w = np.bincount(how, weights=weights * values, minlength=_HOURS_PER_WEEK)
        tot_w = np.bincount(how, weights=weights, minlength=_HOURS_PER_WEEK)
        prior = by_hour_of_day[np.arange(_HOURS_PER_WEEK) % 24]
        self.probability_ = (on_w + k * prior) / (tot_w + k)
        return self

    def predict_probability(self, horizon: int) -> pd.Series:
        """Chance the grid is on in each of the next `horizon` hours."""
        if self.probability_ is None or self._last is None or self._freq is None:
            raise NotFittedError("call fit before predicting")
        if horizon < 1:
            raise ValueError("horizon must be a positive integer")
        index = pd.date_range(
            self._last + self._freq, periods=horizon, freq=self._freq, name=TIMESTAMP
        )
        how = np.asarray(index.dayofweek * 24 + index.hour)
        return pd.Series(self.probability_[how], index=index, name="p_grid_on")

    def predict_quantiles(
        self,
        horizon: int,
        quantiles: tuple[float, ...] = DEFAULT_QUANTILES,
    ) -> pd.DataFrame:
        p = self.predict_probability(horizon)
        cols = {quantile_column(q): (p > 1.0 - q).astype(float) for q in quantiles}
        return pd.DataFrame(cols, index=p.index)
