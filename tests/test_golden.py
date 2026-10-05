"""Golden numbers: the code that makes the published results cannot shift silently.

Each scenario runs a real code path (the backtest harness, conformal
calibration, the value backtest, the planner, a full pipeline day with its
score, fuel reconciliation) on fixed synthetic data and compares every number
with tests/golden/<scenario>.json. The full studies need data that stays off
the repository and take an hour; these take seconds and exercise the same
code, so CI catches a change to any number the studies would report.

When a change to the numbers is intended (a better model, a fixed bug), make
it deliberate and visible:

    uv run pytest tests/test_golden.py --update-golden

then review the diff of tests/golden/*.json in the pull request, rerun the
affected studies (examples/verify_results.py) and update their results.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Callable
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from vaticore import engine
from vaticore.datasets import make_synthetic_fleet, make_synthetic_site
from vaticore.decisions.dispatch import SiteAssets, plan_dispatch
from vaticore.evaluation.regression import differences
from vaticore.schemas import FUEL_LEVEL_L, GENERATION_KW, GENSET_KW, LOAD_KW, TIMESTAMP

GOLDEN = Path(__file__).parent / "golden"
RTOL = 1e-6
ATOL = 1e-6

pytestmark = pytest.mark.filterwarnings("ignore")


def _quickstart_site() -> pd.DataFrame:
    fleet = make_synthetic_fleet(days=75, seed=3)
    return engine.select_site(fleet, "lagos-energy", "ikeja-minigrid")


def _summary(frame: pd.DataFrame) -> dict[str, dict[str, float]]:
    return {str(model): {k: float(v) for k, v in row.items()} for model, row in frame.iterrows()}


def scenario_quickstart_backtest() -> dict[str, Any]:
    """The README's demo numbers: quantile GBM against persistence, load and solar."""
    site = _quickstart_site()
    out: dict[str, Any] = {}
    for target in (LOAD_KW, GENERATION_KW):
        result = engine.run_backtest(
            site, target, horizon=24, initial=24 * 45, step=72, model="quantile_gbm"
        )
        summary = result.summary()
        out[target] = {
            "summary": _summary(summary),
            "improvement": float(
                1 - summary.loc["quantile_gbm", "pinball"] / summary.loc["persistence", "pinball"]
            ),
        }
    return out


def scenario_conformal_forecast() -> dict[str, Any]:
    """Calibrated day-ahead net load forecast, the planning model's live path."""
    site = make_synthetic_site("op", "s", days=40, seed=11, gap_fraction=0.02)
    out = {}
    for calibrate in (False, True):
        fc = engine.net_load_forecast(site, horizon=24, calibrate=calibrate)
        out["calibrated" if calibrate else "raw"] = {
            col: [round(float(v), 9) for v in fc[col]] for col in fc.columns
        }
    return out


def _assets() -> SiteAssets:
    return SiteAssets(
        battery_kwh=300.0,
        battery_power_kw=100.0,
        genset_kw=100.0,
        min_soc_kwh=30.0,
        genset_min_run_hours=2.0,
        diesel_price_per_l=1.1,
        value_of_lost_load_per_kwh=1.0,
    )


def scenario_value_backtest() -> dict[str, Any]:
    """The value backtest behind research notes 1 to 3, on a small synthetic mini-grid."""
    site = make_synthetic_site(
        "op", "s", days=70, seed=5, base_load_kw=60.0, solar_peak_kw=80.0, gap_fraction=0.0
    )
    result = engine.run_value_backtest(site, assets=_assets(), initial=24 * 42)
    report = result.value
    policies = {}
    for name, policy in report.results.items():
        row = {k: v for k, v in asdict(policy).items() if isinstance(v, int | float)}
        row["savings_vs_baseline"] = report.savings(name)
        share = report.share_of_possible(name)
        row["share_of_possible"] = share if share is not None else float("nan")
        policies[name] = row
    coverage = {
        model: next(float(i.observed) for i in cal.intervals if (i.lower, i.upper) == (0.1, 0.9))
        for model, cal in result.calibration.items()
    }
    return {"policies": policies, "coverage_80": coverage}


