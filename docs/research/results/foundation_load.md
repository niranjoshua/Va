# Foundation model study: Elia total load, day ahead (test)

Scored on 453 days from 2025-01-01, every hour, the same hours as research note 2. Values in MW. Context (chosen in the design part): chronos-2 8064 h, timesfm-2.5 8064 h, timesfm-3.0 (research only) 8064 h.

| Forecast | Pinball (MW) | vs persistence | MAE (MW) | P10-P90 held (target 80%) |
|---|---|---|---|---|
| persistence | 178.44 | +0.0% | 510.02 | 78.8% |
| quantile_gbm_day_ahead | 96.19 | +46.1% | 287.83 | 69.2% |
| quantile_gbm_day_ahead+conformal | 95.16 | +46.7% | 287.83 | 80.4% |
| chronos-2 | 75.02 | +58.0% | 230.21 | 77.9% |
| chronos-2+conformal | 75.20 | +57.9% | 230.21 | 80.3% |
| timesfm-2.5 | 83.03 | +53.5% | 252.72 | 80.7% |
| timesfm-2.5+conformal | 83.30 | +53.3% | 252.72 | 80.4% |
| timesfm-3.0 (research only) | 75.38 | +57.8% | 231.96 | 79.7% |
| timesfm-3.0 (research only)+conformal | 75.54 | +57.7% | 231.96 | 79.9% |
| Elia day-ahead (professional) | 95.23 | +46.6% | 292.44 | 74.9% |

No model here uses weather; Elia's forecast does. Conformal calibration uses only the previous 28 days' misses.

Compute for the whole test on a 4-core CPU: quantile_gbm_day_ahead 16.9 min, chronos-2 85.5 min, timesfm-2.5 7.3 min, timesfm-3.0 (research only) 6.2 min.

Runtime 116.0 minutes.
