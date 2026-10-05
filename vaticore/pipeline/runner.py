"""The daily pipeline: for every site, check the data, forecast, plan, store, send.

One run per site per plan day. The plan covers the site's local day,
midnight to midnight on its own clock by default (a site can set another
plan start hour), and is made the evening before from the readings available
at that moment. A run therefore never sees the future: history stops at the
issue time, the hours between issue and midnight are forecast like the rest,
and the battery's charge is carried forward to midnight on that forecast.

For each site:
  1. Read its history and check the data (health.py). If the data is too
     broken to plan on, store that and tell the site why: never a silent gap.
  2. Choose the model by how much history the site has. Research note 3:
     with under 8 weeks, pretrained Chronos-2 is far better calibrated than
     the GBM, which needs months; with more, the calibrated GBM (the published
     planning model) plans and Chronos-2 runs beside it in shadow, so every
     pilot produces a head-to-head comparison on real data.
  3. Check each model's recent record (monitoring.py). A model whose range
     has drifted, or which has fallen behind persistence, is suspended and
     the plan falls back down the chain; a challenger (the weather model,
     where the site has weather) that has clearly beaten the default model
     over two weeks goes first. Every change is stored and alerted.
  4. Fall back rather than fail: primary model calibrated, then uncalibrated,
     then the next model, then persistence. A site always gets a plan if its
     data allows one, and the run records why a fallback happened. The other
     models run in shadow and are scored, so the evidence keeps coming in.
  5. Plan the generator on the high quantile of net load (P90) and count on the
     grid only at its low quantile (P10). Also plan the persistence baseline,
     "tomorrow looks like today", which every plan is later scored against.
  6. Store everything (store.py), compose the message (local clock) and send
     it to the site's consenting recipients, once.

One site failing never stops the others; failures are stored with the error.
"""

from __future__ import annotations

import functools
import logging
import traceback
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd

from vaticore import engine
from vaticore.decisions.dispatch import DispatchPlan, SiteAssets, plan_dispatch
from vaticore.delivery.channels import Channel, address_hash, mask_address
from vaticore.delivery.message import (
    LANGUAGES,
    PlanMessage,
    TrackRecord,
    data_note,
    no_plan_message,
    phrase,
    plan_message,
    track_text,
)
from vaticore.delivery.recipients import Recipient, recipients_for
from vaticore.forecasting.base import quantile_column
from vaticore.pipeline import monitoring
from vaticore.pipeline.health import HealthReport, check_health
from vaticore.pipeline.monitoring import MonitorConfig
from vaticore.pipeline.store import SENT_STATES, PlanStore, RunRecord
from vaticore.pipeline.weather import LiveWeather, site_weather
from vaticore.schemas import (
    BATTERY_SOC_PCT,
    GENERATION_KW,
    GRID_AVAILABLE,
    LOAD_KW,
    OPTIONAL_COLUMNS,
    TIMESTAMP,
)
from vaticore.sites.model import Portfolio, Site
from vaticore.storage import TimeSeriesRepository

log = logging.getLogger("vaticore.pipeline")

GBM = engine.PLANNING_MODEL
CHRONOS = "chronos_2"
WEATHER = engine.WEATHER_MODEL
PERSISTENCE = "persistence"
QUANTILES = (0.1, 0.5, 0.9)


@dataclass(frozen=True)
class PipelineConfig:
    plan_quantile: float = 0.9
    grid_plan_quantile: float = 0.1
    # Battery charge at the start of the plan when the site reports none, and
    # how old a reported charge may be before it is no longer trusted.
    assumed_soc_fraction: float = 0.5
    soc_max_age_hours: float = 3.0
    # History read per site; Chronos-2 uses up to 48 weeks.
    history_days: int = 340
    # Below this much history, plan on Chronos-2 rather than the GBM.
    short_history_days: float = 56.0
    shadow: bool = True
    # Pin the models to try, in order (persistence is always the last resort).
    # None chooses by history length as described above.
    model_order: tuple[str, ...] | None = None
    # Days of scored plans needed before the message quotes a track record.
    track_record_min_days: int = 3
    # Suspend drifting models and promote proven challengers; None turns it off.
    monitor: MonitorConfig | None = field(default_factory=MonitorConfig)


