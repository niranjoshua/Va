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


_SITES = (
    "[[site]]\n"
    'operator_id = "op"\nsite_id = "s1"\nname = "Site One"\nsite_type = "telecom_tower"\n'
    'latitude = 6.5\nlongitude = 3.4\ntimezone = "Africa/Lagos"\ncurrency = "NGN"\n'
    "value_of_lost_load_per_kwh = 5000.0\n"
    "[site.battery]\nusable_kwh = 20.0\npower_kw = 10.0\n"
    "[site.generator]\nrated_kw = 15.0\nfuel_price_per_l = 1250.0\n"
)


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
    (env / "sites.toml").write_text(_SITES)
    assert main(["ingest", "--sources", str(env / "sources.toml")]) == 0
    assert "op/s1 (csv): 48 rows" in capsys.readouterr().out

    # The readings are months old by now: the report says so and fails.
    assert main(["health", "--portfolio", str(env / "sites.toml"), "--days", "7"]) == 1
    assert "op/s1" in capsys.readouterr().out


def test_monitor_prints_each_model_week_by_week(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, date, datetime, timedelta

    from vaticore.pipeline.store import ScoreRecord

    store = _store(env)
    now = datetime.now(tz=UTC)
    detail = {
        "models": {
            "persistence (baseline)": {"role": "baseline", "pinball": 1.0, "coverage_80": 0.8,
                                       "hours": 24},
            "quantile_gbm_day_ahead+conformal": {"role": "primary", "pinball": 0.8,
                                                 "coverage_80": 0.79, "hours": 24},
        }
    }  # fmt: skip
    store.save_score(
        ScoreRecord("op", "s1", date.today() - timedelta(days=1), now, 24, 0, 0.8, 0.79,
                    0, 0, 0, 0, 0, 0, 0, 0, 0, detail)
    )  # fmt: skip
    store.close()
    (env / "sites.toml").write_text(_SITES)
    assert main(["monitor", "--portfolio", str(env / "sites.toml")]) == 0
    out = capsys.readouterr().out
    assert "op/s1" in out and "quantile_gbm_day_ahead" in out and "+20%" in out


def test_live_weather_needs_a_key_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from vaticore.config import Settings
    from vaticore.pipeline.cli import _weather

    assert _weather(Settings(environment="production")) is None
    assert _weather(Settings(environment="production", weather_api_key="k")) is not None  # type: ignore[arg-type]
    assert _weather(Settings(environment="local")) is not None
    assert _weather(Settings(weather_provider="none")) is None


def test_fuel_deliveries_are_recorded_and_reconciled(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, datetime, timedelta

    from vaticore.storage import DuckDBRepository

    (env / "sites.toml").write_text(_SITES)
    now = pd.Timestamp(datetime.now(tz=UTC)).floor("h")
    hours = pd.date_range(now - timedelta(days=3), now, freq="h", inclusive="left")
    level = [400.0] * len(hours)
    level[30:] = [360.0] * (len(hours) - 30)  # 40 L gone overnight, generator off
    repo = DuckDBRepository(f"{env / 'readings.duckdb'}")
    repo.upsert(
        pd.DataFrame({"operator_id": "op", "site_id": "s1", "timestamp": hours, "load_kw": 3.0,
                      "generation_kw": 0.0, "genset_kw": 0.0, "fuel_level_l": level})
    )  # fmt: skip
    repo.close()

    at = (now - timedelta(days=2)).tz_convert("Africa/Lagos").strftime("%Y-%m-%d %H:%M")
    assert main(["fuel", "add", "--site", "op/s1", "--litres", "200", "--at", at,
                 "--timezone", "Africa/Lagos", "--reference", "INV-9"]) == 0  # fmt: skip
    with pytest.raises(SystemExit, match="offset"):
        main(["fuel", "add", "--site", "op/s1", "--litres", "10", "--at", at])
    (env / "deliveries.csv").write_text(
        "operator_id,site_id,delivered_at,litres,reference\n"
        f"op,s1,{(now - timedelta(days=1)).isoformat()},50,INV-10\n"
    )
    assert main(["fuel", "import", "--csv", str(env / "deliveries.csv")]) == 0
    capsys.readouterr()

    assert main(["fuel", "report", "--portfolio", str(env / "sites.toml"), "--days", "3"]) == 1
    out = capsys.readouterr().out
    assert "Delivered 250 L" in out and "drop_while_off" not in out  # codes stay internal
    assert "while the generator was off" in out and "INV-9" in out


def test_migrate_and_a_verified_backup(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate", "--status"]) == 0
    assert "pending: 1 " in capsys.readouterr().out
    assert main(["migrate"]) == 0
    assert "up to date" in capsys.readouterr().out
    assert main(["migrate", "--status"]) == 0
    assert "pending: none" in capsys.readouterr().out

    store = _store(env)
    store.add_delivery("op", "s1", pd.Timestamp("2026-10-01", tz="UTC").to_pydatetime(), 50.0,
                       None, pd.Timestamp("2026-10-01", tz="UTC").to_pydatetime())  # fmt: skip
    store.close()
    assert main(["backup", "--out", str(env / "backups"), "--verify", "--keep", "2"]) == 0
    out = capsys.readouterr().out
    assert out.count("Restore check") == 2 and "FAILED" not in out
    dump = next(p for p in (env / "backups").iterdir() if p.is_dir())
    assert main(["restore-check", "--dump", str(dump)]) == 0
