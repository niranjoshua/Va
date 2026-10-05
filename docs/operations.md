# Running Vaticore in production

How the service is deployed, checked, backed up and kept separate per
operator. `DEPLOY.md` is the first-time setup; this page is how it runs.

## Two environments

`render.yaml` defines both, from one Docker image:

| | Staging | Production |
|---|---|---|
| Deploys | every push to `main`, automatically | only when you press Deploy in Render |
| Database | its own Postgres | its own Postgres |
| Daily plans | made and stored, never sent | made, stored and sent (WhatsApp, email) |
| Services | `vaticore-api-staging`, `vaticore-dashboard-staging`, `vaticore-daily-plans-staging` | `vaticore-api`, `vaticore-dashboard`, `vaticore-daily-plans`, `vaticore-weekly-summary`, `vaticore-restore-drill` |

A change reaches production like this: merge to `main` (CI green), watch
staging deploy and run its next daily plans, then deploy production by hand.
Staging uses real-shaped data (a copy of a pilot's readings, or the example
portfolio) and never messages anyone.

## Operators' data kept separate

Every reading, plan, score, delivery and fuel record carries `operator_id` and
`site_id`. Access goes through one rule (`vaticore/access.py`):

- **Operator keys** (`python -m vaticore.pipeline apikey create --operator X`)
  reach only operator X's sites, in the API and the dashboard. A key is shown
  once and stored only as a hash; revoke it with `apikey revoke`.
- **The admin token** (`VATICORE_API_TOKEN`) reaches every operator. It is
  Vaticore's own, never given to an operator.
- Staging and production refuse every request without a key. Only local
  development, with no admin token set, is open, so a demo works out of the box.

The dashboard asks for a key, lists only that operator's sites, and slows down
guessing (five wrong keys lock the form for a minute). Signing in with a single
sign-on provider (Google or Microsoft accounts) is the next step for the
dashboard, once operators ask for it; keys work today and are what the API uses.

## Database migrations

The stores create their tables when they first open. Every change after that
is a numbered migration in `vaticore/storage/migrations.py`, recorded in
`schema_migrations`:

```bash
uv run python -m vaticore.pipeline migrate --status   # what is applied and pending
uv run python -m vaticore.pipeline migrate            # apply what is pending
```

Render runs `migrate` before each API deploy goes live (`preDeployCommand`)
and at the start of the daily job. `/ready` answers 503 while a migration is
pending, so traffic never reaches code whose schema is not there yet. Code
refuses to run on a database migrated by a newer release. To add a migration:
append (never edit) a numbered entry, safe to run while the old code is
serving.

## Logs, errors and uptime

| What | How | Set up |
|---|---|---|
| Logs | One JSON object per line (`VATICORE_LOG_FORMAT=json`): time, level, logger, message; each API request with its status and duration | On in `render.yaml`; Render keeps and searches them, or stream them to Better Stack or Datadog |
| Errors | Unhandled errors in the API, dashboard and pipeline go to Sentry, tagged with environment and release; no request bodies or personal data | Create a Sentry project, set `VATICORE_SENTRY_DSN` |
| API uptime | `GET /ready`: the database answers and the schema is current (`/health` only says the process is up) | Point an uptime monitor (Better Stack, UptimeRobot) at `https://<api>/ready`, every minute |
| Daily job | The job pings `VATICORE_HEARTBEAT_URL` when it finishes (with `/fail` if a site failed); the monitor alerts if no ping arrives | A Healthchecks.io or Better Stack heartbeat, expected daily at 17:00 UTC, with an hour's grace |
| Model drift | Alerts from monitoring (`docs/pipeline.md`) | `VATICORE_OPS_EMAIL` with SMTP |

A heartbeat matters as much as error tracking: a job that never starts raises
no error.

## Backups, tested by restoring

Render's managed Postgres takes the backups (daily, with point-in-time
recovery on paid plans). Vaticore proves they would restore:

```bash
uv run python -m vaticore.pipeline backup --out /backups --keep 14 --verify
uv run python -m vaticore.pipeline restore-check --dump /backups/vaticore-<time>.dump
```

`backup` dumps the database with `pg_dump` and writes a manifest beside it:
each table's row count, read in the same snapshot as the dump, and the file's
checksum. `--verify` restores the dump into the scratch database
(`VATICORE_RESTORE_TEST_URL`) and checks that every table came back with the
same number of rows. The `vaticore-restore-drill` job does this every Monday
against production and fails loudly if a restore would not work.

The scratch database must be a separate, empty database the first time. It is
then marked as Vaticore's restore scratch, and only a marked database is ever
wiped, so pointing it at a real database by mistake is refused. For copies
kept off Render, run `backup` from a machine with storage, or copy the dumps
to object storage.

## The Docker image

One image serves the API, the dashboard and the pipeline. It carries the
Postgres client tools for backups and runs as an unprivileged user.

```bash
docker build -t vaticore .
docker build -t vaticore-fm --build-arg VATICORE_WITH_FOUNDATION=1 .   # plus Chronos-2
```

The foundation build installs CPU-only PyTorch (not PyPI's Linux build, which
brings several gigabytes of GPU libraries) and every other package at its
locked version, then downloads the Chronos-2 weights into the image. At run
time it never contacts Hugging Face (`HF_HUB_OFFLINE`), so a plan never waits
on a download or fails because one did. Docker reports the base image at
about 2.3 GB and the foundation image at about 4.1 GB, uncompressed. The daily
jobs use it; the API and dashboard use the lighter image.

Memory: one Chronos-2 forecast peaked at 1.3 GB in testing (with a PyTorch
build that includes GPU libraries; the image's CPU build needs less). The
daily jobs therefore run on Render's `standard` instance (2 GB), not the
default `starter` (512 MB), which would kill them partway through.
`tests/test_operations.py` fails if a job that loads the foundation models is
given a smaller instance. CI builds and starts the image on every pull
request, and builds the foundation image (and forecasts with Chronos-2 with no
network) whenever its inputs change.

## What needs your accounts

| Service | For | Where it goes |
|---|---|---|
| Render | Hosting both environments, managed Postgres | Blueprint from `render.yaml` |
| Sentry | Error tracking | `VATICORE_SENTRY_DSN` |
| Better Stack or UptimeRobot | Uptime of `/ready` | The monitor's own settings |
| Healthchecks.io or Better Stack | The daily job's heartbeat | `VATICORE_HEARTBEAT_URL` |
| Meta (WhatsApp) | Plans on WhatsApp | `docs/whatsapp-setup.md` |
| An SMTP provider | Email plans and alerts | `VATICORE_SMTP_*`, `VATICORE_EMAIL_FROM`, `VATICORE_OPS_EMAIL` |
| Open-Meteo API subscription | Live weather, commercial use | `VATICORE_WEATHER_API_KEY` |