@dataclass
class SiteRun:
    operator_id: str
    site_id: str
    plan_date: date
    status: str  # planned, no_plan, failed, or missed (the daily job never ran)
    model: str | None = None
    fallback: list[str] = field(default_factory=list)
    health: HealthReport | None = None
    message: PlanMessage | None = None  # English, the reference
    # The same message in every other language in LANGUAGES, by code.
    translations: dict[str, PlanMessage] = field(default_factory=dict)
    plan: DispatchPlan | None = None
    error: str | None = None
    deliveries: list[tuple[str, str]] = field(default_factory=list)  # (masked, status)
    alerts: list[str] = field(default_factory=list)  # model status changes, for ops


def plan_window(site: Site, plan_date: date) -> tuple[pd.Timestamp, pd.Timestamp]:
    """UTC start and end of a site's plan day, from its local plan start hour."""
    local = pd.Timestamp(plan_date).tz_localize(site.timezone) + pd.Timedelta(
        hours=site.plan_start_hour
    )
    start = local.tz_convert("UTC")
    return start, start + pd.Timedelta(hours=24)


def site_today(site: Site, now: datetime) -> date:
    """The site's current local date."""
    return pd.Timestamp(now).tz_convert(site.timezone).date()


def next_plan_date(site: Site, now: datetime) -> date:
    """The first plan day that has not started yet at `now`.

    Run in the evening, this is tomorrow for a midnight-to-midnight site; run
    at 05:40 for a site whose plans start at 06:00, it is today.
    """
    today = site_today(site, now)
    start, _ = plan_window(site, today)
    return today if start > pd.Timestamp(now) else today + timedelta(days=1)


def run_site(
    site: Site,
    repo: TimeSeriesRepository,
    store: PlanStore,
    *,
    plan_date: date,
    config: PipelineConfig | None = None,
    soc_kwh: float | None = None,
    now: datetime | None = None,
    weather: LiveWeather | None = None,
) -> SiteRun:
    """Plan one site for one day and store the result. Never raises for data reasons.

    With a weather provider, the site's weather is fetched and cached and the
    weather model runs (in shadow until monitoring promotes it).
    """
    config = config or PipelineConfig()
    now = now or datetime.now(tz=UTC)
    start, end = plan_window(site, plan_date)
    # Readings are used up to the moment the plan is issued, and never past
    # the plan start: replaying an old day sees only what was known then.
    issued = min(start, pd.Timestamp(now).tz_convert("UTC"))
    result = SiteRun(site.operator_id, site.site_id, plan_date, status="failed")
    try:
        history = _prepare_history(site, repo, issued, config)
        health = check_health(
            history,
            issued,
            has_grid=site.grid is not None,
            grid_reliable=bool(site.grid and site.grid.reliable),
            has_solar=site.solar is not None,
            timezone=site.timezone,
        )
        result.health = health
        if not health.ok_to_plan:
            result.status = "no_plan"
            codes = [issue.code for issue in health.issues]
            _set_messages(
                result,
                lambda lang: no_plan_message(
                    site_name=site.name,
                    timezone=site.timezone,
                    start=start,
                    end=end,
                    reason=data_note(lang, health.summary(), codes),
                    language=lang,
                ),
            )
            store.save_run(_record(site, result, start, end, now))
            return result
        _plan(
            site, history, health, store, result, start, end, config, soc_kwh, now, weather, issued
        )
    except Exception as exc:  # stored, reported, and the next site still runs
        log.exception("site %s/%s failed", site.operator_id, site.site_id)
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        # Never leave the site guessing: say there is no plan, and why.
        result.plan = None
        _set_messages(result, lambda lang: fault_message(site, start, end, "fault", lang))
        store.save_run(_record(site, result, start, end, now, error=traceback.format_exc(limit=3)))
    return result


