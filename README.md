# Vaticore

Probabilistic energy forecasting and planning for distributed energy sites:
telecom towers, bank branches, commercial and industrial sites, institutions
and mini-grids.

The name comes from the Latin *vaticinari*, to foretell, and *core*: the
forecasting core that operators build their dispatch decisions on.

Vaticore predicts electricity **load**, **solar generation** and **whether the
grid will be on** for each site an operator runs, and turns those forecasts
into an hourly plan: when to run the generator, when to lean on the grid, and
how much battery to hold. Built first for Nigerian operators who spend heavily
on diesel. See [`docs/architecture.md`](docs/architecture.md) for how it fits
together and [`docs/research/`](docs/research/README.md) for the evidence.
Forecasts are probabilistic, the pipeline tolerates messy and sparse data, and
evaluation is a first-class concern.

## Status

Ready for a first pilot: a daily service that plans each site the evening
before, tells the people who run it, and scores itself.

- **Daily pipeline**: every evening, each site's next-day plan is made,
  stored and sent on WhatsApp or email, in English or Nigerian Pidgin per
  person. Replies of 1 (followed) and 2 (not, and why) are recorded; STOP
  always wins. A site with poor data is told "no plan today, run as usual",
  and a 19:30 safety net catches anything the job missed. Yesterday's plans
  are scored against what happened; supervisors get a Monday summary
  (`docs/pipeline.md`).
- **Pilot measurement**: shadow weeks (plans made and scored, never sent),
  the go/no-go verdict (`shadow-review`) and the savings report (`report`),
  in the CLI, the API and the dashboard (`docs/pilot-measurement.md`).
- **Fuel and monitoring**: diesel delivered against burned
  (`docs/fuel.md`), data health per site, and model monitoring that falls
  back to a safer model when one drifts.
- **Efficiency review**: replays each site's own history under the operating
  rules that save the most diesel (battery first; run hard, then off) and
  says which to adopt, with the litres and money against what the site did
  (`efficiency`, `docs/pipeline.md`).
- **Production**: staging and production on Render from one image, operator
  keys that reach only that operator's sites, migrations, logs, error
  tracking, uptime and heartbeat checks, and backups proven by restoring
  them (`docs/operations.md`, `docs/runbook.md`).

- **Ingestion and schema**: CSV/API intake, timezone normalisation, gap
  detection, and strict validation against the internal schema.
- **Models**: a probabilistic persistence baseline and a quantile gradient
  boosting model, both behind one interface. `quantile_gbm_day_ahead` uses only
  lags known when a day ahead forecast is issued, so its range stays honest over
  24 hours; it is the default for planning. The GBM can take
  exogenous weather covariates (forecast radiation, cloud cover, temperature),
  which sharply improve the solar generation forecast (see
  `examples/weather_lift.py`).
- **Evaluation**: pinball loss, calibration and a rolling origin backtest that
  always scores the candidate against persistence. On the quickstart's
  synthetic demo data the quantile GBM cuts pinball loss by **43% on load** and
  **17% on solar generation** versus the baseline.
- **Calibration**: conformal calibration (CQR) so the P10 to P90 range holds
  about 80% of outcomes, as a wrapper for any model
  (`ConformalQuantileForecaster`) and fold by fold inside backtests. Coverage is
  reported with every run.
- **Sites**: a typed registry of sites and their assets (solar, battery,
  generator, grid connection), loaded from TOML. `examples/sites/` has a
  Nigerian portfolio with one site of each type.
- **Grid**: intermittent grid supply in the planner, and a grid availability
  forecaster (hour-of-week, recency weighted) so a plan counts on the grid only
  in its reliable hours.
