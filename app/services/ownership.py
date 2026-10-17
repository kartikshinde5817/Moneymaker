"""Does this website / email actually belong to this company?

Discovery sources hand back URLs and addresses that belong to somebody else.
A tender notice links to the procurement portal it was published through, and
lists that portal's helpdesk address. A directory entry links to an aggregator.

Without a check, the system audits the wrong site and then writes "I noticed
your website has no mobile viewport" about a page the prospect has never seen.
That is precisely the fabrication the evidence model exists to prevent, so it
is blocked here rather than apologised for later.

The test is deliberately conservative: when ownership cannot be established,
the asset is discarded rather than used. A missed signal costs one weak lead;
a wrong one costs credibility with a real business.
"""

from __future__ import annotations

import re

from app.services.research.fetcher import registrable_domain
from app.services.resolution import normalize_name

# Portals, aggregators and marketplaces that are never a prospect's own site.
THIRD_PARTY_HOSTS = {
    "multiquote.com", "proactis.com", "in-tendhost.co.uk", "intend.co.uk",
    "delta-esourcing.com", "bravosolution.co.uk", "jaggaer.com", "sap.com",
    "ariba.com", "sell2wales.gov.wales", "publiccontractsscotland.gov.uk",
    "etendersni.gov.uk", "find-tender.service.gov.uk",
    "contractsfinder.service.gov.uk", "gov.uk", "sam.gov", "fbo.gov",
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "tripadvisor.com", "tripadvisor.co.uk", "yelp.com",
    "just-eat.co.uk", "deliveroo.co.uk", "ubereats.com", "opentable.com",
    "booking.com", "expedia.com", "airbnb.com", "google.com", "wixsite.com",
    "business.site", "sites.google.com", "linktr.ee", "companieshouse.gov.uk",
    "find-and-update.company-information.service.gov.uk", "indeed.com",
    "adzuna.co.uk", "adzuna.com", "glassdoor.com", "reed.co.uk", "totaljobs.com",
    "news.ycombinator.com", "openstreetmap.org",
}

STOPWORDS = {
    "the", "and", "group", "holdings", "services", "solutions", "systems",
    "international", "global", "uk", "usa", "company", "enterprises", "trading",
}

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(normalize_name(text)) if t and t not in STOPWORDS}


def is_third_party_host(url_or_domain: str) -> bool:
    domain = registrable_domain(url_or_domain) or (url_or_domain or "").lower()
    if not domain:
        return True
    return any(domain == host or domain.endswith("." + host) for host in THIRD_PARTY_HOSTS)


def domain_belongs_to(company_name: str, url: str) -> bool:
    """True only when the domain is recognisably this company's own."""
    domain = registrable_domain(url)
    if not domain or is_third_party_host(domain):
        return False

    label = domain.split(".")[0]
    flat_label = re.sub(r"[^a-z0-9]", "", label)
    name_tokens = _tokens(company_name)
    if not name_tokens or not flat_label:
        return False

    flat_name = "".join(_TOKEN.findall(normalize_name(company_name)))

    # Whole normalised name appears in the domain, e.g. londonenergy -> londonenergy.co.uk
    if flat_name and flat_name in flat_label:
        return True
    if flat_label in flat_name and len(flat_label) >= 6:
        return True
    # A distinctive name token appears in the domain label. The leading token
    # carries the most weight, so it is allowed to be one character shorter.
    ordered = [t for t in _TOKEN.findall(normalize_name(company_name)) if t not in STOPWORDS]
    for index, token in enumerate(ordered):
        minimum = 4 if index == 0 else 5
        if len(token) >= minimum and token in flat_label:
            return True
    # Acronym match, e.g. "British Steel Corporation" -> bsc.co.uk
    if len(name_tokens) >= 2:
        initials = "".join(t[0] for t in _TOKEN.findall(normalize_name(company_name)))
        if len(initials) >= 3 and flat_label == initials:
            return True
    return False


def email_belongs_to(company_name: str, email: str, company_domain: str | None = None) -> bool:
    """True when the address is on the company's own mail domain."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    domain = email.split("@", 1)[1]
    registrable = registrable_domain(domain)
    if not registrable or is_third_party_host(registrable):
        return False
    if company_domain and registrable == registrable_domain(company_domain):
        return True
    # Free mailbox providers belong to a person, not a portal - acceptable for
    # a small business, since many trade from a Gmail address.
    if registrable in {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
        "yahoo.com", "yahoo.co.uk", "icloud.com", "aol.com", "btinternet.com",
    }:
        return True
    return domain_belongs_to(company_name, f"https://{registrable}")


def pick_own_website(company_name: str, *candidates: str | None) -> str:
    """First candidate that is demonstrably the company's own site."""
    for candidate in candidates:
        if candidate and domain_belongs_to(company_name, candidate):
            return candidate
    return ""


def pick_own_email(company_name: str, company_domain: str | None, *candidates: str | None) -> str:
    for candidate in candidates:
        if candidate and email_belongs_to(company_name, candidate, company_domain):
            return candidate
    return ""


def emails_in_text(text: str) -> list[str]:
    """Addresses mentioned in a tender body are often better than the portal's."""
    from app.services.research.audit import EMAIL_RE

    out = []
    for raw in EMAIL_RE.findall(text or ""):
        e = raw.strip().lower().rstrip(".")
        if e not in out:
            out.append(e)
    return out
