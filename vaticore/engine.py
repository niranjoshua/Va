"""Engine facade: one orchestration path for forecasting, backtesting and advice.

The API and the dashboard both go through here, so there is a single, tested
route from a site's history to a forecast, a backtest, or an operator advisory.
Nothing above this layer touches model classes directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from vaticore.decisions import Advisory, battery_and_genset_advisory
from vaticore.decisions.dispatch import DispatchPlan, SiteAssets, plan_dispatch
from vaticore.decisions.sizing import SizingCosts, SizingReport, size_site
from vaticore.evaluation.backtest import BacktestResult, FoldForecast, backtest_site
from vaticore.evaluation.calibration import (
    CalibrationReport,
    calibration_report,
    conformalize_forecasts,
)
from vaticore.evaluation.value import PolicySource, ValueReport, value_backtest
from vaticore.forecasting import Forecaster, PersistenceForecaster, QuantileGBMForecaster
from vaticore.forecasting.base import (
    DEFAULT_QUANTILES,
    InsufficientHistoryError,
    quantile_column,
)
from vaticore.forecasting.conformal import ConformalQuantileForecaster
from vaticore.forecasting.foundation import ChronosForecaster
from vaticore.forecasting.grid_availability import GridAvailabilityForecaster
from vaticore.forecasting.quantile_gbm import DAY_AHEAD_LAGS
from vaticore.schemas import (
    GENERATION_KW,
    GRID_AVAILABLE,
    LOAD_KW,
    NET_LOAD_KW,
    OPERATOR_ID,
    SITE_ID,
    TIMESTAMP,
    with_net_load,
)
from vaticore.sites.model import Site
from vaticore.tracking import Tracker

# Registry of model builders keyed by public name. Add new models here once
# they conform to the Forecaster interface.
ModelFactory = Callable[[str, tuple[float, ...]], Forecaster]

CHRONOS_CONTEXT_HOURS = 24 * 7 * 48

_MODELS: dict[str, ModelFactory] = {
    "persistence": lambda target, quantiles: PersistenceForecaster(target=target),
    "quantile_gbm": lambda target, quantiles: QuantileGBMForecaster(
        target=target, quantiles=quantiles
    ),
    # Day ahead preset: lags known at issue time only, so bands stay honest
    # over a 24 hour horizon. The default for planning (see DAY_AHEAD_LAGS).
    "quantile_gbm_day_ahead": lambda target, quantiles: QuantileGBMForecaster(
        target=target, quantiles=quantiles, lags=DAY_AHEAD_LAGS
    ),
    # Pretrained, zero shot challenger (research note 3). Needs the foundation
    # extra; weights download on first use. 48 weeks of context, as chosen on
    # the note's design period.
    "chronos_2": lambda target, quantiles: ChronosForecaster(
        target=target, context_length=CHRONOS_CONTEXT_HOURS
    ),
}

# Model used for day ahead planning and the value backtest.
PLANNING_MODEL = "quantile_gbm_day_ahead"

# The day-ahead GBM with live weather (radiation, cloud, temperature) as
# features: the challenger for solar sites, where weather is the largest
# accuracy gap measured (research note 2). It runs on net load through
# net_load_forecast, given the site's weather.
WEATHER_MODEL = "quantile_gbm_weather"
WEATHER_FEATURES = ("shortwave_radiation", "cloud_cover", "temperature_2m")
# Weather history the weather model needs before it is trusted to train.
MIN_WEATHER_DAYS = 14


def available_models() -> list[str]:
    return list(_MODELS)


def _build(model: str, target: str, quantiles: tuple[float, ...]) -> Forecaster:
    if model not in _MODELS:
        raise ValueError(f"unknown model {model!r}; available: {available_models()}")
    return _MODELS[model](target, quantiles)


def select_site(fleet: pd.DataFrame, operator_id: str, site_id: str) -> pd.DataFrame:
    """Return one site's rows. Multi tenant scoping lives here, once."""
    site = fleet[(fleet[OPERATOR_ID] == operator_id) & (fleet[SITE_ID] == site_id)]
    if site.empty:
        raise KeyError(f"no data for operator {operator_id!r} site {site_id!r}")
    return site.sort_values(TIMESTAMP).reset_index(drop=True)


