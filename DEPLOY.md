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
2. In Render, create two Postgres databases (staging and production) and a
   third, small, empty one for restore checks.
3. **New > Blueprint**, select the repo. Render reads `render.yaml`: a staging
   environment that deploys every push to `main`, and a production one that
   deploys when you press Deploy (`docs/operations.md`).
4. Set secrets in the Render dashboard (never commit them), per service:
   - `VATICORE_DATABASE_URL`: that environment's Postgres connection string;
   - `VATICORE_API_TOKEN`: the admin token (a long random string);
   - `VATICORE_RESTORE_TEST_URL` on the restore drill: the empty database;
   - optional: `VATICORE_SENTRY_DSN`, `VATICORE_HEARTBEAT_URL`,
     `ANTHROPIC_API_KEY` (the LLM copilot degrades to a template without one).
5. Deploy. Each API deploy first applies database migrations; Render then
   checks `GET /ready` (database reachable, schema current).
6. Give each operator a key: `python -m vaticore.pipeline apikey create
   --operator <id> --name <who>` (run in the API's Render shell).

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
| `VATICORE_API_TOKEN` | admin token; required outside local development | unset |
| `VATICORE_LOG_FORMAT` | text, or json for hosted log search | text |
| `VATICORE_SENTRY_DSN` | error tracking | unset |
| `VATICORE_HEARTBEAT_URL` | daily job heartbeat | unset |
| `VATICORE_RESTORE_TEST_URL` | scratch database for restore checks | unset |

## Demo data

Locally, the API's forecast and advisory endpoints and the dashboard use
synthetic demo data, so a fresh checkout works out of the box. In staging and
production the dashboard and the site endpoints read real readings from the
database, and demo data is never seeded.

Running it day to day (migrations, logs, uptime, backups and restore checks,
the image with the foundation models): `docs/operations.md`.

## The daily pipeline

`render.yaml` also defines `vaticore-daily-plans`, a scheduled job that runs
at 17:00 UTC (18:00 in Lagos): it pulls new readings, scores finished plan
days, then plans each site's next local day (midnight to midnight) and sends
the plans on WhatsApp the evening before. It needs, in the Render dashboard:

- `VATICORE_DATABASE_URL`: the same Postgres as the web service.
- `VATICORE_WHATSAPP_TOKEN` and `VATICORE_WHATSAPP_PHONE_NUMBER_ID`.
- Secret files `portfolio.toml` (the operator's sites) and `recipients.toml`
  (who receives plans, with consent).

The web service needs `VATICORE_WHATSAPP_APP_SECRET`,
`VATICORE_WHATSAPP_VERIFY_TOKEN` (for the webhook) and `VATICORE_API_TOKEN`
(for the plan and scorecard endpoints). Step by step: `docs/whatsapp-setup.md`
and `docs/pipeline.md`.
