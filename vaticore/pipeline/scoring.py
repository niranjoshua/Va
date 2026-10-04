"""Scoring: once a plan day is over, measure what the plan was worth.

For each finished day this compares the stored forecasts and plans with what
actually happened, with the same arithmetic as the research notes:

  - accuracy and honesty of every model that ran (pinball loss, MAE, and how
    often the P10 to P90 range held);
  - litres, outages and cost had the site followed the plan, against the
    baseline plan ("tomorrow looks like today") and a perfect-forecast bound,
    all from the same starting battery charge.

"Had the site followed the plan" matters: in shadow mode a site runs as usual,
so this is the value on offer, not yet the value taken. Replies of 1 and 2
(followed or not) are stored beside each day, so the two can be told apart
when sites start acting on the plans.

A day is scored once at least 75% of its hours have readings; until then it is
retried, and after three days it is scored on what exists, with the gap shown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd

from vaticore.decisions.dispatch import (
    DispatchOutcome,
    SiteAssets,
    plan_dispatch,
    simulate_dispatch,
)
from vaticore.evaluation.metrics import coverage, mae, mean_pinball_loss
from vaticore.pipeline.store import PlanStore, ScoreRecord
from vaticore.schemas import GENERATION_KW, GRID_AVAILABLE, LOAD_KW, TIMESTAMP
from vaticore.sites.model import Portfolio, Site
from vaticore.storage import TimeSeriesRepository

log = logging.getLogger("vaticore.pipeline")

MIN_ACTUAL_SHARE = 0.75
GIVE_UP_AFTER = pd.Timedelta(days=3)


def score_day(
    site: Site,
    repo: TimeSeriesRepository,
    store: PlanStore,
    plan_date: date,
    *,
    now: datetime | None = None,
) -> ScoreRecord | None:
    """Score one stored plan day. None if it is too early to score."""
    now = now or datetime.now(tz=UTC)
    run = store.get_run(site.operator_id, site.site_id, plan_date)
    if run is None or run.status != "planned":
        return None
    plan = store.plan_hours(site.operator_id, site.site_id, plan_date)
    if plan.empty:
        return None
    stamps = pd.DatetimeIndex(pd.to_datetime(plan["timestamp"], utc=True))
    actual = _actuals(site, repo, stamps)
    present = int(actual["net"].notna().sum())
    if (
        present < MIN_ACTUAL_SHARE * len(stamps)
        and pd.Timestamp(now) - pd.Timestamp(run.plan_end) < GIVE_UP_AFTER
    ):
        return None

    assets = site.dispatch_assets()
    soc = float(run.soc_start_kwh if run.soc_start_kwh is not None else assets.min_soc_kwh)
    grid = _actual_grid(site, actual)
    net = actual["net"].to_numpy(dtype=float)

    followed = simulate_dispatch(
        net,
        plan["genset_on"].astype(bool).to_numpy(),
        soc_kwh=soc,
        assets=assets,
        grid_available=grid,
    )
    baseline = simulate_dispatch(
        net,
        plan["baseline_genset_on"].astype(bool).to_numpy(),
        soc_kwh=soc,
        assets=assets,
        grid_available=grid,
    )
    perfect_plan = plan_dispatch(
        pd.Series(np.nan_to_num(net), index=stamps),
        soc_kwh=soc,
        assets=assets,
        planned_grid_available=grid,
    )
    perfect = simulate_dispatch(
        net, perfect_plan.genset_on, soc_kwh=soc, assets=assets, grid_available=grid
    )

    models = _model_scores(store.forecasts(site.operator_id, site.site_id, plan_date), actual)
    primary = next((m for m in models.values() if m["role"] == "primary"), None)
    record = ScoreRecord(
        operator_id=site.operator_id,
        site_id=site.site_id,
        plan_date=plan_date,
        scored_at=pd.Timestamp(now).to_pydatetime(),
        hours_scored=present,
        hours_missing=len(stamps) - present,
        pinball_primary=None if primary is None else float(str(primary["pinball"])),
        coverage_primary=None if primary is None else float(str(primary["coverage_80"])),
        fuel_plan_l=followed.fuel_l,
        fuel_baseline_l=baseline.fuel_l,
        fuel_perfect_l=perfect.fuel_l,
        unserved_plan_kwh=followed.unserved_kwh,
        unserved_baseline_kwh=baseline.unserved_kwh,
        unserved_perfect_kwh=perfect.unserved_kwh,
        cost_plan=_cost(followed, assets),
        cost_baseline=_cost(baseline, assets),
        cost_perfect=_cost(perfect, assets),
        detail={
            "models": models,
            "genset_starts": {
                "plan": followed.genset_starts,
                "baseline": baseline.genset_starts,
                "perfect": perfect.genset_starts,
            },
            "grid_unknown_hours": followed.grid_unknown_steps,
            "soc_assumed": run.soc_assumed,
        },
    )
    store.save_score(record)
    return record


def score_due(
    portfolio: Portfolio,
    repo: TimeSeriesRepository,
    store: PlanStore,
    *,
    now: datetime | None = None,
) -> list[ScoreRecord]:
    """Score every finished, unscored plan day for the portfolio's sites."""
    now = now or datetime.now(tz=UTC)
    sites = {s.key: s for s in portfolio.sites}
    scored = []
    for operator_id, site_id, plan_date in store.unscored(now):
        site = sites.get((operator_id, site_id))
        if site is None:
            continue
        try:
            record = score_day(site, repo, store, plan_date, now=now)
        except Exception:
            log.exception("scoring %s/%s %s failed", operator_id, site_id, plan_date)
            continue
        if record is not None:
            scored.append(record)
    return scored


