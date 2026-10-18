"""Lead scoring. Deterministic, versioned, and fully explainable.

Deliberately not an LLM. A score has to be stable - the same evidence must
produce the same number today and next month - and it has to be arguable:
every point traces to a named rule and the signal that triggered it. That also
makes the weights tunable later by regressing reply rates against rule hits.

Score is the sum of matched rules, clamped to 0-100.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.models import Company, Contact, Signal, as_aware, utcnow
from app.services import catalog
from app.services.compliance import ALLOWED, is_role_address

SCORE_VERSION = 1

HIGH_THRESHOLD = 70
MEDIUM_THRESHOLD = 40


@dataclass(frozen=True)
class Rule:
    code: str
    points: int
    description: str


RULES: dict[str, Rule] = {r.code: r for r in [
    # Evidence of a stated requirement - the strongest thing we can have.
    Rule("tender_match", 30, "Open tender or RFP matching our services"),
    Rule("explicit_request", 30, "Publicly stated requirement"),
    Rule("hiring_for_skill", 25, "Advertising a role needing this skill"),
    Rule("public_post", 18, "Public post describing a matching need"),

    # Observed gaps on their own website.
    Rule("no_website", 25, "No working website"),
    Rule("parked_domain", 25, "Domain registered but parked"),
    Rule("placeholder_site", 20, "Placeholder page only"),
    Rule("not_mobile_friendly", 15, "Site not mobile friendly"),
    Rule("outdated_tech", 12, "Outdated front-end technology"),
    Rule("missing_ecommerce", 10, "Retail business with no online store"),
    Rule("missing_booking", 10, "No online booking or ordering"),
    Rule("no_ssl", 8, "No HTTPS"),
    Rule("stale_content", 8, "Site content years out of date"),
    Rule("slow_site", 5, "Slow first-byte time"),
    Rule("weak_contact_route", 4, "No contact form"),

    # Fit and reachability.
    Rule("industry_match", 15, "Industry matches our target list"),
    Rule("recently_incorporated", 12, "Incorporated within 12 months"),
    Rule("recent_signal", 10, "Evidence observed in the last 30 days"),
    Rule("decision_maker", 10, "Named decision-maker identified"),
    Rule("verified_email", 8, "Deliverable business email on file"),
    Rule("role_email", 3, "Only a generic role address"),
    Rule("has_phone", 3, "Phone number on file"),
    Rule("size_band_match", 7, "Company size inside target band"),

    # Penalties.
    Rule("competitor", -40, "Appears to be a competing agency"),
    Rule("no_contact_route", -20, "No way to contact them"),
    Rule("email_not_allowed", -25, "Country policy forbids cold email"),
    Rule("suppressed", -100, "On the suppression list"),
    Rule("existing_customer", -100, "Already a customer"),
]}

# Signal types that cap out rather than stack - three outdated-tech findings is
# still one outdated site, not 36 points of evidence.
CAPPED_ONCE = {
    "outdated_tech", "missing_booking", "no_ssl", "stale_content",
    "not_mobile_friendly", "weak_contact_route", "slow_site",
}

COMPETITOR_WORDS = (
    "web design", "web development", "digital agency", "software house",
    "it solutions", "software development", "creative agency", "seo agency",
    "marketing agency", "app development", "design studio", "webdesign",
)

TARGET_INDUSTRY_HINTS = {
    "web_development": (
        "restaurant", "cafe", "hotel", "salon", "gym", "clinic", "dental",
        "retail", "shop", "estate", "law", "travel", "newly incorporated",
    ),
    "computer_vision": (
        "manufact", "factory", "warehouse", "logistic", "construction",
        "food", "packaging", "textile", "steel", "chemical", "mining",
        "industrial", "recycling", "automotive", "pharma",
    ),
    "ai_agent": (
        "software", "saas", "insurance", "travel", "recruit", "call centre",
        "call center", "property", "healthcare", "education", "finance",
    ),
    "ai_automation": (
        "accounting", "logistic", "wholesale", "marketing", "clinic",
        "professional services", "consult",
    ),
}


@dataclass
class ScoreResult:
    score: int
    intent_level: str
    breakdown: dict

    @property
    def is_high(self) -> bool:
        return self.score >= HIGH_THRESHOLD


def _hit(code: str, note: str = "", signal_id: int | None = None) -> dict:
    rule = RULES[code]
    row = {"code": code, "points": rule.points, "description": rule.description}
    if note:
        row["note"] = note[:300]
    if signal_id:
        row["signal_id"] = signal_id
    return row


def score_lead(
    *,
    company: Company,
    signals: list[Signal],
    contact: Contact | None,
    service_category: str,
    suppressed: bool = False,
) -> ScoreResult:
    hits: list[dict] = []
    used: set[str] = set()
    now = utcnow()

    for sig in signals:
        code = sig.signal_type
        if code not in RULES:
            continue
        if code in CAPPED_ONCE and code in used:
            continue
        # A gap signal only counts toward the service it belongs to.
        if sig.service_category and sig.service_category != service_category:
            continue
        used.add(code)
        hits.append(_hit(code, sig.quote or "", sig.id))

    # Recency, judged on the newest piece of evidence.
    if signals:
        newest = max(
            (as_aware(s.observed_at) for s in signals if s.observed_at), default=None
        )
        if newest:
            age_days = (now - newest).days
            if age_days <= 30:
                hits.append(_hit("recent_signal", f"newest evidence is {age_days} days old"))

    blob = f"{company.name} {company.industry or ''} {company.city or ''}".lower()
    if any(w in blob for w in COMPETITOR_WORDS):
        hits.append(_hit("competitor", f"name or industry matches an agency pattern"))

    hints = TARGET_INDUSTRY_HINTS.get(service_category) or tuple(
        h.lower() for h in catalog.industries_for(service_category))
    for hint in hints:
        if hint in (company.industry or "").lower() or hint in (company.name or "").lower():
            hits.append(_hit("industry_match", f"matched '{hint}'"))
            break

    if contact and contact.email:
        if contact.full_name:
            hits.append(_hit("decision_maker", contact.full_name))
        if contact.email_status in ("mx_ok", "valid_syntax"):
            hits.append(_hit("verified_email", contact.email))
        if is_role_address(contact.email):
            hits.append(_hit("role_email", contact.email))
    elif not company.phone:
        hits.append(_hit("no_contact_route"))

    if company.phone:
        hits.append(_hit("has_phone"))

    if company.email_policy != ALLOWED:
        hits.append(_hit("email_not_allowed", f"policy: {company.email_policy}"))

    if suppressed:
        hits.append(_hit("suppressed"))

    total = max(0, min(100, sum(h["points"] for h in hits)))
    intent = (
        "high" if total >= HIGH_THRESHOLD
        else "medium" if total >= MEDIUM_THRESHOLD
        else "low"
    )
    return ScoreResult(
        score=total,
        intent_level=intent,
        breakdown={
            "score": total,
            "version": SCORE_VERSION,
            "intent_level": intent,
            "rules": sorted(hits, key=lambda h: -abs(h["points"])),
        },
    )


def explain(breakdown: dict) -> str:
    """One-line human summary, used in the UI and the campaign log."""
    rules = breakdown.get("rules", [])
    if not rules:
        return "no scoring rules matched"
    parts = [f"{r['points']:+d} {r['description']}" for r in rules[:5]]
    return f"{breakdown.get('score', 0)}/100 — " + "; ".join(parts)
