# Vaticore architecture

How the system is put together, why, and what comes next. Read with
[`CLAUDE.md`](../CLAUDE.md) (how we build) and
[`docs/research/`](research/README.md) (how we prove it works).

## What the system does

Vaticore tells an operator of many distributed energy sites what to do with
their power, hour by hour, and proves what that advice is worth. The sites
include telecom towers, bank branches, factories, hospitals and mini-grids.
For each site it forecasts:

- demand
- solar output
- whether the grid will be on

Each forecast is a calibrated range, not a single guess. From those forecasts
it plans:

- when to run the generator
- when to lean on the grid
- how much battery to hold

Every plan is scored against what actually happened, in litres of diesel,
outage hours and money.

It advises; a person decides. Vaticore never controls equipment. That is a
safety and liability boundary, not a missing feature.

## One engine, many kinds of site

A tower and a factory differ in size and priorities, not in physics. Each has:

- demand
- maybe solar
- a battery
- maybe a generator
- maybe an unreliable grid

So there is one site model and one engine. `site_type` records what a site is
for. It drives defaults and reporting, never a separate code path.

| Site type | Typical load | What drives cost | What the operator needs |
|---|---|---|---|
| `telecom_tower` | 2 to 6 kW, flat, 24/7 | Diesel, fuel logistics and theft, uptime penalties | Generator hours, grid reliance, refuelling |
| `bank_branch` | 20 to 60 kW, business hours, ATMs 24/7 | Diesel, Band A grid tariff, uptime | Plan around grid hours, battery sizing |
| `commercial_industrial` | 100 kW to MW, shifts | Diesel, demand charges, production losses | Shift planning, solar self-use |
| `institution` | Critical loads (hospitals, schools) | Outages are the dominant cost | Guaranteed critical supply |
| `mini_grid` | Village or estate, evening peak | Diesel, customer outages | Daily generator schedule |

## Layers and data flow

```
 sources          ingestion           storage          features & forecasting        decisions        evaluation         surfaces
 ---------        ----------          --------         ----------------------        ---------        ----------         --------
 site meters  --> adapters:     -->   time series  --> calendar, lags, weather  -->  dispatch    -->  backtest     -->   API
 RMS / BMS        validate,           per operator     load, solar, net load         planner:         calibration        dashboard
 generator        UTC, gaps           and site         grid availability             grid,            value (litres,     reports
 grid on/off      (Elia, CSV,                          conformal calibration         generator,       outages, money)
 weather          ENTSO-E, ...)                                                      battery          benchmarks
                                                                                     (advisory)
                                    tracking: every experiment and study logged to MLflow
```

Data only moves left to right. Each layer has one job and one contract.

## Package map

| Package | Responsibility | Key types | Status |
|---|---|---|---|
| `schemas.py` | The internal time series schema and its validation | `TimeSeriesSchema`, column names | Stable |
| `sites/` | Sites, assets and portfolios (TOML) | `Site`, `SiteType`, `Portfolio` | New |
| `ingestion/` | Adapters from outside formats to the schema; gap reports | `load_csv`, `read_elia_load`, `to_site_frame` | CSV and Elia done; RMS vendors next |
| `storage/` | Multi-tenant persistence | DuckDB, Postgres/TimescaleDB repositories | Done |
| `features/` | Calendar, lag and weather features | `OpenMeteoProvider`, `join_weather` | Weather client ready; needs the archive |
| `forecasting/` | Every model behind one interface | `Forecaster`, `PersistenceForecaster`, `QuantileGBMForecaster`, `ConformalQuantileForecaster`, `GridAvailabilityForecaster` | Load, solar, net load, grid availability |
| `decisions/` | Forecast to plan: merit order dispatch; the daily advisory | `SiteAssets`, `plan_dispatch`, `simulate_dispatch` | Grid, generator, battery |
| `evaluation/` | Accuracy, calibration, value and benchmarks | `backtest_site`, `calibration_report`, `value_backtest`, `external_forecasts` | Done |
| `tracking/` | Experiment logging, with a no-op fallback | `Tracker`, `MlflowTracker` | Done |
| `engine.py` | The one orchestration path the API and dashboard use | `plan_for_site`, `run_value_backtest` | Done |
| `api/` | FastAPI service | `/forecast`, `/advisory`, `/plan` | Done |
| `dashboard/` | Streamlit operator view | Site today, track record | Done; to be hosted |
| `copilot/` | Plain-language explanations grounded on engine numbers | `explain_advisory` | Optional |

## Contracts

Four contracts hold the system together. Changing one is a design decision,
not a refactor.