def scenario_dispatch_plan() -> dict[str, Any]:
    """The planner on a fixed net load day: runs, fuel, battery, minimum run time."""
    hours = pd.date_range("2026-03-09 23:00", periods=24, freq="h", tz="UTC")
    net = pd.Series(
        [40, 38, 36, 35, 35, 38, 45, 20, 0, -20, -35, -40, -38, -30, -10, 10, 50, 85, 95, 90,
         80, 70, 55, 45], index=hours, dtype=float,
    )  # fmt: skip
    grid = np.array([0] * 6 + [1] * 8 + [0] * 10, dtype=float)
    out = {}
    for name, kwargs in (("off_grid", {}), ("weak_grid", {"planned_grid_available": grid})):
        assets = _assets()
        if kwargs:
            assets = SiteAssets(**{**asdict(assets), "grid_kw": 60.0, "grid_price_per_kwh": 0.2})
        plan = plan_dispatch(net, soc_kwh=120.0, assets=assets, **kwargs)  # type: ignore[arg-type]
        out[name] = {
            "genset_on": [bool(x) for x in plan.genset_on],
            "fuel_l": plan.expected.fuel_l,
            "genset_hours": plan.expected.genset_hours,
            "genset_starts": plan.expected.genset_starts,
            "unserved_kwh": plan.expected.unserved_kwh,
            "soc_end_kwh": plan.expected.soc_end_kwh,
            "summary": plan.summary(),
        }
    return out


def scenario_pipeline_day(tmp_path: Path) -> dict[str, Any]:
    """A full pipeline day: plan issued the evening before, then scored."""
    from vaticore.pipeline.demo import synthetic_history
    from vaticore.pipeline.runner import PipelineConfig, plan_window, run_site
    from vaticore.pipeline.scoring import score_day
    from vaticore.pipeline.store import PlanStore
    from vaticore.sites import Site
    from vaticore.storage import DuckDBRepository

    site = Site(
        operator_id="op", site_id="tower", name="Tower", site_type="telecom_tower",
        latitude=7.8, longitude=6.7, timezone="Africa/Lagos", currency="NGN",
        value_of_lost_load_per_kwh=5000.0,
        battery={"usable_kwh": 30.0, "power_kw": 15.0, "min_soc_kwh": 6.0},
        solar={"kwp": 12.0},
        generator={"rated_kw": 16.0, "fuel_price_per_l": 1250.0, "min_run_hours": 2},
    )  # fmt: skip
    day = date(2026, 3, 10)
    start, end = plan_window(site, day)
    repo = DuckDBRepository(":memory:")
    repo.upsert(synthetic_history(site, start + pd.Timedelta(days=2), 90, seed=3))
    store = PlanStore("duckdb:///:memory:")
    issued = (start - pd.Timedelta(hours=6)).to_pydatetime()
    config = PipelineConfig(model_order=(engine.PLANNING_MODEL,), shadow=False, monitor=None)
    run = run_site(site, repo, store, plan_date=day, config=config, now=issued)
    record = score_day(site, repo, store, day, now=(end + pd.Timedelta(days=1)).to_pydatetime())
    assert run.plan is not None and record is not None
    return {
        "model": run.model,
        "genset_on": [bool(x) for x in run.plan.genset_on],
        "expected_fuel_l": run.plan.expected.fuel_l,
        "message": run.message.text if run.message else None,
        "score": {
            k: v
            for k, v in asdict(record).items()
            if isinstance(v, int | float) and not isinstance(v, bool)
        },
    }


