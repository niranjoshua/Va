from __future__ import annotations

import pytest

pytestmark = pytest.mark.filterwarnings("ignore")

# Skip the whole module cleanly if the service extra is not installed.
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from vaticore.api.main import create_app, get_fleet  # noqa: E402
from vaticore.datasets import make_synthetic_fleet  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    # Small, fast demo fleet for tests instead of the default 90 day one.
    app.dependency_overrides[get_fleet] = lambda: make_synthetic_fleet(days=20, seed=1)
    return TestClient(app)


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_models(client: TestClient) -> None:
    resp = client.get("/models")
    assert resp.status_code == 200
    assert "quantile_gbm" in resp.json()


def test_sites(client: TestClient) -> None:
    resp = client.get("/sites")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 3
    assert {s["operator_id"] for s in body} == {"lagos-energy", "accra-power"}


def test_forecast_returns_quantile_points(client: TestClient) -> None:
    resp = client.post(
        "/forecast",
        json={
            "operator_id": "lagos-energy",
            "site_id": "ikeja-minigrid",
            "target": "load_kw",
            "horizon": 12,
            "model": "persistence",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["points"]) == 12
    first = body["points"][0]["quantiles"]
    assert first["q0.1"] <= first["q0.5"] <= first["q0.9"]


def test_forecast_unknown_site_404(client: TestClient) -> None:
    resp = client.post(
        "/forecast",
        json={"operator_id": "nope", "site_id": "nope", "model": "persistence"},
    )
    assert resp.status_code == 404


def test_forecast_bad_target_400(client: TestClient) -> None:
    resp = client.post(
        "/forecast",
        json={
            "operator_id": "lagos-energy",
            "site_id": "ikeja-minigrid",
            "target": "not_a_target",
            "model": "persistence",
        },
    )
    assert resp.status_code == 400


def test_advisory(client: TestClient) -> None:
    resp = client.post(
        "/advisory",
        json={
            "operator_id": "lagos-energy",
            "site_id": "ikeja-minigrid",
            "horizon": 24,
            "usable_battery_kwh": 500.0,
            "model": "persistence",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["horizon_hours"] == 24
    assert "genset_recommended" in body


def test_plan_returns_an_hourly_advisory_schedule(client: TestClient) -> None:
    resp = client.post(
        "/plan",
        json={
            "operator_id": "lagos-energy",
            "site_id": "ikeja-minigrid",
            "battery_kwh": 600,
            "battery_power_kw": 150,
            "genset_kw": 100,
            "soc_kwh": 300,
            "model": "persistence",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["advisory_only"] is True
    assert len(body["hours"]) == 24
    assert isinstance(body["summary"], str) and body["summary"]
    on_hours = sum(h["genset_on"] for h in body["hours"])
    assert body["expected_genset_hours"] <= on_hours
    assert body["expected_genset_starts"] == len(body["run_windows"])


def test_plan_rejects_a_battery_charge_outside_its_capacity(client: TestClient) -> None:
    resp = client.post(
        "/plan",
        json={
            "operator_id": "lagos-energy",
            "site_id": "ikeja-minigrid",
            "battery_kwh": 100,
            "battery_power_kw": 50,
            "genset_kw": 50,
            "soc_kwh": 500,
            "model": "persistence",
        },
    )
    assert resp.status_code == 400


def test_plan_speaks_the_site_clock_and_rejects_unknown_timezones(client: TestClient) -> None:
    request = {
        "operator_id": "lagos-energy",
        "site_id": "ikeja-minigrid",
        "battery_kwh": 600,
        "battery_power_kw": 150,
        "genset_kw": 100,
        "soc_kwh": 300,
        "model": "persistence",
        "grid_kw": 200,
        "assume_grid_always_on": True,
        "timezone": "Africa/Lagos",
    }
    resp = client.post("/plan", json=request)
    assert resp.status_code == 200, resp.text
    assert resp.json()["grid_windows"] == ["all day"]
    assert client.post("/plan", json={**request, "timezone": "Mars/Olympus"}).status_code == 422


def test_plan_for_a_site_on_a_reliable_grid(client: TestClient) -> None:
    resp = client.post(
        "/plan",
        json={
            "operator_id": "lagos-energy",
            "site_id": "ikeja-minigrid",
            "battery_kwh": 600,
            "battery_power_kw": 150,
            "genset_kw": 100,
            "soc_kwh": 300,
            "model": "persistence",
            "grid_kw": 200,
            "assume_grid_always_on": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["grid_windows"] == ["all day"]
    assert body["expected_genset_hours"] == 0
    assert all(h["planned_grid_on"] for h in body["hours"])


# -- daily pipeline endpoints ----------------------------------------------------


def _pipeline_client(**settings: object) -> tuple[TestClient, object]:
    import hashlib  # noqa: F401

    from vaticore.api.main import get_app_settings, get_plan_store
    from vaticore.config import Settings
    from vaticore.pipeline.store import PlanStore

    store = PlanStore("duckdb:///:memory:")
    app = create_app()
    app.dependency_overrides[get_app_settings] = lambda: Settings(**settings)  # type: ignore[arg-type]
    app.dependency_overrides[get_plan_store] = lambda: store
    return TestClient(app), store


def _signed(secret: str, body: bytes) -> dict[str, str]:
    import hashlib
    import hmac

    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"x-hub-signature-256": f"sha256={digest}", "content-type": "application/json"}


def test_whatsapp_webhook_verification() -> None:
    client, _ = _pipeline_client(whatsapp_verify_token="word")
    query = {"hub.mode": "subscribe", "hub.verify_token": "word", "hub.challenge": "12345"}
    ok = client.get("/webhooks/whatsapp", params=query)
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/webhooks/whatsapp", params={**query, "hub.verify_token": "nope"})
    assert bad.status_code == 403


def test_whatsapp_webhook_events_must_be_signed() -> None:
    import json

    body = json.dumps({"entry": []}).encode()
    unsigned, _ = _pipeline_client()
    assert unsigned.post("/webhooks/whatsapp", content=body).status_code == 503

    client, _ = _pipeline_client(whatsapp_app_secret="s3cret")
    forged = client.post("/webhooks/whatsapp", content=body, headers=_signed("wrong", body))
    assert forged.status_code == 401
    good = client.post("/webhooks/whatsapp", content=body, headers=_signed("s3cret", body))
    assert good.status_code == 200 and good.json()["replies"] == 0


def test_a_stop_reply_through_the_webhook_opts_out() -> None:
    import json

    from vaticore.delivery.recipients import recipient_hash

    client, store = _pipeline_client(whatsapp_app_secret="s3cret")
    message = {
        "id": "wamid.in",
        "from": "2348000000001",
        "type": "text",
        "text": {"body": "STOP"},
        "timestamp": "1773120000",
    }
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [message]}}]}]}).encode()
    resp = client.post("/webhooks/whatsapp", content=body, headers=_signed("s3cret", body))
    assert resp.status_code == 200 and resp.json()["opt_outs"] == 1
    assert store.is_opted_out(recipient_hash("+2348000000001"))  # type: ignore[attr-defined]


