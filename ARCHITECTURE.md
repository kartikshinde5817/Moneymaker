# Money Maker — Lead Generation & Outreach System

Architecture and build plan. Read section 0 first: three items in the original
spec will get the sending domain blacklisted or draw legal notices, and the
fixes change the design.

Status: design document. No code from this plan has been written yet. The
current app (`main.py`, 1475 lines, SQLite + OpenStreetMap) is the starting
point and is described in section 19 as Phase 0.

---

## 0. Reality check — read before anything else

Four findings that change the design. Each is a factual constraint, not a
preference.

### 0.1 Do not cold-email Germany

German law (UWG §7) requires **prior express consent for unsolicited
commercial email, including business-to-business**. There is no B2B exemption,
unlike most of the EU. Enforcement is not theoretical: German law firms send
formal warning notices (*Abmahnung*) with billable costs attached, and this is
an established practice. A single campaign into Germany can generate dozens of
them.

**Recommendation:** remove Germany from email targeting. Reach German
prospects through LinkedIn connection requests (person-to-person, not bulk
email), paid ads, or inbound. Keep German companies in the database as leads,
flag them `email_blocked = true`, and surface them in a separate "manual
outreach" queue.

Austria has an equivalent rule. Treat it the same way.

### 0.2 Drop Russia

Two blockers, both concrete:

- Federal Law 38-FZ (Advertising) Article 18 requires prior consent before
  sending advertising by any communications network. Same posture as Germany.
- Federal Law 242-FZ requires personal data of Russian citizens to be stored
  on servers physically located in Russia. A lead database of named contacts
  at Russian companies, hosted outside Russia, does not satisfy this.

On top of that, payment rails between India and Russia for services are
unreliable enough that a won deal may not be collectable.

**Recommendation:** remove Russia from scope entirely.

### 0.3 The LinkedIn plan as written has no legal path

The spec asks for LinkedIn post search and profile data while also saying "do
not circumvent LinkedIn protections." Those two requirements cannot both be
met by scraping:

- LinkedIn's official APIs (Marketing, Sales Navigator, Talent) require an
  approved partnership and **do not expose post search or profile search**.
  There is no API that does what the spec describes.
- Scraping public LinkedIn pages is not a US computer-crime offence after
  *hiQ v. LinkedIn* (9th Cir.), but it remains a breach of LinkedIn's User
  Agreement. Consequences are account termination, IP blocks, and civil
  exposure. LinkedIn has litigated this repeatedly and successfully.
- Licensed data resellers exist (Bright Data, Coresignal, People Data Labs,
  Apollo). They shift the compliance burden to the vendor and cost real money.
  Note that this vendor market is unstable — providers have shut down as a
  direct result of LinkedIn litigation — so do not build a core dependency on
  any single one.

**Recommendation:** cut LinkedIn from the MVP. Replace the three LinkedIn
campaigns (10:00, 11:00, 12:00) with sources that carry the *same* "we need
this" signal, are free, and have official APIs:

| Replacement source | Signal it gives you | Cost |
|---|---|---|
| Company job boards (Greenhouse / Lever public JSON) | "Hiring a Computer Vision Engineer" → they have a CV problem *and* budget | Free |
| Adzuna API (UK, US, DE, AE) | Same, aggregated across job boards | Free tier |
| UK Contracts Finder / Find a Tender | Public-sector RFPs naming "CCTV analytics", "website redesign" | Free |
| US SAM.gov Opportunities API | Federal RFPs, same | Free |
| UK Companies House API | Newly incorporated companies → no website yet | Free |
| Hacker News "Who is hiring" (Algolia API) | Startups naming their stack gaps | Free |
| Reddit API (r/smallbusiness, r/restaurateur) | "Need a website for my cafe" posts | Free |

A company posting a job for a computer vision engineer is a **stronger and
better-evidenced** buying signal than a LinkedIn post, because it proves
budget was approved. Revisit LinkedIn in Phase 3 through a licensed provider.

### 0.4 Email infrastructure will decide whether this works at all

The best lead database in the world is worthless if mail lands in spam. Three
specifics that are commonly missed:

- **Transactional ESPs prohibit cold outreach.** Postmark is transactional-only
  by policy. SendGrid's acceptable use policy bars unsolicited mail and
  purchased lists. Amazon SES will suspend accounts over complaint rates.
  Building on these means an account termination at some point.
  **Use real Google Workspace or Microsoft 365 mailboxes via their APIs**
  instead. Replies land in a real inbox, and inbox placement is far better.
- **Never send from the primary domain.** If `yourcompany.com` gets a spam
  reputation, client email and invoices stop being delivered. Buy a separate
  lookalike domain for outreach, e.g. `yourcompany.io` or `get-yourcompany.com`.
- **Warm up, then cap.** 5–10 sends/day in week one, ramping to a hard ceiling
  of roughly 40–50/day per mailbox. Scale by adding mailboxes, not by raising
  the cap. SPF, DKIM and DMARC are mandatory. Keep hard bounces under 2% by
  verifying addresses before sending.

One further point on the requested "Emails Opened" metric: open tracking works
by embedding an invisible image, which in the EU and UK is generally treated as
requiring consent, and it measurably hurts deliverability. **Recommendation:**
disable open tracking for UK/EU recipients; use link-click tracking on your own
domain instead, and treat replies as the real success metric.

### 0.5 Where that leaves the target markets

