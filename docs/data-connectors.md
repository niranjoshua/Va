# Data connectors and the site health report

Forecasts are only as good as the readings behind them, and real operator data
is messy: exports in local time labelled as UTC, meters stuck on one value,
days missing, duplicate rows from overlapping exports, watts where kW were
expected. This page covers how readings get in, what is fixed on the way and
how each site's data is checked. The code is in `vaticore/ingestion/connectors/`
and `vaticore/pipeline/health.py`.

There are three ways in:

| Way in | For | How |
|---|---|---|
| CSV export | any platform that can export files (most can) | `connector = "csv"` with a column mapping |
| Victron VRM | sites with Victron inverters or battery monitors | `connector = "victron_vrm"` with an installation ID and a token |
| Push API | operators or integrators who send readings themselves | `POST /ingest/{operator_id}/{site_id}` with an operator key |

## What is stored

Every reading is scoped by `operator_id` and `site_id`, stored in UTC, and
validated before it is written.

| Column | Unit | Required | Used for |
|---|---|---|---|
| `load_kw` | kW | yes (may be missing per row) | the load forecast |
| `generation_kw` | kW | yes (missing or zero without solar) | the solar forecast |
| `grid_available` | 1 on, 0 off | no | grid forecast and plan on weak-grid sites |
| `battery_soc_pct` | percent, 0 to 100 | no | the plan's starting battery charge |
| `genset_kw` | kW | no | generator runtime, fuel reconciliation (next) |
| `fuel_level_l` | litres | no | fuel reconciliation (next) |

A batch that leaves a value missing never wipes a stored one. A battery-charge
feed from VRM can therefore run alongside load readings from another source
without erasing them.

## Sources file

Each site's source is declared in a TOML file kept outside the repository and
named by `VATICORE_SOURCES_FILE`. `examples/sites/sources.example.toml` shows
both connectors. Tokens never go in this file: each source names the
environment variable that holds its token.

```bash
uv run python -m vaticore.pipeline ingest                       # every source
uv run python -m vaticore.pipeline ingest --sources /secure/sources.toml
```

Each run reads from 6 hours before the last stored reading (platforms correct
late data) to now. A site with nothing stored yet is backfilled 60 days, or
`backfill_days` if the source sets it (use 365 to load an operator's past
year). One failing source never stops the others. Each prints what it read
and every fix it made, and the command exits with status 1 if any source
failed, so the scheduler can alert.

## CSV exports

Inverter, battery and tower monitoring platforms (Victron VRM, Huawei
FusionSolar, Deye and Solarman, SMA, tower remote monitoring systems) can
export CSV. The columns differ, so each source maps them:

```toml
[[source]]
operator_id = "example-towerco"
site_id = "lag-ikd-0142"
connector = "csv"
path = "/data/exports/lag-ikd-0142/*.csv"   # a file or a glob
timezone = "Africa/Lagos"                   # the clock the timestamps are in

[source.columns]                            # internal name = header in the file
timestamp = "Time"
load_kw = "Load Power(W)"
battery_soc_pct = "SOC(%)"
grid_available = "Grid Status"

[source.scale]                              # multiply after reading
load_kw = 0.001                             # W to kW

[source.grid]                               # text states, or a voltage:
on = ["On", "Connected"]                    # min_voltage = 150
off = ["Off", "Disconnected"]
```

What is handled, and reported in the run's notes:

| Problem | What happens |
|---|---|
| Timestamps with no timezone | Read in the source's `timezone`; refused if none is set, because guessing is how a whole site's solar ends up an hour out |
| Clock change hours (where a timezone has them) | Ambiguous or missing local times are dropped, not guessed |
| Unreadable timestamps | Row dropped, counted |
| Duplicate timestamps (overlapping exports) | Last row kept, counted |
| Text in number columns (`n/a`, `--`) | Left missing, counted |
| Battery charge above 100% | Capped at 100, counted |
| Negative power readings | Left missing, counted |
| Grid state as text or voltage | Mapped to 1 on, 0 off; anything unrecognised is missing, never guessed |
| Rows outside the sync window | Skipped, counted, with a pointer to `backfill_days` |