def fault_message(
    site: Site, start: pd.Timestamp, end: pd.Timestamp, reason: str, language: str = "en"
) -> PlanMessage:
    """'No plan today, run as usual', for a fault on our side ("fault") or a missed run."""
    return no_plan_message(
        site_name=site.name,
        timezone=site.timezone,
        start=start,
        end=end,
        reason=phrase(language, reason),
        language=language,
    )


def _set_messages(result: SiteRun, build: Callable[[str], PlanMessage]) -> None:
    result.message = build("en")
    result.translations = {lang: build(lang) for lang in LANGUAGES if lang != "en"}


def run_portfolio(
    portfolio: Portfolio,
    repo: TimeSeriesRepository,
    store: PlanStore,
    *,
    plan_date: date | None = None,
    sites: Sequence[tuple[str, str]] | None = None,
    channels: Sequence[Channel] = (),
    recipients: Sequence[Recipient] = (),
    force: bool = False,
    config: PipelineConfig | None = None,
    now: datetime | None = None,
    weather: LiveWeather | None = None,
) -> list[SiteRun]:
    """Plan (and optionally deliver) every site, or the listed ones."""
    config = config or PipelineConfig()
    now = now or datetime.now(tz=UTC)
    chosen = [s for s in portfolio.sites if sites is None or s.key in set(sites)]
    runs = []
    for site in chosen:
        day = plan_date or next_plan_date(site, now)
        if not force and channels:
            existing = store.get_run(site.operator_id, site.site_id, day)
            if existing is not None and existing.status in ("planned", "missed"):
                # Re-planning after a plan (or "no plan today") was sent would
                # contradict what the site already received. Resend instead.
                run = _from_record(existing)
                run.deliveries = deliver(
                    store, site, run, channels, list(recipients), force=False, now=now
                )
                runs.append(run)
                continue
        run = run_site(site, repo, store, plan_date=day, config=config, now=now, weather=weather)
        for alert in run.alerts:
            log.warning("model monitoring: %s", alert)
        if channels and run.message is not None:
            run.deliveries = deliver(store, site, run, channels, list(recipients), force, now)
        runs.append(run)
        log.info(
            "%s/%s %s: %s via %s%s",
            site.operator_id,
            site.site_id,
            day,
            run.status,
            run.model,
            f" (fallback: {'; '.join(run.fallback)})" if run.fallback else "",
        )
    return runs


def deliver(
    store: PlanStore,
    site: Site,
    run: SiteRun,
    channels: Sequence[Channel],
    recipients: list[Recipient],
    force: bool,
    now: datetime,
) -> list[tuple[str, str]]:
    """Send one site's message to its consenting recipients, once per channel.

    A person who opted out on any of their addresses (STOP on WhatsApp, an
    unsubscribe by email) gets nothing on any channel.
    """
    if run.message is None:
        return []
    outcomes: list[tuple[str, str]] = []
    for person in recipients_for(recipients, site.operator_id, site.site_id):
        if any(store.is_opted_out(h) for h in person.hashes):
            outcomes.append((person.masked, "opted_out"))
            continue
        for channel in channels:
            to = channel.address(person)
            if to is None:
                continue
            message = message_for(run, person.language, channel)
            who, masked = address_hash(to), mask_address(to)
            previous = store.delivery_status(
                site.operator_id, site.site_id, run.plan_date, channel.name, who
            )
            if previous in SENT_STATES and not force:
                outcomes.append((masked, f"{channel.name} already_sent"))
                continue
            sent = channel.send(to, message)
            store.record_delivery(
                operator_id=site.operator_id,
                site_id=site.site_id,
                plan_date=run.plan_date,
                channel=channel.name,
                recipient_hash=who,
                recipient_masked=masked,
                status=sent.status,
                provider_message_id=sent.provider_message_id,
                error=sent.error,
                attempts=sent.attempts,
                at=now,
                language=message.language,
            )
            status = sent.status if not sent.error else f"failed: {sent.error}"
            outcomes.append((masked, f"{channel.name} {status}"))
    return outcomes


