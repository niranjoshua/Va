# Foundation model study: design (context length)

Elia total load, 2024-07-01 to 2024-12-31, a design period only; the test period (2025 on) is untouched. Each model forecasts each day at 00:00 UTC from the last N hours. Values in MW.

| Forecast | Pinball (MW) | vs persistence | MAE (MW) | P10-P90 held (target 80%) |
|---|---|---|---|---|
| persistence | 174.28 | +0.0% | 485.88 | 81.1% |
| chronos-2, 4 weeks | 68.83 | +60.5% | 209.69 | 75.9% |
| chronos-2, 12 weeks | 67.70 | +61.2% | 206.79 | 75.1% |
| chronos-2, 48 weeks | 67.63 | +61.2% | 205.20 | 74.8% |
| timesfm-2.5, 4 weeks | 97.07 | +44.3% | 290.09 | 68.8% |
| timesfm-2.5, 12 weeks | 81.61 | +53.2% | 247.71 | 72.4% |
| timesfm-2.5, 48 weeks | 73.77 | +57.7% | 225.41 | 78.0% |

## Chosen for the test

- chronos-2: 8064 hours (48 weeks), the lowest pinball loss.
- timesfm-2.5: 8064 hours (48 weeks), the lowest pinball loss.

Runtime 14.7 minutes.
