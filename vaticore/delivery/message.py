"""The morning message: one site's plan, readable on a phone in ten seconds.

Rules the wording follows:
  - Actions first (when to run the generator), then context.
  - Local clock times, never UTC.
  - Plain words, no model names, no jargon; numbers rounded to what a site
    manager uses.
  - Always say the plan is advisory, and say plainly when the data is weak or
    when there is no plan.

Each message exists in two forms. The text form is for the console, logs and
WhatsApp replies inside a conversation. The template form fills the six
variables of the approved WhatsApp template (docs/whatsapp-setup.md), which
cannot contain line breaks.

Languages: every plan is written in each language in LANGUAGES, and each
person receives the one set in their recipient record (`language = "pcm"` for
Nigerian Pidgin). The Pidgin wording was written for review by native
speakers before its template is submitted to Meta (docs/whatsapp-setup.md).
Another language is added with a phrase table here and an approved template;
until its template is approved the person gets English.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd

TEMPLATE_BODY = (
    "Vaticore plan for {{1}}, {{2}}.\n"
    "Generator: {{3}}\n"
    "Grid: {{4}}\n"
    "Battery: {{5}}\n"
    "Note: {{6}}\n"
    "Advisory only: your team decides. Reply 1 if you follow the plan, 2 if not, "
    "or STOP to stop these messages."
)
PIDGIN_TEMPLATE_BODY = (
    "Vaticore plan for {{1}}, {{2}}.\n"
    "Generator: {{3}}\n"
    "Grid: {{4}}\n"
    "Battery: {{5}}\n"
    "Note: {{6}}\n"
    "Na advice be dis, your team go decide. Reply 1 if una follow the plan, 2 if una "
    "no follow am, or STOP make we stop dis messages."
)
TEMPLATE_BODIES = {"en": TEMPLATE_BODY, "pcm": PIDGIN_TEMPLATE_BODY}
LANGUAGES = tuple(TEMPLATE_BODIES)

# Every phrase a plan uses, per language. Keys are shared; English is the
# reference wording (tests pin it).
PHRASES: dict[str, dict[str, str]] = {
    "en": {
        "gen_none": "none at this site.",
        "gen_not_needed": "not needed today.",
        "gen_runs": "{spans} ({hours:.0f} h in {count} {runs}, about {litres}).",
        "run": "run",
        "runs": "runs",
        "gen_unserved": " Even so, about {kwh:.0f} kWh may go unserved: check fuel and loads.",
        "grid_none": "no grid connection.",
        "grid_counted": "counted on {spans}; run the generator if it fails.",
        "grid_not_counted": "not counted on today.",
        "battery_none": "no battery.",
        "battery_start": "starts about {pct:.0f}%",
        "assumed": " (assumed)",
        "battery_end": "ends about {pct:.0f}%",
        "no_plan_gen": "no plan today; run the site as usual.",
        "no_plan": "no plan today.",
        "fault": (
            "Vaticore could not make today's plan because of a fault on our side; "
            "our team has been alerted."
        ),
        "missed": "Vaticore's planning did not run today; our team has been alerted.",
        "data_ok": "Data OK.",
        "track": "Last {days} days: {parts} (against planning from yesterday).",
        "track_held": "the forecast range held {held:.0%} of hours",
        "track_value": "following the plans would have {value}",
        "subject": "Vaticore plan for {site}, {day}",
        "heading": "Vaticore plan for {site}",
        "label_generator": "Generator",
        "label_grid": "Grid",
        "label_battery": "Battery",
        "label_note": "Note",
        "email_footer": (
            "Advisory only: your team decides.\n"
            "Did you follow the plan? Reply 1 if yes, 2 if not. To stop these emails, "
            "reply with STOP.\n"
        ),
        "html_footer": (
            "Advisory only: your team decides. "
            "Reply 1 if you followed the plan, 2 if not, or STOP to stop these emails."
        ),
    },
    "pcm": {
        "gen_none": "no generator for dis site.",
        "gen_not_needed": "no need to on am today.",
        "gen_runs": "on am {spans} ({hours:.0f} hours, {count} {runs}, about {litres}).",
        "run": "time",
        "runs": "times",
        "gen_unserved": (
            " Even so, about {kwh:.0f} kWh fit no reach: check fuel and wetin dey use power."
        ),
        "grid_none": "no grid for dis site.",
        "grid_counted": "we dey expect am {spans}; if e no come, on the generator.",
        "grid_not_counted": "no count on grid today.",
        "battery_none": "no battery.",
        "battery_start": "e go start around {pct:.0f}%",
        "assumed": " (na guess)",
        "battery_end": "end around {pct:.0f}%",
        "no_plan_gen": "no plan today; run the site as normal.",
        "no_plan": "no plan today.",
        "fault": (
            "Vaticore no fit make today plan because of wahala for our side; our team dey on am."
        ),
        "missed": "Vaticore planning no run today; our team dey on am.",
        "data_ok": "Data dey OK.",
        "track": "For last {days} days: {parts} (compared to planning from yesterday).",
        "track_held": "the forecast range catch {held:.0%} of the hours",
        "track_value": "if una follow the plans, una for {value}",
        "subject": "Vaticore plan for {site}, {day}",
        "heading": "Vaticore plan for {site}",
        "label_generator": "Generator",
        "label_grid": "Grid",
        "label_battery": "Battery",
        "label_note": "Note",
        "email_footer": (
            "Na advice be dis: your team go decide.\n"
            "Una follow the plan? Reply 1 if yes, 2 if no. To stop dis emails, reply STOP.\n"
        ),
        "html_footer": (
            "Na advice be dis: your team go decide. "
            "Reply 1 if una follow the plan, 2 if una no follow am, or STOP to stop dis emails."
        ),
    },
}

# Data health issues in other languages, by issue code (English uses the
# health check's own sentences, which carry the exact numbers).
HEALTH_NOTES: dict[str, dict[str, str]] = {
    "pcm": {
        "no_data": "No load readings don land yet.",
        "stale": "Readings from the site don old; abeg check the data link.",
        "short_history": "Readings never reach two days; plans go start when e reach.",
        "missing": "Plenty readings miss for the last week.",
        "stuck_meter": "The load meter fit don hang.",
        "negative_load": "Some load readings no correct.",
        "no_solar_record": "No solar readings: the plan count solar as zero.",
        "no_grid_record": "No grid on/off record: the plan no count on grid.",
    },
}


def phrase(language: str, key: str, **values: object) -> str:
    table = PHRASES.get(language, PHRASES["en"])
    return table[key].format(**values)


@dataclass(frozen=True)
class TrackRecord:
    """A site's last week of scored plans, for the message's note."""

    days: int
    range_held: float | None
    litres_saved: float
    outage_kwh_avoided: float


def track_text(track: TrackRecord, language: str = "en") -> str:
    parts = []
    if track.range_held is not None:
        parts.append(phrase(language, "track_held", held=track.range_held))
    value = value_words(track.litres_saved, track.outage_kwh_avoided, language)
    parts.append(phrase(language, "track_value", value=value))
    return phrase(language, "track", days=track.days, parts="; ".join(parts))


def value_words(litres_saved: float, outage_kwh_avoided: float, language: str = "en") -> str:
    """Litres and outages against the baseline, stated honestly in either direction."""
    litres = abs(litres_saved)
    outages = abs(outage_kwh_avoided)
    pcm = language == "pcm"
    if litres_saved >= 1 and outage_kwh_avoided >= -0.5:
        if pcm:
            extra = f" and stop {outages:,.0f} kWh of outage" if outage_kwh_avoided >= 1 else ""
            return f"save {litres:,.0f} L diesel{extra}"
        extra = f" and avoided {outages:,.0f} kWh of outages" if outage_kwh_avoided >= 1 else ""
        return f"saved {litres:,.0f} L of diesel{extra}"
    if litres_saved <= -1 and outage_kwh_avoided >= 1:
        if pcm:
            return f"use {litres:,.0f} L more diesel to stop {outages:,.0f} kWh of outage"
        return f"used {litres:,.0f} L more diesel to avoid {outages:,.0f} kWh of outages"
    if litres_saved <= -1 and outage_kwh_avoided <= -1:
        if pcm:
            return f"use {litres:,.0f} L more diesel and get {outages:,.0f} kWh more outage"
        return f"used {litres:,.0f} L more diesel and had {outages:,.0f} kWh more outages"
    if litres_saved <= -1:
        if pcm:
            return f"use {litres:,.0f} L more diesel and outage no reduce"
        return f"used {litres:,.0f} L more diesel with no fewer outages"
    if outage_kwh_avoided >= 1:
        if pcm:
            return f"stop {outages:,.0f} kWh of outage with the same diesel"
        return f"avoided {outages:,.0f} kWh of outages for the same diesel"
    if outage_kwh_avoided <= -1:
        return (
            f"get {outages:,.0f} kWh more outage" if pcm else f"had {outages:,.0f} kWh more outages"
        )
    return "make no difference wey we fit measure" if pcm else "made no measurable difference"


def data_note(language: str, summary: str, issue_codes: Sequence[str]) -> str:
    """The health check's note: its own sentences in English, by issue elsewhere."""
    if language == "en" or language not in HEALTH_NOTES:
        return summary
    if not issue_codes:
        return phrase(language, "data_ok")
    notes = HEALTH_NOTES[language]
    seen: list[str] = []
    for code in issue_codes:
        text = notes.get(code)
        if text and text not in seen:
            seen.append(text)
    return " ".join(seen) or summary


