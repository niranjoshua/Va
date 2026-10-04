# Deploying Vaticore

Three ways to run the service, from fastest to most production ready.

## 1. Local, no Docker

```bash
uv sync --extra service
uv run uvicorn vaticore.api.main:app --reload
# API on http://localhost:8000, interactive docs at http://localhost:8000/docs
```

Dashboard:

```bash
uv sync --extra dashboard
uv run streamlit run vaticore/dashboard/app.py
```

## 2. Local, full stack with Docker

Brings up the API and a TimescaleDB database together:

```bash
docker compose up --build
```

## 3. Cloud (Render)

The repo ships a `render.yaml` blueprint and a `Dockerfile`.

1. Push this repo to GitHub.
2. In Render: **New > Blueprint**, select the repo. Render reads `render.yaml`.
3. Set secrets in the Render dashboard (never commit them):
   - `VATICORE_DATABASE_URL` (a managed Postgres/Timescale connection string)
   - `ANTHROPIC_API_KEY` (only if you want the LLM copilot; it degrades to a
     template without one)
4. Deploy. Render health checks `GET /health`.

The same `Dockerfile` runs on Railway, Fly.io, Google Cloud Run, or any
container host. The container honours the platform provided `PORT`.

## Environment variables

All configuration is environment driven (see `.env.example`). Never commit real
secrets or operator data.

| Variable | Purpose | Default |
| --- | --- | --- |
| `VATICORE_ENVIRONMENT` | local, staging or production | local |
| `VATICORE_DATABASE_URL` | data store connection | local DuckDB |
| `ANTHROPIC_API_KEY` | enables the LLM copilot | unset (template fallback) |
| `VATICORE_WEATHER_PROVIDER` | weather source | open-meteo |

## What is still stubbed

The API's data source is synthetic demo data today (`get_fleet`). Wire the
`storage/` repository to your database and replace that dependency to serve real
operator data. Nothing else about the handlers changes.

## The daily pipeline

`render.yaml` also defines `vaticore-daily-plans`, a scheduled job that runs
at 04:40 UTC (05:40 in Lagos): it scores finished plan days, then plans every
site and sends the plans on WhatsApp. It needs, in the Render dashboard:

- `VATICORE_DATABASE_URL`: the same Postgres as the web service.
- `VATICORE_WHATSAPP_TOKEN` and `VATICORE_WHATSAPP_PHONE_NUMBER_ID`.
- Secret files `portfolio.toml` (the operator's sites) and `recipients.toml`
  (who receives plans, with consent).

The web service needs `VATICORE_WHATSAPP_APP_SECRET`,
`VATICORE_WHATSAPP_VERIFY_TOKEN` (for the webhook) and `VATICORE_API_TOKEN`
(for the plan and scorecard endpoints). Step by step: `docs/whatsapp-setup.md`
and `docs/pipeline.md`.
