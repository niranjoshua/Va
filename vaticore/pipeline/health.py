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

from vaticore.schemas import BATTERY_SOC_PCT, GENERATION_KW, GRID_AVAILABLE, LOAD_KW, TIMESTAMP

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


# -- the per-site health report ------------------------------------------------


@dataclass(frozen=True)
class SiteHealthReport:
    """A site's data over recent weeks: what is there, what is wrong, what to fix."""

    operator_id: str
    site_id: str
    start: pd.Timestamp
    end: pd.Timestamp
    resolution: pd.Timedelta | None
    coverage: dict[str, float]  # share of hours with a reading, per column
    worst_days: list[tuple[str, float]]  # (local date, load coverage), lowest first
    longest_gap_hours: float
    findings: tuple[HealthIssue, ...]

    @property
    def status(self) -> HealthStatus:
        if any(f.severity is HealthStatus.FAIL for f in self.findings):
            return HealthStatus.FAIL
        return HealthStatus.WARN if self.findings else HealthStatus.OK

    def to_text(self) -> str:
        lines = [
            f"Data health for {self.operator_id}/{self.site_id}, "
            f"{self.start:%d %b} to {self.end:%d %b %Y}: {self.status.value.upper()}",
            "Resolution: " + ("unknown" if self.resolution is None else _duration(self.resolution)),
            "Coverage: "
            + ", ".join(f"{name} {share:.0%}" for name, share in self.coverage.items()),
            f"Longest gap in load readings: {self.longest_gap_hours:.0f} h",
        ]
        if self.worst_days:
            lines.append(
                "Worst days: " + ", ".join(f"{day} {share:.0%}" for day, share in self.worst_days)
            )
        for finding in self.findings:
            lines.append(f"- [{finding.severity.value}] {finding.message}")
        if not self.findings:
            lines.append("- No problems found.")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "operator_id": self.operator_id,
            "site_id": self.site_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "status": self.status.value,
            "resolution_minutes": None
            if self.resolution is None
            else self.resolution / pd.Timedelta(minutes=1),
            "coverage": self.coverage,
            "worst_days": self.worst_days,
            "longest_gap_hours": self.longest_gap_hours,
            "findings": [
                {"code": f.code, "severity": f.severity.value, "message": f.message}
                for f in self.findings
            ],
        }


def site_health_report(
    raw: pd.DataFrame,
    *,
    operator_id: str,
    site_id: str,
    end: pd.Timestamp,
    days: int = 30,
    timezone: str = "UTC",
    longitude: float | None = None,
    has_solar: bool = True,
    has_grid: bool = False,
    has_battery: bool = True,
) -> SiteHealthReport:
    """Inspect a site's raw readings over the last `days` days."""
    start = end - pd.Timedelta(days=days)
    frame = raw[(raw[TIMESTAMP] >= start) & (raw[TIMESTAMP] < end)].sort_values(TIMESTAMP)
    stamps = pd.DatetimeIndex(frame[TIMESTAMP])
    resolution = (
        pd.Timedelta(stamps.to_series().diff().dropna().median()) if len(stamps) > 2 else None
    )
    hours = pd.date_range(start.ceil("h"), end, freq="1h", inclusive="left")
    hourly = (
        frame.set_index(TIMESTAMP).select_dtypes("number").resample("1h").mean().reindex(hours)
        if not frame.empty
        else pd.DataFrame(index=hours)
    )
    columns = [LOAD_KW]
    if has_solar:
        columns.append(GENERATION_KW)
    if has_grid:
        columns.append(GRID_AVAILABLE)
    if has_battery:
        columns.append(BATTERY_SOC_PCT)
    coverage = {
        c: float(hourly[c].notna().mean()) if c in hourly and len(hourly) else 0.0 for c in columns
    }

    findings: list[HealthIssue] = []
    load = hourly[LOAD_KW] if LOAD_KW in hourly else pd.Series(np.nan, index=hours)
    if load.notna().sum() == 0:
        findings.append(
            HealthIssue("no_data", HealthStatus.FAIL, "No load readings in the period.")
        )
    if coverage.get(LOAD_KW, 0.0) < 1.0 - MISSING_WARN and load.notna().any():
        findings.append(
            HealthIssue(
                "missing",
                HealthStatus.WARN,
                f"Load readings cover {coverage[LOAD_KW]:.0%} of hours (aim for 90% or more).",
            )
        )

    gaps = _runs(load.isna().to_numpy())
    longest_gap = float(max(gaps, default=0))

    local = load.copy()
    local.index = pd.DatetimeIndex(local.index).tz_convert(timezone)
    daily = local.notna().groupby(local.index.date).mean()
    worst = [(f"{day:%d %b}", float(share)) for day, share in daily.sort_values().items()]
    worst_days = [w for w in worst if w[1] < 0.9][:5]

    stuck = _runs(_unchanged(load.to_numpy()))
    if max(stuck, default=0) >= STUCK_HOURS:
        findings.append(
            HealthIssue(
                "stuck_meter",
                HealthStatus.WARN,
                f"The load reading did not change for {max(stuck)} hours in a row at least "
                "once: check the meter.",
            )
        )
    if (load < 0).any():
        findings.append(HealthIssue("negative_load", HealthStatus.WARN, "Negative load readings."))
    typical = load.quantile(0.99) if load.notna().any() else np.nan
    spikes = int((load > 3 * typical).sum()) if typical and typical > 0 else 0
    if spikes:
        findings.append(
            HealthIssue(
                "spikes",
                HealthStatus.WARN,
                f"{spikes} load reading(s) above three times the usual peak: likely glitches.",
            )
        )

    if has_solar and GENERATION_KW in hourly and hourly[GENERATION_KW].notna().sum() > 48:
        solar = hourly[GENERATION_KW]
        findings.extend(_solar_checks(solar, timezone, longitude))

    if has_grid and coverage.get(GRID_AVAILABLE, 0.0) < 0.5:
        findings.append(
            HealthIssue(
                "no_grid_record",
                HealthStatus.WARN,
                "Grid on/off is recorded for under half the hours: plans will not count on "
                "the grid where it is unknown.",
            )
        )
    if has_battery and coverage.get(BATTERY_SOC_PCT, 0.0) == 0.0:
        findings.append(
            HealthIssue(
                "no_battery_charge",
                HealthStatus.WARN,
                "No battery charge readings: plans assume the battery starts half full.",
            )
        )
    return SiteHealthReport(
        operator_id,
        site_id,
        start,
        end,
        resolution,
        coverage,
        worst_days,
        longest_gap,
        tuple(findings),
    )


