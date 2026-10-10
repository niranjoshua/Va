# Diesel study: telecom tower with solar, off grid

Site `tower`, test run. Period (UTC) 2017-01-01 00:00:00+00:00 to 2018-12-31 23:00:00+00:00; scored days: 365.

## Site

- Average load 3.3 kW; solar 48 kWh on an average day (real ENTSO-E Spain shapes).
- Battery 20 kWh usable, 10 kW, floor 2 kWh. Generator 16 kW, minimum load 30%, minimum run 2 hours, HOMER default fuel curve.
- Missing hours: load 5, solar 2.

## Litres against how sites run today

Every policy keeps the real-time backstop (the generator is started if the battery is about to run out), so outages are comparable. Savings are against "Today: start when the battery runs out", the best-run practice.

| Policy | Diesel (L) | Saved vs today | Generator hours | Starts | Unserved (kWh) | Outage hours |
|---|---|---|---|---|---|---|
| Today: start when the battery runs out | 7,636 | 0.0% | 3,074 | 1,523 | 0 | 0 |
| Today: evening timer | 8,826 | -15.6% | 3,553 | 1,072 | 0 | 0 |
| Vaticore plan, today's planner | 7,807 | -2.2% | 3,143 | 1,487 | 0 | 0 |
| Vaticore plan, run hard | 6,171 | 19.2% | 1,574 | 779 | 0 | 0 |
| Vaticore plan, run hard + look ahead | 6,140 | 19.6% | 1,613 | 797 | 0 | 0 |
| Charger setting alone, no forecast | 5,753 | 24.7% | 1,384 | 723 | 0 | 0 |
| Perfect forecast, run hard + look ahead | 5,714 | 25.2% | 1,394 | 730 | 0 | 0 |

## Method

- Day-ahead net load forecasts (quantile GBM, conformal calibration over the previous 28 days), rolling origin at 00:00 UTC, trained only on data before each day. Vaticore plans on P50 net load.
- Each day is run on what actually happened; the battery is carried from day to day; every policy starts half full.
- Runtime 9.4 minutes.
