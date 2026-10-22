"""The remaining discovery sources.

  google_places    paid, best business coverage. Subject to Google's 30-day
                   caching rule, so Places-derived fields carry an expiry.
  companies_house  free with a key. Newly incorporated UK companies have no
                   website yet, which is the cleanest web-development lead
                   there is - and the incorporation date proves recency.
  adzuna           free tier. A company advertising for a computer-vision
                   engineer has both the problem and approved budget.
  hacker_news      free, no key. Public posts stating a requirement.
  openstreetmap    free, no key. Coverage is thin but it costs nothing and
                   keeps the app usable before any key is configured.
"""

from __future__ import annotations

import datetime as dt
import re
import time

import httpx

from app.config import settings
from app.services import catalog
from app.services.discovery.base import (
    EXPLICIT,
    INFERRED,
    Prospect,
    RawSignal,
    SourceResult,
    keywords_for,
    register,
    utcnow,
)

UA = {"User-Agent": settings.scraper_user_agent}


# ---------------------------------------------------------------------------
# Google Places (New)
# ---------------------------------------------------------------------------

PLACES_SEARCH = "https://places.googleapis.com/v1/places:searchText"

# Business types worth approaching per service, as Places text queries.
PLACES_QUERIES = {
    "web_development": [
        "restaurant", "cafe", "hotel", "beauty salon", "gym", "dental clinic",
        "law firm", "estate agent", "car dealership", "florist",
    ],
    "computer_vision": [
        "manufacturing company", "factory", "warehouse", "logistics company",
        "food processing plant", "construction company", "recycling plant",
    ],
    "ai_agent": [
        "software company", "insurance broker", "travel agency",
        "recruitment agency", "call centre", "property management company",
    ],
    "ai_automation": [
        "accounting firm", "logistics company", "wholesale distributor",
        "marketing agency", "healthcare clinic",
    ],
    "digital_marketing": [
        "restaurant", "dental clinic", "beauty salon", "gym", "estate agent",
        "law firm", "hotel", "car dealership",
    ],
}

COUNTRY_REGION = {"GB": "gb", "US": "us", "AE": "ae", "IE": "ie"}


class GooglePlacesSource:
    key = "google_places"
    label = "Google Places (business listings)"
    needs_key = True
    countries = ["GB", "US", "AE", "IE"]
    services = list(PLACES_QUERIES)

    def available(self) -> bool:
        return bool(settings.google_places_key)

    def discover(
        self, *, service_category: str, keywords: list[str], countries: list[str],
        locations: list[str], limit: int,
    ) -> SourceResult:
        out = SourceResult()
        key = settings.google_places_key
        if not key:
            return out
        queries = keywords or PLACES_QUERIES.get(service_category, [service_category])
        places = locations or ["London"]
        seen: set[str] = set()

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": ",".join(
                [
                    "places.id", "places.displayName", "places.formattedAddress",
                    "places.websiteUri", "places.nationalPhoneNumber",
                    "places.internationalPhoneNumber", "places.primaryTypeDisplayName",
                    "places.types", "places.rating", "places.userRatingCount",
                    "places.businessStatus", "places.location",
                ]
            ),
        }
        try:
            with httpx.Client(timeout=30) as c:
                for loc in places:
                    for q in queries:
                        if len(out.prospects) >= limit:
                            break
                        text = f"{q} in {loc}"
                        body = {"textQuery": text, "maxResultCount": 20}
                        region = COUNTRY_REGION.get((countries or ["GB"])[0].upper())
                        if region:
                            body["regionCode"] = region.upper()
                        r = c.post(PLACES_SEARCH, headers=headers, json=body)
                        if r.status_code != 200:
                            out.errors.append(f"places HTTP {r.status_code}: {r.text[:160]}")
                            continue
                        items = r.json().get("places", []) or []
                        out.queries.append((text, len(items)))
                        out.cost_usd += 0.032  # Text Search (Pro) list price
                        for pl in items:
                            pid = pl.get("id")
                            if not pid or pid in seen:
                                continue
                            seen.add(pid)
                            if pl.get("businessStatus") not in (None, "OPERATIONAL"):
                                continue
                            p = self._to_prospect(pl, q, loc, countries)
                            out.prospects.append(p)
                            if len(out.prospects) >= limit:
                                break
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"places: {type(exc).__name__}: {exc}")
        return out

    def _to_prospect(self, pl: dict, query: str, loc: str, countries: list[str]) -> Prospect:
        name = (pl.get("displayName") or {}).get("text", "")
        website = pl.get("websiteUri") or None
        addr = pl.get("formattedAddress") or ""
        country = (countries or ["GB"])[0].upper()
        p = Prospect(
            name=name, source_kind="google_places",
            source_url=f"https://www.google.com/maps/place/?q=place_id:{pl.get('id')}",
            country=country, city=loc, address=addr, website=website,
            phone=pl.get("internationalPhoneNumber") or pl.get("nationalPhoneNumber"),
            category=(pl.get("primaryTypeDisplayName") or {}).get("text") or query,
            industry=query,
            extra={
                "rating": pl.get("rating"),
                "review_count": pl.get("userRatingCount"),
                "types": pl.get("types", []),
                "places_expires_at": (utcnow() + dt.timedelta(days=30)).isoformat(),
            },
        )
        if pl.get("id"):
            p.identifiers.append(("google_place_id", pl["id"]))
        if not website:
            # The strongest deterministic web-development signal available.
            p.signals.append(
                RawSignal(
                    signal_type="no_website", evidence_class=INFERRED,
                    quote=f"No website listed on the Google Business Profile for {name}.",
                    service_category="web_development", observed_at=utcnow(),
                    source_url=p.source_url,
                )
            )
        return p