@dataclass(frozen=True)
class Scorecard:
    """A site's track record over recent scored days."""

    operator_id: str
    site_id: str
    days: int
    range_held: float | None
    litres_saved: float
    outage_kwh_avoided: float
    cost_saved: float
    share_of_possible: float | None
    models: pd.DataFrame  # mean pinball and range held per model

    def summary(self) -> str:
        if self.days == 0:
            return f"{self.operator_id}/{self.site_id}: no scored days yet."
        held = "n/a" if self.range_held is None else f"{self.range_held:.0%}"
        text = (
            f"{self.operator_id}/{self.site_id}, {self.days} scored days: forecast range held "
            f"{held} of hours (target 80%). Against planning from yesterday, following the "
            f"plans would have {value_phrase(self.litres_saved, self.outage_kwh_avoided)}"
        )
        if self.share_of_possible is not None and self.share_of_possible < 0:
            text += "; net of costs, more expensive than planning from yesterday"
        elif self.share_of_possible is not None:
            text += (
                f"; net of costs, {min(self.share_of_possible, 1.0):.0%} of what a perfect "
                "forecast could save"
            )
        return text + "."


def scorecard(store: PlanStore, operator_id: str, site_id: str, *, days: int = 30) -> Scorecard:
    since = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)).date()
    scores = store.scores(operator_id, site_id, since=since)
    scores = scores[scores["hours_scored"].fillna(0) > 0] if not scores.empty else scores
    if scores.empty:
        return Scorecard(operator_id, site_id, 0, None, 0.0, 0.0, 0.0, None, pd.DataFrame())
    held = scores["coverage_primary"].dropna()
    possible = float((scores["cost_baseline"] - scores["cost_perfect"]).sum())
    saved = float((scores["cost_baseline"] - scores["cost_plan"]).sum())
    rows = []
    import json

    for detail in scores["detail"]:
        parsed = json.loads(detail) if isinstance(detail, str) else detail
        for name, metrics in parsed.get("models", {}).items():
            rows.append({"model": name, **metrics})
    models = (
        pd.DataFrame(rows)
        .groupby("model")[["pinball", "coverage_80"]]
        .mean()
        .sort_values("pinball")
        if rows
        else pd.DataFrame()
    )
    return Scorecard(
        operator_id,
        site_id,
        len(scores),
        None if held.empty else float(held.mean()),
        float((scores["fuel_baseline_l"] - scores["fuel_plan_l"]).sum()),
        float((scores["unserved_baseline_kwh"] - scores["unserved_plan_kwh"]).sum()),
        saved,
        saved / possible if possible > 1e-9 else None,
        models,
    )


