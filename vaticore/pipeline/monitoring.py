"""Model monitoring: is each model still honest and still better than persistence?

Every scored day stores, per model, its pinball loss and how often the P10 to
P90 range held (scoring.py). This module turns those into a per-site, per-model
record over a recent window and acts on it:

  - **Suspend** a model whose range has drifted (held under 65% or over 95% of
    hours, against a target of 80%) or whose pinball loss is more than 10%
    worse than persistence over the same days. A suspended model is skipped
    as the planning model, so the plan falls back down the chain (Chronos-2,
    the GBM, then persistence) and a site never goes without a plan.
  - **Reinstate** it only once it is clearly back: range held between 70% and
    90% and no worse than persistence. The gap between the two sets of limits
    stops a model flapping in and out on noise.
  - **Promote** a challenger (such as the weather model) ahead of the default
    model only when it has beaten it over the full window by a clear margin,
    with an honest range. Models keep running in shadow while suspended or
    challenging, so the evidence keeps coming in.

Persistence is the floor and is never suspended. Every change of status is
stored with its reason and reported as an alert.

The limits are deliberately wide. Over 14 days a well-calibrated model's range
holds 80% of about 336 hours, but hours within a day are correlated, so the
effective sample is closer to the number of days and a few points either way
is noise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from vaticore.pipeline.store import PlanStore

PERSISTENCE = "persistence"
OK = "ok"
SUSPENDED = "suspended"
LEARNING = "learning"  # not enough scored hours yet to judge


@dataclass(frozen=True)
class MonitorConfig:
    window_days: int = 14
    # At least this many scored hours in the window before a model is judged.
    min_hours: int = 120
    suspend_below: float = 0.65
    suspend_above: float = 0.95
    recover_from: float = 0.70
    recover_to: float = 0.90
    # Suspend when pinball is this much worse than persistence (0.10 = 10%).
    worse_than_persistence: float = 0.10
    # Promote a challenger when its pinball beats the default model's by this
    # much over the same days (0.05 = 5%).
    promote_margin: float = 0.05


@dataclass(frozen=True)
class ModelHealth:
    """One model's record at one site over the monitoring window."""

    model: str
    status: str  # ok, suspended or learning
    hours: int
    days: int
    range_held: float | None
    pinball: float | None
    persistence_pinball: float | None
    reason: str | None = None

    @property
    def skill(self) -> float | None:
        """Pinball improvement over persistence on the same days (0.2 = 20% better)."""
        if self.pinball is None or not self.persistence_pinball:
            return None
        return 1.0 - self.pinball / self.persistence_pinball

    def describe(self) -> str:
        held = "n/a" if self.range_held is None else f"{self.range_held:.0%}"
        skill = "n/a" if self.skill is None else f"{self.skill:+.0%}"
        return (
            f"{self.model}: {self.status}, range held {held} of {self.hours} h over "
            f"{self.days} days, pinball vs persistence {skill}"
            + (f" ({self.reason})" if self.reason else "")
        )


@dataclass(frozen=True)
class StatusChange:
    operator_id: str
    site_id: str
    model: str
    old: str | None
    new: str
    reason: str

    def describe(self) -> str:
        verb = {SUSPENDED: "suspended", OK: "reinstated"}.get(self.new, self.new)
        return f"{self.operator_id}/{self.site_id}: {self.model} {verb}: {self.reason}"


def base_model(name: str) -> str:
    """'quantile_gbm_day_ahead+conformal' and its raw form are one model."""
    return name.replace(" (baseline)", "").split("+")[0]


