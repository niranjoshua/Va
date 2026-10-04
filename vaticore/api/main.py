"""FastAPI application: the programmatic surface of the product.

Endpoints are thin. All forecasting, backtesting and advisory logic lives in the
engine facade, so the API and the dashboard always agree. The data source is
injected (get_fleet), backed by synthetic demo data for now and swapped for a
real repository against operator feeds without touching the handlers.

Run with:
    uv sync --extra service
    uv run uvicorn vaticore.api.main:app --reload
"""

from __future__ import annotations

import hmac
import json
from datetime import date
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import pandas as pd

from vaticore import __version__, engine
from vaticore.api.schemas import (
    AdvisoryRequest,
    AdvisoryResponse,
    ForecastPoint,
    ForecastRequest,
    ForecastResponse,
    IngestBatch,
    PlanHour,
    PlanRequest,
    PlanResponse,
    RunWindow,
    SiteInfo,
)
from vaticore.config import Settings, get_settings
from vaticore.datasets import make_synthetic_fleet
from vaticore.decisions.dispatch import SiteAssets
from vaticore.delivery.webhook import handle_webhook, verify_signature
from vaticore.forecasting.base import ForecasterError, quantile_column
from vaticore.pipeline.health import site_health_report
from vaticore.pipeline.scoring import scorecard
from vaticore.pipeline.store import PlanStore
from vaticore.schemas import GENERATION_KW, LOAD_KW, OPERATOR_ID, SITE_ID, TIMESTAMP
from vaticore.sites.model import Portfolio, Site, load_portfolio
from vaticore.storage import TimeSeriesRepository

try:  # FastAPI resolves this annotation at runtime; it needs the service extra.
    from fastapi import Request
except ImportError:  # pragma: no cover - service extra not installed
    Request = Any  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from fastapi import FastAPI


_VALID_TARGETS = {LOAD_KW, GENERATION_KW}


@lru_cache(maxsize=1)
def get_repository() -> TimeSeriesRepository:
    """Cached repository, seeded with demo data if the store is empty.

    Seeding means a fresh deployment is never blank. Replace the seed with a real
    ingestion feed and this same store serves live operator data.
    """
    from vaticore.storage import get_repository as build_repository

    repo = build_repository()
    # Demo data only outside production: a live database must never be seeded.
    if repo.count() == 0 and get_settings().environment != "production":
        repo.upsert(make_synthetic_fleet(days=90, seed=1))
    return repo


def get_fleet() -> pd.DataFrame:
    """Data source dependency: read the full fleet from the repository."""
    return get_repository().read_fleet()


def get_app_settings() -> Settings:
    """Settings dependency, overridable in tests."""
    return get_settings()


def _registered_site(operator_id: str, site_id: str) -> Site | None:
    """The site's record from the configured portfolio, if there is one."""
    path = get_settings().portfolio_file
    if path is None:
        return None
    portfolio = _portfolio(str(path))
    try:
        return portfolio.get(operator_id, site_id)
    except KeyError:
        return None


@lru_cache(maxsize=4)
def _portfolio(path: str) -> Portfolio:
    return load_portfolio(path)


@lru_cache(maxsize=1)
def get_plan_store() -> PlanStore:
    """The pipeline's plan store (plans, scores, deliveries, replies)."""
    settings = get_settings()
    return PlanStore(settings.plan_store_url or settings.database_url)


