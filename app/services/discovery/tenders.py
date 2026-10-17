"""Public procurement notices - the highest-quality source in the system.

A tender notice is a buying organisation publishing, in its own words, what it
needs, with a named contact and a real email address, specifically so that
suppliers get in touch. There is no ambiguity about intent and no question
about whether contact is welcome.

  UK  Contracts Finder, OCDS endpoint - free, no API key
  US  SAM.gov opportunities API       - free, API key required

Every signal from here is `explicit_request`, because the buyer literally
wrote the requirement down.
"""

from __future__ import annotations

import datetime as dt

import httpx

from app.config import settings
from app.services.discovery.base import (
    EXPLICIT,
    Prospect,
    RawSignal,
    SourceResult,
    keywords_for,
    register,
    utcnow,
)

CF_URL = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
CF_NOTICE = "https://www.contractsfinder.service.gov.uk/Notice/"
# Find a Tender carries UK contracts above the old EU thresholds, so it is
# where the larger industrial and CCTV work appears. Same OCDS shape, so the
# parser below is shared.
FTS_URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
FTS_NOTICE = "https://www.find-tender.service.gov.uk/Notice/"
SAM_URL = "https://api.sam.gov/opportunities/v2/search"


def _matches(text: str, keywords: list[str]) -> str | None:
    """Return the keyword that matched, so the quote can be justified."""
    low = (text or "").lower()
    for kw in keywords:
        if kw.lower() in low:
            return kw
    return None


def _excerpt_around(text: str, keyword: str, width: int = 220) -> str:
    low, kw = (text or "").lower(), keyword.lower()
    i = low.find(kw)
    if i < 0:
        return (text or "")[:width]
    start = max(0, i - width // 2)
    return ("…" if start else "") + text[start : start + width].strip() + "…"


class ContractsFinderSource:
    key = "contracts_finder"
    label = "UK Contracts Finder (public tenders)"
    needs_key = False
    countries = ["GB"]
    services = ["web_development", "computer_vision", "ai_agent", "ai_automation"]

    def available(self) -> bool:
        return True

    def discover(
        self, *, service_category: str, keywords: list[str], countries: list[str],
        locations: list[str], limit: int,
    ) -> SourceResult:
        out = SourceResult()
        if "GB" not in [c.upper() for c in countries]:
            return out
        kws = keywords_for(service_category, keywords)
        since = (dt.date.today() - dt.timedelta(days=120)).isoformat()
        seen: set[str] = set()

        try:
            with httpx.Client(timeout=45, headers={"User-Agent": settings.scraper_user_agent}) as c:
                # The OCDS endpoint paginates via a `links.next` cursor.
                url: str | None = CF_URL
                params: dict | None = {
                    "publishedFrom": since,
                    "stages": "tender",
                    "limit": 100,
                }
                pages = 0
                while url and pages < 5 and len(out.prospects) < limit:
                    r = c.get(url, params=params)
                    params = None  # the `next` link already carries them
                    if r.status_code != 200:
                        out.errors.append(f"contracts_finder HTTP {r.status_code}")
                        break
                    data = r.json()
                    releases = data.get("releases", [])
                    out.queries.append((f"tenders since {since} (page {pages + 1})", len(releases)))
                    for rel in releases:
                        p = self._to_prospect(rel, kws)
                        if p and p.source_url not in seen:
                            seen.add(p.source_url)
                            out.prospects.append(p)
                            if len(out.prospects) >= limit:
                                break
                    url = (data.get("links") or {}).get("next")
                    pages += 1
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"contracts_finder: {type(exc).__name__}: {exc}")
        return out

    def _to_prospect(self, release: dict, keywords: list[str]) -> Prospect | None:
        tender = release.get("tender") or {}
        title = tender.get("title") or ""
        desc = tender.get("description") or ""
        blob = f"{title}\n{desc}"
        matched = _matches(blob, keywords)
        if not matched:
            return None

        buyer = release.get("buyer") or {}
        parties = release.get("parties") or []
        party = next((p for p in parties if p.get("id") == buyer.get("id")), None) or (
            parties[0] if parties else {}
        )
        contact = party.get("contactPoint") or {}
        addr = party.get("address") or {}

        name = buyer.get("name") or party.get("name") or ""
        if not name:
            return None

        ocid = release.get("ocid") or release.get("id") or ""
        url = f"{CF_NOTICE}{ocid}"
        published = tender.get("datePublished") or release.get("date")
        observed = utcnow()
        if published:
            try:
                observed = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                pass

        website = ((party.get("details") or {}).get("url")) or None

        p = Prospect(
            name=name,
            source_kind="tender",
            source_url=url,
            country="GB",
            city=addr.get("locality"),
            address=", ".join(
                x for x in (addr.get("streetAddress"), addr.get("locality"), addr.get("postalCode")) if x
            )
            or None,
            website=website,
            phone=contact.get("telephone"),
            email=(contact.get("email") or "").strip().lower() or None,
            contact_name=contact.get("name"),
            contact_title="Procurement contact",
            industry=(tender.get("classification") or {}).get("description"),
            category="public sector",
            excerpt=blob[:2000],
        )
        if ocid:
            p.identifiers.append(("tender_ocid", ocid))
        if buyer.get("id"):
            p.identifiers.append(("registry_no", buyer["id"]))
        p.signals.append(
            RawSignal(
                signal_type="tender_match",
                evidence_class=EXPLICIT,
                quote=f'Tender "{title}": {_excerpt_around(desc or title, matched)}',
                service_category=None,
                observed_at=observed,
                source_url=url,
            )
        )
        return p


