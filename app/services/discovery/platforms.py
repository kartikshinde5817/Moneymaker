"""Additional discovery platforms.

  serper        Web search for phrase queries. The single highest-value source
                in the system, because it finds people *saying* they need what
                we sell. Needs a key (serper.dev has a free allowance).
  github        Organisations publishing computer-vision or AI code. Free, but
                LOW QUALITY for selling services: tested live, it returns the
                OpenCV project itself, Microsoft, and research repos. A company
                with a CV repo already does CV in-house, so these read as
                competitors, not buyers. Off by default; enable only if you are
                looking for partners or hiring targets rather than clients.
  yelp          US small-business coverage for the web-development pitch.
                Free Fusion API key.

On LinkedIn, plainly: there is no API that exposes post or profile search, and
scraping it breaches their user agreement. What this app does instead is read
the LinkedIn URL a company publishes on *its own website* and put it in a
manual outreach lane. That is the company's own published link, and a human
does the outreach. See services/linkedin.py.
"""

from __future__ import annotations

import datetime as dt
import re

import httpx

from app.config import settings
from app.services import catalog
from app.services.discovery.base import (
    EXPLICIT,
    INFERRED,
    Prospect,
    RawSignal,
    SourceResult,
    register,
    utcnow,
)

UA = {"User-Agent": settings.scraper_user_agent}


# ---------------------------------------------------------------------------
# Serper - web search for explicit buying intent
# ---------------------------------------------------------------------------

SERPER_URL = "https://google.serper.dev/search"

# Phrases where somebody is stating a requirement in their own words. Quoted
# so the engine matches the phrase, not the loose keywords.
INTENT_PHRASES = {
    "web_development": [
        '"looking for a web developer"', '"need a new website"',
        '"looking for a web design agency"', '"website redesign" "request for proposal"',
        '"seeking a website developer"', '"need an ecommerce website"',
    ],
    "computer_vision": [
        '"looking for a computer vision" company', '"need PPE detection"',
        '"looking for CCTV analytics"', '"need a machine vision" supplier',
        '"request for proposal" "computer vision"', '"need automated visual inspection"',
    ],
    "ai_agent": [
        '"looking for an AI chatbot" developer', '"need an AI agent" built',
        '"looking for a voice AI" company', '"need a customer support chatbot"',
        '"request for proposal" "conversational AI"',
    ],
    "ai_automation": [
        '"looking for AI automation"', '"need workflow automation" consultant',
        '"request for proposal" "process automation"',
    ],
    "digital_marketing": [
        '"looking for a digital marketing agency"', '"need help with SEO"',
        '"looking for a social media manager"', '"looking for a marketing agency"',
        '"request for proposal" "digital marketing"', '"need more leads" "marketing"',
    ],
}

COUNTRY_GL = {"GB": ("uk", "en"), "US": ("us", "en"), "IE": ("ie", "en"), "AE": ("ae", "en")}

# Aggregators and job boards - the result is about a company, not from one.
SKIP_DOMAINS = re.compile(
    r"(linkedin\.com|indeed\.|glassdoor|reed\.co\.uk|totaljobs|monster\.|"
    r"upwork\.com|fiverr\.com|freelancer\.|peopleperhour|clutch\.co|"
    r"reddit\.com|quora\.com|youtube\.com|facebook\.com|twitter\.com|x\.com|"
    r"wikipedia\.org|medium\.com|github\.io)", re.I
)