def forecast_site(
    history: pd.DataFrame,
    target: str,
    *,
    horizon: int,
    model: str = "quantile_gbm",
    quantiles: tuple[float, ...] = DEFAULT_QUANTILES,
) -> pd.DataFrame:
    """Fit the chosen model on one site's history and forecast the horizon."""
    forecaster = _build(model, target, quantiles).fit(history)
    return forecaster.predict_quantiles(horizon=horizon, quantiles=quantiles)


def run_backtest(
    history: pd.DataFrame,
    target: str,
    *,
    horizon: int,
    initial: int,
    step: int | None = None,
    model: str = "quantile_gbm",
    quantiles: tuple[float, ...] = DEFAULT_QUANTILES,
    exog: tuple[str, ...] = (),
    tracker: Tracker | None = None,
    operator_id: str | None = None,
    site_id: str | None = None,
) -> BacktestResult:
    """Backtest a model against persistence on one site.

    When exog columns are given and the model is quantile_gbm, the candidate is
    built with those exogenous (weather) features and receives the test window's
    values as future_exog, mirroring having a weather forecast in production.

    When a tracker is supplied, the comparison is logged to it (see
    vaticore.tracking). Pass operator_id and site_id so the logged run records
    which site it scored. A NoOpTracker (the default via get_tracker when tracking
    is not configured) makes this a no-op.
    """

    def make_model() -> Forecaster:
        if model == "quantile_gbm" and exog:
            return QuantileGBMForecaster(target=target, quantiles=quantiles, exog_features=exog)
        return _build(model, target, quantiles)

    result = backtest_site(
        history,
        target=target,
        make_model=make_model,
        horizon=horizon,
        initial=initial,
        step=step,
        quantiles=quantiles,
        model_name=model,
        exog=exog,
    )

    if tracker is not None:
        tracker.log_backtest(
            result,
            candidate_model=model,
            operator_id=operator_id,
            site_id=site_id,
            extra_params={
                "initial": initial,
                "step": step if step is not None else horizon,
                "exog": ",".join(exog) if exog else None,
            },
        )

    return result


def advisory_for_site(
    history: pd.DataFrame,
    *,
    horizon: int,
    usable_battery_kwh: float,
    step_hours: float = 1.0,
    model: str = "quantile_gbm",
    quantiles: tuple[float, ...] = DEFAULT_QUANTILES,
) -> Advisory:
    """Forecast load and generation, then derive a battery and genset advisory."""
    load_fc = forecast_site(history, LOAD_KW, horizon=horizon, model=model, quantiles=quantiles)
    gen_fc = forecast_site(
        history, GENERATION_KW, horizon=horizon, model=model, quantiles=quantiles
    )
    reserve_q = max(quantiles)
    return battery_and_genset_advisory(
        load_fc,
        gen_fc,
        usable_battery_kwh=usable_battery_kwh,
        step_hours=step_hours,
        reserve_quantile=reserve_q,
    )


# -- net load: calibration, value and the hourly plan ------------------------


def _net_load_model(
    model: str, quantiles: tuple[float, ...], exog: tuple[str, ...] = ()
) -> Callable[[], Forecaster]:
    """Factory for a model forecasting signed net load (no clipping at zero)."""
    if model == WEATHER_MODEL:
        return lambda: QuantileGBMForecaster(
            target=NET_LOAD_KW,
            quantiles=quantiles,
            lags=DAY_AHEAD_LAGS,
            exog_features=exog or WEATHER_FEATURES,
            nonnegative=False,
        )
    if model in ("quantile_gbm", "quantile_gbm_day_ahead"):
        lags = DAY_AHEAD_LAGS if model == "quantile_gbm_day_ahead" else (1, 2, 3, 24, 48, 168)
        return lambda: QuantileGBMForecaster(
            target=NET_LOAD_KW,
            quantiles=quantiles,
            lags=lags,
            exog_features=exog,
            nonnegative=False,
        )
    if model == "persistence":
        return lambda: PersistenceForecaster(target=NET_LOAD_KW, nonnegative=False)
    if model == "chronos_2":
        return lambda: ChronosForecaster(
            target=NET_LOAD_KW, context_length=CHRONOS_CONTEXT_HOURS, nonnegative=False
        )
    raise ValueError(f"unknown model {model!r}; available: {available_models()}")


