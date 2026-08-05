"""Forecasting models. Every model implements the Forecaster interface."""

from vaticore.forecasting.base import Forecaster
from vaticore.forecasting.baseline import PersistenceForecaster

__all__ = ["Forecaster", "PersistenceForecaster"]
