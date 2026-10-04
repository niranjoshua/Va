# Pretrained foundation models against Vaticore's planning model

Vaticore research note 3. Chronos-2 (Amazon) and TimesFM (Google) are
pretrained on large collections of time series and forecast a new series
"zero shot", with no training on the site. This note asks whether they should
replace or join the day-ahead quantile GBM, on the Elia data of note 2, and
whether they help new sites with only weeks of history.

## Answer

1. **On Belgian load, Chronos-2 is the most accurate forecast we have
   measured.** Over the same 453 held-out days as note 2, zero shot and
   without weather, its pinball loss was 21% lower than both Vaticore's
   calibrated GBM and Elia's own professional forecast (75.2 against 95.2
   MW), and its MAE 21% lower than Elia's. After conformal calibration its
   P10 to P90 range held 80.3% of outcomes against an 80% target.
2. **The better forecast buys a little more value, not a lot.** On note 2's
   Belgian-shaped mini-grid, planning on calibrated Chronos-2 captured 86.7%
   of the savings a perfect forecast could give, against 85.2% for the
   calibrated GBM, and left 10% less energy unserved (602 against 669 kWh).
   The calibrated GBM had already captured most of what was available. This
   repeats the lesson of notes 1 and 2: once the range is honest, extra
   accuracy is worth less to the plan than calibration was.
3. **New sites are where pretrained models change the product.** With only two weeks of a site's history, Chronos-2 forecast Belgian load more accurately than the GBM trained on three years (pinball 84.0 against 95.0 MW, on the same 182 days). The GBM cannot run at all on two weeks, and on four weeks its range held only 43% of outcomes. This is the strongest commercial result in the note: a new site can get a usable, nearly honest forecast in its first fortnight.
4. **Two findings came before any accuracy number.**
   - **Licences.** Chronos-2 and TimesFM 2.5 are Apache 2.0 and usable in
     the product. TimesFM 3.0 is released under a non-commercial licence; it
     appears below as a research reference only and cannot serve customers.
   - **Contamination.** The models' pretraining corpus (GiftEvalPretrain)
     contains the Spanish load series of note 1, value for value from 2015 to
     2018, including its 2018 test year. Scoring the models on it would
     measure memory, not forecasting, so this note uses Elia only.

**Decision.** Chronos-2 with conformal calibration becomes a planning model
in the engine (`chronos_2`) and runs beside the GBM on every pilot. It
replaces the GBM as the default only if it also wins on pilot site data,
which no pretrained model has seen. TimesFM 2.5 is less accurate here and
is not taken forward. The GBM stays as the reference and the fallback: it
needs no large download and can be explained feature by feature.

## Load, day ahead (Belgium, national), test

Trained (or, for the foundation models, given context) on data before each
day; every hour of 453 days from 1 January 2025 scored, one forecast per day
at 00:00 UTC. Identical hours to note 2.

| Forecast | Pinball (MW) | vs persistence | MAE (MW) | P10-P90 held (target 80%) |
|---|---|---|---|---|
| Persistence | 178.4 | | 510.0 | 78.8% |
| Vaticore GBM, calibrated (note 2) | 95.2 | 46.7% better | 287.8 | 80.4% |
| Elia day-ahead (professional, with weather) | 95.2 | 46.6% better | 292.4 | 74.9% |
| TimesFM 2.5 | 83.0 | 53.5% better | 252.7 | 80.7% |
| TimesFM 2.5, calibrated | 83.3 | 53.3% better | 252.7 | 80.4% |
| **Chronos-2** | **75.0** | **58.0% better** | **230.2** | 77.9% |
| **Chronos-2, calibrated** | **75.2** | **57.9% better** | **230.2** | **80.3%** |
| TimesFM 3.0 (research only) | 75.4 | 57.8% better | 232.0 | 79.7% |

Points to note:

- **The pretrained models arrive nearly calibrated.** Chronos-2's raw range
  held 77.9% and TimesFM 2.5's 80.7%, where the raw GBM held 69.2%.
  Calibration still moves Chronos-2 onto the target, at no cost in accuracy.
- **None of these forecasts uses weather.** Elia's does. Weather covariates
  (Chronos-2 accepts them) are the obvious next test.
- **TimesFM 3.0 matches Chronos-2** but cannot be used commercially.

## Value on a Belgian-shaped solar mini-grid, test

Note 2's site: real Belgian demand and solar shaped to a 120 kW peak and 200
kWp, a 600 kWh battery and a 100 kW generator. 130 days scored (19 November
2025 to 28 March 2026, mostly winter) after 120 days of history. Every
policy plans the generator on its own calibrated P90 of net load.

| Net load forecast | Pinball (kW) | vs persistence | P10-P90 held |
|---|---|---|---|
| Persistence | 4.47 | | 83.1% |
| Vaticore GBM, calibrated | 3.32 | 25.6% better | 80.2% |
| TimesFM 2.5, calibrated | 3.27 | 26.7% better | 81.3% |
| **Chronos-2, calibrated** | **2.87** | **35.7% better** | **81.0%** |
| TimesFM 3.0 (research only), calibrated | 2.97 | 33.5% better | 82.2% |