| Market | Cold B2B email | Governing rule | Verdict |
|---|---|---|---|
| **USA** | Yes, opt-out basis | CAN-SPAM: needs valid physical postal address, honest headers, opt-out honoured within 10 business days | **Primary market** |
| **UK** | Yes, to corporate subscribers | PECR reg. 22 exempts corporate bodies; UK GDPR still needs a legitimate-interest basis, privacy notice, right to object | **Primary market** |
| **UAE / Dubai** | Probably, for corporate addresses | UAE PDPL (Decree-Law 45/2021) + TDRA rules; less settled than US/UK | **Secondary — get local advice before scaling** |
| **Germany / Austria** | **No** | UWG §7 — prior consent required even B2B | **Excluded from email** |
| **Russia** | **No** | 38-FZ consent + 242-FZ data localisation | **Excluded entirely** |

Everything below is designed around US + UK first, UAE second, with a
per-country policy table so markets can be switched on without a code change.

---

## 1. System architecture

Seven containers behind Docker Compose. Each one exists because something
would break if it shared a process with the others.

```
                    ┌──────────────┐
                    │   browser    │
                    └──────┬───────┘
                           │ HTTPS
                    ┌──────▼───────┐
                    │   nginx      │  TLS, static SPA, /api proxy
                    └──────┬───────┘
                ┌──────────┴─────────┐
        ┌───────▼───────┐    ┌───────▼────────┐
        │  api          │    │  web (static)  │
        │  FastAPI      │    │  React + Vite  │
        │  JWT auth     │    └────────────────┘
        │  REST only    │
        └───┬───────┬───┘
            │       │ enqueue
            │  ┌────▼─────┐
            │  │  redis   │  job queue, token buckets, cache
            │  └────┬─────┘
            │       │ consume
            │  ┌────▼──────────┐      ┌──────────────┐
            │  │  worker       │◄────►│  browser     │
            │  │  ARQ, async   │      │  Playwright  │
            │  │  all pipeline │      │  (JS pages)  │
            │  └────┬──────────┘      └──────────────┘
            │       │
            │  ┌────▼──────────┐
            │  │  scheduler    │  ARQ cron → enqueues campaign runs
            │  └────┬──────────┘
            │       │
        ┌───▼───────▼───┐
        │  postgres 16  │  single source of truth
        └───────────────┘
```

**Why the worker is separate from the api.** A campaign run makes several
hundred outbound HTTP requests and takes minutes. Running that in the API
process makes the dashboard unresponsive and ties campaign progress to
uvicorn's lifecycle — a deploy mid-run loses the run. Separating them means
the API always answers in milliseconds and workers can be scaled or restarted
independently.

**Why a separate browser container.** Playwright pulls in ~400MB of Chromium
and its system libraries. Keeping it out of the API image keeps that image
small and lets it be omitted entirely in environments that do not need
JavaScript rendering. Most sites do not need it — see section 7.

**Why Redis and not just Postgres for the queue.** Redis also serves as the
distributed rate limiter (token buckets per mailbox, per API vendor, per
domain). That is the harder requirement; the queue comes along for free.

### Queue choice: ARQ, not Celery

The spec suggests Celery. My recommendation is **ARQ**, with reasoning:

- The workload is almost entirely I/O-bound HTTP — fetching websites, calling
  APIs, calling an LLM. ARQ is `asyncio`-native, so one worker process handles
  hundreds of concurrent fetches. Celery's default model is prefork processes,
  which is the wrong shape for this and burns memory.
- The rest of the stack (FastAPI, httpx, asyncpg, Playwright) is async. ARQ
  matches it; Celery forces sync/async bridging in every task.
- ARQ has built-in cron, so the scheduler needs no extra component.
- Configuration is roughly 50 lines versus Celery's much larger surface.

**When Celery would win:** complex multi-queue routing with per-queue worker
pools, chords and chained workflows, or an existing team that already knows it.
Neither applies here. **Migration path if needed:** tasks are plain async
functions taking JSON-serialisable arguments, so swapping the runner later
touches only the enqueue calls and the worker entrypoint.

---

## 2. Agent architecture

The spec lists eleven agents. The most important design decision in this
document is this:

> **Only three of the eleven need an LLM. The other eight must be
> deterministic code.**

Reasoning. The spec demands a *transparent* score and *no fabricated*
requirements. An LLM cannot deliver a transparent score, because the same
input can produce 71 one day and 64 the next, and the explanation is generated
after the fact rather than being the actual cause. Deterministic rules can be
unit-tested, replayed against historical data, tuned from reply outcomes, and
shown to the user as arithmetic. They also cost nothing and run in
microseconds.

| # | Agent | Implementation | Why |
|---|---|---|---|
| 1 | Discovery | Deterministic — API clients | Just queries and pagination |
| 2 | Entity resolution / dedup | Deterministic — identifier matching | Must be reproducible and auditable |
| 3 | Website research | Deterministic — fetch, parse, fingerprint | Facts, not judgement |
| 4 | Signal extraction | **Hybrid** — rules first, LLM for free text | Rules catch structured signals; LLM reads prose |
| 5 | Qualification & intent | **LLM** (structured output) | Genuine judgement call |
| 6 | Scoring | Deterministic — versioned rule table | Transparency requirement |
| 7 | Personalization | **LLM** (grounded, cited) | Genuine writing task |
| 8 | Email validation | Deterministic — syntax, MX, suppression | Correctness, not judgement |
| 9 | Outreach | Deterministic — rate-limited sender | Must be exact |
| 10 | Follow-up | Deterministic — state machine | Must be exact |
| 11 | Analytics | Deterministic — SQL views | Must be exact |

### The anti-fabrication mechanism

The spec says the AI "MUST NOT fabricate requirements." A prompt instruction is
not a mechanism. The mechanism is:

1. Every fact about a company lives in the `signals` table, and every signal
   row carries `source_id`, a verbatim `quote`, a `source_url`, and
   `observed_at`.
