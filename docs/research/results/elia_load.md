# Elia study: total load, day ahead

Trained from 2022-01-01; scored on 453 days from 2025-01-01 (every hour, 00:00 to 23:00 UTC). Values in MW.

| Forecast | Pinball (MW) | vs persistence | MAE (MW) | P10-P90 held (target 80%) |
|---|---|---|---|---|
| persistence | 178.4 | +0.0% | 510.0 | 78.8% |
| quantile_gbm_day_ahead | 96.2 | +46.1% | 287.8 | 69.2% |
| quantile_gbm_day_ahead+conformal | 95.2 | +46.7% | 287.8 | 80.4% |
| Elia day-ahead (professional) | 95.2 | +46.6% | 292.4 | 74.9% |

Elia's forecast is issued at 18:00 local time the day before and uses weather and operator knowledge. Vaticore's day-ahead model uses only calendar features and load lags known at midnight UTC, no weather.

Runtime 18.6 minutes.
