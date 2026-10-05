# Setting up WhatsApp delivery

How to connect Vaticore's daily plans to WhatsApp, step by step. It uses
Meta's **WhatsApp Business Platform (Cloud API)**, the official way for a
business to send WhatsApp messages from software.

Meta changes its screens and prices often. The steps below were right in
October 2026; where a label differs, look for the nearest equivalent and
check Meta's own documentation (developers.facebook.com/docs/whatsapp).

## What you need first

- **The registered company.** Meta verifies the business behind the account.
  Use Vaticore Ltd's certificate of incorporation, a business address, the
  website (vaticore.com) and an email at the company domain.
- **A phone number for Vaticore's sender** that is **not** already registered
  on the WhatsApp or WhatsApp Business app. A new SIM or a virtual number both
  work; it only needs to receive one SMS or call for verification.
- **The API running on a public HTTPS address** (for example on Render, see
  `DEPLOY.md`), so Meta can send delivery receipts and replies to it.

## 1. Business portfolio and verification

1. Go to **business.facebook.com** and create a business portfolio for
   **Vaticore Ltd**.
2. In **Business settings > Security centre**, start **Business
   verification** and upload the company documents. This can take from a day
   to a couple of weeks.
3. You can build and test (steps 2 to 4) while verification is pending.
   Until it completes, Meta limits how many people you can message.

## 2. The app and a test number

1. Go to **developers.facebook.com**, sign in, and **Create app**. Choose the
   business type and link it to the Vaticore Ltd portfolio.
2. **Add product: WhatsApp**. Meta creates a WhatsApp Business account and a
   free **test phone number**.
3. In **WhatsApp > API setup**, note the **Phone number ID** (not the phone
   number itself). This is `VATICORE_WHATSAPP_PHONE_NUMBER_ID`.
4. Under **To**, add your own WhatsApp number as a test recipient and enter
   the code it receives. The test number can only message numbers added here.
5. Copy the **temporary access token** (valid for 24 hours) into
   `VATICORE_WHATSAPP_TOKEN` in your `.env`, and check the connection:

   ```bash
   uv run python -m vaticore.pipeline whatsapp-test --to +234XXXXXXXXXX
   ```

   You should receive Meta's "Hello World" message. If not, the command
   prints Meta's error with a hint.

## 3. The daily plan template

A business may only message someone first with a **pre-approved template**.
Vaticore uses one template for every plan.

1. Open **WhatsApp Manager > Message templates > Create template**.
2. **Category: Utility** (plans are operational messages, not marketing).
3. **Name:** `vaticore_daily_plan`. **Language:** English.
4. **Body:** paste exactly:

   ```text
   Vaticore plan for {{1}}, {{2}}.
   Generator: {{3}}
   Grid: {{4}}
   Battery: {{5}}
   Note: {{6}}
   Advisory only: your team decides. Reply 1 if you follow the plan, 2 if not, or STOP to stop these messages.
   ```

5. **Sample values** (Meta asks for one per variable):
   1. `Macro tower, Ikorodu`
   2. `Tue 10 Mar`
   3. `19:00 to 21:00 (2 h in 1 run, about 7 L).`
   4. `counted on 06:00 to 14:00; run the generator if it fails.`
   5. `starts about 60%, ends about 41%.`
   6. `Data OK.`
6. Submit. Approval usually takes minutes to a few hours. If Meta rejects it,
   the reason is shown; the usual fix is a small wording change.
7. If you choose a different name or language code, set
   `VATICORE_WHATSAPP_TEMPLATE_NAME` and `VATICORE_WHATSAPP_TEMPLATE_LANGUAGE`.

The template's wording lives in `vaticore/delivery/message.py`
(`TEMPLATE_BODY`). If you change one, change the other to match.

### The weekly summary template

Supervisors get a short summary of their sites' week every Monday (only
people with `weekly_summary = true` in the recipients file). It is a second
template; submit it at the same time as the daily plan, since approval is the
step that takes longest.

1. **Category: Utility.** **Name:** `vaticore_weekly_summary`. **Language:**
   English.
2. **Body:** paste exactly:

   ```text
   Vaticore week of {{1}}.
   Plans: {{2}}
   Value: {{3}}
   Why not followed: {{4}}
   Fuel: {{5}}
   Data: {{6}}
   Advisory only. Reply STOP to stop these messages.
   ```

3. **Sample values:**
   1. `28 Sep to 4 Oct, 3 sites`
   2. `21 sent, 15 followed, 4 not followed, 2 unanswered.`
   3. `following the plans saved about 85 L (NGN 106,250) (modelled against planning from yesterday).`
   4. `no diesel 3, generator fault 1.`
   5. `1 check at Ikorodu tower, 28 L (NGN 35,000).`
   6. `readings complete at every site.`
4. If you choose another name, set `VATICORE_WHATSAPP_SUMMARY_TEMPLATE`. The
   wording lives in `vaticore/pipeline/summary.py` (`SUMMARY_BODY`).

### The Pidgin template

People with `language = "pcm"` in the recipients file get their plans in
Nigerian Pidgin once this template is approved; until then they get English.
Meta's template languages do not include Pidgin, so it is submitted under
**English** with its own name.

Before submitting: have a native speaker read the wording, and test it with
two or three technicians (docs/pilot/technician-test.md). Change it in
`vaticore/delivery/message.py` (`PIDGIN_TEMPLATE_BODY` and the `"pcm"` phrase
table) if they suggest better words, then submit exactly what is there.