# ---------------------------------------------------------------------------
# Companies House (UK)
# ---------------------------------------------------------------------------

CH_SEARCH = "https://api.company-information.service.gov.uk/advanced-search/companies"

# SIC codes by sector. A brand-new company in these sectors needs a website.
CH_SIC = {
    "web_development": [
        "56101", "56102", "56103", "55100", "55201", "96020", "93130",
        "86230", "47710", "47190", "68310", "79110", "93110",
    ],
    "computer_vision": [
        "10110", "10130", "10710", "10890", "13100", "20120", "22220",
        "23610", "24100", "25620", "28150", "29100", "31010", "41201",
        "42110", "52103", "52241", "38320",
    ],
    "ai_agent": ["62012", "62020", "65120", "66220", "68320", "79120", "82200"],
    "ai_automation": ["69201", "70229", "73110", "52290", "46900"],
    # New consumer-facing businesses: restaurants, hotels, retail, beauty,
    # dental, gyms, estate agents - the ones that need customers from day one.
    "digital_marketing": ["56101", "55100", "47190", "96020", "86230", "93130", "68310", "47710"],
}


class CompaniesHouseSource:
    key = "companies_house"
    label = "UK Companies House (new incorporations)"
    needs_key = True
    countries = ["GB"]
    services = list(CH_SIC)

    def available(self) -> bool:
        return bool(settings.companies_house_key)

    def discover(
        self, *, service_category: str, keywords: list[str], countries: list[str],
        locations: list[str], limit: int,
    ) -> SourceResult:
        out = SourceResult()
        key = settings.companies_house_key
        if not key or "GB" not in [c.upper() for c in countries]:
            return out
        sic = CH_SIC.get(service_category, [])
        if not sic:
            return out
        since = (dt.date.today() - dt.timedelta(days=90)).isoformat()

        try:
            with httpx.Client(timeout=40, auth=(key, ""), headers=UA) as c:
                params = {
                    "incorporated_from": since,
                    "company_status": "active",
                    "sic_codes": ",".join(sic[:20]),
                    "size": min(limit * 2, 100),
                }
                if locations:
                    params["location"] = locations[0]
                r = c.get(CH_SEARCH, params=params)
                if r.status_code != 200:
                    out.errors.append(f"companies_house HTTP {r.status_code}: {r.text[:160]}")
                    return out
                items = r.json().get("items", []) or []
                out.queries.append((f"SIC {len(sic)} codes incorporated since {since}", len(items)))
                for it in items:
                    p = self._to_prospect(it)
                    if p:
                        out.prospects.append(p)
                    if len(out.prospects) >= limit:
                        break
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"companies_house: {type(exc).__name__}: {exc}")
        return out

    def _to_prospect(self, it: dict) -> Prospect | None:
        name = it.get("company_name")
        number = it.get("company_number")
        if not name or not number:
            return None
        addr = it.get("registered_office_address") or {}
        created = it.get("date_of_creation")
        url = f"https://find-and-update.company-information.service.gov.uk/company/{number}"
        observed = utcnow()
        if created:
            try:
                observed = dt.datetime.fromisoformat(created).replace(tzinfo=dt.timezone.utc)
            except ValueError:
                pass
        p = Prospect(
            name=name, source_kind="companies_house", source_url=url, country="GB",
            city=addr.get("locality"),
            address=", ".join(
                x for x in (
                    addr.get("address_line_1"), addr.get("locality"), addr.get("postal_code")
                ) if x
            ) or None,
            industry=", ".join(it.get("sic_codes") or []),
            category="newly incorporated",
            excerpt=f"{name} incorporated {created}",
        )
        p.identifiers.append(("registry_no", f"GB-COH-{number}"))
        p.signals.append(
            RawSignal(
                signal_type="recently_incorporated", evidence_class=INFERRED,
                quote=f"{name} was incorporated at Companies House on {created}.",
                service_category="web_development", observed_at=observed, source_url=url,
            )
        )
        return p