class SerperSource:
    key = "serper"
    label = "Web search — explicit requirements"
    needs_key = True
    countries = ["GB", "US", "IE", "AE"]
    services = list(INTENT_PHRASES)

    def available(self) -> bool:
        return bool(settings.serper_api_key)

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
                    gl, hl = COUNTRY_GL.get(country.upper(), ("us", "en"))
                    for phrase in phrases[:6]:
                        if len(out.prospects) >= limit:
                            break
                        query = f"{phrase} {locations[0]}" if locations else phrase
                        r = c.post(
                            SERPER_URL,
                            headers={"X-API-KEY": settings.serper_api_key,
                                     "Content-Type": "application/json"},
                            json={"q": query, "gl": gl, "hl": hl, "num": 20,
                                  "tbs": "qdr:m6"},  # last 6 months only
                        )
                        if r.status_code != 200:
                            out.errors.append(f"serper HTTP {r.status_code} for {query[:40]}")
                            continue
                        hits = (r.json().get("organic") or [])
                        out.queries.append((query, len(hits)))
                        out.cost_usd += 0.001
                        for hit in hits:
                            p = self._to_prospect(hit, query, country.upper(), seen)
                            if p:
                                out.prospects.append(p)
                                if len(out.prospects) >= limit:
                                    break
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"serper: {type(exc).__name__}: {exc}")
        return out

    def _to_prospect(self, hit: dict, query: str, country: str, seen: set) -> Prospect | None:
        link = hit.get("link") or ""
        if not link or SKIP_DOMAINS.search(link):
            return None
        from app.services.research.fetcher import registrable_domain

        domain = registrable_domain(link)
        if not domain or domain in seen:
            return None
        seen.add(domain)

        title = hit.get("title") or ""
        snippet = hit.get("snippet") or ""
        # Trim the SEO suffix most page titles carry.
        name = re.split(r"\s+[|\-–—]\s+", title)[0].strip()[:160]
        if not name or len(name) < 3:
            name = domain

        return Prospect(
            name=name, source_kind="serp", source_url=link, country=country,
            website=f"https://{domain}", category="web search",
            excerpt=f"{title}\n{snippet}",
            signals=[RawSignal(
                signal_type="explicit_request", evidence_class=EXPLICIT,
                quote=f'Search for {query} returned: "{title}" — {snippet[:240]}',
                observed_at=utcnow(), source_url=link, confidence=0.75,
            )],
        )


# ---------------------------------------------------------------------------
# GitHub - organisations building in our space
# ---------------------------------------------------------------------------

GH_SEARCH = "https://api.github.com/search/repositories"

GH_TOPICS = {
    "computer_vision": ["opencv", "object-detection", "yolo", "image-processing"],
    "ai_agent": ["chatbot", "llm", "rag", "ai-agent"],
    "ai_automation": ["workflow-automation", "llm", "automation"],
}


class GitHubSource:
    key = "github"
    label = "GitHub — low quality, finds competitors not clients"
    needs_key = False
    countries = ["GB", "US", "IE", "AE"]
    services = list(GH_TOPICS)

    def available(self) -> bool:
        return True  # unauthenticated search works, just rate-limited

    def discover(
        self, *, service_category: str, keywords: list[str], countries: list[str],
        locations: list[str], limit: int,
    ) -> SourceResult:
        out = SourceResult()
        topics = GH_TOPICS.get(service_category)
        if not topics:
            return out
        since = (dt.date.today() - dt.timedelta(days=180)).isoformat()
        seen: set[str] = set()
        headers = dict(UA, Accept="application/vnd.github+json")

        try:
            with httpx.Client(timeout=30, headers=headers) as c:
                for topic in topics[:3]:
                    if len(out.prospects) >= limit:
                        break
                    r = c.get(GH_SEARCH, params={
                        "q": f"topic:{topic} pushed:>{since} stars:>5",
                        "sort": "updated", "per_page": 40,
                    })
                    if r.status_code == 403:
                        out.errors.append("github rate limit reached (add a token to raise it)")
                        break
                    if r.status_code != 200:
                        out.errors.append(f"github HTTP {r.status_code}")
                        continue
                    items = r.json().get("items", []) or []
                    out.queries.append((f"topic:{topic}", len(items)))
                    for repo in items:
                        owner = repo.get("owner") or {}
                        # Only organisations - a personal repo is not a company.
                        if owner.get("type") != "Organization":
                            continue
                        login = owner.get("login")
                        if not login or login.lower() in seen:
                            continue
                        seen.add(login.lower())
                        p = self._to_prospect(repo, owner, topic)
                        if p:
                            out.prospects.append(p)
                            if len(out.prospects) >= limit:
                                break
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"github: {type(exc).__name__}: {exc}")
        return out

    def _to_prospect(self, repo: dict, owner: dict, topic: str) -> Prospect | None:
        login = owner.get("login", "")
        url = owner.get("html_url") or ""
        homepage = (repo.get("homepage") or "").strip()
        return Prospect(
            name=login, source_kind="github", source_url=url,
            country="US",  # unknown until the website audit resolves it
            website=homepage or None, category="technology",
            industry="software",
            excerpt=f"{repo.get('full_name')}: {repo.get('description') or ''}",
            identifiers=[("github_org", login.lower())],
            signals=[RawSignal(
                signal_type="public_post", evidence_class=INFERRED,
                quote=(f"{login} maintains the public repository "
                       f"{repo.get('full_name')} tagged '{topic}' "
                       f"({repo.get('stargazers_count', 0)} stars), last updated "
                       f"{(repo.get('pushed_at') or '')[:10]}."),
                observed_at=utcnow(), source_url=repo.get("html_url", url),
                confidence=0.6,
            )],
        )


