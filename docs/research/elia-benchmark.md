# Vaticore against a grid operator's own forecasts

Vaticore research note 2. Belgian load and solar from Elia, the national
transmission system operator. Elia publishes its day-ahead P10, P50 and P90
beside the measured values, so Vaticore can be scored against a professional
forecast, not only against persistence.

## Answer

1. **Load: parity with Elia, with a better calibrated range, and no weather.**
   Over 453 held-out days (January 2025 to March 2026), Vaticore's day-ahead
   load forecast matched Elia's on pinball loss. It was slightly better on
   MAE, and its range held 80.4% of outcomes against Elia's 74.9%. Vaticore
   used only calendar features and load lags known at midnight. Elia uses
   weather forecasts and operator knowledge.
2. **Solar: weather is the gap.** Without weather, Vaticore beat persistence
   by 23% on solar. Elia's weather-driven forecast beat it by 68%. Weather is
   now the top research priority.
3. **Value: a calibrated range beats a better point forecast.** On a solar
   mini-grid shaped from real Belgian demand and solar, planning on Vaticore's
   calibrated P90 captured 85% of what a perfect forecast could save. Planning
   on Elia's professional medians captured 84%. Vaticore's plan left about half
   as much energy unserved. This replicates note 1's finding on independent
   data.

## Load, day ahead (Belgium, national)

Trained from January 2022. Every hour of 453 days from 1 January 2025 is
scored, one forecast per day at 00:00 UTC.

| Forecast | Pinball (MW) | vs persistence | MAE (MW) | P10-P90 held (target 80%) | Below P90 (target 90%) |
|---|---|---|---|---|---|
| Persistence | 178.4 | | 510.0 | 78.8% | 89.5% |
| Vaticore, day-ahead model | 96.2 | 46.1% better | 287.8 | 69.2% | 83.4% |
| **Vaticore, calibrated** | **95.2** | **46.7% better** | **287.8** | **80.4%** | **89.6%** |
| Elia day-ahead (6 PM) | 95.2 | 46.6% better | 292.4 | 74.9% | 83.6% |

Two things stand out:

- **Calibration closes the gap to Elia.** Calibration took Vaticore from just
  behind Elia to level with it, and gave the most honest range in the table.
- **Elia's range is too narrow.** Its P90 was exceeded on 16.4% of hours
  instead of 10%, and its median sat below the outcome 59% of the time.

## Solar, day ahead (Belgium, national)

Trained on the first 120 days of the solar series (from July 2025). 275 days
are scored.

| Forecast | Pinball (MW) | vs persistence | MAE (MW) | P10-P90 held (target 80%) |
|---|---|---|---|---|
| Persistence | 178.8 | | 429.4 | 79.5% |
| Vaticore, no weather | 138.5 | 22.5% better | 422.0 | 78.3% |
| Vaticore, no weather, calibrated | 138.3 | 22.7% better | 422.0 | 80.8% |
| Elia day-ahead (11 AM), weather driven | 57.7 | 67.7% better | 172.0 | 85.8% |

Tomorrow's sun cannot be read from yesterday's output. A calibrated range stays
honest (80.8%), but it is wide. A weather-driven forecast narrows it. Closing
this gap is the next study.

## Value on a Belgian-shaped solar mini-grid

This uses real Belgian demand and solar for 22 July 2025 to 28 March 2026:

- Demand scaled to a 120 kW peak; solar to a 200 kWp array.
- 600 kWh battery and a 100 kW generator, with prices as in note 1.
- The first 120 days train. 130 days are scored, mostly autumn and winter,
  when solar is weakest.

On net load, the raw day-ahead model's range held 56.9% of outcomes.
Calibration brought it to 80.2%.

| Policy | Diesel (L) | Unserved (kWh) | Outage hours | Total cost | Share of possible gain |
|---|---|---|---|---|---|
| Persistence P50 (baseline) | 74,670 | 6,218 | 250 | 88,356 | 0% |
| Persistence P90 | 76,072 | 2,675 | 180 | 86,354 | 56% |
| Elia medians (load P50 minus solar P50) | 76,306 | 1,400 | 164 | 85,336 | 84% |
| Vaticore P50 | 75,622 | 3,638 | 193 | 86,823 | 43% |
| Vaticore P90 | 76,814 | 786 | 106 | 85,282 | 85% |
| **Vaticore calibrated P90** | 76,927 | **669** | **102** | 85,289 | **85%** |
| Perfect forecast | 76,586 | 509 | 120 | 84,754 | 100% |

Against the baseline, Vaticore's calibrated P90 plan gave:

- 89% less unserved energy;
- 59% fewer outage hours;
- 3.5% lower total cost;
- for 3% more diesel.

Against Elia's medians it left 52% less energy unserved, with 38% fewer outage
hours, at the same total cost. Elia publishes separate load and solar
quantiles, and quantiles do not subtract, so only Elia's medians can form a
net load plan. That is exactly the limitation a calibrated net load range
removes.

## Method

- **Data.** Elia open data:
  - total load (ods001), 15 minute, January 2015 to April 2026, with Elia's
    day-ahead (6 PM) and most recent P10, P50 and P90;
  - solar by region, 15 minute, July 2025 to August 2026, with Elia's
    day-ahead (11 AM) P10, P50 and P90.
- **Resampling.** Hourly values are means of the four 15 minute values, when
  at least three are present. Elia's hourly quantiles are means of its 15
  minute quantiles. Errors within an hour are strongly correlated, so this is a
  close approximation that errs towards a slightly wider band, which favours
  Elia on coverage.
- **Vaticore's forecasts.**
  - `quantile_gbm_day_ahead`: calendar features plus the value 24, 48, 72 and
    168 hours earlier. No weather.
  - Refit every day on everything before the origin.
  - Conformal calibration from the previous 28 days only.
- **Timing.**
  - Elia's day-ahead load forecast is issued at 18:00 Belgian time the day
    before. Vaticore's is issued at 00:00 UTC.
  - Vaticore's features use no value less than 24 hours old. Elia's newest
    data is at most about 8 hours old at issue. So Vaticore's information is
    no fresher than Elia's for any hour, and Elia also has weather.
- **Held-out periods.**
  - The load test period (2025 onward) was not used for any design choice.
  - The feature set and calibration window were fixed in note 1, on other data.
- **Value study.** The same site model, planning rule and perfect-forecast
  bound as note 1.

## Limitations

- Belgian national load and solar are far smoother than one site's. Single
  sites will have larger errors, which makes an honest range more valuable,
  not less.
- The value study covers 130 days, mostly winter. Treat its percentages as
  indicative and the load benchmark (453 days) as the headline.
- Vaticore's load parity with Elia is at national scale. A site-level
  benchmark needs site data, which is what pilots provide.

## Reproduce

Convert Elia's ods001 export to a semicolon CSV if it arrives in another
format. Then run:

```bash
uv run python examples/elia_study.py load  --load-csv ods001.csv --out results/
uv run python examples/elia_study.py solar --solar-csv solar.csv.gz --out results/
uv run python examples/elia_study.py value --load-csv ods001.csv \
    --solar-csv solar.csv.gz --out results/
```

Full outputs are in [`results/`](results/): `elia_load`, `elia_solar` and
`elia_value`, as markdown and JSON.
