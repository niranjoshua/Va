"""The pipeline command line: keys, opt-outs, ingest and health, end to end on files."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from vaticore.delivery.recipients import email_hash, recipient_hash
from vaticore.pipeline.cli import main
from vaticore.pipeline.store import PlanStore

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)  # no .env from the checkout
    monkeypatch.setenv("VATICORE_DATABASE_URL", f"duckdb:///{tmp_path / 'readings.duckdb'}")
    monkeypatch.setenv("VATICORE_PLAN_STORE_URL", f"duckdb:///{tmp_path / 'plans.duckdb'}")
    return tmp_path


def _store(env: Path) -> PlanStore:
    return PlanStore(f"duckdb:///{env / 'plans.duckdb'}")


def test_api_keys_are_created_listed_and_revoked(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["apikey", "create", "--operator", "op", "--name", "ops"]) == 0
    key = capsys.readouterr().out.strip().splitlines()[-1]
    store = _store(env)
    assert store.operator_for_key(key) == "op"
    store.close()

    assert main(["apikey", "list"]) == 0
    listing = capsys.readouterr().out
    assert "op" in listing and key not in listing
    key_id = key.split("_")[1]
    assert main(["apikey", "revoke", "--key-id", key_id]) == 0
    assert main(["apikey", "revoke", "--key-id", "nope"]) == 1
    store = _store(env)
    assert store.operator_for_key(key) is None
    store.close()


def test_opt_outs_by_phone_or_email(env: Path) -> None:
    assert main(["optout", "--phone", "+234 800 000 0001"]) == 0
    assert main(["optout", "--email", "Ada@Bank.ng"]) == 0
    store = _store(env)
    assert store.is_opted_out(recipient_hash("+2348000000001"))
    assert store.is_opted_out(email_hash("ada@bank.ng"))
    store.close()
    assert main(["optout", "--email", "ada@bank.ng", "--undo"]) == 0
    store = _store(env)
    assert not store.is_opted_out(email_hash("ada@bank.ng"))
    store.close()


def test_ingest_then_health(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    index = pd.date_range("2026-03-01", periods=48, freq="h")
    pd.DataFrame({"Time": index.strftime("%Y-%m-%d %H:%M"), "Load(W)": 3000, "SOC": 70}).to_csv(
        env / "export.csv", index=False
    )
    (env / "sources.toml").write_text(
        "[[source]]\n"
        'operator_id = "op"\nsite_id = "s1"\nconnector = "csv"\n'
        f'path = "{env / "export.csv"}"\ntimezone = "Africa/Lagos"\n'
        "backfill_days = 1000\n"
        "[source.columns]\n"
        'timestamp = "Time"\nload_kw = "Load(W)"\nbattery_soc_pct = "SOC"\n'
        "[source.scale]\nload_kw = 0.001\n"
    )
    (env / "sites.toml").write_text(
        "[[site]]\n"
        'operator_id = "op"\nsite_id = "s1"\nname = "Site One"\nsite_type = "telecom_tower"\n'
        'latitude = 6.5\nlongitude = 3.4\ntimezone = "Africa/Lagos"\ncurrency = "NGN"\n'
        "value_of_lost_load_per_kwh = 5000.0\n"
        "[site.battery]\nusable_kwh = 20.0\npower_kw = 10.0\n"
        "[site.generator]\nrated_kw = 15.0\nfuel_price_per_l = 1250.0\n"
    )
    assert main(["ingest", "--sources", str(env / "sources.toml")]) == 0
    assert "op/s1 (csv): 48 rows" in capsys.readouterr().out

    # The readings are months old by now: the report says so and fails.
    assert main(["health", "--portfolio", str(env / "sites.toml"), "--days", "7"]) == 1
    assert "op/s1" in capsys.readouterr().out
