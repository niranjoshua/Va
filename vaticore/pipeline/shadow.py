"""The shadow weeks' verdict: send plans to these sites, or not yet?

Before any plan reaches a technician, the pilot runs for four weeks with plans
made, stored and scored but never sent (docs/pilot-measurement.md). This
module turns those weeks into one page per operator: for each site, did the
forecast beat "tomorrow looks like today", did its range hold, would following
the plans have saved money, and was there enough data to say? Each site gets
a verdict by rules written down before anyone looks at the numbers:

  GO       enough scored days, forecast clearly better than persistence,
           range honest, and following the plans would have saved money.
  REVIEW   enough days, but one of those is borderline: look before sending.
  NOT YET  too few scored days or too much missing data to judge.
  NO-GO    enough days, and the forecast is no better than persistence or
           following the plans would have cost money. Find out why first.

    uv run python -m vaticore.pipeline shadow-review --operator example-towerco --days 28
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from vaticore.pipeline.savings import Period
from vaticore.pipeline.scoring import value_phrase
from vaticore.pipeline.store import PlanStore
from vaticore.sites.model import Site

# The rules. Changing them after seeing results defeats the point.
MIN_SCORED_DAYS = 20
MIN_SCORED_SHARE = 0.7  # of the period's days
MIN_SKILL = 0.10  # pinball at least 10% better than persistence
RANGE_HELD = (0.70, 0.90)  # the P10 to P90 range should hold about 80% of hours


@dataclass(frozen=True)
class SiteReview:
    site: Site
    days: int
    planned: int
    scored: int
    pinball: float | None
    persistence_pinball: float | None
    range_held: float | None
    litres_saved: float
    outage_kwh_avoided: float
    net_value_saved: float
    share_of_possible: float | None

    @property
    def skill(self) -> float | None:
        if self.pinball is None or not self.persistence_pinball:
            return None
        return 1 - self.pinball / self.persistence_pinball

    @property
    def verdict(self) -> str:
        return self.verdict_and_reasons[0]

    @property
    def verdict_and_reasons(self) -> tuple[str, list[str]]:
        if self.scored < MIN_SCORED_DAYS or self.scored < MIN_SCORED_SHARE * self.days:
            return "NOT YET", [
                f"{self.scored} of {self.days} days scored; need {MIN_SCORED_DAYS} and "
                f"{MIN_SCORED_SHARE:.0%} of the period. Fix the data link, then wait."
            ]
        reasons, hard, soft = [], False, False
        skill = self.skill
        if skill is None or skill <= 0:
            hard = True
            reasons.append("the forecast is no better than persistence")
        elif skill < MIN_SKILL:
            soft = True
            reasons.append(f"the forecast is only {skill:.0%} better than persistence")
        if self.net_value_saved <= 0:
            hard = True
            reasons.append("following the plans would not have saved money")
        if self.range_held is None or not RANGE_HELD[0] <= self.range_held <= RANGE_HELD[1]:
            soft = True
            held = "unknown" if self.range_held is None else f"{self.range_held:.0%}"
            reasons.append(f"the forecast range held {held} of hours (target 80%)")
        if hard:
            return "NO-GO", reasons
        if soft:
            return "REVIEW", reasons
        return "GO", ["forecast clearly better than persistence, honest range, saves money"]


@dataclass(frozen=True)
class ShadowReview:
    operator_id: str
    currency: str
    period: Period
    sites: tuple[SiteReview, ...]

    @property
    def verdict(self) -> str:
        """GO only when every site that can be judged is GO or REVIEW, and most are GO."""
        verdicts = [s.verdict for s in self.sites]
        judged = [v for v in verdicts if v != "NOT YET"]
        if not judged:
            return "NOT YET"
        if "NO-GO" in judged:
            return "GO FOR SOME SITES" if "GO" in judged else "NO-GO"
        return "GO" if judged.count("GO") >= len(judged) / 2 else "REVIEW"

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator_id": self.operator_id,
            "currency": self.currency,
            "period": {"start": self.period.start.isoformat(), "end": self.period.end.isoformat()},
            "verdict": self.verdict,
            "rules": {
                "min_scored_days": MIN_SCORED_DAYS,
                "min_scored_share": MIN_SCORED_SHARE,
                "min_skill": MIN_SKILL,
                "range_held": list(RANGE_HELD),
            },
            "sites": [
                {
                    "site_id": s.site.site_id,
                    "name": s.site.name,
                    "verdict": s.verdict,
                    "reasons": s.verdict_and_reasons[1],
                    "days": s.days,
                    "planned": s.planned,
                    "scored": s.scored,
                    "skill": s.skill,
                    "range_held": s.range_held,
                    "litres_saved": s.litres_saved,
                    "outage_kwh_avoided": s.outage_kwh_avoided,
                    "net_value_saved": s.net_value_saved,
                    "share_of_possible": s.share_of_possible,
                }
                for s in self.sites
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def to_markdown(self) -> str:
        cur = self.currency
        lines = [
            f"# Shadow weeks: {self.operator_id}",
            "",
            f"{self.period.label()} ({self.period.days} days). Plans were made, stored and "
            "scored every day, and sent to nobody.",
            "",
            f"**Verdict: {self.verdict}**",
            "",
            "| Site | Verdict | Days scored | Forecast vs persistence | Range held "
            f"(target 80%) | Diesel saved if followed (L) | Net saving ({cur}) |",
            "|---|---|---|---|---|---|---|",
        ]
        for s in self.sites:
            skill = "n/a" if s.skill is None else f"{s.skill:+.0%}"
            held = "n/a" if s.range_held is None else f"{s.range_held:.0%}"
            lines.append(
                f"| {s.site.name} | {s.verdict} | {s.scored} of {s.days} | {skill} | {held} | "
                f"{s.litres_saved:,.0f} | {s.net_value_saved:,.0f} |"
            )
        lines += ["", "## Why", ""]
        for s in self.sites:
            verdict, reasons = s.verdict_and_reasons
            lines.append(f"- **{s.site.name}: {verdict}.** " + "; ".join(reasons) + ".")
            if s.scored:
                lines.append(
                    f"  Following its plans would have {value_phrase(s.litres_saved, s.outage_kwh_avoided)}"
                    + (
                        f", {min(s.share_of_possible, 1.0):.0%} of what a perfect forecast could save."
                        if s.share_of_possible is not None and s.share_of_possible > 0
                        else "."
                    )
                )
        lines += [
            "",
            "## The rules (set before the shadow weeks)",
            "",
            f"- At least {MIN_SCORED_DAYS} scored days, and {MIN_SCORED_SHARE:.0%} of the period.",
            f"- Forecast pinball loss at least {MIN_SKILL:.0%} lower than persistence "
            '("tomorrow looks like today").',
            f"- The P10 to P90 range holds {RANGE_HELD[0]:.0%} to {RANGE_HELD[1]:.0%} of hours.",
            "- Following the plans would have saved money, outages priced in.",
            "",
            "GO sites can start receiving plans. REVIEW sites: look at the reasons with the "
            "team first. NOT YET: fix the data and extend the shadow weeks. NO-GO: do not send; "
            "find out why (often wrong site details: run `site check`).",
        ]
        return "\n".join(lines) + "\n"


def shadow_review(sites: Sequence[Site], store: PlanStore, period: Period) -> ShadowReview:
    """Review one operator's sites over the shadow period."""
    if not sites:
        raise ValueError("no sites to review")
    operators = {s.operator_id for s in sites}
    if len(operators) != 1:
        raise ValueError(f"one operator per review, got {sorted(operators)}")
    reviews = [_review_site(site, store, period) for site in sites]
    return ShadowReview(next(iter(operators)), sites[0].currency, period, tuple(reviews))


