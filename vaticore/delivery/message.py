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
"""

from __future__ import annotations

import re
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


@dataclass(frozen=True)
class PlanMessage:
    site_name: str
    day: str
    generator: str
    grid: str
    battery: str
    note: str

    @property
    def params(self) -> list[str]:
        """The six template variables, each one line, within WhatsApp's limits."""
        return [
            _one_line(v, 200)
            for v in (self.site_name, self.day, self.generator, self.grid, self.battery, self.note)
        ]

    @property
    def text(self) -> str:
        body = TEMPLATE_BODY
        for i, value in enumerate(self.params, start=1):
            body = body.replace("{{" + str(i) + "}}", value)
        return body

    def to_dict(self) -> dict[str, str]:
        return {
            "site_name": self.site_name,
            "day": self.day,
            "generator": self.generator,
            "grid": self.grid,
            "battery": self.battery,
            "note": self.note,
        }


def day_label(start: pd.Timestamp, end: pd.Timestamp, timezone: str) -> str:
    """'Tue 18 Nov, 06:00 to 06:00 Wed' in the site's clock."""
    a, b = start.tz_convert(timezone), end.tz_convert(timezone)
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
) -> PlanMessage:
    """Compose the message for a planned day."""
    spans = run_windows(timestamps, genset_on, timezone)
    if not has_generator:
        generator = "none at this site."
    elif not spans:
        generator = "not needed today."
    else:
        runs = "run" if len(spans) == 1 else "runs"
        generator = (
            f"{join_spans(spans)} ({genset_hours:.0f} h in {len(spans)} {runs}, "
            f"about {_litres(fuel_l)})."
        )
    if unserved_kwh > 0.5:
        generator += (
            f" Even so, about {unserved_kwh:.0f} kWh may go unserved: check fuel and loads."
        )

    if grid_on is None:
        grid = "no grid connection."
    else:
        grid_spans = run_windows(timestamps, grid_on, timezone)
        grid = (
            f"counted on {join_spans(grid_spans)}; run the generator if it fails."
            if grid_spans
            else "not counted on today."
        )

    if soc_end_pct is None:
        battery = "no battery."
    else:
        start_txt = "" if soc_start_pct is None else f"starts about {soc_start_pct:.0f}%"
        if soc_assumed and start_txt:
            start_txt += " (assumed)"
        end_txt = f"ends about {soc_end_pct:.0f}%"
        battery = f"{start_txt}, {end_txt}." if start_txt else f"{end_txt}."

    note = data_note if not track_note else f"{data_note} {track_note}"
    return PlanMessage(
        site_name=site_name,
        day=day_label(start, end, timezone),
        generator=generator,
        grid=grid,
        battery=battery,
        note=note,
    )


def no_plan_message(
    *, site_name: str, timezone: str, start: pd.Timestamp, end: pd.Timestamp, reason: str
) -> PlanMessage:
    """Tell the site there is no plan today, and why, rather than staying silent."""
    return PlanMessage(
        site_name=site_name,
        day=day_label(start, end, timezone),
        generator="no plan today; run the site as usual.",
        grid="no plan today.",
        battery="no plan today.",
        note=reason,
    )


def _litres(value: float) -> str:
    return f"{value:,.0f} L" if value >= 10 else f"{value:.0f} L"


def _one_line(value: str, limit: int) -> str:
    """WhatsApp template variables: no line breaks or tabs, no long space runs."""
    flat = re.sub(r"[\r\n\t]+", " ", value)
    flat = re.sub(r" {2,}", " ", flat).strip()
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."