def policy_label(model: str, quantile: float) -> str:
    """Human label for a planning policy, for example 'quantile_gbm P90'."""
    return f"{model} P{round(quantile * 100)}"


@dataclass(frozen=True)
class ValueBacktest:
    """Everything one value backtest produced."""

    backtest: BacktestResult
    calibration: dict[str, CalibrationReport]
    value: ValueReport
    baseline_policy: str


def run_value_backtest(
    history: pd.DataFrame,
    *,
    assets: SiteAssets,
    initial: int,
    horizon: int = 24,
    model: str = PLANNING_MODEL,
    quantiles: tuple[float, ...] = DEFAULT_QUANTILES,
    plan_quantiles: tuple[float, ...] = (0.5, 0.9),
    conformal_window: int | None = 28,
    exog: tuple[str, ...] = (),
    tracker: Tracker | None = None,
    operator_id: str | None = None,
    site_id: str | None = None,
    grid_plan_quantile: float = 0.1,
) -> ValueBacktest:
    """Measure what forecasts are worth to one site in fuel, outages and money.

    Forecasts net load day ahead with the candidate model and persistence on
    back to back windows, optionally calibrates the candidate's bands fold by
    fold (conformal_window prior folds; None to skip), then plans and operates
    the site on each policy's forecasts. The baseline is persistence planned
    on its median: roughly what an operator does today by assuming tomorrow
    looks like yesterday.

    For a site with a grid connection (assets.grid_kw > 0) the history must
    carry grid_available. Every policy then shares one grid plan, forecast
    before each day from the grid record up to that moment
    (GridAvailabilityForecaster at grid_plan_quantile), so differences between
    policies still come from the net load forecast alone.
    """
    if model == "persistence":
        raise ValueError("the candidate model must differ from the persistence baseline")
    missing = [q for q in plan_quantiles if not any(abs(q - x) < 1e-9 for x in quantiles)]
    if missing:
        raise ValueError(f"plan quantiles {missing} are not among the forecast quantiles")

    data = with_net_load(history)
    result = backtest_site(
        data,
        target=NET_LOAD_KW,
        make_model=_net_load_model(model, quantiles, exog),
        horizon=horizon,
        initial=initial,
        step=horizon,
        quantiles=quantiles,
        model_name=model,
        exog=exog,
        nonnegative=False,
    )
    base_fc = result.forecasts_for("persistence")
    cand_fc = result.forecasts_for(model)

    calibration = {
        "persistence": calibration_report(base_fc),
        model: calibration_report(cand_fc),
    }
    baseline = policy_label("persistence", 0.5)
    policies: dict[str, PolicySource] = {baseline: PolicySource(base_fc, 0.5)}
    for q in plan_quantiles:
        if abs(q - 0.5) > 1e-9:
            policies[policy_label("persistence", q)] = PolicySource(base_fc, q)
    for q in plan_quantiles:
        policies[policy_label(model, q)] = PolicySource(cand_fc, q)

    if conformal_window:
        conf = conformalize_forecasts(cand_fc, window_folds=conformal_window, nonnegative=False)
        conf_name = conf.forecasts[0].model
        calibration[conf_name] = calibration_report(conf.forecasts)
        for q in plan_quantiles:
            if abs(q - 0.5) > 1e-9:  # calibration leaves the median unchanged
                policies[policy_label(conf_name, q)] = PolicySource(conf.forecasts, q)

    grid_actual = None
    if assets.grid_kw > 0:
        grid_plan, grid_actual = _grid_plan_for_folds(data, cand_fc, grid_plan_quantile)
        policies = {
            name: PolicySource(src.forecasts, src.plan_quantile, grid_plan)
            for name, src in policies.items()
        }
    value = value_backtest(policies, assets=assets, baseline=baseline, grid_actual=grid_actual)
    outcome = ValueBacktest(
        backtest=result, calibration=calibration, value=value, baseline_policy=baseline
    )
    if tracker is not None:
        _log_value_backtest(
            tracker,
            outcome,
            model=model,
            horizon=horizon,
            initial=initial,
            conformal_window=conformal_window,
            operator_id=operator_id,
            site_id=site_id,
        )
    return outcome