def test_stored_plans_and_scorecards_need_the_token() -> None:
    from datetime import UTC, date, datetime

    from vaticore.pipeline.store import RunRecord

    client, store = _pipeline_client(api_token="t0k")
    store.save_run(  # type: ignore[attr-defined]
        RunRecord(
            operator_id="op",
            site_id="s1",
            plan_date=date(2026, 3, 10),
            plan_start=datetime(2026, 3, 10, 5, tzinfo=UTC),
            plan_end=datetime(2026, 3, 11, 5, tzinfo=UTC),
            issued_at=datetime(2026, 3, 10, 5, tzinfo=UTC),
            status="no_plan",
            message='{"text": "no plan today", "fields": {}}',
        )
    )
    path = "/sites/op/s1/plans/2026-03-10"
    assert client.get(path).status_code == 401
    auth = {"Authorization": "Bearer t0k"}
    body = client.get(path, headers=auth).json()
    assert body["status"] == "no_plan" and body["message"] == "no plan today"
    assert body["advisory_only"] is True
    assert client.get("/sites/op/s1/plans/2026-03-11", headers=auth).status_code == 404
    card = client.get("/sites/op/s1/scorecard", headers=auth).json()
    assert card["days"] == 0 and "no scored days" in card["summary"]

    production, _ = _pipeline_client(environment="production")
    assert production.get(path).status_code == 401


