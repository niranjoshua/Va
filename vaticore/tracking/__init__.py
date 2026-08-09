"""Experiment tracking: log every model comparison to MLflow, reproducibly.

Every backtest is a comparison of a candidate model against the persistence
baseline. Logging that comparison (the site, the model, the config and the
metrics) is how model choices stay reproducible for both pilots and the research
paper. This package wraps MLflow behind a small interface so the rest of the
system never imports mlflow directly and works whether or not tracking is
configured.

MLflow is an optional dependency (the "tracking" extra). Callers should obtain a
tracker with get_tracker(); when no tracking URI is configured, or mlflow is not
installed, they get a NoOpTracker and nothing else changes.
"""

from vaticore.tracking.mlflow_tracker import (
    BacktestSummary,
    MlflowTracker,
    NoOpTracker,
    Tracker,
    get_tracker,
    summarize_backtest,
)

__all__ = [
    "BacktestSummary",
    "MlflowTracker",
    "NoOpTracker",
    "Tracker",
    "get_tracker",
    "summarize_backtest",
]
