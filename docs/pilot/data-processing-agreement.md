# Data processing agreement: template

Schedule 3 of the pilot agreement. Under the Nigeria Data Protection Act 2023
(NDPA) and the NDPC's General Application and Implementation Directive (GAID,
effective 19 September 2025), when one company processes personal data on
another's behalf, the two need a written contract that says what is
processed, why, how it is protected, and what happens when something goes
wrong. **It is not legal advice:** have it reviewed by a Nigerian data
protection lawyer or a licensed Data Protection Compliance Organisation
(DPCO).

## Who is who

- **The Operator is the data controller** for the personal data of its staff
  and contractors who receive plans: it decides who gets them and why.
- **Vaticore is the data processor**: it processes that data only to deliver
  the plans, summaries and reports the Operator asked for.
- **Site energy data** (load, generator output, grid hours, fuel) is
  generally not personal data. Where a reading could identify a person (a
  one-person site's fuel log tied to a named attendant), treat it as personal
  data too.

## The template

---

**DATA PROCESSING AGREEMENT** between [Operator] (the "Controller") and
Vaticore Ltd (the "Processor"), part of the pilot agreement dated [date].

**1. What is processed.** Names, roles, WhatsApp numbers and email addresses
of the people the Controller nominates to receive plans or summaries; their
replies to plans (1, 2, the reason given, STOP); delivery and read receipts;
and the times of each.

**2. Why.** Only to deliver daily plans and weekly summaries, record whether
plans were followed and why, honour opt-outs, and measure the pilot. Nothing
else: no marketing, no sale, no sharing beyond clause 5.

**3. Lawful basis and consent.** The Controller confirms each person it
nominates has agreed to receive plans on the channel given, and keeps a
record of when and how (the Processor records it too, in the recipient list).
Anyone may stop at any time by replying STOP; the Processor stops at once.

**4. Instructions.** The Processor acts only on the Controller's documented
instructions (this agreement and the pilot agreement), and tells the
Controller if it believes an instruction breaks the NDPA.

**5. Sub-processors.** The Controller authorises these sub-processors:

| Sub-processor | What for | Where the data is |
|---|---|---|
| Render Services, Inc. | Hosting the service and its database | Frankfurt, Germany (EU) |
| Meta Platforms (WhatsApp Business Platform) | Delivering WhatsApp messages and replies | Meta's data centres (EU and US) |
| [Email provider, e.g. Brevo / Zoho / Google] | Delivering email | [region] |
| Functional Software, Inc. (Sentry) | Error reports, which exclude message content and phone numbers | [US / EU] |

The Processor tells the Controller [14] days before adding or replacing one,
and the Controller may object.

**6. Transfers outside Nigeria.** The data is stored and processed outside
Nigeria (clause 5). The Processor relies on [the adequacy of the destination's
data protection law, the EU General Data Protection Regulation / contractual
safeguards with each sub-processor that give protection no lower than the
NDPA], as Part VIII of the NDPA requires, and will provide copies of the
relevant terms on request.

**7. Security.** The Processor keeps the data:

- encrypted in transit (HTTPS) and at rest (the hosting provider's encrypted
  storage);
- in a database reachable only from the Processor's own services, not from
  the internet;
- with phone numbers stored for matching replies as one-way hashes, and shown
  only masked (+234******5678) in logs and reports;
- behind per-operator access keys, stored hashed, so one operator can never
  see another's data;
- backed up daily by the hosting provider, with restores tested weekly.

**8. Staff.** Only Processor staff who need the data for the pilot can reach
it, and they are bound to confidentiality.

**9. Breaches.** The Processor tells the Controller without undue delay, and
within [24] hours, of becoming aware of a personal data breach, with what is
known: what happened, what data, how many people, what has been done. The
Controller decides on notifying the NDPC (within 72 hours where required,
NDPA section 40) and the people affected; the Processor helps.

**10. People's rights.** The Processor helps the Controller answer requests
from the people concerned (to see, correct or delete their data, or to stop
receiving messages) within [7] days of being asked.

**11. Records and audits.** The Processor keeps a record of its processing
for the Controller and answers reasonable questions about compliance. Audits
by the Controller: on [30] days' notice, at the Controller's cost, at most
once a year.

**12. Retention and the end.** Replies and delivery records are kept for the
pilot plus [12] months for measuring and verifying results, then deleted.
When the pilot ends, the Processor returns or deletes the personal data
within [30] days, as the Controller chooses, and confirms in writing.

Signed for the Controller: ______________ Name, title, date

Signed for the Processor: ______________ Name, title, date

---

## Vaticore's own obligations under the NDPA

Beyond this agreement, check with your lawyer or DPCO:

- **Registration with the NDPC.** Organisations processing the personal data
  of more than 200 people in six months are "data controllers or processors
  of major importance" and must register (GAID tiers: Ordinary High Level
  from 200, Extra High from 1,000, Ultra High from 5,000; reported fees
  ₦10,000, ₦100,000 and ₦250,000). A first pilot with a few dozen recipients
  is likely under the threshold; a second operator may not be. Check the
  current thresholds on ndpc.gov.ng before relying on these numbers.
- **A privacy policy** on vaticore.com that covers recipients, site data and
  the sub-processors above (docs/pilot/privacy-and-consent.md has the short
  notice for recipients).
- **A Data Protection Officer** once registration requires one.
- **Annual compliance audit returns** if registered at Extra or Ultra High
  level (filed by 31 March).
