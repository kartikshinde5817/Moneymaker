# Money Maker

Lead generation and outreach for a technology consultancy. It finds companies
with a **provable** reason to need web development, computer vision, or AI
agents, records the evidence, and writes outreach that references it.

Targets the **United Kingdom and United States**. See [Legal limits](#legal-limits).

```bash
python3 run.py
```

No virtualenv activation needed — it creates `venv/`, installs dependencies and
re-launches itself inside it on first run. Then open
<http://127.0.0.1:8000>. It works with no API keys at all, using free sources.

```bash
python3 run.py --check      # config status + a real SMTP/IMAP login test
python3 run.py --no-browser # don't open a browser
python3 run.py --reinstall  # force dependency reinstall
```

`--check` logs in for real. Having `SMTP_USER` set is not the same as it
working: a mailbox name that does not exist looks perfectly fine in the config
and fails only when you try to send.

---

## What makes it different from a scraper

A scraper gives you a list of businesses. This gives you a list of businesses
**plus the reason you are contacting each one**, with a URL you can click to
check it.

Every fact lives in a `signals` table with a verbatim quote and a source link:

```
[explicit_request] tender_match
  Tender "RA359497 - Fire Detection & Suppression System": The contractor
  shall provide a complete engineered solution...
  https://www.contractsfinder.service.gov.uk/Notice/ocds-b5fd17-...

[inferred_opportunity] not_mobile_friendly
  https://example-cafe.co.uk has no mobile viewport tag, so it does not
  adapt to phone screens.
```

Three rules follow from that, and they are enforced in code rather than by
prompt instructions:

1. **A company with no signals is not a lead** and can never enter the send queue.
2. **The email writer only sees signal rows** and must cite the ids it used. A
   citation of anything it was not given is rejected and regenerated.
3. **Language is gated on evidence class.** "I saw your tender for…" is only
   reachable when an `explicit_request` signal exists. Where we merely observed
   a gap, phrasing like "you said you need" is rejected before the draft can be
   queued.

---

## Lead sources

| Source | Signal it provides | Key |
|---|---|---|
| **Web search (Serper)** | Pages where somebody states a requirement in their own words — the highest-value source here | Free tier |
| **UK Contracts Finder** | Public tenders — the buyer states the requirement and names a contact | Free, none |
| **Hacker News** | Public posts describing a need | Free, none |
| **OpenStreetMap** | Local businesses to audit | Free, none |
| **Companies House** | UK companies incorporated in the last 90 days — no website yet | Free key |
| **Adzuna** | Job ads. Hiring a CV engineer proves budget was approved | Free tier |
| **Google Places** | Best business coverage, phone and website | Paid |
| **SAM.gov** | US federal opportunities | Free key |
| **GitHub** | ⚠️ **Low quality — off by default.** Tested live, it returns the OpenCV project, Microsoft and research repos. A company with a CV repo already does CV in-house, so these read as competitors | Free |
| **Website audit** | Runs on every lead — see below | Built in |

### Which source for which service

Configured per campaign, free sources only. The ordering follows one finding
from live runs: **the bottleneck is not finding companies, it is finding ones
you can email.** OpenStreetMap returned 24 real Bath businesses and produced
zero email addresses, because small venues publish none. Tenders name a
procurement officer; every web-search result is a live site the audit can mine.
So contactable sources come first.

| Campaign | Sources, best first |
|---|---|
| **Web Development** | Web search · Contracts Finder · Companies House · OpenStreetMap |
| **Computer Vision** | Contracts Finder · Find a Tender · Adzuna · SAM.gov · Web search |
| **AI Agents & Chatbots** | Web search · Adzuna · Contracts Finder · Find a Tender · Hacker News |
| **AI Automation** | Web search · Adzuna · Contracts Finder · Find a Tender |

**Free keys, in the order worth getting them** — the Settings page lists these
with direct links and shows which are set:

| # | Key | Free allowance | What it unlocks |
|---|---|---|---|
| 1 | [Serper](https://serper.dev) | 2,500 searches | Feeds **all four** campaigns, and every result is a live website with a contact page. Biggest single improvement |
| 2 | [Adzuna](https://developer.adzuna.com) | free tier | Job ads — hiring a CV engineer proves budget exists |
| 3 | [Companies House](https://developer.company-information.service.gov.uk) | free | Every UK company incorporated in the last 90 days |
| 4 | [SAM.gov](https://sam.gov/content/api-keys) | free | US federal opportunities |

Working right now with **no key at all**: UK Contracts Finder, UK Find a
Tender, Hacker News, OpenStreetMap.

### The website audit

Fetches each prospect's own site and derives facts from the markup: no site at
all, a parked domain, a placeholder page, no mobile viewport, no HTTPS, dead
jQuery or Flash, a footer stuck three years ago, no booking on a restaurant, no
checkout on a shop, slow first byte.

This is the strongest material in the system, because it is specific, true, and
verifiable by the person reading the email.

**It checks ownership first.** Discovery sources hand back URLs that belong to
somebody else — a tender links to the procurement portal it was published
through, a café links to its Deliveroo page. Auditing those would produce
claims about a page the prospect has never seen, so anything not provably
theirs is discarded rather than used.

---

## Scoring

Deterministic and versioned, never an LLM — a score has to be stable and
arguable. Every lead stores its arithmetic:

```
64/100
  +30  Open tender or RFP matching our services
  +10  Evidence observed in the last 30 days
  +10  Named decision-maker identified
  +8   Deliverable business email on file
  +3   Only a generic role address
```

Bands: 70+ high intent, 40–69 medium, below 40 low. Leads under a campaign's
`min_score` are kept with their reason recorded but never drafted.

---

## Daily operation

Campaigns **research** on schedule; they never email at that moment. Approved
mail leaves through a dispatcher that runs every ten minutes.

| Time | Campaign | Countries |
|---|---|---|
| 07:00 | Web Development | GB, US, IE, AE |
| 09:00 | Computer Vision / OpenCV | GB, US |
| 11:00 | AI Agents & Chatbots | GB, US, AE |
| 13:00 | AI Automation | GB, US |

Every run rotates in **two fresh cities** from a 72-city pool, least recently
searched first and alternating countries, so the whole pool is covered before
anything repeats. Searching London every morning would just re-find yesterday's
businesses.

Two more jobs run continuously: the **dispatcher** every 10 minutes releases
approved mail, and the **inbox watcher** every 5 minutes checks for replies.

Times, sources, locations, limits and approval mode are all editable in the
dashboard and take effect without a restart.

**The Agent page is the front door.** It draws the nine-stage pipeline as a live
graph — each node lights up as it takes control, counts what passed through, and
marks in red what it rejected. Those numbers are the run itself, not a mock:
`Research 7 ✕5` means seven websites were audited and five prospects were
dropped because they had no site and no evidence. The drop-offs are the point —
they are the businesses the system decided *not* to contact, and why.

### Replies, bounces and alerts

The inbox watcher matches incoming mail back to the message that caused it —
on `In-Reply-To` headers first, falling back to the sender address — then acts:

| Incoming | What happens |
|---|---|
| A human reply | Sequence stops, lead marked, **Telegram alert** with who replied, what they said, and the sentiment |
| "not interested" / "remove me" | Suppressed permanently, lead closed |
| Hard bounce | Address suppressed so it is never retried |
| Auto-reply | Logged, no alert |

Telegram setup takes a minute: message @BotFather, `/newbot`, copy the token;
message your new bot once; open
`https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the chat id. Both go
in `.env`, then press **Send test** on the Settings page.

### LinkedIn

A manual lane, deliberately. The app never logs into LinkedIn or fetches
anything from it. Links come from two safe places:

1. The LinkedIn URL a company publishes **on its own website** — the audit
   already reads it from the company's own server.
2. Failing that, a LinkedIn search URL you open yourself, in your own browser.

Each entry carries the evidence and a suggested angle; you write the note.
This matters most for **Germany and Austria**, where cold email needs prior
consent, so LinkedIn is the only channel left.

Verified working: `praj.net` → `linkedin.com/company/praj-industries/`.
Note that small local businesses mostly publish Instagram and Facebook rather
than LinkedIn, so this lane fills up on the B2B and industrial campaigns.

### WhatsApp

When a lead has a usable phone number, a message is prepared and queued with a
`wa.me` link — one click opens WhatsApp with the text already typed.

**It is not sent automatically, and cannot be.** Meta's official API only
delivers pre-approved templates to numbers that have opted in, so a scraped
number has no compliant automated route. Libraries that drive WhatsApp Web get
the number banned, usually within days. A human pressing send is what keeps the
number alive.

**The Outbox is the screen you live in.** Each draft appears beside the
evidence it was written from, with the cited items highlighted. Read the claim,
confirm it is true, approve. Auto-send exists per campaign, but start on manual
until you trust the output.

---

## Legal limits

Cold B2B email is lawful in some countries and not others. The rule is enforced
in the database as `companies.email_policy`, so no code path can email a
blocked country.

| Country | Cold B2B email | Why |
|---|---|---|
| **United States** | Allowed | CAN-SPAM is opt-out based |
| **United Kingdom** | Allowed | PECR reg. 22 exempts corporate subscribers |
| **Ireland, Netherlands, UAE** | Allowed | Equivalent B2B provisions |
| **Australia, New Zealand, Singapore, India, South Africa, France, Sweden** | Allowed | Opt-out regimes |
| **Any other country** | Manual only | No reviewed policy; reach by LinkedIn or phone |

German law firms pursue unsolicited B2B email commercially, with billable
warning notices. Those leads are still collected and shown — reach them by
LinkedIn or phone instead.

Every message carries a real postal address (required by CAN-SPAM), a
`List-Unsubscribe` header with RFC 8058 one-click support, and a footer opt-out.
**The app refuses to send if `COMPANY_POSTAL_ADDRESS` is unset, or if
`PUBLIC_BASE_URL` still points at localhost** — in that case the unsubscribe
link in the email would silently fail for the recipient, which defeats the one
protection they have and is unlawful in every market here. Unsubscribes
and hard bounces are permanent and survive rediscovery.

This is an engineering control, not legal advice. Have a solicitor review your
privacy notice before the first UK campaign.

---

## Deliverability

More campaigns die here than anywhere else.

- **Use a separate domain.** If your outreach domain earns a spam reputation
  and it is also your main domain, client email and invoices stop arriving.
- **Use a real mailbox.** Google Workspace or Microsoft 365. Transactional
  providers — SendGrid, Postmark, SES — prohibit cold outreach in their terms
  and will suspend the account.
- **Warm up.** Enforced automatically, and not overridable by raising the cap:

  | Mailbox age | Ceiling |
  |---|---|
  | Days 0–6 | 8/day |
  | Days 7–13 | 20/day |
  | Days 14–20 | 30/day |
  | Days 21–29 | 50/day |
  | Day 30+ | your configured cap |

  Scale by adding mailboxes, not by raising the ceiling. 30–50/day per mailbox
  is the sustainable norm; 100/day from a single new address is not.
- **Set up SPF, DKIM and DMARC** before the first send. Check with
  `venv/bin/python tools/check_dns.py`.
- Open tracking is deliberately absent: it needs consent in the UK and EU and
  it hurts inbox placement. Replies are the metric that matters.

---

## Configuration

Copy `.env.example` to `.env`. Every value is optional; each one unlocks a
capability.

| Variable | Effect if unset |
|---|---|
| `ANTHROPIC_API_KEY` | Outreach is written from deterministic templates |
| `GOOGLE_PLACES_KEY` | That source is skipped |
| `COMPANIES_HOUSE_KEY` | That source is skipped (free key) |
| `ADZUNA_APP_ID` / `_KEY` | That source is skipped (free tier) |
| `SMTP_*` | Drafts queue but cannot be sent |
| `COMPANY_POSTAL_ADDRESS` | **Sending is refused** |

SQLite by default. For Postgres, set `DATABASE_URL` — nothing else changes.

---

## Hosting it somewhere that stays on

Netlify, Vercel, and free Render/Railway tiers can't run this: no process
stays alive for the scheduler, and no disk persists for the database. See
**[deploy/ORACLE_SETUP.md](deploy/ORACLE_SETUP.md)** for step-by-step setup on
Oracle Cloud's Always Free tier — the only option that's free indefinitely
and gives a real, persistent server. `deploy/setup.sh` automates most of it.

---

## Architecture

[ARCHITECTURE.md](ARCHITECTURE.md) has the full design: agent topology, data
flow, schema, scoring method, deduplication, compliance, and the roadmap.

```
app/
  models.py            schema — companies, signals, leads, messages
  api.py               REST API, live workflow state, public unsubscribe
  scheduler.py         APScheduler; campaign times come from the database
  seed.py              default campaigns, migration from the old app
  services/
    discovery/         one module per lead source
    research/          fetcher (SSRF-guarded), website audit engine
    resolution.py      entity resolution and deduplication
    ownership.py       is this website/email really theirs?
    signals.py         audit findings -> quotable evidence
    scoring.py         deterministic rules
    ai.py              qualification + personalization + citation enforcement
    mailer.py          validation, assembly, sending, warmup
    compliance.py      country policy, suppression
    pipeline.py        orchestrates the stages + StageTracker for the canvas
  static/              dashboard
```

### Deduplication

The same business arriving from three sources must produce one company and one
email. Matching runs strongest-first: registrable domain, registry number,
Places id, then phone **plus** name similarity, then normalised name within the
same city. Weaker than that, it stays separate — a missed merge costs one
duplicate row, a wrong merge emails the wrong business.

A separate, stricter gate applies before every send: no contact to any address
at that company inside the cooling-off window (90 days by default), regardless
of which source found them.

### Security

The fetcher takes URLs from third-party data, so it resolves every hostname and
refuses private, loopback, link-local and reserved ranges, re-checking on each
redirect. Without that, a crafted `website` value turns the worker into a proxy
into your own network.

---

## What this does not do

- **No LinkedIn scraping.** Their API offers no post or profile search, and
  scraping breaches the user agreement — they enforce it with bans and civil
  claims. There is a **LinkedIn lane** instead (see below), and job ads plus
  tenders carry the same buying signal legally.
- **No reply detection yet.** Replies and bounces are modelled in the schema
  but inbox polling is not built. Watch your own inbox and mark leads manually.
- **No follow-up sequences yet.** Schema is in place; the scheduler for them is
  not.
- **No authentication.** Bind to localhost. Do not expose this to the internet
  as it stands.

---

## The previous version

The old single-file app lives in `legacy/`, untouched. **Its leads are no
longer imported automatically.** They carry no evidence — just a business name
— so they cannot be qualified, scored or written to, and 191 of them were
drowning out 5 real ones. Import them as history only if you want them:

```bash
curl -X POST http://127.0.0.1:8000/api/legacy/import
```

To clear evidence-free rows at any time:

```bash
curl -X DELETE http://127.0.0.1:8000/api/leads/purge-evidenceless
```
# Moneymaker
