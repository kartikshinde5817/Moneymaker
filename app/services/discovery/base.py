"""Common shapes for every discovery source.

A source's only job is to return `Prospect` objects with whatever evidence it
saw. It does not decide whether the prospect is any good - qualification,
scoring and deduplication happen later, in one place, for every source.

Adding a new source means writing one function that returns Prospects and
registering it. Nothing else in the pipeline changes.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Callable, Protocol

WEB = "web_development"
CV = "computer_vision"
AI_AGENT = "ai_agent"
AI_AUTO = "ai_automation"
DM = "digital_marketing"

EXPLICIT = "explicit_request"
INFERRED = "inferred_opportunity"


@dataclass
class RawSignal:
    """Evidence found at discovery time, before anything is stored."""

    signal_type: str
    quote: str
    evidence_class: str = INFERRED
    service_category: str | None = None
    observed_at: dt.datetime | None = None
    confidence: float = 1.0
    source_url: str = ""


@dataclass
class Prospect:
    name: str
    source_kind: str
    source_url: str
    country: str = "GB"
    city: str | None = None
    address: str | None = None
    website: str | None = None
    phone: str | None = None
    email: str | None = None
    contact_name: str | None = None
    contact_title: str | None = None
    industry: str | None = None
    category: str | None = None
    # (kind, value) pairs feeding company_identifiers - this is what dedups
    identifiers: list[tuple[str, str]] = field(default_factory=list)
    signals: list[RawSignal] = field(default_factory=list)
    excerpt: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class SourceResult:
    prospects: list[Prospect] = field(default_factory=list)
    queries: list[tuple[str, int]] = field(default_factory=list)  # (query, n_results)
    errors: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


class DiscoverySource(Protocol):
    key: str
    label: str
    needs_key: bool

    def available(self) -> bool: ...

    def discover(
        self, *, service_category: str, keywords: list[str], countries: list[str],
        locations: list[str], limit: int,
    ) -> SourceResult: ...


REGISTRY: dict[str, "DiscoverySource"] = {}


def register(source: "DiscoverySource") -> "DiscoverySource":
    REGISTRY[source.key] = source
    return source


def get_source(key: str) -> "DiscoverySource | None":
    return REGISTRY.get(key)


def available_sources() -> list[dict]:
    return [
        {
            "key": s.key,
            "label": s.label,
            "needs_key": s.needs_key,
            "available": s.available(),
            "countries": getattr(s, "countries", []),
            "services": getattr(s, "services", []),
        }
        for s in REGISTRY.values()
    ]


# Keyword groups per service. Used by sources that take free-text queries.
KEYWORD_GROUPS: dict[str, list[str]] = {
    WEB: [
        "website developer", "web development", "website redesign",
        "new website", "ecommerce website", "online booking system",
        "online ordering system", "web design agency", "website required",
    ],
    CV: [
        "computer vision", "OpenCV", "YOLO", "PPE detection",
        "helmet detection", "fire detection", "smoke detection",
        "safety monitoring", "CCTV analytics", "machine vision",
        "visual inspection", "quality inspection", "defect detection",
        "worker safety", "industrial automation",
    ],
    AI_AGENT: [
        "AI agent", "AI chatbot", "voice AI", "AI calling agent",
        "conversational AI", "RAG chatbot", "AI assistant",
        "customer support automation", "WhatsApp automation",
    ],
    AI_AUTO: [
        "AI automation", "workflow automation", "business process automation",
        "sales automation", "lead qualification automation", "LLM integration",
    ],
    DM: [
        "digital marketing", "SEO", "social media marketing", "Google Ads",
        "marketing agency", "social media manager", "PPC", "content marketing",
        "local SEO",
    ],
}


def keywords_for(service_category: str, override: list[str] | None = None) -> list[str]:
    if override:
        return [k for k in override if k.strip()]
    if service_category in KEYWORD_GROUPS:
        return KEYWORD_GROUPS[service_category]
    # Imported here: catalog reads the database, which discovery must not need at import time.
    from app.services import catalog
    return catalog.keywords_for(service_category) or [service_category.replace("_", " ")]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
