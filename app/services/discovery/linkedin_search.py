"""Finding LinkedIn buying signals without touching LinkedIn.

The distinction that makes this lawful:

  Scraping LinkedIn means your software connects to linkedin.com and reads
  pages. That breaches their User Agreement and gets accounts and IPs banned.
  This module never connects to linkedin.com. Not once.

  What it does instead is query a *search engine* for LinkedIn pages that the
  engine has already crawled and indexed, and use only the title and snippet
  the engine returns. Google crawled it; we read Google's index. No LinkedIn
  request is made, no LinkedIn terms apply, no account is at risk.

That gives real LinkedIn buying signals - someone posting "we need a web
developer" - with the post URL for you to open yourself.

The catch, stated plainly: a search snippet contains no email address. Since
leads without a contact route are discarded, each hit triggers one follow-up
search for the company's own website, which the audit then mines for an
address. That costs two search credits per lead instead of one.

Requires SERPER_API_KEY. Coverage is limited to what the engine has indexed,
which for LinkedIn means public posts and company pages, not member profiles.
"""

from __future__ import annotations

import re

import httpx

from app.config import settings
from app.services.discovery.base import (
    EXPLICIT,
    Prospect,
    RawSignal,
    SourceResult,
    register,
    utcnow,
)
from app.services.research.fetcher import registrable_domain

SERPER_URL = "https://google.serper.dev/search"

# Restricted to the surfaces search engines actually index.
LINKEDIN_SCOPES = ("site:linkedin.com/posts", "site:linkedin.com/pulse")

INTENT = {
    "web_development": [
        '"looking for a web developer"', '"need a website built"',
        '"looking for a web design agency"', '"hiring a web developer"',
        '"need an ecommerce website"', '"website redesign" recommendations',
    ],
    "computer_vision": [
        '"looking for a computer vision"', '"need PPE detection"',
        '"looking for CCTV analytics"', '"machine vision" "looking for a partner"',
        '"need object detection"', '"looking for a computer vision partner"',
    ],
    "ai_agent": [
        '"looking for an AI chatbot"', '"need an AI agent"',
        '"looking for a voice AI"', '"need a chatbot built"',
        '"looking for an AI development partner"',
    ],
    "ai_automation": [
        '"looking for AI automation"', '"need workflow automation"',
        '"looking for an automation partner"',
    ],
}

COUNTRY_GL = {
    "GB": "uk", "US": "us", "IE": "ie", "AE": "ae", "AU": "au",
    "NZ": "nz", "SG": "sg", "IN": "in", "ZA": "za", "NL": "nl",
}

# "Jane Doe on LinkedIn: we're hiring..."  /  "Acme Ltd | LinkedIn"
# /  "John Smith - CTO - Acme Ltd | LinkedIn"
TITLE_PATTERNS = (
    re.compile(r"^(?P<person>.+?)\s+on\s+LinkedIn", re.I),
    re.compile(r"^(?P<person>.+?)\s+-\s+.+?\s+-\s+(?P<company>.+?)\s*\|\s*LinkedIn", re.I),
    re.compile(r"^(?P<company>.+?)\s*\|\s*LinkedIn", re.I),
)

NOT_A_COMPANY = re.compile(
    r"^(jobs?|careers?|search|posts?|feed|people|learning|events?|groups?|"
    r"linkedin|sign in|log in)$", re.I
)


def _parse_title(title: str) -> tuple[str, str]:
    """Return (company, person) from a LinkedIn search-result title."""
    title = (title or "").strip()
    for pattern in TITLE_PATTERNS:
        m = pattern.match(title)
        if not m:
            continue
        groups = m.groupdict()
        company = (groups.get("company") or "").strip(" -–—|")
        person = (groups.get("person") or "").strip(" -–—|")
        if company and not NOT_A_COMPANY.match(company):
            return company[:160], person[:120]
        if person and not NOT_A_COMPANY.match(person):
            # "Jane Doe on LinkedIn" - the poster, company unknown for now.
            return "", person[:120]
    return "", ""


