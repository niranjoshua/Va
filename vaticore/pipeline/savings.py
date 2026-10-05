"""What the pilot saved: the sponsor's monthly report and the supervisors' week.

A pilot is judged on one question: did the sites burn less diesel, without
more outages, because of the plans? This module answers it in three layers,
from the strongest evidence to the weakest, and labels each one:

1. Measured diesel, before against during. Litres per day from each site's
   own generator readings (output through the generator's fuel curve, as in
   fuel reconciliation), in an agreed baseline period (before plans were sent)
   and in the pilot period. With control sites (sites that get no plans), the
   change at control sites is subtracted from the change at pilot sites
   (difference in differences), which removes what both groups share: grid
   supply, weather, diesel price, the season. Without control sites the
   before-and-after change is shown with the grid hours and load of both
   periods, and a warning when they moved enough to explain the change.

2. Adoption. Plan days sent, replies of 1 and 2, and the reasons given after a
   2 (generator fault, no diesel, grid was on...). A plan that could not be
   carried out is not a plan that was wrong.

3. Modelled value. Each scored day already holds what following that day's
   plan would have saved against planning from yesterday, simulated on what
   actually happened (pipeline/scoring.py). Split by days followed, not
   followed and unanswered, this separates the value taken from the value
   on offer. It is a model, not a meter, and is labelled as such.

Fuel checks (short deliveries, drops while the generator was off) and data
coverage complete the report. See docs/pilot-measurement.md for how the
periods and control sites are agreed with the operator before the pilot.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from vaticore.delivery.webhook import REASON_LABELS
from vaticore.fuel import FuelConfig, Severity, reconcile
from vaticore.pipeline.scoring import value_phrase
from vaticore.pipeline.store import SENT_STATES, PlanStore
from vaticore.schemas import GENSET_KW, GRID_AVAILABLE, LOAD_KW, TIMESTAMP
from vaticore.sites.model import Site
from vaticore.storage import TimeSeriesRepository

# A day counts as measured when the generator's output covers this many hours.
MIN_MEASURED_HOURS = 20
# Before-and-after without control sites is flagged as confounded when grid
# supply or load moved this much between the periods.
GRID_SHIFT_HOURS = 2.0
LOAD_SHIFT = 0.10


@dataclass(frozen=True)
class Period:
    """Local calendar days, first and last included."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"period ends ({self.end}) before it starts ({self.start})")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @classmethod
    def parse(cls, text: str) -> Period:
        """'2026-10-01:2026-10-31'."""
        first, sep, last = text.partition(":")
        if not sep:
            raise ValueError(f"a period is FIRST:LAST, e.g. 2026-10-01:2026-10-31, got {text!r}")
        return cls(date.fromisoformat(first), date.fromisoformat(last))

    def label(self) -> str:
        return f"{self.start:%d %b %Y} to {self.end:%d %b %Y}"

    def utc_bounds(self, timezone: str) -> tuple[pd.Timestamp, pd.Timestamp]:
        start = pd.Timestamp(self.start).tz_localize(timezone).tz_convert("UTC")
        end = pd.Timestamp(self.end + timedelta(days=1)).tz_localize(timezone).tz_convert("UTC")
        return start, end


# -- per site, per day: what was planned, sent, answered and worth --------------


