"""Backups, and proof that they restore.

A backup nobody has restored is a hope, not a backup. So every backup here
comes with a manifest (each table's row count at the moment of the dump, and
the file's checksum), and `verify_restore` restores it into a scratch database
and checks every table came back with the same number of rows.

    uv run python -m vaticore.pipeline backup --out /backups --keep 14 --verify
    uv run python -m vaticore.pipeline restore-check --dump /backups/vaticore-....dump

Postgres: `pg_dump` in its custom format, taken from the same snapshot the row
counts were read in, so the counts are exact even while the database is in
use. Needs `pg_dump` and `pg_restore` at least as new as the server (the
Docker image carries them). TimescaleDB hypertables are restored with the
extension's pre- and post-restore steps.

DuckDB: `EXPORT DATABASE` to Parquet files, restored with `IMPORT DATABASE`.

The scratch database is wiped before each check. To make pointing it at a
real database impossible by accident, a scratch database must be empty the
first time; it is then marked as Vaticore's restore scratch, and only a
marked database is ever wiped.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MARKER = "vaticore_restore_scratch"


@dataclass(frozen=True)
class BackupResult:
    path: Path
    manifest_path: Path
    tables: dict[str, int]


@dataclass
class RestoreCheck:
    dump: Path
    tables: dict[str, tuple[int, int | None]] = field(default_factory=dict)  # expected, restored
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_text(self) -> str:
        lines = [f"Restore check of {self.dump.name}: {'OK' if self.ok else 'FAILED'}"]
        for table, (expected, restored) in sorted(self.tables.items()):
            mark = "ok" if expected == restored else "MISMATCH"
            lines.append(f"  {table}: {expected} rows backed up, {restored} restored ({mark})")
        lines.extend(f"  problem: {p}" for p in self.problems)
        return "\n".join(lines)


def backup(url: str, out_dir: Path, *, now: datetime | None = None) -> BackupResult:
    """Dump a database to `out_dir`, with a manifest beside it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(tz=UTC)).strftime("%Y%m%dT%H%M%SZ")
    if url.startswith("postgres"):
        path, tables, extras = _pg_backup(_dsn(url), out_dir / f"vaticore-{stamp}.dump")
        kind = "postgres"
    else:
        path, tables = _duck_backup(url, out_dir / f"vaticore-{stamp}")
        kind, extras = "duckdb", {}
    manifest = {
        "kind": kind,
        "created_at": stamp,
        "source": _redact(url),
        "file": path.name,
        "sha256": _checksum(path),
        "tables": tables,
        **extras,
    }
    manifest_path = path.parent / f"{path.name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return BackupResult(path, manifest_path, tables)


def verify_restore(dump: Path, scratch_url: str | None = None) -> RestoreCheck:
    """Restore a backup into a scratch database and compare every table's rows."""
    check = RestoreCheck(dump)
    manifest_path = dump.parent / f"{dump.name}.manifest.json"
    if not manifest_path.exists():
        check.problems.append(f"no manifest beside the dump ({manifest_path.name})")
        return check
    manifest: dict[str, Any] = json.loads(manifest_path.read_text())
    if _checksum(dump) != manifest["sha256"]:
        check.problems.append("checksum does not match the manifest: the file is damaged")
        return check
    expected: dict[str, int] = manifest["tables"]
    try:
        if manifest["kind"] == "postgres":
            if not scratch_url or not scratch_url.startswith("postgres"):
                check.problems.append("a Postgres backup needs a Postgres scratch database")
                return check
            restored = _pg_restore(dump, _dsn(scratch_url), manifest, check)
        else:
            restored = _duck_restore(dump)
    except Exception as exc:  # reported, never raised: a failed drill is a result
        check.problems.append(f"restore failed: {type(exc).__name__}: {exc}")
        return check
    for table, rows in expected.items():
        got = restored.get(table)
        check.tables[table] = (rows, got)
        if got is None:
            check.problems.append(f"table {table} missing after restore")
        elif got != rows:
            check.problems.append(f"table {table}: {rows} rows backed up, {got} restored")
    return check


