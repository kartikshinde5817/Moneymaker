"""Database schema.

The shape here follows ARCHITECTURE.md section 4. Two tables carry most of the
design weight:

  company_identifiers  - the deduplication engine. A UNIQUE(kind, value) index
                         makes the insert itself the duplicate check, so the
                         same business found via Places, a tender and its own
                         website collapses into one company row.

  signals              - the evidence store. Every claim the system makes about
                         a prospect is a row here with a source URL and a
                         verbatim quote. Outreach may only cite these, which is
                         what stops the AI inventing requirements.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def as_aware(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite drops tzinfo on round-trip, so comparisons need this coercion."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


class Base(DeclarativeBase):
    pass


TS = DateTime(timezone=True)


# ---------------------------------------------------------------------------
# Auth & settings
# ---------------------------------------------------------------------------


# Columns added after the first release carry a server_default (or are
# nullable) so the startup migration can add them to a table that already
# has rows.
OWNER_FK = "users.id"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # admin runs the platform; customer buys bots and uses them on its own data
    role: Mapped[str] = mapped_column(String(32), default="customer")
    name: Mapped[str | None] = mapped_column(String(160))
    company: Mapped[str | None] = mapped_column(String(160))
    # active | suspended
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    # Mobile number in international form (+919876543210), asked at sign-up.
    phone: Mapped[str | None] = mapped_column(String(24))
    # The one free trial each account gets: which bot, and when it started.
    trial_bot: Mapped[str | None] = mapped_column(String(32))
    trial_started_at: Mapped[dt.datetime | None] = mapped_column(TS)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(TS)
    # Google Authenticator (TOTP). Set once the QR code has been scanned and
    # confirmed; until then the account is password-only.
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    # Set when the person clicks the link we emailed them (or resets their
    # password by email, which proves the same thing). Null = not verified yet.
    email_verified_at: Mapped[dt.datetime | None] = mapped_column(TS)
    # When they ticked "I agree to the Terms and Privacy Policy" at signup.
    terms_accepted_at: Mapped[dt.datetime | None] = mapped_column(TS)


class AuthToken(Base):
    """A one-time link sent by email: verify an address, or reset a password.

    Only a hash of the token is stored, so a copy of the database cannot be
    used to take over an account.
    """

    __tablename__ = "auth_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # verify | reset
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(TS)
    used_at: Mapped[dt.datetime | None] = mapped_column(TS)


class Session(Base):
    """Server-side login sessions. The cookie holds only the opaque token."""

    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(TS)
    last_seen_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class AccountSettings(Base):
    """Each customer's own sending identity and mailbox.

    Passwords are stored encrypted (see services/tenant.py). For the admin
    account, empty fields fall back to the values in the environment, which
    is how the app was configured before it had customers.
    """

    __tablename__ = "account_settings"
    owner_id: Mapped[int] = mapped_column(ForeignKey(OWNER_FK, ondelete="CASCADE"), primary_key=True)
    sender_name: Mapped[str | None] = mapped_column(String(160))
    sender_email: Mapped[str | None] = mapped_column(String(320))
    company_name: Mapped[str | None] = mapped_column(String(160))
    company_website: Mapped[str | None] = mapped_column(String(512))
    postal_address: Mapped[str | None] = mapped_column(Text)
    privacy_url: Mapped[str | None] = mapped_column(String(512))
    smtp_host: Mapped[str | None] = mapped_column(String(255))
    smtp_port: Mapped[int | None] = mapped_column(Integer)
    smtp_use_ssl: Mapped[bool | None] = mapped_column(Boolean)
    smtp_user: Mapped[str | None] = mapped_column(String(320))
    smtp_pass_enc: Mapped[str | None] = mapped_column(Text)
    imap_host: Mapped[str | None] = mapped_column(String(255))
    imap_port: Mapped[int | None] = mapped_column(Integer)
    imap_user: Mapped[str | None] = mapped_column(String(320))
    imap_pass_enc: Mapped[str | None] = mapped_column(Text)
    imap_folder: Mapped[str | None] = mapped_column(String(120))
    daily_cap: Mapped[int | None] = mapped_column(Integer)
    timezone: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Product: bots, subscriptions, payments
# ---------------------------------------------------------------------------


class Bot(Base):
    """A bot customers can subscribe to. One per service the platform sells."""

    __tablename__ = "bots"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True)  # = service_category
    name: Mapped[str] = mapped_column(String(80))
    tagline: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    features: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    sort_order: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    # Whole rupees / dollars. Razorpay wants the smallest unit, so x100 at checkout.
    price_inr_month: Mapped[int] = mapped_column(Integer, default=1999)
    price_inr_year: Mapped[int] = mapped_column(Integer, default=19999)
    price_usd_month: Mapped[int] = mapped_column(Integer, default=29)
    price_usd_year: Mapped[int] = mapped_column(Integer, default=290)
    # What the agent does. Set by the admin, shown to customers, and enforced
    # on every customer's agent for this bot. Null = not set yet; the app then
    # falls back to the built-in defaults (see services/billing.py).
    countries: Mapped[list | None] = mapped_column(JSON)
    sources: Mapped[list | None] = mapped_column(JSON)
    schedule_days: Mapped[str | None] = mapped_column(String(16))
    schedule_time: Mapped[str | None] = mapped_column(String(5))
    leads_per_run: Mapped[int | None] = mapped_column(Integer)
    min_score: Mapped[int | None] = mapped_column(Integer)
    # Plan lengths in days, and the free trial in hours (null = platform default).
    trial_hours: Mapped[int | None] = mapped_column(Integer)
    month_days: Mapped[int | None] = mapped_column(Integer)
    year_days: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class Subscription(Base):
    """A period of access to one bot. Access = any row whose window covers now."""

    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey(OWNER_FK, ondelete="CASCADE"), index=True)
    bot_key: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # trial | paid | grant
    period: Mapped[str] = mapped_column(String(8))  # day | month | year | custom
    currency: Mapped[str | None] = mapped_column(String(3))
    amount: Mapped[int] = mapped_column(Integer, default=0)  # smallest unit
    starts_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(TS, index=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(TS)
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey(OWNER_FK, ondelete="CASCADE"), index=True)
    bot_key: Mapped[str] = mapped_column(String(32))
    period: Mapped[str] = mapped_column(String(8))  # month | year
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[int] = mapped_column(Integer)  # smallest unit (paise / cents)
    provider: Mapped[str] = mapped_column(String(16), default="razorpay")
    order_id: Mapped[str] = mapped_column(String(64), unique=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # created | paid | failed
    status: Mapped[str] = mapped_column(String(16), default="created", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, index=True)
    paid_at: Mapped[dt.datetime | None] = mapped_column(TS)


class Setting(Base):
    """Runtime-editable operational settings. Secrets stay in the environment."""

    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------------------
# Companies, identity and contacts
# ---------------------------------------------------------------------------


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(320))
    normalized_name: Mapped[str] = mapped_column(String(320), index=True)
    primary_domain: Mapped[str | None] = mapped_column(String(255), index=True)
    country: Mapped[str] = mapped_column(String(2), index=True, default="GB")
    region: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(160))
    address: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Numeric(9, 6))
    lng: Mapped[float | None] = mapped_column(Numeric(9, 6))
    industry: Mapped[str | None] = mapped_column(String(120))
    size_band: Mapped[str | None] = mapped_column(String(32))
    phone: Mapped[str | None] = mapped_column(String(64))
    website: Mapped[str | None] = mapped_column(String(512))
    # Taken from the links the company publishes on its own website, never
    # from scraping LinkedIn. Used for the manual outreach lane.
    linkedin_url: Mapped[str | None] = mapped_column(String(512))
    socials: Mapped[dict] = mapped_column(JSON, default=dict)
    # allowed | manual_only | blocked  - set from the country policy table so no
    # code path can email a jurisdiction that forbids it.
    email_policy: Mapped[str] = mapped_column(String(16), default="allowed")
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)

    identifiers: Mapped[list["CompanyIdentifier"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    signals: Mapped[list["Signal"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class CompanyIdentifier(Base):
    """One row per known identifier. UNIQUE(kind, value) performs the dedup."""

    __tablename__ = "company_identifiers"
    __table_args__ = (UniqueConstraint("kind", "value", name="uq_identifier"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    # domain | phone_e164 | google_place_id | registry_no | linkedin_url | osm_id
    kind: Mapped[str] = mapped_column(String(32))
    value: Mapped[str] = mapped_column(String(512))

    company: Mapped[Company] = relationship(back_populates="identifiers")


class CompanyMerge(Base):
    __tablename__ = "company_merges"
    id: Mapped[int] = mapped_column(primary_key=True)
    winner_id: Mapped[int] = mapped_column(Integer)
    loser_id: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(255))
    merged_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("company_id", "email", name="uq_contact_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    full_name: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    # unknown | valid_syntax | mx_ok | bounced | invalid | role_address
    email_status: Mapped[str] = mapped_column(String(24), default="unknown")
    is_decision_maker: Mapped[bool] = mapped_column(Boolean, default=False)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)

    company: Mapped[Company] = relationship(back_populates="contacts")


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class Source(Base):
    """Provenance. Every fact traces back to one of these rows."""

    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    # google_places | website | companies_house | job_board | tender | serp | osm
    kind: Mapped[str] = mapped_column(String(32), index=True)
    url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    excerpt: Mapped[str | None] = mapped_column(Text)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signal_company_time", "company_id", "observed_at"),
        UniqueConstraint("company_id", "signal_type", "source_id", name="uq_signal"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    signal_type: Mapped[str] = mapped_column(String(64))
    # explicit_request  -> they publicly stated the requirement
    # inferred_opportunity -> we observed a gap; must never be phrased as a request
    evidence_class: Mapped[str] = mapped_column(String(24), default="inferred_opportunity")
    service_category: Mapped[str | None] = mapped_column(String(32))
    quote: Mapped[str | None] = mapped_column(Text)
    detected_by: Mapped[str] = mapped_column(String(8), default="rule")  # rule | llm
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    observed_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)

    company: Mapped[Company] = relationship(back_populates="signals")
    source: Mapped[Source] = relationship()


class WebsiteAudit(Base):
    __tablename__ = "website_audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str | None] = mapped_column(String(512))
    has_site: Mapped[bool] = mapped_column(Boolean, default=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    has_ssl: Mapped[bool] = mapped_column(Boolean, default=False)
    is_mobile_friendly: Mapped[bool] = mapped_column(Boolean, default=False)
    has_booking: Mapped[bool] = mapped_column(Boolean, default=False)
    has_ecommerce: Mapped[bool] = mapped_column(Boolean, default=False)
    has_contact_form: Mapped[bool] = mapped_column(Boolean, default=False)
    copyright_year: Mapped[int | None] = mapped_column(Integer)
    page_bytes: Mapped[int | None] = mapped_column(Integer)
    ttfb_ms: Mapped[int | None] = mapped_column(Integer)
    tech: Mapped[dict] = mapped_column(JSON, default=dict)
    emails_found: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    audited_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


# ---------------------------------------------------------------------------
# Campaigns and leads
# ---------------------------------------------------------------------------


class Service(Base):
    """A service the user sells, beyond the four built in to the code.

    Built-in services keep their hand-tuned search phrases in code. These rows
    supply the same four things for a service the user defines: what we sell,
    what to search for, what people write when they need it, and which kinds of
    business tend to buy it.
    """

    __tablename__ = "services"
    id: Mapped[int] = mapped_column(primary_key=True)
    # Stored on leads and campaigns in a String(32) column, so the key is capped there.
    key: Mapped[str] = mapped_column(String(32), unique=True)
    label: Mapped[str] = mapped_column(String(80))
    pitch: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    intent_phrases: Mapped[list] = mapped_column(JSON, default=list)
    industries: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey(OWNER_FK, ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    # web_development | computer_vision | ai_agent | ai_automation
    service_category: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    industries: Mapped[list] = mapped_column(JSON, default=list)
    locations: Mapped[list] = mapped_column(JSON, default=list)
    countries: Mapped[list] = mapped_column(JSON, default=lambda: ["GB", "US"])
    # Rotate fresh cities in daily instead of re-searching the same place.
    rotate_cities: Mapped[bool] = mapped_column(Boolean, default=True)
    city_history: Mapped[dict] = mapped_column(JSON, default=dict)
    schedule_time: Mapped[str] = mapped_column(String(5), default="07:00")  # HH:MM
    schedule_days: Mapped[str] = mapped_column(String(40), default="mon-fri")
    daily_lead_limit: Mapped[int] = mapped_column(Integer, default=25)
    daily_email_limit: Mapped[int] = mapped_column(Integer, default=20)
    approval_mode: Mapped[str] = mapped_column(String(16), default="manual")  # manual|auto
    min_score: Mapped[int] = mapped_column(Integer, default=55)
    # False until the user has chosen their countries, cities, start time and
    # days. A new agent starts empty: it never runs on someone else's choices.
    configured: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class CampaignRun(Base):
    __tablename__ = "campaign_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey(OWNER_FK, ondelete="CASCADE"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    started_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(16), default="running")
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    log: Mapped[list] = mapped_column(JSON, default=list)
    # Per-node state for the live workflow canvas. Written by StageTracker as
    # the pipeline advances, so the dashboard shows the real run, not a mock.
    stage_state: Mapped[dict] = mapped_column(JSON, default=dict)
    current_item: Mapped[str | None] = mapped_column(String(320))
    error: Mapped[str | None] = mapped_column(Text)


class SearchQuery(Base):
    __tablename__ = "search_queries"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("campaign_runs.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(32))
    query: Mapped[str] = mapped_column(Text)
    results_count: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        # One lead per company per service *per customer*: two customers may
        # both prospect the same business.
        UniqueConstraint("owner_id", "company_id", "service_category",
                         name="uq_lead_owner_company_service"),
        Index("ix_lead_status_action", "status", "next_action_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey(OWNER_FK, ondelete="CASCADE"), index=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"))
    service_category: Mapped[str] = mapped_column(String(32), index=True)
    intent_level: Mapped[str] = mapped_column(String(8), default="low")
    intent_reason: Mapped[str | None] = mapped_column(Text)
    score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    score_version: Mapped[int] = mapped_column(Integer, default=1)
    # discovered|researched|qualified|scored|rejected|drafted|pending_approval
    # |queued|sent|replied|bounced|unsubscribed|closed
    stage: Mapped[str] = mapped_column(String(24), default="discovered", index=True)
    status: Mapped[str] = mapped_column(String(24), default="new", index=True)
    source_kind: Mapped[str | None] = mapped_column(String(32), index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, index=True)
    last_contact_at: Mapped[dt.datetime | None] = mapped_column(TS)
    next_action_at: Mapped[dt.datetime | None] = mapped_column(TS)

    company: Mapped[Company] = relationship()
    contact: Mapped[Contact | None] = relationship()


class AIAnalysis(Base):
    __tablename__ = "ai_analyses"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    agent: Mapped[str] = mapped_column(String(32))  # qualify | research | personalize
    model: Mapped[str] = mapped_column(String(64))
    input_signal_ids: Mapped[list] = mapped_column(JSON, default=list)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, index=True)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


class Mailbox(Base):
    __tablename__ = "mailboxes"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey(OWNER_FK, ondelete="CASCADE"), index=True)
    address: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(160), default="")
    provider: Mapped[str] = mapped_column(String(16), default="smtp")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_cap: Mapped[int] = mapped_column(Integer, default=40)
    # Warmup: day 1-7 caps at 8, week 2 at 20, then the full cap. Ramping this
    # is what keeps a new sending domain out of spam folders.
    warmup_started_at: Mapped[dt.datetime | None] = mapped_column(TS)
    last_sent_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class EmailMessage(Base):
    __tablename__ = "email_messages"
    __table_args__ = (Index("ix_msg_status_sched", "status", "scheduled_for"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey(OWNER_FK, ondelete="CASCADE"), index=True)
    mailbox_id: Mapped[int | None] = mapped_column(ForeignKey("mailboxes.id"))
    to_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    sequence_step: Mapped[int] = mapped_column(Integer, default=0)
    # draft|pending_approval|queued|sending|sent|failed|rejected|cancelled
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    cited_signal_ids: Mapped[list] = mapped_column(JSON, default=list)
    unsubscribe_token: Mapped[str | None] = mapped_column(String(64), index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    scheduled_for: Mapped[dt.datetime | None] = mapped_column(TS)
    approved_by: Mapped[str | None] = mapped_column(String(160))
    approved_at: Mapped[dt.datetime | None] = mapped_column(TS)
    sent_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, index=True)


class EmailEvent(Base):
    __tablename__ = "email_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("email_messages.id", ondelete="CASCADE"), index=True
    )
    # queued|sent|delivered|bounce|complaint|click|reply|unsubscribe|failed
    type: Mapped[str] = mapped_column(String(24), index=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class WhatsAppMessage(Base):
    """A click-to-send WhatsApp message waiting for a human.

    Cold WhatsApp to a scraped number cannot be automated: Meta's Cloud API
    only delivers pre-approved templates to people who opted in, and sending
    unsolicited messages gets the number banned. So the app prepares the
    message and a wa.me link, and a person sends it. That keeps a human in
    the loop on the channel where the rules are strictest.
    """

    __tablename__ = "whatsapp_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey(OWNER_FK, ondelete="CASCADE"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    phone_e164: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text)
    # pending | opened | sent | skipped
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    cited_signal_ids: Mapped[list] = mapped_column(JSON, default=list)
    opened_at: Mapped[dt.datetime | None] = mapped_column(TS)
    marked_sent_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, index=True)


class AppState(Base):
    """Small shared values, e.g. when the PC sender last checked in."""

    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class Suppression(Base):
    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("owner_id", "scope", "value", name="uq_suppression_owner"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    # The customer whose list this is. NULL = platform-wide (hard bounces:
    # an address that does not exist is dead for everyone).
    owner_id: Mapped[int | None] = mapped_column(ForeignKey(OWNER_FK, ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(8), default="email")  # email | domain
    value: Mapped[str] = mapped_column(String(320), index=True)
    reason: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(160), default="system")
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, index=True)