# ---------------------------------------------------------------------------
# Adzuna job ads
# ---------------------------------------------------------------------------

ADZUNA = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
ADZUNA_COUNTRIES = {"GB": "gb", "US": "us", "AE": "ae", "IE": "ie"}

# A firm hiring for these skills has the problem AND signed-off budget, but
# is also hiring in-house - so it is a consultancy lead, not a product lead.
AGENCY_WORDS = re.compile(
    r"\b(recruit|staffing|agency|consultanc|outsourc|headhunt|talent solutions)\b", re.I
)


class AdzunaSource:
    key = "adzuna"
    label = "Adzuna job ads (hiring signals)"
    needs_key = True
    countries = ["GB", "US", "AE", "IE"]
    services = ["computer_vision", "ai_agent", "ai_automation", "web_development", "digital_marketing"]

    def available(self) -> bool:
        return bool(settings.adzuna_app_id and settings.adzuna_app_key)

    def discover(
        self, *, service_category: str, keywords: list[str], countries: list[str],
        locations: list[str], limit: int,
    ) -> SourceResult:
        out = SourceResult()
        if not self.available():
            return out
        kws = keywords_for(service_category, keywords)
        seen: set[str] = set()
        try:
            with httpx.Client(timeout=30, headers=UA) as c:
                for country in countries:
                    cc = ADZUNA_COUNTRIES.get(country.upper())
                    if not cc:
                        continue
                    for kw in kws[:8]:
                        if len(out.prospects) >= limit:
                            break
                        r = c.get(
                            ADZUNA.format(country=cc),
                            params={
                                "app_id": settings.adzuna_app_id,
                                "app_key": settings.adzuna_app_key,
                                "what_phrase": kw, "results_per_page": 50,
                                "max_days_old": 45, "content-type": "application/json",
                            },
                        )
                        if r.status_code != 200:
                            out.errors.append(f"adzuna HTTP {r.status_code} for '{kw}'")
                            continue
                        items = r.json().get("results", []) or []
                        out.queries.append((f"{kw} ({cc})", len(items)))
                        for job in items:
                            company = ((job.get("company") or {}).get("display_name") or "").strip()
                            if not company or company.lower() in seen:
                                continue
                            if AGENCY_WORDS.search(company):
                                continue  # recruiters advertise on others' behalf
                            seen.add(company.lower())
                            p = self._to_prospect(job, company, kw, country.upper())
                            out.prospects.append(p)
                            if len(out.prospects) >= limit:
                                break
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"adzuna: {type(exc).__name__}: {exc}")
        return out

    def _to_prospect(self, job: dict, company: str, kw: str, country: str) -> Prospect:
        loc = (job.get("location") or {}).get("display_name")
        url = job.get("redirect_url") or ""
        title = job.get("title") or ""
        desc = (job.get("description") or "")[:600]
        observed = utcnow()
        if job.get("created"):
            try:
                observed = dt.datetime.fromisoformat(job["created"].replace("Z", "+00:00"))
            except ValueError:
                pass
        p = Prospect(
            name=company, source_kind="job_board", source_url=url, country=country,
            city=loc, address=loc, industry=(job.get("category") or {}).get("label"),
            category="hiring", excerpt=f"{title}\n{desc}",
        )
        p.signals.append(
            RawSignal(
                signal_type="hiring_for_skill", evidence_class=EXPLICIT,
                quote=f'{company} is advertising for "{title}" (matched "{kw}").',
                observed_at=observed, source_url=url,
            )
        )
        return p


# ---------------------------------------------------------------------------
# Hacker News (public posts)
# ---------------------------------------------------------------------------

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
COMPANY_RE = re.compile(r"^([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,3})\s*[|(-]")