@dataclass(frozen=True)
class PlanMessage:
    site_name: str
    day: str
    generator: str
    grid: str
    battery: str
    note: str
    language: str = "en"

    @property
    def params(self) -> list[str]:
        """The six template variables, each one line, within WhatsApp's limits."""
        return [
            _one_line(v, 200)
            for v in (self.site_name, self.day, self.generator, self.grid, self.battery, self.note)
        ]

    @property
    def text(self) -> str:
        body = TEMPLATE_BODIES.get(self.language, TEMPLATE_BODY)
        for i, value in enumerate(self.params, start=1):
            body = body.replace("{{" + str(i) + "}}", value)
        return body

    @property
    def email_subject(self) -> str:
        return _one_line(phrase(self.language, "subject", site=self.site_name, day=self.day), 150)

    @property
    def email_text(self) -> str:
        p = PHRASES.get(self.language, PHRASES["en"])
        return (
            f"{self.email_subject}.\n\n"
            f"{p['label_generator']}: {self.generator}\n"
            f"{p['label_grid']}: {self.grid}\n"
            f"{p['label_battery']}: {self.battery}\n"
            f"{p['label_note']}: {self.note}\n\n" + p["email_footer"]
        )

    @property
    def email_html(self) -> str:
        p = PHRASES.get(self.language, PHRASES["en"])
        rows = "".join(
            f'<tr><td style="padding:4px 12px 4px 0;color:#6B676D">{label}</td>'
            f'<td style="padding:4px 0">{html.escape(value)}</td></tr>'
            for label, value in (
                (p["label_generator"], self.generator),
                (p["label_grid"], self.grid),
                (p["label_battery"], self.battery),
                (p["label_note"], self.note),
            )
        )
        return (
            '<div style="font-family:Arial,sans-serif;font-size:15px;color:#1D1A1E">'
            f"<p><b>{html.escape(phrase(self.language, 'heading', site=self.site_name))}</b><br>"
            f"{html.escape(self.day)}</p>"
            f'<table style="border-collapse:collapse">{rows}</table>'
            f'<p style="color:#6B676D;font-size:13px">{html.escape(p["html_footer"])}</p>'
            "</div>"
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "site_name": self.site_name,
            "day": self.day,
            "generator": self.generator,
            "grid": self.grid,
            "battery": self.battery,
            "note": self.note,
            "language": self.language,
        }