def last_days(days: int, today: date) -> Period:
    """The `days` full days before today."""
    return Period(today - timedelta(days=days), today - timedelta(days=1))


def _review_site(site: Site, store: PlanStore, period: Period) -> SiteReview:
    runs = store.runs(site.operator_id, site.site_id)
    if not runs.empty:
        runs = runs[_in(runs["plan_date"], period)]
    scores = store.scores(site.operator_id, site.site_id, since=period.start)
    if not scores.empty:
        scores = scores[_in(scores["plan_date"], period) & (scores["hours_scored"].fillna(0) > 0)]
    planned = int((runs["status"] == "planned").sum()) if not runs.empty else 0
    if scores.empty:
        return SiteReview(site, period.days, planned, 0, None, None, None, 0.0, 0.0, 0.0, None)

    primary, baseline = [], []
    for detail in scores["detail"]:
        models = (json.loads(detail) if isinstance(detail, str) else detail or {}).get("models", {})
        main = next((m for m in models.values() if m.get("role") == "primary"), None)
        base = next((m for m in models.values() if m.get("role") == "baseline"), None)
        if main is not None and base is not None:
            primary.append(float(main["pinball"]))
            baseline.append(float(base["pinball"]))
    held = scores["coverage_primary"].dropna()
    possible = float((scores["cost_baseline"] - scores["cost_perfect"]).sum())
    saved = float((scores["cost_baseline"] - scores["cost_plan"]).sum())
    return SiteReview(
        site=site,
        days=period.days,
        planned=planned,
        scored=len(scores),
        pinball=float(np.mean(primary)) if primary else None,
        persistence_pinball=float(np.mean(baseline)) if baseline else None,
        range_held=None if held.empty else float(held.mean()),
        litres_saved=float((scores["fuel_baseline_l"] - scores["fuel_plan_l"]).sum()),
        outage_kwh_avoided=float(
            (scores["unserved_baseline_kwh"] - scores["unserved_plan_kwh"]).sum()
        ),
        net_value_saved=saved,
        share_of_possible=saved / possible if possible > 1e-9 else None,
    )


def _in(dates: pd.Series, period: Period) -> pd.Series:
    days = pd.to_datetime(dates).dt.date
    return (days >= period.start) & (days <= period.end)
