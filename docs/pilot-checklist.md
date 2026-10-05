# First pilot: the to-do list

What is left before, and during, the first pilot. "You" means steps that need
Vaticore's accounts, money, signatures or the operator; "build" means code
still to write. Tick items off in the pull request that does them.

Last updated: 5 October 2026.

## Already in place

- Daily plans for every site, the evening before, on WhatsApp and email, with
  replies of 1 and 2, STOP, and a "why not?" question after a 2.
- Scoring of every finished day; model monitoring with fallback; fuel
  reconciliation; data health reports.
- The pilot savings report (`report`) and the supervisors' Monday summary
  (`summary`); how to measure a pilot: `docs/pilot-measurement.md`.
- Staging and production on Render, migrations, logs, error tracking, uptime
  check, heartbeat, tested backups, operator keys, and published numbers
  locked by tests.
- The daily jobs sized for Chronos-2 (2 GB instances).

## 1. This week: accounts that take time (you)

- [ ] **Meta business verification** for Vaticore Ltd (a day to two weeks).
      `docs/whatsapp-setup.md`, step 1.
- [ ] **Submit both WhatsApp templates** for approval:
      `vaticore_daily_plan` and `vaticore_weekly_summary`. The exact text and
      sample values are in `docs/whatsapp-setup.md`, step 3. Approval can be
      refused; a small wording change usually fixes it.
- [ ] **Sender number** (a SIM not on WhatsApp) and a **permanent token**.
      `docs/whatsapp-setup.md`, steps 4 and 5.
- [ ] **Render:** create three Postgres databases (staging, production, and a
      small empty one for the restore drill), then **New > Blueprint** from
      `render.yaml`. Secrets per service are listed in `DEPLOY.md`.
- [ ] **Sentry** project (`VATICORE_SENTRY_DSN`), an **uptime monitor** on
      `https://<api>/ready`, and a **heartbeat** for the daily job
      (`VATICORE_HEARTBEAT_URL`). `docs/operations.md`.
- [ ] **SMTP provider** if anyone wants plans, summaries or the pilot report
      by email.

## 2. Next week: rehearse on staging (you, with me if you like)

With one real site's export, before any operator staff are involved:

- [ ] Upload a `portfolio.toml` with that site, and a `recipients.toml` with
      only your own number (`weekly_summary = true`), as secret files on the
      staging services.
- [ ] Load its history: a sources file (`docs/data-connectors.md`) and
      `python -m vaticore.pipeline ingest`, or
      `python examples/ingest_csv.py export.csv --operator ... --site ...`.
- [ ] `python -m vaticore.pipeline health --days 60`: fix what it flags
      (timezone, gaps, stuck meters) before going further.
- [ ] `python -m vaticore.pipeline run --channel whatsapp --recipients ...`:
      the plan reaches your phone. Reply 2, answer the "why", and check the
      reply and reason in the store (`feedback`).
- [ ] Next day: `score`, then `scorecard`, then
      `summary --channel whatsapp --week-start <last Monday>`, then `report`
      with a short baseline and pilot. All four should run without errors.
- [ ] Check the alerts: the uptime monitor, the heartbeat and Sentry each
      fire once (stop the API briefly; skip a run).

## 3. Before the pilot starts: the operator (you)

- [ ] **Pilot agreement**: advisory only, in writing; the measurement choices
      from `docs/pilot-measurement.md` (pilot sites, control sites, baseline
      and pilot periods, prices, what counts as success).
- [ ] **Data protection** under the Nigeria Data Protection Act 2023: a data
      processing agreement with the operator, a short privacy notice for the
      people who receive plans, and a retention period for readings and
      replies. Consider whether Vaticore needs to register with the NDPC.
- [ ] **Recipients' consent**, recorded in `consent_note` for each person.
- [ ] **Site details** for every pilot and control site: battery size,
      generator rating, tank size, diesel price, tariff band, the outage cost
      the operator uses, and the monitoring platform login.
- [ ] **Talk to two or three technicians** with a printed sample plan: is it
      clear in ten seconds? Which language do they want?

## 4. The shadow weeks (you run it; I can review the numbers)

- [ ] Four weeks with plans made, stored and scored but **not sent**. This is
      the baseline period of the report.
- [ ] Weekly: `health`, `scorecard`, `monitor`. Fix data problems as they
      appear.
- [ ] Go or no-go at the end: are the forecasts better than persistence on
      these sites, and would following the plans have saved diesel? If not,
      find out why before sending anything.

## 5. Still to build (me, in this order)

- [ ] **A message when there is no plan**: "no plan today, run as usual", so
      silence never leaves a technician guessing.
- [ ] **Pidgin template** (and Yoruba or Hausa if the technicians ask), with
      the language chosen per recipient.
- [ ] **Adding a site without editing secret files**: a `site check` command
      that validates a site's details and a one-page onboarding form.
- [ ] **Runbook**: who is alerted when the 18:00 run fails, how to rerun one
      site, what to tell the operator.
- [ ] **The pilot report in the dashboard and the API**, for the operator to
      open themselves.
- [ ] **A go/no-go summary of the shadow weeks**: forecast accuracy against
      persistence and the value on offer, per site, on one page.

## Later, not for the first pilot

- Google or Microsoft sign-in for the dashboard (keys work today).
- The paid Open-Meteo key: only once the weather model beats the current one
  on the pilot's own scored days.
- New models, a React front end.
