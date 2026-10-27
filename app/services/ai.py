"""The three LLM stages, and the guardrails that keep them honest.

The system's core promise is that it never invents a requirement. That is
enforced here by construction, not by asking the model nicely:

  * The model's context contains ONLY signal rows for this company. It never
    sees speculation, its own earlier output, or free-form notes.
  * Its reply must list `cited_signal_ids`. Any id that was not in the input
    is a fabrication, and the reply is rejected.
  * Phrases that assert the prospect asked for something ("you said you
    need...") are only permitted when an `explicit_request` signal exists.
    Otherwise the draft is rejected and regenerated.
  * With no API key, or after two failed attempts, it falls back to a
    deterministic template that can only state facts already on file.

Every call is logged to `ai_analyses` with its token count and cost.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

import httpx

from app.config import settings
from app.services import catalog
from app.models import Company, Contact, Signal
from app.services.compliance import find_unsupported_claims
from app.services.discovery.base import EXPLICIT

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# USD per million tokens, for the running cost figure on the dashboard.
PRICING = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
}

SERVICE_PITCH = {
    "web_development": (
        "custom websites and web applications - business and corporate sites, "
        "restaurant and hotel sites with online booking and ordering, "
        "e-commerce, redesigns and modernisation"
    ),
    "computer_vision": (
        "computer-vision systems built on OpenCV and YOLO - PPE and helmet "
        "detection, fire and smoke detection, worker-safety and restricted-area "
        "monitoring, production-line quality inspection, and CCTV analytics that "
        "run on existing camera infrastructure"
    ),
    "ai_agent": (
        "AI agents - chatbots, voice and calling agents, WhatsApp agents, "
        "customer-support and appointment-booking assistants, and RAG assistants "
        "over a company's own documents"
    ),
    "ai_automation": (
        "AI workflow and business-process automation - lead qualification, "
        "document processing, internal AI tools and LLM integrations"
    ),
    "digital_marketing": (
        "digital marketing - SEO, Google Business and local search, social media "
        "management, paid ads and analytics set-up"
    ),
}


class AIUnavailable(Exception):
    pass


def available() -> bool:
    return bool(settings.ai_enabled and settings.anthropic_api_key)


def _cost(model: str, tin: int, tout: int) -> float:
    price_in, price_out = PRICING.get(model, (3.0, 15.0))
    return (tin / 1e6) * price_in + (tout / 1e6) * price_out


def _call(model: str, system: str, user: str, tool: dict, max_tokens: int = 1200) -> tuple[dict, int, int]:
    """One Messages API call forced through a tool schema for structured output."""
    if not settings.anthropic_api_key:
        raise AIUnavailable("no ANTHROPIC_API_KEY configured")
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool["name"]},
    }
    with httpx.Client(timeout=90) as c:
        r = c.post(
            API_URL,
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            json=payload,
        )
    if r.status_code != 200:
        raise AIUnavailable(f"HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    usage = data.get("usage", {})
    for block in data.get("content", []):
        if block.get("type") == "tool_use":
            return block.get("input", {}), usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    raise AIUnavailable("model returned no structured output")


def _signal_block(signals: list[Signal]) -> str:
    """The ONLY facts the model is allowed to use."""
    if not signals:
        return "(no evidence on file)"
    lines = []
    for s in signals:
        when = s.observed_at.date().isoformat() if s.observed_at else "unknown date"
        lines.append(
            f"[signal_id={s.id}] type={s.signal_type} class={s.evidence_class} "
            f"observed={when}\n  evidence: {s.quote}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage 1 - qualification and intent
# ---------------------------------------------------------------------------

QUALIFY_TOOL = {
    "name": "record_qualification",
    "description": "Record whether this company is a genuine prospect.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_qualified": {"type": "boolean"},
            "service_category": {
                "type": "string",
                "enum": ["web_development", "computer_vision", "ai_agent", "ai_automation", "none"],
            },
            "intent_level": {"type": "string", "enum": ["high", "medium", "low"]},
            "intent_reason": {
                "type": "string",
                "description": "One or two sentences, grounded only in the listed evidence.",
            },
            "recommended_service": {"type": "string"},
            "outreach_angle": {"type": "string"},
            "cited_signal_ids": {"type": "array", "items": {"type": "integer"}},
            "disqualify_reason": {"type": "string"},
        },
        "required": ["is_qualified", "service_category", "intent_level", "intent_reason", "cited_signal_ids"],
    },
}

QUALIFY_SYSTEM = """You qualify sales leads for a technology consultancy.

