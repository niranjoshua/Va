"""The plan store on DuckDB, and on Postgres when VATICORE_TEST_POSTGRES_URL is set."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pandas as pd
import pytest

from vaticore.pipeline.store import PlanStore, RunRecord, ScoreRecord

_PG = os.environ.get("VATICORE_TEST_POSTGRES_URL")
DAY = date(2026, 3, 10)
START = datetime(2026, 3, 10, 5, tzinfo=UTC)
END = datetime(2026, 3, 11, 5, tzinfo=UTC)


@pytest.fixture(params=["duckdb", "postgres"])
def store(request: pytest.FixtureRequest) -> Iterator[PlanStore]:
    if request.param == "duckdb":
        plan_store = PlanStore("duckdb:///:memory:")
        yield plan_store
        plan_store.close()
        return
    if not _PG:
        pytest.skip("set VATICORE_TEST_POSTGRES_URL to run against Postgres")
    pytest.importorskip("psycopg")
    import psycopg

    schema = f"test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(_PG, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    url = _PG + ("&" if "?" in _PG else "?") + f"options=-csearch_path%3D{schema}"
    plan_store = PlanStore(url)
    yield plan_store
    plan_store.close()
    with psycopg.connect(_PG, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


def _run(**overrides: object) -> RunRecord:
    base: dict[str, object] = {
        "operator_id": "op",
        "site_id": "s1",
        "plan_date": DAY,
        "plan_start": START,
        "plan_end": END,
        "issued_at": START,
        "status": "planned",
        "model": "persistence",
        "health": {"status": "ok", "issues": []},
        "soc_start_kwh": 15.0,
        "soc_assumed": True,
        "expected_fuel_l": 7.5,
        "expected_genset_starts": 2,
        "assets": {"battery_kwh": 30.0},
    }
    base.update(overrides)
    return RunRecord(**base)  # type: ignore[arg-type]


def _hours(n: int = 24) -> tuple[pd.DataFrame, pd.DataFrame]:
    stamps = pd.date_range(START, periods=n, freq="h")
    forecasts = pd.concat(
        [
            pd.DataFrame(
                {"model": m, "role": r, "timestamp": stamps, "q10": 1.0, "q50": 2.0, "q90": 3.0}
            )
            for m, r in (("persistence", "primary"), ("persistence", "baseline"))
        ],
        ignore_index=True,
    )
    plan = pd.DataFrame(
        {
            "timestamp": stamps,
            "planned_net_load_kw": 3.0,
            "genset_on": [i % 2 == 0 for i in range(n)],
            "grid_on": False,
            "expected_soc_kwh": 10.0,
            "baseline_genset_on": True,
        }
    )
    return forecasts, plan


def test_runs_round_trip_and_reruns_replace(store: PlanStore) -> None:
    forecasts, plan = _hours()
    store.save_run(_run(), forecasts=forecasts, plan=plan)
    store.save_run(_run(model="other"), forecasts=forecasts.head(4), plan=plan.head(4))

    run = store.get_run("op", "s1", DAY)
    assert run is not None
    assert run.model == "other" and run.plan_date == DAY and run.plan_start == START
    assert run.health == {"status": "ok", "issues": []} and run.soc_assumed is True
    assert run.expected_genset_starts == 2
    assert len(store.plan_hours("op", "s1", DAY)) == 4
    assert len(store.forecasts("op", "s1", DAY)) == 4
    assert store.get_run("op", "other-site", DAY) is None
    assert len(store.runs(operator_id="op")) == 1


def test_finished_days_wait_for_scoring(store: PlanStore) -> None:
    store.save_run(_run())
    store.save_run(_run(site_id="s2", status="no_plan"))
    assert store.unscored(START) == []
    assert store.unscored(END) == [("op", "s1", DAY)]
    store.save_score(
        ScoreRecord(
            operator_id="op",
            site_id="s1",
            plan_date=DAY,
            scored_at=END,
            hours_scored=24,
            hours_missing=0,
            pinball_primary=0.4,
            coverage_primary=0.8,
            fuel_plan_l=10.0,
            fuel_baseline_l=12.0,
            fuel_perfect_l=9.0,
            unserved_plan_kwh=0.0,
            unserved_baseline_kwh=3.0,
            unserved_perfect_kwh=0.0,
            cost_plan=1.0,
            cost_baseline=2.0,
            cost_perfect=0.5,
            detail={"models": {}},
        )
    )
    assert store.unscored(END) == []
    scores = store.scores("op", "s1")
    assert len(scores) == 1 and float(scores["fuel_baseline_l"].iloc[0]) == 12.0


def test_deliveries_preferences_and_feedback(store: PlanStore) -> None:
    common = {
        "operator_id": "op",
        "site_id": "s1",
        "plan_date": DAY,
        "channel": "whatsapp",
        "recipient_hash": "h1",
        "recipient_masked": "+234******0001",
    }
    store.record_delivery(
        **common, status="failed", provider_message_id=None, error="x", attempts=3, at=START
    )  # type: ignore[arg-type]
    assert store.delivery_status("op", "s1", DAY, "whatsapp", "h1") == "failed"
    assert store.last_delivery_to("h1") is None
    store.record_delivery(
        **common, status="sent", provider_message_id="wamid.1", error=None, attempts=1, at=END
    )  # type: ignore[arg-type]
    assert store.last_delivery_to("h1") == ("op", "s1", DAY)
    assert store.update_delivery_status("wamid.1", "delivered", END)
    assert store.update_delivery_status("wamid.1", "sent", END)  # ignored: older state
    assert store.delivery_status("op", "s1", DAY, "whatsapp", "h1") == "delivered"
    assert not store.update_delivery_status("wamid.unknown", "read", END)

    assert not store.is_opted_out("h1")
    store.set_opt_out("h1", True, "reply", END)
    assert store.is_opted_out("h1")

    first = store.record_feedback(
        provider_message_id="in.1",
        received_at=END,
        recipient_hash="h1",
        kind="followed",
        text="1",
        plan=("op", "s1", DAY),
    )
    again = store.record_feedback(
        provider_message_id="in.1",
        received_at=END,
        recipient_hash="h1",
        kind="followed",
        text="1",
        plan=("op", "s1", DAY),
    )
    assert first and not again
    assert store.feedback("op")["kind"].tolist() == ["followed"]


def test_operator_api_keys(store: PlanStore) -> None:
    key = store.create_api_key("op", "ops laptop", START)
    assert key.startswith("vk_") and store.operator_for_key(key) == "op"
    assert store.operator_for_key(key + "x") is None
    assert store.operator_for_key("not a key") is None
    listed = store.api_keys()
    assert list(listed["operator_id"]) == ["op"] and key not in listed.to_string()
    key_id = str(listed["key_id"].iloc[0])
    assert store.revoke_api_key(key_id, END)
    assert store.operator_for_key(key) is None
    assert not store.revoke_api_key("nope", END)
