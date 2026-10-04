"""Pretrained time series foundation models behind the Forecaster interface.

Foundation models are trained in advance on very large collections of time
series and forecast a new series "zero shot", from its recent history alone,
with no per site training. That matters commercially: a new site with only a
few weeks of data can get a forecast on its first day, while the quantile GBM
needs months of the site's own history.

Two backends are wrapped:

  ChronosForecaster   Amazon's Chronos-2 (default "amazon/chronos-2").
  TimesFMForecaster   Google's TimesFM (default "google/timesfm-3.0-pytorch").

Both are challengers, not the planning default. They earn production only by
beating the current model on pinball loss, calibration and value in the
standard backtest (docs/research). Both run on a CPU; a GPU only makes them
faster.

How the wrappers behave, and why:
  - fit() stores the recent history on a regular time grid (the model's
    context). Nothing is trained, so fitting is instant and the same weights
    serve every site. Weights are loaded once per process and shared.
  - Gaps stay visible as NaN in the context. Chronos-2 handles missing values
    natively; TimesFM fills interior gaps by linear interpolation inside the
    library. A context with too few observed values raises
    InsufficientHistoryError instead of forecasting from nothing.
  - The models predict a fixed set of quantile levels. Requested quantiles
    between those levels are interpolated; quantiles outside the range the
    model predicts raise ValueError rather than being extrapolated silently.
  - Outputs are clipped at zero for non negative targets and sorted so
    quantiles never cross, matching every other Vaticore forecaster.

The packages are optional (`uv sync --extra foundation`); importing this module
does not import them. Weights download from Hugging Face on first use.
"""

from __future__ import annotations

from abc import abstractmethod
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd

from vaticore.forecasting.base import (
    DEFAULT_QUANTILES,
    Forecaster,
    InsufficientHistoryError,
    NotFittedError,
    quantile_column,
)
from vaticore.schemas import TIMESTAMP

CHRONOS_MODEL = "amazon/chronos-2"
TIMESFM_MODEL = "google/timesfm-3.0-pytorch"

# Hours of history given to the model by default: 12 weeks, enough to show
# daily and weekly patterns without making each forecast slow on a CPU.
DEFAULT_CONTEXT_LENGTH = 24 * 7 * 12


class FoundationForecaster(Forecaster):
    """Shared behaviour for pretrained, zero shot forecasters.

    Parameters
    ----------
    target:
        Column to forecast, for example "load_kw" or "net_load_kw".
    context_length:
        Maximum number of most recent grid steps passed to the model.
    min_history:
        Minimum number of observed (non missing) values in the context.
        Defaults to two days at the inferred resolution.
    nonnegative:
        Clip forecasts at zero. Set False for a signed target such as net load.
    model_id:
        Hugging Face model name or a local directory with the weights.
    device:
        Torch device, for example "cpu" or "cuda".
    pipeline:
        An already loaded model object. Used by tests and by callers that load
        the weights themselves; when None the weights are loaded on first use.
    """

    #: Quantile levels the underlying model predicts directly.
    model_quantiles: tuple[float, ...] = ()

    def __init__(
        self,
        target: str,
        *,
        context_length: int = DEFAULT_CONTEXT_LENGTH,
        min_history: int | None = None,
        nonnegative: bool = True,
        model_id: str,
        device: str = "cpu",
        pipeline: Any = None,
    ) -> None:
        if context_length < 2:
            raise ValueError("context_length must be at least 2")
        self.target = target
        self.context_length = context_length
        self.min_history = min_history
        self.nonnegative = nonnegative
        self.model_id = model_id
        self.device = device
        self._pipeline = pipeline

        self._context: np.ndarray | None = None
        self._freq: pd.Timedelta | None = None
        self._last_timestamp: pd.Timestamp | None = None

    def fit(self, history: pd.DataFrame) -> FoundationForecaster:
        if self.target not in history.columns:
            raise ValueError(f"target column {self.target!r} not present in history")
        frame = (
            history[[TIMESTAMP, self.target]]
            .dropna(subset=[TIMESTAMP])
            .drop_duplicates(subset=[TIMESTAMP])
            .sort_values(TIMESTAMP)
            .set_index(TIMESTAMP)
        )
        if len(frame) < 2:
            raise InsufficientHistoryError("need at least two observations to infer spacing")
        freq = pd.Timedelta(frame.index.to_series().diff().dropna().median())
        if freq <= pd.Timedelta(0):
            raise ValueError("non increasing timestamps in history")

        # Regular grid so a gap is a NaN at the right place, not a silent shift.
        grid = pd.date_range(frame.index[0], frame.index[-1], freq=freq, name=TIMESTAMP)
        values = frame[self.target].reindex(grid).to_numpy(dtype=float)
        context = values[-self.context_length :]

        min_history = self.min_history or max(2, round(2 * pd.Timedelta("1D") / freq))
        observed = int(np.sum(~np.isnan(context)))
        if observed < min_history:
            raise InsufficientHistoryError(
                f"need at least {min_history} observed values in the context, got {observed}"
            )

        self._context = context
        self._freq = freq
        self._last_timestamp = pd.Timestamp(grid[-1])
        return self

    def predict_quantiles(
        self,
        horizon: int,
        quantiles: tuple[float, ...] = DEFAULT_QUANTILES,
    ) -> pd.DataFrame:
        if self._context is None or self._freq is None or self._last_timestamp is None:
            raise NotFittedError("call fit before predict_quantiles")
        if horizon < 1:
            raise ValueError("horizon must be a positive integer")
        lo, hi = min(self.model_quantiles), max(self.model_quantiles)
        outside = [q for q in quantiles if q < lo - 1e-9 or q > hi + 1e-9]
        if outside:
            raise ValueError(
                f"{type(self).__name__} predicts quantiles from {lo:g} to {hi:g}; "
                f"cannot provide {outside}"
            )

        levels = np.asarray(self.model_quantiles, dtype=float)
        raw = np.asarray(self._predict(self._context, horizon), dtype=float)
        if raw.shape != (horizon, len(levels)):
            raise RuntimeError(
                f"model returned shape {raw.shape}, expected {(horizon, len(levels))}"
            )
        raw = np.sort(raw, axis=1)
        # Interpolate each requested quantile across the model's own levels.
        values = np.column_stack([[np.interp(q, levels, row) for row in raw] for q in quantiles])
        if self.nonnegative:
            values = np.clip(values, 0.0, None)
        values = np.sort(values, axis=1)

        index = pd.date_range(
            start=self._last_timestamp + self._freq,
            periods=horizon,
            freq=self._freq,
            name=TIMESTAMP,
        )
        return pd.DataFrame(values, index=index, columns=[quantile_column(q) for q in quantiles])

    @property
    def pipeline(self) -> Any:
        """The loaded model, loading the weights on first use."""
        if self._pipeline is None:
            self._pipeline = self._load()
        return self._pipeline

    @abstractmethod
    def _load(self) -> Any:
        """Load the model weights."""

    @abstractmethod
    def _predict(self, context: np.ndarray, horizon: int) -> np.ndarray:
        """Return a (horizon, len(model_quantiles)) array of quantile forecasts."""