Rules you must follow:
- Judge ONLY from the evidence listed. Do not use outside knowledge about the company.
- If the evidence does not support a business reason to contact them, set is_qualified=false.
- Disqualify any company that appears to be a competitor: a web design agency, software
  house, digital agency, or IT consultancy that would sell these same services.
- intent_level is "high" only when the evidence shows they publicly stated a requirement.
  An observed gap on their website is at most "medium".
- cited_signal_ids must list the signal_id values you actually relied on."""


def _qualify_tool() -> dict:
    """QUALIFY_TOOL with the user's own services added to the allowed answers."""
    keys = [s["key"] for s in catalog.all_services()] + ["none"]
    tool = dict(QUALIFY_TOOL)
    schema = dict(tool["input_schema"])
    props = dict(schema["properties"])
    props["service_category"] = {"type": "string", "enum": keys}
    schema["properties"] = props
    tool["input_schema"] = schema
    return tool


def qualify(company: Company, signals: list[Signal], service_category: str) -> dict:
    user = f"""Company: {company.name}
Country: {company.country}   City: {company.city or 'unknown'}
Industry: {company.industry or 'unknown'}
Website: {company.website or 'none on file'}
Service we are considering pitching: {service_category}

Evidence on file:
{_signal_block(signals)}"""

    out, tin, tout = _call(settings.llm_model_fast, QUALIFY_SYSTEM, user, _qualify_tool(), 900)
    valid_ids = {s.id for s in signals}
    out["cited_signal_ids"] = [i for i in out.get("cited_signal_ids", []) if i in valid_ids]
    out["_usage"] = {
        "model": settings.llm_model_fast, "tokens_in": tin, "tokens_out": tout,
        "cost_usd": _cost(settings.llm_model_fast, tin, tout),
    }
    return out


# ---------------------------------------------------------------------------
# Stage 2 - personalized outreach
# ---------------------------------------------------------------------------

WRITE_TOOL = {
    "name": "write_outreach_email",
    "description": "Write one short cold outreach email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Under 45 characters, specific to them, no emoji, no clickbait, no 'Re:'."},
            "body": {
                "type": "string",
                "description": "70-120 words, plain text, no markdown, no signature block.",
            },
            "cited_signal_ids": {"type": "array", "items": {"type": "integer"}},
            "evidence_sentence": {
                "type": "string",
                "description": "The one sentence in the body that references the evidence.",
            },
        },
        "required": ["subject", "body", "cited_signal_ids"],
    },
}

WRITE_SYSTEM_BASE = """You write short, specific, non-spammy B2B cold emails that get replies.

Structure, in this order:
1. Greet by first name if given, otherwise "Hi <Company> team".
2. One or two sentences on what you actually observed (from the evidence).
3. One sentence on why that matters for this kind of business (e.g. a restaurant
   loses bookings, a dentist loses appointments). General truth, not a claim about them.
4. One sentence on what we do, then a small, free, specific offer (e.g. "a short
   list of the 3 changes I'd make first") and a question they can answer with
   one word ("Would that be useful?"). Do not ask for a meeting or a call.
5. A one-line easy opt-out ("If it's not for you, just reply no").

Hard rules:
- Reference the actual evidence provided. Be concrete, not generic.
- 70-120 words. Plain text. No markdown, no bullet lists, no emoji.
- No pushy language, no fake urgency, no flattery, no "I hope this finds you well".
- Do not invent facts, numbers, statistics, case studies or client names.
- Do not write a signature or sign-off - that is appended afterwards.
- Do not mention the portfolio link; it is appended afterwards.
- Write in British English for UK recipients and American English for US ones.
"""

EXPLICIT_CLAUSE = """
- The evidence includes something this organisation published stating a requirement.
  You MAY refer to it directly, e.g. "I saw your tender for..." or
  "I noticed you're hiring a ...". Reference it accurately.
"""