def test_operator_keys_reach_only_their_own_sites() -> None:
    from datetime import UTC, datetime

    client, store = _pipeline_client(environment="production")
    key = store.create_api_key("op", "ops team", datetime.now(tz=UTC))  # type: ignore[attr-defined]
    other = store.create_api_key("rival", "x", datetime.now(tz=UTC))  # type: ignore[attr-defined]
    path = "/sites/op/s1/scorecard"
    assert client.get(path, headers={"Authorization": f"Bearer {key}"}).status_code == 200
    assert client.get(path, headers={"Authorization": f"Bearer {other}"}).status_code == 403
    assert client.get(path, headers={"Authorization": "Bearer vk_nope_nope"}).status_code == 401
    key_id = key.split("_")[1]
    store.revoke_api_key(key_id, datetime.now(tz=UTC))  # type: ignore[attr-defined]
    assert client.get(path, headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_readings_can_be_pushed_in() -> None:
    from vaticore.api.main import get_repository
    from vaticore.storage import DuckDBRepository

    client, _ = _pipeline_client(api_token="admin")
    repo = DuckDBRepository(":memory:")
    client.app.dependency_overrides[get_repository] = lambda: repo  # type: ignore[attr-defined]
    auth = {"Authorization": "Bearer admin"}
    readings = [
        {"timestamp": "2026-03-10T06:00:00+01:00", "load_kw": 3.2, "battery_soc_pct": 64},
        {"timestamp": "2026-03-10T07:00:00+01:00", "load_kw": 3.4, "grid_available": 0},
    ]
    resp = client.post("/ingest/op/s1", json={"readings": readings}, headers=auth)
    assert resp.status_code == 200 and resp.json() == {"rows": 2}
    stored = repo.read_history("op", "s1")
    assert str(stored["timestamp"].iloc[0]) == "2026-03-10 05:00:00+00:00"
    assert stored["battery_soc_pct"].iloc[0] == 64.0

    naive = [{"timestamp": "2026-03-10 08:00", "load_kw": 3.0}]
    assert client.post("/ingest/op/s1", json={"readings": naive}, headers=auth).status_code == 422
    local = {"readings": naive, "timezone": "Africa/Lagos"}
    assert client.post("/ingest/op/s1", json=local, headers=auth).json() == {"rows": 1}
    twice = {"readings": readings + readings[:1]}
    assert client.post("/ingest/op/s1", json=twice, headers=auth).status_code == 422
    bad = {"readings": [{"timestamp": "2026-03-10T09:00:00Z", "battery_soc_pct": 140}]}
    assert client.post("/ingest/op/s1", json=bad, headers=auth).status_code == 422


def test_site_health_endpoint() -> None:
    from vaticore.api.main import get_repository

    client, _ = _pipeline_client()
    client.app.dependency_overrides[get_repository] = lambda: _health_repo()  # type: ignore[attr-defined]
    body = client.get("/sites/lagos-energy/ikeja-minigrid/health?days=10").json()
    assert body["status"] in {"ok", "warn", "fail"}
    assert "load_kw" in body["coverage"] and "Data health" in body["summary"]


def _health_repo():  # type: ignore[no-untyped-def]
    import pandas as pd

    from vaticore.storage import DuckDBRepository

    repo = DuckDBRepository(":memory:")
    fleet = make_synthetic_fleet(days=12, seed=1)
    shift = pd.Timestamp.now(tz="UTC").normalize() - fleet["timestamp"].max().normalize()
    fleet["timestamp"] = fleet["timestamp"] + shift
    repo.upsert(fleet)
    return repo


def test_model_monitoring_endpoint() -> None:
    from datetime import UTC, date, datetime, timedelta

    from vaticore.pipeline.store import ScoreRecord

    client, store = _pipeline_client(api_token="admin")
    now = datetime.now(tz=UTC)
    for k in range(1, 4):
        detail = {
            "models": {
                "persistence (baseline)": {"role": "baseline", "pinball": 1.0,
                                           "coverage_80": 0.8, "hours": 24},
                "chronos_2+conformal": {"role": "primary", "pinball": 0.6,
                                        "coverage_80": 0.5, "hours": 24},
            }
        }  # fmt: skip
        day = date.today() - timedelta(days=k)
        store.save_score(  # type: ignore[attr-defined]
            ScoreRecord("op", "s1", day, now, 24, 0, 0.6, 0.5, 0, 0, 0, 0, 0, 0, 0, 0, 0, detail)
        )
    store.set_model_status("op", "s1", "chronos_2", "suspended", "range held 50%", now)  # type: ignore[attr-defined]
    resp = client.get("/sites/op/s1/models", headers={"Authorization": "Bearer admin"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["statuses"][0]["status"] == "suspended"
    chronos = [w for w in body["weekly"] if w["model"] == "chronos_2"]
    assert sum(w["days"] for w in chronos) == 3 and chronos[0]["range_held"] == 0.5
    assert client.get("/sites/op/s1/models").status_code == 401