class HackerNewsSource:
    key = "hacker_news"
    label = "Hacker News (public requirement posts)"
    needs_key = False
    countries = ["GB", "US", "AE", "IE"]
    services = ["computer_vision", "ai_agent", "ai_automation", "web_development", "digital_marketing"]

    def available(self) -> bool:
        return True

    def discover(
        self, *, service_category: str, keywords: list[str], countries: list[str],
        locations: list[str], limit: int,
    ) -> SourceResult:
        out = SourceResult()
        kws = keywords_for(service_category, keywords)
        cutoff = int(time.time()) - 90 * 86400
        seen: set[str] = set()
        try:
            with httpx.Client(timeout=30, headers=UA) as c:
                for kw in kws[:6]:
                    if len(out.prospects) >= limit:
                        break
                    r = c.get(
                        HN_SEARCH,
                        params={
                            "query": kw, "tags": "comment",
                            "numericFilters": f"created_at_i>{cutoff}", "hitsPerPage": 40,
                        },
                    )
                    if r.status_code != 200:
                        out.errors.append(f"hacker_news HTTP {r.status_code}")
                        continue
                    hits = r.json().get("hits", []) or []
                    out.queries.append((kw, len(hits)))
                    for h in hits:
                        text = re.sub(r"<[^>]+>", " ", h.get("comment_text") or "")
                        text = re.sub(r"\s+", " ", text).strip()
                        m = COMPANY_RE.match(text)
                        if not m:
                            continue  # no identifiable company - not actionable
                        company = m.group(1).strip()
                        if company.lower() in seen or len(company) < 3:
                            continue
                        seen.add(company.lower())
                        url = f"https://news.ycombinator.com/item?id={h.get('objectID')}"
                        p = Prospect(
                            name=company, source_kind="hacker_news", source_url=url,
                            country="US", category="tech", excerpt=text[:1200],
                        )
                        p.signals.append(
                            RawSignal(
                                signal_type="public_post", evidence_class=EXPLICIT,
                                quote=f'Hacker News post mentioning "{kw}": {text[:200]}',
                                observed_at=dt.datetime.fromtimestamp(
                                    h.get("created_at_i", time.time()), dt.timezone.utc
                                ),
                                source_url=url, confidence=0.7,
                            )
                        )
                        out.prospects.append(p)
                        if len(out.prospects) >= limit:
                            break
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"hacker_news: {type(exc).__name__}: {exc}")
        return out


# ---------------------------------------------------------------------------
# OpenStreetMap (free fallback, no key)
# ---------------------------------------------------------------------------

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSM_TERMS = {
    "web_development": ["cafe", "restaurant", "hotel", "hairdresser", "gym", "dentist"],
    "computer_vision": ["factory", "warehouse", "industrial", "manufacturing"],
    "ai_agent": ["software company", "insurance", "travel agency"],
    "ai_automation": ["accountant", "logistics", "wholesale"],
    "digital_marketing": ["restaurant", "dentist", "hairdresser", "gym", "estate agent"],
}


class OpenStreetMapSource:
    key = "openstreetmap"
    label = "OpenStreetMap (free, no key)"
    needs_key = False
    countries = ["GB", "US", "AE", "IE"]
    services = list(OSM_TERMS)

    def available(self) -> bool:
        return True

    def discover(
        self, *, service_category: str, keywords: list[str], countries: list[str],
        locations: list[str], limit: int,
    ) -> SourceResult:
        out = SourceResult()
        terms = (keywords or OSM_TERMS.get(service_category)
                 or catalog.industries_for(service_category) or ["business"])
        places = locations or ["London"]
        country = (countries or ["GB"])[0].upper()
        seen: set[str] = set()
        try:
            with httpx.Client(timeout=30, headers=UA) as c:
                for loc in places:
                    for term in terms[:6]:
                        if len(out.prospects) >= limit:
                            break
                        r = c.get(
                            NOMINATIM,
                            params={
                                "q": f"{term} in {loc}", "format": "json",
                                "limit": 40, "extratags": 1, "addressdetails": 1,
                            },
                        )
                        if r.status_code != 200:
                            out.errors.append(f"osm HTTP {r.status_code}")
                            continue
                        items = r.json() or []
                        out.queries.append((f"{term} in {loc}", len(items)))
                        for it in items:
                            name = it.get("name")
                            if not name or name.lower() in seen:
                                continue
                            if it.get("class") in ("place", "highway", "boundary"):
                                continue
                            seen.add(name.lower())
                            tags = it.get("extratags") or {}
                            addr = it.get("address") or {}
                            osm_id = f"{it.get('osm_type')}/{it.get('osm_id')}"
                            p = Prospect(
                                name=name, source_kind="osm",
                                source_url=f"https://www.openstreetmap.org/{osm_id}",
                                country=(addr.get("country_code") or country).upper(),
                                city=addr.get("city") or addr.get("town") or loc,
                                address=(it.get("display_name") or "")[:240],
                                website=tags.get("website") or tags.get("contact:website"),
                                phone=tags.get("phone") or tags.get("contact:phone"),
                                email=(tags.get("email") or tags.get("contact:email") or "").lower()
                                or None,
                                category=term, industry=term,
                            )
                            p.identifiers.append(("osm_id", osm_id))
                            out.prospects.append(p)
                            if len(out.prospects) >= limit:
                                break
                        time.sleep(1.1)  # Nominatim usage policy: 1 req/sec
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"osm: {type(exc).__name__}: {exc}")
        return out


register(CompaniesHouseSource())
register(AdzunaSource())
register(HackerNewsSource())
register(OpenStreetMapSource())
