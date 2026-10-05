"""Check a site before it joins the portfolio, and whether it is ready to plan.

Adding a site means adding a [[site]] block to the operator's portfolio file
(a secret file on Render, never in the repository). A mistake there (a
swapped latitude, a fuel price per drum instead of per litre, a missing tank
size) does not crash anything; it quietly makes every plan and report for that
site wrong. So every new or changed portfolio file is checked first:

    uv run python -m vaticore.pipeline site check --portfolio new-portfolio.toml
    uv run python -m vaticore.pipeline site check --with-data   # on the server

Each site is checked on its own, so one broken block does not hide the
others. Findings are FAIL (fix before uploading), WARN (the site works, but a
plan, a fuel check or the pilot report will be weaker) and INFO. Every finding
says how to fix it. docs/pilot/site-onboarding-form.md is the form whose
answers fill the block.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import ValidationError

from vaticore.sites.model import Site


class Level(StrEnum):
    FAIL = "FAIL"
    WARN = "WARN"
    INFO = "INFO"


@dataclass(frozen=True)
class Finding:
    level: Level
    message: str


@dataclass
class SiteCheck:
    label: str  # operator_id/site_id, or the block number when unreadable
    site: Site | None
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: Level, message: str) -> None:
        self.findings.append(Finding(level, message))

    @property
    def status(self) -> Level:
        levels = {f.level for f in self.findings}
        for level in (Level.FAIL, Level.WARN):
            if level in levels:
                return level
        return Level.INFO

    def to_text(self) -> str:
        head = {Level.FAIL: "FAIL", Level.WARN: "WARN", Level.INFO: "OK"}[self.status]
        name = f" ({self.site.name})" if self.site else ""
        lines = [f"{head}  {self.label}{name}"]
        order = {Level.FAIL: 0, Level.WARN: 1, Level.INFO: 2}
        for f in sorted(self.findings, key=lambda f: order[f.level]):
            lines.append(f"  [{f.level.value}] {f.message}")
        return "\n".join(lines)


# Nigeria, roughly: anything outside is almost always a typo or swapped values.
_NIGERIA = {"lat": (4.0, 14.0), "lon": (2.5, 15.0)}
# Plausible diesel prices per litre, by currency, to catch per-drum or per-kobo slips.
_DIESEL_PRICE = {"NGN": (400.0, 5000.0), "USD": (0.4, 4.0), "EUR": (0.5, 4.0), "GBP": (0.5, 4.0)}
_SITE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def check_portfolio_file(path: str | Path) -> list[SiteCheck]:
    """Read a portfolio file and check every site in it, one by one."""
    try:
        raw: dict[str, Any] = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        check = SiteCheck(str(path), None)
        check.add(Level.FAIL, f"not valid TOML: {exc}. Check quotes and [[site]] headers.")
        return [check]
    blocks = raw.get("site", [])
    if not blocks:
        check = SiteCheck(str(path), None)
        check.add(Level.FAIL, "no [[site]] blocks found")
        return [check]
    checks = []
    seen: dict[tuple[str, str], int] = {}
    for number, block in enumerate(blocks, start=1):
        label = f"{block.get('operator_id', '?')}/{block.get('site_id', '?')} (block {number})"
        try:
            site = Site(**block)
        except ValidationError as exc:
            check = SiteCheck(label, None)
            for error in exc.errors():
                where = ".".join(str(p) for p in error["loc"]) or "site"
                check.add(Level.FAIL, f"{where}: {error['msg']}")
            checks.append(check)
            continue
        check = SiteCheck(f"{site.operator_id}/{site.site_id}", site)
        if site.key in seen:
            check.add(Level.FAIL, f"same operator_id and site_id as block {seen[site.key]}")
        seen[site.key] = number
        check.findings.extend(check_site(site))
        checks.append(check)
    return checks


def check_site(site: Site) -> list[Finding]:
    """Sanity checks on one site's record: values that load but are probably wrong."""
    out: list[Finding] = []

    def add(level: Level, message: str) -> None:
        out.append(Finding(level, message))

    if not _SITE_ID.match(site.site_id) or not _SITE_ID.match(site.operator_id):
        add(
            Level.WARN,
            "operator_id and site_id work best as lowercase letters, digits and dashes "
            "(they appear in web addresses and file names), e.g. lag-ikd-0142.",
        )
    if abs(site.latitude) < 0.01 and abs(site.longitude) < 0.01:
        add(Level.FAIL, "latitude and longitude are 0, 0: fill in the site's real position.")
    elif site.currency == "NGN":
        lat_ok = _NIGERIA["lat"][0] <= site.latitude <= _NIGERIA["lat"][1]
        lon_ok = _NIGERIA["lon"][0] <= site.longitude <= _NIGERIA["lon"][1]
        if not (lat_ok and lon_ok):
            swapped = (
                _NIGERIA["lat"][0] <= site.longitude <= _NIGERIA["lat"][1]
                and _NIGERIA["lon"][0] <= site.latitude <= _NIGERIA["lon"][1]
            )
            add(
                Level.FAIL if swapped else Level.WARN,
                f"position {site.latitude}, {site.longitude} is outside Nigeria"
                + ("; latitude and longitude look swapped." if swapped else ".")
                + " Solar and weather use it.",
            )
        if site.timezone != "Africa/Lagos":
            add(Level.WARN, f"timezone is {site.timezone}; Nigerian sites use Africa/Lagos.")

    if site.value_of_lost_load_per_kwh <= 0:
        add(
            Level.WARN,
            "value_of_lost_load_per_kwh is 0: outages cost nothing, so plans will never run "
            "the generator to prevent one. Ask the operator what an hour without power costs "
            "this site, divided by its load in kW.",
        )

    bat = site.battery
    if bat.min_soc_kwh == 0:
        add(
            Level.WARN,
            "battery.min_soc_kwh is 0: plans may drain the battery completely. Use the "
            "operator's floor (often 40-50% of usable for lead-acid, 10-20% for lithium).",
        )
    if bat.power_kw > 3 * bat.usable_kwh:
        add(
            Level.WARN,
            f"battery.power_kw ({bat.power_kw:g}) is over three times usable_kwh "
            f"({bat.usable_kwh:g}): check both are in kW and kWh.",
        )

    gen = site.generator
    if gen is not None:
        low, high = _DIESEL_PRICE.get(site.currency, (0.0, float("inf")))
        if not low <= gen.fuel_price_per_l <= high:
            add(
                Level.WARN,
                f"generator.fuel_price_per_l is {gen.fuel_price_per_l:,g} {site.currency}: "
                f"expected {low:,g} to {high:,g} per litre. Per drum or per kobo by mistake?",
            )
        if gen.fuel_price_per_l == 0:
            add(Level.FAIL, "generator.fuel_price_per_l is 0: savings would show as zero.")
        if gen.tank_l is None:
            add(
                Level.WARN,
                "generator.tank_l is missing: fuel checks cannot tell a refill from a "
                "delivery that never reached the tank. Read it off the tank or its spec.",
            )
        if gen.rated_kw > 2000:
            add(Level.WARN, f"generator.rated_kw is {gen.rated_kw:,g}: in kW, not watts or kVA?")
    if site.grid is not None:
        if site.grid.reliable:
            add(
                Level.INFO,
                "grid.reliable = true: plans assume the grid is always on. Use it only for a "
                "grid that almost never fails.",
            )
        else:
            add(
                Level.INFO,
                "weak grid: plans forecast grid hours, so the site's readings must include "
                "grid on/off (grid_available).",
            )
        if site.grid.tariff_band is None and site.currency == "NGN":
            add(
                Level.INFO,
                "grid.tariff_band is not set (NERC Band A to E): record it for reporting.",
            )
    if site.solar is None and site.generator is not None and site.grid is None:
        add(Level.INFO, "generator and battery only: plans decide when to run the generator.")
    if site.plan_start_hour != 0:
        add(
            Level.INFO,
            f"plans start at {site.plan_start_hour:02d}:00 local, not midnight: the daily "
            "job must run before then.",
        )
    return out


