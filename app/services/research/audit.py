"""Website audit engine.

This is the competitive edge of the system. Everything it produces is a fact
about a page the business publishes to the world, so it is legal to collect,
verifiable by the recipient, and specific enough to open an email with.

"Your site has no mobile viewport tag and the footer says 2016" is a real
observation about a real page. That is a different business from "you are a
restaurant in Leeds so you probably want a website".

Nothing here uses an LLM. These are deterministic checks against markup.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from app.services.research.fetcher import FetchResult, fetch, normalize_url

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,24}")
COPYRIGHT_RE = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–—]\s*)?(\d{4})", re.I)

# Markers that prove a capability exists on the site. Absence of all of them in
# a sector that needs one is the signal we act on.
BOOKING_MARKERS = (
    "opentable", "resdiary", "quandoo", "sevenrooms", "tablein", "eveve",
    "booking.com", "reservewithgoogle", "calendly", "acuityscheduling",
    "simplybook", "setmore", "book now", "book a table", "make a reservation",
    "book online", "reserve a table", "schedule an appointment",
)
ECOMMERCE_MARKERS = (
    "shopify", "woocommerce", "bigcommerce", "magento", "squarespace-commerce",
    "snipcart", "ecwid", "add to cart", "add to basket", "shopping cart",
    "checkout", "/cart", "stripe.com", "paypal.com/checkout",
)
ORDERING_MARKERS = (
    "ubereats", "deliveroo", "just-eat", "justeat", "doordash", "grubhub",
    "slicelife", "toasttab", "order online", "order now", "food delivery",
)

# Technology fingerprints. Only ones that genuinely indicate age or a gap.
TECH_PATTERNS: dict[str, tuple[str, ...]] = {
    "wordpress": ("wp-content", "wp-includes", 'name="generator" content="WordPress'),
    "wix": ("wix.com", "wixstatic", "_wixCssImports"),
    "squarespace": ("squarespace.com", "static1.squarespace"),
    "shopify": ("cdn.shopify.com", "shopify.js", "Shopify.theme"),
    "webflow": ("webflow.js", "assets.website-files.com"),
    "godaddy_builder": ("img1.wsimg.com", "godaddysites.com"),
    "react": ("__NEXT_DATA__", "react-dom", "data-reactroot"),
    "bootstrap": ("bootstrap.min.css", "bootstrap.css"),
    "jquery": ("jquery",),
    "flash": (".swf", "application/x-shockwave-flash"),
    "google_analytics_legacy": ("ga.js", "analytics.js"),
    "google_analytics_4": ("gtag/js", "googletagmanager.com/gtag"),
}

JQUERY_VERSION_RE = re.compile(r"jquery[-.]?(\d+)\.(\d+)(?:\.(\d+))?(?:\.min)?\.js", re.I)

CONTACT_PATHS = ("/contact", "/contact-us", "/contactus", "/about", "/about-us", "/get-in-touch")

# A parked domain is a business that bought a name and never built the site.
# That is among the strongest web-development signals available, so it must be
# detected rather than recorded as "has a website".
PARKING_MARKERS = (
    "sedoparking", "parkingcrew", "bodis.com", "above.com", "afternic",
    "hugedomains", "dan.com/buy-domain", "buy this domain", "domain is for sale",
    "this domain is parked", "domain parking", "godaddy.com/domainsearch",
    "namecheap.com/domains", "porkbun.com", "/lander", "undeveloped.com",
    "domain for sale", "inquire about this domain",
)
# Below this much visible text, a page is a placeholder rather than a website.
THIN_CONTENT_CHARS = 350

# Sectors where a missing capability is a concrete, arguable gap.
HOSPITALITY = {"restaurant", "cafe", "hotel", "resort", "bar", "pub", "bakery", "food"}
RETAIL = {"retail", "shop", "store", "boutique", "clothing", "furniture", "jewellery"}
APPOINTMENT = {"salon", "spa", "gym", "fitness", "clinic", "dental", "dentist", "barber", "physio"}


@dataclass
class Audit:
    url: str = ""
    has_site: bool = False
    http_status: int | None = None
    has_ssl: bool = False
    is_mobile_friendly: bool = False
    has_booking: bool = False
    has_ecommerce: bool = False
    has_ordering: bool = False
    has_contact_form: bool = False
    is_parked: bool = False
    is_thin: bool = False
    visible_chars: int = 0
    copyright_year: int | None = None
    page_bytes: int = 0
    ttfb_ms: int = 0
    title: str = ""
    meta_description: str = ""
    tech: dict = field(default_factory=dict)
    emails_found: list[str] = field(default_factory=list)
    phones_found: list[str] = field(default_factory=list)
    social: dict = field(default_factory=dict)
    notes: str = ""
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "has_site": self.has_site,
            "http_status": self.http_status,
            "has_ssl": self.has_ssl,
            "is_mobile_friendly": self.is_mobile_friendly,
            "has_booking": self.has_booking,
            "has_ecommerce": self.has_ecommerce,
            "has_contact_form": self.has_contact_form,
            "is_parked": self.is_parked,
            "is_thin": self.is_thin,
            "visible_chars": self.visible_chars,
            "copyright_year": self.copyright_year,
            "page_bytes": self.page_bytes,
            "ttfb_ms": self.ttfb_ms,
            "tech": self.tech,
            "emails_found": self.emails_found,
            "notes": self.notes,
        }


def _detect_tech(html_lower: str) -> dict:
    tech: dict = {}
    for name, markers in TECH_PATTERNS.items():
        if any(m.lower() in html_lower for m in markers):
            tech[name] = True
    m = JQUERY_VERSION_RE.search(html_lower)
    if m:
        major, minor = int(m.group(1)), int(m.group(2))
        tech["jquery_version"] = f"{major}.{minor}"
        # jQuery 1.x/2.x are long end-of-life and carry known CVEs.
        tech["jquery_outdated"] = major < 3
    return tech


def _extract_emails(text: str, domain_hint: str = "") -> list[str]:
    found = []
    for raw in EMAIL_RE.findall(text or ""):
        e = raw.strip().lower().rstrip(".")
        if e.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            continue
        if any(junk in e for junk in ("example.com", "sentry.io", "@2x", "wixpress")):
            continue
        if e not in found:
            found.append(e)

    def rank(e: str) -> tuple[int, int]:
        local, _, dom = e.partition("@")
        on_domain = 0 if (domain_hint and domain_hint in dom) else 1
        preference = {
            "info": 0, "contact": 0, "hello": 1, "enquiries": 1, "enquiry": 1,
            "sales": 2, "office": 2, "admin": 3, "bookings": 3,
        }.get(local, 5)
        return (on_domain, preference)

    return sorted(found, key=rank)[:10]


def _social_links(soup: BeautifulSoup) -> dict:
    out: dict = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        for key, needle in (
            ("linkedin", "linkedin.com/"),
            ("facebook", "facebook.com/"),
            ("instagram", "instagram.com/"),
            ("twitter", "twitter.com/"),
            ("youtube", "youtube.com/"),
        ):
            if needle in href and key not in out:
                out[key] = a["href"][:300]
    return out


def _analyse_html(audit: Audit, result: FetchResult, domain_hint: str) -> None:
    html = result.text or ""
    lower = html.lower()
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    visible = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    audit.visible_chars = len(visible)
    audit.is_parked = any(m in lower for m in PARKING_MARKERS)
    audit.is_thin = audit.visible_chars < THIN_CONTENT_CHARS

    if soup.title and soup.title.string:
        audit.title = soup.title.string.strip()[:300]
    desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if desc and desc.get("content"):
        audit.meta_description = desc["content"].strip()[:500]

    # A missing viewport tag is the single clearest "this predates mobile" tell.
    viewport = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    audit.is_mobile_friendly = bool(viewport and "width" in (viewport.get("content") or ""))

    audit.tech = _detect_tech(lower)
    audit.has_booking = any(m in lower for m in BOOKING_MARKERS)
    audit.has_ecommerce = any(m in lower for m in ECOMMERCE_MARKERS)
    audit.has_ordering = any(m in lower for m in ORDERING_MARKERS)
    audit.has_contact_form = bool(
        soup.find("form")
        and soup.find("input", attrs={"type": re.compile("email|text", re.I)})
    ) or "mailto:" in lower

    years = [int(y) for y in COPYRIGHT_RE.findall(html) if 1995 <= int(y) <= dt.date.today().year + 1]
    if years:
        audit.copyright_year = max(years)

    audit.emails_found = _extract_emails(html, domain_hint)
    audit.social = _social_links(soup)

    # Layout tables used for structure (not data) is a pre-2010 construction tell.
    tables = soup.find_all("table")
    if tables and not soup.find("th"):
        for t in tables:
            if len(t.find_all("tr")) > 2:
                audit.tech["table_layout"] = True
                break


def audit_website(url: str, *, category_hint: str = "", deep: bool = True) -> Audit:
    """Fetch and analyse a business website. Never raises."""
    audit = Audit()
    normalized = normalize_url(url)
    if not normalized:
        audit.error = "no usable website address"
        return audit

    audit.url = normalized
    result = fetch(normalized, respect_robots=True)

    # If https failed outright, try http - plenty of small-business sites are
    # still plain http, and that itself is a finding.
    if not result.ok and normalized.startswith("https://"):
        alt = fetch("http://" + normalized[len("https://") :], respect_robots=True)
        if alt.ok:
            result = alt

    audit.http_status = result.status
    audit.page_bytes = result.bytes_len
    audit.ttfb_ms = result.ttfb_ms
    audit.url = result.final_url or normalized
    audit.has_ssl = audit.url.startswith("https://")

    if not result.ok:
        audit.error = result.error or f"HTTP {result.status}"
        # A blocking server still proves a site exists.
        audit.has_site = result.status in (401, 403, 429)
        return audit

    audit.has_site = True
    domain_hint = ""
    from app.services.research.fetcher import registrable_domain

    domain_hint = registrable_domain(audit.url)
    _analyse_html(audit, result, domain_hint)

    # One extra hop to a contact page, only if we still have no email.
    if deep and not audit.emails_found:
        base = audit.url.rstrip("/")
        for path in CONTACT_PATHS:
            sub = fetch(base + path, respect_robots=True, timeout=10)
            if sub.ok and sub.text:
                emails = _extract_emails(sub.text, domain_hint)
                if emails:
                    audit.emails_found = emails
                    if not audit.has_contact_form and "<form" in sub.text.lower():
                        audit.has_contact_form = True
                    break

    audit.notes = _summarise(audit, category_hint)
    return audit


def _summarise(audit: Audit, category_hint: str) -> str:
    bits = []
    if audit.is_parked:
        return "domain is parked - no real website"
    if audit.is_thin:
        bits.append(f"placeholder page only ({audit.visible_chars} chars of text)")
    if not audit.is_mobile_friendly:
        bits.append("no mobile viewport")
    if not audit.has_ssl:
        bits.append("no HTTPS")
    if audit.copyright_year and audit.copyright_year < dt.date.today().year - 2:
        bits.append(f"footer dated {audit.copyright_year}")
    if audit.tech.get("jquery_outdated"):
        bits.append(f"jQuery {audit.tech.get('jquery_version')}")
    if audit.tech.get("flash"):
        bits.append("Flash content")
    if audit.tech.get("table_layout"):
        bits.append("table-based layout")
    cat = (category_hint or "").lower()
    if any(k in cat for k in HOSPITALITY) and not (audit.has_booking or audit.has_ordering):
        bits.append("no online booking or ordering")
    if any(k in cat for k in RETAIL) and not audit.has_ecommerce:
        bits.append("no online store")
    if any(k in cat for k in APPOINTMENT) and not audit.has_booking:
        bits.append("no appointment booking")
    if not audit.has_contact_form:
        bits.append("no contact form")
    return "; ".join(bits)
