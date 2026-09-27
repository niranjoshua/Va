# What a forecast is worth to a solar mini-grid

Vaticore research note 1. A one-year, held-out study on real grid data.

## Question

Forecast accuracy scores (pinball loss, MAE) say which forecast is closer. An
operator needs to know which forecast saves diesel and keeps the lights on.
This study measures that directly. Each forecast drives a day-ahead generator
plan, and the plan is then run against what actually happened.

## Answer

On 365 days of 2018 that played no part in designing the method, planning the
generator on Vaticore's calibrated P90 net load forecast gave these results
against planning on persistence ("tomorrow looks like yesterday", roughly what
operators do today):

| | Site A (600 kWh battery) | Site B (300 kWh battery) |
|---|---|---|
| Energy not served | **82% less** (44,119 to 7,844 kWh) | **84% less** (35,840 to 5,624 kWh) |
| Hours with an outage | **72% fewer** (970 to 276) | **72% fewer** (952 to 265) |
| Total cost at 1.00 per unserved kWh | **10% lower** | **7% lower** |
| Share of a perfect forecast's value captured | **61%** | **63%** |
| Diesel used | 16% more | 11% more |
| Pinball loss, day-ahead net load | **23% lower** | **22% lower** |
| P10-P90 range held (target 80%) | **80.7%** (raw model 71.0%) | **80.7%** (raw model 70.8%) |

The gain comes from buying reliability with a modest amount of extra diesel,
placed in the right hours. With diesel at 1.10 per litre it pays whenever an
unserved kWh is worth more than about 0.55 (break-even 0.55 on both sites; see
the sensitivity tables in the full results). Below that, running the generator
less is cheaper, and the report says so.

Vaticore also beat the strongest naive plan: persistence planned on its own
P90. Against that plan, Vaticore left about half as much energy unserved (Site
A 7,844 against 14,490 kWh; Site B 5,624 against 11,859 kWh) at lower total
cost.

## Three findings

1. **Calibration matters more than accuracy for decisions.** Planning on the
   median (P50) of a more accurate model saved almost nothing. Planning on a
   calibrated upper quantile saved most of what was available. A good range is
   what makes a forecast useful to an operator.
2. **Day-ahead models must only see day-ahead information.** The original
   gradient boosting setup used 1 to 3 hour lags and forecast recursively. On a
   2017 diagnostic period its P10-P90 band held only 30% of day-ahead outcomes,
   and its pinball loss was no better than persistence (7.36 against 7.31).
   Restricting features to lags known at issue time (the same hour 1, 2 and 3
   days earlier and one week earlier) gave honest bands (70%) and a 16% lower
   pinball loss on the same period. This is now `quantile_gbm_day_ahead`, the
   default for planning.
3. **Conformal calibration closes the rest of the gap, without leakage.** Each
   day's band is adjusted using only the misses of the previous 28 days
   (conformalized quantile regression). Coverage moved from 71% to 80.7%
   against an 80% target, on both sites.

## Data

- ENTSO-E hourly data for Spain, 2015 to 2018, as published in the Darts
  forecasting library (`energy_dataset.csv`): `total load actual` and
  `generation solar`. Public, reproducible, and not committed to this repo.
- Period used: 2017 to 2018 (17,520 hours). 2017 is training only. All 365
  days of 2018 are scored. Missing hours: 5 in load and 2 in solar.
- Design decisions (the day-ahead lag set, the calibration window, the site
  sizes) were made on May to October 2017 only. 2018 was run once.

## Site model

Real demand and solar shapes are scaled to a representative solar mini-grid:

- Demand scaled to a 120 kW peak, about 2,030 kWh on an average day.
- Solar: the real output shape on a 200 kWp array (Site A) or 160 kWp (Site B).
- Battery: Site A 600 kWh usable, 150 kW, 60 kWh floor. Site B 300 kWh,
  100 kW, 30 kWh floor. 95% efficiency each way.
- Generator 100 kW with a 30% minimum stable load. Fuel follows the HOMER Pro
  default curve: 0.08145 L/h per rated kW plus 0.246 L per kWh.
- Diesel 1.10 per litre. An unserved kWh priced at 1.00, with 0.25 to 5.00
  shown in the sensitivity tables.

Net load (demand minus solar) is forecast directly. It goes negative on sunny
middays, which charges the battery.

## Method

- Rolling origin: one forecast per day at 00:00 UTC for the next 24 hours,
  trained only on data before that moment.
- Policies: persistence planned on P50 (the baseline) and on P90; Vaticore
  planned on P50, on P90, and on the conformally calibrated P90.
- Planning rule, identical for every policy: walk forward through the forecast
  and schedule the generator for exactly the hours in which the battery could
  not otherwise serve that hour's forecast net load.
- Operation: run the real day with that schedule fixed. A generator that was
  not scheduled does not start, so a shortfall becomes unserved energy. Battery
  state carries from day to day, separately for each policy.
- Perfect forecast bound: the same planning rule applied to the actual outcome.
  It bounds what any forecast can achieve under this rule. It is not a global
  optimum.

## Limitations

- Country-level solar is smoother than one site's panels, because clouds
  average out across Spain. Single-site forecast errors will be larger, which
  makes calibration more valuable, not less. Pilot data will confirm this.
- The site is a scaled composite, not a metered mini-grid. The absolute
  litres and costs illustrate the mechanism. The relative results are the
  finding.
- The planning rule is deliberately simple. A smarter planner (for example,
  pre-charging the battery from the generator before a known evening peak)
  would raise every policy, including the perfect forecast bound.
- No weather features yet. Weather is expected to be the largest remaining
  accuracy gain for solar.

## Reproduce

```bash
curl -L -o energy_dataset.csv \
  https://raw.githubusercontent.com/unit8co/darts/master/datasets/energy_dataset.csv
uv run python examples/value_study.py energy_dataset.csv \
  --start 2017-01-01 --end 2018-12-31T23:00 --initial-days 365 \
  --site-id site-A-600kWh --out results/
uv run python examples/value_study.py energy_dataset.csv \
  --start 2017-01-01 --end 2018-12-31T23:00 --initial-days 365 \
  --battery-kwh 300 --battery-power-kw 100 --min-soc-kwh 30 --pv-kwp 160 \
  --site-id site-B-300kWh --out results/
```

Each run takes about 10 minutes on a laptop. Full reports, including the
sensitivity tables, are in [`results/`](results/). Set
`VATICORE_MLFLOW_TRACKING_URI` to log each run to MLflow.