def site_days(store: PlanStore, sites: Sequence[Site], period: Period) -> pd.DataFrame:
    """One row per site and plan day in the period.

    Columns: operator_id, site_id, plan_date, status (planned, no_plan, failed
    or missing), sent, response (followed, not_followed or none), reason,
    scored, hours_missing, litres_saved, outage_kwh_avoided, fuel_value_saved,
    net_value_saved (diesel, grid and outages priced as in the site record) and
    litres_on_offer_perfect. Savings are modelled, against the baseline plan.
    """
    rows = []
    for site in sites:
        runs = store.runs(site.operator_id, site.site_id)
        runs = _in_period(runs, period)
        scores = _in_period(store.scores(site.operator_id, site.site_id), period)
        deliveries = store.deliveries(site.operator_id)
        deliveries = _in_period(deliveries[deliveries["site_id"] == site.site_id], period)
        feedback = store.feedback(site.operator_id)
        feedback = _in_period(feedback[feedback["site_id"] == site.site_id], period)
        price = site.generator.fuel_price_per_l if site.generator else 0.0
        for day in pd.date_range(period.start, period.end, freq="D").date:
            run = runs[runs["plan_date"] == day]
            score = scores[scores["plan_date"] == day]
            sent = deliveries[
                (deliveries["plan_date"] == day) & deliveries["status"].isin(SENT_STATES)
            ]
            response, reason = _response(feedback[feedback["plan_date"] == day])
            row: dict[str, Any] = {
                "operator_id": site.operator_id,
                "site_id": site.site_id,
                "plan_date": day,
                "status": str(run.iloc[0]["status"]) if not run.empty else "missing",
                "sent": not sent.empty,
                "response": response,
                "reason": reason,
                "scored": not score.empty,
                "hours_missing": np.nan,
                "litres_saved": np.nan,
                "outage_kwh_avoided": np.nan,
                "fuel_value_saved": np.nan,
                "net_value_saved": np.nan,
                "litres_on_offer_perfect": np.nan,
            }
            if not score.empty:
                s = score.iloc[0]
                saved = float(s["fuel_baseline_l"] - s["fuel_plan_l"])
                row.update(
                    hours_missing=float(s["hours_missing"]),
                    litres_saved=saved,
                    outage_kwh_avoided=float(s["unserved_baseline_kwh"] - s["unserved_plan_kwh"]),
                    fuel_value_saved=saved * price,
                    net_value_saved=float(s["cost_baseline"] - s["cost_plan"]),
                    litres_on_offer_perfect=float(s["fuel_baseline_l"] - s["fuel_perfect_l"]),
                )
            rows.append(row)
    return pd.DataFrame(rows)


def _in_period(frame: pd.DataFrame, period: Period) -> pd.DataFrame:
    if frame.empty or "plan_date" not in frame:
        return frame.assign(plan_date=pd.Series(dtype=object)) if frame.empty else frame
    days = pd.to_datetime(frame["plan_date"]).dt.date
    frame = frame.assign(plan_date=days)
    return frame[(days >= period.start) & (days <= period.end)]


def _response(feedback: pd.DataFrame) -> tuple[str, str | None]:
    """The day's answer: the latest 1 or 2, and the latest reason given."""
    if feedback.empty:
        return "none", None
    ordered = feedback.sort_values("received_at")
    answers = ordered[ordered["kind"].isin(["followed", "not_followed"])]
    response = str(answers.iloc[-1]["kind"]) if not answers.empty else "none"
    reasons = ordered[ordered["kind"] == "reason"]
    reason = None
    if response == "not_followed" and not reasons.empty and "reason" in reasons:
        value = reasons.iloc[-1]["reason"]
        reason = None if pd.isna(value) else str(value)
    return response, reason


# -- measured diesel -------------------------------------------------------------


@dataclass(frozen=True)
class MeasuredPeriod:
    """One site's measured generator use over one period."""

    days_measured: int
    litres_per_day: float | None
    genset_hours_per_day: float | None
    grid_hours_per_day: float | None
    load_kwh_per_day: float | None
    reading_coverage: float  # share of hours with a load reading