def _solar_checks(solar: pd.Series, timezone: str, longitude: float | None) -> list[HealthIssue]:
    findings = []
    peak = solar.max()
    if not peak or peak <= 0:
        return [HealthIssue("no_solar", HealthStatus.WARN, "Solar readings are all zero.")]
    hours_utc = pd.DatetimeIndex(solar.index).hour
    # Mean solar output by hour of day (UTC); its centre of mass is solar noon.
    profile = solar.groupby(hours_utc).mean().reindex(range(24), fill_value=0.0).fillna(0.0)
    weights = profile.clip(lower=0.0)
    if weights.sum() > 0 and longitude is not None:
        noon_utc = float((weights * (weights.index + 0.5)).sum() / weights.sum())
        expected = 12.0 - longitude / 15.0
        offset = noon_utc - expected
        if abs(offset) >= 0.75:
            local = pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(hours=noon_utc)
            findings.append(
                HealthIssue(
                    "timezone",
                    HealthStatus.WARN,
                    f"Solar peaks around {local.tz_convert(timezone):%H:%M} local time instead "
                    f"of near midday: timestamps are probably {abs(offset):.0f} h "
                    f"{'late' if offset > 0 else 'early'}, often a local-time export labelled "
                    "as UTC. Check the source's timezone setting.",
                )
            )
    local_hours = pd.DatetimeIndex(solar.index).tz_convert(timezone).hour
    night = solar[(local_hours >= 21) | (local_hours < 4)]
    if night.notna().any() and (night > 0.05 * peak).mean() > 0.05:
        findings.append(
            HealthIssue(
                "solar_at_night",
                HealthStatus.WARN,
                "Solar output appears at night: timestamps or the solar channel look wrong.",
            )
        )
    return findings


def _runs(mask: np.ndarray) -> list[int]:
    """Lengths of consecutive True runs."""
    runs, current = [], 0
    for flag in mask:
        if flag:
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    return runs


def _unchanged(values: np.ndarray) -> np.ndarray:
    """True where a reading equals the previous one (and both exist and are positive)."""
    same = np.zeros(len(values), dtype=bool)
    if len(values) > 1:
        prev, cur = values[:-1], values[1:]
        same[1:] = (cur == prev) & ~np.isnan(cur) & (cur > 0)
    return same


def _duration(delta: pd.Timedelta) -> str:
    minutes = delta / pd.Timedelta(minutes=1)
    return f"{minutes:.0f} min" if minutes < 60 else f"{minutes / 60:g} h"