def day_label(start: pd.Timestamp, end: pd.Timestamp, timezone: str) -> str:
    """'Tue 18 Nov' for a whole local day, else 'Tue 18 Nov, 06:00 to 06:00 Wed'."""
    a, b = start.tz_convert(timezone), end.tz_convert(timezone)
    if (a.hour, a.minute, b.hour, b.minute) == (0, 0, 0, 0) and b.date() == (
        a + pd.Timedelta(days=1)
    ).date():
        return f"{a:%a} {a.day} {a:%b}"
    end_txt = f"{b:%H:%M}" if a.date() == b.date() else f"{b:%H:%M} {b:%a}"
    return f"{a:%a} {a.day} {a:%b}, {a:%H:%M} to {end_txt}"


def run_windows(timestamps: pd.DatetimeIndex, on: list[bool], timezone: str) -> list[str]:
    """Contiguous generator runs as local clock spans: '18:00 to 20:00'."""
    spans: list[str] = []
    start: pd.Timestamp | None = None
    step = pd.Timedelta(hours=1)
    stamps = [*timestamps, timestamps[-1] + step]
    flags = [*on, False]
    for ts, flag in zip(stamps, flags, strict=True):
        if flag and start is None:
            start = ts
        elif not flag and start is not None:
            a, b = start.tz_convert(timezone), ts.tz_convert(timezone)
            end_txt = "midnight" if (b.hour, b.minute) == (0, 0) else f"{b:%H:%M}"
            spans.append(f"{a:%H:%M} to {end_txt}")
            start = None
    return spans