def model_days(
    store: PlanStore, operator_id: str, site_id: str, *, since: date, until: date | None = None
) -> pd.DataFrame:
    """One row per scored day and model: pinball, range held, hours, and persistence's pinball.

    Baseline and live persistence forecasts are the same model, so persistence's
    row on each day is whichever of them was scored.
    """
    scores = store.scores(operator_id, site_id, since=since)
    rows = []
    for _, score in scores.iterrows():
        day = pd.Timestamp(score["plan_date"]).date()
        if until is not None and day >= until:
            continue
        detail = score["detail"]
        parsed = json.loads(detail) if isinstance(detail, str) else (detail or {})
        models = parsed.get("models", {})
        reference = next(
            (m["pinball"] for name, m in models.items() if base_model(name) == PERSISTENCE), None
        )
        seen: set[str] = set()
        for name, metrics in models.items():
            model = base_model(name)
            if model in seen:  # persistence can appear as baseline and as primary
                continue
            seen.add(model)
            rows.append(
                {
                    "plan_date": day,
                    "model": model,
                    "role": metrics.get("role"),
                    "hours": int(metrics.get("hours", 0)),
                    "pinball": float(metrics["pinball"]),
                    "range_held": float(metrics["coverage_80"]),
                    "persistence_pinball": None if reference is None else float(reference),
                }
            )
    columns = [
        "plan_date",
        "model",
        "role",
        "hours",
        "pinball",
        "range_held",
        "persistence_pinball",
    ]
    return pd.DataFrame(rows, columns=columns)


def weekly_report(
    store: PlanStore, operator_id: str, site_id: str, *, weeks: int = 8
) -> pd.DataFrame:
    """Each model's calibration and accuracy week by week, for review and charts."""
    since = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(weeks=weeks)).date()
    days = model_days(store, operator_id, site_id, since=since)
    if days.empty:
        return pd.DataFrame(
            columns=["week", "model", "days", "hours", "range_held", "pinball", "skill"]
        )
    days["week"] = pd.to_datetime(days["plan_date"]).dt.to_period("W-SUN").dt.start_time.dt.date
    rows = []
    for (week, model), group in days.groupby(["week", "model"]):
        rows.append({"week": week, "model": model, **_summarise(group)})
    out = pd.DataFrame(rows)
    out["skill"] = 1.0 - out["pinball"] / out["persistence_pinball"]
    return out[["week", "model", "days", "hours", "range_held", "pinball", "skill"]].sort_values(
        ["week", "model"]
    )


def assess(
    store: PlanStore,
    operator_id: str,
    site_id: str,
    as_of: date,
    config: MonitorConfig | None = None,
    *,
    previous: dict[str, str] | None = None,
) -> dict[str, ModelHealth]:
    """Each model's health over the window before `as_of` (never using later days)."""
    config = config or MonitorConfig()
    previous = previous or {}
    since = as_of - pd.Timedelta(days=config.window_days)
    days = model_days(store, operator_id, site_id, since=since, until=as_of)
    out: dict[str, ModelHealth] = {}
    for model, group in days.groupby("model"):
        summary = _summarise(group)
        hours = int(summary["hours"])
        held = summary["range_held"]
        pinball = summary["pinball"]
        reference = summary["persistence_pinball"]
        status, reason = OK, None
        if model == PERSISTENCE:
            status = OK
        elif hours < config.min_hours:
            # Too little evidence: keep a suspension in force, otherwise trust.
            status = SUSPENDED if previous.get(str(model)) == SUSPENDED else LEARNING
            reason = f"{hours} scored hours so far, judged from {config.min_hours}"
        elif previous.get(str(model)) == SUSPENDED:
            back = config.recover_from <= held <= config.recover_to and (
                reference is None or pinball <= reference
            )
            status = OK if back else SUSPENDED
            reason = f"range held {held:.0%} and pinball {_versus(pinball, reference)}" + (
                "" if back else ", not yet back within limits"
            )
        else:
            problems = []
            if held < config.suspend_below:
                problems.append(f"range held only {held:.0%} of hours (target 80%)")
            elif held > config.suspend_above:
                problems.append(f"range held {held:.0%} of hours, too wide to be useful")
            if reference and pinball > reference * (1 + config.worse_than_persistence):
                problems.append(f"pinball {_versus(pinball, reference)}")
            if problems:
                status, reason = SUSPENDED, "; ".join(problems)
        out[str(model)] = ModelHealth(
            model=str(model),
            status=status,
            hours=hours,
            days=int(summary["days"]),
            range_held=held,
            pinball=pinball,
            persistence_pinball=reference,
            reason=reason,
        )
    return out