def _grid_plan_for_folds(
    data: pd.DataFrame, folds: list[FoldForecast], quantile: float
) -> tuple[pd.Series, pd.Series]:
    """Grid plan per fold from data strictly before it, plus the actual record."""
    if GRID_AVAILABLE not in data.columns:
        raise ValueError(
            f"this site has a grid connection but no {GRID_AVAILABLE!r} history; "
            "record grid on/off per hour or set grid_kw=0"
        )
    stamps = pd.DatetimeIndex(data[TIMESTAMP])
    parts = []
    for fold in folds:
        before = data[stamps < fold.timestamps[0]]
        forecaster = GridAvailabilityForecaster().fit(before)
        fc = forecaster.predict_quantiles(len(fold.timestamps), (quantile,))
        parts.append(fc[quantile_column(quantile)].reindex(fold.timestamps))
    actual = data.set_index(TIMESTAMP)[GRID_AVAILABLE].astype(float)
    return pd.concat(parts), actual


def _log_value_backtest(
    tracker: Tracker,
    outcome: ValueBacktest,
    *,
    model: str,
    horizon: int,
    initial: int,
    conformal_window: int | None,
    operator_id: str | None,
    site_id: str | None,
) -> None:
    params: dict[str, object] = {
        "operator_id": operator_id,
        "site_id": site_id,
        "candidate_model": model,
        "target": NET_LOAD_KW,
        "horizon": horizon,
        "initial": initial,
        "conformal_window": conformal_window,
        "baseline_policy": outcome.baseline_policy,
    }
    params.update({f"asset.{k}": v for k, v in asdict(outcome.value.assets).items()})
    metrics: dict[str, float] = {}
    frame = outcome.value.to_frame()
    for policy, row in frame.iterrows():
        for col in ("fuel_l", "unserved_kwh", "total_cost", "savings_vs_baseline"):
            metrics[f"{policy}.{col}"] = float(row[col])
        share = row["share_of_possible"]
        if share is not None and pd.notna(share):
            metrics[f"{policy}.share_of_possible"] = float(share)
    for name, report in outcome.calibration.items():
        for band in report.intervals:
            metrics[f"{name}.coverage_{band.lower:g}_{band.upper:g}"] = band.observed
    tracker.log_summary(
        run_name=f"value-{model}",
        params=params,
        metrics=metrics,
        tags={"kind": "value_backtest"},
    )


