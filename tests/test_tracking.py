"""Tests for experiment tracking.

These do not require mlflow (the optional "tracking" extra). They cover the pure
summary reduction, the no-op fallback, the factory's configuration logic, and
that the engine forwards a backtest to whatever tracker it is given.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd
import pytest

from vaticore.config import Settings
from vaticore.engine import run_backtest
from vaticore.evaluation.backtest import BacktestResult, backtest_site
from vaticore.forecasting import PersistenceForecaster
from vaticore.schemas import LOAD_KW
from vaticore.tracking import (
    NoOpTracker,
    Tracker,
    get_tracker,
    summarize_backtest,
)
from vaticore.tracking import mlflow_tracker as mt
from vaticore.tracking.mlflow_tracker import BacktestSummary


def _result(single_site: pd.DataFrame) -> BacktestResult:
    return backtest_site(
        single_site,
        target=LOAD_KW,
        make_model=lambda: PersistenceForecaster(target=LOAD_KW),
        horizon=24,
        initial=24 * 14,
        step=24,
        model_name="quantile_gbm",
    )


def test_summarize_records_params_and_per_model_metrics(single_site: pd.DataFrame) -> None:
    result = _result(single_site)
    summary = summarize_backtest(
        result,
        candidate_model="quantile_gbm",
        operator_id="op1",
        site_id="siteA",
    )

    assert summary.params["operator_id"] == "op1"
    assert summary.params["site_id"] == "siteA"
    assert summary.params["candidate_model"] == "quantile_gbm"
    assert summary.params["target"] == LOAD_KW
    assert summary.params["horizon"] == 24
    assert summary.params["n_folds"] >= 1

    # Mean fold metrics logged per model, for both candidate and baseline.
    assert "persistence.pinball" in summary.metrics
    assert "quantile_gbm.pinball" in summary.metrics
    assert "quantile_gbm.mae" in summary.metrics
    assert "quantile_gbm.coverage" in summary.metrics


def test_summarize_reports_skill_over_baseline(single_site: pd.DataFrame) -> None:
    result = _result(single_site)
    summary = summarize_backtest(result, candidate_model="quantile_gbm")

    assert "pinball_skill_vs_baseline" in summary.metrics
    assert "beats_baseline" in summary.metrics
    assert summary.metrics["beats_baseline"] in (0.0, 1.0)


def test_summarize_omits_skill_when_candidate_is_baseline(single_site: pd.DataFrame) -> None:
    # A backtest whose candidate IS persistence should not report skill over itself.
    result = backtest_site(
        single_site,
        target=LOAD_KW,
        make_model=lambda: PersistenceForecaster(target=LOAD_KW),
        horizon=24,
        initial=24 * 14,
        step=24,
        model_name="persistence",
    )
    summary = summarize_backtest(result, candidate_model="persistence")
    assert "pinball_skill_vs_baseline" not in summary.metrics


def test_summarize_drops_unset_params(single_site: pd.DataFrame) -> None:
    result = _result(single_site)
    summary = summarize_backtest(result, candidate_model="quantile_gbm")
    # operator_id/site_id were not provided, so they must not appear as "None".
    assert "operator_id" not in summary.params
    assert "site_id" not in summary.params


def test_summarize_includes_extra_params(single_site: pd.DataFrame) -> None:
    result = _result(single_site)
    summary = summarize_backtest(
        result,
        candidate_model="quantile_gbm",
        extra_params={"initial": 336, "exog": "temperature_2m"},
    )
    assert summary.params["initial"] == 336
    assert summary.params["exog"] == "temperature_2m"


def test_noop_tracker_is_disabled_and_returns_none(single_site: pd.DataFrame) -> None:
    tracker = NoOpTracker()
    assert tracker.enabled is False
    result = _result(single_site)
    assert tracker.log_backtest(result, candidate_model="quantile_gbm") is None


def test_get_tracker_returns_noop_without_uri() -> None:
    settings = Settings(mlflow_tracking_uri=None)
    assert isinstance(get_tracker(settings), NoOpTracker)


def test_get_tracker_degrades_to_noop_when_mlflow_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # mlflow is an optional extra. When it is absent, MlflowTracker raises
    # ImportError; the factory must degrade to a NoOpTracker rather than fail the
    # pipeline. Simulate the missing extra so the test does not depend on the env.
    def _raise(*args: Any, **kwargs: Any) -> mt.MlflowTracker:
        raise ImportError("mlflow not installed")

    monkeypatch.setattr(mt, "MlflowTracker", _raise)
    settings = Settings(mlflow_tracking_uri="sqlite:///unused.db")
    assert isinstance(get_tracker(settings), NoOpTracker)


def test_mlflow_tracker_logs_a_real_run(single_site: pd.DataFrame, tmp_path: Any) -> None:
    # Exercises the real MLflow path when the tracking extra is installed; skips
    # cleanly otherwise, so the suite never requires the optional dependency.
    pytest.importorskip("mlflow")
    import mlflow

    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    tracker = mt.MlflowTracker(tracking_uri=uri, experiment="vaticore-test")
    assert tracker.enabled is True

    result = _result(single_site)
    run_id = tracker.log_backtest(
        result,
        candidate_model="quantile_gbm",
        operator_id="op1",
        site_id="siteA",
    )
    assert run_id is not None

    mlflow.set_tracking_uri(uri)
    run = mlflow.get_run(run_id)
    assert run.data.params["operator_id"] == "op1"
    assert "quantile_gbm.pinball" in run.data.metrics
    assert "pinball_skill_vs_baseline" in run.data.metrics


class _RecordingTracker(Tracker):
    """Captures the last logged backtest so engine wiring can be asserted."""

    def __init__(self) -> None:
        self.summary: BacktestSummary | None = None

    @property
    def enabled(self) -> bool:
        return True

    def log_backtest(
        self,
        result: BacktestResult,
        *,
        candidate_model: str,
        operator_id: str | None = None,
        site_id: str | None = None,
        extra_params: Mapping[str, Any] | None = None,
        run_name: str | None = None,
    ) -> str | None:
        self.summary = summarize_backtest(
            result,
            candidate_model=candidate_model,
            operator_id=operator_id,
            site_id=site_id,
            extra_params=extra_params,
        )
        return "run-123"


def test_run_backtest_logs_to_supplied_tracker(single_site: pd.DataFrame) -> None:
    tracker = _RecordingTracker()
    run_backtest(
        single_site,
        LOAD_KW,
        horizon=24,
        initial=24 * 14,
        step=24,
        model="persistence",
        tracker=tracker,
        operator_id="op1",
        site_id="siteA",
    )
    assert tracker.summary is not None
    assert tracker.summary.params["operator_id"] == "op1"
    assert tracker.summary.params["site_id"] == "siteA"
    # Engine forwards backtest config as extra params.
    assert tracker.summary.params["initial"] == 24 * 14
    assert tracker.summary.params["step"] == 24


def test_run_backtest_without_tracker_is_unchanged(single_site: pd.DataFrame) -> None:
    # The default path takes no tracker and behaves exactly as before.
    result = run_backtest(
        single_site,
        LOAD_KW,
        horizon=24,
        initial=24 * 14,
        step=24,
        model="persistence",
    )
    assert not result.to_frame().empty
