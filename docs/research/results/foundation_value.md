# Foundation model study: value on a Belgian-shaped solar mini-grid (test)

The site of research note 2: real Belgian demand and solar shaped to a 120 kW peak and 200 kWp, battery 600 kWh, generator 100 kW. Scored 2025-11-19 to 2026-03-28 (130 days) after 120 training days. Context (chosen in the design part): chronos-2 8064 h, timesfm-2.5 8064 h, timesfm-3.0 (research only) 8064 h.

## Net load accuracy and calibration

| Forecast | Pinball (kW) | vs persistence | MAE (kW) | P10-P90 held (target 80%) |
|---|---|---|---|---|
| persistence | 4.47 | +0.0% | 11.10 | 83.1% |
| quantile_gbm_day_ahead | 3.41 | +23.7% | 10.23 | 56.9% |
| quantile_gbm_day_ahead+conformal | 3.32 | +25.6% | 10.23 | 80.2% |
| chronos-2 | 2.87 | +35.8% | 9.21 | 78.3% |
| chronos-2+conformal | 2.87 | +35.7% | 9.21 | 81.0% |
| timesfm-2.5 | 3.28 | +26.6% | 9.83 | 84.3% |
| timesfm-2.5+conformal | 3.27 | +26.7% | 9.83 | 81.3% |
| timesfm-3.0 (research only) | 2.98 | +33.4% | 9.43 | 85.4% |
| timesfm-3.0 (research only)+conformal | 2.97 | +33.5% | 9.43 | 82.2% |

## Value: litres, outages and money (an unserved kWh at 1.00, diesel 1.10/L)

| Policy | Diesel (L) | Unserved (kWh) | Outage hours | Total cost | Saved vs baseline | Share of possible |
|---|---|---|---|---|---|---|
| persistence P50 | 74,670 | 6,218 | 250 | 88,356 | 0 | 0.0% |
| persistence P90 | 76,072 | 2,675 | 180 | 86,354 | 2,002 | 55.6% |
| quantile_gbm_day_ahead+conformal P90 | 76,927 | 669 | 102 | 85,289 | 3,067 | 85.2% |
| chronos-2+conformal P90 | 76,936 | 602 | 98 | 85,232 | 3,124 | 86.7% |
| timesfm-2.5+conformal P90 | 76,991 | 551 | 97 | 85,241 | 3,115 | 86.5% |
| timesfm-3.0 (research only)+conformal P90 | 76,991 | 495 | 94 | 85,186 | 3,170 | 88.0% |
| perfect forecast | 76,586 | 509 | 120 | 84,754 | 3,602 | 100.0% |

Runtime 63.0 minutes.
