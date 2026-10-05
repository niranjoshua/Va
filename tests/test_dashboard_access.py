"""Dashboard sign-in: each operator sees only its own sites."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from vaticore import access
from vaticore.config import Settings
from vaticore.dashboard import data
from vaticore.datasets import make_synthetic_site
from vaticore.pipeline.store import PlanStore
from vaticore.storage import DuckDBRepository

NOW = datetime(2026, 10, 5, tzinfo=UTC)
PROD = Settings(environment="production", api_token="admin-secret")  # type: ignore[arg-type]


@pytest.fixture
def store() -> PlanStore:
    return PlanStore("duckdb:///:memory:")


def _repo() -> DuckDBRepository:
    repo = DuckDBRepository(":memory:")
    for operator, site in (("tower-co", "t1"), ("tower-co", "t2"), ("bank", "b1")):
        frame = make_synthetic_site(operator, site, days=3, seed=1, gap_fraction=0.0)
        shift = pd.Timestamp.now(tz="UTC").floor("h") - frame["timestamp"].max()
        frame["timestamp"] = frame["timestamp"] + shift
        repo.upsert(frame)
    return repo


def test_credentials_resolve_to_one_operator_or_the_admin(store: PlanStore) -> None:
    key = store.create_api_key("tower-co", "ops", NOW)
    assert access.resolve(key, PROD, store) == access.Access("tower-co")
    assert access.resolve(f"Bearer {key}", PROD, store) == access.Access("tower-co")
    assert access.resolve("admin-secret", PROD, store) == access.Access(None)
    assert access.resolve("", PROD, store) is None
    assert access.resolve("vk_nope_nope", PROD, store) is None
    store.revoke_api_key(key.split("_")[1], NOW)
    assert access.resolve(key, PROD, store) is None
    assert access.open_without_credentials(Settings(environment="local"))
    assert not access.open_without_credentials(Settings(environment="staging"))


def test_an_operator_sees_only_its_own_sites(store: PlanStore) -> None:
    repo = _repo()
    tower = access.Access("tower-co")
    assert data.visible_sites(PROD, tower, repo) == [("tower-co", "t1"), ("tower-co", "t2")]
    assert len(data.visible_sites(PROD, access.Access(None), repo)) == 3
    assert not data.site_history(PROD, tower, repo, "tower-co", "t1").empty
    with pytest.raises(PermissionError):
        data.site_history(PROD, tower, repo, "bank", "b1")


def test_guessing_keys_is_slowed_down(store: PlanStore) -> None:
    guard = data.SignInGuard(limit=3, lockout_s=60.0)
    for t in range(3):
        assert guard.attempt("wrong", PROD, store, now=float(t)) is None
    assert guard.locked(now=10.0)
    good = store.create_api_key("bank", "x", NOW)
    assert guard.attempt(good, PROD, store, now=10.0) is None  # locked, even with a good key
    assert guard.attempt(good, PROD, store, now=100.0) == access.Access("bank")


def test_the_dashboard_asks_for_a_key_and_scopes_the_sites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from streamlit.testing.v1 import AppTest

    readings = tmp_path / "readings.duckdb"
    plans = tmp_path / "plans.duckdb"
    repo = DuckDBRepository(str(readings))
    for operator, site in (("tower-co", "t1"), ("bank", "b1")):
        frame = make_synthetic_site(operator, site, days=60, seed=1, gap_fraction=0.0)
        shift = pd.Timestamp.now(tz="UTC").floor("h") - frame["timestamp"].max()
        frame["timestamp"] = frame["timestamp"] + shift
        repo.upsert(frame)
    repo.close()
    store = PlanStore(f"duckdb:///{plans}")
    key = store.create_api_key("tower-co", "ops", NOW)
    store.close()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VATICORE_ENVIRONMENT", "staging")
    monkeypatch.setenv("VATICORE_DATABASE_URL", f"duckdb:///{readings}")
    monkeypatch.setenv("VATICORE_PLAN_STORE_URL", f"duckdb:///{plans}")

    app_file = Path(__file__).resolve().parents[1] / "vaticore" / "dashboard" / "app.py"
    at = AppTest.from_file(str(app_file), default_timeout=300)
    at.run()
    assert at.text_input[0].label == "Access key" and not at.selectbox

    at.text_input[0].input("vk_wrong_key")
    at.button[0].click()
    at.run()
    assert "not recognised" in at.error[0].value

    at.text_input[0].input(key)
    at.button[0].click()
    at.run()
    assert not at.exception, at.exception
    assert at.selectbox[0].options == ["tower-co / t1"]  # the bank's site is not listed

    # The pilot view: this operator's verdict and savings, never another's.
    portfolio = tmp_path / "sites.toml"
    portfolio.write_text(
        "".join(
            f'[[site]]\noperator_id = "{op}"\nsite_id = "{site}"\nname = "{site}"\n'
            'site_type = "telecom_tower"\nlatitude = 6.5\nlongitude = 3.4\n'
            'timezone = "Africa/Lagos"\ncurrency = "NGN"\nvalue_of_lost_load_per_kwh = 5000.0\n'
            "[site.battery]\nusable_kwh = 20.0\npower_kw = 10.0\nmin_soc_kwh = 4.0\n"
            "[site.generator]\nrated_kw = 15.0\nfuel_price_per_l = 1250.0\ntank_l = 300.0\n"
            for op, site in (("tower-co", "t1"), ("bank", "b1"))
        )
    )
    monkeypatch.setenv("VATICORE_PORTFOLIO_FILE", str(portfolio))
    at.radio[0].set_value("Pilot report")
    at.run()
    assert not at.exception, at.exception
    assert at.selectbox[0].options == ["tower-co"]
    assert at.metric[0].label == "Verdict" and at.metric[0].value == "NOT YET"


def test_pilot_reports_are_scoped_to_the_operator(tmp_path: Path) -> None:
    portfolio = tmp_path / "sites.toml"
    portfolio.write_text(
        '[[site]]\noperator_id = "bank"\nsite_id = "b1"\nname = "b1"\n'
        'site_type = "bank_branch"\nlatitude = 9.1\nlongitude = 7.4\n'
        'timezone = "Africa/Lagos"\ncurrency = "NGN"\nvalue_of_lost_load_per_kwh = 5000.0\n'
        "[site.battery]\nusable_kwh = 20.0\npower_kw = 10.0\n"
        "[site.generator]\nrated_kw = 15.0\nfuel_price_per_l = 1250.0\n"
    )
    settings = Settings(environment="production", portfolio_file=portfolio)  # type: ignore[call-arg]
    assert data.pilot_operators(settings, access.Access("bank")) == ["bank"]
    assert data.pilot_operators(settings, access.Access("tower-co")) == []
    assert [s.site_id for s in data.operator_sites(settings, access.Access(None), "bank")] == ["b1"]
    with pytest.raises(PermissionError):
        data.operator_sites(settings, access.Access("tower-co"), "bank")
