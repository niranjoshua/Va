# Vaticore research programme

Vaticore's claims are only as good as the evidence behind them. This page sets
out the questions we are answering, the rules every study follows, and where
each result lives. Studies are real, tested code in this repository, not
notebooks, and each can be rerun from one command.

## The questions

| # | Question | Why it matters | Status |
|---|---|---|---|
| RQ1 | Does a calibrated range beat a more accurate point forecast when a site plans its generator? | Decides what we sell: honest ranges, not just accuracy | **Supported on two independent datasets** (notes 1 and 2); note 3 agrees: a 21% more accurate forecast added under 2 points of value once ranges were calibrated |
| RQ2 | What is a forecast worth in litres, outage hours and money, against today's practice? | The number a customer pays for | Measured on shaped sites (notes 1 and 2); pilot data next |
| RQ3 | How much does weather add, for solar and for net load? | The largest accuracy gap left | Gap measured against Elia (note 2); weather study needs archived forecasts |
| RQ4 | Can a site's grid supply hours be forecast well enough to plan on? | Core for towers, banks and factories on weak grids | Baseline built; needs real grid on/off records |
| RQ5 | How fast can a new site be forecast well, borrowing from similar sites? | Onboarding thousands of towers | **Pretrained models answer it on public data** (note 3): Chronos-2 with two weeks of history beat the GBM with three years; fleet models and site data next |
| RQ6 | Can fuel use be reconciled against runtime and deliveries to flag losses? | Pilferage is a major cost for Nigerian tower fleets | Planned; needs fuel and runtime data |

## Protocol: rules every study follows

1. **Chronological splits only.** Every forecast uses information available at
   its issue time and nothing later. Lags, features, calibration and grid plans
   are all built from the past.
2. **Design and test are separate.** Choices such as features, windows and site
   sizes are made on a design period. The test period is run once, and the
   report says which is which.
3. **Always report the baseline.** Persistence is scored in every study. A
   model that does not beat it is reported as such.
4. **Report a bound as well as a baseline.** Value studies include a perfect
   forecast under the same planning rule, so savings read as a share of what
   was possible.
5. **Accuracy, honesty and value, every time.** Pinball loss and MAE, P10 to
   P90 coverage against 80%, and litres, outages and cost.
6. **Show the assumptions that move the answer.** An outage has no market
   price, so every value study includes a sensitivity table across a range of
   outage prices.
7. **Say what the data is and is not.** Shaped or scaled sites, aggregate
   solar and missing hours are stated in the note, with the limitations.
8. **Reproducible by default.** Exact commands are in each note. Runs can be
   logged to MLflow. Data files are never committed; notes record file names,
   columns and periods.
9. **Check for contamination.** Before scoring a pretrained model, check the
   dataset against its published pretraining corpora. A model that has seen
   the test period is measuring memory.
10. **Negative results are published too.** A finding that a model or idea did
   not work is kept, with why.

## Studies

| Note | Question | Data | Headline |
|---|---|---|---|
| [1. What a forecast is worth to a solar mini-grid](value-study-2018.md) | RQ1, RQ2 | ENTSO-E Spain demand and solar, 2017 to 2018, one held-out year | Calibrated P90 plan: 82 to 84% less unserved energy, 72% fewer outage hours, 7 to 10% lower cost |
| [2. Vaticore against a grid operator's own forecasts](elia-benchmark.md) | RQ1, RQ2, RQ3 | Elia (Belgium) load 2015 to 2026 and solar 2025 to 2026, with Elia's P10, P50 and P90 | Load: parity with Elia's forecast over 453 held-out days, with a better calibrated range (80.4% against 74.9%), no weather. Solar: weather is the gap (Elia 68% better than persistence, Vaticore 23%). Value: calibrated P90 captured 85% of possible savings, Elia's medians 84% |
| [3. Pretrained foundation models against Vaticore's planning model](foundation-models.md) | RQ1, RQ5 | Elia load and solar (Spain excluded: it is in the models' pretraining data) | Chronos-2, zero shot, no weather: 21% lower pinball than Elia's own load forecast and Vaticore's GBM over 453 days, calibrated range 80.3%; value 86.7% of possible against 85.2%; with two weeks of history it beat the GBM with three years |

## Data catalogue

| Dataset | Content | Period | Licence and source | Used in |
|---|---|---|---|---|
| ENTSO-E Spain (via Darts) | Hourly total load, solar generation and TSO forecasts | 2015 to 2018 | Public; `unit8co/darts` datasets. The load series is in GiftEvalPretrain, so never score pretrained models on it | Note 1 |
| Elia ods001 | 15 minute Belgian total load, with Elia's day-ahead and most recent P10, P50 and P90 | 2015 to April 2026 | Elia Open Data | Note 2 |
| Elia solar by region | 15 minute measured and upscaled solar for Belgium and 13 regions, with Elia's P10, P50 and P90 | July 2025 to August 2026 | Elia Open Data | Note 2 |
| Pilot site data | Load, solar, battery, generator runtime, fuel and grid on/off | To come | Operator owned; never committed | Planned |

## What we need next

- **Real site data.** Six to twelve months per site, from a tower fleet or a
  bank or factory portfolio: load, solar, generator runtime and fuel, and grid
  on/off by hour. This moves RQ2, RQ4 and RQ6 from shaped sites to real ones.
- **Archived weather forecasts.** Forecasts as they were issued, not
  reanalysis, for honest weather backtests (RQ3). Open-Meteo's historical
  forecast API provides these.