2. The personalization agent's context window contains **only** signal rows for
   that company. It never sees free-form notes or its own earlier output.
3. Its structured output must include `cited_signal_ids`. A response citing a
   signal id that was not in its input is rejected and retried.
4. If a company has zero signals, it **cannot** enter the email queue. It is
   not a lead; it is a directory entry.
5. Signals are typed `explicit_request` versus `inferred_opportunity`. The
   email template set is chosen by that type, so "I saw your post regarding…"
   is only reachable when an actual post exists.

This is enforced in code, not in prose, so it cannot drift.

### Pipeline as a state machine

Agents are stages over a durable row, not conversational agents. Each lead
carries a `stage` column and each transition is checkpointed, so a crashed or
redeployed worker resumes instead of restarting.

```
discovered → resolved → researched → signals_extracted → qualified
    → scored → (rejected | drafted) → pending_approval → queued
    → sent → (bounced | replied | no_response) → follow_up_1 → …
```

Every transition writes to `audit_logs`. A lead can be replayed from any
stage, which matters when the scoring rules change and 4,000 existing leads
need rescoring.

---

## 3. Data flow

One campaign run, end to end:

```
scheduler fires cron
  │
  ├─► create campaign_runs row (status=running)
  │
  ├─► DISCOVERY  ── per configured source, per keyword group
  │     Places API / Companies House / Adzuna / tenders / SERP
  │     └─► raw payload → raw_documents (hashed, deduped)
  │     └─► one `sources` row per fetched URL (provenance)
  │
  ├─► RESOLUTION
  │     normalise domain, phone→E.164, name
  │     look up company_identifiers → existing company or new
  │     └─► companies + company_identifiers
  │
  ├─► GATE: already a lead for this service_category? → stop, count as dup
  │
  ├─► WEBSITE RESEARCH  (concurrent, capped)
  │     robots.txt check → fetch → parse → fingerprint
  │     └─► website_audits (has_site, mobile, ssl, tech, booking, ecommerce)
  │     └─► emails found → contacts
  │
  ├─► SIGNAL EXTRACTION
  │     rules: no site, no viewport meta, no TLS, jQuery 1.x, copyright < 2020
  │     LLM:   job-post text, tender text, review text → typed signals
  │     └─► signals (each with quote + source_url)
  │
  ├─► QUALIFICATION  (LLM, structured)   → service_category, intent, reason
  ├─► SCORING        (rules)             → score + breakdown JSON
  │
  ├─► GATE: score < threshold → status=rejected, keep the row and the reason
  │
  ├─► PERSONALIZATION (LLM, grounded)    → subject, body, cited_signal_ids
  ├─► EMAIL VALIDATION                   → syntax, MX, disposable, suppression
  │
  ├─► approval_mode == auto ? queued : pending_approval
  │
  └─► close campaign_runs (stats JSON, errors)

separately, on its own cadence:
  DISPATCHER  every 5 min → take queued messages whose recipient is inside
              their local business-hours window, respect per-mailbox token
              bucket, send, record email_events
  INBOX POLL  every 5 min → Gmail/Graph history → replies, bounces
              → stop sequences, update lead status, suppress hard bounces
```

Note the discovery/dispatch split: campaigns *research* at 07:00 in your
timezone, but nothing is emailed then. Messages are released inside the
recipient's local 09:00–11:00 window, which is where reply rates concentrate.

---

## 4. Database schema

PostgreSQL 16. Abridged DDL — the tables that carry design decisions are shown
in full, the rest are listed.

### Deduplication core

```sql
CREATE TABLE companies (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    normalized_name TEXT NOT NULL,          -- lowercase, no suffixes/punct
    primary_domain  TEXT UNIQUE,            -- registrable domain, no www
    country         CHAR(2) NOT NULL,
    region          TEXT, city TEXT,
    lat NUMERIC(9,6), lng NUMERIC(9,6),
    industry        TEXT, size_band TEXT,
    email_policy    TEXT NOT NULL DEFAULT 'allowed',  -- allowed|manual_only|blocked
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The dedup engine. One row per known identifier; uniqueness does the work.
CREATE TABLE company_identifiers (
    id         BIGSERIAL PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,   -- domain|phone_e164|google_place_id
                                -- |linkedin_url|registry_no|email_domain
    value      TEXT NOT NULL,
    UNIQUE (kind, value)
);
CREATE INDEX ON company_identifiers (company_id);

CREATE TABLE company_merges (
    winner_id BIGINT REFERENCES companies(id),
    loser_id  BIGINT,
    reason    TEXT,
    merged_at TIMESTAMPTZ DEFAULT now()
);
```

**Why a separate identifiers table.** The same restaurant appears in Places, in
a tender document, and on its own website with three different name spellings.
Columns on `companies` cannot express "this company is known by four phone
numbers and two domains." A unique index on `(kind, value)` makes the
insert itself the dedup check — no scan, no fuzzy match at write time.

### Evidence and provenance

```sql
CREATE TABLE sources (
    id           BIGSERIAL PRIMARY KEY,
    kind         TEXT NOT NULL,   -- google_places|website|job_board|tender|serp|registry
    url          TEXT NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    http_status  INT,
    content_hash TEXT,            -- dedupes identical refetches
    raw_ref      TEXT             -- object-storage key for the raw payload
);

CREATE TABLE signals (
    id          BIGSERIAL PRIMARY KEY,
    company_id  BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    source_id   BIGINT NOT NULL REFERENCES sources(id),
    signal_type TEXT NOT NULL,       -- no_website|not_mobile_friendly|outdated_tech
                                     -- |hiring_cv_engineer|tender_match|explicit_request …
    evidence_class TEXT NOT NULL,    -- explicit_request | inferred_opportunity
    quote       TEXT,                -- verbatim, for the email to cite
    detected_by TEXT NOT NULL,       -- rule | llm
    confidence  NUMERIC(3,2),
    observed_at TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON signals (company_id, observed_at DESC);
```

