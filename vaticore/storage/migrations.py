"""Versioned database migrations: every schema change recorded, applied once, in order.

The stores create their tables idempotently when they open (the baseline). Any
change after that is a numbered migration here, applied by

    uv run python -m vaticore.pipeline migrate            # apply what is pending
    uv run python -m vaticore.pipeline migrate --status   # list without applying

before a deploy goes live (render.yaml runs it as the pre-deploy step and at
the start of the daily job). Each migration runs in its own transaction and is
recorded in `schema_migrations`, per database ("plans" for the plan store,
"readings" for the observation store), so the two can live in one database or
two.

Rules for adding one:
  - Append, never edit or reorder: a migration that has run somewhere is
    history. Fix a mistake with a new migration.
  - Make it safe to run on a live database (add columns as nullable, build
    indexes that do not lock writes for long), because the old code keeps
    serving until the new deploy is up.
  - Give Postgres and DuckDB statements separately where they differ; leave
    one empty when a change does not apply there.

Code refuses to run against a database migrated by newer code (a version it
does not know), rather than misreading a schema it has never seen.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

PLANS = "plans"
READINGS = "readings"


@dataclass(frozen=True)
class Migration:
    version: int
    database: str  # plans or readings
    name: str
    postgres: tuple[str, ...] = ()
    duckdb: tuple[str, ...] = ()


_SUMMARY_DELIVERIES = """
CREATE TABLE IF NOT EXISTS summary_deliveries (
    operator_id TEXT NOT NULL,
    week_start DATE NOT NULL,
    channel TEXT NOT NULL,
    recipient_hash TEXT NOT NULL,
    recipient_masked TEXT,
    status TEXT NOT NULL,
    provider_message_id TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (operator_id, week_start, channel, recipient_hash)
)
"""

# Append only. DuckDB gets no secondary indexes: its tables are small, it scans
# them fast, and its indexes complicate updates of indexed columns.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        PLANS,
        "index deliveries by provider message id, for WhatsApp receipts",
        postgres=(
            "CREATE INDEX IF NOT EXISTS deliveries_provider_message_id"
            " ON deliveries (provider_message_id)",
        ),
    ),
    Migration(
        2,
        PLANS,
        "index deliveries by recipient, for matching replies",
        postgres=(
            "CREATE INDEX IF NOT EXISTS deliveries_recipient"
            " ON deliveries (recipient_hash, updated_at)",
        ),
    ),
    Migration(
        3,
        PLANS,
        "index plan runs by end time, for finding days to score",
        postgres=("CREATE INDEX IF NOT EXISTS pipeline_runs_plan_end ON pipeline_runs (plan_end)",),
    ),
    Migration(
        4,
        PLANS,
        "why a plan was not followed: a reason on each reply",
        postgres=("ALTER TABLE IF EXISTS feedback ADD COLUMN IF NOT EXISTS reason TEXT",),
        duckdb=("ALTER TABLE IF EXISTS feedback ADD COLUMN IF NOT EXISTS reason TEXT",),
    ),
    Migration(
        5,
        PLANS,
        "weekly summaries sent to supervisors",
        postgres=(_SUMMARY_DELIVERIES,),
        duckdb=(_SUMMARY_DELIVERIES,),
    ),
)

_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    database TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (database, version)
)
"""


class SchemaTooNewError(RuntimeError):
    """The database was migrated by newer code than this."""


@dataclass(frozen=True)
class MigrationStatus:
    database: str
    applied: tuple[int, ...]
    pending: tuple[Migration, ...]

    @property
    def current(self) -> bool:
        return not self.pending


class _Connection:
    """Just enough of DuckDB and psycopg to run migrations on either."""

    def __init__(self, url: str) -> None:
        if url.startswith("postgres"):
            import psycopg

            self.postgres = True
            self.raw: Any = psycopg.connect(
                url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
            )
        else:
            import duckdb

            from vaticore.storage.duckdb_repository import _path_from_url

            self.postgres = False
            self.raw = duckdb.connect(_path_from_url(url) if url.startswith("duckdb") else url)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        if self.postgres:
            self.raw.execute(sql.replace("?", "%s"), params)
        else:
            self.raw.execute(sql, list(params))

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        if self.postgres:
            with self.raw.cursor() as cur:
                cur.execute(sql.replace("?", "%s"), params)
                return list(cur.fetchall())
        result: list[tuple[Any, ...]] = self.raw.execute(sql, list(params)).fetchall()
        return result

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self.postgres:
            with self.raw.transaction():
                yield
            return
        self.raw.execute("BEGIN TRANSACTION")
        try:
            yield
        except BaseException:
            self.raw.execute("ROLLBACK")
            raise
        self.raw.execute("COMMIT")

    def close(self) -> None:
        self.raw.close()


def status(url: str, database: str) -> MigrationStatus:
    """What has been applied to a database, and what is pending."""
    con = _Connection(url)
    try:
        return _status(con, database)
    finally:
        con.close()


def migrate(url: str, database: str, *, now: datetime | None = None) -> list[Migration]:
    """Apply a database's pending migrations in order; return those applied."""
    con = _Connection(url)
    try:
        state = _status(con, database)
        applied = []
        for migration in state.pending:
            statements = migration.postgres if con.postgres else migration.duckdb
            with con.transaction():
                for sql in statements:
                    con.execute(sql)
                con.execute(
                    "INSERT INTO schema_migrations (database, version, name, applied_at)"
                    " VALUES (?, ?, ?, ?)",
                    (database, migration.version, migration.name, now or datetime.now(tz=UTC)),
                )
            applied.append(migration)
        return applied
    finally:
        con.close()


def _status(con: _Connection, database: str) -> MigrationStatus:
    con.execute(_TABLE)
    done = tuple(
        int(r[0])
        for r in con.rows(
            "SELECT version FROM schema_migrations WHERE database = ? ORDER BY version",
            (database,),
        )
    )
    known = {m.version for m in MIGRATIONS if m.database == database}
    unknown = sorted(set(done) - known)
    if unknown:
        raise SchemaTooNewError(
            f"the {database} database has migration(s) {unknown} that this code does not "
            "know: it was migrated by a newer release. Deploy that release (or newer)."
        )
    pending = tuple(m for m in MIGRATIONS if m.database == database and m.version not in done)
    return MigrationStatus(database, done, pending)
