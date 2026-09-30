# Elia study: solar generation (Belgium), day ahead

Trained on the first 120 days; scored on 275 days. Values in MW.

| Forecast | Pinball (MW) | vs persistence | MAE (MW) | P10-P90 held (target 80%) |
|---|---|---|---|---|
| persistence | 178.8 | +0.0% | 429.4 | 79.5% |
| quantile_gbm_day_ahead | 138.5 | +22.5% | 422.0 | 78.3% |
| quantile_gbm_day_ahead+conformal | 138.3 | +22.7% | 422.0 | 80.8% |
| Elia day-ahead (professional) | 57.7 | +67.7% | 172.0 | 85.8% |

Vaticore runs without weather in this part: history and calendar only. Elia's forecast is driven by weather forecasts. The gap is the value of weather.

Runtime 3.4 minutes.