`signals` is the heart of the system. It is what makes this different from a
scraper: every lead can answer "why did you contact me?" with a URL and a
quote.

### Google Places retention constraint

Google Maps Platform terms permit caching `place_id` indefinitely, but **other
Places content only for a limited period (30 days)**. A lead database that
permanently stores Places-derived names, phones and ratings is in breach.

Design response: store `google_place_id` permanently in
`company_identifiers`; store Places-derived fields in a `places_snapshots`
table with `expires_at = fetched_at + 30 days`, and run a nightly job that
either refreshes or nulls expired rows. Facts confirmed independently from the
company's own website are stored on `companies` and are not subject to this.

### Remaining tables

```
users(id, email, password_hash argon2, role, created_at)
contacts(id, company_id, full_name, title, email, email_status,
         is_decision_maker, source_id, UNIQUE(company_id, lower(email)))
website_audits(id, company_id, url, has_site, http_status, has_ssl,
         is_mobile_friendly, tech jsonb, has_booking, has_ecommerce,
         has_contact_form, copyright_year, perf_score, audited_at)
leads(id, company_id, contact_id, campaign_id, service_category, intent_level,
      score, score_breakdown jsonb, score_version, stage, status,
      created_at, last_contact_at, next_action_at,
      UNIQUE(company_id, service_category))        -- one lead per service
ai_analyses(id, lead_id, agent, model, prompt_hash, input_signal_ids bigint[],
      output jsonb, tokens_in, tokens_out, cost_usd, created_at)
campaigns(id, name, service_category, enabled, sources text[], keyword_groups jsonb,
      industries text[], locations jsonb, schedule_cron, timezone,
      daily_lead_limit, daily_email_limit, approval_mode, min_score, created_at)
campaign_runs(id, campaign_id, started_at, finished_at, status, stats jsonb, error)
search_queries(id, run_id, source, query, results_count, cost_usd)
email_messages(id, lead_id, contact_id, mailbox_id, thread_id, sequence_step,
      subject, body, status, provider_message_id, scheduled_for, sent_at,
      approved_by, approved_at, cited_signal_ids bigint[])
email_events(id, message_id, type, occurred_at, payload jsonb)
      -- queued|sent|delivered|bounce|complaint|click|reply|unsubscribe
mailboxes(id, address, provider, credentials_ref, daily_cap, warmup_stage, enabled)
suppressions(id, scope, value, reason, created_at)   -- scope: email|domain
      -- UNIQUE(scope, lower(value))
sequences(id, campaign_id, step_no, delay_days, template_id, stop_conditions jsonb)
audit_logs(id, actor, action, entity, entity_id, before jsonb, after jsonb, at)
score_rules(code, description, points, condition jsonb, version, active)
```

Key indexes beyond the primary keys:

```sql
CREATE INDEX ON leads (status, next_action_at) WHERE status = 'active';
CREATE INDEX ON email_messages (status, scheduled_for) WHERE status = 'queued';
CREATE INDEX ON companies USING gin (normalized_name gin_trgm_ops);  -- fuzzy fallback
```

---

## 5. API architecture

REST, JSON, JWT bearer auth. Versioned under `/api/v1`.

```
POST   /auth/login                   → access + refresh token
POST   /auth/refresh

GET    /campaigns                     GET/PUT/DELETE /campaigns/{id}
POST   /campaigns                     POST /campaigns/{id}/run     → run now
GET    /campaigns/{id}/runs           GET  /runs/{id}              → live log

GET    /leads?service=&source=&country=&intent=&min_score=&status=
             &date_from=&date_to=&q=&page=&sort=
GET    /leads/{id}                    → company, contacts, signals, audits,
                                        ai_analyses, messages, events
PATCH  /leads/{id}                    → status, notes, next_action
POST   /leads/{id}/rescore
POST   /leads/{id}/redraft

GET    /outbox?status=pending_approval
POST   /outbox/{id}/approve           POST /outbox/{id}/reject
PATCH  /outbox/{id}                   → edit subject/body before approving
POST   /outbox/bulk-approve

GET    /stats/summary                 GET /stats/timeseries?metric=&bucket=day
GET    /stats/breakdown?by=service|source|industry|country|status
GET    /stats/funnel

GET/PUT /settings                     GET/POST /mailboxes
GET/POST/DELETE /suppressions
GET    /export/leads.xlsx
POST   /webhooks/email/{provider}     → bounce/complaint callbacks (HMAC-verified)
GET    /u/{token}                     → public one-click unsubscribe (no auth)
```

Long operations never block: `POST /campaigns/{id}/run` returns `202` with a
run id, and the dashboard polls `/runs/{id}` or subscribes over SSE.

---

## 6. Frontend structure

React 18 + Vite + TypeScript + Tailwind + TanStack Query + Recharts.

**Why Vite/React SPA and not Next.js.** Next.js earns its complexity with SSR,
SEO and edge rendering. This is an authenticated internal tool for one company
— no SEO, no public pages, no server rendering benefit. Next.js would add a
second Node server to run and deploy for nothing. A static SPA bundle served by
nginx is one less container and one less failure mode. If a public marketing
site is ever needed, that is a separate project.