def message_for(run: SiteRun, language: str, channel: Channel) -> PlanMessage:
    """The run's message in the person's language, if this channel can send it.

    WhatsApp can send a language only once its template is approved
    (channel.languages); email and the console send any. Otherwise English.
    """
    assert run.message is not None
    translated = run.translations.get(language)
    if translated is None:
        return run.message
    allowed = getattr(channel, "languages", None)
    if allowed is not None and language not in allowed:
        return run.message
    return translated


def send_missing(
    portfolio: Portfolio,
    store: PlanStore,
    *,
    channels: Sequence[Channel],
    recipients: Sequence[Recipient],
    now: datetime | None = None,
) -> list[SiteRun]:
    """The evening safety net, run after the daily job: nobody is left without a message.

    For each site's next plan day: if the daily job stored a message, it is
    sent to anyone who has not received it yet (a failed send is retried). If
    the job never ran for the site, "no plan today, run as usual" is stored
    as a missed run and sent. Returns the runs that needed action.
    """
    now = now or datetime.now(tz=UTC)
    acted = []
    for site in portfolio.sites:
        day = next_plan_date(site, now)
        record = store.get_run(site.operator_id, site.site_id, day)
        newly_missed = False
        if record is not None and record.message:
            run = _from_record(record)
        else:
            newly_missed = True
            start, end = plan_window(site, day)
            run = SiteRun(site.operator_id, site.site_id, day, status="missed")
            _set_messages(run, functools.partial(fault_message, site, start, end, "missed"))
            run.error = "the daily planning job did not run for this site"
            store.save_run(_record(site, run, start, end, now))
        run.deliveries = deliver(store, site, run, channels, list(recipients), False, now)
        if newly_missed or any(
            not status.endswith("already_sent") and status != "opted_out"
            for _, status in run.deliveries
        ):
            acted.append(run)
    return acted


# -- internals ---------------------------------------------------------------


def _prepare_history(
    site: Site, repo: TimeSeriesRepository, cutoff: pd.Timestamp, config: PipelineConfig
) -> pd.DataFrame:
    """Hourly history strictly before the cutoff (the issue time), with solar handled."""
    raw = repo.read_history(
        site.operator_id,
        site.site_id,
        cutoff - pd.Timedelta(days=config.history_days),
        cutoff - pd.Timedelta(microseconds=1),
    )
    if raw.empty:
        return raw
    frame = raw.set_index(TIMESTAMP).sort_index()
    numeric = [c for c in (LOAD_KW, GENERATION_KW, *OPTIONAL_COLUMNS) if c in frame.columns]
    # Resolution is explicit: plans are hourly, so finer data is averaged into
    # hours (a grid hour counts as on only if it was on throughout).
    hourly = frame[numeric].resample("1h").mean()
    if GRID_AVAILABLE in hourly:
        hourly[GRID_AVAILABLE] = np.floor(hourly[GRID_AVAILABLE] + 1e-9)
    if site.solar is None:
        # No solar at the site: generation is zero, not unknown.
        hourly[GENERATION_KW] = hourly[GENERATION_KW].fillna(0.0)
    elif hourly[GENERATION_KW].isna().all():
        hourly[GENERATION_KW] = 0.0  # reported in the health check
    hourly["operator_id"] = site.operator_id
    hourly["site_id"] = site.site_id
    return hourly.reset_index()


