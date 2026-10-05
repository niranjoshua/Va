"""Typed, environment driven settings.

All configuration flows through here so behaviour is reproducible and no module
reaches for os.environ directly. Values load from the process environment and
from a local .env file (see .env.example).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global application settings.

    Secrets and deployment specific values belong here, never hard coded.
    """

    model_config = SettingsConfigDict(
        env_prefix="VATICORE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Runtime
    environment: str = Field(default="local", description="local, staging or production.")
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="text", description="text, or json for hosted log search")
    # Error tracking (Sentry; needs the ops extra) and the daily job's heartbeat
    # (Healthchecks.io or Better Stack): see vaticore/observability.py.
    sentry_dsn: SecretStr | None = Field(default=None)
    heartbeat_url: SecretStr | None = Field(default=None)
    # A scratch database that backup checks restore into (never production).
    restore_test_url: SecretStr | None = Field(default=None)

    # Storage. DuckDB is fine for local development and early backtests.
    # Point this at Postgres/TimescaleDB in staging and production.
    database_url: str = Field(default="duckdb:///vaticore.duckdb")

    # Weather provider for live forecasts: "open-meteo" or "none". Open-Meteo's
    # free API is for non-commercial use only; commercial use needs an API
    # subscription key. Without a key, production runs skip live weather.
    weather_provider: str = Field(default="open-meteo")
    weather_api_key: SecretStr | None = Field(default=None)

    # Experiment tracking.
    mlflow_tracking_uri: str | None = Field(default=None)

    # Default quantiles produced across the system.
    default_quantiles: tuple[float, ...] = Field(default=(0.1, 0.5, 0.9))

    # Daily pipeline. The portfolio (sites and assets) and the recipients file
    # (phone numbers: personal data, keep outside the repository).
    portfolio_file: Path | None = Field(default=None)
    recipients_file: Path | None = Field(default=None)
    # Plans and scores; defaults to the observation database.
    plan_store_url: str | None = Field(default=None)

    # WhatsApp Business Cloud API (Meta). See docs/whatsapp-setup.md.
    whatsapp_token: SecretStr | None = Field(default=None)
    whatsapp_phone_number_id: str | None = Field(default=None)
    whatsapp_api_version: str = Field(default="v23.0")
    whatsapp_template_name: str = Field(default="vaticore_daily_plan")
    whatsapp_template_language: str = Field(default="en")
    # Webhook security: the app secret signs every callback; the verify token
    # is the shared word Meta sends when the webhook is first registered.
    whatsapp_app_secret: SecretStr | None = Field(default=None)
    whatsapp_verify_token: SecretStr | None = Field(default=None)

    # Admin bearer token for the API (all operators). Operators get their own
    # keys (python -m vaticore.pipeline apikey create), which only reach their
    # own sites. Required in production: plans are operator data.
    api_token: SecretStr | None = Field(default=None)

    # Email delivery over SMTP (any provider: Google Workspace, Microsoft 365,
    # Zoho, Amazon SES, Postmark).
    smtp_host: str | None = Field(default=None)
    smtp_port: int = Field(default=587)
    smtp_username: str | None = Field(default=None)
    smtp_password: SecretStr | None = Field(default=None)
    smtp_ssl: bool = Field(default=False, description="SSL from the start (port 465)")
    email_from: str | None = Field(default=None, description="for example plans@vaticore.com")
    email_reply_to: str | None = Field(default=None)

    # Monitoring connectors: which source each site's readings come from.
    sources_file: Path | None = Field(default=None)

    # Where model monitoring alerts go (a model suspended or reinstated), by
    # email over the SMTP settings above. Unset: alerts are logged only.
    ops_email: str | None = Field(default=None)


def get_settings() -> Settings:
    """Return a fresh Settings instance.

    Kept as a function (not a module level singleton) so tests can override the
    environment and construct isolated settings.
    """
    return Settings()


PROJECT_ROOT = Path(__file__).resolve().parent.parent