1. **The schema.**
   - Every frame crossing an ingestion boundary is validated.
   - It is scoped by `operator_id` and `site_id`, and timestamps are UTC.
   - Targets are nullable, so a gap stays a visible gap.
   - `grid_available` (1 on, 0 off, missing if unknown) is optional; a grid
     site must record it.
2. **The forecaster.**
   - Every model implements `fit(history)` and
     `predict_quantiles(horizon, quantiles)`, and returns non-crossing
     quantiles.
   - The grid availability model obeys it too. For an on/off outcome, the q
     quantile is on when the chance of on exceeds 1 - q.
3. **The site.**
   - `Site.dispatch_assets()` is the only way physical assets reach the
     planner.
   - Prices are the site's own, in its own currency.
4. **The plan.**
   - `plan_dispatch` and `simulate_dispatch` share one physical model, so a
     plan and its outcome always agree on the physics.
   - Plans are advisory records: what to do, when, and what it should cost.

## Forecast targets

| Target | Why it matters | Baseline | Current model | Evidence |
|---|---|---|---|---|
| Load | What must be served | Seasonal persistence | Day-ahead quantile GBM | Research notes 1 and 2 |
| Solar | Free energy, weather driven | Seasonal persistence | Quantile GBM; weather next | Note 2: weather is the gap to close |
| Net load (load minus solar) | What the battery, grid and generator must cover | Seasonal persistence | Day-ahead quantile GBM plus conformal calibration | Notes 1 and 2 |
| Grid availability | When grid power can be counted on | Hour-of-week history | Hierarchical hour-of-week, recency weighted | Needs real site data |

Planning uses a high quantile of net load (P90) and a low quantile of grid
availability (P10). That is the cautious plan that holds on a bad day. The
value backtest measures whether that caution pays at each site's prices.

## Decisions: the merit order

In each hour, net load is served by:

1. the grid, if it is on;
2. the generator, if it was scheduled;
3. the battery.

Anything left is an outage. Surplus solar, and spare grid capacity, recharge
the battery. A generator that was not scheduled does not start. The planner
books the generator for exactly the hours the forecast needs it.

This rule is deliberately simple and explainable. Smarter planners, such as
pre-charging before a known peak or economic dispatch across tariff bands,
plug in behind the same interface. They must beat it in the value backtest to
replace it.

## Evaluation: three questions, every time

1. **Is it accurate?**
   - Pinball loss, MAE and RMSE against persistence.
   - Against a professional forecast where one exists, such as Elia's.
2. **Is it honest?**
   - Does the P10 to P90 range hold about 80% of outcomes?
   - Conformal calibration corrects it using only past outcomes.
3. **Is it worth anything?**
   - The value backtest replays each forecast's plans against reality.
   - It reports litres, outage hours and cost against persistence and a
     perfect-forecast bound, across a range of outage prices.

All three are real, tested code, and every study is reproducible from one
command. The protocol is in [`docs/research/README.md`](research/README.md).

## Multi-tenancy, privacy and safety

- Every function takes, or is scoped by, `operator_id` and `site_id`. No
  single-site code paths.
- Operator data is never committed. Studies record file names, periods and
  assumptions, never raw data.
- Secrets live in the environment (`.env`, never committed).
- Advisory only. Any move towards writing setpoints to site controllers needs
  an explicit, documented decision on safety and liability first.

## Deployment

- One Docker image serves the API and the dashboard.
- TimescaleDB is the production store.
- A Render blueprint is in `render.yaml` (see `DEPLOY.md`).

Planned: a scheduled pipeline that, each hour, ingests new readings, re-plans
every site and publishes plans and alerts.

## What comes next architecturally

In order of how much each unblocks the product:

1. **Scheduled pipeline and plan store.**
   - Hourly ingest, forecast and plan per site.
   - Plans are stored, so every recommendation can later be scored against
     what happened. This turns pilots into evidence automatically.
2. **Site data connectors.**
   - Remote monitoring (RMS) exports from tower and hybrid controller vendors.
   - Battery management systems.
   - Generator controllers and fuel-level sensors.
   - Each is an adapter into the schema.
3. **Weather service.**
   - Cached archived forecasts (not reanalysis) for backtests.
   - Live forecasts for plans.
4. **Fuel module.**
   - Expected fuel use from generator runtime and load, reconciled against
     deliveries and tank levels.
   - Flags likely pilferage and plans refuelling runs.
5. **Sizing studies.**
   - The same simulator, swept over solar and battery sizes, answers "what
     should this site add, and what will it save?". Useful before any
     installation.
6. **Portfolio reporting.**
   - Diesel avoided, outages avoided, and CO2 avoided (2.68 kg per litre of
     diesel) across a fleet, for operations, finance and ESG.