def prune(out_dir: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` backups (and their manifests)."""
    manifests = sorted(out_dir.glob("vaticore-*.manifest.json"))
    removed = []
    for manifest in manifests[: max(0, len(manifests) - keep)]:
        target = out_dir / manifest.name.removesuffix(".manifest.json")
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        manifest.unlink()
        removed.append(target)
    return removed


# -- Postgres ----------------------------------------------------------------------


def _pg_backup(dsn: str, path: Path) -> tuple[Path, dict[str, int], dict[str, Any]]:
    import psycopg

    # Count rows and dump from one snapshot, so the manifest matches the file.
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = con.execute("SELECT pg_export_snapshot()").fetchone()[0]  # type: ignore[index]
        tables = {
            name: int(con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])  # type: ignore[index]
            for name in _pg_tables(con)
        }
        timescale = bool(
            con.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'").fetchone()
        )
        _run(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                f"--snapshot={snapshot}",
                f"--file={path}",
            ],
            dsn,
        )
        con.execute("ROLLBACK")
    return path, tables, {"timescaledb": timescale}


def _pg_restore(
    dump: Path, dsn: str, manifest: dict[str, Any], check: RestoreCheck
) -> dict[str, int]:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as con:
        tables = _pg_all_tables(con)
        if tables and MARKER not in tables:
            raise RuntimeError(
                "the scratch database has tables and is not marked as Vaticore's restore "
                "scratch: refusing to wipe it. Point VATICORE_RESTORE_TEST_URL at an empty "
                "database made for restore checks."
            )
        con.execute("DROP SCHEMA public CASCADE")
        con.execute("CREATE SCHEMA public")
        if manifest.get("timescaledb"):
            con.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            con.execute("SELECT timescaledb_pre_restore()")
    if shutil.which("pg_restore") is None:
        raise RuntimeError("pg_restore is not installed (the Docker image includes it)")
    result = subprocess.run(
        # The database name only; host, user and password come from the environment.
        [
            "pg_restore",
            "--no-owner",
            "--no-privileges",
            f"--dbname={urlsplit(dsn).path.lstrip('/')}",
            str(dump),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_pg_env(dsn),
    )
    if result.returncode != 0:
        # pg_restore also exits non-zero on harmless warnings; the row counts decide.
        check.problems.extend(
            f"pg_restore: {line}" for line in result.stderr.splitlines() if "error" in line.lower()
        )
    with psycopg.connect(dsn, autocommit=True) as con:
        if manifest.get("timescaledb"):
            con.execute("SELECT timescaledb_post_restore()")
        restored = {
            name: int(con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])  # type: ignore[index]
            for name in _pg_tables(con)
        }
        con.execute(f"CREATE TABLE IF NOT EXISTS {MARKER} (checked_at TIMESTAMPTZ)")
        con.execute(f"INSERT INTO {MARKER} VALUES (now())")
    return restored


def _pg_tables(con: Any) -> list[str]:
    """The public schema's tables (the restore marker excluded)."""
    return [name for name in _pg_all_tables(con) if name != MARKER]


def _pg_all_tables(con: Any) -> list[str]:
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"
    ).fetchall()
    return [str(r[0]) for r in rows]


def _pg_env(dsn: str) -> dict[str, str]:
    """libpq settings for pg_dump and pg_restore: never a password on the command line."""
    import os

    parts = urlsplit(dsn)
    env = dict(os.environ)
    env.update(
        {
            "PGHOST": parts.hostname or "localhost",
            "PGPORT": str(parts.port or 5432),
            "PGDATABASE": parts.path.lstrip("/"),
        }
    )
    if parts.username:
        env["PGUSER"] = parts.username
    if parts.password:
        env["PGPASSWORD"] = parts.password
    if "sslmode=require" in (parts.query or ""):
        env["PGSSLMODE"] = "require"
    return env


# -- DuckDB ------------------------------------------------------------------------


def _duck_backup(url: str, target: Path) -> tuple[Path, dict[str, int]]:
    import duckdb

    from vaticore.storage.duckdb_repository import _path_from_url

    path = _path_from_url(url) if url.startswith("duckdb") else url
    con = duckdb.connect(path, read_only=path != ":memory:")
    try:
        tables = _duck_counts(con)
        con.execute(f"EXPORT DATABASE '{target}' (FORMAT parquet)")
    finally:
        con.close()
    return target, tables


def _duck_restore(dump: Path) -> dict[str, int]:
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        con.execute(f"IMPORT DATABASE '{dump}'")
        return _duck_counts(con)
    finally:
        con.close()


def _duck_counts(con: Any) -> dict[str, int]:
    names = [
        str(r[0])
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'main' AND table_type = 'BASE TABLE' ORDER BY table_name"
        ).fetchall()
    ]
    return {n: int(con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0]) for n in names}


# -- helpers -----------------------------------------------------------------------


def _dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def _redact(url: str) -> str:
    """host/database only: never the password in a manifest."""
    if not url.startswith("postgres"):
        return url.split("?")[0]
    parts = urlsplit(_dsn(url))
    return f"{parts.hostname}{':' + str(parts.port) if parts.port else ''}{parts.path}"


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
    for file in files:
        if path.is_dir():
            digest.update(str(file.relative_to(path)).encode())
        with file.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], dsn: str) -> None:
    if shutil.which(command[0]) is None:
        raise RuntimeError(f"{command[0]} is not installed (the Docker image includes it)")
    result = subprocess.run(command, capture_output=True, text=True, check=False, env=_pg_env(dsn))
    if result.returncode != 0:
        raise RuntimeError(f"{command[0]} failed: {result.stderr.strip()[:500]}")
