# First pilot: the to-do list

What is left before, and during, the first pilot. Every step is written out,
click by click, in **docs/pilot/launch-guide.md**; the documents it needs are
in **docs/pilot/**. Tick items off as they are done.

Last updated: 5 October 2026.

## Built and ready

- Daily plans the evening before, on WhatsApp and email, in English or
  Nigerian Pidgin per person; replies of 1 and 2, the "why not?" question,
  STOP.
- Never silent: "no plan today, run as usual" when data is poor or planning
  fails, and the 19:30 safety net for anything the daily job missed.
- Scoring, model monitoring, fuel reconciliation, data health.
- The shadow-weeks verdict (`shadow-review`), the pilot savings report
  (`report`), both also in the dashboard and the API; the supervisors' Monday
  summary.
- `site check` for portfolio files, `examples/push_csv.py` for loading
  history, `replies` for checking answers.
- Staging and production on Render, created by the blueprint with their
  databases, in Frankfurt; migrations, logs, Sentry, uptime, heartbeat,
  tested backups, operator keys; published numbers locked by tests.
- The runbook (`docs/runbook.md`).

## Week 1: accounts (launch guide, parts 1 and 2)

- [ ] Meta business portfolio created; **verification submitted** with the CAC
      certificate and an address document.
- [ ] Meta app created; the test number sends you "Hello World"
      (`whatsapp-test`).
- [ ] Templates **submitted**: `vaticore_daily_plan`, `vaticore_weekly_summary`.
- [ ] Payment method on the WhatsApp account.
- [ ] Render account, card, and the **blueprint applied**; staging `/ready`
      answers "ready"; production deployed by hand.
- [ ] Sentry project; DSN on every service; test message received.
- [ ] Better Stack: uptime monitor on production `/ready`; heartbeat on the
      daily job.
- [ ] Email provider with SPF and DKIM; `email-test` received (optional).

## Week 1: the operator (launch guide, part 4)

- [ ] Kickoff meeting: pilot and control sites, measurement and success
      threshold, data access, people, dates. Note sent the same day.
- [ ] Draft pilot agreement (`docs/pilot/pilot-agreement.md`) and data
      processing agreement (`docs/pilot/data-processing-agreement.md`) to a
      Nigerian lawyer, then to the operator.

## Week 2: rehearsal and preparation (launch guide, parts 3 and 4)

- [ ] Meta webhook pointed at staging.
- [ ] One real site: `site check` clean, history loaded, `health` clean.
- [ ] A plan on your phone with the right times; a 2 and its reason recorded
      (`replies`); the safety net finds nothing missed.
- [ ] Next day: `score`, `scorecard`, `shadow-review`, `summary`, `report` all
      run; the dashboard's Pilot report view opens.
- [ ] Every pilot and control site's onboarding form
      (`docs/pilot/site-onboarding-form.md`); the full portfolio passes
      `site check`.
- [ ] Technician tests (`docs/pilot/technician-test.md`); Pidgin wording
      checked by a native speaker; Pidgin template submitted if wanted.
- [ ] Consent from every recipient (`docs/pilot/privacy-and-consent.md`);
      privacy notice on vaticore.com.
- [ ] Agreements signed. NDPC registration question answered by the lawyer.
- [ ] Real sender number and display name approved; permanent token on every
      service.

## Weeks 3 to 6: shadow (launch guide, part 5)

- [ ] Production running with the full portfolio; recipients file with
      Vaticore staff only. First shadow day noted.
- [ ] Every Monday: `health`, `scorecard`, `shadow-review --days 7`,
      `monitor`; fixes logged.
- [ ] Week 4: data quality check-in with the operator.
- [ ] End of week 6: `shadow-review --days 28`; go/no-go meeting; decision
      confirmed in writing.

## Week 7 onward: live

- [ ] Meta webhook pointed at production.
- [ ] Real recipients file on the sending services; Pidgin switched on if
      approved (`VATICORE_WHATSAPP_EXTRA_TEMPLATES`).
- [ ] First evening watched; two technicians asked; operator called on day 2.
- [ ] Monthly: `report` sent and talked through.

## Later, not for the first pilot

- A Hausa phrase table (Meta supports Hausa templates), when a translator
  provides one.
- Google or Microsoft sign-in for the dashboard (keys work today).
- The paid Open-Meteo key: only once the weather model beats the current one
  on the pilot's own scored days.
- New models, a React front end.