class LinkedInSearchSource:
    key = "linkedin_search"
    label = "LinkedIn signals via search index (never scrapes LinkedIn)"
    needs_key = True
    countries = list(COUNTRY_GL)
    services = list(INTENT)

    def available(self) -> bool:
        return bool(settings.serper_api_key)

    def discover(
        self, *, service_category: str, keywords: list[str], countries: list[str],
        locations: list[str], limit: int,
    ) -> SourceResult:
        out = SourceResult()
        if not self.available():
            return out
        phrases = keywords or INTENT.get(service_category, [])
        if not phrases:
            return out

        seen: set[str] = set()
        try:
            with httpx.Client(timeout=30) as client:
                for country in (countries or ["GB"]):
                    gl = COUNTRY_GL.get(country.upper())
                    if not gl:
                        continue
                    for phrase in phrases[:4]:
                        if len(out.prospects) >= limit:
                            break
                        query = f"{LINKEDIN_SCOPES[0]} {phrase}"
                        hits = self._search(client, query, gl, out)
                        for hit in hits:
                            p = self._to_prospect(hit, phrase, country.upper(), seen)
                            if not p:
                                continue
                            # A snippet has no email. Find their own site so the
                            # audit can, otherwise the lead gets discarded for
                            # having no contact route.
                            self._attach_website(client, p, gl, out)
                            out.prospects.append(p)
                            if len(out.prospects) >= limit:
                                break
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"linkedin_search: {type(exc).__name__}: {exc}")
        return out

    def _search(self, client: httpx.Client, query: str, gl: str, out: SourceResult) -> list[dict]:
        r = client.post(
            SERPER_URL,
            headers={"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"},
            json={"q": query, "gl": gl, "hl": "en", "num": 20, "tbs": "qdr:y"},
        )
        if r.status_code != 200:
            out.errors.append(f"linkedin_search HTTP {r.status_code}")
            return []
        hits = r.json().get("organic") or []
        out.queries.append((query, len(hits)))
        out.cost_usd += 0.001
        return hits

    def _to_prospect(self, hit: dict, phrase: str, country: str, seen: set) -> Prospect | None:
        link = hit.get("link") or ""
        if "linkedin.com" not in link:
            return None
        title = hit.get("title") or ""
        snippet = (hit.get("snippet") or "").strip()
        company, person = _parse_title(title)
        name = company or person
        if not name or name.lower() in seen:
            return None
        seen.add(name.lower())

        return Prospect(
            name=name,
            source_kind="linkedin_search",
            source_url=link,
            country=country,
            contact_name=person or None,
            category="linkedin post",
            excerpt=f"{title}\n{snippet}",
            identifiers=[("linkedin_url", link)] if "/company/" in link else [],
            signals=[RawSignal(
                signal_type="explicit_request",
                evidence_class=EXPLICIT,
                quote=(f'LinkedIn post indexed by search for {phrase}: '
                       f'"{snippet[:260]}"'),
                observed_at=utcnow(), source_url=link, confidence=0.7,
            )],
        )

    def _attach_website(self, client: httpx.Client, p: Prospect, gl: str, out: SourceResult) -> None:
        """One follow-up search for the company's own site."""
        try:
            r = client.post(
                SERPER_URL,
                headers={"X-API-KEY": settings.serper_api_key,
                         "Content-Type": "application/json"},
                json={"q": f'"{p.name}" official website -site:linkedin.com',
                      "gl": gl, "hl": "en", "num": 5},
            )
            if r.status_code != 200:
                return
            out.cost_usd += 0.001
            for hit in (r.json().get("organic") or []):
                domain = registrable_domain(hit.get("link") or "")
                if not domain:
                    continue
                if domain in ("linkedin.com", "facebook.com", "crunchbase.com",
                              "bloomberg.com", "glassdoor.com", "indeed.com",
                              "wikipedia.org", "youtube.com", "x.com", "twitter.com"):
                    continue
                p.website = f"https://{domain}"
                return
        except Exception:
            return  # no website found - the contact gate will drop it


register(LinkedInSearchSource())
