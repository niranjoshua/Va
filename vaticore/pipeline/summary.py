"""The supervisors' week: one short message every Monday about their sites.

Daily plans go to the people at the sites. The people who manage them see a
weekly summary instead, readable on a phone in under a minute:

    Vaticore week of 28 Sep to 4 Oct, 3 sites.
    Plans: 21 sent, 15 followed, 4 not followed, 2 unanswered.
    Value: following the plans saved about 85 L (NGN 106,250); another 30 L was on offer on days not followed.
    Why not followed: no diesel 3, generator fault 1.
    Fuel: 1 check at Ikorodu tower, 28 L (NGN 35,000).
    Data: readings missing at Wuse branch on 2 days.

On WhatsApp it uses the "vaticore_weekly_summary" utility template (a
business-initiated message, so it needs Meta's approval like the daily plan;
docs/whatsapp-setup.md). Only recipients with weekly_summary = true in the
recipients file receive it. Each summary is recorded, so a rerun never sends
the same week twice.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from vaticore.delivery.channels import (
    EmailChannel,
    SendResult,
    WhatsAppChannel,
    address_hash,
    mask_address,
)
from vaticore.delivery.recipients import Recipient
from vaticore.delivery.webhook import REASON_LABELS
from vaticore.fuel import Severity, reconcile
from vaticore.pipeline.savings import Period, site_days
from vaticore.pipeline.store import PlanStore
from vaticore.sites.model import Site
from vaticore.storage import TimeSeriesRepository

SUMMARY_TEMPLATE = "vaticore_weekly_summary"
SUMMARY_BODY = (
    "Vaticore week of {{1}}.\n"
    "Plans: {{2}}\n"
    "Value: {{3}}\n"
    "Why not followed: {{4}}\n"
    "Fuel: {{5}}\n"
    "Data: {{6}}\n"
    "Advisory only. Reply STOP to stop these messages."
)
# A day is reported as missing data when its plan could not be made or fewer
# than this many of its hours had readings to score against.
MAX_MISSING_HOURS = 6


@dataclass(frozen=True)
class WeeklySummary:
    week: str
    plans: str
    value: str
    reasons: str
    fuel: str
    data: str

    @property
    def params(self) -> list[str]:
        """The template's six variables, one line each, within WhatsApp's limits."""
        return [
            " ".join(v.split())[:250]
            for v in (self.week, self.plans, self.value, self.reasons, self.fuel, self.data)
        ]

    @property
    def text(self) -> str:
        body = SUMMARY_BODY
        for i, value in enumerate(self.params, start=1):
            body = body.replace("{{" + str(i) + "}}", value)
        return body

    @property
    def email_subject(self) -> str:
        return f"Vaticore week of {self.week}"

    @property
    def email_html(self) -> str:
        rows = "".join(
            f'<tr><td style="padding:4px 12px 4px 0;color:#6B676D;vertical-align:top">'
            f"{label}</td><td>{html.escape(value)}</td></tr>"
            for label, value in (
                ("Plans", self.plans),
                ("Value", self.value),
                ("Why not followed", self.reasons),
                ("Fuel", self.fuel),
                ("Data", self.data),
            )
        )
        return (
            f"<p><strong>Vaticore week of {html.escape(self.week)}</strong></p>"
            f"<table>{rows}</table><p>Advisory only: your teams decide.</p>"
        )


def week_of(day: date) -> Period:
    """The Monday to Sunday week before `day` (the last full week)."""
    monday = day - timedelta(days=day.weekday() + 7)
    return Period(monday, monday + timedelta(days=6))


def weekly_summary(
    sites: Sequence[Site], repo: TimeSeriesRepository, store: PlanStore, week: Period
) -> WeeklySummary:
    """One supervisor's week across their sites."""
    if not sites:
        raise ValueError("a summary needs at least one site")
    names = {s.site_id: s.name for s in sites}
    currency = sites[0].currency
    days = site_days(store, sites, week)
    label = (
        f"{week.start.day} {week.start:%b} to {week.end.day} {week.end:%b}, "
        f"{len(sites)} site{'s' * (len(sites) > 1)}"
    )

    sent = days[days["sent"]] if not days.empty else days
    if sent.empty:
        plans = "none sent this week."
    else:
        counts = sent["response"].value_counts()
        plans = (
            f"{len(sent)} sent, {int(counts.get('followed', 0))} followed, "
            f"{int(counts.get('not_followed', 0))} not followed, "
            f"{int(counts.get('none', 0))} unanswered."
        )

    value = _value(days, currency)

    reasons = (
        sent.loc[sent["response"] == "not_followed", "reason"].fillna("not given")
        if not sent.empty
        else pd.Series(dtype=object)
    )
    why = (
        ", ".join(
            f"{REASON_LABELS.get(str(code), str(code))} {count}"
            for code, count in reasons.value_counts().items()
        )
        + "."
        if not reasons.empty
        else "none."
    )

    fuel = _fuel(sites, repo, store, week, currency)

    gaps = days[
        (days["status"] != "planned")
        | (days["hours_missing"].fillna(0) > MAX_MISSING_HOURS)
    ] if not days.empty else days  # fmt: skip
    if gaps.empty:
        data = "readings complete at every site."
    else:
        per_site = gaps.groupby("site_id").size().sort_values(ascending=False)
        data = (
            "readings missing at "
            + "; ".join(
                f"{names[str(s)]} on {n} day{'s' * (n > 1)}" for s, n in per_site.head(3).items()
            )
            + ("; and others" if len(per_site) > 3 else "")
            + "."
        )
    return WeeklySummary(label, plans, value, why, fuel, data)


