"""First-run seeding and migration from the old single-file app."""

from __future__ import annotations

import logging
import os
import sqlite3

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.models import Campaign, Company, Lead, utcnow
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


def apply_bot_limits(campaign: Campaign, bot) -> bool:
    """Hold a customer's agent inside what its bot allows.

    The admin sets, per bot, the countries it may search, the sources it uses,
    the days it runs, the most leads per run and the lowest score it may
    draft at. Returns True when something had to change.
    """
    from app.services.billing import fill_agent_defaults

    fill_agent_defaults(bot)
    before = (list(campaign.countries or []), list(campaign.sources or []),
              campaign.schedule_days, campaign.daily_lead_limit, campaign.min_score)
    allowed = list(bot.countries)
    campaign.countries = [c for c in (campaign.countries or []) if c in allowed] or allowed
    campaign.sources = list(bot.sources or [])
    campaign.schedule_days = bot.schedule_days
    campaign.daily_lead_limit = max(1, min(campaign.daily_lead_limit or bot.leads_per_run, bot.leads_per_run))
    campaign.min_score = max(campaign.min_score if campaign.min_score is not None else 0, bot.min_score)
    return before != (list(campaign.countries), list(campaign.sources), campaign.schedule_days,
                      campaign.daily_lead_limit, campaign.min_score)


def starter_campaign(db: Session, owner_id: int, bot_key: str) -> Campaign | None:
    """Give a customer their ready-made agent the first time they unlock a bot.

    There is exactly one agent per bot and customers cannot create more: what
    it searches is set on the bot by the admin.
    """
    from app.services import billing, catalog

    has = db.execute(select(Campaign).where(
        Campaign.owner_id == owner_id, Campaign.service_category == bot_key)).scalars().first()
    if has:
        return None
    bot = db.execute(select(billing.Bot).where(billing.Bot.key == bot_key)).scalar_one_or_none()
    if bot is None:
        return None
    billing.fill_agent_defaults(bot)
    campaign = Campaign(
        owner_id=owner_id, name=catalog.label_for(bot_key), service_category=bot_key,
        sources=list(bot.sources or []), countries=list(bot.countries), locations=[],
        rotate_cities=True, city_history={}, schedule_time=bot.schedule_time,
        schedule_days=bot.schedule_days, min_score=bot.min_score, keywords=[], industries=[],
        enabled=True, approval_mode="manual",
        daily_lead_limit=bot.leads_per_run, daily_email_limit=bot.leads_per_run,
    )
    db.add(campaign)
    db.flush()
    return campaign


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


def apply_recommended_sources(db: Session, owner_id: int) -> int:
    """Reset one account's agents to the recommended free source list.

    Used by the "Use recommended sources" button, so an existing database
    picks up a better configuration without being wiped.
    """
    changed = 0
    for spec in DEFAULT_CAMPAIGNS:
        campaign = db.execute(
            select(Campaign).where(Campaign.owner_id == owner_id,
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
    """Startup seeding: the bot catalogue only.

    The admin account runs the platform and has no agents of its own, so no
    campaigns or mailboxes are created here. Customers get a ready-made agent
    when they unlock a bot (see starter_campaign).
    """
    from app.services.billing import seed_bots

    seed_bots(db)
    return {}
