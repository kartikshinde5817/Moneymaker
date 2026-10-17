"""Entity resolution - turning many discovered records into one company.

The same restaurant turns up in Places, in a tender, and on its own website
with three different name spellings. Without this, it gets emailed three times,
which is the fastest way to a spam complaint.

Matching runs strongest-signal-first and stops at the first conclusive hit:

  1. registrable domain   conclusive
  2. registry number      conclusive
  3. google place id      conclusive
  4. phone (E.164)        strong, but shared reception numbers exist in office
                          parks, so it also needs name similarity
  5. normalised name      only within the same city, and only above a high
                          similarity threshold

Anything weaker is left as a separate company rather than guessed at. A missed
merge costs one duplicate row; a wrong merge emails the wrong business.
"""

from __future__ import annotations

import difflib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, CompanyIdentifier, Contact
from app.services.compliance import email_policy_for_country
from app.services.research.fetcher import registrable_domain

LEGAL_SUFFIXES = re.compile(
    r"\b(ltd|limited|llc|l\.l\.c|inc|incorporated|plc|llp|lp|gmbh|bv|nv|pty|"
    r"corp|corporation|co|company|holdings|group|services|solutions|the)\b",
    re.I,
)
PUNCT = re.compile(r"[^a-z0-9\s]")
WS = re.compile(r"\s+")

# Dialling codes for the countries in scope, for E.164 normalisation.
COUNTRY_DIAL = {"GB": "44", "US": "1", "AE": "971", "IE": "353", "DE": "49"}


def normalize_name(name: str) -> str:
    s = (name or "").lower()
    s = PUNCT.sub(" ", s)
    s = LEGAL_SUFFIXES.sub(" ", s)
    return WS.sub(" ", s).strip()


def normalize_phone(phone: str | None, country: str = "GB") -> str:
    """Best-effort E.164. Wrong guesses just mean a missed merge, never a bad one."""
    if not phone:
        return ""
    digits = re.sub(r"[^\d+]", "", phone)
    if digits.startswith("+"):
        return "+" + re.sub(r"\D", "", digits)
    digits = re.sub(r"\D", "", digits)
    if not digits:
        return ""
    dial = COUNTRY_DIAL.get((country or "GB").upper(), "")
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0"):
        return f"+{dial}{digits[1:]}" if dial else ""
    if dial and digits.startswith(dial):
        return "+" + digits
    return f"+{dial}{digits}" if dial else "+" + digits


def name_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def _by_identifier(db: Session, kind: str, value: str) -> Company | None:
    if not value:
        return None
    row = db.execute(
        select(CompanyIdentifier).where(
            CompanyIdentifier.kind == kind, CompanyIdentifier.value == value
        )
    ).scalar_one_or_none()
    return db.get(Company, row.company_id) if row else None


def add_identifier(db: Session, company: Company, kind: str, value: str) -> None:
    """Idempotent. Never repoints an identifier already owned by another company."""
    if not value:
        return
    existing = db.execute(
        select(CompanyIdentifier).where(
            CompanyIdentifier.kind == kind, CompanyIdentifier.value == value
        )
    ).scalar_one_or_none()
    if existing:
        return
    db.add(CompanyIdentifier(company_id=company.id, kind=kind, value=value))
    db.flush()


def find_company(db: Session, prospect) -> tuple[Company | None, str]:
    """Return (company, how_it_matched) or (None, '')."""
    domain = registrable_domain(prospect.website or "")
    if domain:
        hit = _by_identifier(db, "domain", domain)
        if hit:
            return hit, "domain"
        hit = db.execute(
            select(Company).where(Company.primary_domain == domain)
        ).scalar_one_or_none()
        if hit:
            return hit, "domain"

    for kind, value in prospect.identifiers:
        if kind in ("registry_no", "google_place_id", "osm_id", "tender_ocid"):
            hit = _by_identifier(db, kind, value)
            if hit:
                return hit, kind

    phone = normalize_phone(prospect.phone, prospect.country)
    if phone:
        hit = _by_identifier(db, "phone_e164", phone)
        # Shared reception numbers mean phone alone is not enough.
        if hit and name_similarity(hit.name, prospect.name) >= 0.6:
            return hit, "phone+name"

    norm = normalize_name(prospect.name)
    if norm and prospect.city:
        candidates = db.execute(
            select(Company).where(
                Company.normalized_name == norm, Company.city == prospect.city
            )
        ).scalars().all()
        if candidates:
            return candidates[0], "name+city"
    return None, ""


def upsert_company(db: Session, prospect) -> tuple[Company, bool]:
    """Find or create the company for a prospect. Returns (company, created)."""
    company, matched_by = find_company(db, prospect)
    created = False
    domain = registrable_domain(prospect.website or "")

    if company is None:
        company = Company(
            name=prospect.name.strip()[:320],
            normalized_name=normalize_name(prospect.name),
            primary_domain=domain or None,
            country=(prospect.country or "GB").upper(),
            city=prospect.city,
            address=prospect.address,
            industry=prospect.industry,
            phone=prospect.phone,
            website=prospect.website,
            email_policy=email_policy_for_country(prospect.country),
        )
        db.add(company)
        db.flush()
        created = True
    else:
        # Fill gaps only. Never overwrite a fact we already hold with a weaker one.
        if not company.website and prospect.website:
            company.website = prospect.website
        if not company.primary_domain and domain:
            company.primary_domain = domain
        if not company.phone and prospect.phone:
            company.phone = prospect.phone
        if not company.address and prospect.address:
            company.address = prospect.address
        if not company.city and prospect.city:
            company.city = prospect.city
        if not company.industry and prospect.industry:
            company.industry = prospect.industry

    if domain:
        add_identifier(db, company, "domain", domain)
    phone = normalize_phone(prospect.phone, company.country)
    if phone:
        add_identifier(db, company, "phone_e164", phone)
    for kind, value in prospect.identifiers:
        add_identifier(db, company, kind, value)

    db.flush()
    return company, created


def upsert_contact(
    db: Session, company: Company, email: str, *, name: str | None = None,
    title: str | None = None, source_id: int | None = None, status: str = "unknown",
) -> Contact | None:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return None
    existing = db.execute(
        select(Contact).where(Contact.company_id == company.id, Contact.email == email)
    ).scalar_one_or_none()
    if existing:
        if name and not existing.full_name:
            existing.full_name = name
        if title and not existing.title:
            existing.title = title
        return existing
    contact = Contact(
        company_id=company.id, email=email, full_name=name, title=title,
        source_id=source_id, email_status=status,
        is_decision_maker=bool(name),
    )
    db.add(contact)
    db.flush()
    return contact
