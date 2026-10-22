"""First-run seeding and migration from the old single-file app."""

from __future__ import annotations

import logging
import os
import sqlite3

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.models import Campaign, Company, Lead, Mailbox, utcnow
from app.services.compliance import email_policy_for_country
from app.services.resolution import normalize_name

log = logging.getLogger(__name__)

# Source selection per service, free sources only.
#
# The ordering below follows one finding from live runs: the bottleneck is not
# finding companies, it is finding ones we can actually EMAIL. OpenStreetMap
# returned 24 real Bath businesses and produced zero email addresses, because
# most small venues publish no email anywhere. Tenders and web search produce
# contactable leads because a tender names a procurement officer and a search
# result IS a website with a contact page to audit.
#
# So sources that yield reachable contacts are listed first in each campaign.

DEFAULT_CAMPAIGNS = [
    {
        # Businesses whose own website proves the need. Web search leads
        # because every result is a live site the audit can mine for an
        # address; Companies House adds brand-new firms that have no site at
        # all, which is the strongest signal even though they are harder to
        # reach.
        "name": "Web Development",
        "service_category": "web_development",
        "sources": [
            "serper",            # free tier - explicit "need a website" posts
            "linkedin_search",   # free tier - LinkedIn posts, via the search index
            "contracts_finder",  # free      - public-sector website redesigns
            "companies_house",   # free key  - new UK firms, no website yet
            "openstreetmap",     # free      - fallback, low email yield
        ],
        "countries": ["GB", "US", "AU", "IE", "AE", "IN"],
        "schedule_time": "07:00",
        "daily_lead_limit": 40,
        "min_score": 45,
    },
    {
        # Industrial buyers. Tenders are the proven winner here - a live run
        # pulled a "Fire Detection & Suppression System" notice with the
        # buyer's own procurement address attached.
        "name": "Computer Vision / OpenCV",
        "service_category": "computer_vision",
        "sources": [
            "contracts_finder",  # free      - proven: named contact + verbatim need
            "find_a_tender",     # free      - higher-value industrial contracts
            "adzuna",            # free tier - hiring a CV engineer proves budget
            "sam_gov",           # free key  - US federal opportunities
            "serper",            # free tier - explicit requests
            "linkedin_search",   # free tier - LinkedIn posts, via the search index
        ],
        "countries": ["GB", "US", "AU", "IN"],
        "schedule_time": "09:00",
        "daily_lead_limit": 30,
        "min_score": 45,
    },
    {
        "name": "AI Agents & Chatbots",
        "service_category": "ai_agent",
        "sources": [
            "serper",            # free tier - "need an AI chatbot" posts
            "linkedin_search",   # free tier - LinkedIn posts, via the search index
            "adzuna",            # free tier - hiring AI engineers
            "contracts_finder",  # free      - public-sector conversational AI
            "find_a_tender",     # free
            "hacker_news",       # free      - startups stating gaps
        ],
        "countries": ["GB", "US", "AU", "SG", "AE", "IN"],
        "schedule_time": "11:00",
        "daily_lead_limit": 30,
        "min_score": 45,
    },
    {
        "name": "AI Automation",
        "service_category": "ai_automation",
        "sources": [
            "serper",            # free tier
            "linkedin_search",   # free tier
            "adzuna",            # free tier
            "contracts_finder",  # free
            "find_a_tender",     # free
        ],
        "countries": ["GB", "US", "AU", "IN", "SG"],
        "schedule_time": "13:00",
        "daily_lead_limit": 25,
        "min_score": 45,
    },
    {
        # Businesses whose own website shows a marketing gap: no analytics,
        # no search description, no social profiles. Web search finds people
        # asking for marketing help; the map finds local businesses to audit.
        "name": "Digital Marketing",
        "service_category": "digital_marketing",
        "sources": [
            "serper",            # free tier - "looking for a marketing agency" posts
            "linkedin_search",   # free tier
            "openstreetmap",     # free      - local businesses to audit
            "companies_house",   # free key  - new UK firms
            "adzuna",            # free tier - hiring marketers
        ],
        "countries": ["GB", "US", "AU", "IE", "AE", "IN"],
        "schedule_time": "15:00",
        "daily_lead_limit": 30,
        "min_score": 45,
    },
]

# A customer's own agents start smaller than the platform owner's.
CUSTOMER_LEAD_LIMIT = 25

# Which free key to get first, and what it unlocks. Surfaced on the Settings
# page so the highest-impact one is obvious.
FREE_KEY_PRIORITY = [
    {"key": "SERPER_API_KEY", "name": "Serper", "url": "https://serper.dev",
     "free": "2,500 searches free",
     "why": "Used by all four campaigns. Finds people stating a requirement in "
            "their own words, and every result is a live website the audit can "
            "mine for an email address. Highest impact by a wide margin."},
    {"key": "ADZUNA_APP_ID / ADZUNA_APP_KEY", "name": "Adzuna",
     "url": "https://developer.adzuna.com", "free": "free tier",
     "why": "Job ads. A company hiring a computer-vision engineer has both the "
            "problem and approved budget. Drives the CV and AI campaigns."},
    {"key": "COMPANIES_HOUSE_KEY", "name": "Companies House",
     "url": "https://developer.company-information.service.gov.uk", "free": "free",
     "why": "Every UK company incorporated in the last 90 days. Brand-new firms "
            "have no website yet, which is the strongest web-development signal."},
    {"key": "SAM_GOV_API_KEY", "name": "SAM.gov",
     "url": "https://sam.gov/content/api-keys", "free": "free",
     "why": "US federal opportunities. Same value as UK tenders, for the US "
            "computer-vision campaign."},
]


