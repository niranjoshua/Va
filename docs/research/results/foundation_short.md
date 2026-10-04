# Foundation model study: new sites with short history (test)

Elia total load, 182 days from 2025-01-01. Every model, persistence included, sees only the last 2, 4 or 8 weeks before each forecast, as a newly connected site would. The full-history rows are the reference for an established site. No conformal calibration: a new site has too few past forecasts to calibrate on. Values in MW; skill is against persistence with full history.

| Forecast | Pinball (MW) | vs persistence | MAE (MW) | P10-P90 held (target 80%) |
|---|---|---|---|---|
| persistence | 175.08 | +0.0% | 501.78 | 79.5% |
| quantile_gbm_day_ahead, full history | 94.99 | +45.7% | 287.33 | 69.3% |
| persistence, 2 weeks | 189.10 | -8.0% | 516.87 | 75.8% |
| chronos-2, 2 weeks | 83.98 | +52.0% | 258.31 | 73.5% |
| timesfm-2.5, 2 weeks | 104.86 | +40.1% | 320.68 | 71.6% |
| timesfm-3.0 (research only), 2 weeks | 83.12 | +52.5% | 256.37 | 81.8% |
| persistence, 4 weeks | 182.72 | -4.4% | 509.37 | 77.5% |
| quantile_gbm_day_ahead, 4 weeks | 136.11 | +22.3% | 367.66 | 43.4% |
| chronos-2, 4 weeks | 76.00 | +56.6% | 235.33 | 76.5% |
| timesfm-2.5, 4 weeks | 98.69 | +43.6% | 298.17 | 67.8% |
| timesfm-3.0 (research only), 4 weeks | 77.58 | +55.7% | 240.32 | 80.9% |
| persistence, 8 weeks | 178.33 | -1.9% | 504.94 | 78.9% |
| quantile_gbm_day_ahead, 8 weeks | 123.47 | +29.5% | 340.66 | 47.0% |
| chronos-2, 8 weeks | 73.05 | +58.3% | 223.48 | 76.6% |
| timesfm-2.5, 8 weeks | 89.38 | +48.9% | 271.25 | 72.3% |
| timesfm-3.0 (research only), 8 weeks | 74.68 | +57.3% | 229.08 | 81.3% |

- quantile_gbm_day_ahead, 2 weeks: cannot run (not enough usable rows after building lag 168 features; provide more history).

Runtime 15.3 minutes.