def net_load_forecast(
    history: pd.DataFrame,
    *,
    horizon: int,
    model: str = PLANNING_MODEL,
    quantiles: tuple[float, ...] = DEFAULT_QUANTILES,
    calibrate: bool = True,
    weather: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Quantile forecast of net load for the hours after the history ends.

    With calibrate=True the bands are corrected on the site's own recent misses
    (conformalized quantile regression over the last week).

    The weather model (WEATHER_MODEL) needs `weather`: hourly
    WEATHER_FEATURES by UTC timestamp, covering the history it trains on and
    every forecast hour. It trains only on hours that have weather, and refuses
    to forecast hours that have none rather than guessing.
    """
    data = with_net_load(history)
    future: pd.DataFrame | None = None
    exog: tuple[str, ...] = ()
    if model == WEATHER_MODEL:
        data, future = _with_weather(data, weather, horizon)
        exog = WEATHER_FEATURES
    make = _net_load_model(model, quantiles, exog)
    forecaster: Forecaster
    if calibrate:
        forecaster = ConformalQuantileForecaster(
            make,
            target=NET_LOAD_KW,
            quantiles=quantiles,
            window_horizon=horizon,
            nonnegative=False,
        )
    else:
        forecaster = make()
    fitted = forecaster.fit(data)
    if future is not None:
        return fitted.predict_quantiles(  # type: ignore[call-arg]
            horizon=horizon, quantiles=quantiles, future_exog=future
        )
    return fitted.predict_quantiles(horizon=horizon, quantiles=quantiles)


def _with_weather(
    data: pd.DataFrame, weather: pd.DataFrame | None, horizon: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """History joined with weather (from the first weather hour), and the future weather."""
    if weather is None or weather.empty:
        raise ValueError("the weather model needs the site's weather")
    missing = [c for c in WEATHER_FEATURES if c not in weather.columns]
    if missing:
        raise ValueError(f"weather is missing {missing}")
    table = weather[[TIMESTAMP, *WEATHER_FEATURES]].copy()
    table[TIMESTAMP] = pd.DatetimeIndex(pd.to_datetime(table[TIMESTAMP], utc=True))
    table = table.dropna(subset=list(WEATHER_FEATURES)).drop_duplicates(TIMESTAMP)
    end = _history_end(data)
    past = data[data[TIMESTAMP] >= table[TIMESTAMP].min()].drop(
        columns=[c for c in WEATHER_FEATURES if c in data.columns]
    )
    joined = past.merge(table, on=TIMESTAMP, how="left")
    covered = joined.dropna(subset=list(WEATHER_FEATURES))
    if covered.empty or (
        _history_end(covered) - pd.Timestamp(covered[TIMESTAMP].min())
    ) < pd.Timedelta(days=MIN_WEATHER_DAYS):
        raise InsufficientHistoryError(
            f"the weather model needs {MIN_WEATHER_DAYS} days of history with weather"
        )
    wanted = pd.date_range(end + pd.Timedelta(hours=1), periods=horizon, freq="1h")
    future = table.set_index(TIMESTAMP).reindex(wanted)
    if future.isna().any().any():
        raise ValueError(
            f"no weather forecast for {int(future.isna().any(axis=1).sum())} of the "
            f"{horizon} forecast hours"
        )
    future.index.name = TIMESTAMP
    return joined, future.reset_index()


def grid_plan(
    history: pd.DataFrame,
    index: pd.DatetimeIndex,
    *,
    assets: SiteAssets,
    quantile: float = 0.1,
    assume_grid_always_on: bool = False,
) -> np.ndarray | None:
    """Hours (1 or 0) the plan may count on grid power; None for a site with no grid.

    Forecast from the site's grid record at a low quantile (P10 by default, the
    cautious choice): the plan counts on the grid only when it is on even in a
    bad week.
    """
    if assets.grid_kw <= 0:
        return None
    if GRID_AVAILABLE in history.columns and history[GRID_AVAILABLE].notna().any():
        forecaster = GridAvailabilityForecaster().fit(history)
        steps = int((index[-1] - _history_end(history)) / pd.Timedelta(hours=1))
        frame = forecaster.predict_quantiles(max(steps, len(index)), (quantile,))
        return frame[quantile_column(quantile)].reindex(index).fillna(0.0).to_numpy(dtype=float)
    if assume_grid_always_on:
        return np.ones(len(index))
    raise ValueError(
        f"this site has a grid connection but no {GRID_AVAILABLE!r} history; record "
        "grid on/off per hour, or pass assume_grid_always_on=True for a reliable grid"
    )


def _history_end(history: pd.DataFrame) -> pd.Timestamp:
    """Last timestamp in a history frame."""
    return pd.Timestamp(pd.DatetimeIndex(history[TIMESTAMP]).max())


def dispatch_plan_for_site(
    history: pd.DataFrame,
    *,
    assets: SiteAssets,
    soc_kwh: float,
    horizon: int = 24,
    model: str = PLANNING_MODEL,
    plan_quantile: float = 0.9,
    quantiles: tuple[float, ...] = DEFAULT_QUANTILES,
    calibrate: bool = True,
    grid_plan_quantile: float = 0.1,
    assume_grid_always_on: bool = False,
    display_timezone: str = "UTC",
) -> DispatchPlan:
    """Today's hour by hour generator schedule for one site. Advisory only.

    Forecasts net load (calibrated on the site's own last week when
    calibrate=True), then schedules the generator for the hours the chosen
    quantile needs it, starting from the battery's current charge. For a site
    with a grid, the plan counts on grid power only in hours the availability
    forecast's grid_plan_quantile (P10 by default, the cautious choice) says it
    will be on.
    """
    if not any(abs(plan_quantile - q) < 1e-9 for q in quantiles):
        raise ValueError(f"plan_quantile {plan_quantile} is not among quantiles {quantiles}")
    forecast = net_load_forecast(
        history, horizon=horizon, model=model, quantiles=quantiles, calibrate=calibrate
    )
    planned_grid = grid_plan(
        history,
        pd.DatetimeIndex(forecast.index),
        assets=assets,
        quantile=grid_plan_quantile,
        assume_grid_always_on=assume_grid_always_on,
    )
    return plan_dispatch(
        forecast[quantile_column(plan_quantile)],
        soc_kwh=soc_kwh,
        assets=assets,
        planned_grid_available=planned_grid,
        display_timezone=display_timezone,
    )


def plan_for_site(
    site: Site,
    history: pd.DataFrame,
    *,
    soc_kwh: float,
    horizon: int = 24,
    plan_quantile: float = 0.9,
) -> DispatchPlan:
    """Today's plan for a registered site, using its own assets and prices."""
    if history.empty:
        raise ValueError(f"no history for site {site.site_id!r}")
    return dispatch_plan_for_site(
        history,
        assets=site.dispatch_assets(),
        soc_kwh=soc_kwh,
        horizon=horizon,
        plan_quantile=plan_quantile,
        assume_grid_always_on=bool(site.grid and site.grid.reliable),
        display_timezone=site.timezone,
    )


