# Runbook: when something goes wrong

Who does what when the evening run, a message or the data fails. Keep it
short and act in order: make sure the site teams are not left guessing, then
find the cause, then tell the operator.

Commands run in a service's **Shell** tab on Render (production:
`vaticore-api`; staging: `vaticore-api-staging`). They share the database and
settings of that environment.

## Who is alerted, and how

| What fails | Who finds out | How |
|---|---|---|
| The 18:00 job does not start or crashes | Vaticore on-call | The heartbeat monitor (no ping by 18:00 Lagos, an hour's grace) |
| One site fails while planning | That site's team, at once | "No plan today, run as usual" with the reason (automatic) |
| Messages fail to send (Meta down, token expired) | Vaticore on-call | Sentry error; the 19:30 safety net retries every failed send |
| The job missed sites entirely | Site teams and Vaticore | The 19:30 safety net sends "no plan today, run as usual" and emails `VATICORE_OPS_EMAIL` |
| The API is down | Vaticore on-call | Uptime monitor on `/ready` (replies and receipts queue at Meta and retry) |
| A model drifts | Vaticore on-call | Email from model monitoring; plans continue on the next model |
| A backup would not restore | Vaticore on-call | The Monday restore drill fails in Sentry |

On-call for the pilot: name one person, and a second for when they travel.
Their phone gets the heartbeat and uptime alerts (Better Stack's app or SMS).

## The evening run failed or did not start

1. **Do not rerun blindly.** Open Render > `vaticore-daily-plans` > **Logs**.
   Find the first error.
2. **Before 19:30 Lagos:** fix the cause if it is quick (an expired secret, a
   bad portfolio upload: run `site check`), then **Trigger Run** on the job.
   Plans already sent are never sent twice; sites that got nothing get their
   plan.
3. **After 19:30:** the safety net has already told the affected sites "no
   plan today, run as usual". Do not send plans for that day: a plan after
   "run as usual" contradicts it. Fix the cause for tomorrow.
4. **One site only:** in the API Shell,

   ```bash
   uv run python -m vaticore.pipeline run --site OPERATOR/SITE --channel whatsapp
   ```

   It plans and sends that site only, and skips people who already got it.
5. Note it in the incident log (below).

## Messages did not arrive

1. `uv run python -m vaticore.pipeline safety-net --channel whatsapp` resends to
   anyone whose send failed. Run it once; it is safe to repeat.
2. If every send fails: the WhatsApp token or phone number ID is wrong or
   expired (Sentry shows `HTTP 401` or `code 190`). Create a new system-user
   token (docs/whatsapp-setup.md, step 4), update `VATICORE_WHATSAPP_TOKEN` on
   every service that has it, then rerun the safety net.
3. If one person never receives: they may have replied STOP (`optout --undo`
   only if they ask to restart), their number may be wrong in the recipients
   file, or they are not on WhatsApp (`code 131026`).
4. If Meta paused the template for low quality: look in WhatsApp Manager >
   Message templates. Do not edit an approved template during the pilot
   without testing the new wording on staging first.

## A site's data stopped

The plan says so itself ("No readings since ..."), and the site gets "no plan
today" once the data is too old to plan from.

1. `uv run python -m vaticore.pipeline health --days 7` to see which sites and since
   when.
2. Ask the operator's monitoring contact to check the logger and its SIM data
   bundle (the commonest cause) or the platform login.
3. If readings arrive later, the next run uses them; scoring catches up for
   up to three days.

## The API is down

Meta retries webhooks for a while, so replies are not lost for short outages.

1. Render > `vaticore-api` > **Events** and **Logs**. A failed deploy rolls
   back by itself; a bad one: **Rollback** to the last good deploy.
2. `/ready` failing with "pending migrations": the pre-deploy migration did
   not run. In the Shell: `uv run python -m vaticore.pipeline migrate`.

## A data breach, or a suspected one

A lost laptop with operator data, a leaked key, a database exposed by
mistake: treat it as a breach until shown otherwise.

1. **Contain** within the hour: revoke leaked keys (`apikey revoke`), rotate
   the admin token and WhatsApp token, close any database access you opened.
2. **Tell the operator at once** (the data processing agreement requires it
   without undue delay). They are the controller of their staff's data.
3. **72 hours:** where the breach is likely to put people at risk, the NDPC
   must be notified within 72 hours of becoming aware (NDPA 2023, section 40).
   Agree with the operator who files it; Vaticore provides the facts.
4. Write down what happened, what data, how many people, what was done.

## Rolling back a bad release

Production deploys only when someone presses Deploy, after staging. If a
release misbehaves: Render > service > **Rollback** to the previous deploy, on
`vaticore-api` and each cron job that changed. Database migrations are written
to be safe with the old code running, so a rollback does not need one undone.

## What to tell the operator

Same evening, one message from Vaticore to the operator's pilot lead:

> Tonight's plans for [sites] did not go out because [cause in one line]. The
> sites were told to run as usual, so nothing changed on the ground. Fixed
> for tomorrow: [what changed]. Sorry for the gap.

Honest, short, no blame on the site teams.

## Incident log

Keep one line per incident in a shared sheet: date, what failed, sites
affected, how long, cause, fix, and whether the operator was told. The pilot
report's "Data" section and the weekly summary's data line show the same gaps
from the data side.