def value_phrase(litres_saved: float, outage_kwh_avoided: float) -> str:
    """Litres and outages against the baseline, stated honestly in either direction."""
    litres = abs(litres_saved)
    outages = abs(outage_kwh_avoided)
    if litres_saved >= 1 and outage_kwh_avoided >= -0.5:
        extra = f" and avoided {outages:,.0f} kWh of outages" if outage_kwh_avoided >= 1 else ""
        return f"saved {litres:,.0f} L of diesel{extra}"
    if litres_saved <= -1 and outage_kwh_avoided >= 1:
        return f"used {litres:,.0f} L more diesel to avoid {outages:,.0f} kWh of outages"
    if litres_saved <= -1 and outage_kwh_avoided <= -1:
        return f"used {litres:,.0f} L more diesel and had {outages:,.0f} kWh more outages"
    if litres_saved <= -1:
        return f"used {litres:,.0f} L more diesel with no fewer outages"
    if outage_kwh_avoided >= 1:
        return f"avoided {outages:,.0f} kWh of outages for the same diesel"
    if outage_kwh_avoided <= -1:
        return f"had {outages:,.0f} kWh more outages"
    return "made no measurable difference"


# -- internals ---------------------------------------------------------------


def _actuals(site: Site, repo: TimeSeriesRepository, stamps: pd.DatetimeIndex) -> pd.DataFrame:
    raw = repo.read_history(
        site.operator_id,
        site.site_id,
        stamps[0],
        stamps[-1] + pd.Timedelta(minutes=59),
    )
    frame = raw.set_index(TIMESTAMP).sort_index() if not raw.empty else pd.DataFrame()
    cols = [c for c in (LOAD_KW, GENERATION_KW, GRID_AVAILABLE) if c in frame.columns]
    hourly = frame[cols].resample("1h").mean() if cols else pd.DataFrame(index=stamps)
    hourly = hourly.reindex(stamps)
    load = hourly.get(LOAD_KW, pd.Series(np.nan, index=stamps))
    gen = hourly.get(GENERATION_KW, pd.Series(np.nan, index=stamps))
    if site.solar is None:
        gen = gen.fillna(0.0)
    out = pd.DataFrame({"net": load - gen}, index=stamps)
    if GRID_AVAILABLE in hourly:
        out[GRID_AVAILABLE] = np.floor(hourly[GRID_AVAILABLE] + 1e-9)
    return out


def _actual_grid(site: Site, actual: pd.DataFrame) -> np.ndarray | None:
    if site.grid is None:
        return None
    if site.grid.reliable:
        return np.ones(len(actual))
    if GRID_AVAILABLE in actual:
        return actual[GRID_AVAILABLE].to_numpy(dtype=float)  # unknown hours count as off
    return np.full(len(actual), np.nan)


def _model_scores(forecasts: pd.DataFrame, actual: pd.DataFrame) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    if forecasts.empty:
        return out
    for (role, model), group in forecasts.groupby(["role", "model"]):
        idx = pd.DatetimeIndex(pd.to_datetime(group["timestamp"], utc=True))
        truth = actual["net"].reindex(idx).to_numpy(dtype=float)
        keep = ~np.isnan(truth)
        if not keep.any():
            continue
        by_q = {
            q: group[col].to_numpy(dtype=float)[keep]
            for q, col in ((0.1, "q10"), (0.5, "q50"), (0.9, "q90"))
        }
        y = truth[keep]
        name = str(model) if role != "baseline" else f"{model} (baseline)"
        out[name] = {
            "role": str(role),
            "pinball": float(mean_pinball_loss(y, by_q)),
            "mae": float(mae(y, by_q[0.5])),
            "coverage_80": float(coverage(y, by_q[0.1], by_q[0.9])),
            "hours": int(keep.sum()),
        }
    return out


def _cost(outcome: DispatchOutcome, assets: SiteAssets) -> float:
    return (
        outcome.fuel_l * assets.diesel_price_per_l
        + outcome.grid_kwh * assets.grid_price_per_kwh
        + outcome.unserved_kwh * assets.value_of_lost_load_per_kwh
    )