```
web/src/
  api/              generated client + TanStack Query hooks
  components/       Table, Filters, StatTile, Chart, Drawer, ApprovalCard
  pages/
    Dashboard.tsx        stat tiles, charts, funnel
    Leads.tsx            filterable table, saved views, bulk actions
    LeadDetail.tsx       evidence panel, AI analysis, email history
    Outbox.tsx           approval queue — the screen used daily
    Campaigns.tsx        schedule, keywords, locations, limits
    CampaignRun.tsx      live log of a run
    Settings.tsx         company profile, mailboxes, limits, suppressions
  lib/
```

Two opinions about the UI:

- **Outbox is the primary screen, not Dashboard.** The daily job is approving
  or rejecting drafts. It should open there, show one lead at a time with its
  evidence beside the draft, and support keyboard approve/reject/edit.
- **The lead detail page leads with evidence.** Above the generated email, show
  the signals with their quotes and clickable source URLs, so a claim can be
  verified in two seconds before it is sent.

---

## 7. Discovery and scraping strategy

Three tiers, cheapest and most-official first.

### Tier 1 — official APIs, free, highest intent

| Source | Use | Markets |
|---|---|---|
| **Companies House API** | New incorporations by SIC code → no website yet | UK |
| **Contracts Finder / Find a Tender** | Public RFPs quoting the requirement verbatim | UK |
| **SAM.gov Opportunities** | Federal RFPs | US |
| **Greenhouse / Lever public job JSON** | `boards-api.greenhouse.io/v1/boards/{t}/jobs` — a CV engineer opening proves budget | All |
| **Adzuna API** | Aggregated job ads by keyword + country | UK, US, AE |
| **HN Algolia API** | "Who is hiring" threads → startup stack gaps | All |
| **Reddit API** | "need a website for my cafe" posts | All |

### Tier 2 — paid but sanctioned

- **Google Places API (New)** — business discovery by type and location.
  Subject to the 30-day caching rule in section 4.
- **SERP API** (Serper.dev, Bright Data, or Google Programmable Search) — for
  phrase queries like `"looking for a web developer" site:*.co.uk`. Google has
  no general-purpose official search API; Programmable Search is the closest
  official option and is rate-limited.
- **Contact data** (Apollo, People Data Labs) — named decision-makers with
  licensed provenance. Only needed once role addresses stop converting.

### Tier 3 — first-party website analysis (the differentiator)

This is where the actual edge is, and it is entirely legal because it examines
publicly served pages of the company being evaluated.

```python
# Signals extracted from one page fetch — no LLM, no third party
has_site           = resolves and 2xx/3xx
has_ssl            = https negotiates with a valid chain
is_mobile_friendly = <meta name="viewport"> present
outdated_tech      = jQuery 1.x, Flash embeds, <table> layout,
                     WordPress < 5, Bootstrap 2/3
stale              = copyright year < current - 3, no content change in 2y
missing_booking    = restaurant/hotel with no booking widget or OpenTable link
missing_ecommerce  = retail with no cart, Shopify, or Woo markers
weak_contact       = no form, no mailto
perf               = TTFB, page weight, render-blocking count
```

A restaurant with no viewport tag and a 2016 copyright is a *provable*
web-redesign lead, with a URL you can paste into the email. That beats any
scraped list.

**Fetching rules, non-negotiable:**

- Parse and obey `robots.txt` per host; cache it.
- One concurrent request per host; 1–2 s delay between requests to the same host.
- Honest, identifying User-Agent with a contact URL.
- Hard timeouts (10 s), max 3 redirects, response capped at 5 MB.
- Playwright only when the static HTML has no `<body>` text — under 15% of sites.
- **SSRF protection:** resolve the hostname first and refuse private ranges
  (`10/8`, `172.16/12`, `192.168/16`, `127/8`, `169.254/16`, IPv6 ULA). Without
  this, a malicious website field lets an attacker make the worker call
  internal services. This is a real vulnerability class, not a formality.

---

## 8. LinkedIn strategy

Covered in 0.3. The short version:

- **MVP:** not used. Replaced by job boards and tenders, which carry the same
  signal legally and free.
- **Phase 3:** if still wanted, buy it — a licensed provider, with the contract
  reviewed, and an abstraction layer so the vendor can be swapped when the
  market shifts again.
- **Manual lane, available now:** the system can flag high-score leads as
  "LinkedIn outreach" and show the company page URL, and a human sends a
  connection request by hand. That is compliant, and for Germany it is the only
  channel available.

---

## 9. AI agent workflow

Three LLM calls per lead, each with a strict JSON schema and grounded input.

**Call 1 — Qualification & intent.** Input: company facts, website audit,
signal rows. Output:

```json
{"service_category":"computer_vision|web_development|ai_agent|ai_automation|multiple|none",
 "intent_level":"high|medium|low",
 "intent_reason":"...",
 "cited_signal_ids":[1,2],
 "recommended_service":"...",
 "disqualify_reason":null}
```

Competitors and agencies disqualify here — an outreach email to a rival web
studio is worse than no email.

**Call 2 — Research summary.** Only for leads that pass scoring, so nothing is
spent on rejects. Produces the company summary, likely problem, relevance and
suggested angle. Strictly separated: facts come from signals, interpretation is
labelled as interpretation.

**Call 3 — Personalization.** Sees only signals, company facts, service
description and sender profile. Must return `cited_signal_ids`, and the
evidence class picks the template family:

- `explicit_request` → "I saw your post/tender/job ad regarding…"
- `inferred_opportunity` → "I came across… and noticed…"

Post-generation validation, in code: cited ids exist in the input; no forbidden
phrasing ("you said you need", "you're looking for") unless evidence class is
`explicit_request`; length between 80 and 160 words; the portfolio link appears
exactly once; no fabricated numbers. Failures retry once, then fall back to the
deterministic template.