Extra arguments for `pandas.read_csv` (a semicolon separator, a header row
further down) go under `[source.read_csv]`.

## Victron VRM

Victron systems are common in Nigerian solar and hybrid installations. The
connector reads the battery's state of charge (VRM attribute code `bs`) from
the VRM API's stats endpoint, hour by hour, a week per request, retrying on
rate limits and server errors.

1. In VRM, open Preferences, Integrations, Access tokens, and create a token
   for an account that can see the installation.
2. Put it in the environment variable named by `token_env` (default
   `VATICORE_VRM_TOKEN`). Never in the sources file.
3. Find the installation ID in the installation's VRM URL and set
   `installation_id`.

Codes for load, solar and generator power differ between installations, so
they are not guessed. Check them in VRM, then map them under
`[source.attributes]` with a `[source.scale]` to convert watts to kW. A token
that VRM rejects stops that source with a clear message; the other sources
still sync.

## Push API

Operators and integrators can send readings directly, up to 10,000 per
request:

```bash
curl -X POST https://api.example/ingest/example-towerco/lag-ikd-0142 \
  -H "Authorization: Bearer vk_..." -H "Content-Type: application/json" \
  -d '{"timezone": "Africa/Lagos",
       "readings": [{"timestamp": "2026-10-06 06:00", "load_kw": 3.2, "battery_soc_pct": 64}]}'
```

- Timestamps need a UTC offset (`2026-10-06T06:00:00+01:00`), or a
  `timezone` for the whole batch. Without either, the request is refused.
- Duplicate timestamps in a batch, values out of range (a negative load, a
  battery charge above 100) and unknown timezones are refused with a 422 that
  says why. Nothing in a refused batch is stored.
- Sending a reading again replaces it; leaving a column out keeps what is
  stored.

### Operator keys

Each operator gets its own key, which reaches only that operator's sites:

```bash
uv run python -m vaticore.pipeline apikey create --operator example-towerco --name "RMS feed"
uv run python -m vaticore.pipeline apikey list
uv run python -m vaticore.pipeline apikey revoke --key-id 1a2b3c4d
```

A key is shown once, when it is created, and stored only as a hash. A key
used on another operator's site gets 403; a revoked or unknown key gets 401.
The admin token (`VATICORE_API_TOKEN`) reaches every operator and is for
Vaticore's own use.

## The site health report

```bash
uv run python -m vaticore.pipeline health --days 30
```

or `GET /sites/{operator_id}/{site_id}/health?days=30` with a key. The report
gives the data's resolution, how much of each channel is present, the days
with the least data and the longest gap, then lists every problem found:

| Code | Means | Severity |
|---|---|---|
| `no_data` | no load readings in the period | fail |
| `missing` | load covers under 90% of hours | warn |
| `stuck_meter` | load unchanged for 12 hours or more | warn |
| `negative_load` | negative load readings | warn |
| `spikes` | readings over three times the usual peak | warn |
| `timezone` | solar peaks far from local solar noon: timestamps are off by the hours stated | warn |
| `solar_at_night` | solar output at night | warn |
| `no_solar` | solar readings all zero | warn |
| `no_grid_record` | weak-grid site with grid on/off for under half the hours | warn |
| `no_battery_charge` | battery site with no charge readings | warn |

The timezone check is the one that saves the most pain. It finds the hour of
peak solar from the data and compares it with when the sun is highest at the
site's longitude. Local time exported as UTC shows up as solar "1 h late" in
Lagos, which would otherwise shift every solar forecast and generator window
by an hour without anyone noticing.

Run the report on an operator's data before the first plan goes out, and
weekly after that. The daily run has its own lighter check that decides
whether to plan at all (`docs/pipeline.md`).

## Adding a connector

A connector is a class with a `name` and
`fetch(source, start, end) -> FetchResult`, returning readings in the internal
schema (UTC timestamps, kW) and a note for every fix it made. Register it in
`default_connectors()` and test it against recorded responses, as
`tests/test_connectors.py` does for VRM. Huawei FusionSolar and Deye/Solarman
APIs are the likeliest next ones; until then their CSV exports work through
the CSV connector.
