"""Pilot tools: never silent, each person's language, site checks, the shadow verdict.

Each test has a known answer: a site whose planning crashes, a site the daily
job never reached, a Pidgin speaker, a portfolio file with planted mistakes,
and shadow weeks with sites built to pass, fail and fall short.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pytest

from vaticore.delivery.channels import SendResult, WhatsAppChannel
from vaticore.delivery.message import (
    PIDGIN_TEMPLATE_BODY,
    TrackRecord,
    data_note,
    plan_message,
    track_text,
    value_words,
)
from vaticore.delivery.recipients import Recipient, recipient_hash
from vaticore.delivery.webhook import REASON_QUESTIONS, handle_webhook
from vaticore.pipeline.cli import extra_templates
from vaticore.pipeline.runner import (
    SiteRun,
    message_for,
    plan_window,
    run_portfolio,
    run_site,
    send_missing,
)
from vaticore.pipeline.savings import Period
from vaticore.pipeline.shadow import shadow_review
from vaticore.pipeline.store import PlanStore, RunRecord, ScoreRecord
from vaticore.sites import Site
from vaticore.sites.check import Level, check_portfolio_file, check_readiness
from vaticore.sites.model import Portfolio

pytestmark = pytest.mark.filterwarnings("ignore")

EVENING = datetime(2026, 10, 5, 17, 30, tzinfo=UTC)  # 18:30 in Lagos
TOMORROW = date(2026, 10, 6)


def _site(site_id: str = "ikd-01", name: str = "Ikorodu tower") -> Site:
    return Site(
        operator_id="op", site_id=site_id, name=name, site_type="telecom_tower",
        latitude=6.6, longitude=3.5, timezone="Africa/Lagos", currency="NGN",
        value_of_lost_load_per_kwh=5000.0,
        battery={"usable_kwh": 20.0, "power_kw": 10.0, "min_soc_kwh": 4.0},
        generator={"rated_kw": 20.0, "fuel_price_per_l": 1000.0, "tank_l": 400.0},
    )  # fmt: skip


SITE = _site()
PEOPLE = [
    Recipient(name="Technician", whatsapp="+2348000000001", operator_id="op",
              sites=("ikd-01",), consent=True, language="pcm"),
    Recipient(name="Supervisor", whatsapp="+2348000000002", operator_id="op",
              sites=("*",), consent=True),
]  # fmt: skip


class _Phone:
    """A WhatsApp-like channel that records what it sent; Pidgin approved or not."""

    name = "whatsapp"

    def __init__(self, languages: tuple[str, ...] = ("en", "pcm")) -> None:
        self.languages = frozenset(languages)
        self.sent: list[tuple[str, Any]] = []

    def address(self, person: Recipient) -> str | None:
        return person.whatsapp

    def send(self, to: str, message: Any) -> SendResult:
        self.sent.append((to, message))
        return SendResult("sent", f"wamid.{len(self.sent)}")


class _BrokenRepo:
    def read_history(self, *args: object, **kwargs: object) -> pd.DataFrame:
        raise RuntimeError("database unreachable")


# -- never silent ----------------------------------------------------------------


def test_a_crash_while_planning_still_tells_the_site_there_is_no_plan() -> None:
    store = PlanStore("duckdb:///:memory:")
    run = run_site(SITE, _BrokenRepo(), store, plan_date=TOMORROW, now=EVENING)  # type: ignore[arg-type]
    assert run.status == "failed" and "database unreachable" in (run.error or "")
    assert run.message is not None
    assert run.message.generator == "no plan today; run the site as usual."
    assert "fault on our side" in run.message.note
    assert run.translations["pcm"].generator == "no plan today; run the site as normal."
    assert "wahala for our side" in run.translations["pcm"].note
    stored = store.get_run("op", "ikd-01", TOMORROW)
    assert stored is not None and stored.status == "failed" and stored.message


def test_the_safety_net_covers_a_site_the_daily_job_never_reached() -> None:
    store = PlanStore("duckdb:///:memory:")
    phone = _Phone()
    portfolio = Portfolio(sites=(SITE,))
    later = EVENING + timedelta(hours=1)
    acted = send_missing(portfolio, store, channels=[phone], recipients=PEOPLE, now=later)
    assert [r.status for r in acted] == ["missed"]
    assert len(phone.sent) == 2  # both people, each in their language
    by_number = {to: m for to, m in phone.sent}
    assert by_number["+2348000000001"].language == "pcm"
    assert "planning no run today" in by_number["+2348000000001"].note
    assert "did not run today" in by_number["+2348000000002"].note
    assert store.get_run("op", "ikd-01", TOMORROW).status == "missed"  # type: ignore[union-attr]

    # Run again: everyone already has it, so nothing is sent.
    assert send_missing(portfolio, store, channels=[phone], recipients=PEOPLE, now=later) == []
    assert len(phone.sent) == 2
    # The daily job, arriving late, does not contradict "run as usual" with a plan.
    run_portfolio(portfolio, _BrokenRepo(), store, plan_date=TOMORROW, channels=[phone],  # type: ignore[arg-type]
                  recipients=PEOPLE, now=later)  # fmt: skip
    assert len(phone.sent) == 2


def test_the_safety_net_resends_a_plan_whose_send_failed() -> None:
    store = PlanStore("duckdb:///:memory:")
    start, end = plan_window(SITE, TOMORROW)
    message = plan_message(
        site_name=SITE.name, timezone=SITE.timezone, start=start, end=end,
        timestamps=pd.date_range(start, periods=24, freq="h"), genset_on=[False] * 24,
        grid_on=None, has_generator=True, fuel_l=0.0, genset_hours=0.0, unserved_kwh=0.0,
        soc_start_pct=50.0, soc_end_pct=40.0, soc_assumed=False, data_note="Data OK.",
        track_note=None,
    )  # fmt: skip
    import json

    store.save_run(
        RunRecord(
            operator_id="op", site_id="ikd-01", plan_date=TOMORROW,
            plan_start=start.to_pydatetime(), plan_end=end.to_pydatetime(),
            issued_at=EVENING, status="planned",
            message=json.dumps({"text": message.text, "fields": message.to_dict()}),
        )
    )  # fmt: skip
    store.record_delivery(
        operator_id="op", site_id="ikd-01", plan_date=TOMORROW, channel="whatsapp",
        recipient_hash=recipient_hash("+2348000000002"), recipient_masked="x",
        status="failed", provider_message_id=None, error="HTTP 500", attempts=3, at=EVENING,
    )  # fmt: skip
    phone = _Phone()
    acted = send_missing(
        Portfolio(sites=(SITE,)), store, channels=[phone],
        recipients=[PEOPLE[1]], now=EVENING + timedelta(hours=1),
    )  # fmt: skip
    assert [r.status for r in acted] == ["planned"]
    assert phone.sent[0][1].generator == "not needed today."


# -- languages -------------------------------------------------------------------


def _pidgin_plan(language: str) -> Any:
    start, end = plan_window(SITE, TOMORROW)
    on = [False] * 18 + [True] * 3 + [False] * 3
    return plan_message(
        site_name=SITE.name, timezone=SITE.timezone, start=start, end=end,
        timestamps=pd.date_range(start, periods=24, freq="h"), genset_on=on,
        grid_on=[False] * 6 + [True] * 8 + [False] * 10, has_generator=True, fuel_l=21.4,
        genset_hours=3.0, unserved_kwh=0.0, soc_start_pct=60.0, soc_end_pct=35.0,
        soc_assumed=True, data_note=data_note(language, "Data OK.", []),
        track_note=track_text(TrackRecord(7, 0.81, 30.0, 4.0), language), language=language,
    )  # fmt: skip


def test_a_plan_reads_in_pidgin_and_english_says_the_same() -> None:
    pcm, en = _pidgin_plan("pcm"), _pidgin_plan("en")
    assert pcm.generator == "on am 18:00 to 21:00 (3 hours, 1 time, about 21 L)."
    assert en.generator == "18:00 to 21:00 (3 h in 1 run, about 21 L)."
    assert pcm.grid == "we dey expect am 06:00 to 14:00; if e no come, on the generator."
    assert pcm.battery == "e go start around 60% (na guess), end around 35%."
    assert pcm.note.startswith("Data dey OK. For last 7 days: the forecast range catch 81%")
    assert "una for save 30 L diesel and stop 4 kWh of outage" in pcm.note
    assert en.note == (
        "Data OK. Last 7 days: the forecast range held 81% of hours; following the plans "
        "would have saved 30 L of diesel and avoided 4 kWh of outages (against planning "
        "from yesterday)."
    )
    assert pcm.text.endswith("STOP make we stop dis messages.") and "Na advice be dis" in pcm.text
    assert len(pcm.params) == 6 and all("\n" not in p for p in pcm.params)
    slots = [PIDGIN_TEMPLATE_BODY.index("{{" + str(n) + "}}") for n in range(1, 7)]
    assert slots == sorted(slots)  # the six variables, in order
    assert "Na advice be dis" in pcm.email_text and "Una follow the plan?" in pcm.email_text
    assert "—" not in pcm.text + pcm.email_text


def test_health_notes_and_values_in_pidgin() -> None:
    assert data_note("pcm", "No readings since ...", ["stale"]).startswith("Readings from the site")
    assert data_note("en", "No readings since 10:00.", ["stale"]) == "No readings since 10:00."
    assert value_words(-12, 30, "pcm") == "use 12 L more diesel to stop 30 kWh of outage"
    assert value_words(-12, 30) == "used 12 L more diesel to avoid 30 kWh of outages"


def test_each_person_gets_their_language_once_its_template_is_approved() -> None:
    run = SiteRun("op", "ikd-01", TOMORROW, "planned", message=_pidgin_plan("en"),
                  translations={"pcm": _pidgin_plan("pcm")})  # fmt: skip
    assert message_for(run, "pcm", _Phone(("en", "pcm"))).language == "pcm"
    assert message_for(run, "pcm", _Phone(("en",))).language == "en"  # not approved yet
    assert message_for(run, "en", _Phone()).language == "en"
    with pytest.raises(ValueError, match="not one of"):
        Recipient(name="x", whatsapp="+2348000000003", operator_id="op", sites=("*",),
                  consent=True, language="pidgin")  # fmt: skip


def test_whatsapp_sends_each_language_through_its_own_template() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.x"}]})

    channel = WhatsAppChannel(
        token="t", phone_number_id="1", client=httpx.Client(transport=httpx.MockTransport(handler)),
        templates=extra_templates("pcm=vaticore_daily_plan_pcm:en"),
    )  # fmt: skip
    assert channel.languages == frozenset({"en", "pcm"})
    channel.send("+2348000000001", _pidgin_plan("pcm"))
    channel.send("+2348000000001", _pidgin_plan("en"))
    assert [b["template"]["name"] for b in bodies] == [
        "vaticore_daily_plan_pcm",
        "vaticore_daily_plan",
    ]
    assert bodies[0]["template"]["language"] == {"code": "en"}
    with pytest.raises(SystemExit, match="language=template:code"):
        extra_templates("pcm=vaticore_daily_plan_pcm")


def test_the_why_question_comes_in_the_language_of_the_plan() -> None:
    store = PlanStore("duckdb:///:memory:")
    who = recipient_hash("+2348000000001")
    store.record_delivery(
        operator_id="op", site_id="ikd-01", plan_date=TOMORROW, channel="whatsapp",
        recipient_hash=who, recipient_masked="x", status="read",
        provider_message_id="wamid.1", error=None, attempts=1, at=EVENING, language="pcm",
    )  # fmt: skip
    asked: list[str] = []
    message = {"id": "in.1", "from": "2348000000001", "type": "text", "text": {"body": "2"},
               "timestamp": str(int((EVENING + timedelta(hours=20)).timestamp()))}  # fmt: skip
    handle_webhook(
        store, {"entry": [{"changes": [{"value": {"messages": [message]}}]}]},
        send_text=lambda to, text: asked.append(text),
    )  # fmt: skip
    assert asked == [REASON_QUESTIONS["pcm"]]


# -- site check ------------------------------------------------------------------


def test_the_example_portfolio_has_nothing_to_fix() -> None:
    checks = check_portfolio_file("examples/sites/nigeria_portfolio.toml")
    assert checks and all(c.status is not Level.FAIL for c in checks)


def test_a_portfolio_with_planted_mistakes(tmp_path: Path) -> None:
    good = (
        '[[site]]\noperator_id = "op"\nsite_id = "ok-1"\nname = "Fine"\n'
        'site_type = "telecom_tower"\nlatitude = 6.5\nlongitude = 3.4\n'
        'timezone = "Africa/Lagos"\ncurrency = "NGN"\nvalue_of_lost_load_per_kwh = 5000.0\n'
        "[site.battery]\nusable_kwh = 20.0\npower_kw = 10.0\nmin_soc_kwh = 8.0\n"
        "[site.generator]\nrated_kw = 15.0\nfuel_price_per_l = 1250.0\ntank_l = 300.0\n"
    )
    swapped = good.replace('"ok-1"', '"swap-1"').replace("latitude = 6.5\nlongitude = 3.4",
                                                         "latitude = 3.4\nlongitude = 6.5")  # fmt: skip
    drum = good.replace('"ok-1"', '"drum-1"').replace("1250.0", "250000.0")
    broken = good.replace('"ok-1"', '"bad-1"').replace('"Africa/Lagos"', '"Lagos"')
    path = tmp_path / "portfolio.toml"
    path.write_text("\n".join([good, swapped, drum, broken, good]))
    checks = {c.label: c for c in check_portfolio_file(path)}
    assert checks["op/ok-1"].status is Level.FAIL  # the duplicate block
    assert any("same operator_id and site_id" in f.message for f in checks["op/ok-1"].findings)
    assert any("look swapped" in f.message for f in checks["op/swap-1"].findings)
    assert checks["op/drum-1"].status is Level.WARN
    assert any("Per drum" in f.message for f in checks["op/drum-1"].findings)
    bad = next(c for label, c in checks.items() if label.startswith("op/bad-1"))
    assert bad.status is Level.FAIL and "unknown timezone" in bad.to_text()
    path.write_text("[[site]\nbroken")
    assert check_portfolio_file(path)[0].status is Level.FAIL


def test_readiness_names_what_is_missing() -> None:
    now = datetime(2026, 10, 5, 12, tzinfo=UTC)
    empty = check_readiness(SITE, pd.DataFrame(), recipients=[], sourced=False, now=now)
    assert [f.level for f in empty][:1] == [Level.FAIL]
    assert any("nobody with consent" in f.message for f in empty)
    stamps = pd.date_range(now - pd.Timedelta(days=5), periods=24 * 4, freq="h")
    readings = pd.DataFrame({"timestamp": stamps, "load_kw": 5.0})
    found = check_readiness(SITE, readings, recipients=PEOPLE, now=now)
    messages = " ".join(f.message for f in found)
    assert "days old" not in messages and "hours old" in messages  # a day stale: warn
    assert "4 days of history" in messages or "3 days of history" in messages
    assert "no generator output readings" in messages
    assert "language(s): en, pcm" in messages


# -- shadow verdict --------------------------------------------------------------


def _scored_days(
    store: PlanStore, site: Site, days: int, *, pinball: float, held: float, saved_l: float,
    start: date = date(2026, 9, 1),
) -> None:  # fmt: skip
    for i in range(days):
        day = start + timedelta(days=i)
        store.save_score(
            ScoreRecord(
                operator_id=site.operator_id, site_id=site.site_id, plan_date=day,
                scored_at=datetime(2026, 10, 1, tzinfo=UTC), hours_scored=24, hours_missing=0,
                pinball_primary=pinball, coverage_primary=held,
                fuel_plan_l=20.0 - saved_l, fuel_baseline_l=20.0, fuel_perfect_l=14.0,
                unserved_plan_kwh=1.0, unserved_baseline_kwh=1.0, unserved_perfect_kwh=0.0,
                cost_plan=1000 * (20.0 - saved_l) + 5000, cost_baseline=25_000.0,
                cost_perfect=19_000.0,
                detail={"models": {
                    "quantile_gbm_day_ahead+conformal": {"role": "primary", "pinball": pinball},
                    "persistence (baseline)": {"role": "baseline", "pinball": 10.0},
                }},
            )
        )  # fmt: skip


def test_the_shadow_verdict_follows_the_rules_set_beforehand() -> None:
    store = PlanStore("duckdb:///:memory:")
    good, worse, short = _site("go-1", "Good"), _site("no-1", "Worse"), _site("ny-1", "Short")
    _scored_days(store, good, 28, pinball=7.0, held=0.79, saved_l=2.0)
    _scored_days(store, worse, 28, pinball=10.5, held=0.80, saved_l=-1.0)
    _scored_days(store, short, 10, pinball=7.0, held=0.80, saved_l=2.0)
    review = shadow_review([good, worse, short], store, Period(date(2026, 9, 1), date(2026, 9, 28)))
    verdicts = {s.site.site_id: s.verdict for s in review.sites}
    assert verdicts == {"go-1": "GO", "no-1": "NO-GO", "ny-1": "NOT YET"}
    assert review.verdict == "GO FOR SOME SITES"
    good_review = next(s for s in review.sites if s.site.site_id == "go-1")
    assert good_review.skill == pytest.approx(0.3)
    assert good_review.litres_saved == pytest.approx(56.0)
    text = review.to_markdown()
    assert "**Verdict: GO FOR SOME SITES**" in text
    assert "the forecast is no better than persistence" in text
    assert "10 of 28 days scored" in text
    assert review.to_dict()["rules"]["min_skill"] == 0.10


# -- the API ---------------------------------------------------------------------


def test_operators_open_their_own_pilot_reports_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from vaticore.api.main import create_app, get_app_settings, get_plan_store, get_repository
    from vaticore.config import Settings
    from vaticore.storage import DuckDBRepository

    portfolio = tmp_path / "sites.toml"
    portfolio.write_text(
        '[[site]]\noperator_id = "op"\nsite_id = "ikd-01"\nname = "Ikorodu"\n'
        'site_type = "telecom_tower"\nlatitude = 6.6\nlongitude = 3.5\n'
        'timezone = "Africa/Lagos"\ncurrency = "NGN"\nvalue_of_lost_load_per_kwh = 5000.0\n'
        "[site.battery]\nusable_kwh = 20.0\npower_kw = 10.0\nmin_soc_kwh = 4.0\n"
        "[site.generator]\nrated_kw = 20.0\nfuel_price_per_l = 1000.0\ntank_l = 400.0\n"
    )
    monkeypatch.setenv("VATICORE_PORTFOLIO_FILE", str(portfolio))
    store = PlanStore("duckdb:///:memory:")
    app = create_app()
    app.dependency_overrides[get_app_settings] = lambda: Settings(environment="production")  # type: ignore[call-arg]
    app.dependency_overrides[get_plan_store] = lambda: store
    app.dependency_overrides[get_repository] = lambda: DuckDBRepository(":memory:")
    client = TestClient(app)
    mine = {"Authorization": f"Bearer {store.create_api_key('op', 'ops', EVENING)}"}
    theirs = {"Authorization": f"Bearer {store.create_api_key('rival', 'x', EVENING)}"}

    path = "/operators/op/report?baseline=2026-09-01:2026-09-14&pilot=2026-09-15:2026-09-28"
    resp = client.get(path, headers=mine)
    assert resp.status_code == 200
    assert resp.json()["markdown"].startswith("# Vaticore pilot report: op")
    assert client.get(path, headers=theirs).status_code == 403
    assert client.get(path).status_code == 401
    bad = "/operators/op/report?baseline=2026-09-15:2026-09-28&pilot=2026-09-15:2026-09-28"
    assert client.get(bad, headers=mine).status_code == 422

    review = client.get("/operators/op/shadow-review?days=28", headers=mine)
    assert review.status_code == 200 and review.json()["verdict"] == "NOT YET"
    assert client.get("/operators/nobody/shadow-review", headers=mine).status_code == 403


def test_a_csv_export_is_pushed_to_the_api_in_batches(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    import sys

    from fastapi.testclient import TestClient

    from vaticore.api.main import create_app, get_app_settings, get_plan_store, get_repository
    from vaticore.config import Settings
    from vaticore.storage import DuckDBRepository

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
    import push_csv

    stamps = pd.date_range("2026-09-01", periods=7000, freq="h")
    csv = tmp_path / "export.csv"
    pd.DataFrame(
        {"Time": stamps.strftime("%Y-%m-%d %H:%M"), "Load (kW)": 5.0, "Genset (kW)": 0.0}
    ).to_csv(csv, index=False)
    repo, store = DuckDBRepository(":memory:"), PlanStore("duckdb:///:memory:")
    app = create_app()
    app.dependency_overrides[get_app_settings] = lambda: Settings(api_token="admin")  # type: ignore[call-arg]
    app.dependency_overrides[get_plan_store] = lambda: store
    app.dependency_overrides[get_repository] = lambda: repo
    client = TestClient(app)
    original = push_csv.httpx.Client
    push_csv.httpx.Client = lambda **kw: client  # type: ignore[assignment,misc]
    try:
        code = push_csv.main([
            str(csv), "--api", "http://test", "--operator", "op", "--site", "ikd-01",
            "--key", "admin", "--timezone", "Africa/Lagos",
            "--map", "Time=timestamp", "--map", "Load (kW)=load_kw", "--map", "Genset (kW)=genset_kw",
        ])  # fmt: skip
    finally:
        push_csv.httpx.Client = original  # type: ignore[misc]
    assert code == 0
    stored = repo.read_history("op", "ikd-01")
    assert len(stored) == 7000 and "genset_kw" in stored
    with pytest.raises(SystemExit, match="--map"):
        push_csv.main([str(csv), "--api", "x", "--operator", "o", "--site", "s", "--key", "k",
                       "--map", "Time=time"])  # fmt: skip


def test_the_onboarding_form_example_passes_the_site_check(tmp_path: Path) -> None:
    form = (Path(__file__).resolve().parents[1] / "docs/pilot/site-onboarding-form.md").read_text()
    block = form.split("```toml\n", 1)[1].split("```", 1)[0]
    path = tmp_path / "portfolio.toml"
    path.write_text(block)
    (check,) = check_portfolio_file(path)
    assert check.status is not Level.FAIL, check.to_text()
