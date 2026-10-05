# Measuring a pilot: agree it before it starts

A pilot ends with one question from the operator: did it save us money? The
answer is only believed if the way of measuring it was agreed **before** the
first plan was sent. This page is that agreement. Fill in the choices with the
operator at kick-off and put them in the pilot agreement.

## What is measured

| | How | Strength |
|---|---|---|
| **Diesel burned** | Litres per day from each generator's recorded output and its fuel curve (the same arithmetic as fuel reconciliation), on days with at least 20 hours of generator readings | The headline. Measured at the site, not modelled |
| **Plans followed** | Replies of 1 and 2 to each plan, and the reason given after a 2 | Shows whether savings came from the plans or from something else |
| **Value of following** | Each finished day replayed on what actually happened, with that day's plan and with "plan from yesterday"; litres, outages and money (`docs/pipeline.md`) | Modelled. Separates the value taken (days followed) from the value on offer |
| **Fuel checks** | Short deliveries, fuel leaving the tank while the generator was off, losses the generator cannot explain (`docs/fuel.md`) | Measured where there is a tank sensor; often the first money found |

## The choices to agree

1. **Pilot sites.** Which sites receive plans. Every site needs generator
   output readings (and grid availability where there is a grid), or its
   diesel cannot be measured.
2. **Baseline period.** At least four weeks just before plans are sent. Run
   the system in shadow over it: plans are made, stored and scored, never
   sent (no recipients, or `run` without a channel). This gives the "before"
   numbers, and the first evidence on the operator's own sites.
3. **Pilot period.** At least eight weeks of plans sent. Shorter pilots are
   dominated by noise: a week of good grid supply moves diesel use more than
   any plan.
4. **Control sites (strongly recommended).** Two to five comparable sites (same
   region, same kind of site, similar grid supply) that keep running as usual
   and receive no plans. The report subtracts their change from the pilot
   sites' change, which removes what both groups share: grid supply, rain,
   diesel price, the season. Without control sites the report shows before
   against after and warns when grid supply or load moved enough to explain
   the change. Choose control sites before the pilot, by a rule written down
   (for example alternate sites on the operator's list), never after seeing
   results.
5. **Prices.** The diesel price per litre and the cost of an outage hour per
   site, from the operator, recorded in the portfolio file. The report uses
   them as recorded.
6. **What counts as success.** For example: "pilot sites burn at least 10%
   less diesel than control sites over the pilot period, with no more outage
   hours". Write the number down.

## Running it

```bash
# Monthly, or at the end of the pilot:
uv run python -m vaticore.pipeline report --operator example-towerco \
    --baseline 2026-09-01:2026-09-30 --pilot 2026-10-01:2026-11-30 \
    --control kog-rur-0077 --control kog-rur-0081 \
    --out reports/ --email sponsor@example.com
```

The report is Markdown (to read, or paste into a deck) with a JSON record
beside it. It states its method, shows every site before and during, and lists
what to read with care: too few readings, a control site that was sent plans,
grid supply that changed between the periods.

Every Monday, supervisors with `weekly_summary = true` in the recipients file
get the week in six lines on WhatsApp or email (`summary`), so problems
(plans not followed for lack of diesel, missing readings) are fixed during the
pilot rather than found at the end.

## Reading the result honestly

- A plan that is not followed saves nothing. Low adoption with high value on
  offer is a training or trust problem, not a forecasting one; the reasons
  after a 2 say which.
- A cautious plan can burn more diesel to avoid outages. The report shows
  litres, outages and net money separately, so that trade is visible.
- Litres from the fuel curve are estimates of what the generator burned.
  Where the site has a tank sensor, the fuel checks compare them with what
  left the tank.
- Advisory only: every plan was a recommendation, and the site team decided.
