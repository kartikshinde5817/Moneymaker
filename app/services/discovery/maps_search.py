"""Two more places to look, both through search APIs (nothing is scraped).

  serper_places  Businesses on Google Maps, through the same Serper key the web
                 search uses. Each result brings a name, address, phone and the
                 business's own website, so the audit and the email finder can
                 start straight away. Best for the Web Development and Digital
                 Marketing bots: a business with no site, or only a Facebook
                 page, is itself the evidence.
  brave_search   The same "who is asking for this" searches as the Serper
                 source, run on Brave's index. A second, separate free quota,
                 and a different set of results.
"""

from __future__ import annotations

import datetime as dt

import httpx

from app.config import settings
from app.services import catalog, cities
from app.services.discovery.base import (
    AI_AGENT, AI_AUTO, CV, DM, EXPLICIT, INFERRED, WEB,
    Prospect, RawSignal, SourceResult, register, utcnow,
)
from app.services.discovery.platforms import INTENT_PHRASES, SerperSource

PLACES_URL = "https://google.serper.dev/places"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"

ALL_COUNTRIES = ["GB", "US", "IE", "NL", "AE", "AU", "NZ", "SG", "IN", "ZA", "FR", "SE"]

# What to look for on the map. Local, owner-run businesses are the best fit for
# a website or marketing pitch; sites that run on plant and process suit vision.
PLACE_TYPES = {
    WEB: ["restaurant", "dentist", "hair salon", "plumber", "electrician", "gym", "estate agent",
          "accountant", "boutique", "builder", "wedding venue", "car repair garage", "florist",
          "physiotherapist", "bakery", "photographer", "yoga studio", "optician"],
    DM: ["restaurant", "dentist", "gym", "hair salon", "estate agent", "solicitor", "physiotherapist",
         "wedding venue", "interior designer", "veterinary clinic", "cafe", "boutique", "driving school"],
    CV: ["manufacturing company", "factory", "warehouse", "food processing plant", "logistics company",
         "packaging company", "construction company", "metal fabrication", "recycling plant"],
    AI_AGENT: ["real estate agency", "travel agency", "clinic", "insurance agency", "law firm",
               "recruitment agency", "hotel", "car dealership", "online store"],
    AI_AUTO: ["accounting firm", "logistics company", "recruitment agency", "insurance broker",
              "property management company", "wholesale distributor", "marketing agency"],
}

COUNTRY_OF_CITY = {city: code for code, names in cities.CITY_POOL.items() for city in names}


def _place_types(service: str) -> list[str]:
    return PLACE_TYPES.get(service) or catalog.industries_for(service) or ["company"]


def _rotate(items: list[str], take: int) -> list[str]:
    """A different slice each day, so repeat runs do not search the same things."""
    if not items:
        return []
    start = (dt.date.today().toordinal() * take) % len(items)
    return [items[(start + i) % len(items)] for i in range(min(take, len(items)))]


