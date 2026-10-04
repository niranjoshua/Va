"""The daily pipeline end to end: health, planning, fallbacks, delivery, scoring.

Sites are small synthetic ones built in code; most runs pin the persistence
model for speed, and one runs the published planning model.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from vaticore import engine
from vaticore.delivery.channels import SendResult
from vaticore.delivery.message import PlanMessage
from vaticore.delivery.recipients import Recipient
from vaticore.pipeline import runner
from vaticore.pipeline.demo import synthetic_history
from vaticore.pipeline.health import HealthStatus, check_health
from vaticore.pipeline.runner import PipelineConfig, plan_window, run_portfolio, run_site
from vaticore.pipeline.scoring import score_day, score_due, scorecard, value_phrase
from vaticore.pipeline.store import PlanStore
from vaticore.schemas import BATTERY_SOC_PCT, GENERATION_KW, GRID_AVAILABLE, LOAD_KW, TIMESTAMP
from vaticore.sites import Site
from vaticore.sites.model import Portfolio
from vaticore.storage import DuckDBRepository

pytestmark = pytest.mark.filterwarnings("ignore")

DAY = date(2026, 3, 10)
FAST = PipelineConfig(model_order=("persistence",), shadow=False)


def _tower(**overrides: object) -> Site:
    spec: dict[str, object] = {
        "operator_id": "op",
        "site_id": "tower-1",
        "name": "Tower One",
        "site_type": "telecom_tower",
        "latitude": 7.8,
        "longitude": 6.7,
        "timezone": "Africa/Lagos",
        "currency": "NGN",
        "value_of_lost_load_per_kwh": 5000.0,
        "battery": {"usable_kwh": 30.0, "power_kw": 15.0, "min_soc_kwh": 6.0},
        "solar": {"kwp": 12.0},
        "generator": {"rated_kw": 16.0, "fuel_price_per_l": 1250.0, "min_run_hours": 2},
    }
    spec.update(overrides)
    return Site(**spec)  # type: ignore[arg-type]


def _grid_site() -> Site:
    return _tower(
        site_id="branch-1",
        name="Branch One",
        site_type="bank_branch",
        grid={"capacity_kw": 20.0, "price_per_kwh": 209.0},
    )


def _fill(repo: DuckDBRepository, site: Site, days: int = 30, extra_days: int = 2) -> None:
    start, _ = plan_window(site, DAY)
    repo.upsert(synthetic_history(site, start + pd.Timedelta(days=extra_days), days, seed=3))


@pytest.fixture
def repo() -> DuckDBRepository:
    return DuckDBRepository(":memory:")


@pytest.fixture
def store() -> PlanStore:
    return PlanStore("duckdb:///:memory:")


class FakeChannel:
    name = "whatsapp"

    def __init__(self) -> None:
        self.sent: list[tuple[str, PlanMessage]] = []

    def address(self, person: Recipient) -> str | None:
        return person.whatsapp

    def send(self, to: str, message: PlanMessage) -> SendResult:
        self.sent.append((to, message))
        return SendResult("sent", provider_message_id=f"wamid.{len(self.sent)}")


def _people(site: Site, consent: bool = True) -> list[Recipient]:
    return [
        Recipient(
            name="Manager",
            whatsapp="+2348000000001",
            operator_id=site.operator_id,
            sites=(site.site_id,),
            consent=consent,
        )
    ]


# -- the plan window ---------------------------------------------------------


def test_plan_window_starts_at_the_local_plan_hour() -> None:
    start, end = plan_window(_tower(), DAY)
    assert start == pd.Timestamp("2026-03-10 05:00", tz="UTC")  # 06:00 in Lagos
    assert end - start == pd.Timedelta(hours=24)
    late, _ = plan_window(_tower(plan_start_hour=22), DAY)
    assert late == pd.Timestamp("2026-03-10 21:00", tz="UTC")


# -- planning -----------------------------------------------------------------


def test_a_site_is_planned_stored_and_messaged(repo: DuckDBRepository, store: PlanStore) -> None:
    site = _tower()
    _fill(repo, site)
    run = run_site(site, repo, store, plan_date=DAY, config=FAST)

    assert run.status == "planned" and run.model == "persistence"
    assert run.health is not None and run.health.status is HealthStatus.OK
    record = store.get_run("op", "tower-1", DAY)
    assert record is not None and record.status == "planned"
    assert record.soc_assumed is True and record.assets is not None
    hours = store.plan_hours("op", "tower-1", DAY)
    assert len(hours) == 24
    assert pd.Timestamp(hours["timestamp"].iloc[0]) == plan_window(site, DAY)[0]
    roles = set(store.forecasts("op", "tower-1", DAY)["role"])
    assert roles == {"primary", "baseline"}

    assert run.message is not None
    assert run.message.text.startswith("Vaticore plan for Tower One, Tue 10 Mar, 06:00")
    assert len(run.message.params) == 6
    assert all("\n" not in p and len(p) <= 200 for p in run.message.params)


def test_the_published_planning_model_plans_with_a_shadow(
    repo: DuckDBRepository, store: PlanStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Chronos-2 is not downloaded in tests: the shadow is skipped, and says so.
    real = engine.net_load_forecast

    def no_chronos(history: pd.DataFrame, **kw: object) -> pd.DataFrame:
        if kw.get("model") == runner.CHRONOS:
            raise ImportError("not installed")
        return real(history, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(engine, "net_load_forecast", no_chronos)
    site = _tower()
    _fill(repo, site, days=70)
    run = run_site(site, repo, store, plan_date=DAY)
    assert run.status == "planned"
    assert run.model == f"{runner.GBM}+conformal"
    assert any("shadow" in note for note in run.fallback)


def test_plans_use_only_readings_from_before_the_plan(
    repo: DuckDBRepository, store: PlanStore
) -> None:
    site = _tower()
    _fill(repo, site, extra_days=0)
    first = run_site(site, repo, store, plan_date=DAY, config=FAST)
    start, _ = plan_window(site, DAY)
    future = pd.DataFrame(
        {
            "operator_id": "op",
            "site_id": "tower-1",
            TIMESTAMP: pd.date_range(start, periods=24, freq="h"),
            LOAD_KW: 999.0,
            GENERATION_KW: 0.0,
        }
    )
    repo.upsert(future)
    second = run_site(site, repo, store, plan_date=DAY, config=FAST)
    assert first.plan is not None and second.plan is not None
    np.testing.assert_allclose(first.plan.planned_net_load_kw, second.plan.planned_net_load_kw)


def test_stale_data_means_no_plan_and_a_plain_reason(
    repo: DuckDBRepository, store: PlanStore
) -> None:
    site = _tower()
    start, _ = plan_window(site, DAY)
    repo.upsert(synthetic_history(site, start - pd.Timedelta(days=3), 20, seed=1))
    run = run_site(site, repo, store, plan_date=DAY, config=FAST)
    assert run.status == "no_plan"
    assert run.message is not None and "No readings since" in run.message.note
    assert "no plan today" in run.message.generator
    assert store.get_run("op", "tower-1", DAY).status == "no_plan"  # type: ignore[union-attr]


def test_models_fall_back_and_say_why(
    repo: DuckDBRepository, store: PlanStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = engine.net_load_forecast

    def broken(history: pd.DataFrame, **kw: object) -> pd.DataFrame:
        if kw.get("model") != "persistence":
            raise RuntimeError("model unavailable")
        return real(history, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(engine, "net_load_forecast", broken)
    site = _tower()
    _fill(repo, site)
    run = run_site(site, repo, store, plan_date=DAY, config=PipelineConfig(shadow=False))
    assert run.status == "planned" and run.model == "persistence"
    assert len(run.fallback) == 4  # two models, calibrated and not
    assert "unavailable" in (store.get_run("op", "tower-1", DAY).fallback or "")  # type: ignore[union-attr]


def test_one_failing_site_does_not_stop_the_others(
    repo: DuckDBRepository, store: PlanStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    good, bad = _tower(), _tower(site_id="tower-2", name="Tower Two")
    _fill(repo, good)
    _fill(repo, bad)
    real = runner._prepare_history

    def explode(site: Site, *args: object) -> pd.DataFrame:
        if site.site_id == "tower-2":
            raise RuntimeError("boom")
        return real(site, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_prepare_history", explode)
    runs = run_portfolio(Portfolio(sites=(good, bad)), repo, store, plan_date=DAY, config=FAST)
    assert [r.status for r in runs] == ["planned", "failed"]
    assert "boom" in (runs[1].error or "")
    failed = store.get_run("op", "tower-2", DAY)
    assert failed is not None and failed.status == "failed" and "boom" in (failed.error or "")


def test_a_grid_site_without_a_grid_record_does_not_count_on_the_grid(
    repo: DuckDBRepository, store: PlanStore
) -> None:
    site = _grid_site()
    start, _ = plan_window(site, DAY)
    history = synthetic_history(site, start + pd.Timedelta(days=1), 30, seed=2)
    repo.upsert(history.drop(columns=[GRID_AVAILABLE]))
    run = run_site(site, repo, store, plan_date=DAY, config=FAST)
    assert run.status == "planned"
    assert run.health is not None
    assert "no_grid_record" in {i.code for i in run.health.issues}
    assert run.plan is not None and not run.plan.planned_grid_on.any()
    assert run.message is not None and run.message.grid == "not counted on today."


def test_a_grid_site_with_a_record_counts_on_the_grid(
    repo: DuckDBRepository, store: PlanStore
) -> None:
    site = _grid_site()
    _fill(repo, site)
    run = run_site(site, repo, store, plan_date=DAY, config=FAST)
    assert run.plan is not None and run.plan.planned_grid_on.any()
    assert run.message is not None and run.message.grid.startswith("counted on")


# -- delivery ------------------------------------------------------------------


def test_each_plan_is_sent_once_and_only_with_consent(
    repo: DuckDBRepository, store: PlanStore
) -> None:
    site = _tower()
    _fill(repo, site)
    channel = FakeChannel()
    people = [*_people(site), *(_people(site, consent=False))]
    portfolio = Portfolio(sites=(site,))

    first = run_portfolio(
        portfolio, repo, store, plan_date=DAY, channels=[channel], recipients=people, config=FAST
    )
    assert len(channel.sent) == 1  # the person without consent is never messaged
    assert first[0].deliveries == [("+234******0001", "whatsapp sent")]

    again = run_portfolio(
        portfolio, repo, store, plan_date=DAY, channels=[channel], recipients=people, config=FAST
    )
    assert len(channel.sent) == 1
    assert again[0].deliveries == [("+234******0001", "whatsapp already_sent")]

    forced = run_portfolio(
        portfolio,
        repo,
        store,
        plan_date=DAY,
        channels=[channel],
        recipients=people,
        config=FAST,
        force=True,
    )
    assert len(channel.sent) == 2 and forced[0].deliveries[0][1] == "whatsapp sent"
    sent = store.deliveries("op")
    assert list(sent["status"]) == ["sent"] and sent["provider_message_id"].iloc[0] == "wamid.2"
    assert "2348000000001" not in sent.to_string()  # only the masked number is stored


def test_a_stored_plan_is_resent_exactly_not_replanned(
    repo: DuckDBRepository, store: PlanStore
) -> None:
    site = _tower()
    _fill(repo, site)
    portfolio = Portfolio(sites=(site,))
    run_portfolio(portfolio, repo, store, plan_date=DAY, config=FAST)  # planned, not sent
    channel = FakeChannel()
    run_portfolio(
        portfolio,
        repo,
        store,
        plan_date=DAY,
        channels=[channel],
        recipients=_people(site),
        config=PipelineConfig(model_order=(runner.GBM,), shadow=False),
    )
    assert store.get_run("op", "tower-1", DAY).model == "persistence"  # type: ignore[union-attr]
    assert channel.sent and channel.sent[0][1].site_name == "Tower One"


def test_people_who_replied_stop_are_not_messaged(repo: DuckDBRepository, store: PlanStore) -> None:
    site = _tower()
    _fill(repo, site)
    people = _people(site)
    store.set_opt_out(people[0].hash, True, "test", datetime(2026, 3, 1))
    channel = FakeChannel()
    runs = run_portfolio(
        Portfolio(sites=(site,)),
        repo,
        store,
        plan_date=DAY,
        channels=[channel],
        recipients=people,
        config=FAST,
    )
    assert channel.sent == []
    assert runs[0].deliveries == [("+234******0001", "opted_out")]


class FakeEmail(FakeChannel):
    name = "email"

    def address(self, person: Recipient) -> str | None:
        return person.email


def test_each_channel_reaches_people_who_use_it_and_stop_ends_both(
    repo: DuckDBRepository, store: PlanStore
) -> None:
    site = _tower()
    _fill(repo, site)
    person = Recipient(
        name="Ada", whatsapp="+2348000000001", email="ada@bank.ng", operator_id="op",
        sites=(site.site_id,), consent=True,
    )  # fmt: skip
    email_only = Recipient(name="Obi", email="obi@bank.ng", operator_id="op",
                           sites=(site.site_id,), consent=True)  # fmt: skip
    whatsapp, email = FakeChannel(), FakeEmail()
    runs = run_portfolio(
        Portfolio(sites=(site,)), repo, store, plan_date=DAY, channels=[whatsapp, email],
        recipients=[person, email_only], config=FAST,
    )  # fmt: skip
    assert [to for to, _ in whatsapp.sent] == ["+2348000000001"]
    assert [to for to, _ in email.sent] == ["ada@bank.ng", "obi@bank.ng"]
    assert runs[0].deliveries == [
        ("+234******0001", "whatsapp sent"),
        ("a**@bank.ng", "email sent"),
        ("o**@bank.ng", "email sent"),
    ]
    assert "ada@bank.ng" not in store.deliveries("op").to_string()

    # STOP sent on WhatsApp also stops the email to the same person.
    store.set_opt_out(person.hashes[0], True, "test", datetime(2026, 3, 10, 12))
    runs = run_portfolio(
        Portfolio(sites=(site,)), repo, store, plan_date=DAY, channels=[whatsapp, email],
        recipients=[person, email_only], config=FAST, force=True,
    )  # fmt: skip
    assert runs[0].deliveries[0] == ("+234******0001", "opted_out")
    assert [to for to, _ in email.sent][-1] == "obi@bank.ng" and len(email.sent) == 3


def test_the_plan_starts_from_the_measured_battery_charge(
    repo: DuckDBRepository, store: PlanStore
) -> None:
    site = _tower()
    start, _ = plan_window(site, DAY)
    history = synthetic_history(site, start, 30, seed=3)  # last reading 6 h before the plan
    history[BATTERY_SOC_PCT] = np.nan
    history.loc[history.index[-1], BATTERY_SOC_PCT] = 80.0
    repo.upsert(history)
    recent = PipelineConfig(model_order=("persistence",), shadow=False, soc_max_age_hours=8)
    run = run_site(site, repo, store, plan_date=DAY, config=recent)
    record = store.get_run("op", "tower-1", DAY)
    assert run.status == "planned" and record is not None
    assert record.soc_assumed is False and record.soc_start_kwh == pytest.approx(24.0)

    # A reading older than the limit (3 h by default) is not trusted: the plan
    # assumes a charge and says so.
    run_site(site, repo, store, plan_date=DAY, config=FAST)
    record = store.get_run("op", "tower-1", DAY)
    assert record is not None and record.soc_assumed is True
    assert record.soc_start_kwh == pytest.approx(6.0 + 0.5 * 24.0)


# -- scoring ---------------------------------------------------------------------


def test_a_finished_day_is_scored_against_baseline_and_bound(
    repo: DuckDBRepository, store: PlanStore
) -> None:
    site = _tower()
    _fill(repo, site)
    run_site(site, repo, store, plan_date=DAY, config=FAST)
    _, end = plan_window(site, DAY)
    record = score_day(site, repo, store, DAY, now=(end + pd.Timedelta(hours=2)).to_pydatetime())

    assert record is not None and record.hours_scored >= 18
    assert record.coverage_primary is not None and 0.0 <= record.coverage_primary <= 1.0
    assert record.pinball_primary is not None and record.pinball_primary >= 0.0
    assert record.unserved_perfect_kwh <= record.unserved_baseline_kwh + 1e-6
    assert set(record.detail["models"]) == {"persistence", "persistence (baseline)"}
    assert store.unscored(end.to_pydatetime() + pd.Timedelta(days=1)) == []

    card = scorecard(store, "op", "tower-1", days=10_000)
    assert card.days == 1 and "scored days" in card.summary()


def test_scoring_waits_for_readings(repo: DuckDBRepository, store: PlanStore) -> None:
    site = _tower()
    _fill(repo, site, extra_days=0)  # readings stop when the plan starts
    run_site(site, repo, store, plan_date=DAY, config=FAST)
    _, end = plan_window(site, DAY)
    soon = (end + pd.Timedelta(hours=2)).to_pydatetime()
    assert score_day(site, repo, store, DAY, now=soon) is None
    late = (end + pd.Timedelta(days=4)).to_pydatetime()
    record = score_day(site, repo, store, DAY, now=late)
    assert record is not None and record.hours_scored == 0 and record.hours_missing == 24


def test_the_message_quotes_a_track_record_once_there_is_one(
    repo: DuckDBRepository, store: PlanStore
) -> None:
    site = _tower()
    start, _ = plan_window(site, DAY)
    repo.upsert(synthetic_history(site, start + pd.Timedelta(days=6), 40, seed=4))
    portfolio = Portfolio(sites=(site,))
    for offset in range(4):
        day = date.fromordinal(DAY.toordinal() + offset)
        run = run_site(site, repo, store, plan_date=day, config=FAST)
        _, end = plan_window(site, day)
        score_due(portfolio, repo, store, now=end.to_pydatetime())
    assert run.message is not None
    assert run.message.note.startswith("Data OK. Last 3 days:")
    assert "against planning from yesterday" in run.message.note


@pytest.mark.parametrize(
    ("litres", "outages", "phrase"),
    [
        (120.0, 0.0, "saved 120 L of diesel"),
        (120.0, 30.0, "saved 120 L of diesel and avoided 30 kWh of outages"),
        (-40.0, 25.0, "used 40 L more diesel to avoid 25 kWh of outages"),
        (-40.0, 0.0, "used 40 L more diesel with no fewer outages"),
        (0.0, 12.0, "avoided 12 kWh of outages for the same diesel"),
        (0.2, 0.1, "made no measurable difference"),
    ],
)
def test_value_is_stated_honestly_in_either_direction(
    litres: float, outages: float, phrase: str
) -> None:
    assert value_phrase(litres, outages) == phrase


# -- data health ----------------------------------------------------------------


def _history(hours: int, end: pd.Timestamp, load: float | None = 3.0) -> pd.DataFrame:
    index = pd.date_range(end=end - pd.Timedelta(hours=1), periods=hours, freq="h")
    rng = np.random.default_rng(0)
    values = None if load is None else load + rng.normal(0, 0.2, hours)
    return pd.DataFrame({TIMESTAMP: index, LOAD_KW: values, GENERATION_KW: 0.0})


def test_health_checks() -> None:
    start = pd.Timestamp("2026-03-10 05:00", tz="UTC")
    assert check_health(_history(24 * 10, start), start).status is HealthStatus.OK

    stale = check_health(_history(24 * 10, start - pd.Timedelta(hours=6)), start)
    assert stale.status is HealthStatus.WARN and stale.issues[0].code == "stale"
    dead = check_health(_history(24 * 10, start - pd.Timedelta(days=2)), start)
    assert dead.status is HealthStatus.FAIL and not dead.ok_to_plan

    short = check_health(_history(30, start), start)
    assert "short_history" in {i.code for i in short.issues} and not short.ok_to_plan

    gappy = _history(24 * 10, start)
    gappy.loc[gappy.index[-40:-10], LOAD_KW] = np.nan
    assert "missing" in {i.code for i in check_health(gappy, start).issues}

    stuck = _history(24 * 10, start)
    stuck.loc[stuck.index[-14:], LOAD_KW] = 4.2
    assert "stuck_meter" in {i.code for i in check_health(stuck, start).issues}

    no_solar = _history(24 * 10, start).assign(**{GENERATION_KW: np.nan})
    assert check_health(no_solar, start, has_solar=False).status is HealthStatus.OK
    assert "no_solar_record" in {i.code for i in check_health(no_solar, start).issues}

    grid = check_health(_history(24 * 10, start), start, has_grid=True)
    assert "no_grid_record" in {i.code for i in grid.issues}
    assert check_health(_history(0, start), start).status is HealthStatus.FAIL
