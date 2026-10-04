"""The daily pipeline: for every site, check the data, forecast, plan, store, send.

One run per site per plan day. The plan covers the 24 hours from the site's
plan start hour (06:00 local by default) and uses only readings from before
that moment, so a run is reproducible and never sees the future.

For each site:
  1. Read its history and check the data (health.py). If the data is too
     broken to plan on, store that and tell the site why: never a silent gap.
  2. Choose the model by how much history the site has. Research note 3:
     with under 8 weeks, pretrained Chronos-2 is far better calibrated than
     the GBM, which needs months; with more, the calibrated GBM (the published
     planning model) plans and Chronos-2 runs beside it in shadow, so every
     pilot produces a head-to-head comparison on real data.
  3. Fall back rather than fail: primary model calibrated, then uncalibrated,
     then the other model, then persistence. A site always gets a plan if its
     data allows one, and the run records why a fallback happened.
  4. Plan the generator on the high quantile of net load (P90) and count on the
     grid only at its low quantile (P10). Also plan the persistence baseline,
     "tomorrow looks like today", which every plan is later scored against.
  5. Store everything (store.py), compose the message (local clock) and send
     it to the site's consenting recipients, once.

One site failing never stops the others; failures are stored with the error.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd

from vaticore import engine
from vaticore.decisions.dispatch import DispatchPlan, SiteAssets, plan_dispatch
from vaticore.delivery.channels import Channel
from vaticore.delivery.message import PlanMessage, no_plan_message, plan_message
from vaticore.delivery.recipients import Recipient, recipients_for
from vaticore.forecasting.base import quantile_column
from vaticore.pipeline.health import HealthReport, check_health
from vaticore.pipeline.scoring import value_phrase
from vaticore.pipeline.store import SENT_STATES, PlanStore, RunRecord
from vaticore.schemas import GENERATION_KW, GRID_AVAILABLE, LOAD_KW, TIMESTAMP
from vaticore.sites.model import Portfolio, Site
from vaticore.storage import TimeSeriesRepository

log = logging.getLogger("vaticore.pipeline")

GBM = engine.PLANNING_MODEL
CHRONOS = "chronos_2"
PERSISTENCE = "persistence"
QUANTILES = (0.1, 0.5, 0.9)


@dataclass(frozen=True)
class PipelineConfig:
    plan_quantile: float = 0.9
    grid_plan_quantile: float = 0.1
    # Battery charge at the start of the plan when the site reports none.
    assumed_soc_fraction: float = 0.5
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


@dataclass
class SiteRun:
    operator_id: str
    site_id: str
    plan_date: date
    status: str  # planned, no_plan or failed
    model: str | None = None
    fallback: list[str] = field(default_factory=list)
    health: HealthReport | None = None
    message: PlanMessage | None = None
    plan: DispatchPlan | None = None
    error: str | None = None
    deliveries: list[tuple[str, str]] = field(default_factory=list)  # (masked, status)


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


def run_site(
    site: Site,
    repo: TimeSeriesRepository,
    store: PlanStore,
    *,
    plan_date: date,
    config: PipelineConfig | None = None,
    soc_kwh: float | None = None,
    now: datetime | None = None,
) -> SiteRun:
    """Plan one site for one day and store the result. Never raises for data reasons."""
    config = config or PipelineConfig()
    now = now or datetime.now(tz=UTC)
    start, end = plan_window(site, plan_date)
    result = SiteRun(site.operator_id, site.site_id, plan_date, status="failed")
    try:
        history = _prepare_history(site, repo, start, config)
        health = check_health(
            history,
            start,
            has_grid=site.grid is not None,
            grid_reliable=bool(site.grid and site.grid.reliable),
            has_solar=site.solar is not None,
            timezone=site.timezone,
        )
        result.health = health
        if not health.ok_to_plan:
            result.status = "no_plan"
            result.message = no_plan_message(
                site_name=site.name,
                timezone=site.timezone,
                start=start,
                end=end,
                reason=health.summary(),
            )
            store.save_run(_record(site, result, start, end, now))
            return result
        _plan(site, history, health, store, result, start, end, config, soc_kwh, now)
    except Exception as exc:  # stored, reported, and the next site still runs
        log.exception("site %s/%s failed", site.operator_id, site.site_id)
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        store.save_run(_record(site, result, start, end, now, error=traceback.format_exc(limit=3)))
    return result


def run_portfolio(
    portfolio: Portfolio,
    repo: TimeSeriesRepository,
    store: PlanStore,
    *,
    plan_date: date | None = None,
    sites: Sequence[tuple[str, str]] | None = None,
    channel: Channel | None = None,
    recipients: Sequence[Recipient] = (),
    force: bool = False,
    config: PipelineConfig | None = None,
    now: datetime | None = None,
) -> list[SiteRun]:
    """Plan (and optionally deliver) every site, or the listed ones."""
    config = config or PipelineConfig()
    now = now or datetime.now(tz=UTC)
    chosen = [s for s in portfolio.sites if sites is None or s.key in set(sites)]
    runs = []
    for site in chosen:
        day = plan_date or site_today(site, now)
        if not force and channel is not None:
            existing = store.get_run(site.operator_id, site.site_id, day)
            if existing is not None and existing.status == "planned":
                # Re-planning after a plan was sent would contradict what the
                # site already received. Resend the stored plan instead.
                run = _from_record(existing)
                run.deliveries = deliver(
                    store, site, run, channel, list(recipients), force=False, now=now
                )
                runs.append(run)
                continue
        run = run_site(site, repo, store, plan_date=day, config=config, now=now)
        if channel is not None and run.message is not None:
            run.deliveries = deliver(store, site, run, channel, list(recipients), force, now)
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
    channel: Channel,
    recipients: list[Recipient],
    force: bool,
    now: datetime,
) -> list[tuple[str, str]]:
    """Send one site's message to its consenting recipients, at most once each."""
    if run.message is None:
        return []
    outcomes: list[tuple[str, str]] = []
    for person in recipients_for(recipients, site.operator_id, site.site_id):
        if store.is_opted_out(person.hash):
            outcomes.append((person.masked, "opted_out"))
            continue
        previous = store.delivery_status(
            site.operator_id, site.site_id, run.plan_date, channel.name, person.hash
        )
        if previous in SENT_STATES and not force:
            outcomes.append((person.masked, "already_sent"))
            continue
        sent = channel.send(person.whatsapp, run.message)
        store.record_delivery(
            operator_id=site.operator_id,
            site_id=site.site_id,
            plan_date=run.plan_date,
            channel=channel.name,
            recipient_hash=person.hash,
            recipient_masked=person.masked,
            status=sent.status,
            provider_message_id=sent.provider_message_id,
            error=sent.error,
            attempts=sent.attempts,
            at=now,
        )
        outcomes.append((person.masked, sent.status if not sent.error else f"failed: {sent.error}"))
    return outcomes