class SerperPlacesSource:
    key = "serper_places"
    label = "Google Maps businesses (via Serper)"
    needs_key = True
    countries = ALL_COUNTRIES
    services = list(PLACE_TYPES)

    def available(self) -> bool:
        return bool(settings.serper_api_key)

    def discover(
        self, *, service_category: str, keywords: list[str], countries: list[str],
        locations: list[str], limit: int,
    ) -> SourceResult:
        out = SourceResult()
        if not self.available():
            return out
        countries = [c.upper() for c in (countries or ["GB"])]
        pool = cities.pool_for(countries)
        places = locations or ([pool[0][1]] if pool else [])
        if not places:
            out.errors.append("serper_places: choose at least one city or turn on city rotation")
            return out
        types = _rotate(_place_types(service_category), 3)
        seen: set[str] = set()
        try:
            with httpx.Client(timeout=30) as c:
                for city in places[:4]:
                    country = COUNTRY_OF_CITY.get(city) or countries[0]
                    if country not in countries:
                        country = countries[0]
                    for kind in types:
                        if len(out.prospects) >= limit:
                            break
                        query = f"{kind} in {city}"
                        r = c.post(PLACES_URL, headers={"X-API-KEY": settings.serper_api_key,
                                                        "Content-Type": "application/json"},
                                   json={"q": query, "gl": country.lower(), "hl": "en", "num": 20})
                        if r.status_code != 200:
                            out.errors.append(f"serper places HTTP {r.status_code} for {query}")
                            continue
                        found = r.json().get("places") or []
                        out.queries.append((query, len(found)))
                        out.cost_usd += 0.001
                        for item in found:
                            p = self._to_prospect(item, service_category, country, city, kind, seen)
                            if p:
                                out.prospects.append(p)
                                if len(out.prospects) >= limit:
                                    break
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"serper_places: {type(exc).__name__}: {exc}")
        return out

    def _to_prospect(self, item: dict, service: str, country: str, city: str, kind: str,
                     seen: set) -> Prospect | None:
        from app.services.ownership import is_third_party_host

        name = (item.get("title") or "").strip()
        cid = str(item.get("cid") or "")
        if not name or (cid or name.lower()) in seen:
            return None
        seen.add(cid or name.lower())
        site = (item.get("website") or "").strip()
        only_social = bool(site) and is_third_party_host(site)
        if only_social:
            site = ""
        link = f"https://www.google.com/maps?cid={cid}" if cid else "https://www.google.com/maps"
        address = (item.get("address") or "")[:240]
        p = Prospect(
            name=name[:160], source_kind="maps", source_url=link, country=country, city=city,
            address=address, website=site or None, phone=item.get("phoneNumber") or None,
            category=item.get("category") or kind, industry=item.get("category") or kind,
            excerpt=f"{name} — {item.get('category') or kind}, {address}",
            # The site comes from the business's own Maps listing, so it is theirs
            # even when the domain does not spell out the name.
            extra={"website_verified": bool(site), "rating": item.get("rating"),
                   "reviews": item.get("ratingCount")},
        )
        if cid:
            p.identifiers.append(("google_cid", cid))
        if not site and service in (WEB, DM):
            how = "only a social-media page" if only_social else "no website"
            p.signals.append(RawSignal(
                signal_type="no_website", evidence_class=INFERRED, observed_at=utcnow(),
                quote=f"{name} ({address or city}) is listed on Google Maps with {how}.",
                source_url=link, confidence=0.8,
            ))
        return p


class BraveSearchSource(SerperSource):
    """The Serper source's searches, run on Brave's search API instead."""

    key = "brave_search"
    label = "Brave search — explicit requirements"
    needs_key = True
    countries = ALL_COUNTRIES
    services = list(INTENT_PHRASES)

    def available(self) -> bool:
        return bool(settings.brave_api_key)

    def discover(
        self, *, service_category: str, keywords: list[str], countries: list[str],
        locations: list[str], limit: int,
    ) -> SourceResult:
        out = SourceResult()
        if not self.available():
            return out
        phrases = keywords or INTENT_PHRASES.get(service_category) or catalog.intent_phrases_for(service_category)
        if not phrases:
            return out
        seen: set[str] = set()
        try:
            with httpx.Client(timeout=30) as c:
                for country in (countries or ["GB"]):
                    for phrase in phrases[:5]:
                        if len(out.prospects) >= limit:
                            break
                        query = f"{phrase} {locations[0]}" if locations else phrase
                        r = c.get(BRAVE_URL, headers={"X-Subscription-Token": settings.brave_api_key,
                                                      "Accept": "application/json"},
                                  params={"q": query, "count": 20, "country": country.lower(),
                                          "freshness": "py"})  # past year
                        if r.status_code != 200:
                            out.errors.append(f"brave HTTP {r.status_code} for {query[:40]}")
                            continue
                        hits = ((r.json().get("web") or {}).get("results")) or []
                        out.queries.append((query, len(hits)))
                        for hit in hits:
                            # Brave names its fields differently; map them onto what
                            # the Serper source already knows how to turn into a prospect.
                            mapped = {"link": hit.get("url"), "title": hit.get("title"),
                                      "snippet": hit.get("description")}
                            p = self._to_prospect(mapped, query, country.upper(), seen)
                            if p:
                                p.source_kind = "serp"
                                out.prospects.append(p)
                                if len(out.prospects) >= limit:
                                    break
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"brave: {type(exc).__name__}: {exc}")
        return out


register(SerperPlacesSource())
register(BraveSearchSource())
