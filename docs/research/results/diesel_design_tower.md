# Diesel study: telecom tower with solar, off grid

Site `tower`, design run. Period (UTC) 2016-01-01 00:00:00+00:00 to 2017-12-31 23:00:00+00:00; scored days: 366.

## Site

- Average load 3.3 kW; solar 48 kWh on an average day (real ENTSO-E Spain shapes).
- Battery 20 kWh usable, 10 kW, floor 2 kWh. Generator 16 kW, minimum load 30%, minimum run 2 hours, HOMER default fuel curve.
- Missing hours: load 7, solar 1.

## Litres against how sites run today

Every policy keeps the real-time backstop (the generator is started if the battery is about to run out), so outages are comparable. Savings are against "Today: start when the battery runs out", the best-run practice.

| Policy | Diesel (L) | Saved vs today | Generator hours | Starts | Unserved (kWh) | Outage hours |
|---|---|---|---|---|---|---|
| Today: start when the battery runs out | 7,084 | 0.0% | 2,852 | 1,428 | 0 | 0 |
| P50 plan, setpoint 0.6 | 5,865 | 17.2% | 1,655 | 823 | 0 | 0 |
| P50 plan, setpoint 0.7 | 5,817 | 17.9% | 1,556 | 787 | 0 | 0 |
| P50 plan, setpoint 0.8 | 5,781 | 18.4% | 1,526 | 767 | 0 | 0 |
| P50 plan, setpoint 0.9 | 5,781 | 18.4% | 1,524 | 767 | 0 | 0 |
| P50 plan, setpoint 1.0 | 5,781 | 18.4% | 1,524 | 767 | 0 | 0 |
| P90 plan, setpoint 0.6 | 6,577 | 7.2% | 1,951 | 1,010 | 0 | 0 |
| P90 plan, setpoint 0.7 | 6,384 | 9.9% | 1,776 | 922 | 0 | 0 |
| P90 plan, setpoint 0.8 | 6,289 | 11.2% | 1,722 | 889 | 0 | 0 |
| P90 plan, setpoint 0.9 | 6,298 | 11.1% | 1,724 | 889 | 0 | 0 |
| P90 plan, setpoint 1.0 | 6,298 | 11.1% | 1,724 | 889 | 0 | 0 |

## Method

- Day-ahead net load forecasts (quantile GBM, conformal calibration over the previous 28 days), rolling origin at 00:00 UTC, trained only on data before each day. Vaticore plans on P50/P90 net load and, on the grid site, P10 grid availability.
- Each day is run on what actually happened; the battery is carried from day to day; every policy starts half full.
- Runtime 9.5 minutes.
