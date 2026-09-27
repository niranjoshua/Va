# Vaticore value study

Site: `site-A-600kWh`, built from `energy_dataset.csv`.
Period (UTC): 2017-01-01 00:00:00+00:00 to 2018-12-31 23:00:00+00:00 (17520 hours). Scored days: 365.

## Site assumptions

- Demand scaled to a 120 kW peak (2034 kWh on an average day).
- Solar: real output shape on a 200 kWp array (1188 kWh on an average day).
- Battery 600 kWh usable, 150 kW, floor 60 kWh, efficiency 0.95 each way.
- Generator 100 kW, minimum load 30.0%, HOMER default fuel curve (0.08145 L/h per kW rated + 0.246 L/kWh).
- Diesel 1.10 per litre; an unserved kWh priced at 1.00 (see the sensitivity table).
- Missing hours: load 5, solar 2.

## Calibration: does the range mean what it says?

| Forecast | P10-P90 holds (target 80%) | Mean width (kW) | Below P10 (target 10%) | Below P90 (target 90%) |
|---|---|---|---|---|
| persistence | 79.7% | 55.1 | 10.5% | 90.2% |
| quantile_gbm_day_ahead | 71.0% | 44.8 | 13.2% | 84.1% |
| quantile_gbm_day_ahead+conformal | 80.7% | 52.0 | 8.5% | 89.1% |

## Value: litres, outages and money

Each policy plans the generator day ahead from its forecast, then the day is run on what actually happened. The baseline plans on persistence (tomorrow looks like yesterday). The perfect forecast row uses the same planning rule on the actual outcome: it bounds what any forecast can achieve here.

| Policy | Diesel (L) | Generator hours | Unserved (kWh) | Hours with outages | Total cost | Saved vs baseline | Share of possible gain |
|---|---|---|---|---|---|---|---|
| persistence P50 | 110,667 | 4,397 | 44,119 | 970 | 165,852 | 0 | 0.0% |
| persistence P90 | 125,987 | 5,064 | 14,490 | 338 | 153,075 | 12,777 | 47.5% |
| quantile_gbm_day_ahead P50 | 109,987 | 4,336 | 45,517 | 1,022 | 166,502 | -650 | -2.4% |
| quantile_gbm_day_ahead P90 | 127,276 | 5,157 | 8,960 | 317 | 148,964 | 16,889 | 62.8% |
| quantile_gbm_day_ahead+conformal P90 | 128,647 | 5,219 | 7,844 | 276 | 149,356 | 16,496 | 61.3% |
| perfect forecast | 125,790 | 4,982 | 580 | 112 | 138,949 | 26,904 | 100.0% |

## Sensitivity: savings against the baseline by cost of an unserved kWh

| Policy | 0.25 per kWh | 0.5 per kWh | 1 per kWh | 2 per kWh | 5 per kWh |
|---|---|---|---|---|---|
| persistence P50 | 0 | 0 | 0 | 0 | 0 |
| persistence P90 | -9,444 | -2,037 | 12,777 | 42,406 | 131,292 |
| quantile_gbm_day_ahead P50 | 399 | 49 | -650 | -2,048 | -6,243 |
| quantile_gbm_day_ahead P90 | -9,480 | -690 | 16,889 | 52,047 | 157,521 |
| quantile_gbm_day_ahead+conformal P90 | -10,709 | -1,641 | 16,496 | 52,770 | 161,592 |
| perfect forecast | -5,750 | 5,135 | 26,904 | 70,442 | 201,058 |

## Method

- Rolling origin, 24 hours ahead, one forecast per day at 00:00 UTC, trained only on data before each origin. Net load (demand minus solar) is forecast directly.
- Conformal calibration (CQR) adjusts each day's P10-P90 using only the misses of the previous 28 days.
- Every policy starts with the battery half full and carries its own battery state from day to day.
- Runtime 8.5 minutes.