def _value(days: pd.DataFrame, currency: str) -> str:
    scored = days[days["scored"]] if not days.empty else days
    if scored.empty:
        return "no finished days to score yet."
    taken = scored[scored["response"] == "followed"]
    missed = scored[scored["response"] == "not_followed"]
    litres = float(taken["litres_saved"].sum())
    money = float(taken["fuel_value_saved"].sum())
    if taken.empty:
        text = "no plan was confirmed as followed, so none of the value was taken"
    elif litres >= 1:
        text = f"following the plans saved about {litres:,.0f} L ({currency} {money:,.0f})"
    elif litres <= -1:
        avoided = float(taken["outage_kwh_avoided"].sum())
        text = f"following the plans used {-litres:,.0f} L more diesel" + (
            f" to avoid {avoided:,.0f} kWh of outages" if avoided >= 1 else ""
        )
    else:
        text = "following the plans made no measurable difference to diesel"
    offer = float(missed["litres_saved"].clip(lower=0).sum())
    if offer >= 1:
        text += f"; another {offer:,.0f} L was on offer on days not followed"
    return text + " (modelled against planning from yesterday)."


def _fuel(
    sites: Sequence[Site],
    repo: TimeSeriesRepository,
    store: PlanStore,
    week: Period,
    currency: str,
) -> str:
    checks = []
    for site in sites:
        if site.generator is None:
            continue
        start, end = week.utc_bounds(site.timezone)
        readings = repo.read_history(site.operator_id, site.site_id, start, end)
        if readings.empty:
            continue
        deliveries = store.deliveries_for(
            site.operator_id, site.site_id, start.to_pydatetime(), end.to_pydatetime()
        )
        report = reconcile(site, readings, deliveries, start=start, end=end)
        flagged = [f for f in report.findings if f.severity is not Severity.INFO and f.litres > 0]
        if flagged:
            litres = sum(f.litres for f in flagged)
            checks.append((site.name, len(flagged), litres, litres * report.price_per_l))
    if not checks:
        return "nothing flagged."
    checks.sort(key=lambda c: -c[2])
    parts = [
        f"{n} check{'s' * (n > 1)} at {name}, {litres:,.0f} L ({currency} {value:,.0f})"
        for name, n, litres, value in checks[:3]
    ]
    return "; ".join(parts) + ("; and others" if len(checks) > 3 else "") + "."


@dataclass(frozen=True)
class SummaryDelivery:
    recipient: str  # masked
    channel: str
    status: str
    error: str | None = None


def send_weekly_summaries(
    sites: Sequence[Site],
    repo: TimeSeriesRepository,
    store: PlanStore,
    recipients: Sequence[Recipient],
    *,
    week: Period,
    whatsapp: WhatsAppChannel | None = None,
    email: EmailChannel | None = None,
    console: bool = False,
    now: datetime,
    force: bool = False,
    template_name: str = SUMMARY_TEMPLATE,
) -> list[SummaryDelivery]:
    """Send each opted-in, consenting supervisor their week, once per channel."""
    out = []
    for person in recipients:
        if not (person.consent and person.weekly_summary):
            continue
        if any(store.is_opted_out(h) for h in person.hashes):
            continue
        theirs = [s for s in sites if person.covers(s.operator_id, s.site_id)]
        if not theirs:
            continue
        summary = weekly_summary(theirs, repo, store, week)
        targets: list[tuple[str, str | None]] = []
        if console:
            targets.append(("console", person.whatsapp or person.email))
        if whatsapp is not None:
            targets.append(("whatsapp", person.whatsapp))
        if email is not None:
            targets.append(("email", person.email))
        for channel, address in targets:
            if address is None:
                continue
            masked = mask_address(address)
            if channel == "console":
                print(f"--- weekly summary to {masked} ---\n{summary.text}\n")
                out.append(SummaryDelivery(masked, channel, "dry_run"))
                continue
            who = address_hash(address)
            if not force and store.summary_sent(person.operator_id, week.start, channel, who):
                out.append(SummaryDelivery(masked, channel, "already_sent"))
                continue
            result = _send(channel, address, summary, whatsapp, email, template_name)
            store.record_summary(
                operator_id=person.operator_id,
                week_start=week.start,
                channel=channel,
                recipient_hash=who,
                recipient_masked=masked,
                status=result.status,
                provider_message_id=result.provider_message_id,
                error=result.error,
                at=now,
            )
            out.append(SummaryDelivery(masked, channel, result.status, result.error))
    return out


def _send(
    channel: str,
    address: str,
    summary: WeeklySummary,
    whatsapp: WhatsAppChannel | None,
    email: EmailChannel | None,
    template_name: str,
) -> SendResult:
    if channel == "whatsapp" and whatsapp is not None:
        return whatsapp.send_template(address, template_name, summary.params)
    if channel == "email" and email is not None:
        return email.send_html(
            address,
            summary.email_subject,
            summary.text + "\n\nTo stop these emails, reply with STOP.",
            summary.email_html,
        )
    return SendResult("failed", None, f"no {channel} channel configured")