MAX_SPANS = 5


def join_spans(spans: list[str]) -> str:
    """'a, b and c'; beyond five spans, the first four and how many more."""
    if len(spans) <= 1:
        return "".join(spans)
    if len(spans) > MAX_SPANS:
        shown = spans[: MAX_SPANS - 1]
        return ", ".join(shown) + f" and {len(spans) - len(shown)} more"
    return ", ".join(spans[:-1]) + " and " + spans[-1]


def plan_message(
    *,
    site_name: str,
    timezone: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    timestamps: pd.DatetimeIndex,
    genset_on: list[bool],
    grid_on: list[bool] | None,
    has_generator: bool,
    fuel_l: float,
    genset_hours: float,
    unserved_kwh: float,
    soc_start_pct: float | None,
    soc_end_pct: float | None,
    soc_assumed: bool,
    data_note: str,
    track_note: str | None,
    language: str = "en",
) -> PlanMessage:
    """Compose the message for a planned day.

    data_note and track_note are already in `language` (see data_note() and
    track_text()).
    """
    p = PHRASES.get(language, PHRASES["en"])
    spans = run_windows(timestamps, genset_on, timezone)
    if not has_generator:
        generator = p["gen_none"]
    elif not spans:
        generator = p["gen_not_needed"]
    else:
        generator = p["gen_runs"].format(
            spans=join_spans(spans),
            hours=genset_hours,
            count=len(spans),
            runs=p["run"] if len(spans) == 1 else p["runs"],
            litres=_litres(fuel_l),
        )
    if unserved_kwh > 0.5:
        generator += p["gen_unserved"].format(kwh=unserved_kwh)

    if grid_on is None:
        grid = p["grid_none"]
    else:
        grid_spans = run_windows(timestamps, grid_on, timezone)
        grid = (
            p["grid_counted"].format(spans=join_spans(grid_spans))
            if grid_spans
            else p["grid_not_counted"]
        )

    if soc_end_pct is None:
        battery = p["battery_none"]
    else:
        start_txt = "" if soc_start_pct is None else p["battery_start"].format(pct=soc_start_pct)
        if soc_assumed and start_txt:
            start_txt += p["assumed"]
        end_txt = p["battery_end"].format(pct=soc_end_pct)
        battery = f"{start_txt}, {end_txt}." if start_txt else f"{end_txt}."

    note = data_note if not track_note else f"{data_note} {track_note}"
    return PlanMessage(
        site_name=site_name,
        day=day_label(start, end, timezone),
        generator=generator,
        grid=grid,
        battery=battery,
        note=note,
        language=language,
    )


def no_plan_message(
    *,
    site_name: str,
    timezone: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    reason: str,
    language: str = "en",
) -> PlanMessage:
    """Tell the site there is no plan today, and why, rather than staying silent."""
    return PlanMessage(
        site_name=site_name,
        day=day_label(start, end, timezone),
        generator=phrase(language, "no_plan_gen"),
        grid=phrase(language, "no_plan"),
        battery=phrase(language, "no_plan"),
        note=reason,
        language=language,
    )


def _litres(value: float) -> str:
    return f"{value:,.0f} L" if value >= 10 else f"{value:.0f} L"


def _one_line(value: str, limit: int) -> str:
    """WhatsApp template variables: no line breaks or tabs, no long space runs."""
    flat = re.sub(r"[\r\n\t]+", " ", value)
    flat = re.sub(r" {2,}", " ", flat).strip()
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."