def create_app() -> FastAPI:
    """Application factory."""
    from fastapi import Depends, FastAPI, Header, HTTPException, Query
    from fastapi.responses import PlainTextResponse

    app = FastAPI(
        title="Vaticore",
        version=__version__,
        summary="Probabilistic energy forecasting for distributed energy operators.",
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/models")
    def models() -> list[str]:
        return engine.available_models()

    @app.get("/sites", response_model=list[SiteInfo])
    def sites(fleet: pd.DataFrame = Depends(get_fleet)) -> list[SiteInfo]:
        out: list[SiteInfo] = []
        for (operator_id, site_id), group in fleet.groupby([OPERATOR_ID, SITE_ID], sort=True):
            out.append(
                SiteInfo(
                    operator_id=str(operator_id),
                    site_id=str(site_id),
                    n_observations=len(group),
                    first_timestamp=group[TIMESTAMP].min(),
                    last_timestamp=group[TIMESTAMP].max(),
                )
            )
        return out

    def _site_history(fleet: pd.DataFrame, operator_id: str, site_id: str) -> pd.DataFrame:
        try:
            return engine.select_site(fleet, operator_id, site_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/forecast", response_model=ForecastResponse)
    def forecast(
        request: ForecastRequest,
        fleet: pd.DataFrame = Depends(get_fleet),
    ) -> ForecastResponse:
        if request.target not in _VALID_TARGETS:
            raise HTTPException(status_code=400, detail=f"target must be one of {_VALID_TARGETS}")
        history = _site_history(fleet, request.operator_id, request.site_id)
        try:
            frame = engine.forecast_site(
                history,
                request.target,
                horizon=request.horizon,
                model=request.model,
                quantiles=tuple(request.quantiles),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        index = pd.DatetimeIndex(frame.index)
        points = [
            ForecastPoint(
                timestamp=ts.to_pydatetime(),
                quantiles={
                    quantile_column(q): float(frame.iloc[i][quantile_column(q)])
                    for q in request.quantiles
                },
            )
            for i, ts in enumerate(index)
        ]
        return ForecastResponse(
            operator_id=request.operator_id,
            site_id=request.site_id,
            target=request.target,
            model=request.model,
            points=points,
        )

    @app.post("/advisory", response_model=AdvisoryResponse)
    def advisory(
        request: AdvisoryRequest,
        fleet: pd.DataFrame = Depends(get_fleet),
    ) -> AdvisoryResponse:
        history = _site_history(fleet, request.operator_id, request.site_id)
        try:
            result = engine.advisory_for_site(
                history,
                horizon=request.horizon,
                usable_battery_kwh=request.usable_battery_kwh,
                step_hours=request.step_hours,
                model=request.model,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return AdvisoryResponse(
            operator_id=request.operator_id,
            site_id=request.site_id,
            horizon_hours=result.horizon_hours,
            expected_net_load_kwh=result.expected_net_load_kwh,
            conservative_net_load_kwh=result.conservative_net_load_kwh,
            recommended_reserve_kwh=result.recommended_reserve_kwh,
            genset_recommended=result.genset_recommended,
            note=result.note,
        )

    @app.post("/plan", response_model=PlanResponse)
    def plan(
        request: PlanRequest,
        fleet: pd.DataFrame = Depends(get_fleet),
    ) -> PlanResponse:
        """Hour by hour generator schedule. Advisory only: a person approves it."""
        history = _site_history(fleet, request.operator_id, request.site_id)
        try:
            assets = SiteAssets(
                battery_kwh=request.battery_kwh,
                battery_power_kw=request.battery_power_kw,
                genset_kw=request.genset_kw,
                min_soc_kwh=request.min_soc_kwh,
                genset_min_run_hours=request.genset_min_run_hours,
                diesel_price_per_l=request.diesel_price_per_l,
                grid_kw=request.grid_kw,
                grid_price_per_kwh=request.grid_price_per_kwh,
            )
            result = engine.dispatch_plan_for_site(
                history,
                assets=assets,
                soc_kwh=request.soc_kwh,
                horizon=request.horizon,
                model=request.model,
                plan_quantile=request.plan_quantile,
                calibrate=request.calibrate,
                assume_grid_always_on=request.assume_grid_always_on,
                display_timezone=request.timezone,
            )
        except (ValueError, ForecasterError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        expected = result.expected
        hours = [
            PlanHour(
                timestamp=ts.to_pydatetime(),
                planned_net_load_kw=float(result.planned_net_load_kw[i]),
                genset_on=bool(result.genset_on[i]),
                planned_genset_kw=float(expected.genset_kw[i]),
                planned_soc_kwh=float(expected.soc_kwh[i]),
                planned_grid_on=bool(result.planned_grid_on[i]),
                planned_grid_kw=float(expected.grid_kw[i]),
            )
            for i, ts in enumerate(result.timestamps)
        ]
        return PlanResponse(
            operator_id=request.operator_id,
            site_id=request.site_id,
            plan_quantile=request.plan_quantile,
            summary=result.summary(),
            run_windows=[
                RunWindow(start=a.to_pydatetime(), end=b.to_pydatetime())
                for a, b in result.run_windows
            ],
            grid_windows=result.grid_window_labels,
            expected_fuel_l=expected.fuel_l,
            expected_genset_hours=expected.genset_hours,
            expected_genset_starts=expected.genset_starts,
            expected_unserved_kwh=expected.unserved_kwh,
            hours=hours,
        )

    # -- daily pipeline: plans, track records and WhatsApp webhooks --------------

    def site_access(
        operator_id: str,
        authorization: str | None = Header(default=None),
        settings: Settings = Depends(get_app_settings),
        store: PlanStore = Depends(get_plan_store),
    ) -> None:
        """Each operator reaches only its own sites; the admin token reaches all.

        Without any credential the API serves data only in local development
        with no admin token set, so a demo works out of the box.
        """
        if not authorization:
            if settings.api_token is None and settings.environment != "production":
                return
            raise HTTPException(401, "send Authorization: Bearer <key>")
        token = authorization.removeprefix("Bearer ").strip()
        admin = settings.api_token
        if admin is not None and hmac.compare_digest(token, admin.get_secret_value()):
            return
        owner = store.operator_for_key(token)
        if owner is None:
            raise HTTPException(401, "unknown or revoked key")
        if owner != operator_id:
            raise HTTPException(403, "this key belongs to another operator")

    @app.get(
        "/sites/{operator_id}/{site_id}/plans/{plan_date}", dependencies=[Depends(site_access)]
    )
    def stored_plan(
        operator_id: str,
        site_id: str,
        plan_date: date,
        store: PlanStore = Depends(get_plan_store),
    ) -> dict[str, Any]:
        run = store.get_run(operator_id, site_id, plan_date)
        if run is None:
            raise HTTPException(404, "no plan stored for that site and date")
        hours = store.plan_hours(operator_id, site_id, plan_date)
        message = json.loads(run.message) if run.message else None
        return {
            "operator_id": run.operator_id,
            "site_id": run.site_id,
            "plan_date": str(run.plan_date),
            "status": run.status,
            "model": run.model,
            "fallback": run.fallback,
            "health": run.health,
            "message": None if message is None else message["text"],
            "expected_fuel_l": run.expected_fuel_l,
            "expected_genset_hours": run.expected_genset_hours,
            "expected_genset_starts": run.expected_genset_starts,
            "expected_unserved_kwh": run.expected_unserved_kwh,
            "hours": [
                {
                    "timestamp": pd.Timestamp(str(ts)).isoformat(),
                    "planned_net_load_kw": float(str(net)),
                    "genset_on": bool(gen),
                    "grid_on": bool(grid),
                }
                for ts, net, gen, grid in hours[
                    ["timestamp", "planned_net_load_kw", "genset_on", "grid_on"]
                ].itertuples(index=False, name=None)
            ],
            "advisory_only": True,
        }

    @app.get("/sites/{operator_id}/{site_id}/scorecard", dependencies=[Depends(site_access)])
    def site_scorecard(
        operator_id: str,
        site_id: str,
        days: int = Query(default=30, ge=1, le=366),
        store: PlanStore = Depends(get_plan_store),
    ) -> dict[str, Any]:
        card = scorecard(store, operator_id, site_id, days=days)
        return {
            "operator_id": operator_id,
            "site_id": site_id,
            "days": card.days,
            "range_held": card.range_held,
            "litres_saved": card.litres_saved,
            "outage_kwh_avoided": card.outage_kwh_avoided,
            "cost_saved": card.cost_saved,
            "share_of_possible": card.share_of_possible,
            "summary": card.summary(),
            "models": card.models.reset_index().to_dict(orient="records"),
            "basis": "if each plan had been followed, against planning from yesterday",
        }

    @app.post("/ingest/{operator_id}/{site_id}", dependencies=[Depends(site_access)])
    def ingest(
        operator_id: str,
        site_id: str,
        batch: IngestBatch,
        repo: TimeSeriesRepository = Depends(get_repository),
    ) -> dict[str, Any]:
        """Push readings from a monitoring system or integrator, up to 10,000 rows.

        Timestamps need a UTC offset, or a timezone for the whole batch.
        Optional columns (grid_available, battery_soc_pct, genset_kw,
        fuel_level_l) may be left out; stored values are then kept.
        """
        if not batch.readings:
            return {"rows": 0}
        frame = pd.DataFrame([r.model_dump(exclude_none=True) for r in batch.readings])
        stamps = pd.to_datetime(frame[TIMESTAMP], errors="coerce", format="mixed", utc=False)
        try:
            if stamps.dt.tz is None:
                if batch.timezone is None:
                    raise HTTPException(422, "timestamps need an offset, or set timezone")
                stamps = stamps.dt.tz_localize(batch.timezone)
            frame[TIMESTAMP] = stamps.dt.tz_convert("UTC")
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, f"unreadable timestamps: {exc}") from exc
        if frame[TIMESTAMP].isna().any():
            raise HTTPException(422, "unreadable timestamps")
        if frame.duplicated(subset=[TIMESTAMP]).any():
            raise HTTPException(422, "duplicate timestamps in the batch")
        frame[OPERATOR_ID] = operator_id
        frame[SITE_ID] = site_id
        for column in (LOAD_KW, GENERATION_KW):
            if column not in frame:
                frame[column] = float("nan")
        try:
            rows = repo.upsert(frame)
        except Exception as exc:  # schema violations come back as readable 422s
            raise HTTPException(422, f"rejected: {str(exc)[:300]}") from exc
        return {"rows": rows}

    @app.get("/sites/{operator_id}/{site_id}/health", dependencies=[Depends(site_access)])
    def site_health(
        operator_id: str,
        site_id: str,
        days: int = Query(default=30, ge=1, le=366),
        repo: TimeSeriesRepository = Depends(get_repository),
    ) -> dict[str, object]:
        """Data health over recent days: coverage, gaps, stuck meters, timezones."""
        site = _registered_site(operator_id, site_id)
        report = site_health_report(
            repo.read_history(operator_id, site_id),
            operator_id=operator_id,
            site_id=site_id,
            end=pd.Timestamp.now(tz="UTC"),
            days=days,
            timezone=site.timezone if site else "UTC",
            longitude=site.longitude if site else None,
            has_solar=site.solar is not None if site else True,
            has_grid=bool(site and site.grid and not site.grid.reliable),
            has_battery=site is not None,
        )
        return {**report.to_dict(), "summary": report.to_text()}

    @app.get("/webhooks/whatsapp", response_class=PlainTextResponse)
    def whatsapp_verify(
        mode: str = Query(alias="hub.mode"),
        token: str = Query(alias="hub.verify_token"),
        challenge: str = Query(alias="hub.challenge"),
        settings: Settings = Depends(get_app_settings),
    ) -> str:
        expected = settings.whatsapp_verify_token
        if (
            mode != "subscribe"
            or expected is None
            or not hmac.compare_digest(token, expected.get_secret_value())
        ):
            raise HTTPException(403, "verification failed")
        return challenge

    @app.post("/webhooks/whatsapp")
    async def whatsapp_events(
        request: Request,
        settings: Settings = Depends(get_app_settings),
        store: PlanStore = Depends(get_plan_store),
    ) -> dict[str, int]:
        if settings.whatsapp_app_secret is None:
            raise HTTPException(503, "set VATICORE_WHATSAPP_APP_SECRET to accept webhooks")
        body = await request.body()
        signature = request.headers.get("x-hub-signature-256")
        if not verify_signature(settings.whatsapp_app_secret.get_secret_value(), body, signature):
            raise HTTPException(401, "bad signature")
        outcome = handle_webhook(store, json.loads(body or b"{}"))
        return {
            "statuses": outcome.statuses,
            "replies": outcome.replies,
            "opt_outs": outcome.opt_outs,
            "opt_ins": outcome.opt_ins,
        }

    return app


app = create_app()