def _plan(
    site: Site,
    history: pd.DataFrame,
    health: HealthReport,
    store: PlanStore,
    result: SiteRun,
    start: pd.Timestamp,
    end: pd.Timestamp,
    config: PipelineConfig,
    soc_kwh: float | None,
    now: datetime,
    provider: LiveWeather | None = None,
    issued: pd.Timestamp | None = None,
) -> None:
    assets = site.dispatch_assets()
    issued = start if issued is None else issued
    # Forecasts start the hour after the history ends.
    last = pd.Timestamp(history[TIMESTAMP].max())
    horizon = int((end - last) / pd.Timedelta(hours=1)) - 1
    window = pd.date_range(start, periods=24, freq="1h", tz="UTC", name=TIMESTAMP)

    weather = None
    if provider is not None:
        weather = site_weather(
            site,
            store,
            provider,
            start - pd.Timedelta(days=config.history_days),
            end,
            now,
            result.fallback,
        )

    short = health.history_days < config.short_history_days
    default = [CHRONOS, GBM] if short else [GBM, CHRONOS]
    challengers = [WEATHER] if weather is not None else []
    if config.model_order is not None:
        order = list(config.model_order)
    elif config.monitor is not None:
        checked, changes = monitoring.check_site(
            store, site.operator_id, site.site_id, result.plan_date, now, config.monitor
        )
        result.alerts = [c.describe() for c in changes]
        order, notes = monitoring.choose_order(default, challengers, checked, config.monitor)
        result.fallback.extend(notes)
    else:
        order = default + challengers
    forecast, model = _forecast_with_fallback(history, horizon, order, result.fallback, weather)
    result.model = model
    primary = forecast.reindex(window)
    if primary.isna().any().any():
        raise RuntimeError("forecast does not cover the plan window")

    if soc_kwh is None:
        soc_kwh = measured_soc_kwh(
            history, assets, issued, pd.Timedelta(hours=config.soc_max_age_hours)
        )
        if soc_kwh is not None:
            # Carry the measured charge forward to the plan start through the
            # hours between the last reading and midnight, on the median forecast.
            soc_kwh = _carry_forward(site, history, forecast, soc_kwh, start, assets, config)
    soc_assumed = soc_kwh is None
    soc = (
        assets.min_soc_kwh + config.assumed_soc_fraction * (assets.battery_kwh - assets.min_soc_kwh)
        if soc_kwh is None
        else float(np.clip(soc_kwh, assets.min_soc_kwh, assets.battery_kwh))
    )
    grid = _grid_plan(site, history, window, assets, config)
    plan = plan_dispatch(
        primary[quantile_column(config.plan_quantile)],
        soc_kwh=soc,
        assets=assets,
        planned_grid_available=grid,
        display_timezone=site.timezone,
    )
    result.plan = plan

    # The baseline every plan is scored against: persistence, planned on its median.
    baseline_fc = engine.net_load_forecast(
        history, horizon=horizon, model=PERSISTENCE, quantiles=QUANTILES, calibrate=False
    ).reindex(window)
    baseline = plan_dispatch(
        baseline_fc[quantile_column(0.5)].fillna(0.0),
        soc_kwh=soc,
        assets=assets,
        planned_grid_available=grid,
    )

    frames = [
        _forecast_rows(primary, model, "primary"),
        _forecast_rows(baseline_fc, PERSISTENCE, "baseline"),
    ]
    if config.shadow and config.model_order is None:
        # Every other model runs beside the plan and is scored, suspended ones
        # included: that is how a suspended model earns its way back.
        for shadow_model in [m for m in (*default, *challengers) if m != model.split("+")[0]]:
            try:
                shadow = engine.net_load_forecast(
                    history,
                    horizon=horizon,
                    model=shadow_model,
                    quantiles=QUANTILES,
                    calibrate=True,
                    weather=weather,
                ).reindex(window)
                frames.append(_forecast_rows(shadow, f"{shadow_model}+conformal", "shadow"))
            except Exception as exc:  # shadow models never block a plan
                result.fallback.append(f"shadow {shadow_model} skipped ({type(exc).__name__})")

    plan_rows = pd.DataFrame(
        {
            "timestamp": window,
            "planned_net_load_kw": plan.planned_net_load_kw,
            "genset_on": plan.genset_on,
            "grid_on": plan.planned_grid_on,
            "expected_soc_kwh": plan.expected.soc_kwh,
            "baseline_genset_on": baseline.genset_on,
        }
    )

    cap = assets.battery_kwh
    track = _track_record(store, site, start, config)
    codes = [issue.code for issue in health.issues]
    _set_messages(
        result,
        lambda lang: plan_message(
            site_name=site.name,
            timezone=site.timezone,
            start=start,
            end=end,
            timestamps=window,
            genset_on=[bool(x) for x in plan.genset_on],
            grid_on=None if site.grid is None else [bool(x) for x in plan.planned_grid_on],
            has_generator=site.generator is not None,
            fuel_l=plan.expected.fuel_l,
            genset_hours=plan.expected.genset_hours,
            unserved_kwh=plan.expected.unserved_kwh,
            soc_start_pct=100 * soc / cap if cap > 0 else None,
            soc_end_pct=100 * plan.expected.soc_end_kwh / cap if cap > 0 else None,
            soc_assumed=soc_assumed,
            data_note=data_note(lang, health.summary(), codes),
            track_note=None if track is None else track_text(track, lang),
            language=lang,
        ),
    )
    result.status = "planned"
    if weather is not None:
        plan_weather = weather[(weather[TIMESTAMP] >= start) & (weather[TIMESTAMP] < end)]
        if not plan_weather.empty:
            store.save_issued_weather(
                site.operator_id, site.site_id, result.plan_date, plan_weather, now
            )
    store.save_run(
        _record(site, result, start, end, now, soc=soc, soc_assumed=soc_assumed, assets=assets),
        forecasts=pd.concat(frames, ignore_index=True),
        plan=plan_rows,
    )