class ChronosForecaster(FoundationForecaster):
    """Amazon Chronos-2, zero shot, with native handling of missing values."""

    model_quantiles: tuple[float, ...] = (
        0.01, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5,
        0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99,
    )  # fmt: skip

    def __init__(
        self,
        target: str,
        *,
        model_id: str = CHRONOS_MODEL,
        **kwargs: Any,
    ) -> None:
        super().__init__(target, model_id=model_id, **kwargs)

    def _load(self) -> Any:
        return _load_chronos(self.model_id, self.device)

    def _predict(self, context: np.ndarray, horizon: int) -> np.ndarray:
        quantiles, _mean = self.pipeline.predict_quantiles(
            [context.astype(np.float32)],
            prediction_length=horizon,
            quantile_levels=list(self.model_quantiles),
        )
        # One series in, one univariate target: shape (1, horizon, levels).
        return np.asarray(quantiles[0][0].detach().cpu().numpy(), dtype=float)


class TimesFMForecaster(FoundationForecaster):
    """Google TimesFM, zero shot. The library interpolates interior gaps."""

    model_quantiles: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)

    def __init__(
        self,
        target: str,
        *,
        model_id: str = TIMESFM_MODEL,
        **kwargs: Any,
    ) -> None:
        super().__init__(target, model_id=model_id, **kwargs)

    def _load(self) -> Any:
        return _load_timesfm(self.model_id, self.device)

    def _predict(self, context: np.ndarray, horizon: int) -> np.ndarray:
        # Leading missing values carry no information; TimesFM drops them too.
        observed = np.flatnonzero(~np.isnan(context))
        trimmed = context[observed[0] :] if observed.size else context
        output = self.pipeline.predict(
            trimmed.astype(np.float32), horizon=horizon, return_quantiles=True
        )
        return np.asarray(output.quantiles, dtype=float)


@lru_cache(maxsize=4)
def _load_chronos(model_id: str, device: str) -> Any:
    try:
        from chronos import BaseChronosPipeline
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError("Chronos needs the foundation extra: uv sync --extra foundation") from exc
    return BaseChronosPipeline.from_pretrained(model_id, device_map=device)


@lru_cache(maxsize=4)
def _load_timesfm(model_id: str, device: str) -> Any:
    try:
        import timesfm
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError("TimesFM needs the foundation extra: uv sync --extra foundation") from exc
    return timesfm.TimesFM3Forecaster.from_pretrained(model_id, device=device)