class SamGovSource:
    key = "sam_gov"
    label = "US SAM.gov (federal opportunities)"
    needs_key = True
    countries = ["US"]
    services = ["web_development", "computer_vision", "ai_agent", "ai_automation"]

    def available(self) -> bool:
        return bool(getattr(settings, "sam_gov_api_key", "") or "")

    def discover(
        self, *, service_category: str, keywords: list[str], countries: list[str],
        locations: list[str], limit: int,
    ) -> SourceResult:
        out = SourceResult()
        api_key = getattr(settings, "sam_gov_api_key", "") or ""
        if not api_key or "US" not in [c.upper() for c in countries]:
            return out
        kws = keywords_for(service_category, keywords)
        posted_from = (dt.date.today() - dt.timedelta(days=90)).strftime("%m/%d/%Y")
        posted_to = dt.date.today().strftime("%m/%d/%Y")

        try:
            with httpx.Client(timeout=45, headers={"User-Agent": settings.scraper_user_agent}) as c:
                for kw in kws[:6]:
                    if len(out.prospects) >= limit:
                        break
                    r = c.get(
                        SAM_URL,
                        params={
                            "api_key": api_key, "postedFrom": posted_from,
                            "postedTo": posted_to, "title": kw, "limit": 50,
                        },
                    )
                    if r.status_code != 200:
                        out.errors.append(f"sam_gov HTTP {r.status_code} for '{kw}'")
                        continue
                    items = r.json().get("opportunitiesData", []) or []
                    out.queries.append((kw, len(items)))
                    for it in items:
                        office = it.get("fullParentPathName", "") or it.get("organizationName", "")
                        if not office:
                            continue
                        contacts = it.get("pointOfContact") or []
                        poc = contacts[0] if contacts else {}
                        url = it.get("uiLink") or ""
                        p = Prospect(
                            name=office.split(".")[-1].strip() or office,
                            source_kind="tender", source_url=url, country="US",
                            email=(poc.get("email") or "").strip().lower() or None,
                            phone=poc.get("phone"), contact_name=poc.get("fullName"),
                            contact_title=poc.get("title") or "Contracting officer",
                            category="public sector",
                            excerpt=(it.get("title") or "")[:2000],
                        )
                        if it.get("noticeId"):
                            p.identifiers.append(("tender_ocid", it["noticeId"]))
                        p.signals.append(
                            RawSignal(
                                signal_type="tender_match", evidence_class=EXPLICIT,
                                quote=f'SAM.gov notice "{it.get("title", "")}"',
                                observed_at=utcnow(), source_url=url,
                            )
                        )
                        out.prospects.append(p)
                        if len(out.prospects) >= limit:
                            break
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"sam_gov: {type(exc).__name__}: {exc}")
        return out


class FindATenderSource(ContractsFinderSource):
    """Higher-value UK contracts. Free, no key, same OCDS format."""

    key = "find_a_tender"
    label = "UK Find a Tender (high-value contracts)"
    needs_key = False
    countries = ["GB"]

    def discover(
        self, *, service_category: str, keywords: list[str], countries: list[str],
        locations: list[str], limit: int,
    ) -> SourceResult:
        out = SourceResult()
        if "GB" not in [c.upper() for c in countries]:
            return out
        kws = keywords_for(service_category, keywords)
        since = (dt.date.today() - dt.timedelta(days=120)).strftime("%Y-%m-%dT00:00:00")
        seen: set[str] = set()
        try:
            with httpx.Client(timeout=45, headers={"User-Agent": settings.scraper_user_agent}) as c:
                url: str | None = FTS_URL
                params: dict | None = {"updatedFrom": since, "limit": 100}
                pages = 0
                while url and pages < 4 and len(out.prospects) < limit:
                    r = c.get(url, params=params)
                    params = None
                    if r.status_code != 200:
                        out.errors.append(f"find_a_tender HTTP {r.status_code}")
                        break
                    data = r.json()
                    releases = data.get("releases", [])
                    out.queries.append((f"FTS since {since[:10]} (page {pages + 1})", len(releases)))
                    for rel in releases:
                        p = self._to_prospect(rel, kws)
                        if p and p.source_url not in seen:
                            seen.add(p.source_url)
                            p.source_url = p.source_url.replace(CF_NOTICE, FTS_NOTICE)
                            out.prospects.append(p)
                            if len(out.prospects) >= limit:
                                break
                    url = (data.get("links") or {}).get("next")
                    pages += 1
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"find_a_tender: {type(exc).__name__}: {exc}")
        return out


register(ContractsFinderSource())
register(FindATenderSource())
register(SamGovSource())
