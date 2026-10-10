# Research note 4: diesel against how sites run today

**Question.** Against how sites actually run their generators, how much
diesel does Vaticore save, with no more outages?

**Answer, on a held-out year (2018).**

- **Off-grid telecom tower:** Vaticore's plan used **19.6% less diesel than
  the best-run practice** and **30.4% less than an evening timer**, with no
  outages. Most of that comes from one setting Vaticore recommends: run the
  generator hard, then off. The setting alone, with no forecast, saved
  **24.7%**, close to what a perfect forecast allows (25.2%).
- **Tower on a weak grid:** running the generator **whenever the grid is
  off** (the commonest practice) burned more than six times the diesel of
  letting the battery go first. Battery first cut diesel by **83.5%**;
  Vaticore's plan, which works battery first, by **82.6%**. Running the
  generator hard does not help here: the grid refills the battery for free.
- **Solar mini-grid:** diesel fell only **2.0%**, but outage hours fell
  **from 94 to 30**. On a mini-grid the forecast mainly buys reliability.

Note 1 compared forecasts against a planner that was already sensible, which
is why it found more outages avoided rather than less diesel. This note
compares against what sites do.

## Sites

Three sites, shaped from real hourly demand and solar (ENTSO-E Spain, the
data of note 1). Results: `results/diesel_test_*.md` and `.json`.

| Site | Load | Solar | Battery | Generator | Grid |
|---|---|---|---|---|---|
| Mini-grid | real demand shape, 120 kW peak | 200 kWp | 600 kWh, 150 kW | 100 kW | none |
| Off-grid tower | 3.3 kW average (the real shape flattened to plus or minus 15%) | 8 kWp | 20 kWh, 10 kW | 16 kW | none |
| Tower on a weak grid | as above | 8 kWp | 20 kWh, 10 kW | 16 kW | 10 kW, on 63% of hours, **synthetic pattern** |

Every generator has a 2 hour minimum run, a 30% minimum load and the HOMER
fuel curve (0.08145 L/h per kW rated plus 0.246 L/kWh). **The weak grid is
synthetic:** a seeded random process, failing more often in the evening;
there is no public hourly record of a Nigerian grid. Its outages are random,
so no forecast can foresee them, which is the hardest case for planning.

## Practices compared

Every policy keeps a real-time backstop: if the battery is about to run out,
the generator is started whatever the plan said. Outages are therefore
comparable, and the litres are a fair comparison.

| Policy | What it does |
|---|---|
| Today: start when the battery runs out | A well-run site: an automatic start or an attentive technician; the generator follows the load. The reference, and the hardest to beat |
| Today: evening timer | Generator 18:00 to midnight every day, plus the backstop |
| Today: generator whenever the grid is off | An automatic transfer switch (grid site only) |
| Vaticore plan, today's planner | Day-ahead plan, hour by hour, generator following the load (the planner of notes 1 to 3) |
| Vaticore plan, run hard | The same, with the charger set to load the generator to 80% and charge the battery (`generator.charge_setpoint`) |
| Vaticore plan, run hard + look ahead | The same, planning each day at once for the fewest litres (`look_ahead`) |
| Charger setting alone, no forecast | Today's best practice with the 80% setting: what the setting gives without any forecast |
| Perfect forecast, run hard + look ahead | The bound |

Vaticore plans on day-ahead forecasts of net load (quantile GBM, conformal
calibration) and, on the grid site, of grid availability.

## Design year, then one test

The protocol of `README.md`: choices on 2017, one run on 2018.

- **Plan quantile: P50.** On the design year a cautious P90 plan used about
  11% more diesel on the mini-grid and 7 to 10 points less saving on the
  tower; with a real-time backstop, caution is not needed for reliability
  (`results/diesel_design_minigrid.md`, `results/diesel_design_tower.md`).
- **Grid assumption: P50.** Planning on the cautious P10 grid made the plan
  start the generator for outages the battery would have covered: 69% more
  diesel than battery first on the design year, against 13% with P50
  (`results/diesel_design_tower-grid-gridP10.md`, `-gridP50.md`).
- **Setpoint: 0.8.** Higher settings saved a little more on the mini-grid
  (2.8% at 1.0 against 0.5% at 0.8 on the design year) and nothing more on
  the tower. We capped it at 80% of rating, the loading commonly advised for
  a diesel engine's life, before choosing.

## What it means for the advice Vaticore gives

1. **Never start the generator just because the grid failed.** Battery
   first is the single largest saving found here.
2. **At an off-grid site with a battery, set the charger to run the
   generator hard, then off.** A generator large for its load wastes most
   of its fuel idling; at 80% it burns about 0.35 L per kWh, at 20% about
   0.65. This cut a quarter of the tower's diesel.
3. **At a grid site, do not run it hard.** Charge only what the gap needs;
   the grid will refill the battery.
4. **The daily plan matters where people start the generator by hand.** It
   came within about 5 points of the best rule at the off-grid tower, with
   no outages, and on the mini-grid it cut outage hours by two thirds.
5. **The look-ahead planner added little:** 0.4 points on the off-grid
   tower, and it was slightly worse on the other two. It stays available (`look_ahead`), off
   by default. A negative result, published as such.

These are rules Vaticore can check against each site's own data and
recommend: that is where most of the diesel is.

## Limitations

- **Shaped sites.** Spanish demand and solar, scaled; a tower's load
  flattened from a national shape; a synthetic grid. Real sites differ, and
  only a pilot against control sites measures them.
- **The fuel curve is linear and generic.** Real engines vary; the
  part-load penalty is the industry standard but not each engine's.
- **Wear is not costed.** Running hard halves generator hours and starts on
  the tower, which helps the engine, but cycles the battery more, which a
  lead-acid battery feels. Neither is priced here.
- **One year, one seed** for the synthetic grid.
- **The best-run reference is hard to beat on purpose.** Many sites run
  worse (timers, an automatic transfer switch); their savings would be
  larger, as the timer and transfer switch rows show.

## Reproduce

```bash
curl -L -o energy_dataset.csv \
  https://raw.githubusercontent.com/unit8co/darts/master/datasets/energy_dataset.csv
uv run python examples/diesel_study.py energy_dataset.csv --phase design --out results/
uv run python examples/diesel_study.py energy_dataset.csv --phase design --sites tower-grid \
  --setpoint 0.8 --plan-quantile 0.5 0.9 --grid-quantile 0.5 --out results/
uv run python examples/diesel_study.py energy_dataset.csv --phase test --setpoint 0.8 \
  --plan-quantile 0.5 --grid-quantile 0.5 --out results/
```

The first design run used the cautious P10 grid plan (the product default
then) and is published as `diesel_design_tower-grid-gridP10`. Each site takes
about ten minutes. `examples/verify_results.py diesel_test_tower ...` reruns
a published result and lists any number that moved.