# -- internals ---------------------------------------------------------------


def _prepare_history(
    site: Site, repo: TimeSeriesRepository, start: pd.Timestamp, config: PipelineConfig
) -> pd.DataFrame:
    """Hourly history strictly before the plan start, with solar handled."""
    raw = repo.read_history(
        site.operator_id,
        site.site_id,
        start - pd.Timedelta(days=config.history_days),
        start - pd.Timedelta(microseconds=1),
    )
    if raw.empty:
        return raw
    frame = raw.set_index(TIMESTAMP).sort_index()
    numeric = [c for c in (LOAD_KW, GENERATION_KW, GRID_AVAILABLE) if c in frame.columns]
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
) -> None:
    assets = site.dispatch_assets()
    # Forecasts start the hour after the history ends.
    last = pd.Timestamp(history[TIMESTAMP].max())
    horizon = int((end - last) / pd.Timedelta(hours=1)) - 1
    window = pd.date_range(start, periods=24, freq="1h", tz="UTC", name=TIMESTAMP)

    short = health.history_days < config.short_history_days
    order = list(config.model_order or ((CHRONOS, GBM) if short else (GBM, CHRONOS)))
    forecast, model = _forecast_with_fallback(history, horizon, order, result.fallback)
    result.model = model
    primary = forecast.reindex(window)
    if primary.isna().any().any():
        raise RuntimeError("forecast does not cover the plan window")

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
        shadow_model = next(m for m in (CHRONOS, GBM) if m != model.split("+")[0])
        try:
            shadow = engine.net_load_forecast(
                history, horizon=horizon, model=shadow_model, quantiles=QUANTILES, calibrate=True
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
    result.message = plan_message(
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
        data_note=health.summary(),
        track_note=_track_note(store, site, start, config),
    )
    result.status = "planned"
    store.save_run(
        _record(site, result, start, end, now, soc=soc, soc_assumed=soc_assumed, assets=assets),
        forecasts=pd.concat(frames, ignore_index=True),
        plan=plan_rows,
    )


def _forecast_with_fallback(
    history: pd.DataFrame, horizon: int, order: list[str], notes: list[str]
) -> tuple[pd.DataFrame, str]:
    attempts = [(m, cal) for m in order if m != PERSISTENCE for cal in (True, False)]
    attempts.append((PERSISTENCE, False))
    for model, calibrate in attempts:
        try:
            forecast = engine.net_load_forecast(
                history, horizon=horizon, model=model, quantiles=QUANTILES, calibrate=calibrate
            )
            return forecast, f"{model}+conformal" if calibrate else model
        except Exception as exc:
            notes.append(
                f"{model}{' calibrated' if calibrate else ''} unavailable "
                f"({type(exc).__name__}: {str(exc)[:120]})"
            )
    raise RuntimeError("no model could forecast this site")


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


def _track_note(
    store: PlanStore, site: Site, start: pd.Timestamp, config: PipelineConfig
) -> str | None:
    """A one-line track record from the last week of scored plans, once there is one."""
    since = (start - pd.Timedelta(days=7)).date()
    scores = store.scores(site.operator_id, site.site_id, since=since)
    scores = scores[scores["hours_scored"].fillna(0) > 0] if not scores.empty else scores
    if len(scores) < config.track_record_min_days:
        return None
    held = scores["coverage_primary"].dropna()
    litres = float((scores["fuel_baseline_l"] - scores["fuel_plan_l"]).sum())
    outages = float((scores["unserved_baseline_kwh"] - scores["unserved_plan_kwh"]).sum())
    parts = [f"Last {len(scores)} days"]
    if not held.empty:
        parts.append(f"the forecast range held {float(held.mean()):.0%} of hours")
    parts.append(f"following the plans would have {value_phrase(litres, outages)}")
    return parts[0] + ": " + "; ".join(parts[1:]) + " (against planning from yesterday)."


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

        message = json.dumps({"text": run.message.text, "fields": run.message.to_dict()})
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
    if record.message:
        fields = json.loads(record.message)["fields"]
        message = PlanMessage(**fields)
    return SiteRun(
        record.operator_id,
        record.site_id,
        record.plan_date,
        record.status,
        model=record.model,
        message=message,
    )
