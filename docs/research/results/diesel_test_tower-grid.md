# Diesel study: telecom tower with solar on a weak grid (synthetic grid pattern)

Site `tower-grid`, test run. Period (UTC) 2017-01-01 00:00:00+00:00 to 2018-12-31 23:00:00+00:00; scored days: 365.

## Site

- Average load 3.3 kW; solar 48 kWh on an average day (real ENTSO-E Spain shapes).
- Battery 20 kWh usable, 10 kW, floor 2 kWh. Generator 16 kW, minimum load 30%, minimum run 2 hours, HOMER default fuel curve.
- Grid 10 kW, on 63% of hours. **The grid's on/off pattern is synthetic** (a seeded random process); no public hourly record of a Nigerian grid exists.
- Missing hours: load 5, solar 2.

## Litres against how sites run today

Every policy keeps the real-time backstop (the generator is started if the battery is about to run out), so outages are comparable. Savings are against "Today: start when the battery runs out", the best-run practice.

| Policy | Diesel (L) | Saved vs today | Generator hours | Starts | Unserved (kWh) | Outage hours |
|---|---|---|---|---|---|---|
| Today: start when the battery runs out | 1,068 | 0.0% | 430 | 236 | 0 | 0 |
| Today: evening timer | 2,829 | -164.9% | 1,139 | 444 | 0 | 0 |
| Today: generator whenever the grid is off | 6,476 | -506.3% | 2,607 | 722 | 0 | 0 |
| Vaticore plan, today's planner | 1,128 | -5.6% | 454 | 256 | 0 | 0 |
| Vaticore plan, run hard | 1,214 | -13.6% | 299 | 176 | 0 | 0 |
| Vaticore plan, run hard + look ahead | 1,236 | -15.7% | 307 | 179 | 0 | 0 |
| Charger setting alone, no forecast | 1,145 | -7.2% | 273 | 158 | 0 | 0 |
| Perfect forecast, run hard + look ahead | 1,080 | -1.1% | 274 | 159 | 0 | 0 |

## Method

- Day-ahead net load forecasts (quantile GBM, conformal calibration over the previous 28 days), rolling origin at 00:00 UTC, trained only on data before each day. Vaticore plans on P50 net load and P50 grid availability.
- Each day is run on what actually happened; the battery is carried from day to day; every policy starts half full.
- Runtime 9.7 minutes.
