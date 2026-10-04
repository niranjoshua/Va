# The daily pipeline

Every morning, for every site, Vaticore checks the data, forecasts, plans, stores
everything, sends the plan on WhatsApp or email, and scores yesterday's plan
against what happened. This page explains what runs, why, and how to operate it. The
code is in `vaticore/pipeline/` and `vaticore/delivery/`.

## The daily loop

```
 04:40 UTC (05:40 Lagos)                                        06:00 Lagos
 ---------------------------------------------------------------------------
 ingest    pull new readings from each site's monitoring platform
           (docs/data-connectors.md); one failing source never stops
           the others
 score     each finished plan day: accuracy, range held, and litres,
           outages and cost had the plan been followed, against the
           baseline and a perfect-forecast bound
 run       for each site in the portfolio:
             1. read history up to the plan start (never after it)
             2. check data health: stale, missing, stuck meter, no grid
                record; if broken, tell the site there is no plan and why
             3. forecast net load with the right model, falling back if
                a model fails; run the other model in shadow
             4. start from the battery's measured charge if a reading is
                under 3 hours old, otherwise assume one and say so
             5. plan the generator on P90 net load, the grid on P10
                availability; plan the persistence baseline too
             6. store forecasts, plan and message
             7. send to consenting recipients on each channel they
                use (WhatsApp, email), once per channel
 webhook   all day: delivered and read receipts, replies (1, 2, STOP)
```

A plan covers 24 hours from the site's `plan_start_hour` (06:00 local by
default; set it per site in the portfolio TOML).

## Which model plans

From research note 3:

| Site history | Plans on | Runs in shadow |
|---|---|---|
| Under 8 weeks | Chronos-2, calibrated | Day-ahead GBM |
| 8 weeks or more | Day-ahead GBM, calibrated (the published planning model) | Chronos-2 |

Where the site has live weather, the **weather model** (the day-ahead GBM
with radiation, cloud cover and temperature as features) also runs in shadow.
Shadow forecasts are stored and scored like the primary's, so every pilot
compares the models on real data without anyone running a study.

If a model fails (missing package, too little history, an error), the run
falls back: primary calibrated, primary uncalibrated, the next model, then
persistence. A site always gets a plan if its data allows one, and the stored
run says why a fallback happened. Chronos-2 needs the `foundation` extra;
without it the GBM plans every site and the run records that.

## Model monitoring

Before each plan, every model's record at the site over the last 14 scored
days is checked (`vaticore/pipeline/monitoring.py`):

| Rule | Limit | Action |
|---|---|---|
| Range held (P10 to P90; target 80%) | under 65% or over 95% | suspend |
| Pinball loss against persistence, same days | more than 10% worse | suspend |
| Suspended model: range held and pinball | 70% to 90%, and no worse than persistence | reinstate |
| Challenger (weather model) against the default model | 5% lower pinball, range 70% to 90%, full window | promote to plan first |
| Fewer than 120 scored hours | | not judged yet |

A suspended model is skipped, so the plan falls down the chain, Chronos-2 to
the GBM to persistence, and a site never goes without a plan. Suspended
models keep running in shadow, so they are scored every day and come back on
their own once their record recovers. The gap between the suspend and
reinstate limits stops a model flapping in and out on noise. Persistence is
the floor and is never suspended.

Every change of status is stored (`model_status`), printed by `run` as an
`ALERT` line, logged as a warning, and emailed to `VATICORE_OPS_EMAIL` when
SMTP is set. The week-by-week record per model is in `monitor` and
`GET /sites/{op}/{site}/models`.

## Weather

With live weather on, each run fetches the site's weather from Open-Meteo:
the days since the last fetch (two days are fetched again, since recent hours
are revised) and the coming days. The weather is cached per site
(`site_weather`), so an outage at Open-Meteo leaves the history in place; the
weather model then runs only if the cache still covers the plan day, and the
run notes the failure either way. Plans never wait on weather.

The past hours come from Open-Meteo's forecast endpoint (its short-range
forecasts stitched together), not reanalysis, so the model trains on the same
kind of input it is given for the coming day. The weather model trains only
on hours that have weather, needs 14 days of them, and refuses to forecast an
hour without a weather forecast rather than guessing.

The weather forecast for each plan day is also kept as issued
(`weather_issued`). That archive is what an honest weather backtest needs
(research question 3), and it grows with every day the pipeline runs.

