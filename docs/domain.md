# vaticore.co.uk: website, email and service addresses

The domain is registered at Cloudflare, so its DNS is already there. This page
sets up, in order:

| Address | What | Where it runs |
|---|---|---|
| `vaticore.co.uk`, `www.vaticore.co.uk` | The website and the privacy notice | Cloudflare Pages, from `docs/landing/` |
| `hello@`, `privacy@`, `tech@` | Email in | Cloudflare Email Routing, forwarded to your inbox |
| `plans@`, alerts | Email out from the service | Brevo (or another SMTP provider) |
| `api.vaticore.co.uk` | The API: Meta's webhook, operators' pushes, reports | Render, `vaticore-api` |
| `app.vaticore.co.uk` | The operators' dashboard | Render, `vaticore-dashboard` |
| `api-staging.`, `app-staging.` | The same, for staging | Render |

Stable addresses matter. Meta's webhook, the uptime monitor, operators'
integrations and the privacy link in every consent message point at these
names, so moving hosts later changes one DNS record instead of all of them.

Time: about two hours, most of it waiting for DNS and certificates. Cost:
nothing beyond the domain, Render and (optionally) a mailbox provider.

## 1. Lock the account down (10 minutes)

In Cloudflare:

1. **My Profile > Authentication:** turn on two-factor authentication. The
   domain now carries the company's email and website; losing this account
   loses both.
2. **Domain Registration > vaticore.co.uk:** check auto-renew is on and the
   contact details are the company's.
3. **DNS > Settings:** enable **DNSSEC**. Cloudflare is the registrar, so it
   finishes the setup itself.
4. **SSL/TLS > Overview:** set the mode to **Full (strict)**. **SSL/TLS > Edge
   Certificates:** turn on **Always Use HTTPS**.

## 2. The website on Cloudflare Pages (20 minutes)

The site is `docs/landing/` in the repository: a static page with the brand
assets and `privacy.html`. Nothing to build.

GitHub Pages also publishes it from this repository on every push to `main`
(`.github/workflows/pages.yml`), at the repository's `github.io` address.
Cloudflare Pages serves the same folder at vaticore.co.uk, the address to
give people.

1. **Workers & Pages > Create > Pages > Connect to Git.** Authorise Cloudflare
   on GitHub for the `niranjoshua/Vaticore` repository only.
2. Project name `vaticore-site`; production branch `main`.
3. Build settings: framework preset **None**, build command **empty**, build
   output directory **`docs/landing`**.
4. **Save and Deploy.** It is live at `vaticore-site.pages.dev` within a
   minute.
5. **Custom domains > Set up a custom domain:** add `vaticore.co.uk`, then
   `www.vaticore.co.uk`. Cloudflare adds the DNS records itself, because the
   domain is in the same account.
6. **Before the site goes live:** fill in the company details in the footer
   of `docs/landing/index.html` and in `docs/landing/privacy.html` (each
   waits in an HTML comment: uncomment it and replace the placeholders),
   exactly as on the company's Companies House page:
   registered name, "registered in England and Wales" (Scotland or Northern
   Ireland if the company number starts SC or NI), company number and
   registered office address. UK law requires these on a company's website
   (and its emails: see step 4), and Meta compares them with the documents
   you upload. Add the ICO registration number to the privacy page once the
   data protection fee is paid. Merge to `main`; Pages redeploys on every
   push.

Every push to `main` then redeploys the site. Pull requests get preview
addresses of their own.

## 3. Email in: Cloudflare Email Routing (15 minutes, free)

1. **Email > Email Routing > Get started.** Cloudflare adds its MX records and
   an SPF record (`v=spf1 include:_spf.mx.cloudflare.net ~all`).
2. **Destination addresses:** add your own inbox and click the link it
   sends.
3. **Routing rules > Create address:** `hello`, `privacy`, `tech` and `ops`,
   each forwarding to your inbox. Optionally a catch-all.
4. Send a test to hello@vaticore.co.uk from another account.

This receives mail only. Meta's email verification code, operators' replies
and STOP-by-email unsubscribes all arrive this way.

If you later want real mailboxes (Google Workspace or Zoho Mail, for
colleagues), they replace these MX records: switch Email Routing off first.

## 4. Email out: Brevo for the service, and for you (30 minutes)

The service sends plans by email (for people who prefer it), the weekly
summary, the pilot report and alerts.

1. Sign up at **brevo.com** (the free plan sends 300 emails a day, enough for
   a pilot).
