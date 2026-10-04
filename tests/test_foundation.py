"""Foundation model wrappers: interface, gaps and quantile handling.

The real models are large downloads, so these tests inject small stand-ins
with the same call signatures. The shared base class does all of Vaticore's
work (grid, gaps, quantiles, clipping); the stand-ins only return numbers.
One opt-in test runs the real weights when VATICORE_FOUNDATION_TESTS=1.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from vaticore.forecasting import Forecaster
from vaticore.forecasting.base import InsufficientHistoryError, NotFittedError, quantile_column
from vaticore.forecasting.foundation import ChronosForecaster, TimesFMForecaster
from vaticore.schemas import LOAD_KW, TIMESTAMP


class _Tensor:
    """Just enough of a torch tensor for the Chronos adapter."""

    def __init__(self, array: np.ndarray) -> None:
        self.array = array

    def __getitem__(self, key: Any) -> _Tensor:
        return _Tensor(self.array[key])

    def detach(self) -> _Tensor:
        return self

    def cpu(self) -> _Tensor:
        return self

    def numpy(self) -> np.ndarray:
        return self.array


class FakeChronos:
    """Forecasts the last observed value, with quantiles spread around it."""

    def __init__(self) -> None:
        self.contexts: list[np.ndarray] = []

    def predict_quantiles(
        self, inputs: list[np.ndarray], prediction_length: int, quantile_levels: list[float]
    ) -> tuple[list[_Tensor], list[_Tensor]]:
        context = inputs[0]
        self.contexts.append(context)
        last = float(context[~np.isnan(context)][-1])
        levels = np.asarray(quantile_levels)
        # Deliberately unsorted rows prove the wrapper sorts them.
        row = (last + 10.0 * (levels - 0.5))[::-1]
        out = np.tile(row, (prediction_length, 1))[None, :, :]
        return [_Tensor(out)], [_Tensor(out[:, :, len(levels) // 2])]


class FakeTimesFM:
    def __init__(self) -> None:
        self.contexts: list[np.ndarray] = []

    def predict(self, context: np.ndarray, horizon: int, return_quantiles: bool) -> Any:
        self.contexts.append(context)
        levels = np.asarray(TimesFMForecaster.model_quantiles)
        row = -5.0 + 10.0 * levels
        return SimpleNamespace(quantiles=np.tile(row, (horizon, 1)))


def _site(hours: int = 24 * 10) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=hours, freq="h", tz="UTC")
    return pd.DataFrame({TIMESTAMP: index, LOAD_KW: 100.0 + np.arange(hours) % 24})


@pytest.mark.parametrize("cls", [ChronosForecaster, TimesFMForecaster])
def test_is_a_forecaster(cls: type[Forecaster]) -> None:
    assert issubclass(cls, Forecaster)


def test_chronos_output_shape_index_and_order() -> None:
    model = ChronosForecaster(LOAD_KW, pipeline=FakeChronos()).fit(_site())
    forecast = model.predict_quantiles(horizon=24, quantiles=(0.1, 0.5, 0.9))

    assert list(forecast.columns) == [quantile_column(q) for q in (0.1, 0.5, 0.9)]
    assert len(forecast) == 24
    assert forecast.index[0] == pd.Timestamp("2024-01-11", tz="UTC")
    assert str(forecast.index.dtype) == "datetime64[ns, UTC]"
    assert np.all(np.diff(forecast.to_numpy(), axis=1) >= 0)
    # Last value 123, quantiles spread by 10 * (q - 0.5).
    np.testing.assert_allclose(forecast.iloc[0].to_numpy(), [119.0, 123.0, 127.0])


def test_quantiles_between_model_levels_are_interpolated() -> None:
    model = TimesFMForecaster(LOAD_KW, pipeline=FakeTimesFM(), nonnegative=False).fit(_site())
    forecast = model.predict_quantiles(horizon=3, quantiles=(0.25, 0.5, 0.85))
    np.testing.assert_allclose(forecast.iloc[0].to_numpy(), [-2.5, 0.0, 3.5])


def test_nonnegative_clips_and_signed_targets_do_not() -> None:
    clipped = TimesFMForecaster(LOAD_KW, pipeline=FakeTimesFM()).fit(_site())
    signed = TimesFMForecaster(LOAD_KW, pipeline=FakeTimesFM(), nonnegative=False).fit(_site())
    assert clipped.predict_quantiles(4)["q0.1"].min() == 0.0
    assert signed.predict_quantiles(4)["q0.1"].min() == pytest.approx(-4.0)


def test_quantiles_outside_the_model_range_raise() -> None:
    model = TimesFMForecaster(LOAD_KW, pipeline=FakeTimesFM()).fit(_site())
    with pytest.raises(ValueError, match=r"0\.05"):
        model.predict_quantiles(horizon=24, quantiles=(0.05, 0.5, 0.95))


def test_gaps_stay_visible_as_nan_in_the_context() -> None:
    site = _site().drop(index=range(100, 110))  # ten missing hours
    fake = FakeChronos()
    ChronosForecaster(LOAD_KW, pipeline=fake).fit(site).predict_quantiles(24)
    context = fake.contexts[0]
    assert context.size == 24 * 10
    assert np.isnan(context).sum() == 10


def test_context_is_limited_to_the_most_recent_steps() -> None:
    fake = FakeTimesFM()
    TimesFMForecaster(LOAD_KW, pipeline=fake, context_length=48).fit(_site()).predict_quantiles(1)
    assert fake.contexts[0].size == 48


def test_timesfm_drops_leading_missing_values() -> None:
    site = _site()
    site.loc[:5, LOAD_KW] = np.nan
    fake = FakeTimesFM()
    TimesFMForecaster(LOAD_KW, pipeline=fake).fit(site).predict_quantiles(1)
    assert not np.isnan(fake.contexts[0][0])


def test_too_little_observed_history_raises() -> None:
    site = _site(hours=24 * 3)
    site.loc[: 24 * 2, LOAD_KW] = np.nan
    with pytest.raises(InsufficientHistoryError):
        ChronosForecaster(LOAD_KW, pipeline=FakeChronos()).fit(site)


def test_predict_before_fit_raises() -> None:
    with pytest.raises(NotFittedError):
        ChronosForecaster(LOAD_KW, pipeline=FakeChronos()).predict_quantiles(24)


@pytest.mark.skipif(
    os.environ.get("VATICORE_FOUNDATION_TESTS") != "1",
    reason="downloads model weights; set VATICORE_FOUNDATION_TESTS=1 to run",
)
@pytest.mark.parametrize("cls", [ChronosForecaster, TimesFMForecaster])
def test_real_weights_forecast_a_daily_cycle(cls: type[ChronosForecaster]) -> None:
    hours = np.arange(24 * 28)
    load = 100.0 + 40.0 * np.sin((hours % 24 - 6) / 24 * 2 * np.pi)
    site = pd.DataFrame(
        {
            TIMESTAMP: pd.date_range("2024-01-01", periods=hours.size, freq="h", tz="UTC"),
            LOAD_KW: load,
        }
    )
    forecast = cls(LOAD_KW).fit(site).predict_quantiles(24)
    expected = load[:24]
    assert np.mean(np.abs(forecast["q0.5"].to_numpy() - expected)) < 5.0