**Licence.** Open-Meteo's free API is for non-commercial use. Commercial use
needs an Open-Meteo API subscription: set `VATICORE_WEATHER_API_KEY`, and
requests go to the customer endpoints. Without a key, production runs skip
live weather and say so; local and staging runs use the free API. Turn
weather off with `VATICORE_WEATHER_PROVIDER=none` or `run --no-weather`.

## Data health

| Check | Warn | Fail (no plan) |
|---|---|---|
| Latest reading before the plan start | over 3 hours old | over 36 hours old |
| Missing load readings in the last week | over 10% | over 50% |
| History | | under 2 days |
| Load meter stuck on one value | 12 hours | |
| Solar site with no solar readings | solar treated as zero | |
| Weak-grid site with no grid on/off record | plan does not count on the grid | |

A warning goes into the message's note. A failure sends a short "no plan
today" message with the reason, because silence is worse than a clear "we
could not plan, check the data link".

A battery site with no charge reading under 3 hours old still gets a plan; the
starting charge is assumed and the message marks it "(assumed)".

This check decides whether to plan today. The fuller **site health report**
(`health` command, `GET /sites/{op}/{site}/health`) looks back over weeks for
the problems that quietly damage forecasts: wrong timezones, stuck meters,
gaps, spikes, solar at night, missing channels. See `docs/data-connectors.md`.

## What is stored

In the database set by `VATICORE_PLAN_STORE_URL` (default: the main
database). DuckDB for local use, Postgres or TimescaleDB in production.

