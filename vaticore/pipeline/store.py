"""The plan store: every forecast, plan, delivery and reply, kept for evidence.

A plan that is not stored cannot be scored, and a pilot that is not scored
proves nothing. So each daily run writes:

  pipeline_runs     one row per site per plan day: status, model used, data
                    health, the message sent and what the plan expects.
  forecast_hours    the hourly net load quantiles of every model that ran
                    (primary, shadow and the persistence baseline), so models
                    are compared on real sites automatically.
  plan_hours        the hourly plan, and the baseline plan it is scored against.
  plan_scores       once the day is over: accuracy, calibration, and litres,
                    outages and cost if the plan had been followed, against the
                    baseline and a perfect-forecast bound.
  deliveries        every message per recipient and channel, with its status.
  recipient_prefs   opt-outs (a reply of STOP), which always win over config.
  feedback          replies: followed the plan, did not (and why), or free text.
  summary_deliveries  weekly summaries sent to supervisors, so none goes twice.

Phone numbers are personal data: the store keeps only a one-way hash (to match
replies and opt-outs) and a masked form for display.

One implementation serves DuckDB (local, small deployments) and Postgres or
TimescaleDB (production), chosen by the database URL like the observation
store. Every table is keyed by operator_id and site_id.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd

_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    operator_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    plan_date DATE NOT NULL,
    plan_start TIMESTAMPTZ NOT NULL,
    plan_end TIMESTAMPTZ NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    model TEXT,
    fallback TEXT,
    health TEXT,
    summary TEXT,
    message TEXT,
    soc_start_kwh {DOUBLE},
    soc_assumed BOOLEAN,
    expected_fuel_l {DOUBLE},
    expected_genset_hours {DOUBLE},
    expected_genset_starts INTEGER,
    expected_unserved_kwh {DOUBLE},
    assets TEXT,
    error TEXT,
    PRIMARY KEY (operator_id, site_id, plan_date)
);
CREATE TABLE IF NOT EXISTS forecast_hours (
    operator_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    plan_date DATE NOT NULL,
    model TEXT NOT NULL,
    role TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    q10 {DOUBLE},
    q50 {DOUBLE},
    q90 {DOUBLE},
    PRIMARY KEY (operator_id, site_id, plan_date, role, model, timestamp)
);
CREATE TABLE IF NOT EXISTS plan_hours (
    operator_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    plan_date DATE NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    planned_net_load_kw {DOUBLE},
    genset_on BOOLEAN,
    grid_on BOOLEAN,
    expected_soc_kwh {DOUBLE},
    baseline_genset_on BOOLEAN,
    PRIMARY KEY (operator_id, site_id, plan_date, timestamp)
);
CREATE TABLE IF NOT EXISTS plan_scores (
    operator_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    plan_date DATE NOT NULL,
    scored_at TIMESTAMPTZ NOT NULL,
    hours_scored INTEGER,
    hours_missing INTEGER,
    pinball_primary {DOUBLE},
    coverage_primary {DOUBLE},
    fuel_plan_l {DOUBLE},
    fuel_baseline_l {DOUBLE},
    fuel_perfect_l {DOUBLE},
    unserved_plan_kwh {DOUBLE},
    unserved_baseline_kwh {DOUBLE},
    unserved_perfect_kwh {DOUBLE},
    cost_plan {DOUBLE},
    cost_baseline {DOUBLE},
    cost_perfect {DOUBLE},
    detail TEXT,
    PRIMARY KEY (operator_id, site_id, plan_date)
);
CREATE TABLE IF NOT EXISTS deliveries (
    operator_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    plan_date DATE NOT NULL,
    channel TEXT NOT NULL,
    recipient_hash TEXT NOT NULL,
    recipient_masked TEXT,
    status TEXT NOT NULL,
    provider_message_id TEXT,
    error TEXT,
    attempts INTEGER,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    message_language TEXT,
    PRIMARY KEY (operator_id, site_id, plan_date, channel, recipient_hash)
);
CREATE TABLE IF NOT EXISTS recipient_prefs (
    recipient_hash TEXT PRIMARY KEY,
    opted_out BOOLEAN NOT NULL,
    source TEXT,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    key_id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    name TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS model_status (
    operator_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    since TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (operator_id, site_id, model)
);
CREATE TABLE IF NOT EXISTS site_weather (
    operator_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    shortwave_radiation {DOUBLE},
    cloud_cover {DOUBLE},
    temperature_2m {DOUBLE},
    issued_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (operator_id, site_id, timestamp)
);
CREATE TABLE IF NOT EXISTS weather_issued (
    operator_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    plan_date DATE NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    shortwave_radiation {DOUBLE},
    cloud_cover {DOUBLE},
    temperature_2m {DOUBLE},
    issued_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (operator_id, site_id, plan_date, timestamp)
);
CREATE TABLE IF NOT EXISTS fuel_deliveries (
    operator_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    delivered_at TIMESTAMPTZ NOT NULL,
    litres {DOUBLE} NOT NULL,
    reference TEXT,
    recorded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (operator_id, site_id, delivered_at)
);
CREATE TABLE IF NOT EXISTS feedback (
    provider_message_id TEXT PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL,
    recipient_hash TEXT NOT NULL,
    operator_id TEXT,
    site_id TEXT,
    plan_date DATE,
    kind TEXT NOT NULL,
    text TEXT,
    reason TEXT
);
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
);
"""

