"""Production basics: migrations, backups that restore, logs and the heartbeat."""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pandas as pd
import pytest

from vaticore.config import Settings
from vaticore.observability import JsonFormatter, heartbeat
from vaticore.pipeline.store import PlanStore, RunRecord
from vaticore.storage import migrations
from vaticore.storage.backup import backup, prune, verify_restore

POSTGRES = os.environ.get("VATICORE_TEST_POSTGRES_URL")
NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)


def _fill(store: PlanStore, runs: int = 3) -> None:
    for k in range(runs):
        start = datetime(2026, 3, 1 + k, 23, tzinfo=UTC)
        store.save_run(RunRecord("op", "s1", date(2026, 3, 2 + k), start, start, start, "planned"))
    store.add_delivery("op", "s1", NOW, 500.0, "INV-1", NOW)


# -- migrations ----------------------------------------------------------------------


def test_migrations_apply_once_and_in_order(tmp_path: Path) -> None:
    url = f"duckdb:///{tmp_path / 'plans.duckdb'}"
    PlanStore(url).close()  # the baseline tables
    before = migrations.status(url, migrations.PLANS)
    assert before.applied == () and [m.version for m in before.pending] == [1, 2, 3, 4, 5, 6]
    applied = migrations.migrate(url, migrations.PLANS, now=NOW)
    assert [m.version for m in applied] == [1, 2, 3, 4, 5, 6]
    assert migrations.migrate(url, migrations.PLANS) == []
    assert migrations.status(url, migrations.PLANS).current
    assert migrations.status(url, migrations.READINGS).current  # nothing for readings yet


def test_code_refuses_a_schema_from_a_newer_release(tmp_path: Path) -> None:
    url = f"duckdb:///{tmp_path / 'plans.duckdb'}"
    migrations.migrate(url, migrations.PLANS, now=NOW)
    import duckdb

    con = duckdb.connect(str(tmp_path / "plans.duckdb"))
    con.execute("INSERT INTO schema_migrations VALUES ('plans', 99, 'from the future', now())")
    con.close()
    with pytest.raises(migrations.SchemaTooNewError, match="newer release"):
        migrations.status(url, migrations.PLANS)


def test_migration_versions_are_unique_and_append_only() -> None:
    versions = [m.version for m in migrations.MIGRATIONS]
    assert versions == sorted(versions) and len(set(versions)) == len(versions)
    assert all(m.database in (migrations.PLANS, migrations.READINGS) for m in migrations.MIGRATIONS)


# -- backups -------------------------------------------------------------------------


def test_a_duckdb_backup_restores_with_every_row(tmp_path: Path) -> None:
    url = f"duckdb:///{tmp_path / 'plans.duckdb'}"
    store = PlanStore(url)
    _fill(store)
    store.close()
    result = backup(url, tmp_path / "backups", now=NOW)
    assert result.tables["pipeline_runs"] == 3 and result.tables["fuel_deliveries"] == 1
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["kind"] == "duckdb" and manifest["sha256"]

    check = verify_restore(result.path)
    assert check.ok, check.to_text()
    assert check.tables["pipeline_runs"] == (3, 3)
    assert "OK" in check.to_text()


def test_a_damaged_backup_is_caught(tmp_path: Path) -> None:
    url = f"duckdb:///{tmp_path / 'plans.duckdb'}"
    store = PlanStore(url)
    _fill(store)
    store.close()
    result = backup(url, tmp_path / "backups", now=NOW)
    victim = next(p for p in sorted(result.path.rglob("*.parquet")))
    victim.write_bytes(victim.read_bytes()[:-10])
    check = verify_restore(result.path)
    assert not check.ok and "damaged" in check.problems[0]


def test_old_backups_are_pruned(tmp_path: Path) -> None:
    url = f"duckdb:///{tmp_path / 'plans.duckdb'}"
    PlanStore(url).close()
    for hour in (1, 2, 3):
        backup(url, tmp_path / "backups", now=NOW.replace(hour=hour))
    removed = prune(tmp_path / "backups", keep=1)
    assert len(removed) == 2
    left = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert left == ["vaticore-20261004T030000Z", "vaticore-20261004T030000Z.manifest.json"]


@pytest.fixture
def pg_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    if POSTGRES is None:
        pytest.skip("set VATICORE_TEST_POSTGRES_URL to run against Postgres")
    for candidate in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        monkeypatch.setenv("PATH", f"{candidate}:{os.environ['PATH']}")
        break
    if shutil.which("pg_dump") is None:
        pytest.skip("pg_dump is not installed")


def _scratch(name: str) -> str:
    import psycopg

    assert POSTGRES is not None
    admin = POSTGRES.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as con:
        con.execute(f"DROP DATABASE IF EXISTS {name}")
        con.execute(f"CREATE DATABASE {name}")
    return POSTGRES.rsplit("/", 1)[0] + f"/{name}"


def test_a_postgres_backup_restores_with_every_row(tmp_path: Path, pg_tools: None) -> None:
    assert POSTGRES is not None
    source = _scratch("vaticore_backup_source")
    store = PlanStore(source)
    _fill(store, runs=5)
    store.close()
    result = backup(source, tmp_path, now=NOW)
    manifest = json.loads(result.manifest_path.read_text())
    assert "@" not in manifest["source"] and manifest["tables"]["pipeline_runs"] == 5

    scratch = _scratch("vaticore_restore_scratch_test")
    first = verify_restore(result.path, scratch)
    assert first.ok, first.to_text()
    again = verify_restore(result.path, scratch)  # a marked scratch is wiped and reused
    assert again.ok and again.tables["pipeline_runs"] == (5, 5)