INFERRED_CLAUSE = """
- CRITICAL: this company has NOT asked for anything. The evidence is only something
  we observed about their public website.
- You must NOT write "you said", "you mentioned", "you need", "you are looking for",
  "your request", or "I saw your post". They never said any of that.
- Use observational phrasing instead: "I came across...", "I noticed that...",
  "I had a look at your site and...". Frame it as an observation and a question,
  never as a response to a request.
"""


def personalize(
    company: Company, signals: list[Signal], contact: Contact | None,
    service_category: str, sender: dict,
) -> dict:
    explicit = any(s.evidence_class == EXPLICIT for s in signals)
    system = WRITE_SYSTEM_BASE + (EXPLICIT_CLAUSE if explicit else INFERRED_CLAUSE)

    greeting = contact.full_name.split()[0] if (contact and contact.full_name) else "there"
    user = f"""Write an outreach email.

FROM: {sender.get('sender_name')} at {sender.get('company_name')}
WE PROVIDE: {SERVICE_PITCH.get(service_category) or catalog.pitch_for(service_category) or service_category}

TO: {company.name}
Greet them as: {greeting}
Their industry: {company.industry or 'unknown'}
Their location: {company.city or company.country}
Their website: {company.website or 'none found'}

EVIDENCE (the only facts you may reference):
{_signal_block(signals)}

Cite the signal_id values you used in cited_signal_ids."""

    last_error = ""
    for attempt in range(2):
        prompt = user if attempt == 0 else user + f"\n\nYour previous attempt was rejected: {last_error}\nFix it."
        out, tin, tout = _call(settings.llm_model_smart, system, prompt, WRITE_TOOL, 1200)

        valid_ids = {s.id for s in signals}
        cited = [i for i in out.get("cited_signal_ids", []) if i in valid_ids]
        body = (out.get("body") or "").strip()
        subject = (out.get("subject") or "").strip()

        problems = []
        if not subject or not body:
            problems.append("subject and body are both required")
        if signals and not cited:
            problems.append("you must cite at least one signal_id from the evidence")
        if not explicit:
            bad = find_unsupported_claims(body)
            if bad:
                problems.append(
                    f"you claimed they asked for something ({', '.join(bad)}) but "
                    f"they never did"
                )
        words = len(body.split())
        if words > 200:
            problems.append(f"body is {words} words, keep it under 150")

        if not problems:
            out["cited_signal_ids"] = cited
            out["_usage"] = {
                "model": settings.llm_model_smart, "tokens_in": tin, "tokens_out": tout,
                "cost_usd": _cost(settings.llm_model_smart, tin, tout),
            }
            out["subject"], out["body"] = subject, body
            return out

        last_error = "; ".join(problems)
        log.warning("personalize rejected (attempt %d): %s", attempt + 1, last_error)

    raise AIUnavailable(f"draft failed validation twice: {last_error}")


# ---------------------------------------------------------------------------
# Deterministic writer - used in free mode, and when the model fails
# ---------------------------------------------------------------------------
#
# How the email is built, and why:
#   1. Greeting by first name, or "Hi <Company> team" - never "Hi there".
#   2. One specific thing we observed (and a second one if we have it). Only
#      facts already on file as evidence; nothing is invented.
#   3. Why it matters for THIS kind of business (a restaurant loses bookings,
#      a dentist loses appointments) - a general truth, not a claim about them.
#   4. A small, free, concrete offer instead of "book a call", and a question
#      they can answer with one word. Low effort to say yes gets more replies.
#   5. A P.S. with your website, and a plain "reply no and I'll stop".
# Wording varies per company (picked by a stable hash), so a run of fifty
# emails does not look like one bulk message to spam filters or to people.