# ---------------------------------------------------------------------------
# Yelp - US small business coverage
# ---------------------------------------------------------------------------

YELP_URL = "https://api.yelp.com/v3/businesses/search"
YELP_TERMS = {
    "web_development": ["restaurants", "cafes", "salons", "gyms", "dentists",
                        "auto repair", "florists", "bakeries"],
}


class YelpSource:
    key = "yelp"
    label = "Yelp — US local businesses"
    needs_key = True
    countries = ["US", "IE", "GB"]
    services = list(YELP_TERMS)

    def available(self) -> bool:
        return bool(getattr(settings, "yelp_api_key", ""))

    def discover(
        self, *, service_category: str, keywords: list[str], countries: list[str],
        locations: list[str], limit: int,
    ) -> SourceResult:
        out = SourceResult()
        key = getattr(settings, "yelp_api_key", "")
        terms = keywords or YELP_TERMS.get(service_category, [])
        if not key or not terms:
            return out
        seen: set[str] = set()
        try:
            with httpx.Client(timeout=30, headers={"Authorization": f"Bearer {key}"}) as c:
                for loc in (locations or ["New York"]):
                    for term in terms[:5]:
                        if len(out.prospects) >= limit:
                            break
                        r = c.get(YELP_URL, params={
                            "term": term, "location": loc, "limit": 40,
                        })
                        if r.status_code != 200:
                            out.errors.append(f"yelp HTTP {r.status_code}")
                            continue
                        items = r.json().get("businesses", []) or []
                        out.queries.append((f"{term} in {loc}", len(items)))
                        for b in items:
                            bid = b.get("id")
                            if not bid or bid in seen or b.get("is_closed"):
                                continue
                            seen.add(bid)
                            loc_info = b.get("location") or {}
                            p = Prospect(
                                name=b.get("name", ""), source_kind="yelp",
                                source_url=b.get("url", ""),
                                country=(loc_info.get("country") or "US").upper(),
                                city=loc_info.get("city") or loc,
                                address=", ".join(loc_info.get("display_address") or []),
                                phone=b.get("phone") or None,
                                category=", ".join(
                                    cat.get("title", "") for cat in (b.get("categories") or [])[:2]
                                ) or term,
                                industry=term,
                                extra={"rating": b.get("rating"),
                                       "reviews": b.get("review_count")},
                            )
                            p.identifiers.append(("yelp_id", bid))
                            out.prospects.append(p)
                            if len(out.prospects) >= limit:
                                break
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"yelp: {type(exc).__name__}: {exc}")
        return out


register(SerperSource())
register(GitHubSource())