def measured_soc_kwh(
    history: pd.DataFrame, assets: SiteAssets, start: pd.Timestamp, max_age: pd.Timedelta
) -> float | None:
    """The battery's charge at the plan start, from its latest recent reading.

    Monitoring systems report state of charge as a percentage of the battery's
    capacity; the plan works in usable kWh, so the percentage is applied to the
    usable capacity and held between the floor and full. None when the site
    reports no charge, or only an old one: the plan then assumes a charge and
    says so.
    """
    if assets.battery_kwh <= 0 or BATTERY_SOC_PCT not in history:
        return None
    readings = history.dropna(subset=[BATTERY_SOC_PCT])
    if readings.empty:
        return None
    last = readings.iloc[-1]
    if start - pd.Timestamp(last[TIMESTAMP]) > max_age:
        return None
    kwh = float(last[BATTERY_SOC_PCT]) / 100.0 * assets.battery_kwh
    return float(np.clip(kwh, assets.min_soc_kwh, assets.battery_kwh))


def _forecast_with_fallback(
    history: pd.DataFrame,
    horizon: int,
    order: list[str],
    notes: list[str],
    weather: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, str]:
    attempts = [(m, cal) for m in order if m != PERSISTENCE for cal in (True, False)]
    attempts.append((PERSISTENCE, False))
    for model, calibrate in attempts:
        try:
            forecast = engine.net_load_forecast(
                history,
                horizon=horizon,
                model=model,
                quantiles=QUANTILES,
                calibrate=calibrate,
                weather=weather,
            )
            return forecast, f"{model}+conformal" if calibrate else model
        except Exception as exc:
            notes.append(
                f"{model}{' calibrated' if calibrate else ''} unavailable "
                f"({type(exc).__name__}: {str(exc)[:120]})"
            )
    raise RuntimeError("no model could forecast this site")


def _carry_forward(
    site: Site,
    history: pd.DataFrame,
    forecast: pd.DataFrame,
    soc_kwh: float,
    start: pd.Timestamp,
    assets: SiteAssets,
    config: PipelineConfig,
) -> float:
    """Expected battery charge at the plan start, from a charge measured before it.

    Applies the same planning rules to the bridge hours (from the hour after
    the last reading up to the plan start) on the median net load forecast.
    With no bridge hours the measured charge is returned as is.
    """
    bridge = forecast[forecast.index < start]
    if bridge.empty:
        return soc_kwh
    index = pd.DatetimeIndex(bridge.index)
    plan = plan_dispatch(
        bridge[quantile_column(0.5)],
        soc_kwh=soc_kwh,
        assets=assets,
        planned_grid_available=_grid_plan(site, history, index, assets, config),
    )
    return float(plan.expected.soc_end_kwh)


