# Diesel study: telecom tower with solar on a weak grid (synthetic grid pattern)

Site `tower-grid`, design run. Period (UTC) 2016-01-01 00:00:00+00:00 to 2017-12-31 23:00:00+00:00; scored days: 366.

## Site

- Average load 3.3 kW; solar 48 kWh on an average day (real ENTSO-E Spain shapes).
- Battery 20 kWh usable, 10 kW, floor 2 kWh. Generator 16 kW, minimum load 30%, minimum run 2 hours, HOMER default fuel curve.
- Grid 10 kW, on 63% of hours. **The grid's on/off pattern is synthetic** (a seeded random process); no public hourly record of a Nigerian grid exists.
- Missing hours: load 7, solar 1.

## Litres against how sites run today

Every policy keeps the real-time backstop (the generator is started if the battery is about to run out), so outages are comparable. Savings are against "Today: start when the battery runs out", the best-run practice.

| Policy | Diesel (L) | Saved vs today | Generator hours | Starts | Unserved (kWh) | Outage hours |
|---|---|---|---|---|---|---|
| Today: start when the battery runs out | 1,073 | 0.0% | 432 | 231 | 0 | 0 |
| P50 plan, setpoint 0.8 | 1,209 | -12.7% | 301 | 168 | 0 | 0 |
| P90 plan, setpoint 0.8 | 1,258 | -17.2% | 320 | 179 | 0 | 0 |

## Method

- Day-ahead net load forecasts (quantile GBM, conformal calibration over the previous 28 days), rolling origin at 00:00 UTC, trained only on data before each day. Vaticore plans on P50/P90 net load and P50 grid availability.
- Each day is run on what actually happened; the battery is carried from day to day; every policy starts half full.
- Runtime 9.7 minutes.