# Delivery states that mean "already sent": never send the same plan twice.
SENT_STATES = ("sent", "delivered", "read")


@dataclass(frozen=True)
class RunRecord:
    operator_id: str
    site_id: str
    plan_date: date
    plan_start: datetime
    plan_end: datetime
    issued_at: datetime
    status: str  # planned, no_plan or failed
    model: str | None = None
    fallback: str | None = None
    health: dict[str, Any] | None = None
    summary: str | None = None
    message: str | None = None
    soc_start_kwh: float | None = None
    soc_assumed: bool | None = None
    expected_fuel_l: float | None = None
    expected_genset_hours: float | None = None
    expected_genset_starts: int | None = None
    expected_unserved_kwh: float | None = None
    assets: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class ScoreRecord:
    operator_id: str
    site_id: str
    plan_date: date
    scored_at: datetime
    hours_scored: int
    hours_missing: int
    pinball_primary: float | None
    coverage_primary: float | None
    fuel_plan_l: float
    fuel_baseline_l: float
    fuel_perfect_l: float
    unserved_plan_kwh: float
    unserved_baseline_kwh: float
    unserved_perfect_kwh: float
    cost_plan: float
    cost_baseline: float
    cost_perfect: float
    detail: dict[str, Any]


_JSON_FIELDS = ("health", "assets", "detail")


_SCHEMA_READY: set[str] = set()


