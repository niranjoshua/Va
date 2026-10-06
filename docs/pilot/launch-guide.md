# Launching the first pilot: the step-by-step guide

Everything between "the software is ready" and "the first plan reaches a
technician's phone", in the order to do it. Each step says where to click,
what to type, what you should see, and what usually goes wrong.

Screens and prices change. These steps were checked in October 2026; where a
label differs, look for the nearest equivalent. Prices are approximate:
check each provider's pricing page before quoting anyone.

## The plan at a glance

| Week | You | With the operator |
|---|---|---|
| 1 | Domain: website, email, DNS (docs/domain.md). Start Meta verification (part 1). Set up Render, Sentry, Better Stack, email (part 2). Submit the WhatsApp templates. | Kickoff meeting. Send the draft pilot agreement and data processing agreement. Ask for site details and monitoring access. |
| 2 | Rehearse on staging with one real site (part 3). | Site details forms (part 4). Technician tests. Consent messages. Lawyer reviews, then signatures. |
| 3 to 6 | Shadow weeks: plans made and scored, not sent (part 5). Monday checks. | Week 4 check-in on data quality. |
| 7 | Go/no-go review. Go live on the GO sites. | Go/no-go meeting; decision in writing. |
| 7 to 14 | Live phase. Daily glance, Monday summary, monthly report. | Monthly report and call. |

Meta verification and template approval are the slowest steps (days, rarely
two weeks), so part 1 starts on day one, in parallel with everything else.

## What you need before you start

- **Company documents:** Vaticore Ltd is registered at Companies House, so:
  the **certificate of incorporation** (download a copy free from the
  company's page on find-and-update.company-information.service.gov.uk, or
  use the one emailed at incorporation), and a document showing the
  **registered office address** in the company's name: a utility bill, bank
  statement or HMRC letter under three months old. If the registered office
  is a formation agent's or accountant's address, use a document addressed to
  the company there (a bank statement usually is).
- **The domain, vaticore.co.uk,** set up first (docs/domain.md): the website
  live with the company's registered name, number, place of registration and
  registered office in its footer, exactly as on Companies House (UK law
  requires it, and Meta compares them), and email at the domain (for example
  hello@vaticore.co.uk).
- **A new number that has never been on WhatsApp:** this becomes Vaticore's
  sender. A Nigerian SIM (MTN, Airtel, Glo, 9mobile) reads as local to site
  teams; a UK SIM or virtual number works just as well and is easier to keep
  from the UK. It only needs to receive one SMS or call. Meta prices messages
  by the recipient's country, so the sender's country does not change cost.
- **The company's card** (Render and Meta bill in dollars; a UK business
  card is fine).
- **Your laptop** with the repository checked out and `uv` installed
  (`uv sync --all-extras` once).
- **About £55 to £60 (about $75) a month** of running costs during the pilot;
  the breakdown is in part 2.

---

## Part 1: WhatsApp (Meta)