**Operational details.** Structured outputs via tool-use schemas. Cache by
`prompt_hash` so re-runs cost nothing. Log tokens and cost per call in
`ai_analyses` — at roughly 3 calls × 2k tokens per lead, 200 leads/day is a
real monthly number and it should be visible on the dashboard. Use a smaller,
cheaper model for qualification and the strongest one only for personalization.

---

## 10. Email workflow

```
draft → validate → (pending_approval | queued) → dispatcher → sent
                                                    │
                        ┌───────────────────────────┼─────────────┐
                     delivered                    bounce        complaint
                        │                           │              │
                   reply? ──yes──► stop sequence  suppress      suppress
                        │                                       + alert
                        no
                        │
                 follow-up 1 (day 3) → follow-up 2 (day 7) → closed
```

**Validation gate, all must pass:** RFC-valid syntax; domain has MX records;
not a disposable domain; not in `suppressions`; no message sent to this company
in the cooling-off window (default 90 days); company `email_policy = allowed`;
recipient country permits cold B2B email; daily caps not exceeded.

**Provider abstraction.** One interface, four implementations:

```python
class EmailProvider(Protocol):
    async def send(self, msg: OutboundMessage) -> ProviderResult: ...
    async def poll_inbox(self, since: datetime) -> list[InboundEvent]: ...
```

Gmail API and Microsoft Graph are the recommended production providers (real
mailboxes, best placement, native reply threading). SMTP stays as a fallback.

**Every message carries:** a valid physical postal address (CAN-SPAM
requirement), `List-Unsubscribe` with both `mailto:` and one-click HTTPS
(RFC 8058), a plain-text footer unsubscribe link, honest headers, and a
`Message-ID` recorded for reply threading.

**Rate limiting** — Redis token buckets at three levels: per mailbox per day
(warmup-aware), per mailbox per hour, and a randomised 3–8 minute gap between
sends from one mailbox so the pattern is not machine-regular.

**Reply handling** is the highest-value automation here: poll Gmail history or
Graph delta every 5 minutes, match by thread id, classify the reply
(interested / not interested / out of office / unsubscribe request) with a
cheap LLM call, and immediately halt the sequence. Nothing damages a brand like
a follow-up sent after someone already replied.

---

## 11. Scheduler architecture

ARQ cron in the scheduler container reads `campaigns.schedule_cron` and
`campaigns.timezone` from the database, so schedules change from the dashboard
with no redeploy.

Revised default schedule, replacing the LinkedIn slots:

| Time (your TZ) | Campaign | Sources |
|---|---|---|
| 07:00 | Web Development — UK | Companies House, Places, website audit |
| 07:30 | Web Development — US | Places, SERP, website audit |
| 08:00 | Computer Vision | Job boards, tenders, industrial directories |
| 09:00 | AI Agents / Automation | Job boards, HN, startup sources |
| 10:00 | Intent sweep | SERP phrase queries across all services |
| 11:00 | Re-audit | Refresh Places snapshots before 30-day expiry |
| hourly | Dispatcher | Release queued mail in recipient business hours |
| */5 min | Inbox poll | Replies, bounces, complaints |
| 23:00 | Analytics rollup | Materialised view refresh |

**Concurrency guard:** every run takes a Postgres advisory lock keyed on
campaign id, so a long run cannot overlap its own next trigger.
**Idempotency:** each run records the queries it issued; a retried run skips
queries already completed.

---

## 12. Deduplication strategy

Four passes, strongest signal first. Each returns a company id or falls through.

1. **Registrable domain** (strip `www`, apply the public suffix list). A domain
   match is conclusive.
2. **Registry number** (Companies House number, EIN). Also conclusive.
3. **Google place id.** Conclusive within Places.
4. **Phone in E.164.** Strong but not conclusive — shared reception numbers
   exist in office parks, so it is accepted only alongside a name similarity
   above 0.6.
5. **Fuzzy fallback:** normalised name (lowercased, legal suffixes and
   punctuation removed) with trigram similarity ≥ 0.85 **and** same city. Below
   that, it becomes a review queue item rather than an automatic merge.

On merge, `company_identifiers` rows repoint to the winner, signals and audits
move across, and a `company_merges` row records it so the decision is
reversible.

**Send-level dedup is separate and stricter.** Before any send: no message to
any contact at this company within the cooling-off window, regardless of
service category or which source found them. Being discovered three ways must
never produce three emails.

---

## 13. Lead scoring methodology

Deterministic, versioned, stored as data in `score_rules` so weights are tuned
from the dashboard without a deploy. Score = sum of matched rules, clamped
0–100. Every lead stores its breakdown.

| Rule | Points | Applies to |
|---|---|---|
| Explicit public request matching a service | +30 | all |
| Open tender / RFP match | +30 | all |
| Job posting for a matching role | +25 | CV, AI |
| No website at all | +25 | web dev |
| Website not mobile friendly | +15 | web dev |
| Outdated stack (jQuery 1.x, WP<5, Flash) | +12 | web dev |
| No TLS | +8 | web dev |
| Retail/hospitality with no booking or cart | +10 | web dev |
| Target industry match | +15 | all |
| Signal observed within 30 days | +10 | all |
| Named decision-maker identified | +10 | all |
| Verified business email | +8 | all |
| Role address only (`info@`) | +3 | all |
| Company size inside target band | +7 | all |
| Incorporated within 12 months | +12 | web dev |
| Competitor or agency | −40 | all |
| Existing customer | −100 | all |
| No contact route at all | −20 | all |
| In suppression list | −100 | all |

Bands: **high ≥ 70**, medium 40–69, low < 40. `campaigns.min_score` gates the
email queue, defaulting to 55.

Stored example:

