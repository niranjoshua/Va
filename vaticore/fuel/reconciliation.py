"""Fuel reconciliation: diesel delivered against diesel burned, to flag losses.

Diesel goes missing on distributed sites: short deliveries, fuel siphoned from
the tank, runtime logged but never run. This module puts three records side by
side and reports where they disagree by more than the measurements can
explain:

  - **Burned (expected):** litres the generator should have used, from its
    output (`genset_kw`) through the site's own fuel curve, the same one the
    planner uses: litres per hour = a * rated_kw + b * output_kw.
  - **Tank level (`fuel_level_l`), where a sensor reports it:** rises are
    refills, falls are fuel leaving the tank.
  - **Deliveries:** litres the supplier says were delivered, recorded with
    `python -m vaticore.pipeline fuel add` or the API.

What it flags (each with litres, money and times):

  | Code | Means |
  |---|---|
  | `short_delivery` | the tank rose by clearly less than a recorded delivery |
  | `delivery_not_seen` | a recorded delivery with no matching rise in the tank |
  | `unrecorded_refill` | the tank rose with no delivery recorded |
  | `drop_while_off` | the tank fell while the generator was off: the strongest sign of siphoning or a leak |
  | `unexplained_loss` | over a day, more fuel left the tank than the generator could have burned |
  | `deliveries_exceed_use` | without a tank sensor: more delivered than the generator could burn plus what the tank can hold |

These are flags for someone to check, not proof of theft: fuel curves are
typical rather than measured for each generator (allow 10 to 15%), sensors
drift, and deliveries are sometimes recorded late. Thresholds are set so a
flag is worth a phone call, and every flag says what was compared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import pandas as pd

from vaticore.schemas import FUEL_LEVEL_L, GENSET_KW, TIMESTAMP
from vaticore.sites.model import Site


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    ALERT = "alert"


@dataclass(frozen=True)
class FuelConfig:
    # The fuel curve is typical, not measured for each generator.
    curve_tolerance: float = 0.15
    # Smallest unexplained loss worth flagging over a day, in litres.
    min_loss_l: float = 15.0
    # A tank rise of at least this much in an hour is a refill.
    refill_min_l: float = 40.0
    # A delivery is short when the tank rose by this share less than recorded...
    short_tolerance: float = 0.10
    # ...and by at least this many litres.
    short_min_l: float = 20.0
    # How far a refill may be from the recorded delivery time.
    match_window: pd.Timedelta = field(default_factory=lambda: pd.Timedelta(hours=12))
    # A fall of at least this much while the generator is off is flagged.
    off_drop_min_l: float = 10.0
    # Output below this share of rated power counts as off.
    off_below: float = 0.05


@dataclass(frozen=True)
class FuelFinding:
    code: str
    severity: Severity
    message: str
    litres: float
    at: pd.Timestamp | None = None


@dataclass(frozen=True)
class FuelReport:
    operator_id: str
    site_id: str
    start: pd.Timestamp
    end: pd.Timestamp
    currency: str
    price_per_l: float
    delivered_l: float
    burned_l: float | None  # expected from generator output; None without readings
    withdrawn_l: float | None  # fuel that left the tank; None without a tank sensor
    genset_coverage: float  # share of hours with generator output readings
    tank_coverage: float  # share of hours with tank level readings
    daily: pd.DataFrame
    findings: tuple[FuelFinding, ...]

    @property
    def flagged_l(self) -> float:
        """Litres behind warnings and alerts (a refill not recorded is not a loss)."""
        return float(
            sum(f.litres for f in self.findings if f.severity is not Severity.INFO and f.litres > 0)
        )

    @property
    def status(self) -> Severity:
        levels = [f.severity for f in self.findings]
        for level in (Severity.ALERT, Severity.WARN):
            if level in levels:
                return level
        return Severity.INFO

    def to_text(self, timezone: str = "UTC") -> str:
        lines = [
            f"Fuel for {self.operator_id}/{self.site_id}, "
            f"{self.start.tz_convert(timezone):%d %b} to {self.end.tz_convert(timezone):%d %b}",
            f"Delivered {self.delivered_l:,.0f} L; "
            + (
                f"generator should have burned about {self.burned_l:,.0f} L "
                f"(output recorded for {self.genset_coverage:.0%} of hours)"
                if self.burned_l is not None
                else "no generator output readings, so burn cannot be estimated"
            )
            + (
                f"; {self.withdrawn_l:,.0f} L left the tank"
                if self.withdrawn_l is not None
                else "; no tank level sensor"
            )
            + ".",
        ]
        if self.flagged_l > 0:
            lines.append(
                f"Flagged: {self.flagged_l:,.0f} L, about {self.currency} "
                f"{self.flagged_l * self.price_per_l:,.0f}. Check these with the site:"
            )
        for f in self.findings:
            when = f" ({f.at.tz_convert(timezone):%d %b %H:%M})" if f.at is not None else ""
            lines.append(f"- [{f.severity.value}] {f.message}{when}")
        if not self.findings:
            lines.append("- Nothing to flag: deliveries, tank and generator agree.")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        daily = self.daily.reset_index().assign(day=lambda d: d["day"].astype(str))
        return {
            "operator_id": self.operator_id,
            "site_id": self.site_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "status": self.status.value,
            "currency": self.currency,
            "delivered_l": self.delivered_l,
            "burned_l": self.burned_l,
            "withdrawn_l": self.withdrawn_l,
            "flagged_l": self.flagged_l,
            "flagged_value": self.flagged_l * self.price_per_l,
            "genset_coverage": self.genset_coverage,
            "tank_coverage": self.tank_coverage,
            "daily": [
                {k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in row.items()}
                for row in daily.to_dict(orient="records")
            ],
            "findings": [
                {
                    "code": f.code,
                    "severity": f.severity.value,
                    "message": f.message,
                    "litres": f.litres,
                    "at": None if f.at is None else f.at.isoformat(),
                }
                for f in self.findings
            ],
        }


def reconcile(
    site: Site,
    readings: pd.DataFrame,
    deliveries: pd.DataFrame,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    config: FuelConfig | None = None,
) -> FuelReport:
    """Reconcile one site's fuel between `start` and `end` (UTC).

    readings: the site's history (TIMESTAMP, and genset_kw and/or fuel_level_l).
    deliveries: one row per delivery, with delivered_at (UTC), litres and an
    optional reference.
    """
    if site.generator is None:
        raise ValueError(f"site {site.operator_id}/{site.site_id} has no generator")
    config = config or FuelConfig()
    gen = site.generator
    hours = pd.date_range(start.ceil("h"), end, freq="1h", inclusive="left", name=TIMESTAMP)
    frame = readings[(readings[TIMESTAMP] >= start) & (readings[TIMESTAMP] < end)]
    hourly = (
        frame.set_index(TIMESTAMP)[[c for c in (GENSET_KW, FUEL_LEVEL_L) if c in frame]]
        .astype(float)
        .resample("1h")
        .mean()
        .reindex(hours)
        if not frame.empty
        else pd.DataFrame(index=hours)
    )
    output = hourly[GENSET_KW] if GENSET_KW in hourly else pd.Series(np.nan, index=hours)
    level = hourly[FUEL_LEVEL_L] if FUEL_LEVEL_L in hourly else pd.Series(np.nan, index=hours)

    # Expected burn per hour from the fuel curve; off hours burn nothing.
    running = output > config.off_below * gen.rated_kw
    burn = pd.Series(
        np.where(
            running,
            gen.fuel_intercept_l_per_kw_h * gen.rated_kw + gen.fuel_slope_l_per_kwh * output,
            0.0,
        ),
        index=hours,
    ).where(output.notna())

    dels = _deliveries(deliveries, start, end)
    findings: list[FuelFinding] = []
    price = gen.fuel_price_per_l

    withdrawn = pd.Series(np.nan, index=hours)
    refills: list[tuple[pd.Timestamp, float]] = []
    if level.notna().sum() >= 2:
        # The level's change during each hour (to the next hour's reading), so a
        # fall lines up with the generator output of the hour it happened in.
        step = level.shift(-1) - level
        refill_hours, refills = _refills(step, config)
        # Fuel that left the tank: the net fall outside refills, so that sensor
        # noise up and down cancels instead of adding up.
        withdrawn = (-step).where(~refill_hours)
        findings.extend(_match_refills(refills, dels, config, price, site.currency))
        findings.extend(_drops_while_off(withdrawn, output, running, config, price, site.currency))

    daily = _daily(hours, burn, withdrawn, dels, site.timezone)
    findings.extend(_daily_losses(daily, config, price, site.currency))

    delivered = float(dels["litres"].sum()) if not dels.empty else 0.0
    burned = float(burn.sum()) if burn.notna().any() else None
    if (
        level.notna().sum() < 2
        and burned is not None
        and gen.tank_l is not None
        and burn.notna().mean() >= 0.9
    ):
        # No tank sensor: the stock can change by at most the tank's size.
        excess = delivered - burned * (1 + config.curve_tolerance) - gen.tank_l
        if excess > config.min_loss_l:
            findings.append(
                FuelFinding(
                    "deliveries_exceed_use",
                    Severity.ALERT,
                    f"{delivered:,.0f} L delivered, but the generator could have burned at most "
                    f"about {burned * (1 + config.curve_tolerance):,.0f} L and the tank holds "
                    f"{gen.tank_l:,.0f} L: {excess:,.0f} L ({site.currency} "
                    f"{excess * price:,.0f}) is unaccounted for.",
                    excess,
                )
            )

    order = {Severity.ALERT: 0, Severity.WARN: 1, Severity.INFO: 2}
    findings.sort(key=lambda f: (order[f.severity], -f.litres))
    return FuelReport(
        operator_id=site.operator_id,
        site_id=site.site_id,
        start=start,
        end=end,
        currency=site.currency,
        price_per_l=price,
        delivered_l=delivered,
        burned_l=burned,
        withdrawn_l=float(withdrawn.sum()) if withdrawn.notna().any() else None,
        genset_coverage=float(output.notna().mean()) if len(hours) else 0.0,
        tank_coverage=float(level.notna().mean()) if len(hours) else 0.0,
        daily=daily,
        findings=tuple(findings),
    )


# -- internals -----------------------------------------------------------------


def _deliveries(deliveries: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if deliveries.empty:
        return pd.DataFrame(columns=["delivered_at", "litres", "reference"])
    out = deliveries.copy()
    out["delivered_at"] = pd.to_datetime(out["delivered_at"], utc=True)
    if "reference" not in out:
        out["reference"] = None
    keep = (out["delivered_at"] >= start) & (out["delivered_at"] < end)
    return out[keep].sort_values("delivered_at").reset_index(drop=True)


def _match_refills(
    refills: list[tuple[pd.Timestamp, float]],
    dels: pd.DataFrame,
    config: FuelConfig,
    price: float,
    currency: str,
) -> list[FuelFinding]:
    """Pair each recorded delivery with the nearest refill seen in the tank."""
    findings = []
    unused = list(refills)
    for _, d in dels.iterrows():
        at, litres = pd.Timestamp(d["delivered_at"]), float(d["litres"])
        ref = f" (ref {d['reference']})" if d.get("reference") else ""
        near = [r for r in unused if abs(r[0] - at) <= config.match_window]
        if not near:
            findings.append(
                FuelFinding(
                    "delivery_not_seen",
                    Severity.WARN,
                    f"Delivery of {litres:,.0f} L{ref} recorded, but the tank level shows no "
                    "refill near that time: check the delivery and the sensor.",
                    litres,
                    at,
                )
            )
            continue
        rise_at, rise = min(near, key=lambda r: abs(r[0] - at))
        unused.remove((rise_at, rise))
        short = litres - rise
        if short > max(config.short_min_l, config.short_tolerance * litres):
            findings.append(
                FuelFinding(
                    "short_delivery",
                    Severity.ALERT,
                    f"Delivery of {litres:,.0f} L{ref} recorded, but the tank rose only "
                    f"{rise:,.0f} L: {short:,.0f} L ({currency} {short * price:,.0f}) short.",
                    short,
                    at,
                )
            )
    for rise_at, rise in unused:
        findings.append(
            FuelFinding(
                "unrecorded_refill",
                Severity.INFO,
                f"The tank rose {rise:,.0f} L with no delivery recorded: add the delivery "
                "so the fuel is accounted for.",
                0.0,
                rise_at,
            )
        )
    return findings


def _refills(
    step: pd.Series, config: FuelConfig
) -> tuple[pd.Series, list[tuple[pd.Timestamp, float]]]:
    """Runs of rising hours that add up to a refill (a delivery can span two hours)."""
    rising = step > 0
    run_id = (~rising).cumsum()
    is_refill = pd.Series(False, index=step.index)
    refills = []
    for _, run in step[rising].groupby(run_id[rising]):
        total = float(run.sum())
        if total >= config.refill_min_l:
            is_refill[run.index] = True
            refills.append((pd.Timestamp(run.index[0]), total))
    return is_refill, refills


def _drops_while_off(
    withdrawn: pd.Series,
    output: pd.Series,
    running: pd.Series,
    config: FuelConfig,
    price: float,
    currency: str,
) -> list[FuelFinding]:
    """Net falls in the tank level over spells when the generator was known to be off."""
    off = output.notna() & ~running & withdrawn.notna()
    findings = []
    spell = (~off).cumsum()
    for _, group in withdrawn[off].groupby(spell[off]):
        total = float(group.sum())
        if total >= config.off_drop_min_l:
            findings.append(
                FuelFinding(
                    "drop_while_off",
                    Severity.ALERT,
                    f"The tank fell {total:,.0f} L while the generator was off "
                    f"({currency} {total * price:,.0f}): fuel taken or leaking.",
                    total,
                    pd.Timestamp(group.idxmax()),
                )
            )
    return findings


def _daily(
    hours: pd.DatetimeIndex,
    burn: pd.Series,
    withdrawn: pd.Series,
    dels: pd.DataFrame,
    timezone: str,
) -> pd.DataFrame:
    """Per local day: delivered, expected burn, withdrawn from the tank, unexplained."""
    local_day = pd.DatetimeIndex(hours).tz_convert(timezone).date
    frame = pd.DataFrame(
        {"burned_l": burn.to_numpy(), "withdrawn_l": withdrawn.to_numpy(), "day": local_day},
        index=hours,
    )
    daily = frame.groupby("day").agg(
        burned_l=("burned_l", lambda s: s.sum(min_count=1)),
        burn_hours=("burned_l", "count"),
        withdrawn_l=("withdrawn_l", lambda s: s.sum(min_count=1)),
        tank_hours=("withdrawn_l", "count"),
    )
    if not dels.empty:
        days = pd.DatetimeIndex(dels["delivered_at"]).tz_convert(timezone).date
        delivered = dels.assign(day=days).groupby("day")["litres"].sum()
        daily["delivered_l"] = delivered.reindex(daily.index).fillna(0.0)
    else:
        daily["delivered_l"] = 0.0
    daily["unexplained_l"] = daily["withdrawn_l"] - daily["burned_l"]
    daily.index.name = "day"
    return daily[
        ["delivered_l", "burned_l", "withdrawn_l", "unexplained_l", "burn_hours", "tank_hours"]
    ]


def _daily_losses(
    daily: pd.DataFrame, config: FuelConfig, price: float, currency: str
) -> list[FuelFinding]:
    findings = []
    for day, row in daily.iterrows():
        # Judge a day only when both records cover most of it.
        if row["burn_hours"] < 20 or row["tank_hours"] < 20:
            continue
        allowed = max(config.min_loss_l, config.curve_tolerance * float(row["burned_l"]))
        loss = float(row["unexplained_l"])
        if loss > allowed:
            findings.append(
                FuelFinding(
                    "unexplained_loss",
                    Severity.WARN,
                    f"{day:%a %d %b}: {row['withdrawn_l']:,.0f} L left the tank but the "
                    f"generator should have burned about {row['burned_l']:,.0f} L: "
                    f"{loss:,.0f} L ({currency} {loss * price:,.0f}) unexplained.",
                    loss,
                )
            )
    return findings
