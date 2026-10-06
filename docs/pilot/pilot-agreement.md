# Pilot agreement: template

A starting draft for the pilot agreement between Vaticore Ltd and the
operator. **It is not legal advice.** Have a commercial lawyer review
it before signing (one qualified in Nigerian law, since the operator and the
sites are in Nigeria); budget one or two hours of their time. If Vaticore Ltd
is a UK company, ask them about the governing-law clause and about invoicing
a Nigerian customer from the UK. Square brackets are
for you to fill in. Keep it short: a pilot agreement that takes two months to
sign has already cost the pilot.

---

**PILOT AGREEMENT**

This agreement is made on [date] between:

**Vaticore Ltd**, a company registered in [Nigeria with RC number / England and
Wales with company number] [number], whose registered office is at [address]
("Vaticore"); and

**[Operator legal name]**, RC number [number], whose registered office is at
[address] (the "Operator").

## 1. What the pilot is

1.1 Vaticore will provide daily energy plans for the Operator's sites listed
in Schedule 1 (the "Pilot Sites"): when to run the generator, rely on the
grid or hold battery charge, based on forecasts of each site's load, solar
output and grid supply.

1.2 Plans are delivered to the people the Operator nominates, on WhatsApp
and/or email, each evening for the following day.

1.3 The pilot has two phases:

- **Shadow phase**, [4] weeks from [date]: plans are made and scored but not
  sent to site teams.
- **Live phase**, [8] weeks from the end of the shadow phase: plans are sent
  to site teams.

1.4 The live phase starts only if the shadow phase meets the criteria in
Schedule 2, or if both parties agree in writing to start anyway.

## 2. Advice only

2.1 **Every plan is a recommendation.** The Operator and its staff decide
whether and how to act on it. Vaticore does not control, and will not
connect to the controls of, any equipment at any site.

2.2 The Operator remains responsible for the safe operation of its sites,
generators, fuel and staff, and for complying with its own procedures and
safety rules, which take precedence over any plan.

2.3 Vaticore will state in every plan that it is advice only, and will say
plainly when data is missing or a plan could not be made.

## 3. What each party provides

3.1 **Vaticore** provides the forecasts, plans, delivery, the weekly summary
for supervisors, the monthly pilot report, and a named contact for the pilot.

3.2 **The Operator** provides:

- read-only access to each Pilot Site's monitoring data (load, generator
  output, grid on/off, battery charge, tank level where available), for at
  least [8] weeks of history and for the pilot's duration;
- the site details in Schedule 1 (equipment sizes, diesel price, outage cost);
- records of diesel deliveries to Pilot Sites during the pilot;
- a pilot lead, and the names and contacts of the people to receive plans,
  each of whom has agreed to receive them;
- [optional] [N] comparable sites as control sites, which run as usual and
  receive no plans.

## 4. How results are measured

4.1 Results are measured as described in Schedule 2, agreed before the pilot
starts. Neither party may change the measurement method after the shadow
phase begins without the other's written agreement.

4.2 Vaticore will share the monthly pilot report with the Operator, including
its method, every site's figures, and anything that weakens the result.

## 5. Fees

5.1 [The pilot is provided free of charge.] / [The Operator will pay
₦[amount] per site per month for the live phase, invoiced monthly, payable
within 30 days.]

5.2 At the end of the pilot, the parties will discuss a commercial agreement
in good faith. Neither is obliged to enter one.

## 6. Data

6.1 Each party keeps ownership of its own data. The Operator's site data
remains the Operator's.

6.2 Vaticore processes personal data (the names and phone numbers of the
people who receive plans) on the Operator's behalf, under the data processing
agreement in Schedule 3, which forms part of this agreement.

6.3 Vaticore may use the Operator's site data, with the Operator's name and
site identities removed and in aggregate with other data, to improve its
models and for research. Vaticore will not publish anything that identifies
the Operator or its sites without the Operator's written consent.

6.4 At the end of the pilot, Vaticore will, at the Operator's choice, return
or delete the Operator's data within [30] days, except where the law requires
it to be kept.

## 7. Confidentiality

Each party keeps the other's confidential information confidential, uses it
only for the pilot, and shares it only with staff and advisers who need it.
This survives the end of the agreement for [2] years.

## 8. Liability

8.1 Because plans are advice only (clause 2), Vaticore is not liable for
decisions the Operator or its staff make, or for any loss from acting or not
acting on a plan, except where caused by Vaticore's fraud, gross negligence
or wilful misconduct.

8.2 Each party's total liability under this agreement is limited to
[₦[amount] / the fees paid under it], except for breach of confidentiality,
breach of the data processing agreement, or fraud.

## 9. Term and ending early

9.1 This agreement runs from signature until the end of the live phase, or
[date] if earlier.

9.2 Either party may end it early with [14] days' written notice, or at once
if the other materially breaches it and does not fix the breach within [14]
days of notice.

## 10. General

Nigerian law governs this agreement. The parties will try to settle any
dispute by discussion between their pilot leads, then senior managers, before
[arbitration in Lagos under the Arbitration and Mediation Act 2023 / the
courts of Lagos State].

Signed for Vaticore Ltd: ______________ Name, title, date

Signed for [Operator]: ______________ Name, title, date

---

## Schedule 1: Pilot Sites

For each site, the details from docs/pilot/site-onboarding-form.md: site ID
and name, location, equipment, diesel price, outage cost, monitoring platform.
Mark control sites as such.

## Schedule 2: How the pilot is measured

From docs/pilot-measurement.md, filled in:

- **Pilot sites:** [list]. **Control sites:** [list, or "none"].
- **Shadow phase:** [dates]. It is also the baseline period.
- **Live phase:** [dates].
- **Measured:** litres of diesel per day at each site, from generator output
  readings and the generator's fuel curve; plans followed (replies 1 and 2)
  and reasons; modelled value of following the plans; fuel checks.
- **Method:** [difference in differences against the control sites / before
  and after, with grid supply and load shown alongside].
- **Go criteria for the live phase:** each site with at least 20 scored days
  in the shadow phase, forecast at least 10% better than persistence, forecast
  range holding 70 to 90% of hours, and following the plans would have saved
  money (Vaticore's `shadow-review`).
- **Success:** [e.g. pilot sites burn at least 10% less diesel than control
  sites over the live phase, with no more outage hours].

## Schedule 3: Data processing agreement

Attach docs/pilot/data-processing-agreement.md, filled in.
