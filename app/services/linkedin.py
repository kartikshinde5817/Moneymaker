"""LinkedIn outreach - a manual lane, not a scraper.

What this does NOT do, and why:

  LinkedIn publishes no API for post search or profile search. The Marketing
  and Sales Navigator APIs require an approved partnership and expose neither.
  Scraping public pages is not a US computer-crime offence after hiQ v.
  LinkedIn, but it still breaches the User Agreement, and LinkedIn enforces
  that with account bans and civil claims. There is no configuration of this
  app that makes automated LinkedIn scraping safe or lawful.

What it does instead:

  The website audit already reads the links a company publishes on its own
  site, including its LinkedIn page. That link is public, published by the
  company, and collected from the company's own server - not from LinkedIn.
  This module turns those into a queue you work by hand: open the page,
  connect, write something yourself.

  That matters most for Germany and Austria, where cold email needs prior
  consent and LinkedIn is the only sensible channel left.

If you later license a compliant LinkedIn data provider, a discovery source
slots in alongside the others without touching this file.
"""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Company, Lead, Signal
from app.services.compliance import ALLOWED

COMPANY_URL = re.compile(r"linkedin\.com/(company|school)/([^/?#]+)", re.I)

# Short prompts for a human to open with. Deliberately not a template to
# paste verbatim - LinkedIn connection notes that read as mail-merge get
# ignored, and the point of this lane is that a person writes it.
ANGLE = {
    "no_website": "They have no website at all — lead with that.",
    "parked_domain": "Their domain is registered but parked. They have already decided they want one.",
    "placeholder_site": "Their site is a placeholder. Something stalled — ask what happened.",
    "not_mobile_friendly": "Their site does not work on a phone. Most of their traffic is mobile.",
    "no_ssl": "Their site shows a 'Not secure' warning, which costs them enquiries.",
    "outdated_tech": "Their site runs on dated code and is probably expensive to change.",
    "stale_content": "Their site looks abandoned — the footer year gives it away.",
    "missing_booking": "No online booking. Every enquiry has to go through the phone.",
    "missing_ecommerce": "No online store, so they are leaving retail revenue on the table.",
    "tender_match": "They have published a tender for exactly this. Reference it directly.",
    "hiring_for_skill": "They are hiring for this skill — they have budget approved.",
    "recently_incorporated": "Brand new company. They need everything.",
    "explicit_request": "They publicly asked for this. Say where you saw it.",
}


def canonical(url: str | None) -> str:
    """Normalise to the company page URL, or '' if it is not one."""
    if not url:
        return ""
    m = COMPANY_URL.search(url)
    if not m:
        return ""
    return f"https://www.linkedin.com/{m.group(1).lower()}/{m.group(2).lower()}/"


def search_url(company_name: str) -> str:
    """LinkedIn's own search page, for when no link was published.

    This opens LinkedIn in your browser as a logged-in human. It is a link,
    not a scrape - nothing is fetched by the application.
    """
    return (
        "https://www.linkedin.com/search/results/companies/?keywords="
        + re.sub(r"\s+", "%20", (company_name or "").strip())
    )


def angle_for(signals: list[Signal]) -> tuple[str, str]:
    """Return (hint, quote) from the strongest signal on file."""
    for sig in signals:
        if sig.signal_type in ANGLE:
            return ANGLE[sig.signal_type], (sig.quote or "")
    return "No specific angle on file — read their page first.", ""


def queue(db: Session, *, only_unemailable: bool = False, limit: int = 100) -> list[dict]:
    """Leads worth approaching by hand on LinkedIn.

    `only_unemailable` narrows it to the ones where LinkedIn is the only
    channel: no email address, or a country where cold email is unlawful.
    """
    stmt = (
        select(Lead, Company)
        .join(Company, Company.id == Lead.company_id)
        .where(Lead.stage.notin_(("rejected", "closed")))
    )
    if only_unemailable:
        stmt = stmt.where(
            (Lead.contact_id.is_(None)) | (Company.email_policy != ALLOWED)
        )
    rows = db.execute(stmt.order_by(Lead.score.desc()).limit(limit)).all()

    out = []
    for lead, company in rows:
        signals = db.execute(
            select(Signal).where(Signal.company_id == company.id)
            .order_by(Signal.observed_at.desc())
        ).scalars().all()
        if not signals:
            continue  # same rule as email: no evidence, no outreach
        hint, quote = angle_for(signals)
        published = canonical(company.linkedin_url)
        out.append({
            "lead_id": lead.id,
            "company": company.name,
            "country": company.country,
            "city": company.city,
            "website": company.website,
            "score": lead.score,
            "service_category": lead.service_category,
            "email_policy": company.email_policy,
            "has_email": lead.contact_id is not None,
            # The link they publish themselves, when we have it.
            "linkedin_url": published,
            # Otherwise a search you run yourself in your own browser.
            "search_url": "" if published else search_url(company.name),
            "angle": hint,
            "evidence": quote[:400],
            "why_linkedin": (
                f"Cold email is not permitted for {company.country}"
                if company.email_policy != ALLOWED
                else "No email address found"
                if lead.contact_id is None
                else "Email is available too"
            ),
        })
    return out


def stats(db: Session) -> dict:
    total = db.execute(
        select(func.count(Company.id)).where(Company.linkedin_url.isnot(None))
    ).scalar_one()
    blocked = db.execute(
        select(func.count(Lead.id))
        .join(Company, Company.id == Lead.company_id)
        .where(Company.email_policy != ALLOWED, Lead.stage.notin_(("rejected", "closed")))
    ).scalar_one()
    no_email = db.execute(
        select(func.count(Lead.id))
        .where(Lead.contact_id.is_(None), Lead.stage.notin_(("rejected", "closed")))
    ).scalar_one()
    return {"with_linkedin_url": total, "email_blocked_country": blocked,
            "no_email_address": no_email}
