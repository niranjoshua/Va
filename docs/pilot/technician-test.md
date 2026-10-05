# Testing the message with technicians

Before any real plan goes out, sit with two or three of the people who will
receive them. You are not asking for their opinion of Vaticore; you are
finding out whether they can act on the message in ten seconds. Thirty
minutes each is enough.

## Before you go

- Print, or have on your phone, three sample messages for their site, made on
  staging (`run --channel console`): an ordinary day, a day with no generator
  needed, and a "no plan today" message. Have the Pidgin version of each too.
- Bring a notebook. Write down their words, not your summary of them.
- Ask the operator's lead to introduce you, then leave you alone with the
  technician if possible: people are more candid without their manager there.

## The script

**1. Their day today (5 minutes).** "Walk me through how you decide when to
run the generator." Listen for: who decides (them, a supervisor, a fixed
schedule), what they look at (battery, grid, time of day), and what goes
wrong (no diesel, generator faults, NEPA coming and going).

**2. Read it cold (5 minutes).** Hand them the ordinary-day message without
explaining it. "Tell me what this is asking you to do tomorrow." Do not help.
Note:

- How long until they say what to do.
- Whether they get the times right (local time, the clock they use).
- What they skip or misread.

**3. The hard cases (5 minutes).** Show the "no generator needed" and "no
plan today" messages. "What would you do?" The right answers: hold the
generator unless something changes; run the site as usual.

**4. Language (5 minutes).** Show the Pidgin version. "Which one would you
rather get?" Ask what words in either version feel strange or bookish.

**5. Replies (5 minutes).** "If you followed it, what would you reply? If you
couldn't, what would stop you?" The reasons they give are the reasons the
"why not?" question should offer (generator fault, no diesel, grid came,
battery problem, told otherwise). Note any reason that is missing.

**6. Timing and channel (5 minutes).** "When is the best time to get
tomorrow's plan?" (It goes out at 18:00 now.) "Do you read WhatsApp in the
evening? Is your phone on data?" "Who else should see it?"

## What to do with what you hear

| You hear | Change |
|---|---|
| They misread the times | Check the site's timezone in the portfolio; consider "from 7pm to 9pm" style in the wording |
| They prefer Pidgin | Set `language = "pcm"` for them; submit the Pidgin template |
| A word confuses them | Change it in `vaticore/delivery/message.py` (and the template, resubmitted to Meta) |
| A common reason for not following is missing | Add it to the reasons list in `vaticore/delivery/webhook.py` |
| They do not read WhatsApp in the evening | Move the job time in `render.yaml`, or add their supervisor |
| They follow a fixed schedule set by someone else | That person must be in the pilot, or plans will never be followed |

Write up what you heard in half a page per person and share it with the
operator's lead: it shows you listened, and it is the start of the trust the
pilot depends on.