| Policy | Diesel (L) | Unserved (kWh) | Outage hours | Total cost | Share of possible savings |
|---|---|---|---|---|---|
| Persistence P50 (today's practice) | 74,670 | 6,218 | 250 | 88,356 | 0% |
| Persistence P90 | 76,072 | 2,675 | 180 | 86,354 | 55.6% |
| Vaticore GBM, calibrated P90 | 76,927 | 669 | 102 | 85,289 | 85.2% |
| TimesFM 2.5, calibrated P90 | 76,991 | 551 | 97 | 85,241 | 86.5% |
| **Chronos-2, calibrated P90** | **76,936** | **602** | **98** | **85,232** | **86.7%** |
| TimesFM 3.0 (research only), calibrated P90 | 76,991 | 495 | 94 | 85,186 | 88.0% |
| Perfect forecast (bound) | 76,586 | 509 | 120 | 84,754 | 100% |

An unserved kWh is priced at 1.00 and diesel at 1.10 per litre, as in notes
1 and 2. The GBM rows reproduce note 2 exactly.

## New sites with short history, test

Elia total load, 182 days from 1 January 2025. Every model, persistence
included, sees only the last 2, 4 or 8 weeks before each forecast, as a newly
connected site would. No calibration: a new site has too few past forecasts
to calibrate on. Skill is against persistence with full history.

| Forecast | 2 weeks of history | 4 weeks | 8 weeks |
|---|---|---|---|
| Persistence | 189.1 (75.8%) | 182.7 (77.5%) | 178.3 (78.9%) |
| Vaticore GBM | cannot run | 136.1 (43.4%) | 123.5 (47.0%) |
| TimesFM 2.5 | 104.9 (71.6%) | 98.7 (67.8%) | 89.4 (72.3%) |
| **Chronos-2** | **84.0 (73.5%)** | **76.0 (76.5%)** | **73.1 (76.6%)** |
| TimesFM 3.0 (research only) | 83.1 (81.8%) | 77.6 (80.9%) | 74.7 (81.3%) |

Pinball loss in MW, with the share of outcomes inside the P10 to P90 range in
brackets. For reference, on the same 182 days persistence with full history
scored 175.1 and the GBM with three years of history 95.0 (69.3% held).

- **Chronos-2 with two weeks beats the GBM with three years.** The GBM needs a
  week of history just to build its features and then months to learn; a
  pretrained model brings what it learnt elsewhere.
- **The GBM's range is badly overconfident on short history** (43 to 47% held
  against 80%). Planning on it at a new site would be dangerous, which is why
  short-history sites need either a pretrained model or a fleet model.
- **Chronos-2's range runs a little narrow (74 to 77%).** Once a site has a
  few weeks of forecasts behind it, conformal calibration can correct that, as
  it did in the full test.

## Method

- **Models.** `ChronosForecaster` and `TimesFMForecaster`
  (`vaticore/forecasting/foundation.py`) wrap the pretrained models behind the
  standard `Forecaster` interface. History is placed on a regular grid so gaps
  stay visible; the models return their own quantile levels, from which P10,
  P50 and P90 are taken. Nothing is trained on the site.
- **Design and test kept apart.** The only design choice, how much history
  each model sees (4, 12 or 48 weeks), was made on Elia load from July to
  December 2024. All three models did best with 48 weeks (Chronos-2 skill
  61.2% over persistence, TimesFM 2.5 57.7%, TimesFM 3.0 64.1%). The test
  periods were then run once.
- **Calibration.** The same conformal calibration as notes 1 and 2: each
  day's band is adjusted using only the previous 28 days' misses.
- **Contamination check.** Before scoring, each dataset was checked against
  the models' published pretraining corpora. The Spanish series of note 1 is
  in GiftEvalPretrain ("spain", 35,064 hourly values from 1 January 2015,
  identical to the study data). Elia data is not in the published corpora.
  The test period starts in January 2025; Chronos-2 was released in October
  2025, so for most of the period the guarantee rests on the corpus check,
  not on dates.
- **Compute.** On a 4-core CPU with no GPU, Chronos-2 with 48 weeks of
  context took about 0.6 seconds per site-day forecast when run alone (the
  design run). The 85-minute figure in the raw load results reflects two
  studies sharing the CPU and is not representative. TimesFM 2.5 took about
  1 second per forecast. A thousand sites a day is minutes of CPU.

## Limitations

- **National load is smooth.** A single tower, branch or mini-grid is far
  noisier. The ranking may change on site data; that is what pilots decide.
- **One dataset, one country.** The accuracy result rests on Belgian load.
  Spanish data could not be used (see contamination).
- **The value test is short.** 130 winter days. Treat its percentages as
  indicative.
- **Explainability.** A GBM's forecast can be traced to features; a
  pretrained model's cannot. For an advisory product this matters less than
  calibration, but operators will ask.

## Reproduce

```bash
uv sync --extra foundation
uv run python examples/foundation_study.py design --load-csv ods001.csv --out results/
uv run python examples/foundation_study.py load   --load-csv ods001.csv --out results/
uv run python examples/foundation_study.py value  --load-csv ods001.csv \
    --solar-csv ods032.csv.gz --out results/
uv run python examples/foundation_study.py short  --load-csv ods001.csv --out results/
```

Full outputs are in [`results/`](results/): `foundation_design`,
`foundation_load`, `foundation_value` and `foundation_short`, as markdown and
JSON.