1. **Category: Utility.** **Name:** `vaticore_daily_plan_pcm`. **Language:**
   English.
2. **Body:**

   ```text
   Vaticore plan for {{1}}, {{2}}.
   Generator: {{3}}
   Grid: {{4}}
   Battery: {{5}}
   Note: {{6}}
   Na advice be dis, your team go decide. Reply 1 if una follow the plan, 2 if una no follow am, or STOP make we stop dis messages.
   ```

3. **Sample values:**
   1. `Macro tower, Ikorodu`
   2. `Tue 10 Mar`
   3. `on am 19:00 to 21:00 (2 hours, 1 time, about 7 L).`
   4. `we dey expect am 06:00 to 14:00; if e no come, on the generator.`
   5. `e go start around 60%, end around 41%.`
   6. `Data dey OK.`
4. Once it is **Active**, set on every service that sends plans
   (`vaticore-daily-plans`, `vaticore-safety-net`):

   ```text
   VATICORE_WHATSAPP_EXTRA_TEMPLATES=pcm=vaticore_daily_plan_pcm:en
   ```

   The "why not?" question and its thanks go to Pidgin speakers in Pidgin
   automatically (they are free text inside the conversation, not templates).

Hausa (`ha`) is a Meta template language, so a Hausa version can be submitted
under Hausa once a translator provides the phrase table; Yoruba and Igbo, like
Pidgin, would go under English.

## 4. A permanent token

The 24-hour token is for testing. For the daily job:

1. In **Business settings > Users > System users**, add a system user (for
   example "vaticore-pipeline") with the **Admin** role.
2. **Assign assets:** the app (full control) and the WhatsApp account (full
   control).
3. **Generate token** for the app, with the permissions
   `whatsapp_business_messaging` and `whatsapp_business_management`, and no
   expiry.
4. Store it as `VATICORE_WHATSAPP_TOKEN` in the hosting dashboard. Never put
   it in the repository, a document or a chat.

## 5. Your real sender number

1. In **WhatsApp Manager > Phone numbers > Add phone number**, add the number
   from "What you need first", with the display name **Vaticore**. Meta
   reviews the display name.
2. Verify it by SMS or call.
3. Replace `VATICORE_WHATSAPP_PHONE_NUMBER_ID` with the new number's ID.

## 6. Webhooks: receipts, replies and STOP

So Vaticore knows when a plan was delivered and read, and hears replies:

1. In the app dashboard, **WhatsApp > Configuration > Webhook > Edit**.
2. **Callback URL:** `https://YOUR-API-ADDRESS/webhooks/whatsapp`
3. **Verify token:** a long random word you choose. Set the same value as
   `VATICORE_WHATSAPP_VERIFY_TOKEN` on the API service first, then click
   **Verify and save**.
4. Under **Webhook fields**, subscribe to **messages**. This covers both
   replies and delivery statuses.
5. In **App settings > Basic**, copy the **App secret** into
   `VATICORE_WHATSAPP_APP_SECRET` on the API service. Every webhook is checked
   against it; without it the API refuses webhooks.
6. Also set `VATICORE_WHATSAPP_TOKEN` and `VATICORE_WHATSAPP_PHONE_NUMBER_ID`
   on the API service, so it can ask "why not?" after a reply of 2. Without
   them the 2 is still recorded; the question is just not asked.

What replies do:

| Reply | Effect |
|---|---|
| `1`, yes, done | Recorded as "followed the plan" for that day |
| `2`, no | Recorded as "did not follow", and Vaticore asks why: A generator fault, B no diesel, C grid was on, D battery problem, E told to run it differently, or any text |
| The answer to "why" (within 24 hours) | Stored as the reason for that day; shown in the weekly summary and the pilot report |
| `STOP` | That number gets no more plans, whatever the recipients file says |
| `START` | Plans resume |
| Anything else | Stored as feedback for the team to read |

## 7. Recipients and consent

1. Copy `examples/sites/recipients.example.toml` to a private location (never
   into the repository) and list each person, their sites and their consent.
2. **Only list people who agreed to receive the plans.** WhatsApp requires it.
   Record when and how in `consent_note`; a line in the pilot agreement and a
   short confirmation message are the simplest.
3. On Render, upload it as a **secret file** named `recipients.toml`, and the
   operator's portfolio as `portfolio.toml`.

## 8. Go live

```bash
# One site, one day, to the console first: check the wording.
uv run python -m vaticore.pipeline run --site example-towerco/lag-ikd-0142 --channel console \
    --recipients /secure/recipients.toml

# Then for real, on WhatsApp.
uv run python -m vaticore.pipeline run --site example-towerco/lag-ikd-0142 --channel whatsapp \
    --recipients /secure/recipients.toml
```

After that, the scheduled job in `render.yaml` sends every site's plan for
the next day at 18:00 Lagos time each evening.

## Costs and limits

- Meta charges per delivered **template** message, at a rate that depends on
  the category (utility) and the recipient's country. Replies inside an open
  conversation window cost less or nothing. Check Meta's current pricing for
  Nigeria before quoting a cost to a customer; one plan a day per person is a
  small monthly amount per site.
- Until business verification completes, Meta limits how many people you can
  message a day. Verify early.
- Keep messages operational. If Meta decides a template is promotional, it
  may reclassify it as marketing, which costs more and needs stricter opt-in.