| Table | One row per | Holds |
|---|---|---|
| `pipeline_runs` | site and plan day | status, model, fallbacks, health, message, expectations |
| `forecast_hours` | site, day, model, hour | P10, P50, P90 of net load (primary, shadow, baseline) |
| `plan_hours` | site, day, hour | generator and grid plan, expected battery, baseline plan |
| `plan_scores` | site and plan day | accuracy, range held, litres, outages and cost vs baseline and bound |
| `deliveries` | site, day, channel, person | sent, delivered, read or failed, with the provider's message id |
| `recipient_prefs` | person | opted out (STOP) or not |
| `feedback` | reply | followed, not followed, STOP, START or free text |
| `model_status` | site and model | suspended or reinstated, why, since when |
| `site_weather` | site and hour | latest weather (the weather model's cache) |
| `weather_issued` | site, day, hour | the weather forecast as it stood when the plan was made |

Re-running a day replaces that day's forecasts and plan. Once a plan has been
sent, a re-run resends the stored plan instead of making a new one, so a site
never receives two different plans for the same day (use `--force` to
re-plan deliberately).

Privacy: phone numbers and email addresses are never stored. The store keeps
a one-way hash (to match replies and STOP) and a masked form such as
`+234******0001` or `a******@bank.ng`. A STOP on any channel stops every
channel for that person.

## Scoring, and what the numbers mean

A day is scored once at least 75% of its hours have readings (or after three
days, on what exists). The plan is run against what actually happened, from
the same starting battery charge, and compared with:

- **the baseline:** the plan the same rules would make from persistence,
  "tomorrow looks like today", roughly what a site does without forecasts;
- **the bound:** the plan a perfect forecast would make.

These are values **had the plan been followed**. In a shadow pilot sites run
as usual, so this is the value on offer, not yet the value taken. Replies of 1
and 2 record which days were followed, so the two can be separated.

From the third scored day, the morning message quotes the last week's track
record, honestly in either direction ("used 40 L more diesel to avoid 25 kWh
of outages" is a valid result for a cautious plan).

## Commands

```bash
uv run python -m vaticore.pipeline demo                  # a week on synthetic data, nothing sent
uv run python -m vaticore.pipeline ingest                # pull new readings (sources file)
uv run python -m vaticore.pipeline health --days 30      # data health report per site
uv run python -m vaticore.pipeline run                   # plan every site, store, send nothing
uv run python -m vaticore.pipeline run --channel console # print the messages
uv run python -m vaticore.pipeline run --channel whatsapp --recipients /secure/recipients.toml
uv run python -m vaticore.pipeline run --channel whatsapp --channel email
uv run python -m vaticore.pipeline run --site OPERATOR/SITE --date 2026-10-06
uv run python -m vaticore.pipeline score                 # score finished days
uv run python -m vaticore.pipeline scorecard --days 30   # each site's track record
uv run python -m vaticore.pipeline monitor --weeks 8     # each model, week by week
uv run python -m vaticore.pipeline apikey create --operator OPERATOR --name "ops team"
uv run python -m vaticore.pipeline optout --email someone@example.com   # or --phone; --undo
uv run python -m vaticore.pipeline whatsapp-test --to +234...
uv run python -m vaticore.pipeline email-test --to someone@example.com
```

`run`, `ingest` and `health` exit with status 1 if any site failed, so the
scheduler can alert.

The API serves stored results. Every site endpoint takes a bearer token:
either the admin token (`VATICORE_API_TOKEN`, all operators) or an operator
key from `apikey create`, which reaches only that operator's sites (another
operator's site answers 403). Keys are stored hashed and shown once.

- `GET /sites/{operator_id}/{site_id}/plans/{date}`
- `GET /sites/{operator_id}/{site_id}/scorecard?days=30`
- `GET /sites/{operator_id}/{site_id}/health?days=30`
- `GET /sites/{operator_id}/{site_id}/models?weeks=8`: monitoring, per model
- `POST /ingest/{operator_id}/{site_id}`: push readings (docs/data-connectors.md)
- `GET` and `POST /webhooks/whatsapp` (Meta's webhook; signed)

In production, a request without a token is refused, and the demo data the
API seeds for local use is never loaded.

## Email

Email goes out over SMTP from any provider (Google Workspace, Microsoft 365,
Zoho, Amazon SES, Postmark): set `VATICORE_SMTP_HOST`, `VATICORE_SMTP_PORT`,
`VATICORE_SMTP_USERNAME`, `VATICORE_SMTP_PASSWORD` and `VATICORE_EMAIL_FROM`
(see `.env.example`), then `email-test --to you@...` to check. Each email has
a plain text and an HTML part and a List-Unsubscribe header. Give a recipient
an `email` in the recipients file (with or without `whatsapp`) and run with
`--channel email`.

Replies to an email reach the `VATICORE_EMAIL_REPLY_TO` inbox; they are not
read automatically yet. Apply an unsubscribe with `optout --email ...`, which
also stops WhatsApp to the same person if the recipients file links the two.
Temporary SMTP errors (4xx, network) are retried; permanent ones (5xx, a
refused address) are stored as failed at once.

## Running it in production

1. **Postgres.** The scheduled job and the API (which receives webhooks) are
   separate processes, so they must share a server database: set
   `VATICORE_DATABASE_URL` to Postgres or TimescaleDB on both.
2. **Readings.** Connect each site's monitoring platform with a sources file
   (`VATICORE_SOURCES_FILE`; see `docs/data-connectors.md`), or let the
   operator push readings to `POST /ingest/...` with their key. Include
   `grid_available` for weak-grid sites and `battery_soc_pct` wherever the
   site reports it.
3. **Portfolio and recipients** as secret files (`portfolio.toml`,
   `recipients.toml`), never in the repository.
4. **WhatsApp:** follow `docs/whatsapp-setup.md`.
5. **Email (optional):** the SMTP settings above; `VATICORE_OPS_EMAIL` for
   monitoring alerts.
6. **Weather:** an Open-Meteo API key (`VATICORE_WEATHER_API_KEY`) for
   commercial use; without it production plans run without live weather.
7. **Schedule:** `render.yaml` defines the job (`vaticore-daily-plans`,
   04:40 UTC daily: ingest, score, run). Any scheduler that runs the three
   commands works.

## Safety

- **Advisory only.** Messages say so, and nothing here controls equipment.
- **Consent first.** Only recipients with `consent = true` are messaged, and
  STOP always wins.
- **No silent failures.** Broken data produces a stated "no plan"; a failing
  site is stored with its error and the job's exit status flags it.
- **No look-ahead.** Plans use readings strictly before the plan start.

## Known limits, next

- Where a site reports no battery charge (or only an old one), the start is
  assumed at 50% of usable and the message says "(assumed)".
- Messages are in English. Pidgin, Hausa and Yoruba versions need approved
  templates in each language.
- Email replies are read by a person, not parsed.
- The weather model has no backtest yet: there were no archived forecasts
  for these sites. It earns its place in shadow, against the default model,
  on each site's own scored days, and is promoted only on that evidence.
- Next: local-day planning and fuel reconciliation.