```json
{"score": 91, "version": 3, "rules": [
  {"code":"explicit_request","points":30,"signal_id":4812,
   "note":"Tender: 'supply of AI-based PPE compliance monitoring'"},
  {"code":"industry_match","points":20,"note":"Manufacturing"},
  {"code":"recent_signal","points":15,"note":"Published 6 days ago"},
  {"code":"decision_maker","points":10,"note":"Procurement contact named in tender"},
  {"code":"has_website","points":10},
  {"code":"business_email","points":6}]}
```

**Tuning loop (Phase 3):** once a few hundred replies exist, regress reply rate
against rule hits and adjust weights from evidence. Because scoring is
deterministic and versioned, the whole history can be rescored and compared.

---

## 14. Security

- **Auth:** argon2id password hashing, JWT access token (15 min) + refresh
  token (7 days, rotating, revocable). Roles: `admin`, `operator`, `viewer`.
- **Secrets:** API keys and mailbox credentials in environment or Docker
  secrets. If any credential must live in the database, encrypt it with Fernet
  using a key held only in the environment. The current app stores API keys as
  plaintext in SQLite — that must not carry forward.
- **SSRF:** the private-IP guard from section 7. The highest-risk surface in
  the system, because it fetches attacker-influenced URLs by design.
- **SQL injection:** parameterised queries only, via SQLAlchemy Core.
- **XSS:** scraped content is rendered as text, never `dangerouslySetInnerHTML`.
  Scraped site content will contain hostile markup eventually.
- **Webhooks:** HMAC signature verification on every provider callback.
- **Rate limits:** per-IP on `/auth/login`, per-user on expensive endpoints.
- **Containers:** non-root users, read-only root filesystems where possible,
  no host network, Postgres not published to the host.
- **Backups:** nightly `pg_dump` to off-host storage, restore tested quarterly.

---

## 15. Compliance

Built in, not bolted on:

- **Per-country policy table** driving `email_policy`. Germany, Austria and
  Russia are blocked at the data layer, so no code path can email them.
- **Suppression list checked on every send**, keyed by email and by domain.
  Unsubscribes are permanent and survive re-discovery.
- **One-click unsubscribe** (RFC 8058) plus footer link, honoured immediately
  rather than within the 10 business days CAN-SPAM allows.
- **Physical postal address** in every message.
- **Provenance for every fact** — `sources.url` and `signals.quote` exist so
  any claim can be traced. This also answers a GDPR subject access request.
- **Retention policy:** delete leads with no engagement after 12 months; honour
  erasure requests through a single endpoint that purges and then suppresses so
  the contact is not re-discovered.
- **Privacy notice** published at a stable URL, linked in every email, stating
  what is collected, the legitimate-interest basis, and how to object.
- **robots.txt honoured**, sane crawl rates, identifying User-Agent.
- **Places 30-day retention** enforced by the expiry job.
- **Audit log** of every send, approval and merge.

One caveat stated plainly: this is an engineering description of how to build
for compliance, not legal advice. Before the first UK or EU campaign, have a
solicitor review the privacy notice and the legitimate-interest assessment.
The cost is small next to a GDPR complaint.

---

## 16. Folder structure

```
moneymaker/
├── docker-compose.yml           dev
├── docker-compose.prod.yml
├── .env.example
├── api/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── alembic/versions/
│   └── app/
│       ├── main.py              FastAPI factory
│       ├── config.py            pydantic-settings
│       ├── db.py                async engine, session
│       ├── models/              SQLAlchemy — company, lead, signal, email …
│       ├── schemas/             pydantic request/response
│       ├── routers/             auth, campaigns, leads, outbox, stats, settings
│       ├── services/
│       │   ├── discovery/       places, companies_house, adzuna, tenders,
│       │   │                    greenhouse, serp, reddit, hn
│       │   ├── research/        fetcher, robots, audit, tech_fingerprint,
│       │   │                    email_finder, ssrf_guard
│       │   ├── resolution/      normalize, dedupe, merge
│       │   ├── intelligence/    signals(rules), llm_client, qualify,
│       │   │                    research, personalize, validators
│       │   ├── scoring/         engine, rules
│       │   ├── email/           providers/{gmail,graph,smtp}, dispatcher,
│       │   │                    validator, suppression, sequences, inbox
│       │   └── compliance/      country_policy, retention, unsubscribe
│       └── tasks/               arq definitions + cron
├── worker/Dockerfile
├── web/                         React SPA (section 6)
├── nginx/
├── tests/                       unit, integration, fixtures/html_samples
└── docs/                        this file, runbook, prompt library
```

---

## 17. Environment variables

```ini
# core
DATABASE_URL=postgresql+asyncpg://mm:pass@db:5432/moneymaker
REDIS_URL=redis://redis:6379/0
SECRET_KEY=                       # JWT signing, 32+ random bytes
FERNET_KEY=                       # encrypts stored credentials
ENVIRONMENT=production
ALLOWED_ORIGINS=https://app.example.com

# AI
ANTHROPIC_API_KEY=
LLM_MODEL_FAST=claude-haiku-4-5-20251001      # qualification, reply classify
LLM_MODEL_SMART=claude-sonnet-5               # personalization
LLM_DAILY_COST_CAP_USD=25

# discovery
GOOGLE_PLACES_KEY=
SERPER_API_KEY=
COMPANIES_HOUSE_KEY=
ADZUNA_APP_ID=
ADZUNA_APP_KEY=
SAM_GOV_API_KEY=

# email
GMAIL_OAUTH_CLIENT_ID=
GMAIL_OAUTH_CLIENT_SECRET=
MS_GRAPH_TENANT_ID=
MS_GRAPH_CLIENT_ID=
MS_GRAPH_CLIENT_SECRET=
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=

# sender identity — required for CAN-SPAM
COMPANY_NAME=
COMPANY_WEBSITE=
COMPANY_POSTAL_ADDRESS=
PORTFOLIO_URL=
UNSUBSCRIBE_BASE_URL=https://app.example.com/u
PRIVACY_POLICY_URL=

# limits
DAILY_EMAIL_CAP_GLOBAL=150
PER_MAILBOX_DAILY_CAP=40
SEND_WINDOW_LOCAL=09:00-11:00
COOLING_OFF_DAYS=90
SCRAPER_USER_AGENT=MoneyMakerBot/1.0 (+https://example.com/bot)
```