Time: an hour of your work, then waiting. Cost: about $0.007 per plan
delivered in Nigeria (Meta's utility rate from 1 October 2026), so 20 people
for 30 days is about $4 a month.

### 1.1 Create the business portfolio and start verification (day 1)

1. Go to **business.facebook.com** and sign in with your personal Facebook
   account (Meta requires a real person as the admin; nobody else sees it).
2. **Create a business portfolio:** name `Vaticore Ltd` (exactly as on the
   certificate of incorporation), your name, and your company email.
3. Open **Settings** (the gear) > **Business info**. Fill in the legal name,
   address, phone and website (`https://vaticore.co.uk`) exactly as on the
   certificate.
4. Open **Security Center** > **Start verification**.
   - Choose **United Kingdom**, and enter the registered name, the registered
     office address and a phone number, exactly as on Companies House. Meta
     may also find the company in the Companies House register itself.
   - When asked how to confirm, choose **domain verification** if you can
     add a DNS record at your domain host (most reliable), or **email** to
     your company address.
   - Upload the **certificate of incorporation** and the **address document**.
   - For domain verification, Meta gives you a TXT record: add it in
     Cloudflare > DNS (docs/domain.md, step 5).
5. You will see "In review". Meta says a decision can take up to 14 working
   days; often it is 2 to 5. Carry on with everything else meanwhile.

**If it is rejected:** the reason is shown. The usual causes are a name that
does not match the document exactly (Ltd against Limited, for example), a
website without the address, or a blurry document. Fix and resubmit.

### 1.2 Create the app and try the test number (day 1)

1. Go to **developers.facebook.com** > **My Apps** > **Create app**.
2. Choose the use case for WhatsApp ("Connect with customers through
   WhatsApp"), name it `Vaticore`, and link it to the **Vaticore Ltd**
   business portfolio.
3. In the app, open **WhatsApp** > **API Setup**. Meta has created a free
   **test number**. Note:
   - the **Phone number ID** (a long number, not the phone number itself);
   - the **temporary access token** (valid 24 hours).
4. Under **To**, add your own WhatsApp number and enter the code it receives.
   The test number can only message up to five numbers you add here.
5. On your laptop, in the repository, create a file `.env` (it is
   git-ignored) with:

   ```bash
   VATICORE_WHATSAPP_TOKEN=the-temporary-token
   VATICORE_WHATSAPP_PHONE_NUMBER_ID=the-phone-number-id
   ```

6. Run:

   ```bash
   uv run python -m vaticore.pipeline whatsapp-test --to +234XXXXXXXXXX
   ```

   You should get Meta's "Hello World" on your phone within seconds. If not,
   the command prints Meta's error with a hint (expired token, number not
   added as a test recipient).

### 1.3 Submit the message templates (day 1 or 2)

A business may message someone first only with a template Meta approved.
Vaticore needs two now and a third after the technician tests.

1. Open **WhatsApp Manager** (from business.facebook.com > the WhatsApp
   accounts entry, or business.facebook.com/wa/manage) > **Message
   templates** > **Create template**.
2. For each template below: **Category: Utility**, the **name** exactly as
   given, **language** as given, paste the **body** exactly, and fill the
   **sample values** Meta asks for (one per variable). No header, no buttons.

| Template | Language | Body and samples |
|---|---|---|
| `vaticore_daily_plan` | English | docs/whatsapp-setup.md, step 3 |
| `vaticore_weekly_summary` | English | docs/whatsapp-setup.md, "The weekly summary template" |
| `vaticore_daily_plan_pcm` | English (see below) | docs/whatsapp-setup.md, "The Pidgin template". Submit after a native speaker and two technicians have read it (part 4.6). |

3. Submit. Utility templates are often approved within minutes, sometimes a
   day. You get an email and the status turns **Active**.

**Why Pidgin is submitted as English:** Meta's template languages include
Hausa but not Nigerian Pidgin, Yoruba or Igbo. Pidgin is submitted under
English with its own template name; Vaticore picks the template by each
person's language.

**If a template is rejected:** the reason is shown. Keep it plainly
operational (no "offer", "free", "discount"), keep the sample values
realistic, and resubmit with a small change. If Meta re-categorises it as
Marketing, appeal: it is a daily operational instruction the person agreed to
receive.

### 1.4 Add a payment method (day 1 or 2)

In **WhatsApp Manager** > **Account tools** > **Payment methods** (or
Business settings > Billing and payments), add the dollar card. Template
messages need a valid payment method on the WhatsApp account.

### 1.5 Your real sender number (after verification)

1. Put the new SIM in a basic phone (it only needs to receive one SMS or
   call). Do **not** install WhatsApp on it.
2. In **WhatsApp Manager** > **Phone numbers** > **Add phone number**:
   - **Display name:** `Vaticore`. Meta reviews it; it must match your
     business name or brand.
   - **Category:** Professional services or Utilities.
   - Verify by SMS or voice call.
3. Note the new number's **Phone number ID**. It replaces the test one
   everywhere.

Until business verification is approved, Meta limits how many people a new
number can message a day; a small pilot fits within it, but verify early.

### 1.6 A permanent token (after the app exists)

The 24-hour token is only for testing. For the services:

1. **Business settings** > **Users** > **System users** > **Add**. Name it
   `vaticore-pipeline`, role **Admin**.
2. **Assign assets:** the `Vaticore` app (full control) and the WhatsApp
   account (full control).
3. **Generate new token:** choose the app, tick
   `whatsapp_business_messaging` and `whatsapp_business_management`, and
   choose **Never** for expiry.
4. Copy it straight into your password manager. You will paste it into
   Render in part 2. Never email it, put it in a document or a chat, or
   commit it.

### 1.7 The webhook (in part 3)

Meta sends delivery receipts and replies to one web address. You set it up
in part 3, pointing first at staging for the rehearsal, then at production
when you go live.

---

## Part 2: Render, Sentry, Better Stack and email

Time: about three hours. Cost per month, roughly:

| Item | Plan | About |
|---|---|---|
| 4 web services (API and dashboard, production and staging) | Starter, $7 each | $28 |
| Production database | Basic 1 GB | $19 |
| Staging database and restore scratch database | Basic 256 MB, $6 each | $12 |
| 5 scheduled jobs | Billed by the minute they run; the daily jobs run a few minutes on Standard | $3 to $8 |
| Sentry, Better Stack, Brevo | Free plans | $0 |
| **Total** | | **about $65 to $70** |

### 2.1 Render account

1. Go to **render.com** > **Get started** > **Sign up with GitHub**, using
   the GitHub account that owns `niranjoshua/Vaticore`.
2. When Render asks for repository access, allow it for the `Vaticore`
   repository.
3. **Workspace settings** > **Billing**: add the payment card.

### 2.2 Make the secrets you will need

On your laptop, make two admin tokens (one for production, one for staging)
and a webhook verify token:

```bash
uv run python -c "import secrets; print('prod admin  ', secrets.token_urlsafe(32))"
uv run python -c "import secrets; print('stage admin ', secrets.token_urlsafe(32))"
uv run python -c "import secrets; print('verify token', secrets.token_urlsafe(24))"
```

Save all three in your password manager.

### 2.3 Apply the blueprint

1. In Render: **New** > **Blueprint**.
2. Choose the `Vaticore` repository, branch `main`. Name the blueprint
   `vaticore`.
3. Render reads `render.yaml` and lists what it will create: **3 databases**
   (`vaticore-db`, `vaticore-db-staging`, `vaticore-db-restore-scratch`) and
   **9 services** (API, dashboard, daily plans, safety net, weekly summary
   and restore drill for production; API, dashboard and daily plans for
   staging), all in **Frankfurt**.
4. Render then asks for every value marked "sync: false". Fill in what you
   have, and leave the rest blank for now (you can add them later in each
   service's **Environment** tab):

| Variable | Value |
|---|---|
| `VATICORE_API_TOKEN` | the production admin token on production services; the staging one on staging services |
| `VATICORE_WHATSAPP_TOKEN` | the permanent token (part 1.6), or the temporary one for now |
| `VATICORE_WHATSAPP_PHONE_NUMBER_ID` | the test number's ID for now; the real one after part 1.5 |
| `VATICORE_WHATSAPP_APP_SECRET` | App dashboard > App settings > Basic > App secret > Show |
| `VATICORE_WHATSAPP_VERIFY_TOKEN` | the verify token you made |
| `VATICORE_WHATSAPP_EXTRA_TEMPLATES` | blank until the Pidgin template is approved; then `pcm=vaticore_daily_plan_pcm:en` |
| `VATICORE_SENTRY_DSN` | blank; part 2.5 |
| `VATICORE_HEARTBEAT_URL` | blank; part 2.6 |
| `VATICORE_SMTP_*`, `VATICORE_EMAIL_FROM`, `VATICORE_OPS_EMAIL` | blank; part 2.7 |
| `VATICORE_VRM_TOKEN` | only if the operator uses Victron VRM |
| `VATICORE_WEATHER_API_KEY`, `ANTHROPIC_API_KEY` | blank: not needed for the pilot |

5. Click **Apply**. Render creates the databases first, then builds the
   image (10 to 20 minutes the first time; the jobs with the forecasting
   models take longest) and wires each database's address into its services
   automatically.
6. **Staging** deploys by itself. **Production** waits for you: open each
   production service and click **Manual Deploy** > **Deploy latest commit**.
   Start with `vaticore-api`.

**Check:** open `https://api-staging.vaticore.co.uk/ready` (before the DNS records
of docs/domain.md are in, use the service's `onrender.com` address; the exact
address is at the top of the service's page). You should see
`{"status":"ready","problems":[]}`. A 503 that lists "migration(s) pending" means the
pre-deploy step did not run: open the service's **Shell** tab and run
`uv run python -m vaticore.pipeline migrate`.

### 2.4 Secret files

Portfolio, recipients and sources files hold operator data and phone
numbers, so they live only as Render secret files. For each service below:
open it > **Environment** > **Secret Files** > **Add Secret File**, use the
exact file name, and paste the contents.

| Service | portfolio.toml | recipients.toml | sources.toml |
|---|---|---|---|
| vaticore-api, vaticore-dashboard | yes | | |
| vaticore-daily-plans | yes | yes | yes |
| vaticore-safety-net, vaticore-weekly-summary | yes | yes | |
| vaticore-api-staging | yes | yes | yes |
| vaticore-dashboard-staging | yes | | |
| vaticore-daily-plans-staging | yes | yes | yes |

You will fill these with real content in parts 3 and 5. Before uploading any
portfolio, check it on your laptop:
`uv run python -m vaticore.pipeline site check --portfolio path/to/portfolio.toml`.

### 2.5 Sentry: error tracking

1. **sentry.io** > sign up (the free Developer plan is enough for the pilot).
2. **Create project** > platform **Python** (or **FastAPI**) > name it
   `vaticore` > keep "Alert me on every new issue".
3. Copy the **DSN** (Project settings > **Client Keys (DSN)**), a web address
   starting `https://`.
4. Paste it as `VATICORE_SENTRY_DSN` on every service (Environment tab),
   then save; Render redeploys.
5. **Test it** from the `vaticore-api-staging` **Shell**:

   ```bash
   uv run python -c "import os, sentry_sdk; sentry_sdk.init(os.environ['VATICORE_SENTRY_DSN']); sentry_sdk.capture_message('Vaticore test'); sentry_sdk.flush()"
   ```

   The message appears in Sentry within a minute, and you get an email.
6. Install the Sentry app on your phone if you want alerts there.

### 2.6 Better Stack: uptime and the daily heartbeat

One free account covers both (10 monitors and 10 heartbeats on the free
plan).

1. **betterstack.com** > sign up > **Uptime**.
2. **Monitors** > **Create monitor**:
   - Alert us when: **URL becomes unavailable**.
   - URL: `https://api.vaticore.co.uk/ready`.
   - Check frequency: 3 minutes (the free plan's fastest).
   - On-call: you, by email and push (install the Better Stack app). Add SMS
     or a call if you have credit.
3. **Heartbeats** > **Create heartbeat**:
   - Name: `Daily plans`.
   - Expect a heartbeat every **1 day**, grace period **1 hour**.
   - Copy its URL into `VATICORE_HEARTBEAT_URL` on `vaticore-daily-plans`.
   The job pings it when it finishes, and pings `.../fail` if any site
   failed, so you hear about a job that never ran as well as one that broke.
4. Optional: a second monitor on the dashboard,
   `https://app.vaticore.co.uk/_stcore/health`, and one on the website,
   `https://vaticore.co.uk`.

### 2.7 Email (optional, but needed for alerts by email)

Vaticore sends email for plans and summaries (if anyone prefers email), the
pilot report, and alerts to you (`VATICORE_OPS_EMAIL`). Pick one provider:

| Provider | Good when | SMTP settings |
|---|---|---|
| Brevo (free: 300 emails a day) | No company mail provider yet | host `smtp-relay.brevo.com`, port 587; username and password are the SMTP login and key from Brevo > SMTP and API |
| Zoho Mail | Your company mail is on Zoho | host `smtp.zoho.com` (or `smtppro.zoho.com`), port 587, an app-specific password |
| Google Workspace | Your company mail is on Google | host `smtp.gmail.com`, port 587, an app password (needs two-step verification on that account) |

1. In the provider, **authenticate your domain**: it gives you SPF and DKIM
   DNS records to add at your domain host. Without them, mail lands in spam.
2. On `vaticore-daily-plans`, `vaticore-safety-net` and
   `vaticore-weekly-summary`, set `VATICORE_SMTP_HOST`,
   `VATICORE_SMTP_USERNAME`, `VATICORE_SMTP_PASSWORD`,
   `VATICORE_EMAIL_FROM` (for example `Vaticore <plans@vaticore.co.uk>`),
   `VATICORE_OPS_EMAIL` (where alerts go: you) and
   `VATICORE_EMAIL_LEGAL_FOOTER` (the company line UK law requires on
   business emails: docs/domain.md, step 4).
3. Test from a Shell where they are set (or locally with `.env`):
   `uv run python -m vaticore.pipeline email-test --to you@vaticore.co.uk`.

---

## Part 3: The rehearsal on staging

Time: about half a day, plus a check the next day. You need one real site's
details and data, and your own phone. Nobody at the operator gets anything.

### 3.1 Point Meta at staging

1. In the Meta app: **WhatsApp** > **Configuration** > **Webhook** > **Edit**.
2. Callback URL: `https://api-staging.vaticore.co.uk/webhooks/whatsapp`.
3. Verify token: the one you made in part 2.2. It must already be set as
   `VATICORE_WHATSAPP_VERIFY_TOKEN` on `vaticore-api-staging`.
4. **Verify and save**, then under **Webhook fields** subscribe to
   **messages**.

You will move it to production in part 5.

### 3.2 Prepare the three files on your laptop

Keep them in a private folder outside the repository (for example
`~/vaticore-private/`), never in the repository.

- **portfolio.toml:** one `[[site]]` block for the rehearsal site, from its
  onboarding form (docs/pilot/site-onboarding-form.md).
- **recipients.toml:** only you, with consent, the weekly summary on, and the
  language you want to test:

  ```toml
  [[recipient]]
  name = "Rehearsal: <your name>"
  whatsapp = "+234XXXXXXXXXX"
  operator_id = "<operator_id>"
  sites = ["*"]
  consent = true
  consent_note = "Vaticore staff, rehearsal"
  weekly_summary = true
  language = "en"
  ```

- **sources.toml:** only if the site's readings come from a platform Vaticore
  can pull (Victron VRM, or CSV files on a server). For a one-off export,
  skip it and push the file instead (3.5).

Check the portfolio:

```bash
uv run python -m vaticore.pipeline site check --portfolio ~/vaticore-private/portfolio.toml
```

Fix every FAIL. Read every WARN and fix what you can.

### 3.3 Upload them to staging

Upload the files as secret files on `vaticore-api-staging`,
`vaticore-dashboard-staging` (portfolio only) and
`vaticore-daily-plans-staging`, as in part 2.4.

### 3.4 Make an operator key on staging

In `vaticore-api-staging` > **Shell**:

```bash
uv run python -m vaticore.pipeline apikey create --operator <operator_id> --name rehearsal
```

Copy the key it prints (shown once) into your password manager.

### 3.5 Load the site's history

**From a platform (sources file):** in the same Shell,
`uv run python -m vaticore.pipeline ingest`.

**From an export file:** on your laptop, first a dry run, then the real
thing:

```bash
uv run python examples/push_csv.py ~/vaticore-private/export.csv \
    --api https://api-staging.vaticore.co.uk \
    --operator <operator_id> --site <site_id> --key <the key from 3.4> \
    --timezone Africa/Lagos \
    --map "Time=timestamp" --map "Load (kW)=load_kw" --map "Genset (kW)=genset_kw" \
    --dry-run
```

Replace the `--map` names with the export's own column headers. Map only
columns already in kW, percent or litres. Then run it again without
`--dry-run`.

### 3.6 Check the data

In the Shell:

```bash
uv run python -m vaticore.pipeline site check --with-data
uv run python -m vaticore.pipeline health --days 60
```

What you want: no FAIL, history of two weeks or more, generator output
present, grid on/off present for a grid site. Typical fixes: a wrong timezone
in the export (times off by an hour), watts mapped as kW (loads 1,000 times
too big), a stuck meter.

### 3.7 Send yourself a plan

```bash
uv run python -m vaticore.pipeline run --channel whatsapp
```

Within a minute your phone gets tomorrow's plan. Check, carefully:

- the times are in Lagos time and match how the site runs;
- the generator hours and litres are believable for this site;
- the battery start and end are believable.

Anything odd usually traces back to a site detail: run `site check` again and
compare with the nameplate photos.

### 3.8 Reply like a technician

1. Reply **2** to the plan. Within seconds you get the "why not?" question.
2. Reply **B**. You get "Thank you, noted."
3. In the Shell: `uv run python -m vaticore.pipeline replies --days 1`. You
   should see your `not_followed` and the `reason (no_diesel)` against the
   plan's date.
4. Reply **STOP**, then **START**, and check `replies` again: both are
   recorded, and you are opted back in.

### 3.9 Test the safety net

```bash
uv run python -m vaticore.pipeline safety-net --channel whatsapp
```

It should say every site's message was already delivered. That is the
evening job confirming nobody was missed.

### 3.10 The next day

```bash
uv run python -m vaticore.pipeline score
uv run python -m vaticore.pipeline scorecard --days 7
uv run python -m vaticore.pipeline shadow-review --operator <operator_id> --days 7
uv run python -m vaticore.pipeline summary --channel whatsapp --week-start <last Monday>
uv run python -m vaticore.pipeline report --operator <operator_id> \
    --baseline <a week ago>:<4 days ago> --pilot <3 days ago>:<yesterday>
```

All five should run without errors. The summary arrives on your phone once
the weekly template is approved. The review and report will say there is too
little data to judge yet, which is correct.

Then open `https://app-staging.vaticore.co.uk`, sign in with
the operator key, and look at **Site today** and **Pilot report**.

### 3.11 Fire each alert once

On production, once it is deployed:

- **Sentry:** the test message in part 2.5.
- **Uptime:** in Render, **Suspend** `vaticore-api` for five minutes; Better
  Stack alerts within about three minutes. **Resume** it.
- **Heartbeat:** after the first evening the production job runs (part 5.1),
  the heartbeat in Better Stack turns green. If it is still waiting an hour
  after 18:00 Lagos, you get the alert: that is the drill, and the runbook's
  first section is what you would do.

**The rehearsal is done when** a plan reached your phone with the right
times, your 2 and its reason were recorded, the safety net found nothing
missed, and each alert reached you once.

---

## Part 4: The operator side

### 4.1 The kickoff meeting (60 minutes, week 1)

With the operator's pilot sponsor, their power or operations lead, and
whoever runs their monitoring platform. Agenda:

1. **What the pilot is, and is not** (10 minutes): daily advice to site
   teams, never control of equipment; four shadow weeks, then eight live.
2. **Sites** (15 minutes): choose 5 to 15 pilot sites with good monitoring
   data, and 2 to 5 similar control sites. Write down the rule used to choose
   the controls (docs/pilot-measurement.md).
3. **Success** (10 minutes): agree the measurement and the success threshold
   (Schedule 2 of the pilot agreement).
4. **Data** (10 minutes): read-only access to the monitoring platform;
   history available; diesel delivery records.
5. **People** (10 minutes): who receives plans, who gets the weekly summary,
   who the single point of contact is on each side.
6. **Next steps and dates** (5 minutes).

Send a one-page note of what was agreed the same day.

### 4.2 The pilot agreement (weeks 1 to 2)

1. Fill in `docs/pilot/pilot-agreement.md`: parties, company numbers, dates,
   sites, fees, and Schedule 2 from the kickoff.
2. Have it reviewed (one to two hours): a UK commercial lawyer for the
   contract itself, since Vaticore Ltd is an English company, and ideally a
   Nigerian lawyer's quick read of the clauses that touch Nigerian law
   (data protection, any regulated sites). Ask your accountant about invoicing
   a Nigerian customer (VAT, Nigerian withholding tax) before the first
   invoice.
3. Send it to the operator as a Word or PDF document. Expect them to send it
   to their legal team: offer a call to walk through it, since a pilot this
   size should take days, not months.
4. Signatures: both parties, dated. Keep the signed PDF in the pilot folder.

### 4.3 Data protection (weeks 1 to 2)

1. Fill in `docs/pilot/data-processing-agreement.md`, attach it as Schedule 3.
2. **UK:** pay the ICO data protection fee (ico.org.uk, "Pay the data
   protection fee"; a small company is in the lowest tier, a modest annual
   fee). Vaticore processes personal data from the UK, so UK GDPR applies.
3. **Nigeria:** ask the lawyer, or a licensed Data Protection Compliance
   Organisation (DPCO), the questions in the agreement's last section:
   whether Vaticore must register with the NDPC (likely not below 200 people
   in six months, but a foreign company serving Nigerian sites should check),
   and that the transfers to Render in Frankfurt, Meta and Sentry are
   covered.
4. Publish the privacy notice at vaticore.co.uk/privacy (it is already in
   `docs/landing/privacy.html`; fill in the company details).

### 4.4 Consent (week 2)

Follow `docs/pilot/privacy-and-consent.md`: the operator's lead introduces
the pilot, you send each person the consent message, and only people who
reply YES go into the recipients file, with the date and how they agreed.

### 4.5 Site details (week 2)

For every pilot and control site, fill `docs/pilot/site-onboarding-form.md`
with the operator's power lead, with nameplate photos. Build the portfolio
file from the forms, run `site check`, and fix every FAIL before uploading.
Control sites go in the portfolio too: they need readings, but nobody on
them goes in the recipients file.

### 4.6 Technician tests (week 2)

Follow `docs/pilot/technician-test.md` with two or three people who will
receive plans. If they prefer Pidgin, have a native speaker check the
wording in `vaticore/delivery/message.py`, then submit the Pidgin template
(part 1.3) and set those people's `language = "pcm"`.

---

## Part 5: The shadow weeks, the decision, going live

### 5.1 Start the shadow weeks (week 3)

1. Upload the full portfolio and sources files to the production services
   (part 2.4).
2. Upload a recipients file with **only Vaticore staff** on it. Every evening
   you receive each site's plan, exactly as a technician would, and nobody at
   the operator does. Do not reply to them: replies count as adoption.
3. Check the first run: Render > `vaticore-daily-plans` > **Logs** after
   18:00 Lagos. Every site should show `planned` or a stated `no_plan`.
4. Note the first shadow day. It starts the baseline period of the pilot
   report.

### 5.2 Every Monday (30 minutes)

In the `vaticore-api` Shell:

```bash
uv run python -m vaticore.pipeline health --days 7
uv run python -m vaticore.pipeline scorecard --days 7
uv run python -m vaticore.pipeline shadow-review --operator <operator_id> --days 7
uv run python -m vaticore.pipeline monitor --weeks 4
```

Look for sites with data gaps (fix the link with the operator), sites whose
forecast does not beat persistence (check the site details first: a wrong
battery size or generator rating is the usual cause), and plans that read
oddly on your phone. Log what you found and changed.

### 5.3 The go/no-go review (end of week 6)

```bash
uv run python -m vaticore.pipeline shadow-review --operator <operator_id> --days 28 --out reports/
```

Or open the dashboard > **Pilot report** > **Shadow weeks**. The rules were
fixed before the shadow weeks began (Schedule 2): at least 20 scored days,
forecast at least 10% better than persistence, range holding 70 to 90%, and
saving money if followed.

Meet the operator with the one-page review:

- **GO sites** start receiving plans.
- **REVIEW sites:** go through the reasons together, then decide.
- **NOT YET sites:** fix the data, extend their shadow weeks, review again.
- **NO-GO sites:** do not send. Find out why first.

Confirm the decision by email the same day: which sites go live, and from
what date.

### 5.4 Going live (week 7)

1. **Point Meta's webhook at production:**
   `https://api.vaticore.co.uk/webhooks/whatsapp`, with production's
   verify token and app secret set on `vaticore-api` (part 3.1).
2. **Real sender number and permanent token** on every service that has
   `VATICORE_WHATSAPP_TOKEN` (part 1.5 and 1.6).
3. **Recipients:** upload the real recipients file (consented site teams on
   the GO sites, supervisors with `weekly_summary = true`) to
   `vaticore-daily-plans`, `vaticore-safety-net` and
   `vaticore-weekly-summary`.
4. **Pidgin:** if approved, set `VATICORE_WHATSAPP_EXTRA_TEMPLATES` to
   `pcm=vaticore_daily_plan_pcm:en` on the same three services.
5. **First evening:** watch the 18:00 run's logs, and at 19:30 the safety
   net's. Check with two technicians that the message arrived and made sense.
6. **Day 2:** call the operator's lead. Small problems now are cheap.

### 5.5 Every month

```bash
uv run python -m vaticore.pipeline report --operator <operator_id> \
    --baseline <first shadow day>:<last shadow day> \
    --pilot <first live day>:<yesterday> \
    --control <control site_id> --control <another> \
    --out reports/ --email sponsor@operator.com
```

Or the dashboard > **Pilot report** > **Pilot savings**, with its download
buttons. Send it with a short covering note, and talk it through on a call:
what was saved, what was not, and why. Honest numbers in month one buy the
trust that month three's contract needs.

When something goes wrong on any day, `docs/runbook.md` says what to do.
