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
from vaticore.pipeline.scoring import scorecard
from vaticore.pipeline.store import PlanStore
from vaticore.schemas import GENERATION_KW, LOAD_KW, OPERATOR_ID, SITE_ID, TIMESTAMP

try:  # FastAPI resolves this annotation at runtime; it needs the service extra.
    from fastapi import Request
except ImportError:  # pragma: no cover - service extra not installed
    Request = Any  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from fastapi import FastAPI

    from vaticore.storage import TimeSeriesRepository

_VALID_TARGETS = {LOAD_KW, GENERATION_KW}


@lru_cache(maxsize=1)
def get_repository() -> TimeSeriesRepository:
    """Cached repository, seeded with demo data if the store is empty.

    Seeding means a fresh deployment is never blank. Replace the seed with a real
    ingestion feed and this same store serves live operator data.
    """
    from vaticore.storage import get_repository as build_repository

    repo = build_repository()
    if repo.count() == 0:
        repo.upsert(make_synthetic_fleet(days=90, seed=1))
    return repo


def get_fleet() -> pd.DataFrame:
    """Data source dependency: read the full fleet from the repository."""
    return get_repository().read_fleet()


def get_app_settings() -> Settings:
    """Settings dependency, overridable in tests."""
    return get_settings()


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

    def require_token(
        authorization: str | None = Header(default=None),
        settings: Settings = Depends(get_app_settings),
    ) -> None:
        if settings.api_token is None:
            if settings.environment == "production":
                raise HTTPException(503, "set VATICORE_API_TOKEN to serve plan data")
            return
        expected = f"Bearer {settings.api_token.get_secret_value()}"
        if not hmac.compare_digest(authorization or "", expected):
            raise HTTPException(401, "missing or wrong bearer token")

    @app.get(
        "/sites/{operator_id}/{site_id}/plans/{plan_date}", dependencies=[Depends(require_token)]
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

    @app.get("/sites/{operator_id}/{site_id}/scorecard", dependencies=[Depends(require_token)])
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