def _campaign_from_spec(spec: dict, owner_id: int, **over) -> Campaign:
    return Campaign(
        owner_id=owner_id, name=spec["name"], service_category=spec["service_category"],
        sources=list(spec["sources"]), countries=list(spec["countries"]),
        locations=[], rotate_cities=True, city_history={},
        schedule_time=spec["schedule_time"], schedule_days="mon-fri",
        min_score=spec["min_score"], keywords=[], industries=[], enabled=True,
        approval_mode="manual",
        daily_lead_limit=over.get("daily_lead_limit", spec["daily_lead_limit"]),
        daily_email_limit=over.get("daily_lead_limit", spec["daily_lead_limit"]),
    )


def starter_campaign(db: Session, owner_id: int, bot_key: str) -> Campaign | None:
    """Give a customer a ready-to-run agent the first time they unlock a bot."""
    has = db.execute(select(Campaign).where(
        Campaign.owner_id == owner_id, Campaign.service_category == bot_key)).scalars().first()
    if has:
        return None
    spec = next((s for s in DEFAULT_CAMPAIGNS if s["service_category"] == bot_key), None)
    if spec is None:
        from app.services import catalog
        spec = {"name": catalog.label_for(bot_key), "service_category": bot_key,
                "sources": [], "countries": ["GB", "US"], "schedule_time": "09:00",
                "daily_lead_limit": CUSTOMER_LEAD_LIMIT, "min_score": 45}
    campaign = _campaign_from_spec(spec, owner_id, daily_lead_limit=min(
        spec["daily_lead_limit"], CUSTOMER_LEAD_LIMIT))
    db.add(campaign)
    db.flush()
    return campaign


def seed_campaigns(db: Session) -> int:
    """The platform owner's own agents, one per built-in bot."""
    from app.services.tenant import admin_id

    owner = admin_id(db)
    made = 0
    for spec in DEFAULT_CAMPAIGNS:
        exists = db.execute(
            select(Campaign).where(Campaign.owner_id == owner,
                                   Campaign.service_category == spec["service_category"])
        ).scalars().first()
        if exists:
            continue
        db.add(_campaign_from_spec(spec, owner))
        made += 1
    db.commit()
    return made


def seed_mailbox(db: Session) -> None:
    from app.config import settings
    from app.services.tenant import admin_id

    if not settings.sender_email:
        return
    exists = db.execute(
        select(Mailbox).where(Mailbox.address == settings.sender_email)
    ).scalar_one_or_none()
    if exists:
        return
    db.add(Mailbox(
        owner_id=admin_id(db),
        address=settings.sender_email,
        display_name=settings.sender_name or "",
        provider="smtp",
        daily_cap=settings.per_mailbox_daily_cap,
    ))
    db.commit()


LEGACY_DB = BASE_DIR / "legacy" / "old_app.db"


def migrate_legacy(db: Session) -> int:
    """Carry leads over from the old single-file app so nothing is lost.

    The old rows have no evidence attached, so they arrive as companies with a
    low score and no draft. They are history, not something to email.
    """
    if not LEGACY_DB.exists():
        return 0
    already = db.execute(select(func.count(Company.id))).scalar_one()
    if already:
        return 0

    moved = 0
    try:
        conn = sqlite3.connect(str(LEGACY_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT l.*, c.project_type, c.location FROM leads l "
            "LEFT JOIN campaigns c ON l.campaign_id = c.id"
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        log.warning("could not read legacy database: %s", exc)
        return 0

    for r in rows:
        name = (r["business_name"] or "").strip()
        if not name:
            continue
        company = Company(
            name=name[:320], normalized_name=normalize_name(name),
            country="IN",  # the old app targeted India
            city=r["location"], address=r["address"], industry=r["category"],
            phone=r["phone"], website=r["website"],
            email_policy=email_policy_for_country("IN"),
        )
        db.add(company)
        db.flush()
        db.add(Lead(
            company_id=company.id, service_category="web_development",
            stage="discovered", status="legacy_import", score=0,
            source_kind="legacy", created_at=utcnow(),
            notes=f"Imported from the previous version (source: {r['source'] or 'unknown'})",
        ))
        moved += 1
    db.commit()
    log.info("migrated %d legacy leads", moved)
    return moved


def apply_recommended_sources(db: Session) -> int:
    """Reset every campaign to the recommended free source list.

    Used by the "Use recommended sources" button, so an existing database
    picks up a better configuration without being wiped.
    """
    from app.services.tenant import admin_id

    changed = 0
    for spec in DEFAULT_CAMPAIGNS:
        campaign = db.execute(
            select(Campaign).where(Campaign.owner_id == admin_id(db),
                                   Campaign.service_category == spec["service_category"])
        ).scalars().first()
        if not campaign:
            continue
        if campaign.sources != spec["sources"] or campaign.countries != spec["countries"]:
            campaign.sources = list(spec["sources"])
            campaign.countries = list(spec["countries"])
            campaign.rotate_cities = True
            changed += 1
    db.commit()
    return changed


def bootstrap(db: Session) -> dict:
    from app.services.billing import seed_bots

    seed_bots(db)
    # The legacy import is deliberately NOT run automatically any more. Those
    # rows carry no evidence - just a business name - so they cannot be
    # qualified, scored or written to, and they drown out real leads. Import
    # them by hand with POST /api/legacy/import if you want them as history.
    return {
        "campaigns_created": seed_campaigns(db),
        "legacy_leads_migrated": 0,
    }
