# First pilot: the to-do list

What is left before, and during, the first pilot. Every step is written out,
click by click, in **docs/pilot/launch-guide.md**; the documents it needs are
in **docs/pilot/**. Tick items off as they are done.

Last updated: 6 October 2026.

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

## Week 1: the domain (docs/domain.md)

- [ ] Cloudflare: two-factor on, auto-renew on, DNSSEC on, SSL Full (strict).
- [ ] Company details from Companies House (name, "registered in England and
      Wales", number, registered office) on the website footer and privacy
      page, in `VATICORE_EMAIL_LEGAL_FOOTER`, and in your email signature.
- [ ] Website live on Cloudflare Pages at vaticore.co.uk and www.
- [ ] Email Routing: hello@, privacy@, tech@, ops@ forward to your inbox.
- [ ] Brevo: domain authenticated (one combined SPF record, DKIM, DMARC);
      Gmail can send as hello@vaticore.co.uk.
- [ ] Meta domain verification TXT record added.
- [ ] After the blueprint: api, app, api-staging and app-staging CNAMEs
      (DNS only); `https://api.vaticore.co.uk/ready` answers "ready".

## Week 1: accounts (launch guide, parts 1 and 2)

- [ ] Meta business portfolio created; **verification submitted** (United
      Kingdom) with the Companies House certificate and an address document.
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
      processing agreement (`docs/pilot/data-processing-agreement.md`) to a UK
      commercial lawyer (with a Nigerian lawyer's read of the data protection
      clauses), then to the operator.
- [ ] ICO data protection fee paid; registration number on the privacy page.
- [ ] Accountant asked about invoicing a Nigerian customer (UK VAT, Nigerian
      withholding tax).

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
      privacy notice on vaticore.co.uk.
- [ ] Agreements signed. NDPC registration question answered by the lawyer or
      DPCO.
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
