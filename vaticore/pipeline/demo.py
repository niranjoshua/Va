"""A full pipeline run on synthetic data: plan, deliver to the console, score.

    uv run python -m vaticore.pipeline demo

Builds a week of daily runs for the example Nigerian portfolio
(examples/sites/nigeria_portfolio.toml) on synthetic readings, in a temporary
in-memory database, and prints each site's messages and track record. Nothing
is sent and nothing is written to disk. It shows exactly what an operator
would receive, and lets anyone check the whole loop works.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd

from vaticore.datasets import make_synthetic_site
from vaticore.delivery.channels import ConsoleChannel
from vaticore.delivery.recipients import Recipient
from vaticore.pipeline.runner import PipelineConfig, plan_window, run_portfolio
from vaticore.pipeline.scoring import score_due, scorecard
from vaticore.pipeline.store import PlanStore
from vaticore.schemas import GENERATION_KW, GRID_AVAILABLE, LOAD_KW, TIMESTAMP
from vaticore.sites.model import Portfolio, Site, SiteType
from vaticore.storage import DuckDBRepository

# Typical average loads by site type, kW (illustrative).
_LOAD_KW = {
    SiteType.TELECOM_TOWER: 3.3,
    SiteType.BANK_BRANCH: 30.0,
    SiteType.INSTITUTION: 80.0,
    SiteType.COMMERCIAL_INDUSTRIAL: 300.0,
    SiteType.MINI_GRID: 30.0,
}


def synthetic_history(site: Site, end: pd.Timestamp, days: int, seed: int) -> pd.DataFrame:
    """Synthetic hourly readings for one registered site, ending at `end`."""
    base = _LOAD_KW.get(site.site_type, 20.0)
    solar = 0.75 * site.solar.kwp if site.solar else 0.0
    frame = make_synthetic_site(
        site.operator_id,
        site.site_id,
        days=days,
        seed=seed,
        base_load_kw=base,
        solar_peak_kw=max(solar, 1e-6),
        gap_fraction=0.01,
    )
    # Shift by whole days so solar stays at midday.
    gap = end - pd.Timestamp(frame[TIMESTAMP].max())
    frame[TIMESTAMP] = pd.DatetimeIndex(frame[TIMESTAMP]) + pd.Timedelta(days=gap.days)
    if site.solar is None:
        frame[GENERATION_KW] = 0.0
    if site.grid is not None and not site.grid.reliable:
        frame[GRID_AVAILABLE] = _weak_grid(pd.DatetimeIndex(frame[TIMESTAMP]), seed + 100)
    frame[LOAD_KW] = frame[LOAD_KW].clip(lower=0.0)
    return frame


def _weak_grid(index: pd.DatetimeIndex, seed: int) -> np.ndarray:
    """Outages in blocks of 2 to 6 hours, one to three a day, likelier in the evening."""
    rng = np.random.default_rng(seed)
    on = np.ones(len(index))
    hours = np.arange(len(index))
    for day_start in range(0, len(index), 24):
        for _ in range(int(rng.integers(1, 4))):
            evening = rng.random() < 0.5
            start = day_start + (int(rng.integers(17, 22)) if evening else int(rng.integers(0, 24)))
            on[(hours >= start) & (hours < start + int(rng.integers(2, 7)))] = 0.0
    return on


def run_demo(
    portfolio: Portfolio,
    *,
    days: int = 7,
    history_days: int = 120,
    printer: Callable[[str], None] = print,
) -> PlanStore:
    """Plan, deliver (console) and score `days` days for every site."""
    repo = DuckDBRepository(":memory:")
    store = PlanStore("duckdb:///:memory:")
    first = date.today() - timedelta(days=days + 1)
    for i, site in enumerate(portfolio.sites):
        start, _ = plan_window(site, first)
        end = start + pd.Timedelta(days=days + 1)
        repo.upsert(synthetic_history(site, end, history_days + days + 1, seed=7 + i))

    people = [
        Recipient(
            name=f"Site manager, {site.name}",
            whatsapp=f"+23480000{i:05d}",
            operator_id=site.operator_id,
            sites=(site.site_id,),
            consent=True,
        )
        for i, site in enumerate(portfolio.sites)
    ]
    silent = ConsoleChannel(printer=lambda _: None)
    console = ConsoleChannel(printer=printer)
    config = PipelineConfig(shadow=False)
    for offset in range(days):
        day = first + timedelta(days=offset)
        last_day = offset == days - 1
        if last_day:
            printer(f"\n===== Messages for {day:%A %d %B %Y} =====\n")
        for site in portfolio.sites:
            start, _ = plan_window(site, day)
            run_portfolio(
                portfolio,
                repo,
                store,
                plan_date=day,
                sites=[site.key],
                channels=[console if last_day else silent],
                recipients=people,
                config=config,
                now=start.to_pydatetime(),
            )
        score_due(portfolio, repo, store, now=datetime.now(tz=UTC))

    printer("\n===== Track record (if each plan had been followed) =====\n")
    for site in portfolio.sites:
        printer(scorecard(store, site.operator_id, site.site_id, days=days + 2).summary())
    return store