# What we observed, in our own voice. {name} business, {domain} their site.
OPENERS = {
    "no_website": ["I was looking for {name} online and couldn't find a website for the business.",
                   "I tried to find {name}'s website and couldn't find one."],
    "parked_domain": ["I noticed {domain} is registered to {name} but still shows a holding page.",
                      "I visited {domain} and it's still showing a parked holding page."],
    "placeholder_site": ["I had a look at {name}'s website and it's still a placeholder page.",
                         "I visited {name}'s site and it only shows a \"coming soon\" style page."],
    "not_mobile_friendly": ["I opened {name}'s website on my phone and it doesn't adjust to the smaller screen.",
                            "I had a look at {domain} on my phone and the page doesn't resize, so it's hard to read."],
    "no_ssl": ["When I opened {domain}, my browser flagged it as \"Not secure\" because it isn't on HTTPS.",
               "I visited {name}'s website and the browser showed a \"Not secure\" warning."],
    "outdated_tech": ["I had a look at {name}'s website and it's built on some fairly dated code.",
                      "I noticed {domain} runs on older web technology."],
    "stale_content": ["I had a look at {name}'s website and the footer still shows an older year.",
                      "I noticed the copyright date on {domain} hasn't been updated for a while."],
    "missing_booking": ["I had a look at {name}'s website and couldn't find a way to book or order online.",
                        "I was on {domain} and noticed there's no way to book or order online."],
    "missing_ecommerce": ["I had a look at {name}'s website and noticed there's no online shop.",
                          "I noticed {domain} shows your products but there's no way to buy them online."],
    "slow_site": ["I opened {domain} and it was slow to load.",
                  "I had a look at {name}'s website and it took a while to start loading."],
    "weak_contact_route": ["I had a look at {name}'s website and couldn't find a contact form.",
                           "I noticed {domain} has no contact form, only an address or number."],
    "no_analytics": ["I had a look at {name}'s website and couldn't find any analytics set up on it.",
                     "I noticed {domain} doesn't seem to have any visitor analytics installed."],
    "no_social_profiles": ["I had a look at {name}'s website and it doesn't link to any social media profiles.",
                           "I noticed {domain} doesn't link to {name}'s social pages."],
    "missing_meta_description": ["I searched for {name} and the website has no description set for search results.",
                                 "I noticed {domain} has no search-result description, so Google picks random text."],
    "weak_page_title": ["I had a look at {name}'s website and the page title doesn't say what the business does.",
                        "I noticed the title of {domain} doesn't describe the business, which is what shows in Google."],
    "recently_incorporated": ["Congratulations on setting up {name}.",
                              "I saw {name} was registered recently - congratulations."],
}

# The second fact, phrased to follow the first one.
ALSO = {
    "not_mobile_friendly": "it doesn't adjust to phone screens",
    "no_ssl": "it shows a \"Not secure\" warning",
    "missing_booking": "there's no way to book or order online",
    "missing_ecommerce": "there's no online shop",
    "stale_content": "the footer still shows an older year",
    "outdated_tech": "it runs on fairly dated code",
    "slow_site": "it's slow to load",
    "weak_contact_route": "there's no contact form",
    "no_analytics": "there's no analytics set up",
    "no_social_profiles": "it doesn't link to any social profiles",
    "missing_meta_description": "it has no description for search results",
    "weak_page_title": "the page title doesn't describe the business",
}

# Why it matters, filled with what this kind of business wants more of.
WHY = {
    "no_website": "Most people look a business up online before they get in touch, so without a site some of those {wins} go elsewhere.",
    "parked_domain": "Anyone who types in your address finds nothing about you, which can cost {wins}.",
    "placeholder_site": "People who find you online can't see what you offer yet, which can cost {wins}.",
    "not_mobile_friendly": "Most visitors are on their phones now, so this can quietly cost {wins}.",
    "no_ssl": "That warning puts some visitors off before they read anything, which can cost {wins}.",
    "outdated_tech": "Older code tends to be slower and harder to keep secure.",
    "stale_content": "Small details like this can make a business look less active than it is.",
    "missing_booking": "People who want to book outside opening hours have to call, and some won't - that's {wins} lost.",
    "missing_ecommerce": "An online shop lets customers buy from you at any hour.",
    "slow_site": "Many visitors leave a page that takes more than a few seconds, which can cost {wins}.",
    "weak_contact_route": "A simple form makes it much easier for people to get in touch.",
    "no_analytics": "Without analytics there's no way to see where visitors come from or which marketing brings {wins}.",
    "no_social_profiles": "Many people check a business's social pages before they get in touch.",
    "missing_meta_description": "That snippet is often what decides whether someone clicks on you or a competitor.",
    "weak_page_title": "The title is the first thing people see in search results, so it decides a lot of clicks.",
    "recently_incorporated": "A clear website is usually one of the first things a new business needs.",
}