- **Decisions**: an hour by hour plan ("run the generator 18:00 to midnight;
  the plan counts on grid power 06:00 to 14:00") from a merit order model:
  grid, then generator, then battery, with a HOMER-standard fuel curve. Served
  at `POST /plan` and on the dashboard. Advisory only. Per site, the generator
  can run hard, then off (`generator.charge_setpoint`), and the day can be
  planned at once for the fewest litres (`look_ahead`); research note 4 says
  where each pays.
- **Sizing studies**: sweeps solar and battery sizes for a site through the same
  hour by hour model, with a real year of weather at its location, and reports
  diesel, outages, payback and CO2 for each (`size_site`,
  `examples/sizing_study.py`).
- **Value backtest**: replays each forecast's plans against what actually
  happened and reports litres, outage hours and money against persistence and
  a perfect forecast (`run_value_backtest`, `examples/value_study.py`).
- **Experiment tracking**: every backtest can be logged to MLflow (site, model,
  config and metrics, plus the pinball skill over persistence), so model
  comparisons are reproducible. Off by default; enable it by setting
  `VATICORE_MLFLOW_TRACKING_URI` and installing the `tracking` extra.
- **Weather**: a real Open-Meteo client (no API key) feeding weather features.
- **Copilot**: an optional LLM layer that explains an advisory in plain language,
  grounded on the engine's numbers, with a deterministic template fallback.
- **Surfaces**: a FastAPI service, a Streamlit dashboard, and a static landing
  page, all driven by one engine facade so they never disagree.
- **Ops**: Dockerfile, docker-compose (with TimescaleDB), and a Render blueprint
  (`DEPLOY.md`).
- **Storage**: DuckDB (local) and Postgres/TimescaleDB (production) repositories
  behind one interface, wired into the API, which seeds demo data on first run
  and serves from the store. `examples/ingest_csv.py` loads a real operator CSV.
  The database is chosen by the `VATICORE_DATABASE_URL` scheme.
- **Foundation models**: Chronos-2 and TimesFM wrapped behind the same
  interface (`foundation` extra). Chronos-2 is a planning model in the engine
  (`chronos_2`); see [research note 3](docs/research/foundation-models.md).

Next: the first pilot's own data, then the weather model in shadow on the
pilot's scored days.

## Evidence

[Research note 1](docs/research/value-study-2018.md) is a one-year held-out study
on public ENTSO-E demand and solar data, shaped into a 120 kW solar mini-grid.
Planning the generator on Vaticore's calibrated P90 forecast cut unserved
energy by 82 to 84% and outage hours by 72% against planning on persistence,
for 11 to 16% more diesel. That lowered total cost by 7 to 10% at $1 per
unserved kWh and captured 61 to 63% of the value of a perfect forecast.
Pinball loss fell 23%, and the calibrated P10 to P90 range held 80.7% of
outcomes (target 80%).

[Research note 2](docs/research/elia-benchmark.md) scores Vaticore against the
Belgian grid operator's own published forecasts. Over 453 held-out days of
national load it matched Elia's day-ahead forecast on pinball loss (95.2 MW
each) and MAE (287.8 against 292.4 MW), without weather data. Its range was
better calibrated: 80.4% of outcomes against Elia's 74.9%, target 80%. On
solar, where Elia uses weather, Elia is far ahead; weather is the next study.

[Research note 4](docs/research/diesel-practice.md) measures diesel against
how sites run today, on a held-out year. At an off-grid telecom tower,
Vaticore's plan used 19.6% less diesel than the best-run practice and 30.4%
less than an evening timer, with no outages; most of it came from running the
generator hard, then off. At a tower on a weak grid (a synthetic outage
pattern), letting the battery go first used 83.5% less diesel than running
the generator whenever the grid is off. On the mini-grid, diesel fell 2.0% and
outage hours fell from 94 to 30.

## Layout

```
vaticore/
  ingestion/     # intake: CSV, Elia, monitoring connectors (docs/data-connectors.md)
  features/      # calendar, lags, weather enrichment
  forecasting/   # baselines, quantile GBM, conformal, grid, foundation models
  evaluation/    # backtesting harness, pinball loss, calibration, baseline comparison
  tracking/      # MLflow experiment tracking, with a no-op fallback
  sites/         # site and asset registry (towers, banks, C&I, institutions, mini-grids)
  decisions/     # forecast -> hourly plan: grid, generator, battery (advisory only)
  api/           # FastAPI service (thin handlers over the engine)
  dashboard/     # Streamlit app
  copilot/       # LLM explanations grounded on the engine's numbers
  pipeline/      # the daily loop, scoring, pilot report, weekly summary (docs/pipeline.md)
  delivery/      # daily plans on WhatsApp and email, replies, opt-outs
  fuel/          # diesel delivered against burned, flags for checking (docs/fuel.md)
  storage/       # multi-tenant, multi-site persistence, numbered migrations
  engine.py      # orchestration facade used by api and dashboard
  schemas.py     # the internal schema, validated at every ingestion boundary
  access.py      # who may see which operator's sites (keys, admin token)
  observability.py  # JSON logs, Sentry, heartbeat
  datasets.py    # synthetic demo data
  config.py      # typed settings (pydantic-settings), env-driven
examples/        # quickstart, research studies, example site portfolios
docs/            # guides, below; docs/research/ holds the evidence
docs/landing/    # the website (index.html, privacy.html)
tests/           # one module per area; golden numbers in tests/golden/
Dockerfile, docker-compose.yml, render.yaml, DEPLOY.md   # deployment
```

