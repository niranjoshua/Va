"""What the dashboard may show, and to whom (kept out of app.py so it is testable).

Locally, with no admin token set, the dashboard shows a synthetic demo fleet
without sign-in. In staging and production it asks for an access key (an
operator key, or the admin token), reads real readings from the observation
store, and lists only the sites the key may see.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pandas as pd

from vaticore import access
from vaticore.access import Access
from vaticore.config import Settings
from vaticore.datasets import make_synthetic_fleet
from vaticore.pipeline.store import PlanStore
from vaticore.schemas import OPERATOR_ID, SITE_ID, TIMESTAMP
from vaticore.sites.model import Site, load_portfolio
from vaticore.storage import TimeSeriesRepository

HISTORY_DAYS = 120


@dataclass
class SignInGuard:
    """Slows down guessing: after `limit` failures, wait `lockout_s` seconds."""

    limit: int = 5
    lockout_s: float = 60.0
    failures: list[float] = field(default_factory=list)

    def locked(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        recent = [t for t in self.failures if now - t < self.lockout_s]
        self.failures = recent
        return len(recent) >= self.limit

    def attempt(
        self, key: str, settings: Settings, store: PlanStore, now: float | None = None
    ) -> Access | None:
        if self.locked(now):
            return None
        granted = access.resolve(key, settings, store)
        if granted is None:
            self.failures.append(time.monotonic() if now is None else now)
        return granted


def demo_mode(settings: Settings) -> bool:
    return access.open_without_credentials(settings)


def visible_sites(
    settings: Settings, granted: Access, repo: TimeSeriesRepository | None
) -> list[tuple[str, str]]:
    """The (operator, site) pairs this person may see, sorted."""
    if demo_mode(settings):
        fleet = demo_fleet()
        pairs = fleet[[OPERATOR_ID, SITE_ID]].drop_duplicates().itertuples(index=False)
    else:
        if repo is None:
            raise ValueError("a repository is needed outside the local demo")
        listed = repo.list_sites()
        pairs = listed[[OPERATOR_ID, SITE_ID]].itertuples(index=False)
    return sorted((str(o), str(s)) for o, s in pairs if granted.may_see(str(o)))


def site_history(
    settings: Settings,
    granted: Access,
    repo: TimeSeriesRepository | None,
    operator_id: str,
    site_id: str,
) -> pd.DataFrame:
    """One site's recent readings, if this person may see it."""
    if not granted.may_see(operator_id):
        raise PermissionError("this key belongs to another operator")
    if demo_mode(settings):
        fleet = demo_fleet()
        rows = fleet[(fleet[OPERATOR_ID] == operator_id) & (fleet[SITE_ID] == site_id)]
        return rows.sort_values(TIMESTAMP).reset_index(drop=True)
    if repo is None:
        raise ValueError("a repository is needed outside the local demo")
    end = pd.Timestamp.now(tz="UTC")
    return repo.read_history(operator_id, site_id, end - pd.Timedelta(days=HISTORY_DAYS), end)


def pilot_operators(settings: Settings, granted: Access) -> list[str]:
    """Operators in the portfolio this person may report on."""
    if settings.portfolio_file is None:
        return []
    portfolio = load_portfolio(settings.portfolio_file)
    return sorted({s.operator_id for s in portfolio.sites if granted.may_see(s.operator_id)})


def operator_sites(settings: Settings, granted: Access, operator_id: str) -> list[Site]:
    """One operator's registered sites, for its pilot reports, if this person may see them."""
    if not granted.may_see(operator_id):
        raise PermissionError("this key belongs to another operator")
    if settings.portfolio_file is None:
        return []
    portfolio = load_portfolio(settings.portfolio_file)
    return [s for s in portfolio.sites if s.operator_id == operator_id]


def demo_fleet() -> pd.DataFrame:
    return make_synthetic_fleet(days=90, seed=1)