# What the business wants more of, by industry. Used to make "why" concrete.
WINS = [
    (r"restaurant|cafe|café|bar|pub|pizza|takeaway|bistro|brasserie|grill|bakery|food", "bookings and orders"),
    (r"dent|clinic|physio|doctor|medical|health|chiro|optic|vet", "new patient appointments"),
    (r"salon|spa|beauty|barber|hair|nail|massage", "bookings"),
    (r"hotel|guest|b&b|bnb|inn|lodge|resort", "direct bookings"),
    (r"shop|store|boutique|retail|florist|gift", "sales"),
    (r"plumb|electric|build|roof|construct|joiner|carpent|clean|garage|repair|landscap|decorat", "quote requests"),
    (r"law|solicitor|legal|account|tax|consult|insur|financ", "new client enquiries"),
    (r"gym|fitness|yoga|pilates|dance|martial|studio", "class sign-ups"),
    (r"estate agent|real estate|property|letting|realtor", "valuation requests"),
    (r"school|tutor|academy|training|driving", "student enquiries"),
    (r"wedding|venue|event|photograph", "booking enquiries"),
]

# A small, free, specific offer per bot, and how we describe what we do.
OFFERS = {
    "web_development": ["I could send you a short list of the 3 changes I'd make first - free, no strings.",
                        "Happy to send over a quick before-and-after idea of how the homepage could look - free."],
    "digital_marketing": ["I could send you a free 5-point check of how {name} shows up on Google, with the quick fixes.",
                          "Happy to put together a short list of quick wins for {name}'s online visibility - free."],
    "ai_agent": ["I could put together a 2-minute demo of an assistant answering the questions {name}'s customers ask most - free.",
                 "Happy to send a short example of how an AI assistant could handle {name}'s routine enquiries."],
    "ai_automation": ["I could send you two or three tasks we'd automate first, with the time each would save.",
                      "Happy to sketch out which routine work at {name} could run on its own - free."],
    "computer_vision": ["I could send a one-page outline of how it would work with the cameras you already have - free.",
                        "Happy to send a short note on what your existing CCTV could detect automatically."],
}
# When there is no site yet, "changes I'd make" makes no sense: offer a plan instead.
NEW_SITE_OFFERS = ["I could send you a free one-page plan of what a simple site for {name} would include, and what it would cost.",
                   "Happy to send over a quick sketch of what {name}'s homepage could look like - free, no strings."]
NEW_SITE_SIGNALS = {"no_website", "parked_domain", "placeholder_site", "recently_incorporated"}
WHAT_WE_DO = {
    "web_development": "{ours} builds fast, mobile-friendly websites that are easy to update",
    "digital_marketing": "{ours} helps businesses get found and chosen online - SEO, social and ads, set up so you can see what's working",
    "ai_agent": "{ours} builds AI assistants for chat, voice and WhatsApp that handle routine customer questions",
    "ai_automation": "{ours} builds AI automation that takes repetitive work off a team",
    "computer_vision": "{ours} builds camera-based AI - PPE and safety monitoring, fire and smoke detection, quality inspection - on existing CCTV",
}
ASKS = ["Would that be useful?", "Want me to send it over?", "Shall I send it across?"]
OPT_OUTS = ["If it's not for you, just reply \"no\" and I won't email again.",
            "Not relevant right now? Reply \"no\" and I'll leave it there.",
            "If now isn't the time, a one-word \"no\" is fine and I won't follow up."]

