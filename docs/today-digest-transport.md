# Getting "what's on today" to a phone

Written 2026-08-29 as the second half of the F3' spike. The first half —
`sb/digest.py`, `GET /api/today`, `python run.py today` — is built and tested.
This document is about the last mile, which is the part that costs money.

lj's ask: *"send a text message of what is going on today (like asking an
assistant what appointments you have)."*

That sentence contains two features, and they have wildly different prices:

- **Outbound** — the system tells me what's on today. Cheap or free.
- **Inbound** — I ask it a question and it answers. Expensive, because it needs
  a phone number and a publicly reachable endpoint.

## The constraint that shapes everything

The dashboard runs on `127.0.0.1:8787` on a desktop behind a home router. A
phone on a cell network cannot reach it. Anything inbound needs either a public
endpoint (port forwarding, or a tunnel like Tailscale/Cloudflare Tunnel) or a
hosted relay. **Outbound needs none of that** — the desktop makes an outgoing
request, which home routers allow by default.

## Option 1 — Twilio, inbound + outbound SMS

Real SMS, both directions. Also the only option that answers questions.

- Per-message: roughly **$0.008 per SMS segment** in the US as of 2026. A daily
  digest is 1–2 segments, so the messages themselves are pennies a month.
- Phone number: around **$1.15–$2/month**.
- **The real cost is A2P 10DLC registration.** US carriers require every
  application-to-person sender to register a brand and a campaign. There are
  one-time registration fees, recurring monthly campaign fees, and per-message
  carrier surcharges on top of Twilio's own rate. For a sole-proprietor sender
  this typically lands somewhere in the **$4–$20/month** range all-in once
  campaign fees are counted, and registration involves vetting that can take
  days and can be rejected.
- Needs a public webhook URL for inbound. Tunnel required.

**Verdict:** the only option that does what lj literally described, and roughly
an order of magnitude more setup and recurring cost than everything else. Worth
it only once the daily digest has proved it gets read.

## Option 2 — Email-to-SMS carrier gateway — **dead, do not build this**

The classic free trick: mail `5551234567@vtext.com` and it arrives as a text.

**AT&T, T-Mobile and Verizon have shut down or are shutting down their
email-to-SMS gateways.** Verizon's `vtext.com` is on a published sunset path.
Messages are dropped silently rather than bounced, which is the worst possible
failure mode for a reminder system — lj would conclude the habit loop was
broken when actually the carrier ate the message.

This was the cheapest option and it is no longer viable. Searching current
sources rather than trusting older knowledge is the only reason this document
does not recommend it.

## Option 3 — Push notification instead of SMS

`ntfy.sh` or Pushover. A notification on the phone rather than a text.

- **ntfy**: open source, free public instance, self-hostable. Publishing is a
  plain HTTP POST to a topic URL — about three lines from `sb/digest.py`. No
  account, no phone number, no registration.
- **Pushover**: one-time purchase per platform, no subscription.
- Delivery is a real push notification, so it appears on the lock screen the
  same way an SMS does.
- Outbound only. Cannot answer a question.

**Verdict:** delivers everything lj described *except* the ability to reply,
for free, today, with no account.

## Option 4 — Scheduled outbound only

Orthogonal to the transport: instead of listening for a question, push the
digest on a timer — every morning at a fixed hour. Needs no public endpoint at
all. `install-backup-task.bat` already establishes the Task Scheduler pattern
this would copy.

Worth noting what this trades away: asking *"what's on today"* at 2pm is a
different act from being told at 7am. The morning push covers the planning
case; it does not cover the mid-day "wait, what was I supposed to do" case.

## Recommendation

**Build option 3 + 4 now: ntfy, pushed on a morning schedule. Defer option 1
until the digest has earned it.**

Reasoning:

1. It costs nothing and needs no account, so it can ship this week rather than
   after a carrier vetting process.
2. It requires no public endpoint, no tunnel, and no open port on the home
   router — which is a security saving, not just a convenience one.
3. The expensive half of lj's request is the *inbound* question-answering, and
   that half is speculative. There are currently **0 decks and 0 reviews**; a
   digest that says "nothing due" is not worth $15/month to be able to
   interrogate. Once D3 has produced fourteen days of real reviews, the digest
   will have real content and the case for paying for two-way SMS can be made
   on evidence.
4. If the morning push turns out to go unread, that is a cheap and fast answer,
   and no money was spent finding it out.

The digest payload is already transport-agnostic — `render_text` produces an
SMS-sized string and `render_long` the full form. Swapping ntfy for Twilio
later is a change of delivery function, not a rewrite.

## What it would take to build the recommendation

1. A `notify` config block: ntfy topic URL, quiet hours, which renderer to use.
2. A `run.py notify` subcommand: build the digest, POST `render_text` output.
3. `install-digest-task.bat`, copying `install-backup-task.bat`, firing daily.
4. Honest failure: if the POST fails, log it where `doctor` can see it. A
   notification system that fails silently is worse than none, because it is
   trusted.

Estimate: 3 points. Not committed to Sprint 2 — raised for Sprint 3 alongside
H3, which already covers desktop notifications and shares the quiet-hours and
never-twice-in-an-hour logic.

## Sources

Prices move; confirm at signup rather than trusting this page.

- Twilio SMS pricing: https://www.zavu.dev/en/blog/twilio-sms-pricing
- A2P 10DLC fees: https://www.ghlscaleup.com/blog/a2p-10dlc-fees-explained
- A2P 10DLC vetting: https://support.twilio.com/hc/en-us/articles/11587910480155-A2P-10DLC-Campaign-Vetting-FAQ
- Carrier gateway shutdown: https://pagertree.com/blog/email-to-sms-replacement
- Gateway status tracker: https://www.emailtotxt.com/carriers
- ntfy: https://github.com/binwiederhier/ntfy