def check_site(
    store: PlanStore,
    operator_id: str,
    site_id: str,
    as_of: date,
    now: datetime,
    config: MonitorConfig | None = None,
) -> tuple[dict[str, ModelHealth], list[StatusChange]]:
    """Assess a site's models, store any change of status, and return the changes."""
    stored = store.model_statuses(operator_id, site_id)
    previous = {model: row["status"] for model, row in stored.items()}
    health = assess(store, operator_id, site_id, as_of, config, previous=previous)
    changes = []
    for model, h in health.items():
        effective = SUSPENDED if h.status == SUSPENDED else OK
        before = previous.get(model)
        if before is None and effective == OK:
            continue  # nothing to record for a model that was never in question
        if before != effective:
            store.set_model_status(operator_id, site_id, model, effective, h.reason or "", now)
            changes.append(
                StatusChange(operator_id, site_id, model, before, effective, h.reason or "")
            )
    return health, changes


def choose_order(
    default: list[str],
    challengers: list[str],
    health: dict[str, ModelHealth],
    config: MonitorConfig | None = None,
) -> tuple[list[str], list[str]]:
    """The models to try, best first, and the notes that explain any change.

    Suspended models are dropped. A challenger with a full window of evidence,
    an honest range and a pinball loss clearly below the default model's on
    the same window goes first. Persistence is always the last resort and is
    added by the caller.
    """
    config = config or MonitorConfig()
    notes: list[str] = []
    order = []
    for model in [*default, *challengers]:
        h = health.get(model)
        if h is not None and h.status == SUSPENDED:
            notes.append(f"{model} suspended by monitoring: {h.reason}")
            continue
        order.append(model)
    usable = [m for m in default if m in order]
    leader = health.get(usable[0]) if usable else None
    best = None
    for model in challengers:
        h = health.get(model)
        if (
            model in order
            and h is not None
            and h.status == OK
            and h.hours >= config.min_hours
            and config.recover_from <= (h.range_held or 0.0) <= config.recover_to
            and h.pinball is not None
            and (
                leader is None
                or leader.pinball is None
                or h.pinball < leader.pinball * (1 - config.promote_margin)
            )
            and (best is None or h.pinball < (health[best].pinball or float("inf")))
        ):
            best = model
    challengers_left = [m for m in challengers if m in order and m != best]
    final = ([best] if best else []) + [m for m in default if m in order] + challengers_left
    if best is not None:
        notes.append(
            f"{best} promoted: beat {usable[0] if usable else 'the default'} over "
            f"{health[best].days} days ({health[best].describe()})"
        )
    return final, notes


def _summarise(group: pd.DataFrame) -> dict[str, float]:
    """Hour-weighted range held and pinball, and persistence's pinball on the same days."""
    hours = group["hours"].clip(lower=0)
    total = float(hours.sum())
    with_reference = group.dropna(subset=["persistence_pinball"])
    ref_hours = with_reference["hours"].clip(lower=0)
    return {
        "days": float(len(group)),
        "hours": total,
        "range_held": float((group["range_held"] * hours).sum() / total) if total else float("nan"),
        "pinball": float((group["pinball"] * hours).sum() / total) if total else float("nan"),
        "persistence_pinball": (
            float((with_reference["persistence_pinball"] * ref_hours).sum() / ref_hours.sum())
            if float(ref_hours.sum()) > 0
            else None  # type: ignore[dict-item]
        ),
    }


def _versus(pinball: float, reference: float | None) -> str:
    if not reference:
        return f"{pinball:.3f} (no persistence score)"
    change = pinball / reference - 1.0
    word = "worse" if change > 0 else "better"
    return f"{abs(change):.0%} {word} than persistence"