def test_restore_checks_never_wipe_a_real_database(tmp_path: Path, pg_tools: None) -> None:
    source = _scratch("vaticore_backup_source")
    PlanStore(source).close()
    result = backup(source, tmp_path, now=NOW)
    precious = _scratch("vaticore_precious")
    store = PlanStore(precious)
    _fill(store)
    store.close()
    check = verify_restore(result.path, precious)
    assert not check.ok and "refusing to wipe" in check.problems[0]
    store = PlanStore(precious)
    assert len(store.runs()) == 3  # untouched
    store.close()


# -- logs and heartbeat ------------------------------------------------------------


def test_json_logs_are_one_object_per_line() -> None:
    record = logging.LogRecord(
        "vaticore.pipeline", logging.WARNING, __file__, 1, "site %s", ("s1",), None
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "WARNING" and payload["message"] == "site s1"
    assert payload["logger"] == "vaticore.pipeline" and pd.Timestamp(payload["time"]).tzinfo


def test_the_heartbeat_reports_success_and_failure() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(heartbeat_url="https://hc.example/ping/abc")  # type: ignore[arg-type]
    heartbeat(settings, ok=True, client=client)
    heartbeat(settings, ok=False, client=client)
    heartbeat(Settings(), ok=True, client=client)  # not configured: nothing sent
    assert seen == ["https://hc.example/ping/abc", "https://hc.example/ping/abc/fail"]


def test_a_database_from_before_reasons_gains_them_on_migrate(tmp_path: Path) -> None:
    """An existing feedback table, created before replies carried a reason."""
    import duckdb

    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE feedback (provider_message_id TEXT PRIMARY KEY, received_at TIMESTAMPTZ"
        " NOT NULL, recipient_hash TEXT NOT NULL, operator_id TEXT, site_id TEXT,"
        " plan_date DATE, kind TEXT NOT NULL, text TEXT)"
    )
    con.execute(
        "INSERT INTO feedback VALUES ('m1', now(), 'h', 'op', 's', '2026-10-01', 'followed', '1')"
    )
    con.close()
    url = f"duckdb:///{path}"
    migrations.migrate(url, migrations.PLANS, now=NOW)
    store = PlanStore(url)
    assert store.record_feedback(
        provider_message_id="m2", received_at=NOW, recipient_hash="h", kind="reason",
        text="B", plan=("op", "s", date(2026, 10, 1)), reason="no_diesel",
    )  # fmt: skip
    rows = store.feedback("op")
    assert "no_diesel" in rows["reason"].tolist() and len(rows) == 2
    store.close()


def test_jobs_that_run_the_foundation_models_have_the_memory_for_them() -> None:
    """Chronos-2 with PyTorch peaks above 1 GB; Render's default instance has 512 MB."""
    import yaml

    blueprint = yaml.safe_load((Path(__file__).parents[1] / "render.yaml").read_text())
    for service in blueprint["services"]:
        env = {e["key"]: e.get("value") for e in service.get("envVars", [])}
        if env.get("VATICORE_WITH_FOUNDATION") == "1":
            assert service.get("plan") in ("standard", "pro", "pro plus"), service["name"]


def test_the_blueprint_wires_every_database_and_keeps_data_in_one_region() -> None:
    """Connection strings come from the blueprint's own databases, all in Frankfurt."""
    import yaml

    blueprint = yaml.safe_load((Path(__file__).parents[1] / "render.yaml").read_text())
    databases = {d["name"]: d for d in blueprint["databases"]}
    assert all(d["region"] == "frankfurt" for d in databases.values())
    assert all(d.get("ipAllowList") == [] for d in databases.values())
    names = set()
    for service in blueprint["services"]:
        names.add(service["name"])
        assert service.get("region") == "frankfurt", service["name"]
        env = {e["key"]: e for e in service.get("envVars", [])}
        url = env.get("VATICORE_DATABASE_URL")
        assert url is not None and "fromDatabase" in url, service["name"]
        assert url["fromDatabase"]["name"] in databases
        staging = env["VATICORE_ENVIRONMENT"]["value"] == "staging"
        assert url["fromDatabase"]["name"] == (
            "vaticore-db-staging" if staging else "vaticore-db"
        ), service["name"]
    drill = next(s for s in blueprint["services"] if s["name"] == "vaticore-restore-drill")
    scratch = next(e for e in drill["envVars"] if e["key"] == "VATICORE_RESTORE_TEST_URL")
    assert scratch["fromDatabase"]["name"] == "vaticore-db-restore-scratch"
    assert {"vaticore-safety-net", "vaticore-weekly-summary", "vaticore-daily-plans"} <= names


def test_web_services_answer_on_the_company_domain() -> None:
    import yaml

    blueprint = yaml.safe_load((Path(__file__).parents[1] / "render.yaml").read_text())
    domains = {s["name"]: s.get("domains", []) for s in blueprint["services"] if s["type"] == "web"}
    assert domains == {
        "vaticore-api": ["api.vaticore.co.uk"],
        "vaticore-dashboard": ["app.vaticore.co.uk"],
        "vaticore-api-staging": ["api-staging.vaticore.co.uk"],
        "vaticore-dashboard-staging": ["app-staging.vaticore.co.uk"],
    }
