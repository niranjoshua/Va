# Vaticore value study

Site: `site-B-300kWh`, built from `energy_dataset.csv`.
Period (UTC): 2017-01-01 00:00:00+00:00 to 2018-12-31 23:00:00+00:00 (17520 hours). Scored days: 365.

## Site assumptions

- Demand scaled to a 120 kW peak (2034 kWh on an average day).
- Solar: real output shape on a 160 kWp array (951 kWh on an average day).
- Battery 300 kWh usable, 100 kW, floor 30 kWh, efficiency 0.95 each way.
- Generator 100 kW, minimum load 30.0%, HOMER default fuel curve (0.08145 L/h per kW rated + 0.246 L/kWh).
- Diesel 1.10 per litre; an unserved kWh priced at 1.00 (see the sensitivity table).
- Missing hours: load 5, solar 2.

## Calibration: does the range mean what it says?

| Forecast | P10-P90 holds (target 80%) | Mean width (kW) | Below P10 (target 10%) | Below P90 (target 90%) |
|---|---|---|---|---|
| persistence | 79.5% | 47.5 | 10.6% | 90.1% |
| quantile_gbm_day_ahead | 70.8% | 36.9 | 13.1% | 84.0% |
| quantile_gbm_day_ahead+conformal | 80.7% | 43.4 | 8.2% | 88.9% |

## Value: litres, outages and money

Each policy plans the generator day ahead from its forecast, then the day is run on what actually happened. The baseline plans on persistence (tomorrow looks like yesterday). The perfect forecast row uses the same planning rule on the actual outcome: it bounds what any forecast can achieve here.

| Policy | Diesel (L) | Generator hours | Unserved (kWh) | Hours with outages | Total cost | Saved vs baseline | Share of possible gain |
|---|---|---|---|---|---|---|---|
| persistence P50 | 142,314 | 5,721 | 35,840 | 952 | 192,386 | 0 | 0.0% |
| persistence P90 | 154,215 | 6,266 | 11,859 | 350 | 181,496 | 10,890 | 50.4% |
| quantile_gbm_day_ahead P50 | 143,138 | 5,733 | 33,201 | 912 | 190,653 | 1,733 | 8.0% |
| quantile_gbm_day_ahead P90 | 156,625 | 6,419 | 6,541 | 290 | 178,828 | 13,558 | 62.8% |
| quantile_gbm_day_ahead+conformal P90 | 157,431 | 6,462 | 5,624 | 265 | 178,798 | 13,588 | 62.9% |
| perfect forecast | 154,500 | 6,196 | 841 | 145 | 170,790 | 21,595 | 100.0% |

## Sensitivity: savings against the baseline by cost of an unserved kWh

| Policy | 0.25 per kWh | 0.5 per kWh | 1 per kWh | 2 per kWh | 5 per kWh |
|---|---|---|---|---|---|
| persistence P50 | 0 | 0 | 0 | 0 | 0 |
| persistence P90 | -7,096 | -1,101 | 10,890 | 34,871 | 106,813 |
| quantile_gbm_day_ahead P50 | -246 | 413 | 1,733 | 4,371 | 12,286 |
| quantile_gbm_day_ahead P90 | -8,416 | -1,092 | 13,558 | 42,857 | 130,755 |
| quantile_gbm_day_ahead+conformal P90 | -9,074 | -1,520 | 13,588 | 43,804 | 134,452 |
| perfect forecast | -4,654 | 4,096 | 21,595 | 56,595 | 161,593 |

## Method

- Rolling origin, 24 hours ahead, one forecast per day at 00:00 UTC, trained only on data before each origin. Net load (demand minus solar) is forecast directly.
- Conformal calibration (CQR) adjusts each day's P10-P90 using only the misses of the previous 28 days.
- Every policy starts with the battery half full and carries its own battery state from day to day.
- Runtime 8.6 minutes.
