# Recipients: privacy notice and consent

Everyone who receives plans must agree first: WhatsApp requires it, and so
does the Nigeria Data Protection Act. This page has the short notice to give
them, the consent message, and how to record the answer.

## How consent works in practice

1. The operator's pilot lead tells the team about the pilot (at a meeting or
   in their usual WhatsApp group) and says Vaticore will ask each person.
2. Vaticore sends the consent message below to each nominated number (from
   your own phone or the operator lead's; this is a one-to-one message, not
   a template).
3. Only people who reply **YES** go into the recipients file, with
   `consent = true` and a `consent_note` saying when and how, e.g.
   `"Replied YES on WhatsApp to Vaticore, 12 Oct 2026 14:05"`.
4. Keep a screenshot of each YES in the pilot folder.
5. People who do not reply are not added. Ask once more after two days, then
   leave it.

## The consent message (English)

> Good afternoon [name]. This is [your name] from Vaticore. [Operator] is
> trying our daily energy plans at [site] for the next few months. Each
> evening you would get one WhatsApp message with tomorrow's plan: when to
> run the generator, what to expect from the grid and the battery. It is
> advice only; you and your team decide. You can reply 1 or 2 to say if you
> followed it, and STOP at any time to stop the messages.
>
> We keep your name, number and replies only to send the plans and measure
> the pilot, and delete them [12] months after it ends. Details:
> [vaticore.co.uk/privacy].
>
> Is it OK to send you the plans? Reply YES or NO.

## The consent message (Pidgin)

Have a native speaker check this before sending.

> Good afternoon [name]. Na [your name] from Vaticore. [Operator] wan try our
> daily energy plan for [site] for the next few months. Every evening you go
> get one WhatsApp message wey get tomorrow plan: when to on generator, wetin
> to expect from grid and battery. Na advice be am; you and your team go
> decide. You fit reply 1 or 2 to tell us if una follow am, and STOP any time
> to stop the messages.
>
> We go keep your name, number and replies only to send the plans and measure
> the pilot, and we go delete them [12] months after the pilot end. More
> info: [vaticore.co.uk/privacy].
>
> We fit dey send you the plans? Reply YES or NO.

## Short privacy notice (for vaticore.co.uk/privacy or the pilot pack)

> **How Vaticore uses your details in the [Operator] pilot**
>
> **Who we are.** Vaticore Ltd, [address], [email]. For this pilot we act on
> behalf of [Operator], who chose to send you plans.
>
> **What we hold.** Your name, role, WhatsApp number or email, your replies to
> plans (1, 2, the reason you give, STOP), and when messages were delivered
> and read.
>
> **Why.** To send you daily energy plans and weekly summaries, to know
> whether plans were followed and why, and to measure the pilot's results.
> Nothing else: no marketing, no selling, no sharing except with the
> companies that deliver and host the service (Meta for WhatsApp, Render for
> hosting in Germany, [email provider]).
>
> **How long.** Until [12] months after the pilot ends, then deleted.
>
> **Your rights.** You can stop the messages at any time by replying STOP. You
> can ask to see, correct or delete your details by writing to [email]. If
> you are not satisfied, you can complain to the Nigeria Data Protection
> Commission (ndpc.gov.ng).
>
> **Safety.** We store your number only in a protected form, show it masked in
> reports, and keep everything encrypted.

## When someone wants out

- **STOP** on WhatsApp or email: automatic, at once, on every channel.
- **Asked in person or by the operator:** run
  `uv run python -m vaticore.pipeline optout --phone +234...`, then remove them
  from the recipients file at the next upload.
- **Asked to delete their details:** remove them from the recipients file,
  and tell Vaticore's tech lead to delete their delivery and reply rows;
  confirm to them in writing within the agreed time.
