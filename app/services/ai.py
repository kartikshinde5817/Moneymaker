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

import json
import logging

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
            "subject": {"type": "string", "description": "Under 70 characters, no emoji, not clickbait."},
            "body": {
                "type": "string",
                "description": "90-150 words, plain text, no markdown, no signature block.",
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

WRITE_SYSTEM_BASE = """You write short, specific, non-spammy B2B cold emails.

Hard rules:
- Reference the actual evidence provided. Be concrete, not generic.
- 90-150 words. Plain text. No markdown, no bullet lists, no emoji.
- One clear ask: a short call. No pushy language, no fake urgency, no flattery.
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
# Deterministic fallback - used with no API key, or when the model fails
# ---------------------------------------------------------------------------

OPENERS = {
    "no_website": "I was looking for {name} online and couldn't find a website for the business.",
    "parked_domain": "I noticed {domain} is registered to {name} but still shows a holding page.",
    "placeholder_site": "I had a look at {name}'s website and it's still a placeholder page.",
    "not_mobile_friendly": "I had a look at {name}'s website on my phone, and it doesn't adjust to the smaller screen.",
    "no_ssl": "I visited {name}'s website and my browser flagged it as \"Not secure\", because it isn't served over HTTPS.",
    "outdated_tech": "I had a look at {name}'s website and noticed it's built on some fairly dated code.",
    "stale_content": "I had a look at {name}'s website and noticed the footer still shows an older year.",
    "missing_booking": "I had a look at {name}'s website and couldn't find a way to book or order online.",
    "missing_ecommerce": "I had a look at {name}'s website and noticed there's no online shop.",
    "tender_match": "I saw {name}'s published tender and wanted to get in touch.",
    "hiring_for_skill": "I saw that {name} is currently hiring in this area.",
    "recently_incorporated": "Congratulations on setting up {name}.",
}

# One plain sentence on why the finding matters - general, never a claim about them.
WHY_IT_MATTERS = {
    "no_website": "These days most customers check a business online before they get in touch.",
    "parked_domain": "Visitors who type in your address currently find nothing about you.",
    "placeholder_site": "Visitors who find you online currently can't see what you offer.",
    "not_mobile_friendly": "Most people now browse on their phones, so this can quietly cost enquiries.",
    "no_ssl": "That warning puts some visitors off before they read anything.",
    "outdated_tech": "Older code tends to load slowly and is harder to keep secure.",
    "stale_content": "Small details like this can make a business look less active than it is.",
    "missing_booking": "Customers who want to book outside opening hours currently have to call.",
    "missing_ecommerce": "An online shop lets customers buy from you at any time.",
    "tender_match": "",
    "hiring_for_skill": "If it helps, we can take on the project while you find the right person.",
    "recently_incorporated": "A clear website is usually one of the first things a new business needs.",
}

CLOSERS = {
    "web_development": "At {company} we build fast, mobile-friendly websites that are easy to update.",
    "computer_vision": "At {company} we build computer-vision systems - PPE and safety monitoring, fire and smoke detection, and quality inspection - that work with existing CCTV.",
    "ai_agent": "At {company} we build AI agents for chat, voice and WhatsApp that handle routine customer conversations.",
    "ai_automation": "At {company} we build AI automation that takes repetitive work off your team.",
}


def _custom_closer(service_category: str, company: str) -> str:
    pitch = catalog.pitch_for(service_category)
    return f"At {company} we provide {pitch[0].lower() + pitch[1:].rstrip('.')}." if pitch else ""


def template_draft(
    company: Company, signals: list[Signal], contact: Contact | None,
    service_category: str, sender: dict,
) -> dict:
    """Facts only, drawn from signals already on file. Cannot fabricate."""
    greeting = contact.full_name.split()[0] if (contact and contact.full_name) else "there"
    domain = company.primary_domain or company.website or "your domain"
    ours = sender.get("company_name") or "our studio"
    website = (sender.get("company_website") or "").strip()

    chosen = next((s for s in signals if s.signal_type in OPENERS), None)
    opener = (
        OPENERS[chosen.signal_type].format(name=company.name, domain=domain)
        if chosen else f"I came across {company.name} and wanted to get in touch."
    )
    why = WHY_IT_MATTERS.get(chosen.signal_type, "") if chosen else ""
    pitch = (CLOSERS.get(service_category, "").format(company=ours)
             or _custom_closer(service_category, ours))

    paragraphs = [
        f"Hi {greeting},",
        f"{opener} {why}".strip(),
        pitch + (f" You can see our work at {website}" if website else ""),
        "Would a quick 15-minute call next week be useful? If it's not a priority "
        "right now, just reply and let me know - I won't follow up.",
    ]
    subject_map = {
        "web_development": f"Quick idea for {company.name}'s website",
        "computer_vision": f"Camera-based safety monitoring for {company.name}",
        "ai_agent": f"An AI assistant for {company.name}'s customers",
        "ai_automation": f"Saving time on routine work at {company.name}",
    }
    if service_category == "web_development" and chosen and chosen.signal_type in (
            "no_website", "parked_domain", "placeholder_site", "recently_incorporated"):
        subject_map["web_development"] = f"A website for {company.name}"
    return {
        "subject": subject_map.get(service_category, f"A quick idea for {company.name}"),
        "body": "\n\n".join(p for p in paragraphs if p),
        "cited_signal_ids": [chosen.id] if chosen else [],
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
