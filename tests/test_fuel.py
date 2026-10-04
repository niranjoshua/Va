"""Fuel reconciliation on small sites with known answers."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vaticore.fuel import Severity, reconcile
from vaticore.schemas import FUEL_LEVEL_L, GENSET_KW, TIMESTAMP
from vaticore.sites import Site

START = pd.Timestamp("2026-03-01 23:00", tz="UTC")  # midnight in Lagos
END = START + pd.Timedelta(days=3)
HOURS = pd.date_range(START, END, freq="h", inclusive="left")
# 20 kW generator at 10 kW: 0.08145 * 20 + 0.246 * 10 = 4.089 L per hour.
BURN_PER_HOUR = 0.08145 * 20 + 0.246 * 10


def _site(tank_l: float | None = 500.0) -> Site:
    return Site(
        operator_id="op", site_id="s1", name="Site One", site_type="telecom_tower",
        latitude=6.5, longitude=3.4, timezone="Africa/Lagos", currency="NGN",
        value_of_lost_load_per_kwh=5000.0,
        battery={"usable_kwh": 20.0, "power_kw": 10.0},
        generator={"rated_kw": 20.0, "fuel_price_per_l": 1000.0, "tank_l": tank_l},
    )  # fmt: skip


def _readings(
    *,
    refill_at: pd.Timestamp | None = None,
    refill_l: float = 0.0,
    siphon_at: pd.Timestamp | None = None,
    siphon_l: float = 0.0,
    tank: bool = True,
) -> pd.DataFrame:
    """The generator runs 10 kW from 07:00 to 11:00 local each day; the tank follows."""
    local = HOURS.tz_convert("Africa/Lagos")
    output = np.where((local.hour >= 7) & (local.hour < 11), 10.0, 0.0)
    level = []
    current = 300.0
    for ts, kw in zip(HOURS, output, strict=True):
        if refill_at is not None and ts == refill_at:
            current += refill_l
        if siphon_at is not None and ts in (siphon_at, siphon_at + pd.Timedelta(hours=1)):
            current -= siphon_l / 2
        level.append(current)
        if kw > 0:
            current -= BURN_PER_HOUR
    frame = pd.DataFrame({TIMESTAMP: HOURS, GENSET_KW: output})
    if tank:
        frame[FUEL_LEVEL_L] = level
    return frame


def _deliveries(*rows: tuple[pd.Timestamp, float, str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["delivered_at", "litres", "reference"])


def test_a_site_where_everything_agrees_flags_nothing() -> None:
    at = START + pd.Timedelta(hours=36)
    report = reconcile(
        _site(), _readings(refill_at=at, refill_l=200.0), _deliveries((at, 200.0, "INV-1")),
        start=START, end=END,
    )  # fmt: skip
    assert report.findings == () and report.status is Severity.INFO
    assert report.burned_l == pytest.approx(12 * BURN_PER_HOUR)
    assert report.withdrawn_l == pytest.approx(12 * BURN_PER_HOUR)
    assert report.delivered_l == 200.0 and report.genset_coverage == 1.0
    assert list(report.daily["burned_l"].round(3)) == [round(4 * BURN_PER_HOUR, 3)] * 3
    assert "Nothing to flag" in report.to_text("Africa/Lagos")


def test_a_short_delivery_is_flagged_with_its_cost() -> None:
    at = START + pd.Timedelta(hours=36)
    report = reconcile(
        _site(), _readings(refill_at=at, refill_l=150.0), _deliveries((at, 200.0, "INV-2")),
        start=START, end=END,
    )  # fmt: skip
    (finding,) = report.findings
    assert finding.code == "short_delivery" and finding.severity is Severity.ALERT
    assert finding.litres == pytest.approx(50.0)
    assert "INV-2" in finding.message and "NGN 50,000" in finding.message
    assert report.flagged_l == pytest.approx(50.0)


def test_fuel_leaving_the_tank_with_the_generator_off_is_flagged() -> None:
    night = START + pd.Timedelta(hours=46)  # 22:00 local on the second day
    report = reconcile(
        _site(), _readings(siphon_at=night, siphon_l=40.0), _deliveries(), start=START, end=END
    )
    codes = {f.code: f for f in report.findings}
    assert codes["drop_while_off"].severity is Severity.ALERT
    assert codes["drop_while_off"].litres == pytest.approx(40.0)
    # The same litres show up in that day's balance as unexplained.
    assert codes["unexplained_loss"].litres == pytest.approx(40.0)
    assert report.status is Severity.ALERT


def test_deliveries_must_show_in_the_tank_and_refills_must_be_recorded() -> None:
    at = START + pd.Timedelta(hours=36)
    report = reconcile(
        _site(),
        _readings(refill_at=at, refill_l=200.0),
        _deliveries((START + pd.Timedelta(hours=60), 300.0, "INV-3")),  # no rise near it
        start=START,
        end=END,
    )
    codes = {f.code for f in report.findings}
    assert codes == {"delivery_not_seen", "unrecorded_refill"}
    unrecorded = next(f for f in report.findings if f.code == "unrecorded_refill")
    assert unrecorded.severity is Severity.INFO and unrecorded.litres == 0.0


def test_without_a_tank_sensor_deliveries_are_checked_against_use() -> None:
    readings = _readings(tank=False)
    plenty = _deliveries((START + pd.Timedelta(hours=10), 1000.0, "INV-4"))
    report = reconcile(_site(tank_l=500.0), readings, plenty, start=START, end=END)
    (finding,) = report.findings
    assert finding.code == "deliveries_exceed_use"
    expected = 1000.0 - 12 * BURN_PER_HOUR * 1.15 - 500.0
    assert finding.litres == pytest.approx(expected)
    assert report.withdrawn_l is None and "no tank level sensor" in report.to_text()

    modest = _deliveries((START + pd.Timedelta(hours=10), 300.0, "INV-5"))
    assert reconcile(_site(), readings, modest, start=START, end=END).findings == ()
    # Without the tank's size the bound cannot be drawn: nothing is claimed.
    assert reconcile(_site(tank_l=None), readings, plenty, start=START, end=END).findings == ()


def test_the_report_serialises_and_needs_a_generator() -> None:
    report = reconcile(_site(), _readings(), _deliveries(), start=START, end=END)
    data = report.to_dict()
    assert data["status"] == "info" and len(data["daily"]) == 3  # type: ignore[arg-type]
    no_generator = _site().model_copy(update={"generator": None})
    with pytest.raises(ValueError, match="no generator"):
        reconcile(no_generator, _readings(), _deliveries(), start=START, end=END)


def test_sensor_noise_does_not_raise_flags() -> None:
    readings = _readings()
    rng = np.random.default_rng(1)
    readings[FUEL_LEVEL_L] = readings[FUEL_LEVEL_L] + rng.normal(0.0, 2.0, len(readings))
    report = reconcile(_site(), readings, _deliveries(), start=START, end=END)
    assert report.findings == ()
    assert report.withdrawn_l == pytest.approx(12 * BURN_PER_HOUR, abs=8.0)
