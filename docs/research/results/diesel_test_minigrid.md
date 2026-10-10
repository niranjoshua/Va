# Diesel study: solar mini-grid, off grid

Site `minigrid`, test run. Period (UTC) 2017-01-01 00:00:00+00:00 to 2018-12-31 23:00:00+00:00; scored days: 365.

## Site

- Average load 84.7 kW; solar 1188 kWh on an average day (real ENTSO-E Spain shapes).
- Battery 600 kWh usable, 150 kW, floor 60 kWh. Generator 100 kW, minimum load 30%, minimum run 2 hours, HOMER default fuel curve.
- Missing hours: load 5, solar 2.

## Litres against how sites run today

Every policy keeps the real-time backstop (the generator is started if the battery is about to run out), so outages are comparable. Savings are against "Today: start when the battery runs out", the best-run practice.

| Policy | Diesel (L) | Saved vs today | Generator hours | Starts | Unserved (kWh) | Outage hours |
|---|---|---|---|---|---|---|
| Today: start when the battery runs out | 126,257 | 0.0% | 5,017 | 610 | 479 | 94 |
| Today: evening timer | 127,822 | -1.2% | 5,116 | 657 | 608 | 106 |
| Vaticore plan, today's planner | 127,334 | -0.9% | 5,077 | 642 | 530 | 94 |
| Vaticore plan, run hard | 123,757 | 2.0% | 4,329 | 887 | 163 | 30 |
| Vaticore plan, run hard + look ahead | 123,969 | 1.8% | 4,344 | 851 | 214 | 35 |
| Charger setting alone, no forecast | 122,131 | 3.3% | 4,261 | 938 | 272 | 51 |
| Perfect forecast, run hard + look ahead | 121,672 | 3.6% | 4,257 | 825 | 21 | 3 |

## Method

- Day-ahead net load forecasts (quantile GBM, conformal calibration over the previous 28 days), rolling origin at 00:00 UTC, trained only on data before each day. Vaticore plans on P50 net load.
- Each day is run on what actually happened; the battery is carried from day to day; every policy starts half full.
- Runtime 9.7 minutes.