def measure(
    site: Site, repo: TimeSeriesRepository, store: PlanStore, period: Period
) -> MeasuredPeriod:
    """Litres a day from the site's own generator readings, and the context."""
    start, end = period.utc_bounds(site.timezone)
    readings = repo.read_history(site.operator_id, site.site_id, start, end)
    hours = pd.date_range(start, end, freq="1h", inclusive="left")
    hourly = (
        readings.set_index(TIMESTAMP)
        .drop(columns=["operator_id", "site_id"], errors="ignore")
        .select_dtypes("number")
        .resample("1h")
        .mean()
        .reindex(hours)
        if not readings.empty
        else pd.DataFrame(index=hours)
    )
    coverage = float(hourly[LOAD_KW].notna().mean()) if LOAD_KW in hourly else 0.0
    load = _per_day(hourly.get(LOAD_KW))
    grid = (
        _per_day(hourly.get(GRID_AVAILABLE))
        if site.grid is not None and not site.grid.reliable
        else None
    )
    if site.generator is None or GENSET_KW not in hourly:
        return MeasuredPeriod(0, None, None, grid, load, coverage)
    deliveries = store.deliveries_for(
        site.operator_id, site.site_id, start.to_pydatetime(), end.to_pydatetime()
    )
    daily = reconcile(site, readings, deliveries, start=start, end=end).daily
    measured = daily[daily["burn_hours"] >= MIN_MEASURED_HOURS]
    output = hourly[GENSET_KW]
    running = (output > FuelConfig().off_below * site.generator.rated_kw).where(output.notna())
    run_hours = _per_day(running.astype(float))
    if measured.empty:
        return MeasuredPeriod(0, None, run_hours, grid, load, coverage)
    litres = float(measured["burned_l"].mean())
    return MeasuredPeriod(len(measured), litres, run_hours, grid, load, coverage)


def _per_day(hourly: pd.Series | None) -> float | None:
    """Mean of an hourly series, as a daily total (hours or kWh a day)."""
    if hourly is None or hourly.notna().sum() < 24:
        return None
    return float(hourly.mean() * 24)


@dataclass(frozen=True)
class SiteResult:
    site: Site
    group: str  # pilot or control
    baseline: MeasuredPeriod
    pilot: MeasuredPeriod

    @property
    def comparable(self) -> bool:
        return self.baseline.litres_per_day is not None and self.pilot.litres_per_day is not None

    @property
    def change_l_per_day(self) -> float | None:
        if not self.comparable:
            return None
        return _litres(self.pilot) - _litres(self.baseline)


@dataclass(frozen=True)
class GroupChange:
    """Litres a day summed over a group's comparable sites, before and during."""

    sites: int
    baseline_l_per_day: float
    pilot_l_per_day: float

    @property
    def change(self) -> float | None:
        if self.baseline_l_per_day <= 0:
            return None
        return self.pilot_l_per_day / self.baseline_l_per_day - 1


def _group(results: Sequence[SiteResult], group: str) -> GroupChange | None:
    chosen = [r for r in results if r.group == group and r.comparable]
    if not chosen:
        return None
    return GroupChange(
        len(chosen),
        float(sum(_litres(r.baseline) for r in chosen)),
        float(sum(_litres(r.pilot) for r in chosen)),
    )


# -- the report ------------------------------------------------------------------