def _grid_plan(
    site: Site,
    history: pd.DataFrame,
    window: pd.DatetimeIndex,
    assets: SiteAssets,
    config: PipelineConfig,
) -> np.ndarray | None:
    if site.grid is None:
        return None
    try:
        return engine.grid_plan(
            history,
            window,
            assets=assets,
            quantile=config.grid_plan_quantile,
            assume_grid_always_on=site.grid.reliable,
        )
    except ValueError:
        # No grid record for an unreliable grid: plan as if it were off
        # (cautious), as the health check tells the site.
        return np.zeros(len(window))


def _forecast_rows(forecast: pd.DataFrame, model: str, role: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "model": model,
            "role": role,
            "timestamp": forecast.index,
            "q10": forecast[quantile_column(0.1)].to_numpy(),
            "q50": forecast[quantile_column(0.5)].to_numpy(),
            "q90": forecast[quantile_column(0.9)].to_numpy(),
        }
    )


def _track_record(
    store: PlanStore, site: Site, start: pd.Timestamp, config: PipelineConfig
) -> TrackRecord | None:
    """The last week of scored plans, once there are enough to quote."""
    since = (start - pd.Timedelta(days=7)).date()
    scores = store.scores(site.operator_id, site.site_id, since=since)
    scores = scores[scores["hours_scored"].fillna(0) > 0] if not scores.empty else scores
    if len(scores) < config.track_record_min_days:
        return None
    held = scores["coverage_primary"].dropna()
    return TrackRecord(
        days=len(scores),
        range_held=None if held.empty else float(held.mean()),
        litres_saved=float((scores["fuel_baseline_l"] - scores["fuel_plan_l"]).sum()),
        outage_kwh_avoided=float(
            (scores["unserved_baseline_kwh"] - scores["unserved_plan_kwh"]).sum()
        ),
    )


def _record(
    site: Site,
    run: SiteRun,
    start: pd.Timestamp,
    end: pd.Timestamp,
    now: datetime,
    *,
    soc: float | None = None,
    soc_assumed: bool | None = None,
    assets: SiteAssets | None = None,
    error: str | None = None,
) -> RunRecord:
    plan = run.plan
    message = None
    if run.message is not None:
        import json

        message = json.dumps(
            {
                "text": run.message.text,
                "fields": run.message.to_dict(),
                "translations": {k: m.to_dict() for k, m in run.translations.items()},
            }
        )
    return RunRecord(
        operator_id=site.operator_id,
        site_id=site.site_id,
        plan_date=run.plan_date,
        plan_start=start.to_pydatetime(),
        plan_end=end.to_pydatetime(),
        issued_at=pd.Timestamp(now).to_pydatetime(),
        status=run.status,
        model=run.model,
        fallback="; ".join(run.fallback) or None,
        health=None if run.health is None else run.health.to_dict(),
        summary=None if plan is None else plan.summary(),
        message=message,
        soc_start_kwh=soc,
        soc_assumed=soc_assumed,
        expected_fuel_l=None if plan is None else plan.expected.fuel_l,
        expected_genset_hours=None if plan is None else plan.expected.genset_hours,
        expected_genset_starts=None if plan is None else plan.expected.genset_starts,
        expected_unserved_kwh=None if plan is None else plan.expected.unserved_kwh,
        assets=None if assets is None else asdict(assets),
        error=error or run.error,
    )


def _from_record(record: RunRecord) -> SiteRun:
    """Rebuild a stored run's message, to resend exactly what was planned."""
    import json

    message = None
    translations: dict[str, PlanMessage] = {}
    if record.message:
        stored = json.loads(record.message)
        message = PlanMessage(**stored["fields"])
        translations = {k: PlanMessage(**v) for k, v in stored.get("translations", {}).items()}
    return SiteRun(
        record.operator_id,
        record.site_id,
        record.plan_date,
        record.status,
        model=record.model,
        message=message,
        translations=translations,
    )
