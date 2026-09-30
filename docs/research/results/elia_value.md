# Elia study: value on a Belgian-shaped solar mini-grid

Real Belgian demand and solar, 2025-07-22 to 2026-03-29; the first 120 days train, 130 days are scored (mostly autumn and winter, when solar is weakest). Demand scaled to a 120 kW peak, solar to 200 kWp, battery 600 kWh, generator 100 kW.

| Policy | Diesel (L) | Unserved (kWh) | Outage hours | Total cost | Saved vs baseline | Share of possible |
|---|---|---|---|---|---|---|
| persistence P50 | 74,670 | 6,218 | 250 | 88,356 | 0 | 0.0% |
| persistence P90 | 76,072 | 2,675 | 180 | 86,354 | 2,002 | 55.6% |
| Elia medians (load P50 minus solar P50) | 76,306 | 1,400 | 164 | 85,336 | 3,020 | 83.8% |
| quantile_gbm_day_ahead P50 | 75,622 | 3,638 | 193 | 86,823 | 1,533 | 42.6% |
| quantile_gbm_day_ahead P90 | 76,814 | 786 | 106 | 85,282 | 3,074 | 85.3% |
| quantile_gbm_day_ahead+conformal P90 | 76,927 | 669 | 102 | 85,289 | 3,067 | 85.2% |
| perfect forecast | 76,586 | 509 | 120 | 84,754 | 3,602 | 100.0% |

Elia publishes separate load and solar quantiles, and quantiles do not subtract, so only Elia's medians can form a net load plan. That policy shows what a professional point forecast is worth to the same site.

Runtime 1.7 minutes.