def check_readiness(
    site: Site,
    readings: pd.DataFrame,
    *,
    recipients: Sequence[Any] | None = None,
    sourced: bool | None = None,
    now: datetime | None = None,
) -> list[Finding]:
    """Whether a site can be planned and measured: its data, its recipients, its source.

    readings: the site's history for the last few weeks (internal schema).
    recipients: the recipients file's entries, or None to skip.
    sourced: whether a sources file entry pulls its readings, or None to skip.
    """
    now = now or datetime.now(tz=UTC)
    out: list[Finding] = []

    def add(level: Level, message: str) -> None:
        out.append(Finding(level, message))

    if readings.empty:
        add(
            Level.FAIL,
            "no readings in the database: connect its monitoring platform "
            "(docs/data-connectors.md) or import an export, then check again.",
        )
    else:
        stamps = pd.to_datetime(readings["timestamp"], utc=True)
        age = pd.Timestamp(now) - stamps.max()
        days = (stamps.max() - stamps.min()) / pd.Timedelta(days=1)
        if age > pd.Timedelta(hours=48):
            add(
                Level.FAIL,
                f"latest reading is {age.days} days old: the data link is down. Plans need "
                "readings from the last day or two.",
            )
        elif age > pd.Timedelta(hours=6):
            add(Level.WARN, f"latest reading is {age / pd.Timedelta(hours=1):.0f} hours old.")
        if days < 14:
            add(
                Level.WARN,
                f"{days:.0f} days of history: plans start after two days, but are much better "
                "from two weeks (Chronos-2) and eight weeks (the quantile model).",
            )
        for column, level, why in (
            (
                "genset_kw",
                Level.WARN,
                "no generator output readings: the pilot report cannot measure this site's "
                "diesel, and fuel checks cannot estimate burn.",
            ),
            (
                "battery_soc_pct",
                Level.INFO,
                "no battery charge readings: plans assume the battery starts half full.",
            ),
            (
                "fuel_level_l",
                Level.INFO,
                "no tank level readings: fuel checks rely on deliveries and burn only.",
            ),
        ):
            if site.generator is None and column in ("genset_kw", "fuel_level_l"):
                continue
            if column not in readings or readings[column].notna().sum() == 0:
                add(level, why)
        if (
            site.grid is not None
            and not site.grid.reliable
            and (
                "grid_available" not in readings or readings["grid_available"].notna().mean() < 0.5
            )
        ):
            add(
                Level.WARN,
                "grid on/off is recorded for under half the hours: plans will not count on "
                "the grid until it is.",
            )
    if recipients is not None:
        covering = [r for r in recipients if r.consent and r.covers(site.operator_id, site.site_id)]
        if not covering:
            add(
                Level.WARN,
                "nobody with consent receives this site's plans (recipients file). Fine for "
                "shadow weeks; add the site team before plans are sent.",
            )
        else:
            languages = sorted({r.language for r in covering})
            add(
                Level.INFO,
                f"{len(covering)} recipient(s) with consent; language(s): {', '.join(languages)}.",
            )
    if sourced is False:
        add(
            Level.INFO,
            "no entry in the sources file: readings must arrive by push "
            "(POST /ingest/...) or import.",
        )
    return out
