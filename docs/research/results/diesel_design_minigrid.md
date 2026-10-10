# Diesel study: solar mini-grid, off grid

Site `minigrid`, design run. Period (UTC) 2016-01-01 00:00:00+00:00 to 2017-12-31 23:00:00+00:00; scored days: 366.

## Site

- Average load 83.9 kW; solar 1202 kWh on an average day (real ENTSO-E Spain shapes).
- Battery 600 kWh usable, 150 kW, floor 60 kWh. Generator 100 kW, minimum load 30%, minimum run 2 hours, HOMER default fuel curve.
- Missing hours: load 7, solar 1.

## Litres against how sites run today

Every policy keeps the real-time backstop (the generator is started if the battery is about to run out), so outages are comparable. Savings are against "Today: start when the battery runs out", the best-run practice.

| Policy | Diesel (L) | Saved vs today | Generator hours | Starts | Unserved (kWh) | Outage hours |
|---|---|---|---|---|---|---|
| Today: start when the battery runs out | 113,585 | 0.0% | 4,541 | 580 | 643 | 103 |
| P50 plan, setpoint 0.6 | 114,768 | -1.0% | 4,424 | 688 | 304 | 46 |
| P50 plan, setpoint 0.7 | 114,228 | -0.6% | 4,245 | 727 | 154 | 20 |
| P50 plan, setpoint 0.8 | 112,982 | 0.5% | 3,982 | 806 | 158 | 22 |
| P50 plan, setpoint 0.9 | 111,667 | 1.7% | 3,693 | 883 | 17 | 5 |
| P50 plan, setpoint 1.0 | 110,360 | 2.8% | 3,416 | 951 | 86 | 11 |
| P90 plan, setpoint 0.6 | 124,203 | -9.3% | 4,875 | 884 | 171 | 26 |
| P90 plan, setpoint 0.7 | 126,599 | -11.5% | 4,817 | 930 | 110 | 17 |
| P90 plan, setpoint 0.8 | 125,263 | -10.3% | 4,574 | 888 | 8 | 2 |
| P90 plan, setpoint 0.9 | 125,657 | -10.6% | 4,359 | 877 | 17 | 4 |
| P90 plan, setpoint 1.0 | 124,669 | -9.8% | 4,098 | 861 | 15 | 3 |

## Method

- Day-ahead net load forecasts (quantile GBM, conformal calibration over the previous 28 days), rolling origin at 00:00 UTC, trained only on data before each day. Vaticore plans on P50/P90 net load and, on the grid site, P10 grid availability.
- Each day is run on what actually happened; the battery is carried from day to day; every policy starts half full.
- Runtime 9.6 minutes.
