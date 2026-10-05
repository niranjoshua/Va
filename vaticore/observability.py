"""Logs, error tracking and the daily job's heartbeat.

- **Logs.** `VATICORE_LOG_FORMAT=json` writes one JSON object per line
  (timestamp, level, logger, message, and any exception), which hosted log
  search (Render, Better Stack, Datadog) indexes without parsing rules. The
  default is plain text for local use.
- **Error tracking.** With `VATICORE_SENTRY_DSN` set and the `ops` extra
  installed, unhandled errors in the API and the pipeline go to Sentry, tagged
  with the environment and release. Request bodies and personal data are not
  sent: recipients appear in logs only masked.
- **Heartbeat.** `VATICORE_HEARTBEAT_URL` is pinged when the daily job
  finishes (`/fail` appended when a site failed), the convention of
  Healthchecks.io and Better Stack heartbeats. A job that never runs, or
  hangs, then raises an alert from the monitor, which an error tracker
  cannot do because nothing errors.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import httpx

from vaticore import __version__
from vaticore.config import Settings

log = logging.getLogger("vaticore")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(settings: Settings) -> None:
    """Configure the root logger once, as text or JSON."""
    handler = logging.StreamHandler()
    if settings.log_format.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())


def init_error_tracking(settings: Settings, component: str) -> bool:
    """Start Sentry if configured; returns whether it is on."""
    if settings.sentry_dsn is None:
        return False
    try:
        import sentry_sdk
    except ImportError:
        log.warning("VATICORE_SENTRY_DSN is set but sentry-sdk is not installed (ops extra)")
        return False
    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.environment,
        release=f"vaticore@{__version__}",
        send_default_pii=False,
        max_request_body_size="never",
        traces_sample_rate=0.0,
    )
    sentry_sdk.set_tag("component", component)
    return True


def heartbeat(settings: Settings, ok: bool, client: httpx.Client | None = None) -> None:
    """Tell the uptime monitor the daily job ran (and whether it succeeded)."""
    if settings.heartbeat_url is None:
        return
    url = settings.heartbeat_url.get_secret_value().rstrip("/")
    try:
        (client or httpx.Client(timeout=10.0)).get(url if ok else f"{url}/fail")
    except httpx.HTTPError as exc:  # a missed ping is itself what the monitor alerts on
        log.warning("heartbeat failed: %s", exc)
