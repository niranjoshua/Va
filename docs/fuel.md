# Fuel reconciliation

Diesel goes missing on distributed sites: deliveries short of what was billed,
fuel siphoned from the tank at night, runtime logged but never run. Vaticore
puts three records side by side and flags where they disagree by more than
the measurements can explain. The code is in `vaticore/fuel/`.

| Record | Where it comes from |
|---|---|
| **Burned (expected)** | The generator's output (`genset_kw`) through the site's fuel curve, the same one the planner uses: litres per hour = a × rated kW + b × output kW |
| **Tank level** | `fuel_level_l`, where a fuel sensor reports it (most tower RMS systems and many generator controllers) |
| **Deliveries** | What the supplier says was delivered, recorded from delivery notes or invoices |

## What it flags

| Flag | Means | Severity |
|---|---|---|
| Short delivery | The tank rose by clearly less than a recorded delivery (more than 10% and 20 L) | alert |
| Fall while the generator was off | The tank fell by 10 L or more over a spell when the generator was off: the strongest sign of siphoning or a leak | alert |
| Deliveries exceed use | No tank sensor: more delivered than the generator could have burned plus what the tank holds | alert |
| Unexplained loss | Over a local day, more fuel left the tank than the generator should have burned (beyond 15% and 15 L) | warn |
| Delivery not seen | A delivery recorded with no matching rise in the tank within 12 hours | warn |
| Refill not recorded | The tank rose with no delivery recorded: record it so the fuel is accounted for | info |

Each flag gives the litres, their value at the site's diesel price, and the
time. Days are the site's local days.

These are flags for someone to check, not proof of theft. Fuel curves are
typical rather than measured for each generator (allow 10 to 15%), sensors
drift, and deliveries are sometimes recorded late. The thresholds are set so a
flag is worth a phone call, and every flag says what was compared. Readings
of the tank level are compared as net changes, so ordinary sensor noise
cancels instead of adding up to a false loss.

## What a site needs

- **Best:** generator output and a tank level sensor, plus deliveries. Every
  flag is available.
- **No tank sensor:** generator output, deliveries and the tank's size
  (`tank_l` on the generator in the portfolio TOML). Only "deliveries exceed
  use" can be checked, over the whole period.
- **No generator output:** burn cannot be estimated; the report says so and
  claims nothing.

Generator output and tank level come in like any other reading
(`docs/data-connectors.md`): map `genset_kw` and `fuel_level_l` in a CSV
source, or push them to the ingest API.

## Recording deliveries

```bash
uv run python -m vaticore.pipeline fuel add --site example-towerco/lag-ikd-0142 \
    --litres 500 --at "2026-10-06 10:30" --timezone Africa/Lagos --reference INV-1042
uv run python -m vaticore.pipeline fuel import --csv deliveries.csv
```

The CSV has `operator_id`, `site_id`, `delivered_at`, `litres` and optionally
`reference`; times need a UTC offset or `--timezone`. Recording a delivery at
the same time again corrects it. Operators can also post deliveries with their
API key:

```
POST /sites/{operator_id}/{site_id}/fuel/deliveries
{"deliveries": [{"delivered_at": "2026-10-06T10:30+01:00", "litres": 500, "reference": "INV-1042"}]}
```

## The report

```bash
uv run python -m vaticore.pipeline fuel report --days 30
```

or `GET /sites/{operator_id}/{site_id}/fuel?days=30`. For each site with a
generator: litres delivered, the expected burn (and for how many hours
generator output was recorded), the fuel that left the tank, a day-by-day
table, and the flags. The command exits with status 1 if any site has an
alert, so a weekly scheduled run can notify someone.

## Limits

- The fuel curve is the generic one unless the site's record gives the
  generator's own (`fuel_intercept_l_per_kw_h`, `fuel_slope_l_per_kwh`). A
  measured curve, from a few weeks with a good tank sensor, tightens every
  check.
- Output averaged over an hour slightly understates burn for a generator that
  ran only part of the hour.
- Runtime counters without output, and fuel cards, are not read yet.