SUBJECTS = {
    "no_website": ["A website for {name}", "{name} online", "Quick question about {name}"],
    "parked_domain": ["{domain}", "{name}'s website", "Your holding page"],
    "placeholder_site": ["{name}'s website", "Your coming-soon page", "Quick question about {name}"],
    "not_mobile_friendly": ["{name}'s website on mobile", "Your site on phones", "Quick note about {domain}"],
    "no_ssl": ["\"Not secure\" on {domain}", "Quick fix for {name}'s website", "Your website warning"],
    "missing_booking": ["Online bookings for {name}", "Booking on {domain}", "Quick idea for {name}"],
    "missing_ecommerce": ["Selling online at {name}", "An online shop for {name}", "Quick idea for {name}"],
    "no_analytics": ["Who visits {domain}?", "Quick question about {name}'s site", "{name}'s website traffic"],
    "missing_meta_description": ["How {name} looks on Google", "{name} in search results", "Quick Google fix"],
    "weak_page_title": ["How {name} looks on Google", "Quick fix for {name}'s Google listing", "{name} in search"],
    "no_social_profiles": ["{name} on social media", "Quick idea for {name}", "{name}'s website"],
    "recently_incorporated": ["Congratulations on {name}", "A website for {name}", "Quick idea for {name}"],
}
SUBJECT_BY_BOT = {
    "web_development": ["Quick idea for {name}'s website", "{name}'s website", "A question about {domain}"],
    "digital_marketing": ["Quick wins for {name} online", "{name} on Google", "A question about {name}"],
    "ai_agent": ["An assistant for {name}'s customers", "Quick idea for {name}", "Customer questions at {name}"],
    "ai_automation": ["Saving time at {name}", "Quick idea for {name}", "Routine work at {name}"],
    "computer_vision": ["Safety monitoring at {name}", "Your cameras at {name}", "Quick idea for {name}"],
}

# Strongest evidence first: something they published beats something we noticed.
PRIORITY = ["tender_match", "explicit_request", "public_post", "hiring_for_skill", "no_website",
            "parked_domain", "placeholder_site", "missing_booking", "not_mobile_friendly", "missing_ecommerce",
            "no_ssl", "slow_site", "no_analytics", "missing_meta_description", "weak_page_title",
            "no_social_profiles", "outdated_tech", "stale_content", "weak_contact_route", "recently_incorporated"]

LEGAL_SUFFIX = re.compile(r"\b(ltd|limited|llc|inc|plc|llp|pvt|private|gmbh|pty|corp|co)\.?$", re.I)


def _pick(options: list[str], seed: str, salt: str = "") -> str:
    """The same company always gets the same wording; different companies differ."""
    if not options:
        return ""
    h = int(hashlib.sha256(f"{seed}|{salt}".encode()).hexdigest(), 16)
    return options[h % len(options)]


def _short_name(name: str) -> str:
    n = (name or "").strip()
    for _ in range(2):
        n = LEGAL_SUFFIX.sub("", n).strip(" ,.-")
    return n or name


def _wins(company: Company) -> str:
    text = " ".join(x for x in (company.industry, company.name) if x).lower()
    for pattern, wins in WINS:
        if re.search(pattern, text):
            return wins
    return "enquiries"


def _custom_closer(service_category: str, company: str) -> str:
    pitch = catalog.pitch_for(service_category)
    return f"{company} provides {pitch[0].lower() + pitch[1:].rstrip('.')}" if pitch else ""


def _explicit_opener(sig: Signal, name: str, service_category: str) -> str:
    """For things they published themselves - quoted accurately, nothing more."""
    quote = sig.quote or ""
    if sig.signal_type == "hiring_for_skill":
        role = re.search(r'advertising for "([^"]+)"', quote)
        return (f"I saw that {name} is hiring for a {role.group(1)}." if role
                else f"I saw that {name} is currently hiring in this area.")
    if sig.signal_type == "tender_match":
        title = re.search(r'(?:Tender|notice) "([^"]+)"', quote)
        return (f"I saw {name}'s published tender, \"{title.group(1)[:90]}\"." if title
                else f"I saw {name}'s published tender and wanted to get in touch.")
    label = SERVICE_LABEL_SHORT.get(service_category, "this")
    return f"I came across a page from {name} that mentions looking for help with {label}."


SERVICE_LABEL_SHORT = {
    "web_development": "a website", "digital_marketing": "marketing", "ai_agent": "an AI assistant",
    "ai_automation": "automation", "computer_vision": "camera-based monitoring",
}


