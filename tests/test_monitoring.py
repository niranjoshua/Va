"""Model monitoring: suspension, recovery, promotion, and the fallback it drives."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from vaticore.pipeline import monitoring, runner
from vaticore.pipeline.demo import synthetic_history
from vaticore.pipeline.monitoring import OK, SUSPENDED, assess, check_site
from vaticore.pipeline.runner import PipelineConfig, plan_window, run_site
from vaticore.pipeline.store import PlanStore, ScoreRecord
from vaticore.sites import Site
from vaticore.storage import DuckDBRepository

pytestmark = pytest.mark.filterwarnings("ignore")

GBM = runner.GBM
CHRONOS = runner.CHRONOS
WEATHER = runner.WEATHER
DAY = date(2026, 3, 30)
NOW = datetime(2026, 3, 30, 4, 40, tzinfo=UTC)


@pytest.fixture
def store() -> PlanStore:
    return PlanStore("duckdb:///:memory:")


def _score(
    store: PlanStore, day: date, models: dict[str, tuple[float, float]], hours: int = 24
) -> None:
    """Store one scored day: {model name: (pinball, range held)}."""
    detail = {
        "models": {
            name: {
                "role": "baseline" if "baseline" in name else "primary",
                "pinball": pinball,
                "mae": pinball,
                "coverage_80": held,
                "hours": hours,
            }
            for name, (pinball, held) in models.items()
        }
    }
    store.save_score(
        ScoreRecord("op", "s1", day, NOW, hours, 0, None, None, 0, 0, 0, 0, 0, 0, 0, 0, 0, detail)
    )


def _history(store: PlanStore, days: int, **models: tuple[float, float]) -> None:
    for k in range(days):
        day = DAY - timedelta(days=k + 1)
        _score(
            store,
            day,
            {
                "persistence (baseline)": (1.0, 0.8),
                **{f"{m}+conformal": v for m, v in models.items()},
            },
        )


def test_a_drifted_range_suspends_and_persistence_never_is(store: PlanStore) -> None:
    _history(store, 10, **{GBM: (0.7, 0.55), CHRONOS: (0.6, 0.81)})
    health = assess(store, "op", "s1", DAY)
    assert health[GBM].status == SUSPENDED and "range held only 55%" in (health[GBM].reason or "")
    assert health[CHRONOS].status == OK and health[CHRONOS].skill == pytest.approx(0.4)
    assert health["persistence"].status == OK
    assert health[GBM].hours == 240 and health[GBM].days == 10


def test_falling_behind_persistence_suspends(store: PlanStore) -> None:
    _history(store, 10, **{GBM: (1.2, 0.8)})
    health = assess(store, "op", "s1", DAY)
    assert health[GBM].status == SUSPENDED and "20% worse than persistence" in (
        health[GBM].reason or ""
    )


def test_too_little_evidence_is_not_judged(store: PlanStore) -> None:
    _history(store, 3, **{GBM: (3.0, 0.1)})
    assert assess(store, "op", "s1", DAY)[GBM].status == monitoring.LEARNING


def test_only_days_before_the_plan_count(store: PlanStore) -> None:
    _history(store, 10, **{GBM: (0.7, 0.8)})
    for k in range(10):  # a bad run on and after the plan day must not be seen
        _score(store, DAY + timedelta(days=k), {f"{GBM}+conformal": (5.0, 0.1)})
    assert assess(store, "op", "s1", DAY)[GBM].status == OK


def test_suspension_holds_until_the_model_is_clearly_back(store: PlanStore) -> None:
    _history(store, 10, **{GBM: (0.7, 0.55)})
    _, changes = check_site(store, "op", "s1", DAY, NOW)
    assert [(c.model, c.old, c.new) for c in changes] == [(GBM, None, SUSPENDED)]
    assert "suspended" in changes[0].describe()
    assert store.model_statuses("op", "s1")[GBM]["status"] == SUSPENDED

    # Next fortnight: range 68%. Good enough not to suspend, not enough to return.
    later = DAY + timedelta(days=14)
    for k in range(14):
        _score(store, later - timedelta(days=k + 1), {
            "persistence (baseline)": (1.0, 0.8), f"{GBM}+conformal": (0.7, 0.68)})  # fmt: skip
    _, changes = check_site(store, "op", "s1", later, NOW)
    assert changes == [] and store.model_statuses("op", "s1")[GBM]["status"] == SUSPENDED

    # Then back at 80% and better than persistence: reinstated.
    latest = later + timedelta(days=14)
    for k in range(14):
        _score(store, latest - timedelta(days=k + 1), {
            "persistence (baseline)": (1.0, 0.8), f"{GBM}+conformal": (0.7, 0.8)})  # fmt: skip
    _, changes = check_site(store, "op", "s1", latest, NOW)
    assert [(c.old, c.new) for c in changes] == [(SUSPENDED, OK)]
    assert "reinstated" in changes[0].describe()


def test_choose_order_drops_suspended_and_promotes_a_proven_challenger(store: PlanStore) -> None:
    _history(store, 14, **{GBM: (0.8, 0.8), CHRONOS: (0.7, 0.5), WEATHER: (0.6, 0.79)})
    health = assess(store, "op", "s1", DAY)
    order, notes = monitoring.choose_order([GBM, CHRONOS], [WEATHER], health)
    assert order == [WEATHER, GBM]
    assert any("chronos_2 suspended" in n for n in notes)
    assert any(f"{WEATHER} promoted" in n for n in notes)

    # Not promoted without a clear margin (5%) over the default model.
    close = assess(store, "op", "s1", DAY)
    close[WEATHER] = monitoring.ModelHealth(WEATHER, OK, 336, 14, 0.8, 0.78, 1.0)
    order, _ = monitoring.choose_order([GBM, CHRONOS], [WEATHER], close)
    assert order == [GBM, WEATHER]


def test_weekly_report(store: PlanStore) -> None:
    today = pd.Timestamp.now(tz="UTC").date()
    for k in range(10):
        _score(store, today - timedelta(days=k + 1), {
            "persistence (baseline)": (1.0, 0.8), f"{GBM}+conformal": (0.8, 0.75)})  # fmt: skip
    report = monitoring.weekly_report(store, "op", "s1", weeks=4)
    assert set(report["model"]) == {GBM, "persistence"}
    gbm = report[report["model"] == GBM]
    assert gbm["days"].sum() == 10 and (gbm["skill"].round(6) == 0.2).all()


# -- in the daily run -------------------------------------------------------------


def _site() -> Site:
    return Site(
        operator_id="op", site_id="s1", name="Site One", site_type="telecom_tower",
        latitude=7.8, longitude=6.7, timezone="Africa/Lagos", currency="NGN",
        value_of_lost_load_per_kwh=5000.0,
        battery={"usable_kwh": 30.0, "power_kw": 15.0, "min_soc_kwh": 6.0},
        solar={"kwp": 12.0}, generator={"rated_kw": 16.0, "fuel_price_per_l": 1250.0},
    )  # fmt: skip


def test_a_suspended_model_is_skipped_and_ops_are_alerted(
    store: PlanStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _site()
    repo = DuckDBRepository(":memory:")
    start, _ = plan_window(site, DAY)
    repo.upsert(synthetic_history(site, start, 90, seed=3))
    _history(store, 14, **{GBM: (1.5, 0.4)})

    tried: list[str] = []
    original = runner.engine.net_load_forecast

    def spy(history: pd.DataFrame, **kwargs: object) -> pd.DataFrame:
        tried.append(str(kwargs["model"]))
        if kwargs["model"] == CHRONOS:
            raise RuntimeError("not installed here")
        return original(history, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner.engine, "net_load_forecast", spy)
    run = run_site(site, repo, store, plan_date=DAY, config=PipelineConfig(shadow=False), now=NOW)
    assert run.status == "planned" and run.model == "persistence"
    assert GBM not in tried[: tried.index("persistence")]
    assert any("quantile_gbm_day_ahead suspended by monitoring" in n for n in run.fallback)
    assert run.alerts and "suspended" in run.alerts[0]
    stored = store.get_run("op", "s1", DAY)
    assert stored is not None and "suspended" in (stored.fallback or "")

    # The alert is raised once, on the change, not every day.
    again = run_site(site, repo, store, plan_date=DAY, config=PipelineConfig(shadow=False), now=NOW)
    assert again.alerts == []


def test_monitoring_can_be_turned_off(store: PlanStore) -> None:
    site = _site()
    repo = DuckDBRepository(":memory:")
    start, _ = plan_window(site, DAY)
    repo.upsert(synthetic_history(site, start, 90, seed=3))
    _history(store, 14, **{GBM: (1.5, 0.4)})
    config = PipelineConfig(shadow=False, monitor=None)
    run = run_site(site, repo, store, plan_date=DAY, config=config, now=NOW)
    assert (run.model or "").startswith(GBM) and run.alerts == []
    assert store.model_statuses("op", "s1") == {}