## Documentation

| Read | For |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | How the system fits together |
| [`docs/research/README.md`](docs/research/README.md) | The research programme, protocol and the three study notes |
| [`docs/pipeline.md`](docs/pipeline.md) | The daily loop: data health, plan, send, score |
| [`docs/pilot-measurement.md`](docs/pilot-measurement.md) | How a pilot's savings are measured, agreed before it starts |
| [`docs/pilot/`](docs/pilot/) | The site onboarding form; privacy and consent messages |
| [`docs/data-connectors.md`](docs/data-connectors.md) | Pulling readings from monitoring platforms |
| [`docs/fuel.md`](docs/fuel.md) | Fuel reconciliation |
| [`docs/whatsapp-setup.md`](docs/whatsapp-setup.md) | WhatsApp Business Platform: templates, webhook, recipients |
| [`DEPLOY.md`](DEPLOY.md) | Local, Docker and Render setup |
| [`docs/operations.md`](docs/operations.md) | Running production: environments, access, migrations, monitoring, backups |
| [`docs/runbook.md`](docs/runbook.md) | What to do when something fails |
| [`docs/domain.md`](docs/domain.md) | vaticore.co.uk: website, email, service addresses |
| [`SECURITY.md`](SECURITY.md) | Reporting a security problem |

## Quickstart

```bash
uv sync                              # core dependencies
uv run pytest                        # run the test suite
uv run python examples/quickstart.py # forecast, backtest and advise, end to end
```

Run the dashboard and the API:

```bash
uv sync --extra dashboard
uv run streamlit run vaticore/dashboard/app.py   # visual demo

uv sync --extra service
uv run uvicorn vaticore.api.main:app --reload    # API, docs at /docs
```

The daily pipeline (plan, send, score), on synthetic data with nothing sent:

```bash
uv run python -m vaticore.pipeline demo
```

Example API call:

```bash
curl -X POST localhost:8000/forecast -H 'content-type: application/json' -d '{
  "operator_id": "lagos-energy", "site_id": "ikeja-minigrid",
  "target": "load_kw", "horizon": 24, "model": "quantile_gbm"
}'

# Today's generator schedule for a 600 kWh battery at half charge
curl -X POST localhost:8000/plan -H 'content-type: application/json' -d '{
  "operator_id": "lagos-energy", "site_id": "ikeja-minigrid",
  "battery_kwh": 600, "battery_power_kw": 150, "genset_kw": 100, "soc_kwh": 300
}'
```

Run the value study on real data (public ENTSO-E Spain data, not committed):

```bash
curl -L -o energy_dataset.csv \
  https://raw.githubusercontent.com/unit8co/darts/master/datasets/energy_dataset.csv
uv run python examples/value_study.py energy_dataset.csv \
  --start 2017-01-01 --end 2018-12-31T23:00 --initial-days 365 --out results/
```

Optional extras, installed only when needed:

```bash
uv sync --extra service     # FastAPI service
uv sync --extra dashboard   # Streamlit dashboard
uv sync --extra models      # xgboost + torch
uv sync --extra foundation  # Chronos-2 and TimesFM (weights from Hugging Face)
uv sync --extra tracking    # MLflow experiment tracking
uv sync --all-extras        # everything
```

Common commands are wrapped in the Makefile: `make check` runs lint, type check
and tests.

## Core contracts

Two contracts hold the system together and should not be broken:

1. **The internal schema** (`vaticore/schemas.py`). Every dataframe crossing an
   ingestion boundary is validated against it. Data is always scoped by
   `operator_id` and `site_id`, timestamps are timezone aware UTC, and targets
   are nullable so gaps are explicit rather than dropped.
2. **The forecaster interface** (`vaticore/forecasting/base.py`). Every model
   implements `fit` and `predict_quantiles`, so models are swappable and
   ensemble-able. Output is always quantiles, even for the baseline.

## Principles

- Prove the pipeline end to end before adding model complexity.
- Probabilistic by default. Evaluate on pinball loss and calibration, always
  against the persistence baseline.
- Built for messy, sparse data and cold-start sites.
- Advisory only. No closed-loop control that touches an operator's dispatch.

See `CLAUDE.md` for build conventions. The code is published to show the
method and let results be checked; it is not open source (`LICENSE`).
