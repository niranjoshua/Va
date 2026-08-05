# Vaticore

Probabilistic energy forecasting for mini-grid and C&I solar operators.

The name comes from the Latin *vaticinari*, to foretell, and *core*: the
forecasting core that operators build their dispatch decisions on.

Vaticore predicts electricity **load** and **solar generation** for distributed
energy operators, and turns those forecasts into operational decisions: how much
battery reserve to hold, when to run a generator, and how much demand may go
unserved. Forecasts are probabilistic, the pipeline tolerates messy and sparse
data, and evaluation is a first-class concern.

## Status

Early scaffold. The end-to-end skeleton is in place and tested: ingestion,
the internal schema, the forecaster interface, a real probabilistic persistence
baseline, the evaluation harness, and a first decision advisory. Heavier models
(quantile gradient boosting, LSTM, TIME-LLM, ensemble) are stubbed against the
interface and land next, in that order, once the pipeline is proven.

## Layout

```
vaticore/
  ingestion/     # CSV/API intake, validation, timestamp normalisation, gap handling
  features/      # calendar, lags, weather enrichment
  forecasting/   # baselines, quantile GBM, (later) LSTM, TIME-LLM, ensemble
  evaluation/    # backtesting harness, pinball loss, calibration, baseline comparison
  decisions/     # forecast -> battery reserve / genset advisory / unserved energy
  api/           # FastAPI service
  dashboard/     # Streamlit app
  storage/       # multi-tenant, multi-site persistence
  config.py      # typed settings (pydantic-settings), env-driven
tests/           # mirrors the package layout
```

## Quickstart

```bash
uv sync                 # core dependencies
uv run pytest           # run the test suite
```

Optional extras, installed only when needed:

```bash
uv sync --extra service     # FastAPI service
uv sync --extra dashboard   # Streamlit dashboard
uv sync --extra models      # xgboost + torch (LSTM, TIME-LLM)
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

See `VATICORE_BRIEF.md` for the full product context and `CLAUDE.md` for build
conventions.
