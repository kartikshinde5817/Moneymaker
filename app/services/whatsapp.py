"""WhatsApp outreach - prepared here, sent by a person.

Why it is not automated, plainly:

  * Meta's WhatsApp Cloud API only delivers *template* messages to numbers
    that have opted in, and every template is reviewed before approval. A
    scraped number has not opted in, so there is no compliant API path.
  * Unofficial libraries that drive WhatsApp Web get the number banned,
    usually within days of bulk sending.
  * In most of the countries this app targets, unsolicited commercial
    messaging to a phone is regulated more tightly than email.

So the app writes the message, normalises the number, and produces a wa.me
link. You click, WhatsApp opens with the text ready, and you press send. One
click per prospect, a human on the hook for the channel where that matters.

If you later get an approved template and an opt-in flow, the send step slots
in here without touching anything else.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, Lead, Signal, WhatsAppMessage, utcnow
from app.services.resolution import normalize_phone

# Numbers that can never be a prospect's mobile.
BAD_PREFIXES = ("+1800", "+1888", "+1877", "+1866", "+44800", "+44808", "+44300")

OPENERS = {
    "no_website": "I noticed {name} doesn't have a website yet",
    "parked_domain": "I noticed {name}'s domain is registered but not built out yet",
    "placeholder_site": "I noticed {name}'s site is still a placeholder page",
    "not_mobile_friendly": "I had a look at {name}'s website on a phone and it doesn't adapt to the screen",
    "no_ssl": "I noticed {name}'s website still shows a 'Not secure' warning in browsers",
    "outdated_tech": "I had a look at {name}'s website and it's running on some fairly dated code",
    "stale_content": "I noticed the footer on {name}'s website still shows an older year",
    "missing_booking": "I noticed there's no way to book or order online from {name}'s site",
    "missing_ecommerce": "I noticed there's no online store on {name}'s site",
    "tender_match": "I saw {name}'s published tender",
    "hiring_for_skill": "I saw {name} is hiring in this area",
    "recently_incorporated": "I saw {name} was recently set up",
    "no_analytics": "I noticed {name}'s website has no analytics on it",
    "no_social_profiles": "I noticed {name}'s website doesn't link to any social media",
    "missing_meta_description": "I noticed {name}'s website has no search description set",
    "weak_page_title": "I noticed {name}'s website title doesn't describe the business",
}

CLOSERS = {
    "web_development": "We build websites for businesses like yours. Would it be worth a quick chat?",
    "computer_vision": "We build AI camera systems for safety and quality checks that run on existing CCTV. Worth a quick chat?",
    "ai_agent": "We build AI assistants that handle routine customer messages. Worth a quick chat?",
    "ai_automation": "We automate repetitive back-office work with AI. Worth a quick chat?",
    "digital_marketing": "We help local businesses get found online - SEO, social media and ads. Worth a quick chat?",
}


def usable_number(phone: str | None, country: str = "GB") -> str:
    """Return E.164 if this looks like a real, reachable number, else ''."""
    number = normalize_phone(phone, country)
    if not number or len(re.sub(r"\D", "", number)) < 8:
        return ""
    if any(number.startswith(p) for p in BAD_PREFIXES):
        return ""
    return number


def compose(company: Company, signals: list[Signal], service: str, sender: dict) -> tuple[str, list[int]]:
    """Short, factual, and grounded in the same evidence the email uses."""
    chosen = next((s for s in signals if s.signal_type in OPENERS), None)
    opener = (
        OPENERS[chosen.signal_type].format(name=company.name)
        if chosen else f"I came across {company.name}"
    )
    # Build the introduction from whichever identity fields are actually set,
    # so a blank SENDER_NAME never produces "this is  from Atkind".
    name = (sender.get("sender_name") or "").strip()
    company = (sender.get("company_name") or "").strip()
    if name and company:
        intro = f"Hi, this is {name} from {company}"
    elif name or company:
        intro = f"Hi, this is {name or company}"
    else:
        intro = "Hi"
    body = (
        f"{intro}. {opener}. "
        + CLOSERS.get(service, "Would it be worth a quick chat?")
        + "\n\nIf not, no problem at all - just let me know and I won't message again."
    )
    return body, ([chosen.id] if chosen else [])


def wa_link(phone_e164: str, body: str) -> str:
    """wa.me opens WhatsApp with the text pre-filled, ready to send."""
    return f"https://wa.me/{re.sub(r'[^0-9]', '', phone_e164)}?text={quote(body)}"


def queue(db: Session, lead: Lead, company: Company, signals: list[Signal], sender: dict) -> WhatsAppMessage | None:
    """Prepare a message if there is a usable number and no duplicate."""
    number = usable_number(company.phone, company.country)
    if not number:
        return None
    existing = db.execute(
        select(WhatsAppMessage).where(
            WhatsAppMessage.lead_id == lead.id,
            WhatsAppMessage.status.in_(("pending", "opened", "sent")),
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    body, cited = compose(company, signals, lead.service_category, sender)
    message = WhatsAppMessage(
        owner_id=lead.owner_id, lead_id=lead.id, phone_e164=number, body=body,
        cited_signal_ids=cited,
    )
    db.add(message)
    db.flush()
    return message


def mark(db: Session, message_id: int, status: str, owner_id: int | None = None) -> bool:
    message = db.get(WhatsAppMessage, message_id)
    if not message or (owner_id is not None and message.owner_id != owner_id):
        return False
    message.status = status
    if status == "opened":
        message.opened_at = utcnow()
    elif status == "sent":
        message.marked_sent_at = utcnow()
    return True
