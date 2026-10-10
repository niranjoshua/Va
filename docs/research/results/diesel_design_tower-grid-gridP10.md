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
| P50 plan, setpoint 0.6 | 1,735 | -61.7% | 546 | 322 | 0 | 0 |
| P50 plan, setpoint 0.7 | 1,787 | -66.5% | 551 | 330 | 0 | 0 |
| P50 plan, setpoint 0.8 | 1,814 | -69.0% | 559 | 337 | 0 | 0 |
| P50 plan, setpoint 0.9 | 1,817 | -69.3% | 559 | 337 | 0 | 0 |
| P50 plan, setpoint 1.0 | 1,817 | -69.3% | 559 | 337 | 0 | 0 |
| P90 plan, setpoint 0.6 | 2,169 | -102.1% | 718 | 435 | 0 | 0 |
| P90 plan, setpoint 0.7 | 2,113 | -96.9% | 679 | 409 | 0 | 0 |
| P90 plan, setpoint 0.8 | 2,090 | -94.7% | 662 | 404 | 0 | 0 |
| P90 plan, setpoint 0.9 | 2,094 | -95.2% | 663 | 404 | 0 | 0 |
| P90 plan, setpoint 1.0 | 2,094 | -95.2% | 663 | 404 | 0 | 0 |

## Method

- Day-ahead net load forecasts (quantile GBM, conformal calibration over the previous 28 days), rolling origin at 00:00 UTC, trained only on data before each day. Vaticore plans on P50/P90 net load and, on the grid site, P10 grid availability.
- Each day is run on what actually happened; the battery is carried from day to day; every policy starts half full.
- Runtime 10.3 minutes.