def size_registered_site(
    site: Site,
    history: pd.DataFrame,
    solar_per_kwp: pd.Series,
    *,
    pv_options_kwp: list[float],
    battery_options_kwh: list[float],
    costs: SizingCosts,
    battery_c_rate: float = 0.5,
    min_soc_fraction: float = 0.1,
) -> SizingReport:
    """Sizing study for a registered site, with its own assets and prices.

    history carries the site's hourly load_kw (and grid_available for a site
    on an unreliable grid); solar_per_kwp is measured or from
    vaticore.features.solar.pv_output_per_kwp at the site's location.
    """
    frame = history.set_index(TIMESTAMP).sort_index()
    grid = None
    if site.grid is not None:
        if site.grid.reliable:
            grid = pd.Series(1.0, index=frame.index)
        elif GRID_AVAILABLE in frame.columns:
            grid = frame[GRID_AVAILABLE].astype(float)
        else:
            raise ValueError(
                f"site {site.site_id!r} has an unreliable grid but no {GRID_AVAILABLE!r} history"
            )
    return size_site(
        frame[LOAD_KW],
        solar_per_kwp,
        site.dispatch_assets(),
        current_pv_kwp=site.solar.kwp if site.solar else 0.0,
        pv_options_kwp=pv_options_kwp,
        battery_options_kwh=battery_options_kwh,
        costs=costs,
        currency=site.currency,
        battery_c_rate=battery_c_rate,
        min_soc_fraction=min_soc_fraction,
        grid_available=grid,
    )