class PlanStore:
    """Plans, forecasts, scores and deliveries, in DuckDB or Postgres."""

    def __init__(self, url: str) -> None:
        if url.startswith("duckdb") or not url.startswith("postgres"):
            import duckdb

            from vaticore.storage.duckdb_repository import _path_from_url

            target = _path_from_url(url) if url.startswith("duckdb") else url
            self._con: Any = duckdb.connect(target or ":memory:")
            self._con.execute("SET TimeZone = 'UTC'")
            self._postgres = False
            ddl = _DDL.format(DOUBLE="DOUBLE")
        else:
            import psycopg

            self._con = psycopg.connect(
                url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
            )
            self._con.execute("SET TIME ZONE 'UTC'")
            self._postgres = True
            ddl = _DDL.format(DOUBLE="DOUBLE PRECISION")
        # The baseline tables, once per database per process (":memory:" is a
        # new database every time).
        if url not in _SCHEMA_READY or ":memory:" in url:
            for statement in ddl.split(";"):
                if statement.strip():
                    self._con.execute(statement)
            _SCHEMA_READY.add(url)

    def ping(self) -> None:
        """Raise if the database cannot be reached."""
        self._df("SELECT 1 AS ok")

    # -- runs ----------------------------------------------------------------

    def save_run(
        self,
        run: RunRecord,
        *,
        forecasts: pd.DataFrame | None = None,
        plan: pd.DataFrame | None = None,
    ) -> None:
        """Write one site-day: the run row, its forecasts and its hourly plan.

        Re-running a day replaces that day's rows, so runs are idempotent.
        """
        key = (run.operator_id, run.site_id, run.plan_date)
        row = asdict(run)
        for name in _JSON_FIELDS:
            if name in row and row[name] is not None:
                row[name] = json.dumps(row[name], default=str)
        with self._transaction():
            self._upsert("pipeline_runs", row, ("operator_id", "site_id", "plan_date"))
            for table in ("forecast_hours", "plan_hours"):
                self._exec(
                    f"DELETE FROM {table} WHERE operator_id = ? AND site_id = ? AND plan_date = ?",
                    key,
                )
            if forecasts is not None and not forecasts.empty:
                cols = ["model", "role", "timestamp", "q10", "q50", "q90"]
                self._insert_frame("forecast_hours", key, forecasts[cols])
            if plan is not None and not plan.empty:
                cols = [
                    "timestamp",
                    "planned_net_load_kw",
                    "genset_on",
                    "grid_on",
                    "expected_soc_kwh",
                    "baseline_genset_on",
                ]
                self._insert_frame("plan_hours", key, plan[cols])

    def get_run(self, operator_id: str, site_id: str, plan_date: date) -> RunRecord | None:
        frame = self._df(
            "SELECT * FROM pipeline_runs WHERE operator_id = ? AND site_id = ? AND plan_date = ?",
            (operator_id, site_id, plan_date),
        )
        if frame.empty:
            return None
        row: dict[str, Any] = {str(k): v for k, v in frame.iloc[0].to_dict().items()}
        for name in _JSON_FIELDS:
            if isinstance(row.get(name), str):
                row[name] = json.loads(row[name])
        row = {k: (None if _is_missing(v) else v) for k, v in row.items()}
        row["plan_date"] = pd.Timestamp(row["plan_date"]).date()
        for name in ("plan_start", "plan_end", "issued_at"):
            row[name] = pd.Timestamp(row[name]).tz_convert("UTC").to_pydatetime()
        if row.get("expected_genset_starts") is not None:
            row["expected_genset_starts"] = int(row["expected_genset_starts"])
        return RunRecord(**row)

    def runs(self, operator_id: str | None = None, site_id: str | None = None) -> pd.DataFrame:
        query, params = "SELECT * FROM pipeline_runs WHERE 1 = 1", []
        if operator_id is not None:
            query += " AND operator_id = ?"
            params.append(operator_id)
        if site_id is not None:
            query += " AND site_id = ?"
            params.append(site_id)
        return self._df(query + " ORDER BY operator_id, site_id, plan_date", params)

    def forecasts(self, operator_id: str, site_id: str, plan_date: date) -> pd.DataFrame:
        return self._df(
            "SELECT * FROM forecast_hours WHERE operator_id = ? AND site_id = ? AND plan_date = ?"
            " ORDER BY model, timestamp",
            (operator_id, site_id, plan_date),
        )

    def plan_hours(self, operator_id: str, site_id: str, plan_date: date) -> pd.DataFrame:
        return self._df(
            "SELECT * FROM plan_hours WHERE operator_id = ? AND site_id = ? AND plan_date = ?"
            " ORDER BY timestamp",
            (operator_id, site_id, plan_date),
        )

    def unscored(self, ended_before: datetime) -> list[tuple[str, str, date]]:
        """Planned days whose window has ended and that have no score yet."""
        frame = self._df(
            "SELECT r.operator_id, r.site_id, r.plan_date FROM pipeline_runs r"
            " LEFT JOIN plan_scores s ON r.operator_id = s.operator_id"
            " AND r.site_id = s.site_id AND r.plan_date = s.plan_date"
            " WHERE r.status = 'planned' AND r.plan_end <= ? AND s.plan_date IS NULL"
            " ORDER BY r.plan_date",
            (ended_before,),
        )
        return [
            (str(op), str(site), pd.Timestamp(str(day)).date())
            for op, site, day in frame[["operator_id", "site_id", "plan_date"]].itertuples(
                index=False, name=None
            )
        ]

    # -- scores --------------------------------------------------------------

    def save_score(self, score: ScoreRecord) -> None:
        row = asdict(score)
        row["detail"] = json.dumps(row["detail"], default=str)
        self._upsert("plan_scores", row, ("operator_id", "site_id", "plan_date"))

    def scores(self, operator_id: str, site_id: str, since: date | None = None) -> pd.DataFrame:
        query = "SELECT * FROM plan_scores WHERE operator_id = ? AND site_id = ?"
        params: list[Any] = [operator_id, site_id]
        if since is not None:
            query += " AND plan_date >= ?"
            params.append(since)
        return self._df(query + " ORDER BY plan_date", params)

    # -- deliveries, preferences and feedback --------------------------------

    def delivery_status(
        self, operator_id: str, site_id: str, plan_date: date, channel: str, recipient_hash: str
    ) -> str | None:
        frame = self._df(
            "SELECT status FROM deliveries WHERE operator_id = ? AND site_id = ? AND"
            " plan_date = ? AND channel = ? AND recipient_hash = ?",
            (operator_id, site_id, plan_date, channel, recipient_hash),
        )
        return None if frame.empty else str(frame.iloc[0]["status"])

    def record_delivery(
        self,
        *,
        operator_id: str,
        site_id: str,
        plan_date: date,
        channel: str,
        recipient_hash: str,
        recipient_masked: str,
        status: str,
        provider_message_id: str | None,
        error: str | None,
        attempts: int,
        at: datetime,
        language: str | None = None,
    ) -> None:
        existing = self._df(
            "SELECT created_at FROM deliveries WHERE operator_id = ? AND site_id = ? AND"
            " plan_date = ? AND channel = ? AND recipient_hash = ?",
            (operator_id, site_id, plan_date, channel, recipient_hash),
        )
        created = at if existing.empty else pd.Timestamp(existing.iloc[0]["created_at"])
        self._upsert(
            "deliveries",
            {
                "operator_id": operator_id,
                "site_id": site_id,
                "plan_date": plan_date,
                "channel": channel,
                "recipient_hash": recipient_hash,
                "recipient_masked": recipient_masked,
                "status": status,
                "provider_message_id": provider_message_id,
                "error": error,
                "attempts": attempts,
                "created_at": created,
                "updated_at": at,
                "message_language": language,
            },
            ("operator_id", "site_id", "plan_date", "channel", "recipient_hash"),
        )

    def update_delivery_status(
        self, provider_message_id: str, status: str, at: datetime, error: str | None = None
    ) -> bool:
        """Apply a provider status callback. Never moves a message backwards."""
        order = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}
        frame = self._df(
            "SELECT status FROM deliveries WHERE provider_message_id = ?", (provider_message_id,)
        )
        if frame.empty:
            return False
        current = str(frame.iloc[0]["status"])
        if order.get(status, 0) <= order.get(current, 0) and status != "failed":
            return True
        self._exec(
            "UPDATE deliveries SET status = ?, error = COALESCE(?, error), updated_at = ?"
            " WHERE provider_message_id = ?",
            (status, error, at, provider_message_id),
        )
        return True

    def deliveries(self, operator_id: str | None = None) -> pd.DataFrame:
        query, params = "SELECT * FROM deliveries", []
        if operator_id is not None:
            query += " WHERE operator_id = ?"
            params.append(operator_id)
        return self._df(query + " ORDER BY plan_date, site_id", params)

    def last_delivery_to(self, recipient_hash: str) -> tuple[str, str, date] | None:
        """The most recent plan sent to a recipient, to attribute a reply to it."""
        frame = self._df(
            "SELECT operator_id, site_id, plan_date FROM deliveries WHERE recipient_hash = ?"
            " AND status IN ('sent', 'delivered', 'read') ORDER BY updated_at DESC LIMIT 1",
            (recipient_hash,),
        )
        if frame.empty:
            return None
        row = frame.iloc[0]
        return str(row["operator_id"]), str(row["site_id"]), pd.Timestamp(row["plan_date"]).date()

    def last_delivery_language(self, recipient_hash: str) -> str:
        """The language of the latest plan sent to a person, to reply in kind."""
        frame = self._df(
            "SELECT message_language FROM deliveries WHERE recipient_hash = ?"
            " AND status IN ('sent', 'delivered', 'read') ORDER BY updated_at DESC LIMIT 1",
            (recipient_hash,),
        )
        if frame.empty or pd.isna(frame.iloc[0]["message_language"]):
            return "en"
        return str(frame.iloc[0]["message_language"])

    def set_opt_out(self, recipient_hash: str, opted_out: bool, source: str, at: datetime) -> None:
        self._upsert(
            "recipient_prefs",
            {
                "recipient_hash": recipient_hash,
                "opted_out": opted_out,
                "source": source,
                "updated_at": at,
            },
            ("recipient_hash",),
        )

    def is_opted_out(self, recipient_hash: str) -> bool:
        frame = self._df(
            "SELECT opted_out FROM recipient_prefs WHERE recipient_hash = ?", (recipient_hash,)
        )
        return bool(frame.iloc[0]["opted_out"]) if not frame.empty else False

    def record_feedback(
        self,
        *,
        provider_message_id: str,
        received_at: datetime,
        recipient_hash: str,
        kind: str,
        text: str | None,
        plan: tuple[str, str, date] | None,
        reason: str | None = None,
    ) -> bool:
        """Store a reply once (providers retry webhooks). Returns False if seen before."""
        seen = self._df(
            "SELECT 1 FROM feedback WHERE provider_message_id = ?", (provider_message_id,)
        )
        if not seen.empty:
            return False
        operator_id, site_id, plan_date = plan if plan is not None else (None, None, None)
        self._exec(
            "INSERT INTO feedback (provider_message_id, received_at, recipient_hash, operator_id,"
            " site_id, plan_date, kind, text, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                provider_message_id,
                received_at,
                recipient_hash,
                operator_id,
                site_id,
                plan_date,
                kind,
                text,
                reason,
            ),
        )
        return True

    def last_feedback_from(self, recipient_hash: str) -> dict[str, Any] | None:
        """The person's most recent reply, to read the next one in its light."""
        frame = self._df(
            "SELECT * FROM feedback WHERE recipient_hash = ? ORDER BY received_at DESC LIMIT 1",
            (recipient_hash,),
        )
        return None if frame.empty else {str(k): v for k, v in frame.iloc[0].items()}

    # -- weekly summaries ----------------------------------------------------

    def summary_sent(
        self, operator_id: str, week_start: date, channel: str, recipient_hash: str
    ) -> bool:
        frame = self._df(
            "SELECT status FROM summary_deliveries WHERE operator_id = ? AND week_start = ?"
            " AND channel = ? AND recipient_hash = ?",
            (operator_id, week_start, channel, recipient_hash),
        )
        return not frame.empty and str(frame.iloc[0]["status"]) in SENT_STATES

    def record_summary(
        self,
        *,
        operator_id: str,
        week_start: date,
        channel: str,
        recipient_hash: str,
        recipient_masked: str,
        status: str,
        provider_message_id: str | None,
        error: str | None,
        at: datetime,
    ) -> None:
        self._upsert(
            "summary_deliveries",
            {
                "operator_id": operator_id,
                "week_start": week_start,
                "channel": channel,
                "recipient_hash": recipient_hash,
                "recipient_masked": recipient_masked,
                "status": status,
                "provider_message_id": provider_message_id,
                "error": error,
                "created_at": at,
            },
            ("operator_id", "week_start", "channel", "recipient_hash"),
        )

    def feedback(self, operator_id: str | None = None) -> pd.DataFrame:
        query, params = "SELECT * FROM feedback", []
        if operator_id is not None:
            query += " WHERE operator_id = ?"
            params.append(operator_id)
        return self._df(query + " ORDER BY received_at", params)

    # -- operator API keys ---------------------------------------------------

    def create_api_key(self, operator_id: str, name: str, at: datetime) -> str:
        """A new key for one operator. Returned once; only its hash is stored."""
        key_id = secrets.token_hex(4)
        secret = secrets.token_urlsafe(24)
        self._exec(
            "INSERT INTO api_keys (key_id, key_hash, operator_id, name, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (key_id, _key_hash(secret), operator_id, name, at),
        )
        return f"vk_{key_id}_{secret}"

    def operator_for_key(self, key: str) -> str | None:
        """The operator a key belongs to, or None if it is unknown or revoked."""
        parts = key.split("_", 2)
        if len(parts) != 3 or parts[0] != "vk":
            return None
        frame = self._df(
            "SELECT key_hash, operator_id FROM api_keys WHERE key_id = ? AND revoked_at IS NULL",
            (parts[1],),
        )
        if frame.empty:
            return None
        if not hmac.compare_digest(str(frame.iloc[0]["key_hash"]), _key_hash(parts[2])):
            return None
        return str(frame.iloc[0]["operator_id"])

    def revoke_api_key(self, key_id: str, at: datetime) -> bool:
        found = self._df("SELECT 1 FROM api_keys WHERE key_id = ?", (key_id,))
        if found.empty:
            return False
        self._exec("UPDATE api_keys SET revoked_at = ? WHERE key_id = ?", (at, key_id))
        return True

    def api_keys(self) -> pd.DataFrame:
        return self._df(
            "SELECT key_id, operator_id, name, created_at, revoked_at FROM api_keys"
            " ORDER BY operator_id, created_at"
        )

    # -- model monitoring ----------------------------------------------------

    def model_statuses(self, operator_id: str, site_id: str) -> dict[str, dict[str, Any]]:
        """Models monitoring has ever suspended at a site, with their current status."""
        frame = self._df(
            "SELECT model, status, reason, since FROM model_status"
            " WHERE operator_id = ? AND site_id = ?",
            (operator_id, site_id),
        )
        return {str(r["model"]): {str(k): v for k, v in r.items()} for _, r in frame.iterrows()}

    def set_model_status(
        self, operator_id: str, site_id: str, model: str, status: str, reason: str, at: datetime
    ) -> None:
        self._upsert(
            "model_status",
            {
                "operator_id": operator_id,
                "site_id": site_id,
                "model": model,
                "status": status,
                "reason": reason,
                "since": at,
            },
            ("operator_id", "site_id", "model"),
        )

    # -- fuel deliveries -----------------------------------------------------

    def add_delivery(
        self,
        operator_id: str,
        site_id: str,
        delivered_at: datetime,
        litres: float,
        reference: str | None,
        at: datetime,
    ) -> None:
        """Record a diesel delivery; recording the same time again corrects it."""
        if litres <= 0:
            raise ValueError("a delivery must be a positive number of litres")
        if delivered_at.tzinfo is None:
            raise ValueError("delivery times need a timezone")
        self._upsert(
            "fuel_deliveries",
            {
                "operator_id": operator_id,
                "site_id": site_id,
                "delivered_at": delivered_at,
                "litres": float(litres),
                "reference": reference,
                "recorded_at": at,
            },
            ("operator_id", "site_id", "delivered_at"),
        )

    def deliveries_for(
        self, operator_id: str, site_id: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        frame = self._df(
            "SELECT delivered_at, litres, reference FROM fuel_deliveries"
            " WHERE operator_id = ? AND site_id = ? AND delivered_at >= ? AND delivered_at < ?"
            " ORDER BY delivered_at",
            (operator_id, site_id, start, end),
        )
        if not frame.empty:
            frame["delivered_at"] = pd.to_datetime(frame["delivered_at"], utc=True)
        return frame

    # -- weather -------------------------------------------------------------

    def save_weather(
        self, operator_id: str, site_id: str, weather: pd.DataFrame, issued_at: datetime
    ) -> None:
        """Keep the latest weather per hour: the cache the weather model trains on."""
        frame = _weather_frame(weather).dropna(subset=["timestamp"])
        if frame.empty:
            return
        frame["issued_at"] = issued_at
        names = ["operator_id", "site_id", *frame.columns]
        rows = [
            [_py(v) for v in (operator_id, site_id, *values)]
            for values in frame.itertuples(index=False, name=None)
        ]
        query = self._sql(
            f"INSERT INTO site_weather ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})"
        )
        with self._transaction():
            self._exec(
                "DELETE FROM site_weather WHERE operator_id = ? AND site_id = ?"
                " AND timestamp >= ? AND timestamp <= ?",
                (
                    operator_id,
                    site_id,
                    frame["timestamp"].min().to_pydatetime(),
                    frame["timestamp"].max().to_pydatetime(),
                ),
            )
            if self._postgres:
                with self._con.cursor() as cur:
                    cur.executemany(query, rows)
            else:
                self._con.executemany(query, rows)

    def weather(
        self, operator_id: str, site_id: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        return self._df(
            "SELECT timestamp, shortwave_radiation, cloud_cover, temperature_2m"
            " FROM site_weather WHERE operator_id = ? AND site_id = ?"
            " AND timestamp >= ? AND timestamp < ? ORDER BY timestamp",
            (operator_id, site_id, start, end),
        ).pipe(_utc_timestamps)

    def last_weather_at(self, operator_id: str, site_id: str) -> pd.Timestamp | None:
        frame = self._df(
            "SELECT max(timestamp) AS last FROM site_weather WHERE operator_id = ? AND site_id = ?",
            (operator_id, site_id),
        )
        last = frame.iloc[0]["last"] if not frame.empty else None
        return None if last is None or pd.isna(last) else pd.Timestamp(last).tz_convert("UTC")

    def save_issued_weather(
        self,
        operator_id: str,
        site_id: str,
        plan_date: date,
        weather: pd.DataFrame,
        issued_at: datetime,
    ) -> None:
        """The weather forecast as it stood when a plan was made, kept for research.

        Backtests that use weather need forecasts as issued, not what the
        weather turned out to be; this table builds that archive day by day.
        """
        frame = _weather_frame(weather)
        frame["issued_at"] = issued_at
        key = (operator_id, site_id, plan_date)
        with self._transaction():
            self._exec(
                "DELETE FROM weather_issued WHERE operator_id = ? AND site_id = ? AND plan_date = ?",
                key,
            )
            self._insert_frame("weather_issued", key, frame)

    def issued_weather(self, operator_id: str, site_id: str, plan_date: date) -> pd.DataFrame:
        return self._df(
            "SELECT timestamp, shortwave_radiation, cloud_cover, temperature_2m, issued_at"
            " FROM weather_issued WHERE operator_id = ? AND site_id = ? AND plan_date = ?"
            " ORDER BY timestamp",
            (operator_id, site_id, plan_date),
        ).pipe(_utc_timestamps)

    def close(self) -> None:
        self._con.close()

    # -- internals -----------------------------------------------------------

    def _sql(self, query: str) -> str:
        return query.replace("?", "%s") if self._postgres else query

    def _exec(self, query: str, params: Sequence[Any] = ()) -> None:
        self._con.execute(self._sql(query), [_py(p) for p in params])

    def _df(self, query: str, params: Sequence[Any] = ()) -> pd.DataFrame:
        values = [_py(p) for p in params]
        if self._postgres:
            with self._con.cursor() as cur:
                cur.execute(self._sql(query), values)
                names = [d.name for d in (cur.description or [])]
                return pd.DataFrame(cur.fetchall(), columns=names)
        frame: pd.DataFrame = self._con.execute(query, values).df()
        return frame

    def _upsert(self, table: str, row: dict[str, Any], keys: tuple[str, ...]) -> None:
        names = list(row)
        updates = ", ".join(f"{n} = excluded.{n}" for n in names if n not in keys)
        self._exec(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})"
            f" ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}",
            [row[n] for n in names],
        )

    def _insert_frame(self, table: str, key: tuple[Any, ...], frame: pd.DataFrame) -> None:
        names = ["operator_id", "site_id", "plan_date", *frame.columns]
        query = self._sql(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})"
        )
        rows = [
            [_py(v) for v in (*key, *values)] for values in frame.itertuples(index=False, name=None)
        ]
        if self._postgres:
            with self._con.cursor() as cur:
                cur.executemany(query, rows)
        else:
            self._con.executemany(query, rows)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._postgres:
            with self._con.transaction():
                yield
        else:
            self._con.execute("BEGIN TRANSACTION")
            try:
                yield
            except BaseException:
                self._con.execute("ROLLBACK")
                raise
            self._con.execute("COMMIT")


WEATHER_COLUMNS = ("shortwave_radiation", "cloud_cover", "temperature_2m")


def _weather_frame(weather: pd.DataFrame) -> pd.DataFrame:
    frame = pd.DataFrame({"timestamp": pd.DatetimeIndex(weather["timestamp"]).tz_convert("UTC")})
    for column in WEATHER_COLUMNS:
        frame[column] = weather[column].to_numpy(dtype=float) if column in weather else float("nan")
    return frame


def _utc_timestamps(frame: pd.DataFrame) -> pd.DataFrame:
    if not frame.empty:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame


def _key_hash(secret: str) -> str:
    return hashlib.sha256(f"vaticore-api-key:{secret}".encode()).hexdigest()


def _py(value: Any) -> Any:
    """Plain Python values for the database drivers; NaN and NaT become NULL."""
    if _is_missing(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        return value.item()
    return value


def _is_missing(value: Any) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False
