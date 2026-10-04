"""Data health: is this site's data good enough to plan on today?

Real operator feeds break in ordinary ways: a logger stops, a meter sticks on
one value, a day of readings goes missing, a site loses its grid record. A
plan built on broken data is worse than no plan, because people act on it.
So every run checks the data first and says, in plain words, what it found.

Three outcomes:
  ok     plan normally.
  warn   plan, and say what is wrong in the message.
  fail   do not plan; tell the site why (no data since a stated time).

Thresholds are deliberately simple and documented here, so an operator can be
told exactly why a plan was or was not sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import pandas as pd

from vaticore.schemas import GENERATION_KW, GRID_AVAILABLE, LOAD_KW, TIMESTAMP

# Latest reading older than this before the plan starts: warn, then fail.
STALE_WARN = pd.Timedelta(hours=3)
STALE_FAIL = pd.Timedelta(hours=36)
# Share of the last week's hours missing a load reading.
MISSING_WARN = 0.10
MISSING_FAIL = 0.50
# A meter stuck on one value for this long is almost certainly faulty.
STUCK_HOURS = 12
# Minimum history to plan at all (pretrained models need about two days).
MIN_HISTORY = pd.Timedelta(days=2)


class HealthStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class HealthIssue:
    code: str
    severity: HealthStatus
    message: str  # plain words, safe to show the site team


@dataclass(frozen=True)
class HealthReport:
    status: HealthStatus
    issues: tuple[HealthIssue, ...] = field(default_factory=tuple)
    last_reading: pd.Timestamp | None = None
    history_days: float = 0.0

    @property
    def ok_to_plan(self) -> bool:
        return self.status is not HealthStatus.FAIL

    def summary(self) -> str:
        if not self.issues:
            return "Data OK."
        return " ".join(issue.message for issue in self.issues)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "issues": [
                {"code": i.code, "severity": i.severity.value, "message": i.message}
                for i in self.issues
            ],
            "last_reading": None if self.last_reading is None else self.last_reading.isoformat(),
            "history_days": round(self.history_days, 2),
        }


def check_health(
    history: pd.DataFrame,
    plan_start: pd.Timestamp,
    *,
    has_grid: bool = False,
    grid_reliable: bool = False,
    has_solar: bool = True,
    timezone: str = "UTC",
) -> HealthReport:
    """Check one site's history before planning from plan_start (UTC).

    history must contain only readings before plan_start.
    """
    issues: list[HealthIssue] = []
    observed = history.dropna(subset=[LOAD_KW]) if LOAD_KW in history else history.iloc[0:0]
    if observed.empty:
        return HealthReport(
            HealthStatus.FAIL,
            (HealthIssue("no_data", HealthStatus.FAIL, "No load readings received yet."),),
        )

    stamps = pd.DatetimeIndex(observed[TIMESTAMP])
    last = pd.Timestamp(stamps.max())
    span = last - pd.Timestamp(stamps.min())
    local_last = last.tz_convert(timezone).strftime("%d %b %H:%M")

    stale = plan_start - last - pd.Timedelta(hours=1)
    if stale > STALE_FAIL:
        issues.append(
            HealthIssue(
                "stale",
                HealthStatus.FAIL,
                f"No readings since {local_last}. Please check the site's data link.",
            )
        )
    elif stale > STALE_WARN:
        hours = int(stale / pd.Timedelta(hours=1))
        issues.append(
            HealthIssue(
                "stale",
                HealthStatus.WARN,
                f"Latest reading is {hours} h old ({local_last}); the plan may be less accurate.",
            )
        )

    if span < MIN_HISTORY:
        issues.append(
            HealthIssue(
                "short_history",
                HealthStatus.FAIL,
                "Less than two days of readings so far; plans start once there is more.",
            )
        )

    week = history[pd.DatetimeIndex(history[TIMESTAMP]) >= plan_start - pd.Timedelta(days=7)]
    expected = min(7 * 24, max(int(span / pd.Timedelta(hours=1)), 1))
    present = int(week[LOAD_KW].notna().sum()) if LOAD_KW in week else 0
    missing = max(0.0, 1.0 - present / expected)
    if missing > MISSING_FAIL:
        issues.append(
            HealthIssue(
                "missing",
                HealthStatus.FAIL,
                f"{missing:.0%} of the last week's readings are missing.",
            )
        )
    elif missing > MISSING_WARN:
        issues.append(
            HealthIssue(
                "missing",
                HealthStatus.WARN,
                f"{missing:.0%} of the last week's readings are missing.",
            )
        )

    tail = observed[LOAD_KW].to_numpy(dtype=float)[-STUCK_HOURS:]
    if tail.size >= STUCK_HOURS and np.nanmax(tail) - np.nanmin(tail) < 1e-6 and tail[0] > 0:
        issues.append(
            HealthIssue(
                "stuck_meter",
                HealthStatus.WARN,
                f"The load meter has shown {tail[0]:g} kW for {STUCK_HOURS} hours; it may be stuck.",
            )
        )

    if LOAD_KW in observed and (observed[LOAD_KW] < 0).any():
        issues.append(
            HealthIssue("negative_load", HealthStatus.WARN, "Some load readings are negative.")
        )
    if has_solar and (GENERATION_KW not in history or history[GENERATION_KW].isna().all()):
        issues.append(
            HealthIssue(
                "no_solar_record",
                HealthStatus.WARN,
                "No solar readings: the plan treats solar as zero.",
            )
        )
    if has_grid and not grid_reliable:
        grid = history[GRID_AVAILABLE] if GRID_AVAILABLE in history else pd.Series(dtype=float)
        if grid.notna().sum() == 0:
            issues.append(
                HealthIssue(
                    "no_grid_record",
                    HealthStatus.WARN,
                    "No grid on/off record: the plan does not count on the grid.",
                )
            )

    status = HealthStatus.OK
    if any(i.severity is HealthStatus.FAIL for i in issues):
        status = HealthStatus.FAIL
    elif issues:
        status = HealthStatus.WARN
    return HealthReport(status, tuple(issues), last, span / pd.Timedelta(days=1))