def scenario_fuel_reconciliation() -> dict[str, Any]:
    from vaticore.fuel import reconcile
    from vaticore.sites import Site

    site = Site(
        operator_id="op", site_id="s1", name="Site", site_type="telecom_tower",
        latitude=6.5, longitude=3.4, timezone="Africa/Lagos", currency="NGN",
        value_of_lost_load_per_kwh=5000.0, battery={"usable_kwh": 20.0, "power_kw": 10.0},
        generator={"rated_kw": 20.0, "fuel_price_per_l": 1000.0, "tank_l": 500.0},
    )  # fmt: skip
    start = pd.Timestamp("2026-03-01 23:00", tz="UTC")
    hours = pd.date_range(start, periods=24 * 7, freq="h")
    rng = np.random.default_rng(2)
    local = hours.tz_convert("Africa/Lagos")
    output = np.where((local.hour >= 18) & (local.hour < 23), 12.0, 0.0)
    burn = np.where(output > 0, 0.08145 * 20 + 0.246 * output, 0.0)
    level = 400 - np.concatenate([[0.0], np.cumsum(burn)[:-1]])
    level[60:] += 170.0  # a 200 L delivery that put only 170 L in the tank
    level[130:] -= 25.0  # taken at night
    readings = pd.DataFrame(
        {TIMESTAMP: hours, GENSET_KW: output, FUEL_LEVEL_L: level + rng.normal(0, 1.0, len(hours))}
    )
    deliveries = pd.DataFrame(
        {"delivered_at": [hours[60]], "litres": [200.0], "reference": ["INV-1"]}
    )
    report = reconcile(
        site, readings, deliveries, start=start, end=hours[-1] + pd.Timedelta(hours=1)
    )
    return {
        "delivered_l": report.delivered_l,
        "burned_l": report.burned_l,
        "withdrawn_l": report.withdrawn_l,
        "findings": [
            {"code": f.code, "severity": f.severity.value, "litres": f.litres}
            for f in report.findings
        ],
    }


SCENARIOS: dict[str, Callable[..., dict[str, Any]]] = {
    "quickstart_backtest": scenario_quickstart_backtest,
    "conformal_forecast": scenario_conformal_forecast,
    "value_backtest": scenario_value_backtest,
    "dispatch_plan": scenario_dispatch_plan,
    "pipeline_day": scenario_pipeline_day,
    "fuel_reconciliation": scenario_fuel_reconciliation,
}


def _differences(expected: Any, actual: Any) -> list[str]:
    return differences(expected, actual, rtol=RTOL, atol=ATOL)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=float, allow_nan=True))


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_golden(name: str, update_golden: bool, tmp_path: Path) -> None:
    scenario = SCENARIOS[name]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        takes_path = "tmp_path" in scenario.__code__.co_varnames[: scenario.__code__.co_argcount]
        actual = _jsonable(scenario(tmp_path) if takes_path else scenario())
    path = GOLDEN / f"{name}.json"
    if update_golden or not path.exists():
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(json.dumps(actual, indent=2, sort_keys=True, allow_nan=True) + "\n")
        if not update_golden:
            pytest.fail(f"wrote new golden file {path.name}; review and commit it")
        return
    expected = json.loads(path.read_text())
    problems = _differences(expected, actual)
    assert not problems, (
        f"{name}: {len(problems)} number(s) changed. If intended, run "
        "`uv run pytest tests/test_golden.py --update-golden` and explain the change "
        "in the pull request.\n" + "\n".join(problems[:40])
    )


def test_the_readme_quotes_the_demo_numbers() -> None:
    """'cuts pinball loss by **43% on load** and **17% on solar'."""
    golden = json.loads((GOLDEN / "quickstart_backtest.json").read_text())
    load = round(100 * golden[LOAD_KW]["improvement"])
    solar = round(100 * golden[GENERATION_KW]["improvement"])
    readme = " ".join((Path(__file__).resolve().parents[1] / "README.md").read_text().split())
    assert f"**{load}% on load** and **{solar}% on solar" in readme


def test_the_comparison_catches_small_changes() -> None:
    assert _differences({"a": 1.0}, {"a": 1.0 + 1e-9}) == []
    assert _differences({"a": 1.0}, {"a": 1.001}) != []
    assert _differences({"a": [True, False]}, {"a": [True, True]}) != []
    assert _differences({"a": 1.0}, {"a": 1.0, "b": 2.0}) == ["/b: new"]
