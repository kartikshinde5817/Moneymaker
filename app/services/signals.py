"""Turning website audits into typed, quotable evidence.

Every function here produces a `Signal` row carrying a verbatim, checkable
statement about a page. That statement is what the outreach email is allowed
to reference, and nothing else is.

All rule-based. No LLM decides whether a fact is true.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, Signal, Source, utcnow
from app.services.discovery.base import EXPLICIT, INFERRED
from app.services.research.audit import APPOINTMENT, HOSPITALITY, RETAIL, Audit

WEB = "web_development"


def get_or_create_source(
    db: Session, kind: str, url: str, *, status: int | None = None,
    excerpt: str = "", content_hash: str | None = None,
) -> Source:
    if url:
        existing = db.execute(
            select(Source).where(Source.kind == kind, Source.url == url)
        ).scalar_one_or_none()
        if existing:
            existing.fetched_at = utcnow()
            if status is not None:
                existing.http_status = status
            if excerpt:
                existing.excerpt = excerpt[:4000]
            return existing
    src = Source(
        kind=kind, url=url or "", http_status=status,
        excerpt=(excerpt or "")[:4000], content_hash=content_hash,
    )
    db.add(src)
    db.flush()
    return src


def record_signal(
    db: Session, company: Company, source: Source, *, signal_type: str, quote: str,
    evidence_class: str = INFERRED, service_category: str | None = None,
    observed_at: dt.datetime | None = None, detected_by: str = "rule",
    confidence: float = 1.0,
) -> Signal | None:
    """Idempotent per (company, signal_type, source)."""
    existing = db.execute(
        select(Signal).where(
            Signal.company_id == company.id,
            Signal.signal_type == signal_type,
            Signal.source_id == source.id,
        )
    ).scalar_one_or_none()
    if existing:
        existing.quote = quote[:2000]
        existing.observed_at = observed_at or existing.observed_at
        return existing
    sig = Signal(
        company_id=company.id, source_id=source.id, signal_type=signal_type,
        evidence_class=evidence_class, service_category=service_category,
        quote=quote[:2000], detected_by=detected_by, confidence=confidence,
        observed_at=observed_at or utcnow(),
    )
    db.add(sig)
    db.flush()
    return sig


def _sector(category: str | None, industry: str | None) -> set[str]:
    blob = f"{category or ''} {industry or ''}".lower()
    hits = set()
    if any(k in blob for k in HOSPITALITY):
        hits.add("hospitality")
    if any(k in blob for k in RETAIL):
        hits.add("retail")
    if any(k in blob for k in APPOINTMENT):
        hits.add("appointment")
    return hits


def signals_from_audit(
    db: Session, company: Company, audit: Audit, *, category: str | None = None
) -> list[Signal]:
    """Derive every web-development signal the audit supports."""
    name = company.name
    url = audit.url or company.website or ""
    source = get_or_create_source(
        db, "website", url, status=audit.http_status,
        excerpt=(audit.title or "") + " " + (audit.meta_description or ""),
    )
    made: list[Signal] = []

    def add(stype: str, quote: str, **kw):
        s = record_signal(
            db, company, source, signal_type=stype, quote=quote,
            service_category=WEB, **kw
        )
        if s:
            made.append(s)

    if not audit.has_site:
        reason = audit.error or "the address does not resolve"
        add("no_website",
            f"{name} has no working website: {url or 'no address on file'} "
            f"could not be loaded ({reason}).")
        return made

    if audit.is_parked:
        add("parked_domain",
            f"{name} owns {url} but it serves a domain-parking page rather than "
            f"a website.")
        return made

    if audit.is_thin:
        add("placeholder_site",
            f"{url} loads but contains only {audit.visible_chars} characters of "
            f"visible text - a placeholder rather than a working site.")

    if not audit.is_mobile_friendly:
        add("not_mobile_friendly",
            f"{url} has no mobile viewport tag, so it does not adapt to phone "
            f"screens.")

    if not audit.has_ssl:
        add("no_ssl",
            f"{url} is served over plain HTTP, so browsers show a "
            f'"Not secure" warning to visitors.')

    this_year = dt.date.today().year
    if audit.copyright_year and audit.copyright_year < this_year - 2:
        add("stale_content",
            f"The footer on {url} still reads {audit.copyright_year}.")

    if audit.tech.get("jquery_outdated"):
        add("outdated_tech",
            f"{url} loads jQuery {audit.tech.get('jquery_version')}, a version "
            f"that reached end of life years ago.")
    if audit.tech.get("flash"):
        add("outdated_tech",
            f"{url} still references Flash content, which no browser has "
            f"supported since 2020.")
    if audit.tech.get("table_layout"):
        add("outdated_tech",
            f"{url} uses table-based page layout, a pre-2010 technique.")

    sectors = _sector(category, company.industry)
    if "hospitality" in sectors and not (audit.has_booking or audit.has_ordering):
        add("missing_booking",
            f"{url} has no online booking or ordering, so enquiries arrive only "
            f"by phone.")
    if "appointment" in sectors and not audit.has_booking:
        add("missing_booking",
            f"{url} has no online appointment booking.")
    if "retail" in sectors and not audit.has_ecommerce:
        add("missing_ecommerce",
            f"{url} has no online store or checkout.")
    if not audit.has_contact_form:
        add("weak_contact_route",
            f"{url} has no contact form or email link on the page.")

    if audit.ttfb_ms and audit.ttfb_ms > 2500:
        add("slow_site",
            f"{url} took {audit.ttfb_ms} ms to send its first byte.")

    return made


def store_raw_signals(db: Session, company: Company, raw_signals: list, source_kind: str) -> list[Signal]:
    """Persist the evidence a discovery source found at collection time."""
    made: list[Signal] = []
    for rs in raw_signals:
        src = get_or_create_source(db, source_kind, rs.source_url or "", excerpt=rs.quote)
        s = record_signal(
            db, company, src, signal_type=rs.signal_type, quote=rs.quote,
            evidence_class=rs.evidence_class, service_category=rs.service_category,
            observed_at=rs.observed_at, confidence=rs.confidence,
        )
        if s:
            made.append(s)
    return made


def company_signals(db: Session, company_id: int) -> list[Signal]:
    return list(
        db.execute(
            select(Signal)
            .where(Signal.company_id == company_id)
            .order_by(Signal.observed_at.desc())
        ).scalars().all()
    )


def has_explicit_request(signals: list[Signal]) -> bool:
    return any(s.evidence_class == EXPLICIT for s in signals)
