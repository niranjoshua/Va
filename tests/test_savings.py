"""The pilot report, the supervisors' week, and asking why a plan was not followed.

The store is filled directly with known plan days, deliveries, replies and
scores, and the sites' readings with a known generator schedule, so every
number in the report has a known answer.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from vaticore.delivery.channels import SendResult
from vaticore.delivery.recipients import Recipient, recipient_hash
from vaticore.delivery.webhook import (
    REASON_QUESTION,
    REASON_THANKS,
    REASON_WINDOW,
    classify_reason,
    handle_webhook,
)
from vaticore.pipeline.savings import Period, savings_report, site_days
from vaticore.pipeline.store import PlanStore, RunRecord, ScoreRecord
from vaticore.pipeline.summary import send_weekly_summaries, week_of, weekly_summary
from vaticore.schemas import GENSET_KW, GRID_AVAILABLE, LOAD_KW, OPERATOR_ID, SITE_ID, TIMESTAMP
from vaticore.sites import Site
from vaticore.storage import DuckDBRepository

OP = "towerco"
BASELINE = Period(date(2026, 9, 1), date(2026, 9, 7))
PILOT = Period(date(2026, 9, 8), date(2026, 9, 14))
PHONE = "+2348000000001"
TZ = "Africa/Lagos"


def _site(site_id: str, name: str, *, grid: bool = True) -> Site:
    return Site(
        operator_id=OP, site_id=site_id, name=name, site_type="telecom_tower",
        latitude=6.6, longitude=3.5, timezone=TZ, currency="NGN",
        value_of_lost_load_per_kwh=5000.0,
        battery={"usable_kwh": 20.0, "power_kw": 10.0},
        generator={"rated_kw": 20.0, "fuel_price_per_l": 1000.0},
        **({"grid": {"capacity_kw": 15.0, "price_per_kwh": 200.0}} if grid else {}),
    )  # fmt: skip


PILOT_SITE = _site("ikd-01", "Ikorodu tower")
CONTROL_SITE = _site("ikj-02", "Ikeja tower")


def _readings(
    site: Site, hours_before: int, hours_during: int, *, grid_before: int = 16,
    grid_during: int = 16, load_kw: float = 5.0,
) -> pd.DataFrame:  # fmt: skip
    """Generator at 12 kW from 18:00 local for N hours a day; grid on for M hours from 06:00."""
    start, _ = BASELINE.utc_bounds(TZ)
    _, end = PILOT.utc_bounds(TZ)
    stamps = pd.date_range(start, end, freq="1h", inclusive="left")
    local = stamps.tz_convert(TZ)
    during = local.date >= PILOT.start
    run_for = np.where(during, hours_during, hours_before)
    grid_for = np.where(during, grid_during, grid_before)
    return pd.DataFrame(
        {
            OPERATOR_ID: site.operator_id,
            SITE_ID: site.site_id,
            TIMESTAMP: stamps,
            LOAD_KW: load_kw,
            "generation_kw": 0.0,
            GENSET_KW: np.where((local.hour >= 18) & (local.hour < 18 + run_for), 12.0, 0.0),
            GRID_AVAILABLE: ((local.hour >= 6) & (local.hour < 6 + grid_for)).astype(float),
        }
    )


def _plan_day(
    store: PlanStore, site: Site, day: date, *, sent: bool, saved_l: float = 0.0,
    unserved_avoided: float = 0.0, hours_missing: int = 0, status: str = "planned",
) -> None:  # fmt: skip
    start = pd.Timestamp(day).tz_localize(TZ).tz_convert("UTC")
    store.save_run(
        RunRecord(
            operator_id=site.operator_id, site_id=site.site_id, plan_date=day,
            plan_start=start.to_pydatetime(), plan_end=(start + pd.Timedelta(days=1)).to_pydatetime(),
            issued_at=(start - pd.Timedelta(hours=6)).to_pydatetime(), status=status,
        )
    )  # fmt: skip
    if status == "planned":
        store.save_score(
            ScoreRecord(
                operator_id=site.operator_id, site_id=site.site_id, plan_date=day,
                scored_at=datetime(2026, 9, 20, tzinfo=UTC), hours_scored=24 - hours_missing,
                hours_missing=hours_missing, pinball_primary=1.0, coverage_primary=0.8,
                fuel_plan_l=20.0 - saved_l, fuel_baseline_l=20.0, fuel_perfect_l=12.0,
                unserved_plan_kwh=5.0 - unserved_avoided, unserved_baseline_kwh=5.0,
                unserved_perfect_kwh=0.0, cost_plan=1000.0 * (20.0 - saved_l),
                cost_baseline=20_000.0, cost_perfect=12_000.0, detail={},
            )
        )  # fmt: skip
    if sent:
        store.record_delivery(
            operator_id=site.operator_id, site_id=site.site_id, plan_date=day,
            channel="whatsapp", recipient_hash=recipient_hash(PHONE),
            recipient_masked="+234******0001", status="read",
            provider_message_id=f"wamid.{site.site_id}.{day}", error=None, attempts=1,
            at=(start - pd.Timedelta(hours=6)).to_pydatetime(),
        )  # fmt: skip


def _reply(store: PlanStore, text: str, at: datetime, sender: Any = None) -> Any:
    message = {
        "id": f"in.{at.timestamp()}.{text}", "from": PHONE.lstrip("+"), "type": "text",
        "text": {"body": text}, "timestamp": str(int(at.timestamp())),
    }  # fmt: skip
    payload = {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}
    return handle_webhook(store, payload, send_text=sender)


@pytest.fixture
def pilot() -> tuple[DuckDBRepository, PlanStore]:
    """A week of shadow plans, then a week of plans sent to the pilot site."""
    repo = DuckDBRepository(":memory:")
    repo.upsert(_readings(PILOT_SITE, hours_before=6, hours_during=4))  # 6 h a day, then 4
    repo.upsert(_readings(CONTROL_SITE, hours_before=6, hours_during=5))  # 6 h, then 5
    store = PlanStore("duckdb:///:memory:")
    for i in range(BASELINE.days):
        day = BASELINE.start + timedelta(days=i)
        _plan_day(store, PILOT_SITE, day, sent=False, saved_l=1.0)
    replies = ["1", "1", "2", "1", None, "2", "1"]  # followed 4, not 2, silent 1
    reasons = {2: "B", 5: "the generator was faulty"}
    for i, answer in enumerate(replies):
        day = PILOT.start + timedelta(days=i)
        _plan_day(store, PILOT_SITE, day, sent=True, saved_l=2.0 + i, unserved_avoided=1.0)
        if answer is not None:
            evening = pd.Timestamp(day).tz_localize(TZ).tz_convert("UTC") + pd.Timedelta(hours=20)
            _reply(store, answer, evening.to_pydatetime())
            if i in reasons:
                _reply(store, reasons[i], (evening + pd.Timedelta(minutes=5)).to_pydatetime())
    return repo, store


def test_periods_parse_and_refuse_nonsense() -> None:
    assert Period.parse("2026-09-01:2026-09-30").days == 30
    with pytest.raises(ValueError, match="FIRST:LAST"):
        Period.parse("2026-09-01")
    with pytest.raises(ValueError, match="before it starts"):
        Period(date(2026, 9, 2), date(2026, 9, 1))
    assert week_of(date(2026, 10, 5)) == Period(date(2026, 9, 28), date(2026, 10, 4))  # a Monday
    assert week_of(date(2026, 10, 8)) == Period(date(2026, 9, 28), date(2026, 10, 4))


def test_site_days_join_plans_deliveries_replies_and_scores(
    pilot: tuple[DuckDBRepository, PlanStore],
) -> None:
    _, store = pilot
    days = site_days(store, [PILOT_SITE], PILOT)
    assert len(days) == 7 and days["sent"].all() and days["scored"].all()
    assert days["response"].tolist() == [
        "followed", "followed", "not_followed", "followed", "none", "not_followed", "followed",
    ]  # fmt: skip
    assert days["reason"].tolist()[2] == "no_diesel"
    assert days["reason"].tolist()[5] == "generator_fault"
    assert days["litres_saved"].tolist() == [2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    assert days["fuel_value_saved"].tolist()[0] == 2000.0


def test_the_report_credits_only_the_difference_from_control_sites(
    pilot: tuple[DuckDBRepository, PlanStore],
) -> None:
    repo, store = pilot
    report = savings_report(
        [PILOT_SITE, CONTROL_SITE], repo, store, baseline=BASELINE, pilot=PILOT,
        control=[CONTROL_SITE.site_id],
    )  # fmt: skip
    assert report.method == "difference_in_differences"
    # Pilot sites: 6 h to 4 h (-33.3%); control: 6 h to 5 h (-16.7%); credited: -16.7%.
    pilot_group, control_group = report.pilot_group, report.control_group
    assert pilot_group is not None and control_group is not None
    assert pilot_group.change == pytest.approx(-1 / 3)
    assert control_group.change == pytest.approx(-1 / 6)
    assert report.measured_change == pytest.approx(-1 / 6)
    per_hour = 0.08145 * 20 + 0.246 * 12  # the generator's fuel curve at 12 kW
    assert pilot_group.baseline_l_per_day == pytest.approx(6 * per_hour)
    assert report.measured_litres_saved_per_day == pytest.approx(per_hour)
    assert report.measured_value_saved == pytest.approx(per_hour * 1000 * 7)

    adoption = report.adoption()
    assert adoption == {
        "plan_days": 7, "sent": 7, "followed": 4, "not_followed": 2, "no_reply": 1,
        "reasons": {"no_diesel": 1, "generator_fault": 1},
    }  # fmt: skip
    modelled = report.modelled()
    assert modelled["followed"]["litres_saved"] == 2 + 3 + 5 + 8
    assert modelled["not_followed"]["litres_saved"] == 4 + 7
    assert modelled["no_reply"]["litres_saved"] == 6
    assert modelled["all"]["outage_kwh_avoided"] == 7
    assert report.modelled(report.shadow_days)["all"]["litres_saved"] == 7

    text = report.to_markdown()
    assert "**17% less diesel**" in text and "difference in differences" in text
    assert "Followed on **4**, not followed on **2**" in text
    assert "- no diesel: 1" in text and "- generator fault: 1" in text
    assert "| Plan followed | 4 | 18 | 18,000 |" in text
    assert "—" not in text  # house style: no em dashes
    assert report.to_dict()["measured"]["change"] == pytest.approx(-1 / 6)
    assert not report.warnings


def test_before_and_after_warns_when_the_grid_moved(
    pilot: tuple[DuckDBRepository, PlanStore],
) -> None:
    _, store = pilot
    repo = DuckDBRepository(":memory:")
    repo.upsert(_readings(PILOT_SITE, 6, 4, grid_before=12, grid_during=18))
    report = savings_report([PILOT_SITE], repo, store, baseline=BASELINE, pilot=PILOT)
    assert report.method == "before_after"
    assert any("Grid supply changed by +6.0 hours" in w for w in report.warnings)
    assert "Read with care" in report.to_markdown()


def test_the_report_refuses_what_it_cannot_measure_fairly(
    pilot: tuple[DuckDBRepository, PlanStore],
) -> None:
    repo, store = pilot
    with pytest.raises(ValueError, match="must end before"):
        savings_report([PILOT_SITE], repo, store, baseline=PILOT, pilot=PILOT)
    with pytest.raises(ValueError, match="not in the portfolio"):
        savings_report([PILOT_SITE], repo, store, baseline=BASELINE, pilot=PILOT, control=["x"])
    # A control site that was sent plans is not a control.
    _plan_day(store, CONTROL_SITE, PILOT.start, sent=True)
    report = savings_report(
        [PILOT_SITE, CONTROL_SITE], repo, store, baseline=BASELINE, pilot=PILOT,
        control=[CONTROL_SITE.site_id],
    )  # fmt: skip
    assert any("not a clean comparison" in w for w in report.warnings)
    # No readings at all: says so instead of inventing a number.
    empty = savings_report(
        [PILOT_SITE], DuckDBRepository(":memory:"), store, baseline=BASELINE, pilot=PILOT
    )
    assert empty.measured_litres_saved_per_day is None
    assert "Not enough generator readings" in empty.to_markdown()


# -- asking why ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("A", "generator_fault"),
        ("b.", "no_diesel"),
        ("C) grid was on", "grid_on"),
        ("NEPA brought light", "grid_on"),
        ("no fuel at site", "no_diesel"),
        ("inverter trip", "battery_problem"),
        ("oga said run am", "instructed"),
        ("we were busy", "other"),
    ],
)
def test_reasons_are_read_from_letters_and_words(text: str, code: str) -> None:
    assert classify_reason(text) == code


def test_a_two_is_followed_by_one_question_and_the_answer_is_kept() -> None:
    store = PlanStore("duckdb:///:memory:")
    _plan_day(store, PILOT_SITE, PILOT.start, sent=True)
    sent: list[tuple[str, str]] = []

    def sender(to: str, text: str) -> None:
        sent.append((to, text))

    evening = datetime(2026, 9, 8, 19, tzinfo=UTC)
    outcome = _reply(store, "2", evening, sender)
    assert outcome.questions == 1 and sent == [(PHONE, REASON_QUESTION)]
    outcome = _reply(store, "B", evening + timedelta(minutes=3), sender)
    assert outcome.reasons == 1 and sent[-1] == (PHONE, REASON_THANKS)
    rows = store.feedback(OP)
    assert rows["kind"].tolist() == ["not_followed", "reason"]
    assert rows["reason"].tolist()[-1] == "no_diesel"
    assert str(rows["plan_date"].tolist()[-1])[:10] == "2026-09-08"

    # Another message after the answer is ordinary feedback, not a second reason.
    _reply(store, "thanks", evening + timedelta(minutes=10), sender)
    assert store.feedback(OP)["kind"].tolist()[-1] == "other"
    # A reply a day later is not read as the reason either.
    _reply(store, "2", evening + timedelta(days=1), sender)
    _reply(store, "A", evening + timedelta(days=1) + REASON_WINDOW + timedelta(hours=1), sender)
    assert store.feedback(OP)["kind"].tolist()[-1] == "other"


def test_a_failing_question_never_loses_the_reply() -> None:
    store = PlanStore("duckdb:///:memory:")
    _plan_day(store, PILOT_SITE, PILOT.start, sent=True)

    def broken(to: str, text: str) -> None:
        raise RuntimeError("Meta is down")

    outcome = _reply(store, "2", datetime(2026, 9, 8, 19, tzinfo=UTC), broken)
    assert outcome.replies == 1 and outcome.questions == 0
    assert store.feedback(OP)["kind"].tolist() == ["not_followed"]


# -- the supervisors' week -------------------------------------------------------


def test_the_weekly_summary_reads_in_six_lines(
    pilot: tuple[DuckDBRepository, PlanStore],
) -> None:
    repo, store = pilot
    summary = weekly_summary([PILOT_SITE], repo, store, PILOT)
    assert summary.week == "8 Sep to 14 Sep, 1 site"
    assert summary.plans == "7 sent, 4 followed, 2 not followed, 1 unanswered."
    assert summary.value.startswith("following the plans saved about 18 L (NGN 18,000)")
    assert "another 11 L was on offer" in summary.value
    assert summary.reasons == "no diesel 1, generator fault 1."
    assert summary.fuel == "nothing flagged."
    assert summary.data == "readings complete at every site."
    assert len(summary.params) == 6 and all("\n" not in p for p in summary.params)
    assert summary.text.startswith("Vaticore week of 8 Sep to 14 Sep, 1 site.\nPlans: 7 sent")


class _FakeWhatsApp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list[str]]] = []

    def send_template(self, to: str, name: str, params: list[str]) -> SendResult:
        self.sent.append((to, name, params))
        return SendResult("sent", f"wamid.{len(self.sent)}")


def test_summaries_go_once_to_supervisors_who_asked(
    pilot: tuple[DuckDBRepository, PlanStore],
) -> None:
    repo, store = pilot
    people = [
        Recipient(name="Supervisor", whatsapp=PHONE, operator_id=OP, sites=("*",),
                  consent=True, weekly_summary=True),
        Recipient(name="Site manager", whatsapp="+2348000000009", operator_id=OP,
                  sites=(PILOT_SITE.site_id,), consent=True),  # daily plans only
        Recipient(name="Not asked", whatsapp="+2348000000008", operator_id=OP, sites=("*",),
                  consent=False, weekly_summary=True),
    ]  # fmt: skip
    whatsapp = _FakeWhatsApp()
    now = datetime(2026, 9, 15, 6, tzinfo=UTC)
    first = send_weekly_summaries(
        [PILOT_SITE, CONTROL_SITE], repo, store, people, week=PILOT,
        whatsapp=whatsapp, now=now,  # type: ignore[arg-type]
    )  # fmt: skip
    assert [(d.channel, d.status) for d in first] == [("whatsapp", "sent")]
    assert whatsapp.sent[0][1] == "vaticore_weekly_summary"
    assert whatsapp.sent[0][2][0] == "8 Sep to 14 Sep, 2 sites"
    again = send_weekly_summaries(
        [PILOT_SITE, CONTROL_SITE], repo, store, people, week=PILOT,
        whatsapp=whatsapp, now=now,  # type: ignore[arg-type]
    )  # fmt: skip
    assert [d.status for d in again] == ["already_sent"] and len(whatsapp.sent) == 1

    store.set_opt_out(recipient_hash(PHONE), True, "whatsapp reply", now)
    stopped = send_weekly_summaries(
        [PILOT_SITE], repo, store, people, week=PILOT, whatsapp=whatsapp, now=now,  # type: ignore[arg-type]
        force=True,
    )  # fmt: skip
    assert stopped == []
