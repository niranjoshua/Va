"""Log a backtest to MLflow.

Runs a rolling origin backtest of the quantile GBM against persistence and logs
the comparison (site, model, config and metrics) to MLflow, so the result is
reproducible for a pilot or the research paper.

Tracking is off unless a tracking URI is configured. To log locally into a
SQLite store and install the extra:

    uv sync --extra tracking
    VATICORE_MLFLOW_TRACKING_URI=sqlite:///mlflow.db uv run python examples/track_backtest.py
    uv run --extra tracking mlflow ui --backend-store-uri sqlite:///mlflow.db

Without the URI (or the extra) this still runs; it just uses a no-op tracker and
logs nothing, so the pipeline never depends on tracking being available.
"""

from __future__ import annotations

import warnings

from vaticore import engine
from vaticore.config import get_settings
from vaticore.datasets import make_synthetic_fleet
from vaticore.schemas import LOAD_KW
from vaticore.tracking import get_tracker

OPERATOR_ID, SITE_ID = "lagos-energy", "ikeja-minigrid"


def main() -> None:
    warnings.filterwarnings("ignore")

    settings = get_settings()
    tracker = get_tracker(settings)
    if tracker.enabled:
        print(f"Logging to MLflow at {settings.mlflow_tracking_uri}")
    else:
        print(
            "Tracking disabled (no VATICORE_MLFLOW_TRACKING_URI or mlflow not installed). "
            "Running the backtest without logging."
        )

    fleet = make_synthetic_fleet(days=75, seed=3)
    site = engine.select_site(fleet, OPERATOR_ID, SITE_ID)
    print(f"Site history: {len(site)} hourly rows")

    print("\nBacktesting quantile GBM against persistence...")
    result = engine.run_backtest(
        site,
        LOAD_KW,
        horizon=24,
        initial=24 * 45,
        step=72,
        model="quantile_gbm",
        tracker=tracker,
        operator_id=OPERATOR_ID,
        site_id=SITE_ID,
    )
    summary = result.summary()
    print(summary.round(3).to_string())

    gbm = summary.loc["quantile_gbm", "pinball"]
    base = summary.loc["persistence", "pinball"]
    print(f"\nPinball loss improvement over baseline: {(1 - gbm / base) * 100:.1f}%")
    if tracker.enabled:
        print(
            "Logged to MLflow. Browse it with: "
            f"uv run --extra tracking mlflow ui --backend-store-uri {settings.mlflow_tracking_uri}"
        )


if __name__ == "__main__":
    main()