---

## 18. Docker architecture

```yaml
services:
  db:        postgres:16-alpine    # volume, healthcheck, not published
  redis:     redis:7-alpine        # appendonly yes
  api:       build ./api           # depends_on db+redis healthy
  worker:    build ./api           # same image, arq entrypoint, replicas: 2
  scheduler: build ./api           # same image, arq cron, replicas: 1 (must be)
  browser:   playwright base       # only profile: full
  web:       build ./web           # multi-stage → static assets
  nginx:     nginx:alpine          # TLS, serves web, proxies /api
```

`api`, `worker` and `scheduler` share one image with different entrypoints —
one build, one dependency set, no drift. `scheduler` must never scale past one
replica or cron fires twice; the advisory lock in section 11 is the backstop.
Migrations run as a one-shot `alembic upgrade head` before `api` starts, not on
import.

---

## 19. Development roadmap

### Phase 0 — foundation (week 1)

Keep the current app running while the base is replaced underneath it.

- `git init` — there is no version control on this project yet, which is the
  single riskiest fact about it right now.
- Docker Compose with Postgres and Redis.
- Full schema via Alembic; migrate existing SQLite leads across.
- FastAPI skeleton with auth; port the existing OSM discovery as one source.
- **Exit test:** existing leads visible in the new stack, nothing lost.

### Phase 1 — MVP (weeks 2–4), UK + US only

The goal is one genuinely good lead per campaign run, not volume.

- Discovery: Google Places + Companies House + website audit engine.
- Entity resolution and deduplication.
- Signal extraction, rules only at first.
- Qualification and personalization LLM calls with citation enforcement.
- Deterministic scoring with the visible breakdown.
- Outbox approval queue — **manual approval only, no auto-send**.
- Single mailbox via Gmail API, warmup schedule, full suppression handling.
- Dashboard: stat tiles, lead table with filters, lead detail with evidence.
- **Exit test:** 50 leads reviewed by hand; at least 40 have a signal you agree
  justifies the email. If not, fix scoring before sending anything.

### Phase 2 — automation (weeks 5–8)

- Job boards, tenders, SERP intent queries, Reddit and HN.
- Follow-up sequences with reply and bounce detection.
- Auto-send behind caps, still with a kill switch.
- Multi-mailbox rotation, per-recipient-timezone send windows.
- Full analytics: funnel, breakdowns, time series, cost per lead.
- UAE added after local legal review.
- **Exit test:** a week runs unattended without a deliverability incident.

### Phase 3 — scale (weeks 9+)

- Licensed LinkedIn data, if still wanted.
- A/B testing of subject lines and angles, measured on replies.
- Score weights tuned by regression on reply outcomes.
- CRM sync (HubSpot/Pipedrive).
- Per-campaign cost and ROI reporting.

---

## 20. MVP vs Phase 2 vs Phase 3

| Capability | MVP | P2 | P3 |
|---|---|---|---|
| Google Places discovery | ● | | |
| Companies House (UK) | ● | | |
| Website audit engine | ● | | |
| Deduplication | ● | | |
| Deterministic scoring | ● | | |
| LLM qualification + personalization | ● | | |
| Citation enforcement | ● | | |
| Approval queue | ● | | |
| Single mailbox + warmup | ● | | |
| Suppression + unsubscribe | ● | | |
| Lead table, detail, evidence panel | ● | | |
| Job boards, tenders, SERP, Reddit/HN | | ● | |
| Follow-up sequences | | ● | |
| Reply + bounce detection | | ● | |
| Auto-send with caps | | ● | |
| Multi-mailbox rotation | | ● | |
| Recipient-timezone windows | | ● | |
| Full analytics + charts | | ● | |
| UAE market | | ● | |
| Licensed LinkedIn data | | | ● |
| A/B testing | | | ● |
| Outcome-tuned scoring | | | ● |
| CRM sync | | | ● |

**Scope honesty:** Phase 1 as specified is roughly three to four weeks of
focused full-time work for one experienced developer. The full spec across all
three phases is a two-to-three month build. Anything promising it in a few days
is promising a demo, not a system that can email real prospects.

---

## Summary of deviations from the original spec

| Spec said | Doing instead | Why |
|---|---|---|
| Target Germany, Russia | Excluded | UWG §7; 38-FZ + 242-FZ |
| LinkedIn scraping campaigns | Job boards, tenders, registries | No legal path; these signals are stronger |
| Next.js | Vite + React SPA | No SSR/SEO need; one less server |
| Celery + Redis | ARQ + Redis | Async I/O workload; far less config |
| 11 autonomous agents | 3 LLM stages + 8 deterministic | Transparency and reproducibility |
| "AI must not fabricate" | Citation enforcement in code | A prompt is not a mechanism |
| ESP (SES/SendGrid) | Gmail / Graph mailboxes | ESP policies bar cold outreach |
| Open tracking metric | Click tracking; off in EU/UK | Consent issue + deliverability cost |
| All campaigns fire 07:00–12:00 | Research then; send in recipient's window | Reply rates follow local mornings |