2. **Senders, domains and dedicated IPs > Domains > Add a domain:**
   `vaticore.co.uk`. Brevo shows a verification TXT record, a DKIM record and
   a DMARC suggestion.
3. In Cloudflare **DNS > Records**, add each as Brevo shows it, with the
   proxy off. For SPF, **edit the existing record** that Email Routing
   created, never add a second one:

   ```text
   v=spf1 include:_spf.mx.cloudflare.net include:spf.brevo.com ~all
   ```

4. Add a DMARC record, starting in monitoring mode:

   | Type | Name | Content |
   |---|---|---|
   | TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:ops@vaticore.co.uk` |

   After a month of clean reports, tighten it to `p=quarantine`.
5. Back in Brevo, **Authenticate**. All green within minutes to an hour.
6. **SMTP and API > SMTP:** note the server (`smtp-relay.brevo.com`, port
   587), your SMTP login and a new SMTP key. These go into Render as
   `VATICORE_SMTP_HOST`, `VATICORE_SMTP_USERNAME`, `VATICORE_SMTP_PASSWORD`,
   with `VATICORE_EMAIL_FROM=Vaticore <plans@vaticore.co.uk>`,
   `VATICORE_EMAIL_REPLY_TO=hello@vaticore.co.uk` and
   `VATICORE_OPS_EMAIL=ops@vaticore.co.uk` (`DEPLOY.md`).
   Also set `VATICORE_EMAIL_LEGAL_FOOTER` to the company line, for example
   `Vaticore Ltd, registered in England and Wales, company number 12345678.
   Registered office: <address>.` Every email the service sends ends with it,
   as UK law requires of a company's business emails.
7. **Your own emails** need the same company line: add it to your Gmail
   signature.
8. **To write as hello@vaticore.co.uk from Gmail:** Gmail > Settings >
   Accounts > **Send mail as** > add `hello@vaticore.co.uk`, SMTP server
   `smtp-relay.brevo.com`, port 587, the same login and key. Gmail sends a code
   to hello@, which Email Routing forwards back to you.

Check it: send an email to a Gmail address, open it, **Show original**: SPF,
DKIM and DMARC should each say PASS.

## 5. Meta's domain verification (5 minutes)

In Meta **Business settings > Brand safety > Domains > Add**
`vaticore.co.uk`, choose **DNS TXT record**, and add the record it shows in
Cloudflare DNS (proxy does not apply to TXT). Click **Verify**. A verified
domain strengthens business verification and lets the website's links
carry the business's identity.

## 6. The service addresses on Render (20 minutes, after the blueprint)

`render.yaml` already declares the four names on their services. After the
blueprint is applied (`DEPLOY.md`), add the DNS records:

| Type | Name | Target | Proxy |
|---|---|---|---|
| CNAME | `api` | `vaticore-api.onrender.com` | DNS only (grey cloud) |
| CNAME | `app` | `vaticore-dashboard.onrender.com` | DNS only |
| CNAME | `api-staging` | `vaticore-api-staging.onrender.com` | DNS only |
| CNAME | `app-staging` | `vaticore-dashboard-staging.onrender.com` | DNS only |

Use each service's actual `onrender.com` address, shown at the top of its
page in Render (Render adds a suffix if the name was taken).

**DNS only** matters: Render must answer these names itself to issue their
certificates. In Render, each service's **Settings > Custom Domains** shows
the name as verified and its certificate as issued within a few minutes.
Then check:

- `https://api.vaticore.co.uk/ready` answers `"status":"ready"`;
- `https://app.vaticore.co.uk` shows the dashboard's sign-in.

From then on, use these names everywhere: the Meta webhook
(`https://api.vaticore.co.uk/webhooks/whatsapp`), Better Stack's monitors,
the operators' push address, and the links you send.

## 7. A short record for the pilot folder

Keep one line per DNS record you added, why, and the date. When something
stops working (mail to spam, a certificate error), that list is the first
place to look.

## Later

- **A Nigerian domain** (`vaticore.ng` or `vaticore.com.ng`, through a NiRA
  accredited registrar) reads as local to Nigerian operators. Point it at the
  same Pages project as an extra custom domain when you have one.
- **Mailboxes for colleagues:** Google Workspace or Zoho Mail; switch Email
  Routing off first (step 3).
- **Cloudflare in front of the API** (orange cloud) adds caching and attack
  protection, but needs Render's and Cloudflare's certificate settings lined
  up. Not needed for a pilot.