@dataclass(frozen=True)
class SavingsReport:
    operator_id: str
    currency: str
    baseline: Period
    pilot: Period
    sites: tuple[SiteResult, ...]
    pilot_days: pd.DataFrame  # site_days() for pilot sites in the pilot period
    shadow_days: pd.DataFrame  # site_days() for pilot sites in the baseline period
    fuel_flags: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    # Headline: measured litres a day saved across the pilot sites, and how.
    @property
    def pilot_group(self) -> GroupChange | None:
        return _group(self.sites, "pilot")

    @property
    def control_group(self) -> GroupChange | None:
        return _group(self.sites, "control")

    @property
    def method(self) -> str:
        if self.pilot_group is None:
            return "none"
        return "difference_in_differences" if self.control_group else "before_after"

    @property
    def measured_change(self) -> float | None:
        """Relative change in litres a day at pilot sites, net of control sites if any."""
        pilot = self.pilot_group
        if pilot is None or pilot.change is None:
            return None
        control = self.control_group
        if control is not None and control.change is not None:
            return pilot.change - control.change
        return pilot.change

    @property
    def measured_litres_saved_per_day(self) -> float | None:
        change = self.measured_change
        pilot = self.pilot_group
        if change is None or pilot is None:
            return None
        return -change * pilot.baseline_l_per_day

    @property
    def measured_value_saved(self) -> float | None:
        """Over the whole pilot period, at each site's diesel price."""
        litres = self.measured_litres_saved_per_day
        pilot = self.pilot_group
        if litres is None or pilot is None or pilot.baseline_l_per_day <= 0:
            return None
        comparable = [r for r in self.sites if r.group == "pilot" and r.comparable]
        weighted_price = (
            sum(_litres(r.baseline) * _price(r.site) for r in comparable) / pilot.baseline_l_per_day
        )
        return litres * weighted_price * self.pilot.days

    def adoption(self) -> dict[str, Any]:
        days = self.pilot_days
        planned = days[days["status"] == "planned"] if not days.empty else days
        sent = planned[planned["sent"]] if not planned.empty else planned
        counts = sent["response"].value_counts() if not sent.empty else pd.Series(dtype=int)
        reasons = (
            sent.loc[sent["response"] == "not_followed", "reason"].fillna("not given")
            if not sent.empty
            else pd.Series(dtype=object)
        )
        return {
            "plan_days": len(planned),
            "sent": len(sent),
            "followed": int(counts.get("followed", 0)),
            "not_followed": int(counts.get("not_followed", 0)),
            "no_reply": int(counts.get("none", 0)),
            "reasons": {str(k): int(v) for k, v in reasons.value_counts().items()},
        }

    def modelled(self, days: pd.DataFrame | None = None) -> dict[str, dict[str, float]]:
        """Modelled savings against planning from yesterday, by the day's answer."""
        days = self.pilot_days if days is None else days
        out: dict[str, dict[str, float]] = {}
        scored = days[days["scored"]] if not days.empty else days
        for label, mask in (
            ("all", slice(None)),
            ("followed", scored.get("response") == "followed"),
            ("not_followed", scored.get("response") == "not_followed"),
            ("no_reply", scored.get("response") == "none"),
        ):
            chosen = scored.loc[mask] if not scored.empty else scored
            out[label] = {
                "days": float(len(chosen)),
                "litres_saved": float(chosen["litres_saved"].sum()) if len(chosen) else 0.0,
                "fuel_value_saved": float(chosen["fuel_value_saved"].sum()) if len(chosen) else 0.0,
                "net_value_saved": float(chosen["net_value_saved"].sum()) if len(chosen) else 0.0,
                "outage_kwh_avoided": (
                    float(chosen["outage_kwh_avoided"].sum()) if len(chosen) else 0.0
                ),
                "litres_on_offer_perfect": (
                    float(chosen["litres_on_offer_perfect"].sum()) if len(chosen) else 0.0
                ),
            }
        return out

    def to_dict(self) -> dict[str, Any]:
        def period(p: Period) -> dict[str, Any]:
            return {"start": p.start.isoformat(), "end": p.end.isoformat(), "days": p.days}

        def measured(m: MeasuredPeriod) -> dict[str, Any]:
            return {
                "days_measured": m.days_measured,
                "litres_per_day": m.litres_per_day,
                "genset_hours_per_day": m.genset_hours_per_day,
                "grid_hours_per_day": m.grid_hours_per_day,
                "load_kwh_per_day": m.load_kwh_per_day,
                "reading_coverage": m.reading_coverage,
            }

        def group(g: GroupChange | None) -> dict[str, Any] | None:
            if g is None:
                return None
            return {
                "sites": g.sites,
                "baseline_l_per_day": g.baseline_l_per_day,
                "pilot_l_per_day": g.pilot_l_per_day,
                "change": g.change,
            }

        return {
            "operator_id": self.operator_id,
            "currency": self.currency,
            "baseline": period(self.baseline),
            "pilot": period(self.pilot),
            "method": self.method,
            "measured": {
                "pilot_group": group(self.pilot_group),
                "control_group": group(self.control_group),
                "change": self.measured_change,
                "litres_saved_per_day": self.measured_litres_saved_per_day,
                "value_saved": self.measured_value_saved,
            },
            "sites": [
                {
                    "site_id": r.site.site_id,
                    "name": r.site.name,
                    "group": r.group,
                    "baseline": measured(r.baseline),
                    "pilot": measured(r.pilot),
                    "change_l_per_day": r.change_l_per_day,
                }
                for r in self.sites
            ],
            "adoption": self.adoption(),
            "modelled": self.modelled(),
            "modelled_shadow": self.modelled(self.shadow_days),
            "fuel_flags": list(self.fuel_flags),
            "warnings": list(self.warnings),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def to_markdown(self) -> str:
        cur = self.currency
        lines = [
            f"# Vaticore pilot report: {self.operator_id}",
            "",
            f"Pilot period {self.pilot.label()} ({self.pilot.days} days), against a baseline "
            f"of {self.baseline.label()} ({self.baseline.days} days) before plans were sent.",
            "",
            "## Diesel, measured at the sites",
            "",
        ]
        litres = self.measured_litres_saved_per_day
        change = self.measured_change
        if litres is None or change is None:
            lines.append(
                "Not enough generator readings in both periods to measure a change yet "
                f"(a day counts when its generator output covers {MIN_MEASURED_HOURS} hours)."
            )
        else:
            verb = "less" if litres >= 0 else "more"
            value = self.measured_value_saved or 0.0
            lines.append(
                f"Pilot sites burned **{abs(change):.0%} {verb} diesel**: about "
                f"**{abs(litres):,.0f} L a day {verb}**, "
                + (
                    f"or **{cur} {abs(value):,.0f}** over the pilot period"
                    if litres >= 0
                    else f"costing **{cur} {abs(value):,.0f}** more over the pilot period"
                )
                + "."
            )
            lines.append("")
            pilot = self.pilot_group
            control = self.control_group
            assert pilot is not None
            if control is not None and control.change is not None and pilot.change is not None:
                lines.append(
                    f"Method: difference in differences. Pilot sites ({pilot.sites}) changed "
                    f"by {pilot.change:+.1%}; control sites ({control.sites}), which received "
                    f"no plans, changed by {control.change:+.1%} over the same weeks. The "
                    "difference is credited to the plans; what both groups shared (grid "
                    "supply, weather, prices) cancels out."
                )
            else:
                lines.append(
                    f"Method: before and after, at {pilot.sites} pilot site(s), with no control "
                    "sites. Anything else that changed between the periods (grid supply, "
                    "weather, load) is mixed into this number; see the context columns below."
                )
        lines += [
            "",
            "| Site | Group | Litres a day, before | During | Change | Generator hours a day, "
            "before / during | Grid hours a day, before / during | Load kWh a day, before / during |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in self.sites:
            lines.append(
                f"| {r.site.name} | {r.group} | {_n(r.baseline.litres_per_day)} | "
                f"{_n(r.pilot.litres_per_day)} | {_n(r.change_l_per_day, sign=True)} | "
                f"{_n(r.baseline.genset_hours_per_day, 1)} / {_n(r.pilot.genset_hours_per_day, 1)} | "
                f"{_n(r.baseline.grid_hours_per_day, 1)} / {_n(r.pilot.grid_hours_per_day, 1)} | "
                f"{_n(r.baseline.load_kwh_per_day)} / {_n(r.pilot.load_kwh_per_day)} |"
            )
        lines.append("")
        lines.append(
            "Litres are estimated from each generator's recorded output and its fuel curve. "
            "Fuel that left the tank by other routes is under fuel checks, not here."
        )

        adoption = self.adoption()
        lines += ["", "## Were the plans followed?", ""]
        if adoption["sent"] == 0:
            lines.append("No plans were sent in the pilot period.")
        else:
            answered = adoption["followed"] + adoption["not_followed"]
            lines.append(
                f"{adoption['sent']} plan days sent. Followed on **{adoption['followed']}**, "
                f"not followed on **{adoption['not_followed']}**, no reply on "
                f"{adoption['no_reply']}"
                + (
                    f" ({adoption['followed'] / answered:.0%} of answered days followed)."
                    if answered
                    else "."
                )
            )
            if adoption["reasons"]:
                lines += ["", "Why plans were not followed:", ""]
                for code, count in sorted(adoption["reasons"].items(), key=lambda kv: -kv[1]):
                    lines.append(f"- {REASON_LABELS.get(code, code)}: {count}")

        modelled = self.modelled()
        lines += [
            "",
            "## What following the plans was worth (modelled)",
            "",
            "Each finished day is replayed on what actually happened at the site: once with "
            "that day's plan, once with planning from yesterday (what sites do without "
            "Vaticore). This is a model of the value, not a meter reading. Net saving prices "
            "diesel, grid power and outages as in each site's record, so a plan that burns "
            "more diesel to avoid outages can still save money.",
            "",
            f"| Days | Number | Diesel saved (L) | Diesel saved ({cur}) | Outages avoided (kWh) "
            f"| Net saving ({cur}) |",
            "|---|---|---|---|---|---|",
        ]
        for key, label in (
            ("followed", "Plan followed"),
            ("not_followed", "Plan not followed"),
            ("no_reply", "No reply"),
            ("all", "All scored days"),
        ):
            m = modelled[key]
            lines.append(
                f"| {label} | {m['days']:.0f} | {m['litres_saved']:,.0f} | "
                f"{m['fuel_value_saved']:,.0f} | {m['outage_kwh_avoided']:,.0f} | "
                f"{m['net_value_saved']:,.0f} |"
            )
        lines += [
            "",
            "Value taken is the 'plan followed' row; the other rows are value on offer that "
            "was not taken.",
        ]
        shadow = self.modelled(self.shadow_days)["all"]
        if shadow["days"]:
            lines.append(
                f"In the baseline period, the plans were made but not sent; over "
                f"{shadow['days']:.0f} scored days, following them would have "
                f"{value_phrase(shadow['litres_saved'], shadow['outage_kwh_avoided'])}."
            )

        lines += ["", "## Fuel checks in the pilot period", ""]
        if not self.fuel_flags:
            lines.append("Nothing flagged: deliveries, tanks and generators agree.")
        else:
            total = sum(float(f["litres"]) for f in self.fuel_flags)
            value = sum(float(f["value"]) for f in self.fuel_flags)
            lines.append(
                f"{len(self.fuel_flags)} finding(s), {total:,.0f} L, about {cur} {value:,.0f}, "
                "to check with the sites:"
            )
            lines.append("")
            for f in self.fuel_flags:
                lines.append(f"- {f['site']}: [{f['severity']}] {f['message']}")

        days = self.pilot_days
        lines += ["", "## Data", ""]
        if not days.empty:
            missing = days[days["status"] != "planned"]
            scored = days[days["scored"]]
            lines.append(
                f"{len(days) - len(missing)} of {len(days)} site-days had a plan; "
                f"{len(scored)} were scored against readings."
            )
        for r in self.sites:
            if r.pilot.reading_coverage < 0.9:
                lines.append(
                    f"- {r.site.name}: readings for {r.pilot.reading_coverage:.0%} of hours "
                    "in the pilot period."
                )
        if self.warnings:
            lines += ["", "## Read with care", ""]
            lines += [f"- {w}" for w in self.warnings]
        lines += ["", "Advisory only: every plan was a recommendation; site teams decided."]
        return "\n".join(lines) + "\n"


def savings_report(
    sites: Sequence[Site],
    repo: TimeSeriesRepository,
    store: PlanStore,
    *,
    baseline: Period,
    pilot: Period,
    control: Sequence[str] = (),
) -> SavingsReport:
    """The pilot's savings for one operator's sites. `control` names site_ids sent no plans."""
    if not sites:
        raise ValueError("no sites to report on")
    operators = {s.operator_id for s in sites}
    if len(operators) != 1:
        raise ValueError(f"one operator per report, got {sorted(operators)}")
    unknown = set(control) - {s.site_id for s in sites}
    if unknown:
        raise ValueError(f"control site(s) not in the portfolio: {sorted(unknown)}")
    currencies = {s.currency for s in sites}
    if len(currencies) != 1:
        raise ValueError(f"sites priced in more than one currency: {sorted(currencies)}")
    if baseline.end >= pilot.start:
        raise ValueError("the baseline period must end before the pilot period starts")

    warnings: list[str] = []
    results = []
    for site in sites:
        group = "control" if site.site_id in control else "pilot"
        results.append(
            SiteResult(
                site,
                group,
                measure(site, repo, store, baseline),
                measure(site, repo, store, pilot),
            )
        )
    pilot_sites = [r.site for r in results if r.group == "pilot"]
    control_sites = [r.site for r in results if r.group == "control"]

    if control_sites:
        sent = site_days(store, control_sites, pilot)
        if not sent.empty and sent["sent"].any():
            warnings.append(
                "Plans were sent to control site(s) during the pilot; they are not a clean "
                "comparison: " + ", ".join(sorted(set(sent.loc[sent["sent"], "site_id"])))
            )
    else:
        warnings.extend(_confounders(results))
    for r in results:
        if not r.comparable:
            warnings.append(
                f"{r.site.name}: too few days with generator readings to compare "
                f"({r.baseline.days_measured} before, {r.pilot.days_measured} during)."
            )

    flags = []
    for site in pilot_sites:
        if site.generator is None:
            continue
        start, end = pilot.utc_bounds(site.timezone)
        readings = repo.read_history(site.operator_id, site.site_id, start, end)
        deliveries = store.deliveries_for(
            site.operator_id, site.site_id, start.to_pydatetime(), end.to_pydatetime()
        )
        report = reconcile(site, readings, deliveries, start=start, end=end)
        for f in report.findings:
            if f.severity is Severity.INFO or f.litres <= 0:
                continue
            flags.append(
                {
                    "site": site.name,
                    "site_id": site.site_id,
                    "code": f.code,
                    "severity": f.severity.value,
                    "litres": f.litres,
                    "value": f.litres * report.price_per_l,
                    "message": f.message,
                }
            )

    return SavingsReport(
        operator_id=next(iter(operators)),
        currency=next(iter(currencies)),
        baseline=baseline,
        pilot=pilot,
        sites=tuple(results),
        pilot_days=site_days(store, pilot_sites, pilot),
        shadow_days=site_days(store, pilot_sites, baseline),
        fuel_flags=tuple(flags),
        warnings=tuple(warnings),
    )


def _confounders(results: Sequence[SiteResult]) -> list[str]:
    """Without control sites, say when grid supply or load moved enough to matter."""
    out = []
    chosen = [r for r in results if r.group == "pilot" and r.comparable]
    grid = [
        (r.baseline.grid_hours_per_day, r.pilot.grid_hours_per_day)
        for r in chosen
        if r.baseline.grid_hours_per_day is not None and r.pilot.grid_hours_per_day is not None
    ]
    if grid:
        shift = float(np.mean([after - before for before, after in grid]))
        if abs(shift) >= GRID_SHIFT_HOURS:
            out.append(
                f"Grid supply changed by {shift:+.1f} hours a day on average between the "
                "periods, which by itself changes diesel use. Treat the measured change as "
                "an upper or lower bound, or add control sites."
            )
    load = [
        (r.baseline.load_kwh_per_day, r.pilot.load_kwh_per_day)
        for r in chosen
        if r.baseline.load_kwh_per_day and r.pilot.load_kwh_per_day is not None
    ]
    if load:
        before = sum(b for b, _ in load)
        after = sum(a for _, a in load)
        if before > 0 and abs(after / before - 1) >= LOAD_SHIFT:
            out.append(
                f"Site load changed by {after / before - 1:+.0%} between the periods; part of "
                "the change in diesel use is the load, not the plans."
            )
    return out


def _litres(measured: MeasuredPeriod) -> float:
    return 0.0 if measured.litres_per_day is None else float(measured.litres_per_day)


def _price(site: Site) -> float:
    return site.generator.fuel_price_per_l if site.generator else 0.0


def _n(value: float | None, decimals: int = 0, *, sign: bool = False) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "n/a"
    return f"{value:{'+' if sign else ''},.{decimals}f}"
