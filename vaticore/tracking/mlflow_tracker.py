"""MLflow backed experiment tracking, with a no-op fallback.

The heavy lifting is done by summarize_backtest(), a pure function that reduces a
BacktestResult to the params and metrics worth logging. It has no MLflow
dependency, so it is trivially testable. MlflowTracker just forwards that summary
to MLflow; NoOpTracker discards it. get_tracker() picks between them based on
configuration, so callers never branch on whether tracking is enabled.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vaticore.config import Settings, get_settings

if TYPE_CHECKING:
    from vaticore.evaluation.backtest import BacktestResult

# The candidate model in a backtest is scored against this baseline. Named here
# so the summary knows which model the "skill" numbers are relative to.
BASELINE_MODEL = "persistence"


@dataclass(frozen=True)
class BacktestSummary:
    """Params and metrics distilled from a backtest, ready to log.

    params are strings/scalars describing the run (site, model, config).
    metrics are floats (mean fold metrics per model, plus skill over baseline).
    """

    params: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)


def _mean(values: list[float]) -> float | None:
    finite = [v for v in values if v is not None]
    if not finite:
        return None
    return float(sum(finite) / len(finite))


def summarize_backtest(
    result: BacktestResult,
    *,
    candidate_model: str,
    operator_id: str | None = None,
    site_id: str | None = None,
    extra_params: Mapping[str, Any] | None = None,
) -> BacktestSummary:
    """Reduce a BacktestResult to loggable params and metrics.

    Mean fold metrics are computed per model. When both the candidate and the
    baseline are present, the candidate's pinball skill over the baseline and a
    beats_baseline flag are added, so a model that fails to beat persistence is
    recorded honestly rather than hidden.
    """
    frame = result.to_frame()
    models = sorted(frame["model"].unique())

    params: dict[str, Any] = {
        "operator_id": operator_id,
        "site_id": site_id,
        "candidate_model": candidate_model,
        "target": result.target,
        "horizon": result.horizon,
        "quantiles": ",".join(str(q) for q in result.quantiles),
        "n_folds": int(frame["fold"].nunique()),
    }
    if extra_params:
        params.update(dict(extra_params))
    # Drop params that were never set, so MLflow does not record "None".
    params = {k: v for k, v in params.items() if v is not None}

    metrics: dict[str, float] = {}
    per_model_pinball: dict[str, float] = {}
    for model in models:
        rows = frame[frame["model"] == model]
        for metric in ("pinball", "mae", "rmse", "coverage"):
            value = _mean(rows[metric].tolist())
            if value is not None:
                metrics[f"{model}.{metric}"] = value
                if metric == "pinball":
                    per_model_pinball[model] = value

    candidate_pinball = per_model_pinball.get(candidate_model)
    baseline_pinball = per_model_pinball.get(BASELINE_MODEL)
    if (
        candidate_pinball is not None
        and baseline_pinball is not None
        and baseline_pinball > 0.0
        and candidate_model != BASELINE_MODEL
    ):
        metrics["pinball_skill_vs_baseline"] = 1.0 - candidate_pinball / baseline_pinball
        metrics["beats_baseline"] = 1.0 if candidate_pinball < baseline_pinball else 0.0

    return BacktestSummary(params=params, metrics=metrics)


class Tracker(ABC):
    """Minimal experiment tracking interface used across the system."""

    @abstractmethod
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
        """Log one backtest comparison. Returns the run id, or None if disabled."""
        ...

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """Whether this tracker actually records anything."""
        ...


class NoOpTracker(Tracker):
    """Tracker that records nothing. The default when tracking is not configured.

    Callers can always call log_backtest without branching on configuration.
    """

    @property
    def enabled(self) -> bool:
        return False

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
        return None


class MlflowTracker(Tracker):
    """Logs backtests to MLflow.

    MLflow is imported lazily so importing this module never requires the
    optional "tracking" extra. Pass a URI (or set VATICORE_MLFLOW_TRACKING_URI)
    to choose the backend, for example sqlite:///mlflow.db for a local store or
    http://host:5000 for a tracking server. A missing URI falls back to MLflow's
    own default.
    """

    def __init__(
        self,
        tracking_uri: str | None = None,
        experiment: str = "vaticore-backtests",
    ) -> None:
        try:
            import mlflow
        except ImportError as exc:  # pragma: no cover - exercised via get_tracker
            raise ImportError(
                "mlflow is required for MlflowTracker. Install the tracking extra, "
                "for example: uv pip install 'vaticore[tracking]'."
            ) from exc

        self._mlflow = mlflow
        if tracking_uri:
            mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(experiment)

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
        summary = summarize_backtest(
            result,
            candidate_model=candidate_model,
            operator_id=operator_id,
            site_id=site_id,
            extra_params=extra_params,
        )
        name = run_name or f"{candidate_model}-{result.target}"
        with self._mlflow.start_run(run_name=name) as run:
            self._mlflow.set_tag("candidate_model", candidate_model)
            self._mlflow.log_params(summary.params)
            self._mlflow.log_metrics(summary.metrics)
            # Per fold metrics as a step series, so calibration can be inspected
            # fold by fold, not just as a mean.
            for row in result.folds:
                self._mlflow.log_metrics(
                    {
                        f"fold.{row.model}.pinball": row.pinball,
                        f"fold.{row.model}.mae": row.mae,
                        f"fold.{row.model}.rmse": row.rmse,
                    },
                    step=row.fold,
                )
            return str(run.info.run_id)


def get_tracker(
    settings: Settings | None = None,
    *,
    experiment: str = "vaticore-backtests",
) -> Tracker:
    """Return an MlflowTracker when tracking is configured, else a NoOpTracker.

    Tracking is considered configured when a tracking URI is set (via Settings or
    the VATICORE_MLFLOW_TRACKING_URI environment variable). If mlflow is not
    installed, this degrades to a NoOpTracker rather than failing the pipeline.
    """
    settings = settings or get_settings()
    if not settings.mlflow_tracking_uri:
        return NoOpTracker()
    try:
        return MlflowTracker(
            tracking_uri=settings.mlflow_tracking_uri,
            experiment=experiment,
        )
    except ImportError:
        return NoOpTracker()