def template_draft(
    company: Company, signals: list[Signal], contact: Contact | None,
    service_category: str, sender: dict,
) -> dict:
    """Facts only, drawn from signals already on file. Cannot fabricate."""
    name = _short_name(company.name)
    seed = f"{company.id}|{company.name}|{service_category}"
    first = contact.full_name.split()[0] if (contact and contact.full_name) else ""
    greeting = f"Hi {first}," if first else (f"Hi {name} team," if len(name) <= 28 else "Hello,")
    domain = company.primary_domain or (company.website or "").replace("https://", "").replace("http://", "").strip("/") \
        or "your website"
    ours = sender.get("company_name") or "We"
    website = (sender.get("company_website") or "").strip()
    fmt = {"name": name, "domain": domain}

    ranked = sorted(signals, key=lambda s: PRIORITY.index(s.signal_type) if s.signal_type in PRIORITY else 99)
    main = ranked[0] if ranked else None
    second = next((s for s in ranked[1:] if s.signal_type in ALSO and s.signal_type != (main and main.signal_type)), None)
    explicit = bool(main and main.evidence_class == EXPLICIT)

    if main is None:
        opener = f"I came across {name} and wanted to get in touch."
    elif explicit and main.signal_type in ("hiring_for_skill", "tender_match", "explicit_request", "public_post"):
        opener = _explicit_opener(main, name, service_category)
    else:
        opener = _pick(OPENERS.get(main.signal_type, [f"I came across {name} and wanted to get in touch."]),
                       seed, "open").format(**fmt)
    if second and not explicit:
        opener = opener.rstrip(".") + f", and {ALSO[second.signal_type]}."
    why = WHY.get(main.signal_type, "").format(wins=_wins(company)) if main else ""
    if main and main.signal_type == "hiring_for_skill":
        why = "If it helps, we can take the work on while you find the right person - or alongside them."

    what = (WHAT_WE_DO.get(service_category) or "").format(ours=ours) or _custom_closer(service_category, ours)
    offers = OFFERS.get(service_category, ["Happy to share a couple of ideas for {name} - free."])
    if service_category == "web_development" and main is not None and main.signal_type in NEW_SITE_SIGNALS:
        offers = NEW_SITE_OFFERS
    offer = _pick(offers, seed, "offer").format(**fmt)
    ask = _pick(ASKS, seed, "ask")
    paragraphs = [
        greeting,
        f"{opener} {why}".strip(),
        (f"{what}. " if what else "") + f"{offer} {ask}",
        _pick(OPT_OUTS, seed, "optout"),
    ]
    if website:
        paragraphs.append(f"P.S. You can see some of our recent work at {website}")

    subjects = (SUBJECTS.get(main.signal_type) if main and not explicit else None) \
        or SUBJECT_BY_BOT.get(service_category) or ["Quick idea for {name}"]
    subject = _pick(subjects, seed, "subject").format(**fmt)

    cited = [s.id for s in (main, second) if s is not None]
    return {
        "subject": subject[:78],
        "body": "\n\n".join(p for p in paragraphs if p),
        "cited_signal_ids": cited,
        "_usage": {"model": "template", "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0},
        "_fallback": True,
    }


def draft_email(
    company: Company, signals: list[Signal], contact: Contact | None,
    service_category: str, sender: dict,
) -> dict:
    """Write the outreach, preferring the model and falling back safely."""
    if available():
        try:
            return personalize(company, signals, contact, service_category, sender)
        except Exception as exc:  # noqa: BLE001
            log.warning("personalize failed for %s: %s", company.name, exc)
    return template_draft(company, signals, contact, service_category, sender)


def render_signature(sender: dict) -> str:
    """Appended to every message. Carries the CAN-SPAM postal address."""
    lines = ["", "", f"{sender.get('sender_name', '')}"]
    if sender.get("company_name"):
        lines.append(sender["company_name"])
    if sender.get("portfolio_url"):
        lines.append(f"Recent work: {sender['portfolio_url']}")
    if sender.get("company_website"):
        lines.append(sender["company_website"])
    return "\n".join(x for x in lines if x is not None)
