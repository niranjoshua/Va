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
    assert body["grid_windows"] == ["00:00 to midnight"]
    assert body["expected_genset_hours"] == 0
    assert all(h["planned_grid_on"] for h in body["hours"])
